"""设置界面（P1-M12）：全配置编辑、隐私说明、数据清理入口与二次确认。

结构：``ttk.Notebook`` 三页 —— 计时与检测 / 提醒内容 / 数据与隐私，
底部为「保存 / 取消 / 恢复默认」。窗口是**模态但不锁定桌面**的普通 Toplevel
（与强制休息窗口不同，用户可以随时切走）。

数据清理的二次确认在本模块落地：构造时通过
``CleanupManager.set_confirm(self._confirm_cleanup)`` 注入确认框，
展示「时间范围 + 受影响行数 + 不可恢复警告」，用户点「是」才执行删除。
业务层 ``data/cleanup.py`` 仍有安全默认——没有确认入口就拒绝删除。

所有 tkinter 操作必须在 UI 线程；``MuQiApp`` 通过动作队列把托盘「设置」
点击转发到 UI 线程后调用 :meth:`SettingsWindow.show`。
"""

import logging
import tkinter as tk
from datetime import date
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, Optional

from config import default as cfg
from config.manager import SettingsManager
from data.cleanup import CleanupManager, CleanupPlan
from data.stats import StatsRepository, stats_repository
from ui.charts import StatsChartWindow

logger = logging.getLogger(__name__)

_ANIMATION_LABELS = {"淡入淡出": "fade", "缩放": "zoom", "闪烁": "blink"}
_ANIMATION_VALUES = {value: label for label, value in _ANIMATION_LABELS.items()}
_METHOD_LABELS = (("弹窗", "popup"), ("通知", "notification"), ("声音", "sound"))
_DATE_HINT = "YYYY-MM-DD"


class SettingsWindow:
    """设置窗口（懒创建：构造时不建 Toplevel，首次 ``show()`` 才创建）。"""

    def __init__(
        self,
        root: tk.Tk,
        manager: SettingsManager,
        cleanup: CleanupManager,
        *,
        stats_repo: Optional[StatsRepository] = None,
    ) -> None:
        """初始化并注入清理用的二次确认回调。

        Args:
            root: 隐藏的应用主窗口。
            manager: 配置管理器，保存时调用 ``update()`` 持久化 JSON。
            cleanup: 数据清理编排器。
            stats_repo: SQLite 仓库；None 时用单例，供统计图表读同一份数据。
        """
        self._root = root
        self._manager = manager
        self._cleanup = cleanup
        self._window: Optional[tk.Toplevel] = None
        self._status_var: Optional[tk.StringVar] = None
        self._stats = stats_repo or stats_repository
        # 图表窗口懒创建，这里只是组装，不产生 Toplevel。
        self._charts = StatsChartWindow(root, self._stats)

        # 关键：把二次确认弹窗注入业务层，否则 execute() 会安全拒绝手动清理。
        self._cleanup.set_confirm(self._confirm_cleanup)

        # 表单变量（与配置键一一对应）
        self._vars: Dict[str, Any] = {
            "reminder_interval_minutes": tk.IntVar(value=60),
            "rest_duration_minutes": tk.IntVar(value=15),
            "idle_threshold_seconds": tk.IntVar(value=60),
            "camera_check_interval_seconds": tk.IntVar(value=60),
            "camera_enabled": tk.BooleanVar(value=True),
            "reminder_text": tk.StringVar(value=""),
            "reminder_image": tk.StringVar(value=""),
            "animation_enabled": tk.BooleanVar(value=True),
            "animation_type": tk.StringVar(value="淡入淡出"),
            "auto_cleanup_enabled": tk.BooleanVar(value=False),
            "auto_cleanup_keep_days": tk.IntVar(value=90),
        }
        self._method_vars: Dict[str, tk.BooleanVar] = {
            key: tk.BooleanVar(value=False) for _, key in _METHOD_LABELS
        }
        self._range_start = tk.StringVar(value="")
        self._range_end = tk.StringVar(value="")

    @property
    def is_visible(self) -> bool:
        """设置窗口当前是否打开。"""
        return self._window is not None

    def show(self) -> None:
        """打开设置窗口；已打开时仅前置并重新载入最新配置。"""
        if self._window is None:
            self._build_window()
        else:
            self._load_from_settings()
        win = self._window
        if win is None:
            return
        win.deiconify()
        win.lift()
        win.focus_force()
        try:
            win.grab_set()  # 模态：仅相对本应用，不锁定整个桌面
        except tk.TclError:
            pass
        self._set_status("")

    def close(self) -> None:
        """关闭设置窗口与统计图表窗口（幂等）。"""
        self._charts.close()
        win = self._window
        if win is None:
            return
        try:
            win.grab_release()
        except tk.TclError:
            pass
        try:
            win.destroy()
        except tk.TclError:
            pass
        finally:
            self._window = None
            self._status_var = None

    # ------------------------------------------------------------------
    # 窗口构建
    # ------------------------------------------------------------------
    def _build_window(self) -> None:
        """创建 Toplevel 与全部控件。"""
        win = tk.Toplevel(self._root)
        self._window = win
        win.title("%s · 设置" % cfg.APP_NAME)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.configure(bg="#F5F5F7")

        win.update_idletasks()
        width, height = 580, 560
        x = (win.winfo_screenwidth() - width) // 2
        y = (win.winfo_screenheight() - height) // 2
        win.geometry("%dx%d+%d+%d" % (width, height, x, y))

        outer = ttk.Frame(win, padding=12)
        outer.pack(fill="both", expand=True)
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(0, 10))

        self._build_timing_tab(notebook)
        self._build_reminder_tab(notebook)
        self._build_data_tab(notebook)
        self._build_footer(outer)
        self._load_from_settings()

    def _build_timing_tab(self, notebook: ttk.Notebook) -> None:
        """第一页：计时与检测参数。"""
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text="计时与检测")
        rows = (
            ("用眼提醒间隔（分钟）", "reminder_interval_minutes", 1, 600),
            ("休息时长（分钟）", "rest_duration_minutes", 1, 180),
            ("键鼠空闲阈值（秒）", "idle_threshold_seconds", 5, 3600),
            ("摄像头检测间隔（秒）", "camera_check_interval_seconds", 5, 3600),
        )
        for row, (label, key, low, high) in enumerate(rows):
            ttk.Label(frame, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=7
            )
            ttk.Spinbox(
                frame, from_=low, to=high, width=10,
                textvariable=self._vars[key],
            ).grid(row=row, column=1, sticky="w")

        ttk.Checkbutton(
            frame, text="启用摄像头人脸检测",
            variable=self._vars["camera_enabled"],
        ).grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=(14, 4))
        ttk.Label(
            frame,
            text="键鼠空闲超过阈值后，按上方间隔开启摄像头抽检；\n"
                 "每次检测完立即释放摄像头，不保存任何画面。",
            foreground="#666", justify="left",
        ).grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(0, 6))

    def _build_reminder_tab(self, notebook: ttk.Notebook) -> None:
        """第二页：提醒文字、图片、动画、提醒方式。"""
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text="提醒内容")

        ttk.Label(frame, text="提醒文字").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self._vars["reminder_text"], width=42).grid(
            row=0, column=1, columnspan=2, sticky="w", pady=6
        )

        ttk.Label(frame, text="提醒图片（可选）").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self._vars["reminder_image"], width=30).grid(
            row=1, column=1, sticky="w", pady=6
        )
        ttk.Button(frame, text="浏览…", command=self._browse_image).grid(
            row=1, column=2, sticky="w", padx=6
        )

        ttk.Checkbutton(
            frame, text="启用弹窗动态特效",
            variable=self._vars["animation_enabled"],
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 6))
        ttk.Label(frame, text="特效类型").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Combobox(
            frame, state="readonly", width=14,
            textvariable=self._vars["animation_type"],
            values=list(_ANIMATION_LABELS.keys()),
        ).grid(row=3, column=1, sticky="w", pady=6)

        ttk.Label(frame, text="提醒方式（可多选）").grid(
            row=4, column=0, sticky="nw", pady=(14, 4)
        )
        methods = ttk.Frame(frame)
        methods.grid(row=4, column=1, columnspan=2, sticky="w", pady=(14, 4))
        for index, (label, key) in enumerate(_METHOD_LABELS):
            ttk.Checkbutton(
                methods, text=label, variable=self._method_vars[key]
            ).grid(row=0, column=index, sticky="w", padx=(0, 12))
        ttk.Label(
            frame, text="提示：强制休息弹窗始终显示；通知/声音为附加提醒方式。",
            foreground="#666",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_data_tab(self, notebook: ttk.Notebook) -> None:
        """第三页：隐私说明与数据清理（手动 + 自动策略）。"""
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text="数据与隐私")

        privacy = ttk.LabelFrame(frame, text="隐私说明", padding=10)
        privacy.pack(fill="x", pady=(0, 12))
        ttk.Label(
            privacy, text=cfg.PRIVACY_NOTICE, wraplength=500, justify="left",
            foreground="#444",
        ).pack(anchor="w")

        auto = ttk.LabelFrame(frame, text="自动清理策略", padding=10)
        auto.pack(fill="x", pady=(0, 12))
        ttk.Checkbutton(
            auto, text="启用自动清理（程序启动及每日首次写库时检查）",
            variable=self._vars["auto_cleanup_enabled"],
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(auto, text="仅保留最近").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(
            auto, from_=1, to=3650, width=8,
            textvariable=self._vars["auto_cleanup_keep_days"],
        ).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(auto, text="天的数据").grid(row=1, column=2, sticky="w", pady=(8, 0))

        manual = ttk.LabelFrame(frame, text="手动清理历史数据", padding=10)
        manual.pack(fill="x", pady=(0, 10))
        buttons = ttk.Frame(manual)
        buttons.pack(fill="x")
        ttk.Button(
            buttons, text="清除 30 天前",
            command=lambda: self._run_cleanup(self._cleanup.plan_before(30)),
        ).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(
            buttons, text="清除 90 天前",
            command=lambda: self._run_cleanup(self._cleanup.plan_before(90)),
        ).grid(row=0, column=1, padx=6)
        ttk.Button(
            buttons, text="清除全部",
            command=lambda: self._run_cleanup(self._cleanup.plan_all()),
        ).grid(row=0, column=2, padx=6)

        range_frame = ttk.Frame(manual)
        range_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(range_frame, text="自定义范围").grid(row=0, column=0, sticky="w")
        ttk.Entry(range_frame, textvariable=self._range_start, width=12).grid(
            row=0, column=1, padx=4
        )
        ttk.Label(range_frame, text="至").grid(row=0, column=2)
        ttk.Entry(range_frame, textvariable=self._range_end, width=12).grid(
            row=0, column=3, padx=4
        )
        ttk.Button(
            range_frame, text="清除该范围", command=self._cleanup_custom_range
        ).grid(row=0, column=4, padx=(8, 0))
        ttk.Label(
            manual, text="日期格式 %s；清理前会二次确认，清理后执行 VACUUM 回收空间。" % _DATE_HINT,
            foreground="#666",
        ).pack(anchor="w", pady=(8, 0))

        ttk.Button(
            frame, text="查看周 / 月统计图表", command=self._charts.show
        ).pack(anchor="w", pady=(2, 8))

        self._status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self._status_var, foreground="#0F6E56").pack(
            anchor="w", pady=(6, 0)
        )

    def _build_footer(self, parent: ttk.Frame) -> None:
        """底部按钮区。"""
        footer = ttk.Frame(parent)
        footer.pack(fill="x")
        ttk.Button(footer, text="恢复默认", command=self._on_reset).pack(side="left")
        ttk.Button(footer, text="取消", command=self.close).pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="保存", command=self._on_save).pack(side="right")

    # ------------------------------------------------------------------
    # 表单读写
    # ------------------------------------------------------------------
    def _load_from_settings(self) -> None:
        """把当前配置载入表单变量。"""
        settings = self._manager.snapshot()
        for key, var in self._vars.items():
            if key == "animation_type":
                var.set(_ANIMATION_VALUES.get(str(settings["animation_type"]), "淡入淡出"))
            else:
                var.set(settings[key])
        methods = set(settings["reminder_methods"])
        for key, var in self._method_vars.items():
            var.set(key in methods)

    def _collect_payload(self) -> Optional[Dict[str, Any]]:
        """收集表单值为配置字典；输入非法时提示并返回 None。

        Returns:
            待保存的配置字典，或 None（存在非法输入）。
        """
        try:
            payload: Dict[str, Any] = {
                "reminder_interval_minutes": int(self._vars["reminder_interval_minutes"].get()),
                "rest_duration_minutes": int(self._vars["rest_duration_minutes"].get()),
                "idle_threshold_seconds": int(self._vars["idle_threshold_seconds"].get()),
                "camera_check_interval_seconds": int(
                    self._vars["camera_check_interval_seconds"].get()
                ),
                "auto_cleanup_keep_days": int(self._vars["auto_cleanup_keep_days"].get()),
                "camera_enabled": bool(self._vars["camera_enabled"].get()),
                "reminder_text": str(self._vars["reminder_text"].get()),
                "reminder_image": str(self._vars["reminder_image"].get()).strip(),
                "animation_enabled": bool(self._vars["animation_enabled"].get()),
                "animation_type": _ANIMATION_LABELS.get(
                    str(self._vars["animation_type"].get()), "fade"
                ),
                "auto_cleanup_enabled": bool(self._vars["auto_cleanup_enabled"].get()),
                "reminder_methods": [
                    key for key, var in self._method_vars.items() if var.get()
                ],
            }
        except (tk.TclError, ValueError):
            messagebox.showerror(
                "%s · 设置" % cfg.APP_NAME,
                "数值项请输入有效整数，且不能为空。",
                parent=self._window,
            )
            return None
        if not payload["reminder_methods"]:
            messagebox.showwarning(
                "%s · 设置" % cfg.APP_NAME,
                "请至少选择一种提醒方式。",
                parent=self._window,
            )
            return None
        return payload

    def _on_save(self) -> None:
        """保存配置到 JSON，并触发配置变更回调（热更新到状态机/托盘等）。"""
        payload = self._collect_payload()
        if payload is None:
            return
        try:
            self._manager.update(payload)
        except Exception as exc:  # noqa: BLE001 - 磁盘异常要给可见反馈
            logger.exception("保存配置失败")
            messagebox.showerror(
                "%s · 设置" % cfg.APP_NAME,
                "保存失败：%s" % exc, parent=self._window,
            )
            return
        logger.info("配置已通过设置界面保存")
        self.close()

    def _on_reset(self) -> None:
        """恢复默认配置（需确认，避免误覆盖用户的自定义设置）。"""
        if not messagebox.askyesno(
            "%s · 设置" % cfg.APP_NAME,
            "确定恢复全部默认设置吗？当前自定义配置将被覆盖。",
            parent=self._window,
        ):
            return
        self._manager.reset()
        self._load_from_settings()
        self._set_status("已恢复默认设置，点击「保存」以生效。")

    def _browse_image(self) -> None:
        """选择本地提醒图片，写入路径输入框。"""
        path = filedialog.askopenfilename(
            title="选择提醒图片",
            initialdir=str(cfg.REMINDERS_DIR),
            parent=self._window,
            filetypes=[
                ("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self._vars["reminder_image"].set(path)

    # ------------------------------------------------------------------
    # 数据清理
    # ------------------------------------------------------------------
    def _confirm_cleanup(self, plan: CleanupPlan) -> bool:
        """二次确认弹窗（由 CleanupManager.execute 回调）。

        Args:
            plan: 清理计划，含范围描述与受影响行数。

        Returns:
            用户确认继续返回 True；取消/关闭返回 False。
        """
        message = (
            "即将执行数据清理：\n\n%s\n\n"
            "⚠️ 此操作非常危险，可能导致不可逆的数据丢失！\n"
            "删除后无法恢复，确认继续吗？" % plan.scope
        )
        return bool(
            messagebox.askyesno(
                "%s · 确认清理" % cfg.APP_NAME,
                message,
                icon="warning",
                default="no",
                parent=self._window,
            )
        )

    def _run_cleanup(self, plan: CleanupPlan) -> None:
        """执行清理并把结果显示在页面状态栏（二次确认在 execute 内部完成）。"""
        result = self._cleanup.execute(plan)
        if result.cancelled:
            self._set_status("已取消，未删除任何数据。")
        elif result.error:
            self._set_status("清理失败：%s" % result.error)
        else:
            self._set_status(
                "已删除 %d 条记录；VACUUM %s。"
                % (result.deleted_rows, "成功" if result.vacuumed else "失败")
            )

    def _cleanup_custom_range(self) -> None:
        """校验自定义日期范围后生成并执行清理计划。"""
        start = self._range_start.get().strip()
        end = self._range_end.get().strip()
        try:
            date.fromisoformat(start)
            date.fromisoformat(end)
        except ValueError:
            messagebox.showerror(
                "%s · 设置" % cfg.APP_NAME,
                "日期格式不正确，请使用 %s。" % _DATE_HINT,
                parent=self._window,
            )
            return
        self._run_cleanup(self._cleanup.plan_range(start, end))

    def _set_status(self, text: str) -> None:
        """更新页面底部的状态文本。

        Args:
            text: 状态文本。
        """
        if self._status_var is not None:
            self._status_var.set(text)
