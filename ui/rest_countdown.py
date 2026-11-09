"""休息倒计时：纯逻辑模型 + tkinter 显示组件。

职责划分
--------
* 本模块负责「还剩多少时间、怎么显示」；
* ``ui/reminder.py`` 负责「全屏锁定窗口、提醒内容、取消按钮」，并嵌入本模块。

**唯一数据源**：生产环境中剩余秒数来自状态机
``StateSnapshot.rest_remaining_seconds``，由 app.py 每秒推给 :meth:`CountdownView.render`，
界面自己不计时，避免状态机与 UI 两套时钟漂移。
:class:`RestCountdown` 另外提供独立 ``tick`` 模式，便于单元测试与未来独立窗口复用。
"""

import tkinter as tk
from dataclasses import dataclass
from typing import Optional

from core.state import format_countdown

_BG = "#1E1E28"
_FG = "#F2F2F5"
_MUTED = "#A9A9B8"
_ACCENT = "#7FD1B0"
_BAR_WIDTH = 360
_BAR_HEIGHT = 8


@dataclass(frozen=True)
class CountdownState:
    """一次倒计时状态快照。

    Attributes:
        remaining_seconds: 剩余秒数（不小于 0）。
        total_seconds: 本轮休息总秒数。
        finished: 是否已结束。
        formatted: ``MM:SS`` 文本。
        progress: 已完成比例，0.0 ~ 1.0。
    """

    remaining_seconds: int
    total_seconds: int
    finished: bool
    formatted: str
    progress: float


class RestCountdown:
    """倒计时模型（纯 Python，不依赖 tkinter，可直接单测）。"""

    def __init__(self, total_seconds: int = 0) -> None:
        """初始化。

        Args:
            total_seconds: 本轮休息总秒数；0 表示尚未开始。
        """
        self._total = max(0, int(total_seconds))
        self._remaining = self._total

    @property
    def state(self) -> CountdownState:
        """返回当前状态快照。"""
        return self._build_state(self._remaining)

    def reset(self, total_seconds: Optional[int] = None) -> CountdownState:
        """开始新一轮倒计时。

        Args:
            total_seconds: 新的总秒数；None 表示沿用当前总秒数。

        Returns:
            重置后的状态快照。
        """
        if total_seconds is not None:
            self._total = max(0, int(total_seconds))
        self._remaining = self._total
        return self.state

    def tick(self, seconds: int = 1) -> CountdownState:
        """独立计时模式下推进 ``seconds`` 秒（生产环境一般不用）。

        Args:
            seconds: 推进秒数。

        Returns:
            推进后的状态快照。
        """
        self._remaining = max(0, self._remaining - max(0, int(seconds)))
        return self.state

    def sync_remaining(self, remaining: int) -> CountdownState:
        """用状态机的读数同步本模型（生产环境使用）。

        Args:
            remaining: 状态机给出的剩余秒数。

        Returns:
            同步后的状态快照。
        """
        self._remaining = max(0, int(remaining))
        return self.state

    def finish(self) -> CountdownState:
        """提前结束倒计时（用户点「取消休息」）。

        Returns:
            已结束的状态快照。
        """
        self._remaining = 0
        return self.state

    def _build_state(self, remaining: int) -> CountdownState:
        """构造状态快照。

        Args:
            remaining: 剩余秒数。

        Returns:
            :class:`CountdownState`。
        """
        if self._total > 0:
            progress = min(1.0, max(0.0, (self._total - remaining) / self._total))
        else:
            progress = 0.0
        return CountdownState(
            remaining_seconds=remaining,
            total_seconds=self._total,
            finished=remaining <= 0,
            formatted=format_countdown(remaining),
            progress=progress,
        )


class CountdownView(tk.Frame):
    """倒计时显示组件：大号 ``MM:SS`` + 自绘进度条 + 状态提示。

    用 ``Canvas`` 自绘进度条而不是 ``ttk.Progressbar``，是为了在深色窗口里
    保持配色一致，且避免引入 ttk 主题配置。
    """

    def __init__(self, parent: tk.Misc, *, total_seconds: int = 900) -> None:
        """构建组件。

        Args:
            parent: 父容器。
            total_seconds: 本轮休息总秒数，用于初始化进度。
        """
        super().__init__(parent, bg=_BG)
        self._model = RestCountdown(total_seconds)

        self._time_label = tk.Label(
            self, text="--:--", font=("Microsoft YaHei", 56, "bold"),
            fg=_FG, bg=_BG,
        )
        self._time_label.pack(pady=(8, 0))

        self._hint_label = tk.Label(
            self, text="休息中 · 用眼计时已暂停",
            font=("Microsoft YaHei", 12), fg=_MUTED, bg=_BG,
        )
        self._hint_label.pack(pady=(0, 12))

        self._canvas = tk.Canvas(
            self, width=_BAR_WIDTH, height=_BAR_HEIGHT,
            bg=_BG, highlightthickness=0,
        )
        self._canvas.pack()
        self._track = self._canvas.create_rectangle(
            0, 0, _BAR_WIDTH, _BAR_HEIGHT, fill="#33333F", outline=""
        )
        self._bar = self._canvas.create_rectangle(
            0, 0, 0, _BAR_HEIGHT, fill=_ACCENT, outline=""
        )
        self.render(self._model.state)

    def render(self, state: CountdownState) -> None:
        """刷新显示。

        Args:
            state: 由 ``RestCountdown`` 产出的状态快照。
        """
        self._time_label.configure(text=state.formatted)
        self._canvas.coords(
            self._bar, 0, 0, int(_BAR_WIDTH * state.progress), _BAR_HEIGHT
        )
        if state.finished:
            self._hint_label.configure(text="休息结束 · 即将恢复用眼计时", fg=_ACCENT)
        else:
            self._hint_label.configure(text="休息中 · 用眼计时已暂停", fg=_MUTED)


class CanvasCountdownRenderer:
    """在 Canvas 上绘制倒计时，**背景透明**，可直接浮在全屏背景图片上。

    ``CountdownView`` 是 Frame，背景不透明，会遮住背景图；本渲染器用
    ``create_text`` / ``create_rectangle`` 绘制，只有 ``create_window`` 放置的
    按钮带自身背景，因此文字能真正「浮」在图上。
    """

    def __init__(
        self, canvas: tk.Canvas, center_x: float, center_y: float,
        *, bar_width: int = _BAR_WIDTH,
    ) -> None:
        """创建倒计时图元。

        Args:
            canvas: 目标 Canvas。
            center_x / center_y: 倒计时区域中心坐标。
            bar_width: 进度条宽度（像素）。
        """
        self._canvas = canvas
        self._time_id = canvas.create_text(
            center_x, center_y, text="--:--",
            font=("Microsoft YaHei", 64, "bold"), fill=_FG,
        )
        self._hint_id = canvas.create_text(
            center_x, center_y + 62, text="休息中 · 用眼计时已暂停",
            font=("Microsoft YaHei", 12), fill=_MUTED,
        )
        x0 = center_x - bar_width / 2
        y0 = center_y + 92
        canvas.create_rectangle(
            x0, y0, x0 + bar_width, y0 + _BAR_HEIGHT,
            fill="#33333F", outline="",
        )
        self._bar_id = canvas.create_rectangle(
            x0, y0, x0, y0 + _BAR_HEIGHT, fill=_ACCENT, outline="",
        )
        self._x0 = x0
        self._y0 = y0
        self._bar_width = bar_width

    def render(self, state: CountdownState) -> None:
        """刷新倒计时文本与进度条。

        Args:
            state: ``RestCountdown`` 产出的状态快照。
        """
        self._canvas.itemconfigure(self._time_id, text=state.formatted)
        self._canvas.coords(
            self._bar_id,
            self._x0, self._y0,
            self._x0 + self._bar_width * state.progress,
            self._y0 + _BAR_HEIGHT,
        )
        if state.finished:
            self._canvas.itemconfigure(
                self._hint_id, text="休息结束 · 即将恢复用眼计时", fill=_ACCENT,
            )
        else:
            self._canvas.itemconfigure(
                self._hint_id, text="休息中 · 用眼计时已暂停", fill=_MUTED,
            )
