import os
import sys
import shutil
import subprocess
import ctypes
from ctypes import wintypes
from PySide6.QtCore import QFileInfo
from PySide6.QtGui import QImage, QPixmap

from ezexp.fsutil import real_dir

def reveal_in_explorer(path):
    try:
        subprocess.Popen(["explorer", "/select," + path])
    except OSError:
        subprocess.Popen(["explorer", real_dir(path) if len(path) <= 3 else path])

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p),
    ]

def recycle_path(path):
    """Send a file/folder to the Recycle Bin."""
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT = 3, 0x40, 0x10, 0x4
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return res == 0 and not op.fAnyOperationsAborted

def show_properties(path):
    """Open the native Explorer Properties dialog."""
    import ctypes as c

    class SHELLEXECUTEINFOW(c.Structure):
        _fields_ = [
            ("cbSize", c.c_ulong), ("fMask", c.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", c.c_wchar_p), ("lpFile", c.c_wchar_p),
            ("lpParameters", c.c_wchar_p), ("lpDirectory", c.c_wchar_p),
            ("nShow", c.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", c.c_void_p), ("lpClass", c.c_wchar_p),
            ("hkeyClass", wintypes.HKEY), ("dwHotKey", c.c_ulong),
            ("hIconOrMonitor", wintypes.HANDLE),
        ]

    SEE_MASK_INVOKEIDLIST = 0x0000000C
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = c.sizeof(sei)
    sei.fMask = SEE_MASK_INVOKEIDLIST
    sei.lpVerb = "properties"
    sei.lpFile = path
    sei.nShow = 1
    try:
        return bool(ctypes.windll.shell32.ShellExecuteExW(c.byref(sei)))
    except Exception:
        return False

def _hbitmap_to_qimage(hbm):
    """Copy a GDI HBITMAP into a QImage (32bpp, alpha forced opaque if absent)."""
    from ctypes import byref, c_long, c_void_p, c_ushort

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", c_long), ("bmWidth", c_long), ("bmHeight", c_long),
                    ("bmWidthBytes", c_long), ("bmPlanes", c_ushort),
                    ("bmBitsPixel", c_ushort), ("bmBits", c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_ulong), ("biWidth", c_long),
                    ("biHeight", c_long), ("biPlanes", c_ushort),
                    ("biBitCount", c_ushort), ("biCompression", ctypes.c_ulong),
                    ("biSizeImage", ctypes.c_ulong), ("biXPelsPerMeter", c_long),
                    ("biYPelsPerMeter", c_long), ("biClrUsed", ctypes.c_ulong),
                    ("biClrImportant", ctypes.c_ulong)]

    gdi32 = ctypes.windll.gdi32
    bm = BITMAP()
    if not gdi32.GetObjectW(hbm, ctypes.sizeof(bm), byref(bm)):
        return None
    w, h = bm.bmWidth, bm.bmHeight
    if w <= 0 or h <= 0 or w * h > 4096 * 4096:
        return None
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h  # top-down
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    hdc = gdi32.CreateCompatibleDC(None)
    buf = ctypes.create_string_buffer(w * h * 4)
    try:
        if gdi32.GetDIBits(hdc, hbm, 0, h, buf, byref(bi), 0) != h:
            return None
    finally:
        gdi32.DeleteDC(hdc)
    raw = bytearray(buf.raw)
    if not any(raw[3::4]):  # no alpha in the bitmap — make it opaque
        raw[3::4] = b"\xff" * len(raw[3::4])
    return QImage(bytes(raw), w, h, w * 4, QImage.Format_ARGB32).copy()

def shell_thumbnail(path, maxdim=512):
    """Explorer-style thumbnail (works for videos, images, documents).
    Returns a QImage or None. Call from a background thread only."""
    from ctypes import byref, c_long, c_void_p, c_wchar_p

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", c_long), ("cy", c_long)]

    GetImageProto = ctypes.WINFUNCTYPE(
        ctypes.c_long, c_void_p, SIZE, ctypes.c_uint, ctypes.POINTER(c_void_p))
    ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)

    path = os.path.normpath(path.replace("/", "\\"))
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)  # MTA is fine for shell thumbnails
    pp = c_void_p()
    funcs = None
    try:
        # IID_IShellItemImageFactory {BCC18B79-BA16-442F-80C4-8A59C30C463B}
        iid = GUID(0xBCC18B79, 0xBA16, 0x442F,
                   (ctypes.c_ubyte * 8)(0x80, 0xC4, 0x8A, 0x59, 0xC3, 0x0C, 0x46, 0x3B))
        hr = ctypes.windll.shell32.SHCreateItemFromParsingName(
            c_wchar_p(path), None, byref(iid), byref(pp))
        if hr < 0 or not pp.value:
            return None
        vtbl_ptr = ctypes.cast(pp, ctypes.POINTER(c_void_p)).contents
        funcs = ctypes.cast(vtbl_ptr, ctypes.POINTER(c_void_p * 4)).contents
        get_image = GetImageProto(funcs[3])
        hbm = c_void_p()
        SIIGBF_BIGGERSIZEOK, SIIGBF_THUMBNAILONLY = 0x1, 0x4
        size = SIZE(maxdim, maxdim)
        hr = get_image(pp, size, SIIGBF_BIGGERSIZEOK | SIIGBF_THUMBNAILONLY, byref(hbm))
        if hr < 0 or not hbm.value:  # no thumbnail: fall back to the file icon
            hr = get_image(pp, size, SIIGBF_BIGGERSIZEOK, byref(hbm))
            if hr < 0 or not hbm.value:
                return None
        img = _hbitmap_to_qimage(hbm.value)
        ctypes.windll.gdi32.DeleteObject(hbm)
        return img
    except Exception:
        return None
    finally:
        if pp.value and funcs:
            try:
                ReleaseProto(funcs[2])(pp)
            except Exception:
                pass
        ole32.CoUninitialize()
