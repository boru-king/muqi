"""应用编排层（P0-M9）：把状态机、检测、摄像头、SQLite、托盘、提醒窗口串起来。

线程模型
--------
* **UI 主线程**：隐藏的 ``tk.Tk`` 跑 ``mainloop()``；1 秒计时主循环用
  ``root.after(1000, ...)`` 驱动，状态机与所有 tkinter 操作都在该线程执行。
* **托盘线程**：``TrayController`` 内部维护，菜单回调只往 ``_actions`` 队列投递动作，
  由 UI 线程的 100ms 任务泵消费，**绝不跨线程调用 tkinter**。
* **摄像头线程**：单次抽检耗时约 1~2 秒，必须在后台线程执行，结果放入
  ``_pending_camera_result``，由下一帧 tick 交给状态机。

数据流（每一秒）::

    detector.poll() ─┐
    camera result ───┼─> EyeTimer.tick() ─> TimerOutput
    locked / paused ─┘                       ├─ events   → 弹窗 / 关窗 / 休息次数
                                            ├─ camera   → 后台抽检 / 取消
                                            └─ snapshot → 托盘 / 倒计时刷新

落盘策略
--------
* 每 30 秒、状态切出「用眼」、跨自然日、退出时各 flush 一次；
* ``_flush_pending`` 显式传入当天日期，避免跨日时把旧的一天算进新的一天；
* 「今日累计」显示值 = SQLite 当日值 + 状态机里尚未落盘的秒数。

本模块不实现锁屏检测（P1 的 ``core/lockscreen.py``）与声音提醒（P2），
但预留了 :meth:`MuQiApp.set_system_locked` 与 ``reminder_methods`` 中的 ``sound`` 分支。
"""

import logging
import queue
import threading
import time
import tkinter as tk
from typing import Any, Optional

from config import default as cfg
from config.default import Settings
from config.manager import SettingsManager, settings_manager
from core.camera import CameraDetector, CameraResult
from core.detector import IdleDetector
from core.lockscreen import LockScreenMonitor
from core.sound import SoundNotifier
from core.state import AppEvent, AppState, StateSnapshot
from core.timer import EyeTimer, TimerInput, TimerOutput
from data.cleanup import CleanupManager
from data.stats import StatsRepository, stats_repository, today_str
from ui.reminder import ReminderWindow
from ui.settings import SettingsWindow
from ui.tray import TrayCallbacks, TrayController

logger = logging.getLogger(__name__)

_TICK_INTERVAL_MS = 1000
_ACTION_PUMP_MS = 100
_FLUSH_INTERVAL_TICKS = 30


class MuQiApp:
    """目憩应用编排器。

    典型用法（``main.py``）::

        app = MuQiApp()
        app.run()

    构造参数支持依赖注入，便于在不打开摄像头/托盘的情况下做集成测试。
    """

    def __init__(
        self,
        *,
        root: Optional[tk.Tk] = None,
        settings_mgr: Optional[SettingsManager] = None,
        stats_repo: Optional[StatsRepository] = None,
        camera: Optional[CameraDetector] = None,
        tray: Optional[TrayController] = None,
        reminder: Optional[ReminderWindow] = None,
        cleanup: Optional[CleanupManager] = None,
        lock_monitor: Optional[LockScreenMonitor] = None,
        sound: Optional[SoundNotifier] = None,
    ) -> None:
        """组装各模块（此时不启动线程、不显示窗口、不打开摄像头）。

        Args:
            root: 隐藏的 tkinter 主窗口；None 时自行创建并 ``withdraw()``。
            settings_mgr: 配置管理器；None 时使用模块级单例。
            stats_repo: SQLite 仓库；None 时使用模块级单例。
            camera: 摄像头检测器；测试可注入 stub。
            tray: 托盘控制器；测试可注入 stub。
            reminder: 强制休息窗口；测试可注入 stub。
            cleanup: 数据清理编排器；测试可注入 stub，避免触发自动删数据。
            lock_monitor: 锁屏/休眠监视器；测试可注入 stub 固定锁定状态。
            sound: 声音提醒播放器；测试可注入 stub 避免真的发出声音。
        """
        self._root = root or tk.Tk()
        self._root.withdraw()  # 需求 5.4：启动后不弹主窗口

        self._settings_mgr = settings_mgr or settings_manager
        self._settings: Settings = self._settings_mgr.load()
        self._stats = stats_repo or stats_repository
        self._timer = EyeTimer(self._settings)
        self._detector = IdleDetector(
            int(self._settings["idle_threshold_seconds"])
        )
        self._camera = camera or CameraDetector()

        # 托盘回调只入队，不直接操作 UI 或状态机（可能来自托盘线程）。
        callbacks = TrayCallbacks(
            on_toggle_pause=lambda paused: self._actions.put(("pause", paused)),
            on_start_rest=lambda: self._actions.put(("rest", None)),
            on_open_settings=lambda: self._actions.put(("settings", None)),
            on_quit=lambda: self._actions.put(("quit", None)),
        )
        self._tray = tray or TrayController(callbacks)
        self._reminder = reminder or ReminderWindow(
            self._root, self._settings, on_cancel=self._on_cancel_rest
        )
        # 清理编排器：二次确认回调由 P1 的设置界面注入；自动清理先接上托盘通知，
        # 这样「自动保留最近 N 天」在 M11 就能生效，无需等设置界面。
        self._cleanup = cleanup or CleanupManager(
            self._stats, notifier=self._tray.notify, settings=self._settings
        )
        # 构造时即把二次确认弹窗注入清理编排器，手动清理才有确认入口。
        self._settings_window = SettingsWindow(
            self._root, self._settings_mgr, self._cleanup,
            stats_repo=self._stats,
        )
        # A 方案：由 1 秒主循环驱动，不额外起线程。
        self._lock_monitor = lock_monitor or LockScreenMonitor()
        self._sound = sound or SoundNotifier()

        # --- 运行期状态 ---
        self._actions: Any = queue.Queue()
        self._running = False
        self._shutdown_done = False
        self._tick_after_id: Optional[str] = None
        self._pump_after_id: Optional[str] = None
        self._tick_count = 0
        self._last_timing = False
        self._user_paused = False
        self._system_locked = False

        # --- 今日统计（内存镜像，DB 为持久来源） ---
        self._today = today_str()
        self._today_eye_seconds = 0
        self._today_rest_count = 0

        # --- 摄像头异步抽检 ---
        self._camera_lock = threading.Lock()
        self._pending_camera_result: Optional[CameraResult] = None
        self._camera_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._camera_error = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self) -> None:
        """启动应用并进入 tkinter 主循环（阻塞，直到退出）。"""
        self._stats.initialize()
        day = self._stats.get_day()
        self._today_eye_seconds = day.eye_seconds
        self._today_rest_count = day.rest_count

        self._settings_mgr.register_change_callback(self._on_settings_changed)
        self._running = True
        self._tray.start()
        # Q6：自动清理在「启动时」检查一次（同一自然日只执行一次）。
        self._cleanup.maybe_run_auto_cleanup()
        self._refresh_tray(self._timer.snapshot())
        self._schedule_tick()
        self._schedule_pump()
        logger.info("目憩已启动，进入后台常驻")
        try:
            self._root.mainloop()
        finally:
            self.shutdown()
            try:
                self._root.destroy()
            except tk.TclError:
                pass

    def shutdown(self) -> None:
        """停止所有循环、强制落盘并释放资源（幂等，必须在 UI 线程执行）。"""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._running = False

        for after_id in (self._tick_after_id, self._pump_after_id):
            if after_id:
                try:
                    self._root.after_cancel(after_id)
                except (tk.TclError, ValueError):
                    pass
        self._tick_after_id = None
        self._pump_after_id = None

        # 退出前把内存里剩余的用眼秒数写回数据库，避免丢失最多 30 秒统计。
        self._flush_pending()
        self._cancel_event.set()
        self._reminder.close()
        self._settings_window.close()
        try:
            self._tray.stop()
        except Exception:  # noqa: BLE001 - 退出流程不能因托盘异常中断
            logger.exception("停止托盘时发生异常")
        try:
            self._root.quit()
        except tk.TclError:
            pass
        logger.info("目憩已退出")

    def set_system_locked(self, locked: bool) -> None:
        """手动覆盖锁屏状态（可从任意线程调用，主要用于测试/外部集成）。

        生产路径已由 :class:`LockScreenMonitor` 在每秒主循环中驱动，
        本方法入队的设置会在下一帧被真实检测结果覆盖，属预期行为。

        Args:
            locked: True=锁屏/休眠，False=解锁。
        """
        self._actions.put(("locked", bool(locked)))

    def snapshot(self) -> StateSnapshot:
        """返回当前状态机快照。

        Returns:
            当前 :class:`StateSnapshot`。
        """
        return self._timer.snapshot()

    @property
    def cleanup(self) -> CleanupManager:
        """暴露清理编排器，供设置界面（P1-M12）生成计划并注入二次确认。"""
        return self._cleanup

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _schedule_tick(self) -> None:
        """注册下一帧 1 秒 tick。"""
        if not self._running:
            return
        self._tick_after_id = self._root.after(_TICK_INTERVAL_MS, self._tick)

    def _tick(self) -> None:
        """1 秒定时回调：执行一帧逻辑并续期。"""
        if not self._running:
            return
        try:
            self._do_tick()
        except Exception:  # noqa: BLE001 - 主循环不能因单帧异常停止
            logger.exception("计时主循环单帧异常")
        self._schedule_tick()

    def _do_tick(self) -> None:
        """一帧的全部编排逻辑（独立成方法，便于集成测试直接调用）。"""
        self._tick_count += 1
        self._check_day_rollover()

        # 0) 锁屏 / 休眠检测（A 方案）：先于键鼠采样，锁定后本帧不计时、不抽检。
        try:
            lock_state = self._lock_monitor.poll()
            self._apply_locked(lock_state.locked, lock_state.reason)
        except Exception as exc:  # noqa: BLE001 - 检测异常不中断主循环
            logger.warning("锁屏检测失败: %s", exc)

        # 1) 键鼠采样：设备/权限异常不能中断主循环
        try:
            sample = self._detector.poll()
        except Exception as exc:  # noqa: BLE001
            logger.warning("键鼠空闲检测失败: %s", exc)
            sample = None

        # 2) 取回后台摄像头结果（若有）
        with self._camera_lock:
            camera_result = self._pending_camera_result
            self._pending_camera_result = None
        if camera_result is not None and not camera_result.cancelled:
            self._camera_error = (
                "" if camera_result.available
                else (camera_result.error or "摄像头不可用")
            )

        # 3) 推进状态机
        output = self._timer.tick(
            TimerInput(
                now=time.monotonic(),
                idle_sample=sample,
                camera_result=camera_result,
                system_locked=self._system_locked,
                user_paused=self._user_paused,
            )
        )

        # 4) 执行外部动作
        if output.cancel_camera:
            self._cancel_event.set()
        if output.need_camera_check:
            self._schedule_camera_check()
        self._dispatch_events(output)

        # 5) 落盘：每 30 秒，或从「计时中」切出时
        timing_now = output.snapshot.timing_active
        if self._tick_count % _FLUSH_INTERVAL_TICKS == 0 or (
            self._last_timing and not timing_now
        ):
            self._flush_pending()
        self._last_timing = timing_now

        # 6) 刷新强制休息窗口的倒计时（UI 不自己计时）
        if output.snapshot.state == AppState.RESTING:
            self._reminder.update_remaining(
                output.snapshot.rest_remaining_seconds
            )

        # 7) 刷新托盘状态
        self._refresh_tray(output.snapshot)

    def _schedule_pump(self) -> None:
        """注册下一帧动作泵（100ms，让托盘操作近实时生效）。"""
        if not self._running:
            return
        self._pump_after_id = self._root.after(_ACTION_PUMP_MS, self._pump)

    def _pump(self) -> None:
        """在 UI 线程消费来自托盘/锁屏监控的动作。"""
        if not self._running:
            return
        try:
            while True:
                name, payload = self._actions.get_nowait()
                self._handle_action(name, payload)
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001 - 单个动作失败不应中断泵
            logger.exception("处理托盘动作失败")
        self._schedule_pump()

    def _handle_action(self, name: str, payload: Any) -> None:
        """执行一个由后台线程投递的动作（已在 UI 线程）。

        Args:
            name: 动作名（pause / rest / quit / locked）。
            payload: 动作参数。
        """
        if name == "pause":
            self._user_paused = bool(payload)
            if not self._user_paused:
                self._detector.reset()  # 恢复检测后重新建立键鼠活跃基准
            logger.info("检测已%s", "暂停" if self._user_paused else "恢复")
        elif name == "rest":
            self._dispatch_events(self._timer.start_rest_now())
        elif name == "settings":
            self._settings_window.show()
        elif name == "quit":
            self.shutdown()
        elif name == "locked":
            # 保留给外部/测试手动覆盖；生产路径由 _lock_monitor 每帧驱动。
            self._apply_locked(bool(payload))
        else:
            logger.warning("未知动作: %s", name)

    def _apply_locked(self, locked: bool, reason: str = "") -> None:
        """应用锁屏状态，只在状态**发生变化**时执行副作用。

        Args:
            locked: True=锁屏/休眠/安全桌面，False=已回到用户桌面。
            reason: 判定原因，仅写入日志。
        """
        locked = bool(locked)
        if locked == self._system_locked:
            return
        was_locked = self._system_locked
        self._system_locked = locked
        if locked:
            self._cancel_event.set()  # 锁屏/休眠立即中断进行中的摄像头抽检
        elif was_locked:
            # 文档 2.1：解锁 → 恢复键鼠活跃状态，避免沿用锁屏前的旧 tick。
            self._detector.reset()
        logger.info(
            "系统%s%s",
            "锁屏/休眠" if locked else "解锁",
            "（%s）" % reason if reason else "",
        )

    # ------------------------------------------------------------------
    # 事件分发
    # ------------------------------------------------------------------
    def _dispatch_events(self, output: TimerOutput) -> None:
        """把状态机事件翻译成 UI / 数据库动作。

        Args:
            output: 本轮状态机输出。
        """
        for event in output.events:
            if event is AppEvent.REST_STARTED:
                self._on_rest_started(output.snapshot)
            elif event in (AppEvent.REST_CANCELLED, AppEvent.REST_COMPLETED):
                self._reminder.close()

    def _on_rest_started(self, snapshot: StateSnapshot) -> None:
        """进入强制休息：记录休息次数、显示锁定窗口、附加提醒方式。

        休息次数按用户确认口径「每次触发强制休息 +1，提前取消不回退」，
        手动开始休息同样计数。

        Args:
            snapshot: 当前状态快照（含休息剩余秒数）。
        """
        # 休息期间桌面已锁定，不应再开启摄像头（也避免刚发起的抽检白跑一次）。
        self._cancel_event.set()
        self._stats.increment_rest_count(day=self._today)
        self._today_rest_count += 1

        rest_seconds = snapshot.rest_remaining_seconds
        if rest_seconds <= 0:
            rest_seconds = int(self._settings["rest_duration_minutes"]) * 60
        self._reminder.show(rest_seconds)

        methods = self._settings.get("reminder_methods", [])
        if "notification" in methods:
            self._tray.notify(
                cfg.APP_NAME,
                str(self._settings.get("reminder_text") or "该休息了"),
            )
        if "sound" in methods:
            result = self._sound.play()
            if not result.played:
                logger.warning("声音提醒播放失败: %s", result.error)

    def _on_cancel_rest(self) -> None:
        """「取消休息」按钮回调：通知状态机结束强制休息。"""
        self._dispatch_events(self._timer.cancel_rest())

    # ------------------------------------------------------------------
    # 摄像头抽检（后台线程）
    # ------------------------------------------------------------------
    def _schedule_camera_check(self) -> None:
        """启动一次后台抽检；已有抽检在进行时直接跳过。"""
        if self._camera_thread is not None and self._camera_thread.is_alive():
            return
        self._cancel_event.clear()
        self._camera_thread = threading.Thread(
            target=self._camera_worker, name="muqi-camera", daemon=True
        )
        self._camera_thread.start()

    def _camera_worker(self) -> None:
        """后台执行单次检测；无论成功失败都回填结果，避免状态机永久 pending。"""
        try:
            result = self._camera.detect_once(self._cancel_event)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须吞掉异常
            logger.exception("摄像头抽检线程异常")
            result = CameraResult(False, False, 0.0, str(exc))
        with self._camera_lock:
            self._pending_camera_result = result

    # ------------------------------------------------------------------
    # 统计与配置
    # ------------------------------------------------------------------
    def _flush_pending(self) -> None:
        """把状态机中未落盘的用眼秒数写入 SQLite（显式指定日期防跨日串账）。"""
        pending = self._timer.drain_pending_seconds()
        if pending <= 0:
            return
        try:
            self._stats.add_eye_seconds(pending, day=self._today)
            self._today_eye_seconds += pending
            # Q6：自动清理在「每日首次写库」时也检查一次（内部按自然日去重）。
            self._cleanup.maybe_run_auto_cleanup()
        except Exception:  # noqa: BLE001 - 写库失败不应中断计时主循环
            logger.exception("用眼时长落盘失败，丢失 %d 秒", pending)

    def _check_day_rollover(self) -> None:
        """自然日切换：先按旧日期落盘，再重置今日内存镜像。"""
        current = today_str()
        if current == self._today:
            return
        self._flush_pending()  # 此时 self._today 仍是旧日期
        self._today = current
        day = self._stats.get_day(current)
        self._today_eye_seconds = day.eye_seconds
        self._today_rest_count = day.rest_count
        logger.info("统计日期切换为 %s", current)

    def _on_settings_changed(self, settings: Settings) -> None:
        """配置变更回调：热更新状态机、检测器与提醒窗口。

        Args:
            settings: 新的完整配置。
        """
        self._settings = settings
        self._timer.apply_settings(settings)
        self._detector.set_idle_threshold(
            int(settings["idle_threshold_seconds"])
        )
        self._reminder.update_settings(settings)
        self._cleanup.apply_settings(settings)
        if not settings["camera_enabled"]:
            # 用户关闭摄像头：中断可能正在进行的抽检，状态机会自动回退纯键鼠模式。
            self._cancel_event.set()
        logger.info("配置已热更新")

    # ------------------------------------------------------------------
    # 托盘状态
    # ------------------------------------------------------------------
    def _refresh_tray(self, snapshot: StateSnapshot) -> None:
        """用状态快照刷新托盘菜单文本。

        Args:
            snapshot: 当前状态快照。
        """
        label = snapshot.state_label
        if self._camera_error:
            label += "（摄像头异常）"
        today_total = self._today_eye_seconds + self._timer.pending_seconds
        self._tray.update_status(
            label,
            today_total,
            paused=self._user_paused,
            rest_remaining=snapshot.rest_remaining_seconds,
        )
