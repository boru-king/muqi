"""配置持久化：JSON 读写、默认值合并、合法性校验与变更通知。

设计要点（为什么这样做）：

1. **单一入口**：所有模块只通过 :class:`SettingsManager` 读配置，不直接解析文件，
   避免多处读取导致的口径不一致。
2. **容错合并**：缺字段、类型错误、越界、枚举非法、JSON 损坏时逐项回退默认值，
   保证程序永远拿到一份可用配置，用户手改坏 JSON 也不会导致启动崩溃。
3. **原子写入**：先写同目录 ``.tmp`` 再 ``os.replace``，防止写入中途崩溃/断电
   造成 ``settings.json`` 截断损坏。
4. **线程安全**：设置界面线程与计时线程会并发访问，用 ``RLock`` 保护。
5. **变更回调**：设置保存后通知状态机热更新阈值，无需重启程序（供 P0-M9 使用）。
"""

import json
import logging
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, cast

from . import default as cfg

logger = logging.getLogger(__name__)


class SettingsManager:
    """管理 ``config/settings.json`` 的加载、校验、保存与变更通知。

    典型用法::

        manager = SettingsManager()          # 或使用模块级单例 settings_manager
        settings = manager.load()
        interval = settings["reminder_interval_minutes"]
        manager.update({"idle_threshold_seconds": 90})
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        """初始化配置管理器。

        Args:
            path: 配置文件路径；为 None 时使用 ``cfg.SETTINGS_FILE``。
                  单元测试可传入临时目录路径以隔离真实配置。
        """
        self._path = Path(path) if path is not None else cfg.SETTINGS_FILE
        self._lock = threading.RLock()
        self._callbacks: List[Callable[[cfg.Settings], None]] = []
        self._settings: Dict[str, Any] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        """当前使用的配置文件路径。"""
        return self._path

    def load(self) -> cfg.Settings:
        """从磁盘加载配置，校验合并后返回快照（幂等，可重复调用）。

        文件不存在、内容损坏或缺少字段时，会用默认值补齐并回写一份完整配置，
        保证仓库内的 ``settings.json`` 始终可读、字段齐全。

        Returns:
            校验后的配置快照（深拷贝，外部修改不会影响内部状态）。

        Raises:
            OSError: 配置目录不可写且需要回写时抛出。
        """
        with self._lock:
            raw = self._read_file()
            validated = self._merge_and_validate(raw)
            self._settings = validated
            self._loaded = True
            if raw != validated:
                self._write_atomic(validated)
            return cast(cfg.Settings, deepcopy(self._settings))

    def snapshot(self) -> cfg.Settings:
        """返回当前内存配置的深拷贝快照；未加载时自动触发 :meth:`load`。

        Returns:
            当前配置快照。
        """
        with self._lock:
            if not self._loaded:
                return self.load()
            return cast(cfg.Settings, deepcopy(self._settings))

    def get(self, key: str, fallback: Any = None) -> Any:
        """读取单个配置项。

        Args:
            key: 配置键名。
            fallback: 键不存在时的返回值。

        Returns:
            配置值，或 ``fallback``。
        """
        with self._lock:
            if not self._loaded:
                self.load()
            return self._settings.get(key, fallback)

    def update(self, partial: Dict[str, Any], *, persist: bool = True) -> cfg.Settings:
        """合并并校验部分配置，持久化后通知所有变更回调。

        Args:
            partial: 需要修改的键值对；未知键会被忽略。
            persist: 是否写回磁盘；False 仅更新内存（供测试或临时覆盖）。

        Returns:
            更新后的完整配置快照。

        Raises:
            OSError: ``persist=True`` 且写入失败时抛出。
        """
        with self._lock:
            if not self._loaded:
                self.load()
            merged = dict(self._settings)
            merged.update(partial)
            validated = self._merge_and_validate(merged)
            self._settings = validated
            if persist:
                self._write_atomic(validated)
            snapshot = cast(cfg.Settings, deepcopy(validated))

        # 回调放在锁外执行：回调可能反向读取配置或做耗时操作，避免阻塞 UI 线程。
        self._notify(snapshot)
        return snapshot

    def reset(self) -> cfg.Settings:
        """恢复全部默认配置并持久化。

        Returns:
            默认配置快照。
        """
        return self.update(deepcopy(dict(cfg.DEFAULT_SETTINGS)))

    def register_change_callback(
        self, callback: Callable[[cfg.Settings], None]
    ) -> None:
        """注册配置变更回调（重复注册同一回调会被忽略）。

        Args:
            callback: 接收新配置快照的可调用对象。
        """
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _read_file(self) -> Dict[str, Any]:
        """读取并解析 JSON；任何异常都降级为空字典。

        Returns:
            解析出的字典，失败时为 ``{}``。
        """
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                return data
            logger.warning("配置根节点不是对象，已回退默认配置: %s", self._path)
        except (OSError, ValueError) as exc:
            # json.JSONDecodeError 是 ValueError 的子类
            logger.warning("读取配置失败，已回退默认配置: %s (%s)", self._path, exc)
        return {}

    def _merge_and_validate(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """以默认值为模板逐键校验用户输入，产出一份完整合法配置。

        Args:
            raw: 来自磁盘或调用方的原始字典。

        Returns:
            只包含契约内键、且每个值均合法的字典。
        """
        source = raw if isinstance(raw, dict) else {}
        result: Dict[str, Any] = {}
        for key, default_value in cfg.DEFAULT_SETTINGS.items():
            result[key] = self._validate_item(
                key, source.get(key, default_value), default_value
            )
        return result

    def _validate_item(self, key: str, value: Any, default: Any) -> Any:
        """校验单个配置项。

        注意 ``bool`` 是 ``int`` 的子类，必须先用 ``isinstance(value, bool)``
        排除，否则 ``True`` 会被当成整数 1 通过数值校验。

        Args:
            key: 配置键名。
            value: 待校验值。
            default: 该项的默认值，校验失败时回退使用。

        Returns:
            合法值或默认值。
        """
        if key == "version":
            # 当前不支持结构迁移，强制写入版本，避免手改后产生误判。
            return cfg.CONFIG_VERSION

        if isinstance(default, bool):
            return value if isinstance(value, bool) else default

        if isinstance(default, int):
            limits = cfg.LIMITS.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                return default
            if limits and not (limits["min"] <= value <= limits["max"]):
                logger.warning("配置项 %s 越界(%r)，已回退默认值 %r", key, value, default)
                return default
            return value

        if key == "animation_type":
            return value if value in cfg.ANIMATION_TYPES else default

        if key == "reminder_methods":
            if not isinstance(value, (list, tuple)):
                return list(default)
            cleaned: List[str] = []
            for item in value:
                if item in cfg.REMINDER_METHODS and item not in cleaned:
                    cleaned.append(item)
            # 至少保留一种提醒方式，否则用户将收不到任何提醒。
            return cleaned or list(default)

        if isinstance(default, str):
            if not isinstance(value, str):
                return default
            if key == "reminder_text" and not value.strip():
                return default
            # 图片路径不在此校验存在性：用户可能稍后才放入文件，
            # 由提醒弹窗在加载图片时做容错（加载失败则忽略图片）。
            return value

        return default

    def _write_atomic(self, data: Dict[str, Any]) -> None:
        """原子写入 JSON 配置。

        Args:
            data: 待写入的完整配置字典。

        Raises:
            OSError: 目录创建或文件写入失败。
        """
        cfg.ensure_directories()
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        # os.replace 在同一分区内为原子操作，Windows 下同样可用。
        os.replace(str(tmp_path), str(self._path))

    def _notify(self, snapshot: cfg.Settings) -> None:
        """逐个触发变更回调；单个回调异常不影响其余回调与主流程。

        Args:
            snapshot: 变更后的配置快照。
        """
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(snapshot)
            except Exception:  # noqa: BLE001 - 回调来自不同模块，需隔离故障
                logger.exception("配置变更回调执行失败: %r", callback)


# 应用级单例：所有模块共享同一份内存配置，避免多处独立实例状态不一致。
# 单元测试请用 SettingsManager(path=临时路径) 构造独立实例。
settings_manager = SettingsManager()
