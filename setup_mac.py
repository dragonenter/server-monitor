"""
py2app setup script for Server Monitor macOS app.
Usage: python setup_mac.py py2app
"""

from setuptools import setup

APP = ["desktop/main.py"]
APP_NAME = "ServerMonitor"

DATA_FILES = []

import os
_icon = "desktop/assets/icon.icns"

OPTIONS = {
    "argv_emulation": False,
    **({"iconfile": _icon} if os.path.exists(_icon) else {}),
    "includes": [],
    "packages": ["PySide6", "psutil"],
    "excludes": ["tkinter", "matplotlib", "numpy", "test", "unittest"],
    "plist": {
        "CFBundleName": "Server Monitor",
        "CFBundleDisplayName": "Server Monitor",
        "CFBundleIdentifier": "com.dragonenter.server-monitor",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
}

setup(
    name=APP_NAME,
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
