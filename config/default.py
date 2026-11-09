"""目憩（Mù Qī）默认配置、类型契约与全局路径常量。

本模块是「配置契约」的唯一来源：

* :class:`Settings` —— Python 3.8 兼容的 TypedDict，定义配置的静态类型；
* :data:`DEFAULT_SETTINGS` —— 全部配置项的默认值；
* :data:`LIMITS` 与枚举常量 —— 合法取值范围，供 ``config.manager`` 校验；
* 路径常量 —— 集中管理，保证源码运行与 PyInstaller 打包行为一致。

用户可覆盖的运行时配置由 ``config/settings.json`` 持久化，本模块不持有可变状态。
"""

import sys
from pathlib import Path
from typing import Dict, List, TypedDict

APP_NAME = "目憩"
APP_VERSION = "1.0.0"
CONFIG_VERSION = 1

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
# 坑：PyInstaller --onefile 会把代码解压到临时目录 _MEIxxxxx，此时 __file__
# 指向临时目录；若以 __file__ 为基准，配置与数据库会写进临时目录并在退出后丢失。
# 因此打包后必须以可执行文件所在目录为基准，源码运行时才用包目录的上级。
if getattr(sys, "frozen", False):
    # 打包后必须区分两类目录：
    #   BASE_DIR     = exe 所在目录，**可写**，放用户配置 / 数据库 / 日志 / 自定义图片
    #   RESOURCE_DIR = PyInstaller 解压目录(sys._MEIPASS)，**只读**，放图标与 Haar XML
    # 若把两者混用，onefile 运行时会去 exe 旁找打包资源而找不到。
    BASE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", str(BASE_DIR)))
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    RESOURCE_DIR = BASE_DIR

CONFIG_DIR = BASE_DIR / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "stats.db"  # 需求：数据库放在程序运行目录的 data/ 下
ASSETS_DIR = RESOURCE_DIR / "assets"              # 只读资源：icon.ico / Haar 级联
ICON_FILE = ASSETS_DIR / "icon.ico"
REMINDERS_DIR = BASE_DIR / "assets" / "reminders"  # 用户自定义图片，可写


class Settings(TypedDict):
    """配置结构。TypedDict 让 JSON 字典也能获得静态类型检查与 IDE 补全。"""

    version: int
    reminder_interval_minutes: int
    rest_duration_minutes: int
    idle_threshold_seconds: int
    camera_check_interval_seconds: int
    camera_enabled: bool
    reminder_text: str
    reminder_image: str
    animation_enabled: bool
    animation_type: str
    reminder_methods: List[str]
    auto_cleanup_enabled: bool
    auto_cleanup_keep_days: int


# 弹窗动态特效（文档 2.4）：fade=淡入淡出 / zoom=缩放 / blink=闪烁
ANIMATION_TYPES = ("fade", "zoom", "blink")
# 提醒方式（Q4 确认可多选）：popup=弹窗 / notification=托盘气泡 / sound=声音
REMINDER_METHODS = ("popup", "notification", "sound")

# 数值型配置的合法范围；越界时回退默认值而不是钳制，避免误输入产生反直觉行为
LIMITS: Dict[str, Dict[str, int]] = {
    "reminder_interval_minutes": {"min": 1, "max": 600},
    "rest_duration_minutes": {"min": 1, "max": 180},
    "idle_threshold_seconds": {"min": 5, "max": 3600},
    "camera_check_interval_seconds": {"min": 5, "max": 3600},
    "auto_cleanup_keep_days": {"min": 1, "max": 3650},
}

DEFAULT_SETTINGS: Settings = {
    "version": CONFIG_VERSION,
    "reminder_interval_minutes": 60,
    "rest_duration_minutes": 15,
    "idle_threshold_seconds": 60,
    "camera_check_interval_seconds": 60,
    # 文档 2.4 / 5.2 写「默认关闭」，需求确认中用户明确改为默认开启。
    # 设置界面仍保留开关与隐私说明，用户可随时关闭。
    "camera_enabled": True,
    "reminder_text": "该休息了，活动一下，放松眼睛吧！",
    "reminder_image": "",
    "animation_enabled": True,
    "animation_type": "fade",
    # 提醒方式可多选；默认仅弹窗，通知 / 声音在设置中按需勾选。
    "reminder_methods": ["popup"],
    # 自动清理默认关闭：删除不可逆，需用户在设置界面主动开启（Q6 按推荐）。
    "auto_cleanup_enabled": False,
    "auto_cleanup_keep_days": 90,
}

# 设置界面展示的隐私说明（文档 5.2）
PRIVACY_NOTICE = (
    "隐私说明：摄像头仅在本机判断「屏幕前是否有人」，使用 Haar Cascade 本地检测，"
    "不进行人脸识别；每次检测完成后立即释放摄像头，"
    "不保存、不上传任何画面，仅输出是否检测到人脸的布尔结果。"
)


def ensure_directories() -> None:
    """确保配置、数据、图标与自定义图片目录存在。

    目录在程序运行目录下按需创建；打包后若放在只读目录（如 Program Files），
    此处会抛出 OSError，由调用方记录日志并降级处理。

    Returns:
        None

    Raises:
        OSError: 运行目录不可写，无法创建所需目录。
    """
    # 只创建可写目录；ASSETS_DIR 在打包后指向只读的 _MEIPASS，不能写入。
    for directory in (CONFIG_DIR, DATA_DIR, REMINDERS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
