"""目憩（Mù Qī）程序入口（P0-M10）。

职责（保持极简，业务逻辑全在 ``core/app.py``）：

1. 配置日志：始终写入 ``data/muqi.log``（滚动，单文件 2 MB），
   控制台可选——PyInstaller ``--windowed`` 打包后 ``sys.stderr`` 可能为 None，
   只配 StreamHandler 会导致启动即崩或错误全部丢失。
2. 单实例防护：Windows 命名 Mutex，避免多开导致多个托盘图标、
   摄像头争抢与 SQLite 并发写（文档未要求，属防御性保护）。
3. 安装全局异常钩子，把未捕获异常写进日志便于排查。
4. 创建 :class:`MuQiApp` 并进入主循环。

运行::

    .venv\\Scripts\\python.exe main.py
"""

import ctypes
import logging
import logging.handlers
import platform
import sys
import tkinter as tk
import tkinter.messagebox as messagebox
from typing import Any, List

from config import default as cfg
from core.app import MuQiApp

_LOG_FILE = cfg.DATA_DIR / "muqi.log"
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUP_COUNT = 3
_MUTEX_NAME = "MuQi_SingleInstance_v1"
_ERROR_ALREADY_EXISTS = 183

# 保持 Mutex 句柄引用，进程存活期间一直持有。
_mutex_handle: Any = None


def configure_logging() -> None:
    """配置日志：文件 handler 必选，控制台 handler 仅在 stderr 可用时添加。

    Raises:
        OSError: ``data/`` 目录不可写，日志文件无法创建。
    """
    cfg.ensure_directories()
    handlers: List[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            str(_LOG_FILE),
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def ensure_single_instance() -> bool:
    """用 Windows 命名 Mutex 检查是否已有实例在运行。

    ``CreateMutexW`` 返回的是 HANDLE（指针宽度），必须显式设置 ``restype``
    为 ``c_void_p``，否则 ctypes 默认按 ``c_int`` 返回，在 64 位进程上会截断句柄。

    Returns:
        True=当前是唯一实例；False=已有实例在运行。
    """
    global _mutex_handle
    if platform.system() != "Windows":
        return True  # 非 Windows 仅用于开发/单元测试，不做限制
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    # Win32 文档：CreateMutexW 成功时 GetLastError 的值未定义，可能残留
    # ERROR_ALREADY_EXISTS(183)。先清零，避免把「首次创建」误判成「已有实例」。
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        return False
    _mutex_handle = handle
    return True


def _excepthook(
    exc_type: Any, exc_value: Any, exc_tb: Any
) -> None:
    """把未捕获异常写入日志（无控制台时唯一的诊断入口）。

    Args:
        exc_type: 异常类型。
        exc_value: 异常实例。
        exc_tb: traceback 对象。
    """
    logging.getLogger("main").critical(
        "未捕获异常", exc_info=(exc_type, exc_value, exc_tb)
    )


def show_already_running() -> None:
    """提示用户程序已在运行（无控制台打包下也能看到）。"""
    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showinfo(cfg.APP_NAME, "目憩已在运行中，请勿重复启动。")
    finally:
        root.destroy()


def main() -> int:
    """程序入口。

    Returns:
        进程退出码：0=正常退出，1=启动失败。
    """
    configure_logging()
    sys.excepthook = _excepthook
    logger = logging.getLogger("main")

    if not ensure_single_instance():
        logger.warning("检测到已有实例，本次启动取消")
        show_already_running()
        return 0

    try:
        MuQiApp().run()
    except Exception:  # noqa: BLE001 - 顶层兜底，确保异常被记录
        logger.exception("应用启动失败")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
