"""系统托盘常驻（P0-M8）：pystray + Pillow。

职责
----
* 程序启动后**不弹主窗口**，只在系统托盘常驻；
* 右键菜单展示当前状态、今日累计用眼时长、休息剩余，并提供
  暂停/恢复检测、手动开始休息、设置、退出；
* 提供气泡通知 ``notify()``，供 P1 的「通知」提醒方式使用。

设计要点（为什么这样做）
------------------------
1. **动态菜单文本用 callable**：pystray 在每次展开菜单时调用 ``MenuItem.text``，
   因此本模块只更新内存字段即可刷新显示，无需重建菜单或调用 ``update_menu()``。
   实测 pystray 0.19.5 的 callable 签名是 ``f(item)``，必须接收一个位置参数。
2. **独立线程**：``Icon.run()`` 是阻塞的消息循环，必须放在后台线程；
   tkinter 主循环仍独占主线程。所有菜单回调通过 ``TrayCallbacks`` 交给 M9 编排，
   托盘模块本身不直接操作状态机或 UI。
3. **就绪同步**：``start()`` 等待 ``setup`` 回调置位（最多 5 秒），
   避免程序刚启动时托盘还没出现就继续执行。
4. **线程安全**：动态字段用 Lock 保护（菜单渲染线程与计时线程并发访问）。
5. **图标容错**：优先加载 ``assets/icon.ico``（Q7 允许用 Pillow 生成的占位图标），
   缺失或损坏时用 :func:`create_default_icon` 在内存绘制，保证托盘永远有图标。
"""

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

from config import default as cfg
from core.state import format_countdown, format_duration

logger = logging.getLogger(__name__)

_BG = "#1E1E28"
_ACCENT = "#7FD1B0"
_FG = "#F2F2F5"
_THREAD_JOIN_TIMEOUT = 3.0
_STARTUP_TIMEOUT = 5.0


def create_default_icon(size: int = 64) -> Image.Image:
    """用 Pillow 程序化绘制占位图标（「目」+ 休憩弧线），不依赖外部素材。

    Args:
        size: 正方形边长（像素）。

    Returns:
        RGBA 图像对象。
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=size // 5,
        fill=_BG,
    )
    # 眼睛外轮廓：横向杏仁形（用椭圆近似，简洁且在小尺寸下仍可辨识）
    line = max(2, size // 20)
    draw.ellipse(
        [size * 0.15, size * 0.30, size * 0.85, size * 0.70],
        outline=_ACCENT,
        width=line,
    )
    # 瞳孔
    r = size * 0.13
    cx = cy = size / 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_FG)
    # 下方弧线：闭目休憩的意象
    draw.arc(
        [size * 0.26, size * 0.52, size * 0.74, size * 0.94],
        start=195,
        end=345,
        fill=_ACCENT,
        width=max(2, size // 24),
    )
    return img


def load_icon(path: Optional[str] = None) -> Image.Image:
    """加载托盘图标；文件缺失或损坏时回退到内存绘制的默认图标。

    Args:
        path: 图标文件路径；None 时使用 ``cfg.ICON_FILE``。

    Returns:
        可用于 ``pystray.Icon`` 的 RGBA 图像。
    """
    target = Path(path) if path else cfg.ICON_FILE
    if target.is_file():
        try:
            with Image.open(target) as src:
                return src.convert("RGBA")
        except Exception as exc:  # noqa: BLE001 - 图标损坏不应阻止托盘启动
            logger.warning("图标加载失败，改用内置图标: %s (%s)", target, exc)
    return create_default_icon()


def export_default_icon(path: Optional[str] = None) -> Path:
    """把默认图标导出为多尺寸 .ico，供托盘与 P3 打包使用。

    Args:
        path: 输出路径；None 时使用 ``cfg.ICON_FILE``（``assets/icon.ico``）。

    Returns:
        实际写入的文件路径。

    Raises:
        OSError: 目标目录不可写。
    """
    target = Path(path) if path else cfg.ICON_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    icon = create_default_icon(64)
    icon.save(
        str(target),
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
    )
    return target


@dataclass(frozen=True)
class TrayCallbacks:
    """托盘菜单动作的回调集合，由 M9 的应用编排层注入。

    Attributes:
        on_toggle_pause: 收到 ``paused``（True=暂停 / False=恢复）。
        on_start_rest: 用户点「手动开始休息」。
        on_open_settings: 用户点「设置」（P1 的 M12 注入后才可用）。
        on_quit: 用户点「退出」。
    """

    on_toggle_pause: Optional[Callable[[bool], None]] = None
    on_start_rest: Optional[Callable[[], None]] = None
    on_open_settings: Optional[Callable[[], None]] = None
    on_quit: Optional[Callable[[], None]] = None


class TrayController:
    """系统托盘控制器：后台线程运行 pystray 消息循环。

    典型用法（M9）::

        tray = TrayController(TrayCallbacks(on_quit=app.shutdown, ...))
        tray.start()
        tray.update_status("用眼中", 3780, paused=False)
        tray.notify("目憩", "该休息了")
        tray.stop()
    """

    def __init__(
        self,
        callbacks: Optional[TrayCallbacks] = None,
        *,
        icon_image: Optional[Image.Image] = None,
    ) -> None:
        """初始化（此时不创建托盘图标、不启动线程）。

        Args:
            callbacks: 菜单动作回调；None 时相关菜单项会置灰。
            icon_image: 自定义图标；None 时调用 :func:`load_icon`。
        """
        self._callbacks = callbacks or TrayCallbacks()
        self._icon_image = icon_image or load_icon()
        self._lock = threading.Lock()
        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._running = False

        # 动态菜单文本的数据源
        self._state_label = "空闲"
        self._today_eye_seconds = 0
        self._paused = False
        self._rest_remaining = 0

    @property
    def is_running(self) -> bool:
        """托盘消息循环是否正在运行。"""
        with self._lock:
            return self._running

    def start(self) -> None:
        """在后台线程启动托盘（幂等），最多等待 5 秒确认图标就绪。"""
        with self._lock:
            if self._running:
                return
            self._icon = pystray.Icon(
                cfg.APP_NAME, self._icon_image, menu=self._build_menu()
            )
            self._running = True

        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="muqi-tray", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=_STARTUP_TIMEOUT):
            logger.warning("托盘图标未在 %.0f 秒内就绪", _STARTUP_TIMEOUT)

    def stop(self) -> None:
        """停止托盘消息循环并回收线程（幂等）。"""
        icon = self._icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001 - 托盘后端异常不应阻止退出
                logger.exception("停止托盘时发生异常")
        thread = self._thread
        # 菜单回调本身运行在托盘线程上（例如「退出」），此时不能 join 自己，
        # 否则会抛 RuntimeError: cannot join current thread。
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=_THREAD_JOIN_TIMEOUT)
        with self._lock:
            self._running = False
            self._icon = None
        self._ready.clear()

    def update_status(
        self,
        state_label: str,
        today_eye_seconds: int,
        *,
        paused: bool = False,
        rest_remaining: int = 0,
    ) -> None:
        """刷新菜单显示的状态数据（可从任意线程调用）。

        Args:
            state_label: 中文状态名，取自 ``StateSnapshot.state_label``。
            today_eye_seconds: 今日累计用眼秒数。
            paused: 是否已手动暂停检测（决定菜单显示「暂停」还是「恢复」）。
            rest_remaining: 强制休息剩余秒数；>0 时显示「休息剩余」菜单项。
        """
        with self._lock:
            self._state_label = state_label
            self._today_eye_seconds = max(0, int(today_eye_seconds))
            self._paused = bool(paused)
            self._rest_remaining = max(0, int(rest_remaining))

    def notify(self, title: str, message: str) -> None:
        """弹出托盘气泡通知（供「通知」提醒方式使用）。

        注意 pystray 的签名是 ``notify(message, title=None)``，参数顺序与
        常见习惯相反，这里按 ``(title, message)`` 封装以免调用方踩坑。

        Args:
            title: 通知标题。
            message: 通知正文。
        """
        with self._lock:
            icon = self._icon
            running = self._running
        if icon is None or not running:
            logger.warning("托盘未运行，跳过通知: %s", title)
            return
        try:
            icon.notify(message, title)
        except Exception:  # noqa: BLE001 - 通知失败不影响主流程
            logger.exception("托盘通知失败")

    # ------------------------------------------------------------------
    # 菜单
    # ------------------------------------------------------------------
    def _build_menu(self) -> Menu:
        """构造右键菜单；所有变化项都用 callable text / visible / enabled。"""
        return Menu(
            MenuItem(self._status_text, lambda *_: None, enabled=False),
            MenuItem(self._today_text, lambda *_: None, enabled=False),
            MenuItem(
                self._rest_text, lambda *_: None,
                visible=self._rest_visible, enabled=False,
            ),
            Menu.SEPARATOR,
            MenuItem(
                self._pause_text, self._on_toggle_pause,
                enabled=self._has_pause_callback,
            ),
            MenuItem(
                "手动开始休息", self._on_start_rest,
                enabled=self._has_rest_callback,
            ),
            MenuItem(
                "设置", self._on_open_settings,
                enabled=self._has_settings_callback,
            ),
            Menu.SEPARATOR,
            MenuItem("退出", self._on_quit),
        )

    # callable 的签名必须是 f(item)：pystray 会把 MenuItem 自身传进来。
    def _status_text(self, _item: Any) -> str:
        with self._lock:
            return "状态：%s" % self._state_label

    def _today_text(self, _item: Any) -> str:
        with self._lock:
            return "今日累计：%s" % format_duration(self._today_eye_seconds)

    def _rest_text(self, _item: Any) -> str:
        with self._lock:
            return "休息剩余：%s" % format_countdown(self._rest_remaining)

    def _rest_visible(self, _item: Any) -> bool:
        with self._lock:
            return self._rest_remaining > 0

    def _pause_text(self, _item: Any) -> str:
        with self._lock:
            return "恢复检测" if self._paused else "暂停检测"

    def _has_pause_callback(self, _item: Any) -> bool:
        return self._callbacks.on_toggle_pause is not None

    def _has_rest_callback(self, _item: Any) -> bool:
        return self._callbacks.on_start_rest is not None

    def _has_settings_callback(self, _item: Any) -> bool:
        return self._callbacks.on_open_settings is not None

    # ------------------------------------------------------------------
    # 菜单动作与线程
    # ------------------------------------------------------------------
    def _on_toggle_pause(self, *_args: Any) -> None:
        """切换暂停/恢复；内部先翻转状态再回调，保证菜单文本立即更新。"""
        with self._lock:
            self._paused = not self._paused
            paused = self._paused
        callback = self._callbacks.on_toggle_pause
        if callback is not None:
            self._safe_call(callback, paused)
        else:
            logger.warning("未注入暂停回调，菜单项应为置灰状态")

    def _on_start_rest(self, *_args: Any) -> None:
        callback = self._callbacks.on_start_rest
        if callback is not None:
            self._safe_call(callback)

    def _on_open_settings(self, *_args: Any) -> None:
        callback = self._callbacks.on_open_settings
        if callback is not None:
            self._safe_call(callback)

    def _on_quit(self, *_args: Any) -> None:
        """先通知业务层做清理（落盘等），再停止托盘自身。"""
        callback = self._callbacks.on_quit
        if callback is not None:
            self._safe_call(callback)
        self.stop()

    @staticmethod
    def _safe_call(callback: Callable[..., None], *args: Any) -> None:
        """隔离业务回调异常，避免托盘消息循环被单个回调打断。"""
        try:
            callback(*args)
        except Exception:  # noqa: BLE001
            logger.exception("托盘菜单回调执行失败")

    def _run(self) -> None:
        """后台线程入口：运行 pystray 消息循环。"""
        icon = self._icon
        if icon is None:
            return

        def setup(_icon: pystray.Icon) -> None:
            # setup 在图标创建完成后由 pystray 调用，此时置位就绪事件。
            _icon.visible = True
            self._ready.set()

        try:
            icon.run(setup=setup)
        except Exception:  # noqa: BLE001 - 后台线程必须自行记录异常
            logger.exception("托盘消息循环异常退出")
            self._ready.set()
        finally:
            with self._lock:
                self._running = False
