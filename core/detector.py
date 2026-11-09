"""键鼠空闲检测（Windows ``GetLastInputInfo``）。

设计要点（为什么这样做）：

1. **32 位回绕**：需求文档第 87 行要求调用 ``GetLastInputInfo`` 与
   ``GetTickCount64``，但 ``LASTINPUTINFO.dwTime`` 是 **32 位 tick**（等价于
   ``GetTickCount()`` 的低 32 位）。若直接用 64 位 tick 相减，系统连续运行约
   49.7 天（2^32 毫秒）后会因回绕得到巨大错误值，导致「一直判定为活跃」。
   本模块把当前 64 位 tick 取低 32 位，再做 **32 位无符号回绕减法**，
   既保留文档要求的 API，又消除该误判。
2. **零钩子、零隐私采集**：只读取系统「最后一次输入时刻」，不安装键盘/鼠标钩子，
   不记录按键内容，不保存任何可还原输入行为的数据。
3. **不自带线程**：由 ``core/app.py`` 的 1 秒主循环驱动 :meth:`IdleDetector.poll`，
   避免多余线程与竞态；单次采样仅两次 Win32 API 调用，CPU 占用远低于 1%。
4. **可测试**：构造时允许注入 tick 读取函数，单元测试无需真实 Windows API
   即可覆盖 49.7 天回绕边界。
"""

import ctypes
import logging
import platform
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_UINT32_MASK = 0xFFFFFFFF
_IS_WINDOWS = platform.system() == "Windows"


class _LASTINPUTINFO(ctypes.Structure):
    """Win32 LASTINPUTINFO 结构。

    不用 ``ctypes.wintypes`` 是为了让本模块在非 Windows 平台也能被导入
    （便于 CI / 单元测试），实际调用仍仅限 Windows。
    """

    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("dwTime", ctypes.c_uint32),
    ]


if _IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    _GetLastInputInfo = _user32.GetLastInputInfo
    _GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    _GetLastInputInfo.restype = ctypes.c_int  # Win32 BOOL

    _GetTickCount64 = _kernel32.GetTickCount64
    _GetTickCount64.argtypes = []
    _GetTickCount64.restype = ctypes.c_ulonglong
else:
    _GetLastInputInfo = None
    _GetTickCount64 = None


def system_last_input_tick() -> int:
    """读取系统最后一次键鼠输入时的 32 位 tick。

    Returns:
        32 位无符号 tick 值（毫秒）。

    Raises:
        RuntimeError: 非 Windows 平台。
        OSError: ``GetLastInputInfo`` 调用失败。
    """
    if not _IS_WINDOWS or _GetLastInputInfo is None:
        raise RuntimeError("IdleDetector 仅支持 Windows（依赖 GetLastInputInfo）")
    info = _LASTINPUTINFO()
    # cbSize 必须由调用方填写，否则 Win32 会直接返回 FALSE。
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not _GetLastInputInfo(ctypes.byref(info)):
        raise ctypes.WinError()
    return int(info.dwTime)


def system_tick64() -> int:
    """读取 ``GetTickCount64``（系统启动以来的毫秒数，64 位）。

    Returns:
        64 位毫秒 tick。

    Raises:
        RuntimeError: 非 Windows 平台。
    """
    if not _IS_WINDOWS or _GetTickCount64 is None:
        raise RuntimeError("IdleDetector 仅支持 Windows（依赖 GetTickCount64）")
    return int(_GetTickCount64())


@dataclass(frozen=True)
class IdleSample:
    """一次空闲检测采样的结果。

    Attributes:
        idle_seconds: 系统已空闲的秒数。
        last_input_tick: 最后一次输入时的 32 位 tick。
        has_new_input: 相对上一次 ``poll`` 是否出现新的键鼠输入；
            首次采样为 True（程序启动即进入键鼠检测计时）。
        sampled_at: ``time.monotonic()`` 采样时刻，供主循环计算时间差。
    """

    idle_seconds: float
    last_input_tick: int
    has_new_input: bool
    sampled_at: float


class IdleDetector:
    """键鼠空闲检测器，由 1 秒主循环调用 :meth:`poll` 驱动。

    典型用法::

        detector = IdleDetector(idle_threshold_seconds=60)
        sample = detector.poll()
        if sample.has_new_input:      # 键鼠再次活动：立即切回键鼠检测
            ...
        if detector.is_idle(sample): # 空闲达阈值：启动摄像头抽检
            ...
    """

    def __init__(
        self,
        idle_threshold_seconds: int = 60,
        *,
        last_input_reader: Optional[Callable[[], int]] = None,
        tick64_reader: Optional[Callable[[], int]] = None,
    ) -> None:
        """初始化检测器。

        Args:
            idle_threshold_seconds: 空闲阈值（秒），达到后切换摄像头抽检。
            last_input_reader: 返回 32 位「最后输入 tick」的函数；
                None 时使用真实 Win32 API。供单元测试注入。
            tick64_reader: 返回 64 位当前 tick 的函数；
                None 时使用真实 ``GetTickCount64``。供单元测试注入。
        """
        self._threshold = max(1, int(idle_threshold_seconds))
        self._last_input_reader = last_input_reader or system_last_input_tick
        self._tick64_reader = tick64_reader or system_tick64
        self._lock = threading.RLock()
        self._last_tick: Optional[int] = None

    @property
    def idle_threshold_seconds(self) -> int:
        """当前空闲阈值（秒）。"""
        with self._lock:
            return self._threshold

    def set_idle_threshold(self, seconds: int) -> None:
        """热更新空闲阈值，供配置变更回调调用（无需重启程序）。

        Args:
            seconds: 新的阈值（秒），最小按 1 秒处理。
        """
        with self._lock:
            self._threshold = max(1, int(seconds))

    def poll(self) -> IdleSample:
        """采样一次系统空闲状态。

        使用 32 位无符号回绕减法计算空闲毫秒数：即使当前 tick 已跨过 2^32
        回绕到小于 ``last_input_tick``，结果依然正确（只要空闲时长 < 49.7 天）。

        Returns:
            本次采样结果 :class:`IdleSample`。

        Raises:
            RuntimeError: 非 Windows 平台且未注入读取函数。
            OSError: Win32 API 调用失败。
        """
        last_tick = int(self._last_input_reader()) & _UINT32_MASK
        now32 = int(self._tick64_reader()) & _UINT32_MASK
        elapsed_ms = (now32 - last_tick) & _UINT32_MASK

        with self._lock:
            # 首次采样视为「有活动」：程序启动后立即进入键鼠检测计时（文档 2.1）。
            has_new_input = self._last_tick is None or last_tick != self._last_tick
            self._last_tick = last_tick

        return IdleSample(
            idle_seconds=elapsed_ms / 1000.0,
            last_input_tick=last_tick,
            has_new_input=has_new_input,
            sampled_at=time.monotonic(),
        )

    def is_idle(self, sample: Optional[IdleSample] = None) -> bool:
        """判断空闲时长是否达到阈值。

        Args:
            sample: 已有采样；None 时立即采样一次。

        Returns:
            达到或超过阈值返回 True。
        """
        if sample is None:
            sample = self.poll()
        with self._lock:
            threshold = self._threshold
        return sample.idle_seconds >= threshold

    @staticmethod
    def has_activity(sample: IdleSample) -> bool:
        """本轮采样是否出现新的键鼠输入（用于立即中断摄像头抽检）。

        Args:
            sample: 本轮采样结果。

        Returns:
            出现新输入返回 True。
        """
        return sample.has_new_input

    def reset(self) -> None:
        """清除上次采样基准。

        在锁屏解锁、手动暂停恢复后调用：下一次 ``poll`` 会重新被视为「有活动」，
        对应文档 2.1「解锁 → 恢复键鼠活跃状态」，避免沿用锁屏前的旧 tick
        导致长时间误判为「无活动」。
        """
        with self._lock:
            self._last_tick = None
