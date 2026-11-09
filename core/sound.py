"""声音提醒（P2-M16）：``winsound``，Windows 标准库，零新增依赖。

设计要点
--------
* 用 ``PlaySound("SystemAsterisk", SND_ALIAS | SND_ASYNC)`` 播放系统提示音：
  **异步**播放，不阻塞 UI 主线程；音色温和不刺耳，符合「温和不打扰」。
* 该调用在部分系统/无声卡环境会失败，回退到 ``MessageBeep``；
  再失败也只返回 ``SoundResult(played=False)`` 并记日志，**绝不抛异常打断提醒流程**。
* ``player`` 可注入，便于单元测试时不真的发出声音。

接入点：``MuQiApp._on_rest_started`` 在 ``reminder_methods`` 含 ``sound`` 时调用
:meth:`SoundNotifier.play`（与弹窗、托盘通知并列，可多选）。
"""

import logging
import platform
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SoundResult:
    """一次声音播放的结果。

    Attributes:
        played: 是否成功触发播放。
        method: 实际使用的播放方式（诊断用）。
        error: 失败原因；成功为空串。
    """

    played: bool
    method: str = ""
    error: str = ""


def _default_player() -> str:
    """播放默认系统提示音，返回使用的方式名。

    Returns:
        播放方式名，例如 ``"PlaySound(SystemAsterisk)"``。

    Raises:
        Exception: 两种播放方式都失败时向上抛出，由 :meth:`SoundNotifier.play` 兜底。
    """
    import winsound  # 延迟导入：非 Windows 平台 import 本模块也不会失败

    try:
        winsound.PlaySound(
            "SystemAsterisk",
            winsound.SND_ALIAS | winsound.SND_ASYNC,
        )
        return "PlaySound(SystemAsterisk)"
    except Exception:  # noqa: BLE001 - 系统音效不可用时回退到蜂鸣
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        return "MessageBeep"


class SoundNotifier:
    """提醒音效播放器（无状态，线程安全）。"""

    def __init__(self, *, player: Optional[Callable[[], str]] = None) -> None:
        """初始化。

        Args:
            player: 返回播放方式名的可调用对象；None 时使用 ``winsound`` 默认实现。
        """
        self._player = player or _default_player

    def play(self) -> SoundResult:
        """播放一次提醒音。

        Returns:
            :class:`SoundResult`；任何异常都被吞掉并转换为 ``played=False``。
        """
        if platform.system() != "Windows":
            return SoundResult(False, error="声音提醒仅支持 Windows")
        try:
            method = self._player()
            logger.debug("已播放提醒音: %s", method)
            return SoundResult(True, method=method)
        except Exception as exc:  # noqa: BLE001 - 提醒流程不能被声音打断
            logger.warning("声音提醒播放失败: %s", exc)
            return SoundResult(False, error=str(exc))
