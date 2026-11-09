"""强制休息提醒窗口（P0-M6）。

按用户 2026-11-06 确认的新交互模型：达到提醒阈值 → **立即弹出本窗口** →
桌面锁定 + 休息倒计时自动开始；只有点「取消休息」或倒计时结束才关闭并恢复用眼计时。
窗口内整合了自定义文字 / 图片 / 动态特效（原计划 P1 的 M14 内容，
因为同属一个窗口、拆开会导致重写，故合并到本模块一起交付）。

桌面锁定采用「普通程序方案」（用户 15:21 选择）：
全屏无边框 + 置顶 + ``grab_set_global()`` 把普通键鼠输入定向到本窗口，
**不安装 WH_KEYBOARD_LL 全局钩子**。系统级快捷键（Win / Alt+Tab / Ctrl+Alt+Del）
由 Windows 处理，普通应用无法拦截，这一点已向用户说明。

安全红线：``grab_set_global()`` 必须配对释放，否则桌面输入会被永久劫持。
因此 :meth:`ReminderWindow.close` 用 try/finally 保证释放 grab、销毁窗口、
取消所有 ``after`` 回调，且所有关闭路径都是幂等的。

线程约束：所有 tkinter 操作必须在主线程。公共方法经 ``_dispatch`` 检测调用线程，
非主线程一律用 ``root.after(0, ...)`` 转发到 UI 线程（供 M9 后台计时线程安全调用）。

全屏背景模式（2026-11-09 用户确认）：配置自定义图片时，用 PIL ``ImageOps.fit``
把图片**裁剪铺满整个屏幕**并叠一层深色半透明遮罩保证文字可读；
窗口内容全部绘制在 Canvas 上（``create_text`` 的文字背景天然透明），
只有「取消休息」按钮用 ``create_window`` 放置，因此文字与倒计时真正浮在图片上。
普通 Frame / Label 背景不透明会盖住图片，故不再使用。未配置图片时回退纯深色背景。
"""

import ctypes
import logging
import platform
import queue
import threading
import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config.default import Settings
from ui.rest_countdown import CanvasCountdownRenderer, RestCountdown

logger = logging.getLogger(__name__)

_BG = "#1E1E28"
_FG = "#F2F2F5"
_MUTED = "#A9A9B8"
_ACCENT = "#7FD1B0"
_BTN_BG = "#2E4A44"
_FINAL_ALPHA = 1.0  # 完全不透明：桌面内容被彻底遮住，符合「锁定」语义
_ANIMATION_STEP_MS = 30
_FADE_STEP = 0.08
_ZOOM_STEP = 0.035
_MAX_BLINK = 6


class ReminderWindow:
    """强制休息窗口：提醒内容 + 桌面锁定 + 倒计时 + 「取消休息」按钮。

    典型用法（由 app.py 在 UI 线程编排）::

        win = ReminderWindow(root, settings, on_cancel=app.on_rest_cancelled)
        win.show(rest_seconds)          # 进入强制休息
        win.update_remaining(600)       # 每秒用状态机快照刷新
        win.close()                     # 取消或倒计时结束
    """

    def __init__(
        self,
        root: tk.Tk,
        settings: Settings,
        on_cancel: Optional[Callable[[], None]] = None,
        *,
        lock_desktop: bool = True,
    ) -> None:
        """初始化（此时不创建窗口、不锁定桌面）。

        Args:
            root: 隐藏的应用主窗口，所有 Toplevel 挂在其下。
            settings: 当前配置（提醒文字、图片、动画开关与类型）。
            on_cancel: 用户点击「取消休息」后的回调；窗口会**先解锁再回调**，
                避免回调中的耗时操作让桌面继续被锁。
            lock_desktop: 是否启用全屏锁定；False 为预览/单元测试模式，
                只构建控件、不显示窗口、不建立 grab，避免测试时锁住用户桌面。
        """
        self._root = root
        self._settings: Dict[str, Any] = dict(settings)
        self._on_cancel = on_cancel
        self._lock_desktop = lock_desktop
        self._ui_thread = threading.current_thread()

        self._window: Optional[tk.Toplevel] = None
        self._canvas: Optional[tk.Canvas] = None
        self._title_id: Optional[int] = None
        self._bg_image_id: Optional[int] = None
        self._renderer: Optional[CanvasCountdownRenderer] = None
        self._countdown = RestCountdown(0)
        self._photo: Optional[Any] = None
        self._after_ids: List[str] = []
        self._visible = False

        # 跨线程调用不直接碰 tkinter：后台线程把任务入队，
        # 由 UI 线程的 50ms 轮询泵消费。tkinter 并非线程安全，
        # 依赖 root.after 从其他线程调用在部分平台并不可靠。
        self._tasks: "queue.Queue" = queue.Queue()
        self._pump_running = False
        self._start_pump()

    @property
    def is_visible(self) -> bool:
        """窗口当前是否处于显示/锁定状态。"""
        return self._visible

    def update_settings(self, settings: Settings) -> None:
        """热更新配置（下次 ``show`` 生效，供配置变更回调调用）。

        Args:
            settings: 新的完整配置。
        """
        self._settings = dict(settings)

    def show(self, rest_seconds: int) -> None:
        """显示强制休息窗口并启动倒计时（可从任意线程调用）。

        Args:
            rest_seconds: 本轮休息总秒数。
        """
        self._dispatch(self._show_impl, rest_seconds)

    def update_remaining(self, seconds: int) -> None:
        """用状态机的剩余秒数刷新界面；归零时自动关闭（可从任意线程调用）。

        Args:
            seconds: ``StateSnapshot.rest_remaining_seconds``。
        """
        self._dispatch(self._update_remaining_impl, seconds)

    def close(self) -> None:
        """关闭窗口并释放桌面输入捕获（幂等，可从任意线程调用）。"""
        self._dispatch(self._close_impl)

    # ------------------------------------------------------------------
    # 窗口构建
    # ------------------------------------------------------------------
    def _show_impl(self, rest_seconds: int) -> None:
        """构建并显示窗口（仅 UI 线程执行）。

        全屏背景图与所有文字/倒计时都画在同一个 Canvas 上：
        ``create_text`` 的文字没有背景矩形，才能浮在图片上；
        只有「取消休息」按钮通过 ``create_window`` 放置。
        """
        if self._visible and self._window is not None:
            self._countdown.reset(rest_seconds)
            if self._renderer is not None:
                self._renderer.render(self._countdown.state)
            return

        win = tk.Toplevel(self._root)
        self._window = win
        win.configure(bg=_BG)
        win.overrideredirect(True)          # 无标题栏，用户无法点系统关闭按钮
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0 if self._lock_desktop else _FINAL_ALPHA)
        win.protocol("WM_DELETE_WINDOW", lambda: None)  # 忽略关闭请求
        win.bind("<Escape>", lambda _e: "break")

        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        canvas = tk.Canvas(win, bg=_BG, highlightthickness=0)
        self._canvas = canvas
        canvas.pack(fill="both", expand=True)

        # 背景图铺满整屏；无图时用 Canvas 自身的深色背景。
        photo = self._build_background_photo(screen_w, screen_h)
        if photo is not None:
            self._bg_image_id = canvas.create_image(
                0, 0, image=photo, anchor="nw"
            )

        center_x = screen_w / 2
        self._title_id = canvas.create_text(
            center_x, screen_h * 0.16, text="目憩 · 该休息了",
            font=("Microsoft YaHei", 28, "bold"), fill=_FG,
        )
        text = str(
            self._settings.get("reminder_text")
            or "该休息了，活动一下，放松眼睛吧！"
        )
        canvas.create_text(
            center_x, screen_h * 0.27, text=text,
            width=int(screen_w * 0.72), justify="center",
            font=("Microsoft YaHei", 15), fill=_FG,
        )

        self._countdown = RestCountdown(rest_seconds)
        self._renderer = CanvasCountdownRenderer(
            canvas, center_x, screen_h * 0.48
        )

        button = tk.Button(
            canvas, text="取消休息", command=self._handle_cancel,
            font=("Microsoft YaHei", 13), fg=_FG, bg=_BTN_BG,
            activebackground=_ACCENT, activeforeground=_BG,
            relief="flat", padx=36, pady=8, cursor="hand2",
        )
        canvas.create_window(center_x, screen_h * 0.73, window=button)

        canvas.create_text(
            center_x, screen_h * 0.85,
            text="倒计时结束后自动恢复 · 休息期间不累计用眼时长",
            font=("Microsoft YaHei", 10), fill=_MUTED,
        )

        self._visible = True
        if self._lock_desktop:
            self._activate_lock(win)
        else:
            # 预览/测试模式：不显示、不置顶抢焦点、不建立 grab。
            win.withdraw()

    def _activate_lock(self, win: tk.Toplevel) -> None:
        """全屏覆盖主屏并建立全局输入捕获。

        **两重保险**：单独用 ``geometry`` 在 ``overrideredirect`` 窗口上会被内容
        布局重新调整成一小片（Windows DPI 缩放时尤其明显），因此先用 Tk geometry
        声明尺寸，再用 Win32 ``SetWindowPos`` 按物理屏幕尺寸强制铺满。
        ``-fullscreen`` 对 override-redirect 窗口会被 Tk 拒绝，不能使用。
        多显示器环境仅覆盖主屏。

        Args:
            win: 已构建好的 Toplevel。
        """
        width = win.winfo_screenwidth()
        height = win.winfo_screenheight()
        win.geometry("%dx%d+0+0" % (width, height))
        # 实测 Tk 对 override-redirect 窗口设置 -fullscreen 会抛
        # "can't set fullscreen attribute: override-redirect flag is set"，
        # 故不调用；全屏由 geometry + 下面的 Win32 SetWindowPos 保证。
        win.deiconify()
        win.lift()
        win.focus_force()
        win.update_idletasks()  # 让 geometry 先生效，再用 Win32 覆盖
        self._force_fullscreen_win32(win)
        try:
            # 把普通键鼠输入全部定向到本窗口；Win/Alt+Tab/Ctrl+Alt+Del 属系统级，
            # 普通应用无法拦截（用户已确认采用此「普通程序方案」）。
            win.grab_set_global()
        except tk.TclError as exc:
            # 已有其他 grab 时可能失败：仍保持置顶遮挡，但不让程序崩溃。
            logger.warning("无法建立全局输入捕获: %s", exc)
        self._start_animation()

    @staticmethod
    def _force_fullscreen_win32(win: tk.Toplevel) -> None:
        """用 Win32 ``SetWindowPos`` 把窗口强制铺满主屏（绕开 Tk 布局/DPI 差异）。

        ``GetSystemMetrics(SM_CXSCREEN/SM_CYSCREEN)`` 与 ``SetWindowPos`` 使用同一套
        坐标（非 DPI-aware 进程里都是逻辑像素，由 Windows 自动缩放），因此天然自洽。
        不传 ``SWP_SHOWWINDOW``，显示仍由 ``deiconify()`` 负责。

        Args:
            win: 需要铺满屏幕的 Toplevel。
        """
        if platform.system() != "Windows":
            return
        try:
            user32 = ctypes.windll.user32
            user32.GetSystemMetrics.argtypes = [ctypes.c_int]
            user32.GetSystemMetrics.restype = ctypes.c_int
            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint,
            ]
            user32.SetWindowPos.restype = ctypes.c_int

            screen_w = int(user32.GetSystemMetrics(0))  # SM_CXSCREEN
            screen_h = int(user32.GetSystemMetrics(1))  # SM_CYSCREEN
            if screen_w <= 0 or screen_h <= 0:
                return
            # 关键坑：winfo_id() 返回 Tk 内层 client HWND，改它的尺寸不会改变
            # 顶层窗口；必须用 wm frame（Windows 上返回 "0x..." 形式的顶层 HWND）。
            try:
                raw_frame = win.frame()
                hwnd = (
                    int(raw_frame, 16)
                    if isinstance(raw_frame, str)
                    else int(raw_frame)
                )
            except Exception:  # noqa: BLE001 - 老版本 Tk 回退到 client HWND
                raw_id = win.winfo_id()
                hwnd = int(raw_id) if isinstance(raw_id, int) else int(str(raw_id), 16)

            # HWND_TOPMOST = -1；SWP_FRAMECHANGED(0x0020) 促使窗口重算尺寸与布局。
            result = user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                ctypes.c_void_p(-1),
                0, 0, screen_w, screen_h,
                0x0020,
            )
            if not result:
                logger.warning("SetWindowPos 返回失败，窗口可能未铺满屏幕")
            logger.debug(
                "Win32 强制全屏: %dx%d, hwnd=%s, ret=%s",
                screen_w, screen_h, hwnd, result,
            )
        except Exception as exc:  # noqa: BLE001 - 失败时仍有前两重保险
            logger.warning("Win32 全屏强制失败（继续依赖 Tk 设置）: %s", exc)

    def _build_background_photo(
        self, screen_w: int, screen_h: int
    ) -> Optional[Any]:
        """把自定义图片裁剪铺满整屏并暗化，返回 PhotoImage。

        ``ImageOps.fit`` 居中裁剪到屏幕尺寸（保持比例、不拉伸变形），
        再叠一层深色半透明遮罩，保证白色文字在任意图片上都清晰可读。

        Args:
            screen_w: 屏幕宽度（像素）。
            screen_h: 屏幕高度（像素）。

        Returns:
            可放到 Canvas 上的 PhotoImage；未配置/文件不存在/加载失败返回 None，
            此时窗口回退为纯深色背景，不影响提醒功能。
        """
        raw_path = str(self._settings.get("reminder_image") or "").strip()
        if not raw_path:
            return None
        path = Path(raw_path)
        if not path.is_file():
            logger.warning("提醒图片不存在，已使用纯色背景: %s", path)
            return None
        try:
            # 延迟导入，避免无 Pillow 环境 import 本模块失败。
            from PIL import Image, ImageOps, ImageTk

            with Image.open(path) as src:
                base = ImageOps.fit(
                    src.convert("RGB"), (screen_w, screen_h),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                ).convert("RGBA")
            # alpha=150/255 ≈ 59% 暗化：图片仍清晰可见，文字对比度也够。
            overlay = Image.new("RGBA", (screen_w, screen_h), (18, 18, 26, 150))
            merged = Image.alpha_composite(base, overlay)
            photo = ImageTk.PhotoImage(merged)
            # 经典 tkinter 坑：PhotoImage 必须保持强引用，否则被 GC 后图片空白。
            self._photo = photo
            return photo
        except Exception as exc:  # noqa: BLE001 - 图片损坏不应影响提醒窗口
            logger.warning("提醒图片加载失败，已使用纯色背景: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 动画（tkinter after()，温和、不打扰）
    # ------------------------------------------------------------------
    def _start_animation(self) -> None:
        """按配置启动淡入淡出 / 缩放 / 闪烁动画。"""
        win = self._window
        if win is None:
            return
        if not bool(self._settings.get("animation_enabled", True)):
            win.attributes("-alpha", _FINAL_ALPHA)
            return
        kind = str(self._settings.get("animation_type", "fade"))
        if kind == "zoom":
            self._zoom_step(0.86)
        elif kind == "blink":
            self._blink_step(0)
        else:
            self._fade_step(0.0)

    def _schedule(self, delay_ms: int, fn: Callable[[], None]) -> None:
        """注册一个可取消的 after 回调。

        Args:
            delay_ms: 延迟毫秒数。
            fn: 无参回调。
        """
        win = self._window
        if win is None:
            return
        try:
            self._after_ids.append(win.after(delay_ms, fn))
        except tk.TclError:
            pass

    def _fade_step(self, alpha: float) -> None:
        """淡入的一步。"""
        win = self._window
        if not self._visible or win is None:
            return
        alpha = min(_FINAL_ALPHA, alpha + _FADE_STEP)
        try:
            win.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if alpha < _FINAL_ALPHA:
            self._schedule(_ANIMATION_STEP_MS, lambda: self._fade_step(alpha))

    def _zoom_step(self, scale: float) -> None:
        """标题字号由小放大的一步（Canvas 版，配合全屏背景）。"""
        if not self._visible or self._canvas is None or self._window is None:
            return
        scale = min(1.0, scale + _ZOOM_STEP)
        try:
            size = max(12, int(28 * scale))
            self._canvas.itemconfigure(
                self._title_id, font=("Microsoft YaHei", size, "bold")
            )
            self._window.attributes("-alpha", _FINAL_ALPHA)
        except tk.TclError:
            return
        if scale < 1.0:
            self._schedule(_ANIMATION_STEP_MS, lambda: self._zoom_step(scale))
        else:
            self._canvas.itemconfigure(
                self._title_id, font=("Microsoft YaHei", 28, "bold")
            )

    def _blink_step(self, count: int) -> None:
        """标题颜色交替闪烁（Canvas 版），结束后恢复常态色。"""
        if not self._visible or self._canvas is None or self._window is None:
            return
        try:
            self._canvas.itemconfigure(
                self._title_id, fill=_ACCENT if count % 2 == 0 else _FG
            )
            self._window.attributes("-alpha", _FINAL_ALPHA)
        except tk.TclError:
            return
        if count < _MAX_BLINK:
            self._schedule(400, lambda: self._blink_step(count + 1))
        else:
            self._canvas.itemconfigure(self._title_id, fill=_FG)

    # ------------------------------------------------------------------
    # 刷新与关闭
    # ------------------------------------------------------------------
    def _update_remaining_impl(self, seconds: int) -> None:
        """仅 UI 线程执行：刷新倒计时，归零时自动解锁关闭。"""
        if not self._visible or self._renderer is None:
            return
        state = self._countdown.sync_remaining(seconds)
        self._renderer.render(state)
        if state.finished:
            self._close_impl()

    def _handle_cancel(self) -> None:
        """「取消休息」按钮回调：先解锁桌面，再通知状态机。"""
        callback = self._on_cancel
        self._close_impl()
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - 回调来自业务层，需隔离故障
                logger.exception("取消休息回调执行失败")

    def _close_impl(self) -> None:
        """仅 UI 线程执行：释放 grab、取消动画、销毁窗口（幂等）。"""
        self._visible = False
        win = self._window
        if win is not None:
            for after_id in self._after_ids:
                try:
                    win.after_cancel(after_id)
                except (tk.TclError, ValueError, AttributeError):
                    pass
        self._after_ids.clear()

        if win is not None:
            try:
                try:
                    win.grab_release()  # 必须早于 destroy，否则桌面输入可能被劫持
                except tk.TclError:
                    pass
                win.destroy()
            except tk.TclError as exc:
                logger.warning("关闭提醒窗口时发生异常: %s", exc)
            finally:
                self._window = None
                self._canvas = None
                self._title_id = None
                self._bg_image_id = None
                self._renderer = None
                self._photo = None  # 解除 PhotoImage 引用，图片内存可回收

    def _dispatch(self, fn: Callable[..., None], *args: Any) -> None:
        """保证 UI 操作在 tkinter 主线程执行。

        Args:
            fn: 待执行的实现方法。
            *args: 传给 ``fn`` 的位置参数。
        """
        if threading.current_thread() is self._ui_thread:
            fn(*args)
        else:
            self._tasks.put((fn, args))

    def _start_pump(self) -> None:
        """启动 UI 线程的任务泵（幂等）。"""
        if self._pump_running:
            return
        self._pump_running = True
        try:
            self._root.after(50, self._pump)
        except tk.TclError:
            self._pump_running = False
            logger.warning("无法启动 UI 任务泵：主窗口不可用")

    def _pump(self) -> None:
        """在 UI 线程消费后台线程投递的任务，并续期下一轮轮询。"""
        try:
            while True:
                fn, args = self._tasks.get_nowait()
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001 - 单个任务失败不应中断任务泵
                    logger.exception("UI 任务执行失败")
        except queue.Empty:
            pass
        except tk.TclError:
            self._pump_running = False
            return
        try:
            self._root.after(50, self._pump)
        except tk.TclError:
            # 主窗口已销毁，泵自然停止。
            self._pump_running = False
