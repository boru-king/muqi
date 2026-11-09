"""历史数据清理（P1-M11）：手动按时间范围 + 自动保留最近 N 天。

**二次确认是硬约束**：:meth:`CleanupManager.execute` 默认要求先经过
``confirm(plan)`` 回调，且**没有注入确认回调时一律拒绝删除**（安全默认），
不会静默丢数据。UI 层（P1 的 ``ui/settings.py``）负责弹出确认框，
展示 ``CleanupPlan.scope`` 与 ``affected_rows`` 后把用户选择回传。

**自动清理不需要弹确认**（Q6 已确认）：由 ``maybe_run_auto_cleanup`` 在
程序启动时与每次写库后调用，同一自然日只检查一次；执行后通过 ``notifier``
发托盘提示并写日志，不打断后台常驻体验。

**清理后必定尝试 VACUUM**：删除提交后调用 ``StatsRepository.vacuum()``，
VACUUM 失败只记 warning，不影响已经完成的删除结果。

分层：本模块是业务编排，实际 SQL 在 ``data/stats.py``；确认框在 UI 层。
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, Optional

from config.default import Settings
from data.stats import StatsRepository, stats_repository, today_str

logger = logging.getLogger(__name__)

_MODE_BEFORE = "before"   # 删除某日期之前（保留最近 N 天）
_MODE_RANGE = "range"     # 删除自定义起止区间
_MODE_ALL = "all"         # 删除全部（含今日）
_DEFAULT_KEEP_DAYS = 90


@dataclass(frozen=True)
class CleanupPlan:
    """一次清理的「预览」，供二次确认弹窗展示，不含任何执行动作。

    Attributes:
        scope: 人类可读的范围描述，例如「清除 2026-10-11 之前的全部记录」。
        mode: 清理模式（before / range / all）。
        affected_rows: 将被删除的记录行数。
        cutoff_date: before 模式的截止日期，删除严格早于该日期的记录。
        start_date / end_date: range 模式的闭区间。
        automatic: 是否由自动策略触发（True 时跳过二次确认）。
    """

    scope: str
    mode: str
    affected_rows: int
    cutoff_date: str = ""
    start_date: str = ""
    end_date: str = ""
    automatic: bool = False


@dataclass(frozen=True)
class CleanupResult:
    """清理执行结果。

    Attributes:
        plan: 对应的清理计划。
        deleted_rows: 实际删除行数。
        vacuumed: VACUUM 是否成功。
        cancelled: 用户取消 / 未配置确认回调而拒绝执行。
        error: 失败原因文本，成功时为空串。
    """

    plan: CleanupPlan
    deleted_rows: int
    vacuumed: bool
    cancelled: bool = False
    error: str = ""


def retention_cutoff(keep_days: int) -> str:
    """计算「保留最近 N 天」对应的删除截止日期。

    保留区间为 ``[cutoff, today]``，共 N 个自然日（含今天），
    因此 ``cutoff = today - (N - 1) 天``；删除严格早于 cutoff 的记录。

    Args:
        keep_days: 需要保留的天数，最小按 1 处理。

    Returns:
        ISO 日期字符串 ``YYYY-MM-DD``。
    """
    days = max(1, int(keep_days))
    return (date.today() - timedelta(days=days - 1)).isoformat()


class CleanupManager:
    """历史数据清理编排器（不依赖 tkinter，可独立单测）。"""

    def __init__(
        self,
        repo: Optional[StatsRepository] = None,
        *,
        confirm: Optional[Callable[[CleanupPlan], bool]] = None,
        notifier: Optional[Callable[[str, str], None]] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        """初始化。

        Args:
            repo: SQLite 仓库；None 时使用模块级单例。
            confirm: 二次确认回调，返回 True 才允许删除；
                None 时手动清理一律被拒绝（安全默认）。
            notifier: 清理完成后的托盘通知回调 ``(title, message)``。
            settings: 初始配置；None 时自动清理默认关闭，需后续 ``apply_settings``。
        """
        self._repo = repo or stats_repository
        self._confirm = confirm
        self._notifier = notifier
        cfg: Dict[str, Any] = dict(settings) if settings else {}
        self._auto_enabled = bool(cfg.get("auto_cleanup_enabled", False))
        self._keep_days = max(1, int(cfg.get("auto_cleanup_keep_days", _DEFAULT_KEEP_DAYS)))
        # 同一自然日只做一次自动清理检查，避免每次落盘都查库。
        self._last_auto_date: Optional[str] = None

    def set_confirm(
        self, confirm: Optional[Callable[[CleanupPlan], bool]]
    ) -> None:
        """设置二次确认回调（由 P1 的设置界面注入）。

        Args:
            confirm: 展示确认框并返回用户选择的回调；None 表示撤销确认入口，
                此后所有手动清理都会被安全拒绝。
        """
        self._confirm = confirm

    @property
    def auto_cleanup_enabled(self) -> bool:
        """自动清理策略是否已启用。"""
        return self._auto_enabled

    @property
    def keep_days(self) -> int:
        """自动策略保留的天数。"""
        return self._keep_days

    def apply_settings(self, settings: Settings) -> None:
        """热更新自动清理配置（供配置变更回调调用）。

        Args:
            settings: 新的完整配置。
        """
        self._auto_enabled = bool(settings["auto_cleanup_enabled"])
        self._keep_days = max(1, int(settings["auto_cleanup_keep_days"]))
        # 用户改了策略后允许当天立即重新检查一次。
        self._last_auto_date = None

    # ------------------------------------------------------------------
    # 生成清理计划（只查询、不删除）
    # ------------------------------------------------------------------
    def plan_before(self, days: int, *, automatic: bool = False) -> CleanupPlan:
        """生成「清除 N 天前 / 保留最近 N 天」的计划。

        Args:
            days: 保留天数。
            automatic: 是否标记为自动策略触发。

        Returns:
            :class:`CleanupPlan`；``affected_rows`` 为将被删除的行数。
        """
        keep = max(1, int(days))
        cutoff = retention_cutoff(keep)
        rows = self._repo.count_rows_before(cutoff)
        scope = "清除 %s 之前的全部记录（保留最近 %d 天，共 %d 条）" % (
            cutoff, keep, rows,
        )
        return CleanupPlan(
            scope=scope, mode=_MODE_BEFORE, affected_rows=rows,
            cutoff_date=cutoff, automatic=automatic,
        )

    def plan_range(self, start_date: str, end_date: str) -> CleanupPlan:
        """生成自定义起止日期的清理计划（闭区间）。

        Args:
            start_date: 起始日期 ``YYYY-MM-DD``。
            end_date: 结束日期 ``YYYY-MM-DD``。

        Returns:
            :class:`CleanupPlan`；起止顺序颠倒时自动交换。
        """
        start, end = (start_date, end_date) if start_date <= end_date else (end_date, start_date)
        rows = self._repo.count_rows_in_range(start, end)
        scope = "清除 %s ~ %s 的记录（共 %d 条）" % (start, end, rows)
        return CleanupPlan(
            scope=scope, mode=_MODE_RANGE, affected_rows=rows,
            start_date=start, end_date=end,
        )

    def plan_all(self) -> CleanupPlan:
        """生成「清除全部历史记录（含今日）」的计划。

        Returns:
            :class:`CleanupPlan`。
        """
        rows = len(self._repo.get_all())
        return CleanupPlan(
            scope="清除全部历史记录，含今日（共 %d 条）" % rows,
            mode=_MODE_ALL,
            affected_rows=rows,
        )

    def plan_auto(self) -> CleanupPlan:
        """按当前自动策略生成计划。

        Returns:
            标记为 ``automatic=True`` 的 :class:`CleanupPlan`。
        """
        return self.plan_before(self._keep_days, automatic=True)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def execute(
        self, plan: CleanupPlan, *, require_confirmation: bool = True
    ) -> CleanupResult:
        """执行清理：二次确认 → 删除 → VACUUM → 通知。

        Args:
            plan: 由 ``plan_*`` 生成的计划。
            require_confirmation: 是否需要二次确认；自动清理应传 False。

        Returns:
            :class:`CleanupResult`；任何拒绝/失败都不会抛异常到调用方。
        """
        if require_confirmation:
            if self._confirm is None:
                # 安全默认：没有确认入口就绝不删除，避免被误调用造成不可逆损失。
                logger.warning("未配置二次确认回调，已拒绝清理")
                return CleanupResult(
                    plan, 0, False, cancelled=True,
                    error="未配置二次确认回调，已拒绝清理",
                )
            try:
                approved = bool(self._confirm(plan))
            except Exception:  # noqa: BLE001 - 确认框异常按「取消」处理
                logger.exception("二次确认回调异常，按取消处理")
                approved = False
            if not approved:
                logger.info("用户取消清理：%s", plan.scope)
                return CleanupResult(plan, 0, False, cancelled=True)

        try:
            if plan.mode == _MODE_BEFORE:
                deleted = self._repo.delete_before(plan.cutoff_date)
            elif plan.mode == _MODE_RANGE:
                deleted = self._repo.delete_range(plan.start_date, plan.end_date)
            elif plan.mode == _MODE_ALL:
                deleted = self._repo.delete_all()
            else:
                raise ValueError("未知清理模式: %s" % plan.mode)
        except Exception as exc:  # noqa: BLE001 - UI 层只需展示结果文本
            logger.exception("清理数据失败")
            return CleanupResult(plan, 0, False, error=str(exc))

        # VACUUM 用独立 autocommit 连接；失败不影响已提交的删除结果。
        vacuumed = False
        try:
            self._repo.vacuum()
            vacuumed = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("VACUUM 失败（删除已完成）: %s", exc)

        logger.info(
            "清理完成: %s | 删除 %d 行 | VACUUM=%s | 自动=%s",
            plan.scope, deleted, vacuumed, plan.automatic,
        )
        if deleted > 0 and self._notifier is not None:
            try:
                self._notifier(
                    "目憩 · 数据清理",
                    "已清理 %d 条历史记录%s"
                    % (deleted, "，并回收磁盘空间" if vacuumed else ""),
                )
            except Exception:  # noqa: BLE001 - 通知失败不影响清理结果
                logger.exception("清理结果通知失败")

        return CleanupResult(plan, deleted, vacuumed)

    # ------------------------------------------------------------------
    # 自动策略
    # ------------------------------------------------------------------
    def maybe_run_auto_cleanup(self) -> Optional[CleanupResult]:
        """按策略检查并执行自动清理；同一自然日最多检查一次。

        调用时机由 ``core/app.py`` 控制：程序启动时 + 每次写库后，
        对应 Q6「启动时 + 每日首次写库」；无过期数据时直接返回 None。

        Returns:
            执行了清理时返回结果，否则 None。
        """
        if not self._auto_enabled:
            return None
        today = today_str()
        if self._last_auto_date == today:
            return None
        self._last_auto_date = today

        plan = self.plan_auto()
        if plan.affected_rows <= 0:
            return None
        logger.info("自动清理触发：保留最近 %d 天", self._keep_days)
        # 自动清理不弹二次确认（Q6 确认），仅日志 + 托盘提示。
        return self.execute(plan, require_confirmation=False)
