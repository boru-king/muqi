"""用眼 / 休息状态机（P0-M5，项目核心）。

职责边界
--------
* 本模块是**纯逻辑**：不 import tkinter / cv2 / sqlite3，不自己起线程，不直接弹窗。
  所有外部动作由 ``core/app.py``（M9）根据 :class:`TimerOutput` 编排。
* 输入是每秒一次的 :class:`TimerInput`（键鼠采样 + 摄像头结果 + 锁屏/暂停标志）；
  输出是 :class:`TimerOutput`（状态快照 + 事件 + 摄像头调度信号）。
* 「连续计时器」只在内存中，进程重启归零；「今日累计」由 SQLite 负责，
  状态机只通过 :meth:`EyeTimer.drain_pending_seconds` 交出待落盘的秒数。
* ``StateSnapshot.daily_eye_seconds`` / ``today_rest_count`` 在本模块恒为 0，
  由 app.py 用 ``dataclasses.replace`` 填入数据库读数后再分发给 UI。

已确认口径（2026-11-06）
------------------------
1. ``continuous_eye_seconds`` **单调递增、不归零**；提醒在 ``interval × n`` 处触发，
   触发后把 ``next_reminder_at`` 对齐到下一个倍数；只有进程重启归零。
2. 提醒触发 = **立即进入强制休息**（``RESTING``）+ 桌面锁定 + 倒计时自动开始；
   休息期间**完全不累加**用眼时长；点「取消休息」或倒计时结束后从原累计值继续，
   ``next_reminder_at`` 不再回退。
3. 键鼠活动是最高优先级，立即中断摄像头抽检；空闲 ≥ 阈值后按设定间隔抽检；
   检测到人继续累加，无人停止累加但继续按周期抽检；锁屏 / 休眠 / 手动暂停停止一切。
4. 设置里改「用眼提醒间隔」时按**方案 A** 重算：
   ``next = (continuous // new_interval + 1) × new_interval``。
5. 摄像头设备不可用（``available=False``）时保守视为「有人」继续计时，
   避免设备故障导致用户实际在用眼却永远不触发提醒；该情况由 app.py 提示用户。
"""

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from config.default import DEFAULT_SETTINGS, Settings
from core.detector import IdleSample
from core.state import AppEvent, AppState, DetectionMode, StateSnapshot

if TYPE_CHECKING:  # 避免运行时 import cv2，让状态机可在无 OpenCV 环境单测
    from core.camera import CameraResult

_SECONDS_PER_TICK = 1
# 主循环两次 tick 间隔超过该值说明进程被挂起（休眠/睡眠）：不补算挂起时长。
_MAX_TICK_GAP_SECONDS = 5.0


@dataclass(frozen=True)
class TimerInput:
    """主循环每秒喂给状态机的一次输入。

    Attributes:
        now: ``time.monotonic()`` 值，用于摄像头抽检间隔与休眠跳变检测。
        idle_sample: 本轮键鼠空闲采样；None 表示本轮无采样（按无活动处理）。
        camera_result: 上一轮异步抽检回来的结果；None 表示本轮没有新结果。
        system_locked: 系统是否处于锁屏 / 休眠（P1 的 lockscreen.py 提供）。
        user_paused: 用户是否在托盘手动暂停检测。
    """

    now: float
    idle_sample: Optional[IdleSample] = None
    camera_result: Optional["CameraResult"] = None
    system_locked: bool = False
    user_paused: bool = False


@dataclass(frozen=True)
class TimerOutput:
    """状态机每轮的输出，供 app.py 编排外部动作。

    Attributes:
        snapshot: 本轮结束后的状态快照。
        events: 本轮发生的事件，按发生顺序排列。
        need_camera_check: 是否应发起一次摄像头抽检（app.py 需在后台线程执行，
            因为打开设备约 1~2 秒，不能阻塞 1 秒主循环）。
        cancel_camera: 键鼠恢复活动，是否应置位取消标志以中断进行中的抽检。
    """

    snapshot: StateSnapshot
    events: Tuple[AppEvent, ...] = ()
    need_camera_check: bool = False
    cancel_camera: bool = False


class EyeTimer:
    """用眼 / 休息状态机；线程安全，由 1 秒主循环驱动 :meth:`tick`。

    典型用法::

        timer = EyeTimer(settings)
        out = timer.tick(TimerInput(now=time.monotonic(), idle_sample=sample))
        for event in out.events:
            app.dispatch(event, out.snapshot)
        if out.need_camera_check:
            app.schedule_camera_check()
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        """初始化状态机（此时不打开任何设备、不弹任何窗口）。

        Args:
            settings: 初始配置；None 时使用 ``DEFAULT_SETTINGS``。
        """
        self._lock = threading.RLock()

        # --- 计时状态 ---
        self._continuous_seconds: int = 0      # 单调递增，仅重启归零
        self._pending_seconds: int = 0         # 尚未落盘的用眼秒数
        self._next_reminder_at: int = 0        # 下一次提醒的累计秒数阈值
        self._rest_remaining: int = 0          # 强制休息剩余秒数

        # --- 运行状态 ---
        self._state: AppState = AppState.IDLE
        self._detection_mode: DetectionMode = DetectionMode.KEYBOARD_MOUSE
        self._timing: bool = False             # 本秒是否正在累加用眼时间
        self._camera_absent: bool = False      # 最近一次摄像头判定是否无人
        self._camera_check_pending: bool = False
        self._last_camera_check_at: Optional[float] = None
        self._last_tick_at: Optional[float] = None
        self._system_locked: bool = False
        self._user_paused: bool = False

        # --- 配置（秒） ---
        cfg: Dict[str, Any] = dict(settings) if settings else dict(DEFAULT_SETTINGS)
        self._reminder_interval: int = max(1, int(cfg["reminder_interval_minutes"])) * 60
        self._rest_duration: int = max(1, int(cfg["rest_duration_minutes"])) * 60
        self._idle_threshold: int = max(1, int(cfg["idle_threshold_seconds"]))
        self._camera_interval: int = max(1, int(cfg["camera_check_interval_seconds"]))
        self._camera_enabled: bool = bool(cfg["camera_enabled"])
        self._next_reminder_at = self._reminder_interval

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def snapshot(self) -> StateSnapshot:
        """返回当前状态快照（线程安全）。

        Returns:
            当前 :class:`StateSnapshot`。
        """
        with self._lock:
            return self._build_snapshot()

    def apply_settings(self, settings: Settings) -> TimerOutput:
        """热更新配置；仅「提醒间隔」变化时才重算下一次提醒阈值。

        采用方案 A：``next = (continuous // new_interval + 1) × new_interval``，
        保证改完设置不会立刻弹窗，且始终保持「每累计 N 分钟提醒一次」的节拍。

        Args:
            settings: 新的完整配置。

        Returns:
            含 ``SETTINGS_CHANGED`` 事件的 :class:`TimerOutput`。
        """
        with self._lock:
            cfg: Dict[str, Any] = dict(settings)
            new_interval = max(1, int(cfg["reminder_interval_minutes"])) * 60
            interval_changed = new_interval != self._reminder_interval

            self._reminder_interval = new_interval
            self._rest_duration = max(1, int(cfg["rest_duration_minutes"])) * 60
            self._idle_threshold = max(1, int(cfg["idle_threshold_seconds"]))
            self._camera_interval = max(1, int(cfg["camera_check_interval_seconds"]))
            self._camera_enabled = bool(cfg["camera_enabled"])

            if interval_changed:
                self._recompute_next_reminder()

            return TimerOutput(
                self._build_snapshot(), (AppEvent.SETTINGS_CHANGED,)
            )

    def tick(self, inp: TimerInput) -> TimerOutput:
        """推进一秒，返回本轮状态与需要执行的外部动作。

        Args:
            inp: 本轮输入。

        Returns:
            :class:`TimerOutput`；任何情况下都不抛设备相关异常。
        """
        with self._lock:
            events: List[AppEvent] = []
            need_camera_check = False
            cancel_camera = False

            # 0) 休眠 / 挂起兜底（Q3 方案 A）：间隔异常大说明进程被冻结，
            #    不补算挂起时长，也不推进休息倒计时，本轮直接返回。
            if (
                self._last_tick_at is not None
                and inp.now - self._last_tick_at > _MAX_TICK_GAP_SECONDS
            ):
                self._last_tick_at = inp.now
                return TimerOutput(self._build_snapshot())
            self._last_tick_at = inp.now

            # 1) 记录外部状态变化（锁屏 / 手动暂停）
            if inp.system_locked and not self._system_locked:
                events.append(AppEvent.LOCKED)
            if not inp.system_locked and self._system_locked:
                events.append(AppEvent.UNLOCKED)
            if inp.user_paused and not self._user_paused:
                events.append(AppEvent.PAUSED)
            if not inp.user_paused and self._user_paused:
                events.append(AppEvent.RESUMED)
            self._system_locked = inp.system_locked
            self._user_paused = inp.user_paused

            # 2) 消费异步回传的摄像头结果
            if inp.camera_result is not None:
                self._camera_check_pending = False
                result = inp.camera_result
                if not result.cancelled:
                    if result.available:
                        self._camera_absent = not result.face_detected
                    else:
                        # 设备故障：保守视为有人，避免「用眼却不计时」的功能失效。
                        self._camera_absent = False

            sample = inp.idle_sample
            has_activity = bool(sample and sample.has_new_input)
            idle_seconds = float(sample.idle_seconds) if sample else 0.0
            old_state = self._state

            # 3) 状态分派（优先级：强制休息 > 手动暂停 > 锁屏 > 正常检测）
            if self._state == AppState.RESTING:
                # 强制休息：只递减倒计时，完全不计时，也不响应键鼠/摄像头输入。
                self._detection_mode = DetectionMode.NONE
                self._timing = False
                self._rest_remaining = max(0, self._rest_remaining - _SECONDS_PER_TICK)
                if self._rest_remaining == 0:
                    events.append(AppEvent.REST_COMPLETED)
                    self._exit_rest()

            elif inp.user_paused:
                self._state = AppState.PAUSED
                self._detection_mode = DetectionMode.NONE
                self._timing = False
                self._camera_check_pending = False

            elif inp.system_locked:
                self._state = AppState.LOCKED
                self._detection_mode = DetectionMode.NONE
                self._timing = False
                if self._camera_check_pending:
                    cancel_camera = True
                self._camera_check_pending = False

            else:
                need_camera_check, cancel_camera = self._run_normal_detection(
                    inp, has_activity, idle_seconds
                )

            # 4) 计时（只有正常检测分支可能把 _timing 置 True）
            if self._timing:
                self._continuous_seconds += _SECONDS_PER_TICK
                self._pending_seconds += _SECONDS_PER_TICK

            # 5) 达到提醒阈值 → 立即进入强制休息
            if (
                self._state != AppState.RESTING
                and not inp.user_paused
                and not inp.system_locked
                and self._continuous_seconds >= self._next_reminder_at
            ):
                self._enter_rest(events, reminder_triggered=True)

            if self._state != old_state:
                events.append(AppEvent.STATE_CHANGED)

            return TimerOutput(
                snapshot=self._build_snapshot(),
                events=tuple(events),
                need_camera_check=need_camera_check,
                cancel_camera=cancel_camera,
            )

    def cancel_rest(self) -> TimerOutput:
        """用户点击「取消休息」：立即结束强制休息，下一步主循环恢复计时。

        累计值保持原样（例如第 60 分钟触发、休息 5 分钟后取消，仍从 3600s 继续）。

        Returns:
            含 ``REST_CANCELLED`` 事件的 :class:`TimerOutput`；
            非休息态时返回空事件。
        """
        with self._lock:
            events: List[AppEvent] = []
            if self._state == AppState.RESTING:
                events.append(AppEvent.REST_CANCELLED)
                events.append(AppEvent.STATE_CHANGED)
                self._exit_rest()
            return TimerOutput(self._build_snapshot(), tuple(events))

    def start_rest_now(self) -> TimerOutput:
        """托盘「手动开始休息」：进入同样的强制休息，但不弹提醒内容。

        同样消费一个提醒周期（把 ``next_reminder_at`` 推到下一个倍数），
        避免休息结束后因为累计值已达阈值而立刻再次弹提醒。

        Returns:
            含 ``REST_STARTED`` 事件的 :class:`TimerOutput`。
        """
        with self._lock:
            events: List[AppEvent] = []
            if self._state != AppState.RESTING:
                self._enter_rest(events, reminder_triggered=False)
                events.append(AppEvent.STATE_CHANGED)
            return TimerOutput(self._build_snapshot(), tuple(events))

    @property
    def pending_seconds(self) -> int:
        """只读查看尚未落盘的用眼秒数（不清零，供 UI 实时显示今日累计）。

        Returns:
            自上次 :meth:`drain_pending_seconds` 以来累计的秒数。
        """
        with self._lock:
            return self._pending_seconds

    def drain_pending_seconds(self) -> int:
        """取出并清零尚未落盘的用眼秒数，供 app.py 写入 SQLite。

        Returns:
            自上次调用以来累计的用眼秒数。
        """
        with self._lock:
            pending = self._pending_seconds
            self._pending_seconds = 0
            return pending

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _run_normal_detection(
        self, inp: TimerInput, has_activity: bool, idle_seconds: float
    ) -> Tuple[bool, bool]:
        """正常检测分支：决定状态、计时开关与摄像头调度信号。

        Args:
            inp: 本轮输入。
            has_activity: 本轮是否出现新的键鼠输入。
            idle_seconds: 系统空闲秒数。

        Returns:
            ``(need_camera_check, cancel_camera)``。
        """
        # 键鼠活动 = 最高优先级：立即切回键鼠检测并中断摄像头抽检。
        if has_activity:
            cancel_camera = self._camera_check_pending
            self._camera_check_pending = False
            self._camera_absent = False
            self._last_camera_check_at = None  # 下次空闲后立即可再抽检
            self._state = AppState.USING
            self._detection_mode = DetectionMode.KEYBOARD_MOUSE
            self._timing = True
            return False, cancel_camera

        # 摄像头被用户关闭：纯键鼠模式，空闲超阈值即视为未用眼。
        if not self._camera_enabled:
            self._detection_mode = DetectionMode.KEYBOARD_MOUSE
            self._camera_check_pending = False
            if idle_seconds >= self._idle_threshold:
                self._state = AppState.IDLE
                self._timing = False
            else:
                self._state = AppState.USING
                self._timing = True
            return False, False

        # 空闲未达阈值：仍按键鼠计时（看屏幕/思考等短暂停顿不算离开）。
        if idle_seconds < self._idle_threshold:
            self._state = AppState.USING
            self._detection_mode = DetectionMode.KEYBOARD_MOUSE
            self._timing = True
            return False, False

        # 空闲 ≥ 阈值：切换到摄像头抽检模式。
        self._detection_mode = DetectionMode.CAMERA
        last = self._last_camera_check_at
        due = not self._camera_check_pending and (
            last is None or inp.now - last >= self._camera_interval
        )
        need_camera_check = False
        if due:
            need_camera_check = True
            self._camera_check_pending = True
            self._last_camera_check_at = inp.now

        # 文档 2.1：空闲 ≥ 阈值即「暂停键鼠计时」，改由摄像头结果决定。
        # 抽检是异步的（打开设备约 1~2 秒），等待结果期间**不能**继续按旧逻辑
        # 计时——否则当「空闲阈值 ≈ 提醒间隔」时，摄像头还没出结果就先触发了
        # 强制休息，用户会看到「没经过摄像头判断就直接进休息界面」。
        if self._camera_check_pending:
            self._state = AppState.CAMERA_CHECK
            self._timing = False
        elif self._camera_absent:
            # 判定无人：暂停累加但不清空累计值，检测到人后从那一刻继续。
            self._state = AppState.IDLE
            self._timing = False
        else:
            # 已拿到「有人」结果：恢复正常计时。
            self._state = AppState.USING
            self._timing = True

        return need_camera_check, False

    def _recompute_next_reminder(self) -> None:
        """方案 A：把下一次提醒对齐到「大于当前累计值的最小 interval 倍数」。"""
        self._next_reminder_at = (
            self._continuous_seconds // self._reminder_interval + 1
        ) * self._reminder_interval

    def _enter_rest(self, events: List[AppEvent], *, reminder_triggered: bool) -> None:
        """进入强制休息：消费当前提醒周期、启动倒计时、停止一切检测与计时。

        Args:
            events: 事件列表，本方法会向其中追加事件。
            reminder_triggered: 是否由达到提醒阈值触发（False 表示手动休息）。
        """
        # 用方案 A 公式而非简单 += interval，这样即使累计值跨过多个周期
        # （例如休眠后跳变）也不会出现「退出休息立刻又提醒」。
        self._recompute_next_reminder()
        self._rest_remaining = self._rest_duration
        self._state = AppState.RESTING
        self._detection_mode = DetectionMode.NONE
        self._timing = False
        self._camera_check_pending = False
        self._camera_absent = False
        if reminder_triggered:
            events.append(AppEvent.REMINDER_TRIGGERED)
        events.append(AppEvent.REST_STARTED)

    def _exit_rest(self) -> None:
        """退出强制休息：累计值与下一次提醒阈值都**保持不变**。

        下一个 tick 会由键鼠采样重新判定是否恢复计时，因此「点取消后多久开始计时」
        最多有 1 秒主循环延迟，符合文档 1 秒轮询的精度。
        """
        self._rest_remaining = 0
        self._state = AppState.IDLE
        self._detection_mode = DetectionMode.KEYBOARD_MOUSE
        self._timing = False
        self._camera_absent = False
        self._last_camera_check_at = None

    def _build_snapshot(self) -> StateSnapshot:
        """构造状态快照（调用方需已持有 ``self._lock``）。"""
        return StateSnapshot(
            state=self._state,
            detection_mode=self._detection_mode,
            continuous_eye_seconds=self._continuous_seconds,
            next_reminder_at_seconds=self._next_reminder_at,
            rest_remaining_seconds=self._rest_remaining,
            timing_active=self._timing,
        )
