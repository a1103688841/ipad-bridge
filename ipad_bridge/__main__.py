"""Entry point: python -m ipad_bridge"""
import logging
import logging.handlers
import os
import sys

from . import win32
from .ble import BleHid
from .config import Config
from .router import MSG_CONNECTION, MSG_TOGGLE, Router, restore_saved_ballistics
from .tray import Tray

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "ipad-bridge.ini")
LOG_PATH = os.path.join(ROOT, "ipad-bridge.log")


def setup_logging():
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handlers = [handler]
    if sys.stderr is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")


def main():
    win32.set_dpi_aware()
    mutex = win32.kernel32.CreateMutexW(None, False, "Local\\ipad-bridge")
    if win32.ctypes.get_last_error() == win32.ERROR_ALREADY_EXISTS:
        win32.message_box("iPad Bridge 已经在运行了（看右下角托盘）。")
        return 1
    setup_logging()
    log = logging.getLogger("ipad_bridge")

    try:
        cfg = Config(CONFIG_PATH)
    except Exception as e:
        win32.message_box("配置文件有误：\n%s\n\n%s" % (CONFIG_PATH, e), flags=win32.MB_ICONERROR)
        return 1

    restore_saved_ballistics()
    log.info("Windows pointer settings (threshold1, threshold2, accel, speed): %s", win32.get_pointer_ballistics())
    router = Router(cfg, ble=None)
    ble = BleHid(on_connection_change=lambda connected: router.post(MSG_CONNECTION, int(connected)))
    ble.motion_interval = cfg.motion_interval_ms / 1000.0
    router.ble = ble
    try:
        ble.start()
    except Exception as e:
        log.exception("bluetooth start failed")
        win32.message_box("蓝牙键鼠启动失败：\n%s" % e, flags=win32.MB_ICONERROR)
        return 1

    tray = Tray(on_toggle=lambda: router.post(MSG_TOGGLE), on_quit=router.quit,
                config_path=CONFIG_PATH, log_path=LOG_PATH)
    router.on_mode_change = lambda ipad_mode: tray.update(ble.connected, ipad_mode)
    tray.start()

    # Ctrl+C / closing the console window: leave the message loop cleanly so hooks get removed.
    console_handler = win32.HANDLER_ROUTINE(lambda event: router.quit() or True)
    win32.kernel32.SetConsoleCtrlHandler(console_handler, True)

    router.install()
    log.info("running (config %s)", CONFIG_PATH)
    try:
        router.run()
    finally:
        router.uninstall()
        tray.stop()
        ble.stop()
        log.info("bye")
    del mutex
    return 0


if __name__ == "__main__":
    sys.exit(main())
