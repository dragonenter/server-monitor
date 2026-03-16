"""
py2app setup script for Server Monitor macOS app.
Usage: python setup_mac.py py2app

前置条件：desktop 包必须已安装到 site-packages 中。
"""

import os
from setuptools import setup

APP = ["desktop/main.py"]
APP_NAME = "ServerMonitor"

_icon = "desktop/assets/icon.icns"

# 不需要的 PySide6 子模块
PYSIDE6_EXCLUDE = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtConcurrent",
    "PySide6.QtDataVisualization", "PySide6.QtDBus", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtHttpServer", "PySide6.QtLocation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork", "PySide6.QtNetworkAuth", "PySide6.QtNfc",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtPositioning",
    "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialBus", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtSql", "PySide6.QtStateMachine", "PySide6.QtSvg",
    "PySide6.QtSvgWidgets", "PySide6.QtTest", "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools", "PySide6.QtWebChannel", "PySide6.QtWebEngine",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets", "PySide6.QtXml",
]

OPTIONS = {
    "argv_emulation": False,
    **({"iconfile": _icon} if os.path.exists(_icon) else {}),
    # desktop 包已在 site-packages 中，py2app 可以自动追踪
    "packages": [
        "desktop", "desktop.ui", "desktop.collectors",
        "psutil",
    ],
    "includes": [
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    ],
    "excludes": [
        "tkinter", "matplotlib", "numpy", "scipy", "pandas", "PIL",
        "test", "unittest", "doctest",
    ] + PYSIDE6_EXCLUDE,
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
    data_files=[],
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
