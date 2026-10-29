import os
import sys
import time
import ctypes
from ctypes import wintypes
from PySide6.QtGui import QColor

from ezexp.settings_store import APP_EXTS

ACCENT = "#0a84ff"

TEXT = "#1d1d1f"

SUBTLE = "#86868b"

CAT_EXTS = {
    "app": APP_EXTS,
    "doc": {
        "pdf", "doc", "docx", "docm", "dot", "dotx", "xls", "xlsx", "xlsm",
        "xlsb", "ppt", "pptx", "pptm", "odt", "ods", "odp", "txt", "text",
        "md", "markdown", "rtf", "csv", "tsv", "epub", "mobi", "log", "ini",
        "cfg", "wps", "xps", "one", "vsd",
    },
    "img": {
        "jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "heic", "heif",
        "tif", "tiff", "ico", "psd", "ai", "raw", "cr2", "cr3", "nef",
        "arw", "dng", "orf", "rw2", "avif", "jfif",
    },
    "vid": {
        "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg",
        "mpeg", "3gp", "ts", "mts", "m2ts", "vob", "ogv",
    },
    "aud": {
        "mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus", "mid",
        "midi", "aiff", "alac", "amr",
    },
    "zip": {
        "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "iso", "cab",
        "tgz", "tbz2", "lz4",
    },
    "code": {
        "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "html", "htm", "css",
        "scss", "less", "json", "xml", "yml", "yaml", "toml", "java", "c",
        "cpp", "cc", "cxx", "h", "hpp", "cs", "go", "rb", "php", "sh",
        "bash", "bat", "cmd", "ps1", "psm1", "sql", "vbs", "lua", "rs",
        "swift", "kt", "kts", "dart", "vue", "svelte", "gradle", "cmake",
        "ipynb", "r", "pl",
    },
}

KIND_NAMES = {
    "pdf": "PDF Document", "doc": "Word Document", "docx": "Word Document",
    "xls": "Excel Spreadsheet", "xlsx": "Excel Spreadsheet",
    "ppt": "PowerPoint Presentation", "pptx": "PowerPoint Presentation",
    "txt": "Plain Text", "md": "Markdown Document", "csv": "CSV File",
    "jpg": "JPEG Image", "jpeg": "JPEG Image", "png": "PNG Image",
    "gif": "GIF Image", "webp": "WebP Image", "svg": "SVG Image",
    "heic": "HEIC Image", "mp4": "MP4 Video", "mov": "QuickTime Movie",
    "mkv": "Matroska Video", "avi": "AVI Video", "mp3": "MP3 Audio",
    "wav": "WAV Audio", "flac": "FLAC Audio", "zip": "ZIP Archive",
    "rar": "RAR Archive", "7z": "7z Archive", "iso": "Disc Image",
    "exe": "Application", "msi": "Windows Installer", "dll": "System Library",
    "py": "Python Script", "js": "JavaScript File", "ts": "TypeScript File",
    "json": "JSON File", "html": "HTML Document", "css": "CSS File",
    "bat": "Batch File", "ps1": "PowerShell Script", "sql": "SQL File",
}

def ext_of(name):
    return name.rsplit(".", 1)[1].lower() if "." in name else ""

TEXT_EXTS = {
    "txt", "text", "md", "markdown", "log", "ini", "cfg", "conf", "csv",
    "tsv", "json", "xml", "yml", "yaml", "toml", "py", "js", "ts", "tsx",
    "jsx", "html", "htm", "css", "scss", "c", "cpp", "h", "hpp", "cs", "go",
    "rb", "php", "sh", "bat", "ps1", "sql", "lua", "rs", "swift", "kt",
    "java", "r", "pl", "srt", "vtt",
}

def category_for(name, is_dir):
    if is_dir:
        return "folder"
    ext = ext_of(name)
    for cat, exts in CAT_EXTS.items():
        if ext in exts:
            return cat
    return "other"

def kind_for(name, is_dir):
    if is_dir:
        return "Folder"
    ext = ext_of(name)
    if ext in KIND_NAMES:
        return KIND_NAMES[ext]
    return ext.upper() + " File" if ext else "File"

def human_size(n):
    if n is None or n < 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{n} B"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return "—"

def fmt_date(mtime):
    try:
        return time.strftime("%d %b %Y", time.localtime(mtime))
    except (ValueError, OverflowError):
        return "—"

def join_path(parent, name):
    return parent + "\\" + name if parent else name

def split_path(path):
    if len(path) == 2 and path[1] == ":":
        return "", path
    i = path.rfind("\\")
    if i < 0:
        return "", path
    return path[:i], path[i + 1:]

def real_dir(path):
    return path + "\\" if len(path) == 2 and path[1] == ":" else path

def like_escape(s, esc="#"):
    return s.replace(esc, esc + esc).replace("%", esc + "%").replace("_", esc + "_")

SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "$winreagent",
    "config.msi", "recovery",
}

SKIP_FILE_NAMES = {"hiberfil.sys", "pagefile.sys", "swapfile.sys",
                   "dumpstack.log", "dumpstack.log.tmp"}

REPARSE_ATTR = 0x400

def fixed_drives():
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    drives = []
    for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if mask >> i & 1:
            root = letter + ":\\"
            if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:  # DRIVE_FIXED
                drives.append(root)
    return drives or ["C:\\"]

def drive_label(root):
    name = ctypes.create_unicode_buffer(261)
    k32 = ctypes.windll.kernel32
    ok = k32.GetVolumeInformationW(ctypes.c_wchar_p(root), name, 261, None, None, None, None, 0)
    label = name.value.strip() if ok and name.value.strip() else "Local Disk"
    return f"{label} ({root[:2]})"

def shell_folders():
    """Resolve the user's real Desktop/Documents/... paths (handles OneDrive redirection)."""
    import winreg
    GUIDS = {
        "Desktop": "Desktop",
        "Personal": "Documents",
        "{374DE290-123F-4565-9164-39C4925E467B}": "Downloads",
        "My Pictures": "Pictures",
        "My Music": "Music",
        "My Video": "Videos",
    }
    home = os.path.expanduser("~")
    out = {}
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        )
        with key:
            for regname, nice in GUIDS.items():
                try:
                    val, _ = winreg.QueryValueEx(key, regname)
                    out[nice] = val
                except OSError:
                    pass
    except OSError:
        pass
    fallbacks = {
        "Desktop": os.path.join(home, "Desktop"),
        "Documents": os.path.join(home, "Documents"),
        "Downloads": os.path.join(home, "Downloads"),
        "Pictures": os.path.join(home, "Pictures"),
        "Music": os.path.join(home, "Music"),
        "Videos": os.path.join(home, "Videos"),
    }
    for nice, path in fallbacks.items():
        out.setdefault(nice, path)
    order = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]
    return [(n, out[n]) for n in order if out.get(n)]
