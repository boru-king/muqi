"""锁屏 / 休眠检测（P1-M13，用户确认的 A 方案）。

原理
----
Windows 有一个「当前接收键鼠输入的桌面」。正常使用时是用户桌面
（名字通常为 ``Default``）；按 Win+L 锁屏、注销、切换用户、弹出 UAC 提权框时，
系统切换到 Winlogon 安全桌面，普通进程调用 ``OpenInputDesktop`` 会失败
（``ERROR_ACCESS_DENIED``）或读到 ``Winlogon`` 桌面名。

因此 **A 方案**的做法是：每秒问一次「我还能打开当前输入桌面吗？」
不能 → 一律按「用户离开」处理，停止抽检与计时；能 → 恢复键鼠检测。
它分不清具体是锁屏、注销还是 UAC（本项目不需要，这几种都要停）。

**不安装 ``WH_KEYBOARD_LL`` 全局钩子**（用户选择「普通程序方案」），
也不注册 WTS 会话通知 / 隐藏窗口消息泵，实现轻量、无杀软误报风险。

休眠兜底
--------
进程休眠时不会收到任何桌面事件，且主线程 ``after`` 停止调度。本模块用
``time.time()``（墙钟，休眠期间由 RTC 补偿继续推进）检测两次轮询的间隔：
超过 5 秒即判定「刚从休眠/时间跳变中恢复」，本帧按锁定处理并暂停检测。
状态机本身另有 ``monotonic`` 间隔 >5s 不补算的保护，两者互为兜底。

依赖注入
--------
``desktop_reader`` / ``wall_clock`` 可注入，便于在非锁屏环境单测所有分支，
也便于未来替换成 WTS 事件方案而不改调用方。
"""

import ctypes
import logging
import platform
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

_DESKTOP_READOBJECTS = 0x0001
_UOI_NAME = 2
_SUSPEND_GAP_SECONDS = 5.0
_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    # use_last_error=True 才能用 ctypes.get_last_error() 读到可靠的 Win32 错误码。
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _user32.OpenInputDesktop.argtypes = [
        ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32,
    ]
    _user32.OpenInputDesktop.restype = ctypes.c_void_p  # HDESK 是指针宽度
    _user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    _user32.CloseDesktop.restype = ctypes.c_int
    _user32.GetUserObjectInformationW.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_ulong),
    ]
    _user32.GetUserObjectInformationW.restype = ctypes.c_int
else:
    _user32 = None


@dataclass(frozen=True)
class LockState:
    """一次锁屏检测结果。

    Attributes:
        locked: 是否应停止全部检测与计时。
        reason: 判定原因（仅用于日志/诊断）。
        suspended: 本帧是否检测到系统休眠或时间跳变。
    """

    locked: bool
    reason: str
    suspended: bool = False


def _desktop_name(handle: int) -> str:
    """读取桌面对象名（``UOI_NAME``）。

    Args:
        handle: ``OpenInputDesktop`` 返回的 HDESK。

    Returns:
        桌面名；读取失败返回空串。
    """
    if _user32 is None:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    needed = ctypes.c_ulong()
    ok = _user32.GetUserObjectInformationW(
        handle,
        _UOI_NAME,
        buffer,
        ctypes.sizeof(buffer),
        ctypes.byref(needed),
    )
    return buffer.value if ok else ""


def read_input_desktop() -> Tuple[bool, str]:
    """检查当前输入桌面是否可访问（即是否处于用户桌面）。

    Returns:
        ``(是否可访问, 描述)``；不可访问意味着锁屏/注销/切换用户/UAC。
    """
    if not _IS_WINDOWS or _user32 is None:
        return True, "非 Windows 平台，跳过桌面检测"
    handle = _user32.OpenInputDesktop(0, 0, _DESKTOP_READOBJECTS)
    if not handle:
        err = ctypes.get_last_error()
        return False, "输入桌面不可访问(err=%d)，按锁屏/注销/UAC 处理" % err
    try:
        name = _desktop_name(handle)
        if name and name.lower() == "winlogon":
            return False, "处于 Winlogon 安全桌面"
        return True, "用户桌面 %s" % (name or "(未知名称)")
    finally:
        # HDESK 必须显式关闭，否则每次轮询泄漏一个内核句柄。
        _user32.CloseDesktop(handle)


class LockScreenMonitor:
    """由 1 秒主循环驱动的锁屏/休眠监视器（不自带线程）。"""

    def __init__(
        self,
        *,
        desktop_reader: Optional[Callable[[], Tuple[bool, str]]] = None,
        wall_clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """初始化。

        Args:
            desktop_reader: 返回 ``(可访问, 描述)`` 的函数；None 时使用真实 Win32 检测。
            wall_clock: 返回墙钟秒数的函数；None 时使用 ``time.time``。
        """
        self._reader = desktop_reader or read_input_desktop
        self._wall_clock = wall_clock or time.time
        self._last_wall = float(self._wall_clock())
        self._last_locked = False

    def poll(self) -> LockState:
        """执行一次检测。

        Returns:
            :class:`LockState`；仅在锁定状态**发生变化**时写 INFO 日志，
            避免每秒刷屏。
        """
        now = float(self._wall_clock())
        gap = now - self._last_wall
        self._last_wall = now

        # 墙钟向前跳变过大 = 进程被挂起过（休眠/睡眠）或系统时间被调整。
        suspended = gap > _SUSPEND_GAP_SECONDS
        try:
            accessible, reason = self._reader()
        except Exception as exc:  # noqa: BLE001 - 检测异常按「不可访问」保守处理
            logger.warning("输入桌面检测异常: %s", exc)
            accessible, reason = False, "检测异常: %s" % exc

        locked = suspended or (not accessible)
        if suspended:
            reason = "系统休眠/时间跳变 %.1fs，暂停检测" % gap

        state = LockState(bool(locked), reason, suspended)
        if state.locked != self._last_locked:
            logger.info(
                "锁定状态变化: %s -> %s (%s)",
                self._last_locked, state.locked, state.reason,
            )
            self._last_locked = state.locked
        return state
