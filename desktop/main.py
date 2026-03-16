import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from desktop.ui.main_window import MonitorMainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Server Monitor")
    app.setOrganizationName("dragonenter")
    # Enable high DPI
    app.setAttribute(Qt.AA_UseHighDpiPixmaps) if hasattr(Qt, 'AA_UseHighDpiPixmaps') else None
    window = MonitorMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
