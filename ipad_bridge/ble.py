"""This PC as a Bluetooth LE keyboard + mouse (HID over GATT) using the built-in Windows stack.

All WinRT calls run on a private asyncio thread. The public methods are thread-safe and only
record what should be sent; a single sender coroutine turns that into HID input reports,
merging mouse motion while a previous notification is still in flight.
"""
import asyncio
import collections
import logging
import threading
import time
import uuid

from winrt.windows.devices.bluetooth import (
    BluetoothAdapter,
    BluetoothConnectionStatus,
    BluetoothError,
    BluetoothLEDevice,
)
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties as Props,
    GattLocalCharacteristicParameters,
    GattLocalDescriptorParameters,
    GattProtectionLevel,
    GattServiceProvider,
    GattServiceProviderAdvertisementStatus,
    GattServiceProviderAdvertisingParameters,
    GattSessionStatus,
)
from winrt.windows.devices.radios import Radio, RadioKind, RadioState
from winrt.windows.storage.streams import DataWriter

from . import hid

log = logging.getLogger(__name__)

ENCRYPTED = GattProtectionLevel.ENCRYPTION_REQUIRED
CONNECTION_POLL = 0.5  # seconds between checks of who is subscribed
READVERTISE_INTERVAL = 5.0  # seconds between attempts to restart dropped advertising
STALE_LINK_WAIT = 6.0  # after start, how long the last iPad may stay linked without subscribing


def uuid16(value):
    return uuid.UUID("0000%04x-0000-1000-8000-00805f9b34fb" % value)


def to_buffer(data):
    writer = DataWriter()
    writer.write_bytes(bytes(data))
    return writer.detach_buffer()


def clamp(value, low=-127, high=127):
    return max(low, min(high, value))


class _Stats:
    """Logs how fast reports actually go out, every few seconds while sending."""

    PERIOD = 5.0

    def __init__(self):
        self._reset(time.perf_counter())

    def _reset(self, now):
        self.since = now
        self.motion = self.other = 0
        self.total = self.worst = 0.0

    def add(self, is_motion, seconds):
        if is_motion:
            self.motion += 1
        else:
            self.other += 1
        self.total += seconds
        self.worst = max(self.worst, seconds)
        now = time.perf_counter()
        span = now - self.since
        if span >= self.PERIOD:
            n = self.motion + self.other
            log.info("sent %.0f motion/s + %d other in %.1fs, notify avg %.1f ms, worst %.1f ms",
                     self.motion / span, self.other, span, self.total / n * 1000, self.worst * 1000)
            self._reset(now)


class BleHid:
    def __init__(self, on_connection_change=None, state_path=None):
        self._on_connection_change = on_connection_change
        self._state_path = state_path  # remembers the last iPad, to recover it after a restart
        self._resetting = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ble", daemon=True)
        self._ready = threading.Event()
        self._start_error = None
        self._stopping = False
        self._wake = None

        self._lock = threading.Lock()
        self._kbd_reports = collections.deque()
        self._button_states = collections.deque()
        self._buttons = 0
        self._dx = self._dy = self._wheel = 0

        self.motion_interval = 0.010
        self._last_motion = 0.0
        self._next_readvertise = 0.0
        self._stats = _Stats()

        self._hid = self._battery = None
        self._kbd = self._mouse = None
        self.connected = False
        self.device_ids = []

    # ---- lifecycle -------------------------------------------------------------------------

    def start(self, timeout=20):
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("蓝牙服务启动超时")
        if self._start_error:
            raise self._start_error

    def stop(self):
        self._stopping = True
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._wake.set)
        self._thread.join(timeout=3)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        self._wake = asyncio.Event()
        try:
            await self._build()
        except Exception as e:  # reported to the caller of start()
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()
        heal = self._loop.create_task(self._heal_stale_link())
        try:
            await self._send_loop()
        finally:
            heal.cancel()
            self._stop_providers()
            log.info("advertising stopped")

    def _stop_providers(self):
        for provider in (self._hid, self._battery):
            if provider is not None:
                try:
                    provider.stop_advertising()
                except Exception:
                    pass

    # ---- recovery ------------------------------------------------------------------------------

    async def _heal_stale_link(self):
        """After this program restarts, the iPad can stay linked to Windows (the link belongs to
        the OS, not to us) without subscribing to the new HID service, so it never comes back.
        Cycling the radio drops that link and the iPad then reconnects properly."""
        await asyncio.sleep(STALE_LINK_WAIT)
        if self.connected or self._stopping or not self._state_path:
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                last = f.read().strip()
        except OSError:
            return
        if not last:
            return
        try:
            device = await BluetoothLEDevice.from_id_async(last)
        except Exception as e:
            log.info("cannot look up last iPad %s: %s", last, e)
            return
        if device is None or device.connection_status != BluetoothConnectionStatus.CONNECTED:
            return
        log.warning("iPad %s is linked but not subscribed, resetting Bluetooth", last)
        await self._reset_radio()

    def reset_bluetooth(self):
        """Thread-safe: turn the Bluetooth radio off and on, then publish the HID service again."""
        asyncio.run_coroutine_threadsafe(self._reset_radio(), self._loop)

    async def _reset_radio(self):
        if self._resetting:
            return
        self._resetting = True
        try:
            await Radio.request_access_async()
            radios = await Radio.get_radios_async()
            radio = next((r for r in radios if r.kind == RadioKind.BLUETOOTH), None)
            if radio is None:
                log.warning("no Bluetooth radio to reset")
                return
            log.info("Bluetooth radio off")
            await radio.set_state_async(RadioState.OFF)
            await asyncio.sleep(2)
            await radio.set_state_async(RadioState.ON)
            log.info("Bluetooth radio on")
            await asyncio.sleep(2)
            self._stop_providers()
            await self._build()
        except Exception:
            log.exception("Bluetooth reset failed")
        finally:
            self._resetting = False

    async def _build(self):
        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None:
            raise RuntimeError("这台电脑没有可用的蓝牙适配器")
        if not adapter.is_peripheral_role_supported:
            raise RuntimeError("蓝牙适配器不支持外设（Peripheral）模式，无法模拟键鼠")
        log.info("adapter %012X, peripheral role supported", adapter.bluetooth_address)

        # The order and properties below must stay the same: bonded iPads cache this table.
        self._hid = await self._new_service(0x1812)
        svc = self._hid.service
        await self._new_char(svc, 0x2A4A, Props.READ, bytes([0x11, 0x01, 0x00, 0x02]))  # HID Information
        await self._new_char(svc, 0x2A4B, Props.READ, hid.REPORT_MAP)                    # Report Map
        await self._new_char(svc, 0x2A4C, Props.WRITE_WITHOUT_RESPONSE, None)            # HID Control Point
        self._kbd = await self._new_char(svc, 0x2A4D, Props.READ | Props.NOTIFY, bytes(8))
        await self._add_report_reference(self._kbd, hid.KEYBOARD_REPORT_ID)
        self._mouse = await self._new_char(svc, 0x2A4D, Props.READ | Props.NOTIFY, bytes(4))
        await self._add_report_reference(self._mouse, hid.MOUSE_REPORT_ID)

        try:
            self._battery = await self._new_service(0x180F)
            await self._new_char(self._battery.service, 0x2A19, Props.READ, bytes([100]))
        except Exception as e:
            log.warning("battery service skipped: %s", e)
            self._battery = None

        params = GattServiceProviderAdvertisingParameters()
        params.is_connectable = True
        params.is_discoverable = True
        self._hid.start_advertising_with_parameters(params)
        if self._battery is not None:
            quiet = GattServiceProviderAdvertisingParameters()
            quiet.is_connectable = True
            quiet.is_discoverable = False
            self._battery.start_advertising_with_parameters(quiet)
        log.info("HID service published, advertising status %s", self._hid.advertisement_status)

    async def _new_service(self, short_uuid):
        result = await GattServiceProvider.create_async(uuid16(short_uuid))
        if result.error != BluetoothError.SUCCESS:
            raise RuntimeError("创建 GATT 服务 0x%04X 失败：%s" % (short_uuid, result.error))
        return result.service_provider

    async def _new_char(self, service, short_uuid, props, static):
        p = GattLocalCharacteristicParameters()
        p.characteristic_properties = props
        p.read_protection_level = ENCRYPTED
        p.write_protection_level = ENCRYPTED
        if static is not None:
            p.static_value = to_buffer(static)
        result = await service.create_characteristic_async(uuid16(short_uuid), p)
        if result.error != BluetoothError.SUCCESS:
            raise RuntimeError("创建特征 0x%04X 失败：%s" % (short_uuid, result.error))
        return result.characteristic

    async def _add_report_reference(self, char, report_id):
        p = GattLocalDescriptorParameters()
        p.read_protection_level = ENCRYPTED
        p.static_value = to_buffer([report_id, 0x01])  # input report
        result = await char.create_descriptor_async(uuid16(0x2908), p)
        if result.error != BluetoothError.SUCCESS:
            raise RuntimeError("创建 Report Reference 失败：%s" % result.error)

    # ---- thread-safe API ---------------------------------------------------------------------

    def add_motion(self, dx, dy, wheel=0):
        with self._lock:
            self._dx += dx
            self._dy += dy
            self._wheel += wheel
        self._kick()

    def set_buttons(self, buttons):
        with self._lock:
            self._buttons = buttons
            self._button_states.append(buttons)
        self._kick()

    def send_keyboard(self, modifiers, keys):
        report = bytes([modifiers, 0] + list(keys)[:6] + [0] * (6 - min(6, len(keys))))
        with self._lock:
            self._kbd_reports.append(report)
        self._kick()

    def release_all(self):
        """Drop pending motion and tell the iPad that every key and button is up."""
        with self._lock:
            self._dx = self._dy = self._wheel = 0
            self._buttons = 0
            self._button_states.append(0)
            self._kbd_reports.append(bytes(8))
        self._kick()

    def _kick(self):
        if self._wake is not None and not self._stopping:
            self._loop.call_soon_threadsafe(self._wake.set)

    # ---- sender -------------------------------------------------------------------------------

    def _next_report(self, now):
        """(characteristic, report, is_motion), seconds to wait before the next motion report, or None."""
        with self._lock:
            if self._kbd_reports:
                return self._kbd, self._kbd_reports.popleft(), False
            if self._button_states:
                buttons = self._button_states.popleft()  # button changes go out right away, in order
            elif self._dx or self._dy or self._wheel:
                # Pace pure motion: merged deltas at a steady rate move the iPad pointer smoothly,
                # bursts of tiny reports make its acceleration curve jumpy.
                wait = self._last_motion + self.motion_interval - now
                if wait > 0:
                    return wait
                buttons = self._buttons
            else:
                return None
            dx, dy, wheel = clamp(self._dx), clamp(self._dy), clamp(self._wheel)
            self._dx -= dx
            self._dy -= dy
            self._wheel -= wheel
        self._last_motion = now
        return self._mouse, bytes([buttons, dx & 0xFF, dy & 0xFF, wheel & 0xFF]), True

    async def _send_loop(self):
        timeout = CONNECTION_POLL
        next_poll = 0.0
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            now = time.perf_counter()
            if now >= next_poll:
                self._check_connection()
                next_poll = now + CONNECTION_POLL
            timeout = max(0.001, next_poll - now)
            while not self._stopping:
                item = self._next_report(time.perf_counter())
                if item is None:
                    break
                if isinstance(item, float):
                    timeout = min(timeout, item)
                    break
                char, report, is_motion = item
                started = time.perf_counter()
                try:
                    results = await char.notify_value_async(to_buffer(report))
                except Exception as e:
                    log.warning("notify failed: %s", e)
                    continue
                self._stats.add(is_motion, time.perf_counter() - started)
                for r in results:
                    if r.status != 0:  # GattCommunicationStatus.SUCCESS
                        log.warning("notify -> %s (protocol error %s)", r.status, r.protocol_error)

    def _check_advertising(self):
        """Restart advertising if Windows dropped it (e.g. after the adapter stopped responding),
        otherwise a bonded iPad that went away can never find this PC again."""
        if self.connected or self._hid is None:
            return
        status = self._hid.advertisement_status
        if status in (GattServiceProviderAdvertisementStatus.STARTED,
                      GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA):
            return
        now = time.perf_counter()
        if now < self._next_readvertise:
            return
        self._next_readvertise = now + READVERTISE_INTERVAL
        log.warning("advertising is %s, restarting it", status)
        try:
            self._hid.stop_advertising()
        except Exception:
            pass
        params = GattServiceProviderAdvertisingParameters()
        params.is_connectable = True
        params.is_discoverable = True
        try:
            self._hid.start_advertising_with_parameters(params)
        except Exception as e:
            log.warning("restart advertising failed: %s", e)

    def _check_connection(self):
        self._check_advertising()
        ids = []
        for client in self._mouse.subscribed_clients:
            session = client.session
            if session.session_status == GattSessionStatus.ACTIVE:
                ids.append(session.device_id.id)
        connected = bool(ids)
        if connected != self.connected or ids != self.device_ids:
            self.connected = connected
            self.device_ids = ids
            log.info("iPad %s %s", "connected" if connected else "disconnected", ids)
            if connected and self._state_path:
                try:
                    with open(self._state_path, "w", encoding="utf-8") as f:
                        f.write(ids[0])
                except OSError as e:
                    log.warning("cannot save state: %s", e)
            if self._on_connection_change:
                self._on_connection_change(connected)
