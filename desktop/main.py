import os
import sys

# py2app 打包后需要确保 desktop 包在搜索路径中
if getattr(sys, 'frozen', False):
    # 打包环境：desktop/main.py 被提升为入口，需要把父目录加到路径
    _app_dir = os.path.dirname(os.path.abspath(__file__))
    _parent = os.path.dirname(_app_dir)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    if _app_dir not in sys.path:
        sys.path.insert(0, _app_dir)

from PySide6.QtWidgets import QApplication
from desktop.ui.main_window import MonitorMainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Server Monitor")
    app.setOrganizationName("dragonenter")
    window = MonitorMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
