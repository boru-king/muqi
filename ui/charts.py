"""统计图表（P2-M15）：最近 7 / 30 天的用眼时长与休息次数。

按 Q10 确认用 **tkinter Canvas 自绘**，不引入 matplotlib 等新依赖，
P3 打包时也不需要额外的 hiddenimport / 数据文件。

分层
----
* :func:`build_chart_data` 是纯逻辑：把 SQLite 记录补齐成连续的 N 天序列
  （无记录的日期补 0），并算出总量/日均，便于脱离 UI 单测。
* :class:`StatsChartWindow` 只负责窗口与 Canvas 绘制，读数据走
  ``StatsRepository.get_recent_days()``。
* 窗口是**非模态**的普通 Toplevel，用户可以一边开着设置一边看图。

入口在 ``ui/settings.py`` 第三页「数据与隐私」的「查看统计图表」按钮；
``MuQiApp`` 构造 ``SettingsWindow`` 时把实际使用的 repo 注入进来，
保证图表读的就是程序正在写的那个数据库。
"""

import logging
import tkinter as tk
from dataclasses import dataclass
from datetime import date, timedelta
from tkinter import ttk
from typing import List, Optional, Sequence

from config import default as cfg
from core.state import format_duration
from data.stats import DailyStats, StatsRepository, stats_repository

logger = logging.getLogger(__name__)

_BG = "#FFFFFF"
_GRID = "#E8E8EC"
_AXIS = "#BCBCC4"
_TEXT = "#3A3A42"
_MUTED = "#8A8A94"
_EYE_COLOR = "#4A9E82"
_REST_COLOR = "#E0A45E"
_FONT = "Microsoft YaHei"


@dataclass(frozen=True)
class ChartData:
    """图表所需的规整数据。

    Attributes:
        labels: ``MM-DD`` 日期标签，按时间升序。
        eye_minutes: 每日用眼分钟数（保留 1 位小数）。
        rest_counts: 每日休息次数。
        total_minutes: 区间累计用眼分钟数。
        avg_minutes: 日均用眼分钟数（含无记录的 0 值日）。
        total_rests: 区间累计休息次数。
    """

    labels: List[str]
    eye_minutes: List[float]
    rest_counts: List[int]
    total_minutes: float
    avg_minutes: float
    total_rests: int


def build_chart_data(
    rows: Sequence[DailyStats], days: int, *, today: Optional[date] = None
) -> ChartData:
    """把数据库记录补齐成连续 N 天的序列。

    数据库只存「有用眼记录」的日期，直接画会导致横轴日期断档、日均被高估，
    因此这里按日历逐日补 0。

    Args:
        rows: ``StatsRepository.get_recent_days()`` 返回的记录。
        days: 统计天数（>=1）。
        today: 基准日期；None 时使用系统当天（可注入以便单测）。

    Returns:
        :class:`ChartData`。
    """
    span = max(1, int(days))
    base_day = today or date.today()
    by_date = {row.date: row for row in rows}

    labels: List[str] = []
    eye_minutes: List[float] = []
    rest_counts: List[int] = []
    for offset in range(span - 1, -1, -1):
        day = (base_day - timedelta(days=offset)).isoformat()
        record = by_date.get(day)
        labels.append(day[5:])  # MM-DD，30 天图也不至于挤爆
        eye_minutes.append(round((record.eye_seconds if record else 0) / 60.0, 1))
        rest_counts.append(int(record.rest_count) if record else 0)

    total = round(sum(eye_minutes), 1)
    return ChartData(
        labels=labels,
        eye_minutes=eye_minutes,
        rest_counts=rest_counts,
        total_minutes=total,
        avg_minutes=round(total / span, 1),
        total_rests=sum(rest_counts),
    )


class StatsChartWindow:
    """周/月统计图表窗口（懒创建，构造时不建 Toplevel）。"""

    def __init__(
        self, root: tk.Tk, repo: Optional[StatsRepository] = None
    ) -> None:
        """初始化。

        Args:
            root: 隐藏的应用主窗口。
            repo: SQLite 仓库；None 时使用模块级单例。
        """
        self._root = root
        self._repo = repo or stats_repository
        self._days = 7
        self._window: Optional[tk.Toplevel] = None
        self._canvas: Optional[tk.Canvas] = None
        self._summary_var: Optional[tk.StringVar] = None

    @property
    def is_visible(self) -> bool:
        """图表窗口当前是否打开。"""
        return self._window is not None

    def show(self, days: Optional[int] = None) -> None:
        """打开/前置图表窗口并刷新数据。

        Args:
            days: 统计天数（7 或 30）；None 时沿用上次选择。
        """
        if days is not None:
            self._days = max(1, int(days))
        if self._window is None:
            self._build_window()
        win = self._window
        if win is None:
            return
        win.deiconify()
        win.lift()
        win.focus_force()
        self._refresh()

    def close(self) -> None:
        """关闭图表窗口（幂等）。"""
        win = self._window
        if win is None:
            return
        try:
            win.destroy()
        except tk.TclError:
            pass
        finally:
            self._window = None
            self._canvas = None
            self._summary_var = None

    # ------------------------------------------------------------------
    # 窗口与绘制
    # ------------------------------------------------------------------
    def _build_window(self) -> None:
        """创建 Toplevel、工具栏与 Canvas。"""
        win = tk.Toplevel(self._root)
        self._window = win
        win.title("%s · 统计图表" % cfg.APP_NAME)
        win.minsize(640, 400)
        win.protocol("WM_DELETE_WINDOW", self.close)

        outer = ttk.Frame(win, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="统计周期：").pack(side="left")
        ttk.Button(
            header, text="最近 7 天", command=lambda: self._switch(7)
        ).pack(side="left", padx=(6, 3))
        ttk.Button(
            header, text="最近 30 天", command=lambda: self._switch(30)
        ).pack(side="left", padx=3)
        ttk.Button(header, text="关闭", command=self.close).pack(side="right")

        self._summary_var = tk.StringVar(value="")
        ttk.Label(
            outer, textvariable=self._summary_var, foreground="#0F6E56"
        ).pack(anchor="w", pady=(10, 4))

        self._canvas = tk.Canvas(outer, bg=_BG, highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        # 窗口缩放时按新尺寸重绘；初次 Configure 时尺寸可能为 1，_refresh 内有兜底。
        self._canvas.bind("<Configure>", lambda _event: self._refresh())

        width, height = 820, 520
        win.update_idletasks()
        x = (win.winfo_screenwidth() - width) // 2
        y = (win.winfo_screenheight() - height) // 2
        win.geometry("%dx%d+%d+%d" % (width, height, x, y))

    def _switch(self, days: int) -> None:
        """切换统计周期。

        Args:
            days: 7 或 30。
        """
        self._days = days
        self._refresh()

    def _refresh(self) -> None:
        """重新读库并重绘 Canvas。"""
        canvas = self._canvas
        if canvas is None:
            return
        try:
            rows = self._repo.get_recent_days(self._days)
        except Exception as exc:  # noqa: BLE001 - 读库失败不应让窗口崩溃
            logger.warning("读取统计数据失败: %s", exc)
            rows = []
        data = build_chart_data(rows, self._days)

        if self._summary_var is not None:
            self._summary_var.set(
                "最近 %d 天：累计用眼 %s，日均 %s，休息 %d 次"
                % (
                    self._days,
                    format_duration(int(data.total_minutes * 60)),
                    format_duration(int(data.avg_minutes * 60)),
                    data.total_rests,
                )
            )

        canvas.delete("all")
        width = max(600, canvas.winfo_width())
        height = max(320, canvas.winfo_height())
        if not any(data.eye_minutes) and not any(data.rest_counts):
            canvas.create_text(
                width / 2, height / 2,
                text="暂无统计数据，使用一段时间后再来看看吧",
                fill=_MUTED, font=(_FONT, 12),
            )
            return

        margin_left, margin_right = 54, 18
        margin_top, margin_bottom = 18, 26
        panel_gap = 34
        panel_width = width - margin_left - margin_right
        panel_height = (height - margin_top - margin_bottom - panel_gap) // 2
        if panel_height < 60:
            return

        self._draw_panel(
            canvas, margin_left, margin_top, panel_width, panel_height,
            data.eye_minutes, data.labels, _EYE_COLOR, "每日用眼时长（分钟）",
        )
        self._draw_panel(
            canvas, margin_left,
            margin_top + panel_height + panel_gap,
            panel_width, panel_height,
            [float(value) for value in data.rest_counts],
            data.labels, _REST_COLOR, "每日休息次数（次）",
        )

    @staticmethod
    def _draw_panel(
        canvas: tk.Canvas,
        x0: float, y0: float, width: float, height: float,
        values: Sequence[float], labels: Sequence[str],
        color: str, title: str,
    ) -> None:
        """在 Canvas 的指定矩形区域画一组柱状图。

        Args:
            canvas: 目标 Canvas。
            x0 / y0: 区域左上角。
            width / height: 区域尺寸。
            values: 每日数值。
            labels: 日期标签（与 values 等长）。
            color: 柱子颜色。
            title: 分区标题。
        """
        base = y0 + height
        plot_height = height - 12
        canvas.create_text(
            x0, y0 - 2, text=title, anchor="sw",
            fill=_TEXT, font=(_FONT, 10),
        )
        canvas.create_line(x0, y0, x0, base, fill=_AXIS)
        canvas.create_line(x0, base, x0 + width, base, fill=_AXIS)

        vmax = max(values) if values else 0.0
        if vmax <= 0:
            vmax = 1.0
        for ratio in (0.5, 1.0):
            grid_y = base - plot_height * ratio
            canvas.create_line(x0, grid_y, x0 + width, grid_y, fill=_GRID)
            canvas.create_text(
                x0 - 6, grid_y, text="%g" % (vmax * ratio), anchor="e",
                fill=_MUTED, font=(_FONT, 9),
            )

        count = len(values)
        slot = width / max(1, count)
        bar_width = max(2.0, slot * 0.62)
        label_step = 1 if count <= 10 else max(1, count // 8)
        for index, value in enumerate(values):
            center_x = x0 + slot * (index + 0.5)
            bar_height = (value / vmax) * plot_height
            if bar_height > 0:
                canvas.create_rectangle(
                    center_x - bar_width / 2, base - bar_height,
                    center_x + bar_width / 2, base,
                    fill=color, outline="",
                )
                if count <= 10:
                    canvas.create_text(
                        center_x, base - bar_height - 3,
                        text="%g" % value, anchor="s",
                        fill=_TEXT, font=(_FONT, 9),
                    )
            if index % label_step == 0:
                canvas.create_text(
                    center_x, base + 8, text=labels[index], anchor="n",
                    fill=_MUTED, font=(_FONT, 9),
                )
