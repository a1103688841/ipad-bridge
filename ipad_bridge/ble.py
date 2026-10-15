"""This PC as a Bluetooth LE keyboard + mouse (HID over GATT) using the built-in Windows stack.

All WinRT calls run on a private asyncio thread. The public methods are thread-safe and only
record what should be sent; a single sender coroutine turns that into HID input reports,
merging mouse motion while a previous notification is still in flight.
"""
import asyncio
import collections
import logging
import threading
import uuid

from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothError
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties as Props,
    GattLocalCharacteristicParameters,
    GattLocalDescriptorParameters,
    GattProtectionLevel,
    GattServiceProvider,
    GattServiceProviderAdvertisingParameters,
    GattSessionStatus,
)
from winrt.windows.storage.streams import DataWriter

from . import hid

log = logging.getLogger(__name__)

ENCRYPTED = GattProtectionLevel.ENCRYPTION_REQUIRED


def uuid16(value):
    return uuid.UUID("0000%04x-0000-1000-8000-00805f9b34fb" % value)


def to_buffer(data):
    writer = DataWriter()
    writer.write_bytes(bytes(data))
    return writer.detach_buffer()


def clamp(value, low=-127, high=127):
    return max(low, min(high, value))


class BleHid:
    def __init__(self, on_connection_change=None):
        self._on_connection_change = on_connection_change
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
        try:
            await self._send_loop()
        finally:
            for provider in (self._hid, self._battery):
                if provider is not None:
                    provider.stop_advertising()
            log.info("advertising stopped")

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

    def _next_report(self):
        with self._lock:
            if self._kbd_reports:
                return self._kbd, self._kbd_reports.popleft()
            if self._button_states:
                buttons = self._button_states.popleft()
            elif self._dx or self._dy or self._wheel:
                buttons = self._buttons
            else:
                return None
            dx, dy, wheel = clamp(self._dx), clamp(self._dy), clamp(self._wheel)
            self._dx -= dx
            self._dy -= dy
            self._wheel -= wheel
        return self._mouse, bytes([buttons, dx & 0xFF, dy & 0xFF, wheel & 0xFF])

    async def _send_loop(self):
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            self._check_connection()
            while not self._stopping:
                item = self._next_report()
                if item is None:
                    break
                char, report = item
                try:
                    results = await char.notify_value_async(to_buffer(report))
                except Exception as e:
                    log.warning("notify failed: %s", e)
                    continue
                for r in results:
                    if r.status != 0:  # GattCommunicationStatus.SUCCESS
                        log.warning("notify -> %s (protocol error %s)", r.status, r.protocol_error)

    def _check_connection(self):
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
            if self._on_connection_change:
                self._on_connection_change(connected)
