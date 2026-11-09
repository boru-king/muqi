"""核心状态契约：状态枚举、检测模式、事件类型与状态快照。

本模块只定义「数据」，不含任何业务逻辑，也不依赖 UI / 摄像头 / 数据库，
供 ``timer`` / ``app`` / ``tray`` 共享，避免模块间循环 import。

状态模型对应用户确认的口径（2026-11-06）：

* 连续用眼计时器 ``continuous_eye_seconds`` **单调递增、不归零**；
  提醒在 ``interval × n`` 秒处触发，触发后 ``next_reminder_at_seconds += interval``。
* ``RESTING`` = 提醒弹窗 + 桌面锁定 + 休息倒计时三者同时成立的强制休息态，
  期间完全不累加用眼时长；点「取消休息」或倒计时结束才恢复计时，且累计值保留。
* 只有程序退出 / 关机重开，连续计时器才归零。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict


class AppState(Enum):
    """应用主状态（互斥）。托盘按此显示：用眼中 / 空闲 / 休息中 / 锁屏。"""

    USING = "using"                  # 用眼中：键鼠活跃，或摄像头抽检判定有人
    IDLE = "idle"                    # 空闲：键鼠无活动（是否计时由状态机按空闲阈值决定）
    CAMERA_CHECK = "camera_check"    # 摄像头抽检中：空闲已超阈值，按设定间隔开合摄像头
    RESTING = "resting"              # 强制休息：提醒内容 + 桌面锁定 + 倒计时，不计时
    LOCKED = "locked"                # 系统锁屏 / 休眠：停止全部检测与计时
    PAUSED = "paused"                # 用户在托盘手动暂停检测


class DetectionMode(Enum):
    """当前采用哪种判据决定「是否用眼」。"""

    KEYBOARD_MOUSE = "keyboard_mouse"  # 键鼠活跃检测（GetLastInputInfo）
    CAMERA = "camera"                  # 摄像头抽检（每次检测完立即释放）
    NONE = "none"                      # 锁屏 / 手动暂停时不做任何检测


class AppEvent(Enum):
    """状态机向外发布的事件，供托盘、弹窗、统计等订阅。"""

    STATE_CHANGED = "state_changed"
    REMINDER_TRIGGERED = "reminder_triggered"   # 达到提醒阈值，进入强制休息
    REST_STARTED = "rest_started"               # 休息倒计时开始（与 REMINDER_TRIGGERED 同时）
    REST_CANCELLED = "rest_cancelled"           # 用户点「取消休息」，提前恢复
    REST_COMPLETED = "rest_completed"           # 倒计时自然结束
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    PAUSED = "paused"
    RESUMED = "resumed"
    SETTINGS_CHANGED = "settings_changed"
    DATA_FLUSHED = "data_flushed"


# 托盘菜单与状态栏使用的中文标签（文档 2.3）
STATE_LABELS: Dict[AppState, str] = {
    AppState.USING: "用眼中",
    AppState.IDLE: "空闲",
    AppState.CAMERA_CHECK: "摄像头抽检中",
    AppState.RESTING: "休息中",
    AppState.LOCKED: "锁屏",
    AppState.PAUSED: "已暂停",
}


@dataclass(frozen=True)
class StateSnapshot:
    """状态机对外暴露的只读快照。

    使用 ``frozen=True`` 保证跨线程传递时不会被订阅者意外修改；
    后台线程只负责生成快照，UI 线程消费快照。

    Attributes:
        state: 当前主状态。
        detection_mode: 当前检测判据。
        continuous_eye_seconds: 本次进程启动以来的连续用眼累计（重启归零）。
        next_reminder_at_seconds: 下一次触发提醒的累计秒数阈值。
        rest_remaining_seconds: 强制休息剩余秒数；非休息态为 0。
        daily_eye_seconds: 今日累计用眼秒数（来自 SQLite，重启后延续）。
        today_rest_count: 今日休息次数。
        timing_active: 当前这一秒是否正在累加用眼时间。
    """

    state: AppState = AppState.IDLE
    detection_mode: DetectionMode = DetectionMode.KEYBOARD_MOUSE
    continuous_eye_seconds: int = 0
    next_reminder_at_seconds: int = 0
    rest_remaining_seconds: int = 0
    daily_eye_seconds: int = 0
    today_rest_count: int = 0
    timing_active: bool = False

    @property
    def state_label(self) -> str:
        """返回用于托盘显示的中文状态名。"""
        return STATE_LABELS.get(self.state, "未知")


# 事件订阅者签名：(事件, 该事件发生后的状态快照) -> None
StateListener = Callable[[AppEvent, StateSnapshot], None]


def format_duration(seconds: int) -> str:
    """把秒数格式化为「X 小时 Y 分」，用于托盘显示今日累计用眼时长。

    Args:
        seconds: 秒数，负数按 0 处理。

    Returns:
        中文时长文本，例如 ``3780 -> "1 小时 3 分"``、``45 -> "0 分"``。
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return "%d 小时 %d 分" % (hours, minutes)
    return "%d 分" % minutes


def format_countdown(seconds: int) -> str:
    """把秒数格式化为 ``MM:SS``，用于休息倒计时窗口。

    Args:
        seconds: 剩余秒数，负数按 0 处理。

    Returns:
        例如 ``905 -> "15:05"``；超过 99 分钟时分钟数照实显示。
    """
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return "%02d:%02d" % (minutes, secs)
