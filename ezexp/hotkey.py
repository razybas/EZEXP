import os
import time
import traceback
import ctypes
from ctypes import wintypes
from PySide6.QtCore import QAbstractNativeEventFilter
from ezexp import APP_DIR

WM_HOTKEY = 0x0312

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000

HOTKEY_ID = 0xB00B

class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        try:
            if event_type == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.callback()
        except Exception:
            try:
                with open(os.path.join(APP_DIR, "error.log"), "a",
                          encoding="utf-8") as fh:
                    fh.write("\n=== %s ===\n" % time.strftime(
                        "%Y-%m-%d %H:%M:%S"))
                    fh.write(traceback.format_exc())
            except Exception:
                pass
        return False, 0

def register_hotkey():
    user32 = ctypes.windll.user32
    if user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT | MOD_NOREPEAT, 0x20):
        return "Alt+Space"
    if user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x20):
        return "Ctrl+Alt+Space"
    if user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_NOREPEAT, 0x20):
        return "Ctrl+Space"
    return None

def unregister_hotkey():
    try:
        ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
    except Exception:
        pass
