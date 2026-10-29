"""EzExp package."""
import os

APP_NAME = "EzExp"

APP_VERSION = "1.2.0"

APP_DIR = DB_DIR = os.environ.get(
    "EZEXP_DB_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "EzExp"),
)

DB_PATH = os.path.join(DB_DIR, "index.db")
