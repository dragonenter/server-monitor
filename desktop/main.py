"""Server Monitor 桌面版入口."""
import os
import sys
import traceback


def _setup_path():
    """确保 py2app 打包环境下导入路径正确."""
    if getattr(sys, 'frozen', False):
        # py2app 打包后，__file__ 在 .app/Contents/Resources/ 下
        bundle_dir = os.path.dirname(os.path.abspath(__file__))
        # 添加 Resources 目录到 path（desktop 包在这里面）
        if bundle_dir not in sys.path:
            sys.path.insert(0, bundle_dir)
        # 也添加 Resources/lib 目录
        lib_dir = os.path.join(bundle_dir, 'lib')
        if os.path.isdir(lib_dir) and lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
    else:
        # 开发环境：确保项目根目录在 path 中
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)


def _write_crash_log(error_msg: str):
    """崩溃时写入日志文件到桌面，方便调试."""
    try:
        desktop = os.path.expanduser("~/Desktop")
        log_path = os.path.join(desktop, "ServerMonitor_crash.log")
        with open(log_path, "w") as f:
            f.write(f"Python: {sys.version}\n")
            f.write(f"Executable: {sys.executable}\n")
            f.write(f"Frozen: {getattr(sys, 'frozen', False)}\n")
            f.write(f"sys.path:\n")
            for p in sys.path:
                f.write(f"  {p}\n")
            f.write(f"\n__file__: {__file__}\n")
            f.write(f"\nError:\n{error_msg}\n")
    except Exception:
        pass


def main():
    _setup_path()

    try:
        from PySide6.QtWidgets import QApplication
        from desktop.ui.main_window import MonitorMainWindow
    except ImportError:
        # 如果绝对导入失败，尝试相对路径导入
        try:
            # py2app 可能把 desktop 内容平铺在 Resources 下
            from ui.main_window import MonitorMainWindow
            from PySide6.QtWidgets import QApplication
        except ImportError as e:
            _write_crash_log(traceback.format_exc())
            raise

    try:
        app = QApplication(sys.argv)
        app.setApplicationName("Server Monitor")
        app.setOrganizationName("dragonenter")

        # 设置 Qt 插件路径（py2app 环境下可能找不到）
        if getattr(sys, 'frozen', False):
            bundle_dir = os.path.dirname(os.path.abspath(__file__))
            plugin_path = os.path.join(bundle_dir, 'lib', 'PySide6', 'Qt', 'plugins')
            if os.path.isdir(plugin_path):
                app.addLibraryPath(plugin_path)
            # 也检查 PySide6 的 qt.conf
            qt_plugin2 = os.path.join(bundle_dir, 'PySide6', 'Qt', 'plugins')
            if os.path.isdir(qt_plugin2):
                app.addLibraryPath(qt_plugin2)

        window = MonitorMainWindow()
        window.show()
        sys.exit(app.exec())
    except Exception as e:
        _write_crash_log(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
