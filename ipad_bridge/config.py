"""User settings in ipad-bridge.ini (created with defaults on first run)."""
import configparser
import os

DEFAULT_INI = """\
[bridge]
# iPad 放在电脑屏幕的哪一边：right 或 left（以所有显示器拼起来的最外侧边缘为准）
side = right

# 在电脑和 iPad 之间来回切换的快捷键，例如 ctrl+alt+q、ctrl+shift+f12、scrolllock
hotkey = ctrl+alt+q

# 发给 iPad 的鼠标移动倍率，觉得 iPad 上指针太慢就调大
speed = 1.0

# 滚轮每格滚动的行数
scroll = 1

# 控制 iPad 时临时关掉 Windows 的"提高指针精确度"（鼠标加速），回到电脑自动恢复。
# iPad 自己还会再加速一次，两层叠加会让指针发飘
disable_windows_accel = true

# 鼠标移动多久合并发送一次（毫秒）。越小越跟手，太小会挤占蓝牙带宽
motion_interval_ms = 10

# 在屏幕边缘连续往外推多少次鼠标事件才切到 iPad（防止误切）
edge_dwell = 3

# iPad 屏幕"宽度"（单位是电脑鼠标的移动量），用来判断什么时候从 iPad 回到电脑。
# 回来得太早就调大，要推很久才回来就调小
ipad_width = 1600

# 到了 iPad 靠电脑那一侧的边缘后，还要再往回推多少才回到电脑
return_push = 200

# true：Ctrl 当作 iPad 的 Command（Ctrl+C/V 直接复制粘贴），Win 键当作 Control
# false：Win 键 = Command，Ctrl = Control
ctrl_as_cmd = false
"""

_KEY_NAMES = {
    "space": 0x20, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "enter": 0x0D, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "pause": 0x13, "scrolllock": 0x91,
    "capslock": 0x14, "printscreen": 0x2C,
}


class Config:
    def __init__(self, path):
        self.path = path
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(DEFAULT_INI)
        parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        parser.read_string(DEFAULT_INI)
        parser.read(path, encoding="utf-8")
        s = parser["bridge"]
        self.side = s.get("side").strip().lower()
        if self.side not in ("left", "right"):
            raise ValueError("side 只能是 left 或 right，现在是 %r" % self.side)
        self.hotkey = s.get("hotkey").strip()
        self.hotkey_vk, self.hotkey_mods = parse_hotkey(self.hotkey)
        self.speed = s.getfloat("speed")
        self.scroll = s.getint("scroll")
        self.disable_windows_accel = s.getboolean("disable_windows_accel")
        self.motion_interval_ms = max(1, s.getint("motion_interval_ms"))
        self.edge_dwell = max(1, s.getint("edge_dwell"))
        self.ipad_width = max(100, s.getint("ipad_width"))
        self.return_push = max(1, s.getint("return_push"))
        self.ctrl_as_cmd = s.getboolean("ctrl_as_cmd")


def parse_hotkey(text):
    """'ctrl+alt+q' -> (virtual key, {'ctrl', 'alt'})."""
    mods = set()
    vk = None
    for part in text.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods.add("ctrl")
        elif part == "alt":
            mods.add("alt")
        elif part == "shift":
            mods.add("shift")
        elif part in ("win", "cmd"):
            mods.add("win")
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        elif part in _KEY_NAMES:
            vk = _KEY_NAMES[part]
        else:
            raise ValueError("看不懂的快捷键部分：%r（完整写法：%r）" % (part, text))
    if vk is None:
        raise ValueError("快捷键里缺少主键：%r" % text)
    return vk, mods
