"""Notification-area icon: shows the state and offers the few actions a user needs."""
import os
import threading

import pystray
from PIL import Image, ImageDraw

_COLORS = {
    "waiting": (150, 150, 150, 255),  # advertising, no iPad connected
    "pc": (40, 120, 230, 255),        # iPad connected, input stays on the PC
    "ipad": (30, 170, 80, 255),       # input goes to the iPad
}


def _tablet(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((12, 4, 52, 60), radius=8, fill=color)
    d.rounded_rectangle((17, 10, 47, 50), radius=3, fill=(255, 255, 255, 255))
    d.ellipse((29, 52, 35, 58), fill=(255, 255, 255, 255))
    return img


class Tray:
    def __init__(self, on_toggle, on_quit, on_reset_bluetooth, config_path, log_path):
        self._state = "waiting"
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self._status_text(), None, enabled=False),
            pystray.MenuItem(
                lambda item: "切回电脑" if self._state == "ipad" else "切换到 iPad",
                lambda: self._on_toggle(),
                default=True,
                enabled=lambda item: self._state != "waiting",
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重置蓝牙（iPad 连不上时用）", lambda: on_reset_bluetooth()),
            pystray.MenuItem("配对新 iPad（打开蓝牙设置）", lambda: os.startfile("ms-settings:bluetooth")),
            pystray.MenuItem("打开配置文件", lambda: os.startfile(config_path)),
            pystray.MenuItem("打开日志", lambda: os.startfile(log_path)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self._on_quit()),
        )
        self.icon = pystray.Icon("ipad-bridge", _tablet(_COLORS["waiting"]), "iPad Bridge：等待 iPad 连接", menu)

    def _status_text(self):
        return {
            "waiting": "等待 iPad 连接…",
            "pc": "iPad 已连接，键鼠在电脑上",
            "ipad": "正在控制 iPad",
        }[self._state]

    def start(self):
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def stop(self):
        self.icon.stop()

    def update(self, connected, ipad_mode):
        state = "ipad" if ipad_mode else ("pc" if connected else "waiting")
        if state == self._state:
            return
        self._state = state
        self.icon.icon = _tablet(_COLORS[state])
        self.icon.title = "iPad Bridge：" + self._status_text()
        self.icon.update_menu()
