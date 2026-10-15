"""Global keyboard/mouse hooks: decide whether input belongs to the PC or the iPad.

Everything here runs on the thread that installed the hooks (the main thread, which also
pumps its message queue). Other threads talk to it only through post().
"""
import ctypes
import logging
import os
import tempfile
import time
from ctypes import wintypes

from . import hid
from .win32 import (
    HOOKPROC, KBDLLHOOKSTRUCT, KEYEVENTF_KEYUP, LLKHF_EXTENDED, LLKHF_UP, MSLLHOOKSTRUCT,
    WH_KEYBOARD_LL, WH_MOUSE_LL, WM_APP, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDOWN,
    WM_MBUTTONUP, WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_QUIT, WM_RBUTTONDOWN, WM_RBUTTONUP,
    cursor_pos, get_pointer_ballistics, kernel32, monitor_rects, set_pointer_ballistics, user32,
)

log = logging.getLogger(__name__)

# Original Windows pointer settings while we have them switched off; survives a crash or kill.
BALLISTICS_FILE = os.path.join(tempfile.gettempdir(), "ipad-bridge-pointer.txt")


def restore_saved_ballistics():
    """Put back pointer settings left behind by a run that ended while controlling the iPad."""
    try:
        with open(BALLISTICS_FILE) as f:
            values = [int(v) for v in f.read().split()]
    except (OSError, ValueError):
        return
    if len(values) == 4:
        set_pointer_ballistics(*values)
        log.info("restored Windows pointer settings %s from a previous run", values)
    os.remove(BALLISTICS_FILE)


MSG_CONNECTION = WM_APP + 1
MSG_TOGGLE = WM_APP + 2

VK_MENU_MASK = 0xE8  # unassigned key, injected so a swallowed Alt/Win hotkey doesn't open menus
RETURN_GRACE = 0.3   # seconds after switching to the iPad during which we never switch back
TRACE_MOVES = 20     # log the first mouse moves of each iPad session (diagnostics)

_BUTTONS = {
    WM_LBUTTONDOWN: (hid.BUTTON_LEFT, True), WM_LBUTTONUP: (hid.BUTTON_LEFT, False),
    WM_RBUTTONDOWN: (hid.BUTTON_RIGHT, True), WM_RBUTTONUP: (hid.BUTTON_RIGHT, False),
    WM_MBUTTONDOWN: (hid.BUTTON_MIDDLE, True), WM_MBUTTONUP: (hid.BUTTON_MIDDLE, False),
}

_MOD_GROUPS = {"ctrl": hid.CTRL_KEYS, "alt": hid.ALT_KEYS, "shift": hid.SHIFT_KEYS, "win": hid.WIN_KEYS}


class Router:
    def __init__(self, cfg, ble):
        self.cfg = cfg
        self.ble = ble
        self.on_mode_change = None
        self.thread_id = kernel32.GetCurrentThreadId()
        self._mouse_proc = HOOKPROC(self._mouse_hook)  # keep references: ctypes callbacks
        self._kbd_proc = HOOKPROC(self._kbd_hook)
        self._hooks = []
        self._right = cfg.side == "right"

        self.ipad_mode = False
        self._monitors = monitor_rects()
        self._edge_count = 0
        self._entry = (0, 0)
        self._park = (0, 0)
        self._vx = 0.0           # distance of the virtual iPad pointer from the edge next to the PC
        self._return_push = 0
        self._frac = [0.0, 0.0]
        self._wheel_acc = 0
        self._saved_ballistics = None
        self._session = [0.0, 0]  # start time, mouse moves seen while on the iPad

        self._held = set()           # physically held keys (async key state is stale while swallowing)
        self._pc_keys = set()         # pressed while on the PC: their releases go to the PC
        self._ipad_keys_down = set()  # pressed while on the iPad: their releases never reach the PC
        self._hotkey_up = None
        self._pc_buttons = 0
        self._ipad_buttons = 0
        self._swallow_button_ups = 0
        self._mods = 0
        self._keys = []

    # ---- message loop --------------------------------------------------------------------------

    def install(self):
        hmod = kernel32.GetModuleHandleW(None)
        for kind, proc in ((WH_MOUSE_LL, self._mouse_proc), (WH_KEYBOARD_LL, self._kbd_proc)):
            h = user32.SetWindowsHookExW(kind, proc, hmod, 0)
            if not h:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hooks.append(h)
        log.info("hooks installed, side=%s hotkey=%s", self.cfg.side, self.cfg.hotkey)

    def uninstall(self):
        for h in self._hooks:
            user32.UnhookWindowsHookEx(h)
        self._hooks = []

    def post(self, msg, wparam=0):
        user32.PostThreadMessageW(self.thread_id, msg, wparam, 0)

    def quit(self):
        self.post(WM_QUIT)

    def run(self):
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == MSG_CONNECTION:
                if not msg.wParam and self.ipad_mode:
                    log.info("iPad disconnected while in iPad mode, back to PC")
                    self._exit()
                self._notify()
            elif msg.message == MSG_TOGGLE:
                if self.ipad_mode:
                    self._exit()
                else:
                    self._enter(cursor_pos(), from_edge=False)
        if self.ipad_mode:
            self._exit()

    def _notify(self):
        if self.on_mode_change:
            self.on_mode_change(self.ipad_mode)

    # ---- mode switching ------------------------------------------------------------------------

    def _monitor_at(self, x, y):
        for r in self._monitors:
            if r[0] <= x < r[2] and r[1] <= y < r[3]:
                return r
        return None

    def _enter(self, pos, from_edge):
        if not self.ble.connected:
            log.info("iPad not connected, staying on PC")
            return
        self._monitors = monitor_rects()
        rect = self._monitor_at(*pos) or self._monitors[0]
        self.ipad_mode = True
        self._entry = pos
        self._park = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        user32.SetCursorPos(*self._park)
        if self.cfg.disable_windows_accel:
            self._raw_pointer()
        self._frac = [0.0, 0.0]
        self._wheel_acc = 0
        self._return_push = 0
        self._ipad_buttons = 0
        if from_edge:
            # Pin the iPad pointer to the edge next to the PC so the virtual position starts in sync.
            self.ble.add_motion(-3000 if self._right else 3000, 0)
            self._vx = 0.0
        else:
            self._vx = self.cfg.ipad_width / 2.0
        self._session = [time.perf_counter(), 0]
        log.info("-> iPad (%s) at %s", "edge" if from_edge else "hotkey", pos)
        self._notify()

    def _raw_pointer(self):
        """Turn off Windows acceleration so the hook sees 1:1 mouse counts; the iPad adds its own."""
        self._saved_ballistics = get_pointer_ballistics()
        with open(BALLISTICS_FILE, "w") as f:
            f.write(" ".join(str(v) for v in self._saved_ballistics))
        set_pointer_ballistics(0, 0, 0, 10)

    def _restore_pointer(self):
        if self._saved_ballistics is None:
            return
        set_pointer_ballistics(*self._saved_ballistics)
        self._saved_ballistics = None
        try:
            os.remove(BALLISTICS_FILE)
        except OSError:
            pass

    def _exit(self, at_edge=False):
        self.ipad_mode = False
        self._restore_pointer()
        self.ble.release_all()
        self._swallow_button_ups |= self._ipad_buttons
        self._ipad_buttons = 0
        self._mods = 0
        self._keys = []
        x, y = self._entry
        if at_edge:
            x += -2 if self._right else 2
        user32.SetCursorPos(x, y)
        span = max(0.001, time.perf_counter() - self._session[0])
        log.info("-> PC (%s) after %.1fs, %.0f mouse moves/s",
                 "edge" if at_edge else "hotkey/disconnect", span, self._session[1] / span)
        self._notify()

    def _try_enter_at_edge(self, px, py):
        """Called for PC mouse moves; True when the move pushed through the outer edge."""
        cx, cy = cursor_pos()
        rect = self._monitor_at(cx, cy)
        if rect is None:  # display layout changed since we last looked
            self._monitors = monitor_rects()
            self._edge_count = 0
            return False
        edge_x = rect[2] - 1 if self._right else rect[0]
        beyond_x = edge_x + 1 if self._right else edge_x - 1
        if cx != edge_x or self._monitor_at(beyond_x, cy) is not None:
            self._edge_count = 0
            return False
        push = px - cx if self._right else cx - px
        # The hook usually reports the move already clipped to the desktop, so a move that stays
        # on the edge also counts as pushing, unless it is clearly sliding along the edge.
        if push < 0 or abs(py - cy) > 2 + push:
            self._edge_count = 0
            return False
        self._edge_count += 1
        if self._edge_count < self.cfg.edge_dwell:
            return False
        self._edge_count = 0
        self._enter((cx, cy), from_edge=True)
        return True

    # ---- mouse ---------------------------------------------------------------------------------

    def _mouse_hook(self, code, wparam, lparam):
        if code >= 0:
            try:
                m = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if self._handle_mouse(wparam, m):
                    return 1
            except Exception:
                log.exception("mouse hook error")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _handle_mouse(self, msg, m):
        button = _BUTTONS.get(msg)
        if not self.ipad_mode:
            if button:
                bit, down = button
                if down:
                    self._pc_buttons |= bit
                elif self._swallow_button_ups & bit:
                    self._swallow_button_ups &= ~bit  # pressed on the iPad, released after coming back
                    return True
                else:
                    self._pc_buttons &= ~bit
                return False
            if msg == WM_MOUSEMOVE and self.ble.connected and not self._pc_buttons:
                return self._try_enter_at_edge(m.pt.x, m.pt.y)
            return False

        if msg == WM_MOUSEMOVE:
            self._session[1] += 1
            dx, dy = m.pt.x - self._park[0], m.pt.y - self._park[1]
            if dx or dy:
                self._ipad_motion(dx, dy)
            if self._session[1] <= TRACE_MOVES:
                log.info("trace #%d pt=(%d,%d) d=(%d,%d) vx=%.0f push=%d flags=%#x",
                         self._session[1], m.pt.x, m.pt.y, dx, dy, self._vx, self._return_push, m.flags)
            return True
        if button:
            bit, down = button
            if not down and self._pc_buttons & bit:
                self._pc_buttons &= ~bit  # pressed on the PC before switching: release it there
                return False
            self._ipad_buttons = self._ipad_buttons | bit if down else self._ipad_buttons & ~bit
            self.ble.set_buttons(self._ipad_buttons)
            return True
        if msg == WM_MOUSEWHEEL:
            self._wheel_acc += ctypes.c_short(m.mouseData >> 16).value
            notches = int(self._wheel_acc / 120)
            if notches:
                self._wheel_acc -= notches * 120
                self.ble.add_motion(0, 0, notches * self.cfg.scroll)
            return True
        return True  # side buttons, horizontal wheel: not forwarded yet, but don't leak to the PC

    def _ipad_motion(self, dx, dy):
        sx = dx * self.cfg.speed + self._frac[0]
        sy = dy * self.cfg.speed + self._frac[1]
        ix, iy = int(sx), int(sy)
        self._frac = [sx - ix, sy - iy]
        if ix or iy:
            self.ble.add_motion(ix, iy)

        away = dx if self._right else -dx
        if away >= 0:
            if away:
                self._vx = min(float(self.cfg.ipad_width), self._vx + away)
                self._return_push = 0
            return
        if self._vx + away > 0:
            self._vx += away
            return
        self._vx = 0.0
        if time.perf_counter() - self._session[0] < RETURN_GRACE:
            return  # just arrived: a twitch back must not bounce us straight to the PC
        self._return_push += -away
        if self._return_push >= self.cfg.return_push:
            self._exit(at_edge=True)

    # ---- keyboard ------------------------------------------------------------------------------

    def _kbd_hook(self, code, wparam, lparam):
        if code >= 0:
            try:
                k = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if self._handle_key(k.vkCode, k.flags):
                    return 1
            except Exception:
                log.exception("keyboard hook error")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _hotkey_mods_held(self):
        return all(self._held & _MOD_GROUPS[g] for g in self.cfg.hotkey_mods)

    def _handle_key(self, vk, flags):
        if vk == VK_MENU_MASK:
            return False
        up = bool(flags & LLKHF_UP)
        if up:
            self._held.discard(vk)
        else:
            self._held.add(vk)

        if not up and vk == self.cfg.hotkey_vk and self._hotkey_mods_held():
            self._hotkey_up = vk
            if self.ipad_mode:
                self._exit()
            else:
                if self.cfg.hotkey_mods & {"alt", "win"}:
                    user32.keybd_event(VK_MENU_MASK, 0, 0, 0)
                    user32.keybd_event(VK_MENU_MASK, 0, KEYEVENTF_KEYUP, 0)
                self._enter(cursor_pos(), from_edge=False)
            return True
        if up and vk == self._hotkey_up:
            self._hotkey_up = None
            return True

        if not self.ipad_mode:
            if up and vk in self._ipad_keys_down:
                self._ipad_keys_down.discard(vk)
                return True
            if up:
                self._pc_keys.discard(vk)
            else:
                self._pc_keys.add(vk)
            return False

        if up and vk in self._pc_keys:
            self._pc_keys.discard(vk)
            return False
        if up:
            self._ipad_keys_down.discard(vk)
        else:
            self._ipad_keys_down.add(vk)
        self._forward_key(vk, bool(flags & LLKHF_EXTENDED), up)
        return True

    def _forward_key(self, vk, extended, up):
        bit = hid.modifier_bit(vk, self.cfg.ctrl_as_cmd)
        if bit:
            mods = self._mods & ~bit if up else self._mods | bit
            if mods == self._mods:
                return
            self._mods = mods
        else:
            usage = hid.usage(vk, extended)
            if not usage:
                return
            if up:
                if usage not in self._keys:
                    return
                self._keys.remove(usage)
            else:
                if usage in self._keys:
                    return  # Windows auto-repeat; the iPad repeats held keys by itself
                if len(self._keys) == 6:
                    self._keys.pop(0)
                self._keys.append(usage)
        self.ble.send_keyboard(self._mods, self._keys)
