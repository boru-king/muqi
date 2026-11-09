"""SQLite 统计持久化：每日累计用眼时长与每日休息次数。

设计要点（为什么这样做）：

1. **一张按日聚合表**：文档只要求「每日累计用眼时长 / 每日休息次数」、
   按时间范围清理与周月图表，按日聚合表用 ``WHERE date < ?`` 即可高效清理，
   无需事件明细表；同时避免高频明细写入导致数据库文件膨胀。
2. **短连接**：每次操作独立 connect / commit / close，不跨线程共享连接
   （``sqlite3`` 默认 ``check_same_thread=True``，后台线程与 UI 线程共享会报错）。
   写频率由 M9 控制在 30s / 状态切换 / 退出时，开销可忽略。
3. **不用 WAL**：WAL 会产生 ``-wal`` / ``-shm`` 附加文件，且 VACUUM 回收效果受限；
   桌面单进程写入场景用默认 DELETE 日志，配合 VACUUM 可直接收缩主文件。
4. **VACUUM 必须独立连接 + autocommit**：sqlite3 默认隐式开启事务，
   在事务内执行 VACUUM 会报 ``cannot VACUUM from within a transaction``，
   因此 ``vacuum()`` 单独用 ``isolation_level=None`` 的连接执行。
5. 删除接口在本模块提供底层能力，业务编排（二次确认、自动策略、触发时机）
   由 P1 的 ``data/cleanup.py`` 负责。
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, List, Optional

from config import default as cfg

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS daily_stats (
    date        TEXT    PRIMARY KEY,          -- YYYY-MM-DD，本地自然日，字典序即时间序
    eye_seconds INTEGER NOT NULL DEFAULT 0,  -- 当日累计用眼秒数
    rest_count  INTEGER NOT NULL DEFAULT 0,  -- 当日休息次数
    updated_at  TEXT    NOT NULL             -- 最后写入时间，便于排查
);
"""


@dataclass(frozen=True)
class DailyStats:
    """某一日的统计记录。

    Attributes:
        date: ISO 日期字符串 ``YYYY-MM-DD``。
        eye_seconds: 当日累计用眼秒数。
        rest_count: 当日休息次数。
        updated_at: 最后写入时间戳；无记录时为空串。
    """

    date: str
    eye_seconds: int = 0
    rest_count: int = 0
    updated_at: str = ""


def today_str() -> str:
    """返回本地自然日的 ISO 日期字符串。

    Returns:
        形如 ``"2026-11-06"`` 的字符串。
    """
    return date.today().isoformat()


def _now_iso() -> str:
    """返回秒精度的本地时间戳，用于 ``updated_at`` 字段。"""
    return datetime.now().isoformat(timespec="seconds")


class StatsRepository:
    """``data/stats.db`` 的数据访问层（按日聚合）。

    典型用法::

        repo = StatsRepository()          # 或模块级单例 stats_repository
        repo.initialize()                 # 程序启动时调用一次
        repo.add_eye_seconds(30)          # 每 30s 落盘一次
        repo.increment_rest_count()       # 触发强制休息时 +1
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        """初始化仓库。

        Args:
            path: 数据库文件路径；None 时使用 ``cfg.DB_FILE``（``data/stats.db``）。
                  单元测试可传入临时路径隔离真实数据。
        """
        self._path = Path(path) if path is not None else cfg.DB_FILE
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        """当前使用的数据库文件路径。"""
        return self._path

    def initialize(self) -> None:
        """创建 ``data/`` 目录与统计表（幂等，可重复调用）。

        Raises:
            sqlite3.Error: 建表失败（如目录只读、文件损坏）。
        """
        with self._lock, self._connection() as conn:
            conn.execute(_CREATE_TABLE_SQL)
        logger.info("统计数据库就绪: %s", self._path)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add_eye_seconds(self, seconds: int, *, day: Optional[str] = None) -> None:
        """累加指定日期的用眼秒数。

        用 ``INSERT OR IGNORE`` + ``UPDATE`` 两步而非 UPSERT，是为了兼容
        Python 3.8 可能自带的旧版 SQLite（UPSERT 语法需 SQLite >= 3.24）。

        Args:
            seconds: 本次要累加的秒数；<= 0 时直接返回。
            day: 目标日期 ``YYYY-MM-DD``；None 表示今天。
        """
        if seconds <= 0:
            return
        target_day = day or today_str()
        now = _now_iso()
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO daily_stats "
                "(date, eye_seconds, rest_count, updated_at) VALUES (?, 0, 0, ?)",
                (target_day, now),
            )
            conn.execute(
                "UPDATE daily_stats SET eye_seconds = eye_seconds + ?, updated_at = ? "
                "WHERE date = ?",
                (seconds, now, target_day),
            )

    def increment_rest_count(
        self, delta: int = 1, *, day: Optional[str] = None
    ) -> None:
        """累加指定日期的休息次数。

        Args:
            delta: 增加次数，默认 1；<= 0 时直接返回。
            day: 目标日期 ``YYYY-MM-DD``；None 表示今天。
        """
        if delta <= 0:
            return
        target_day = day or today_str()
        now = _now_iso()
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO daily_stats "
                "(date, eye_seconds, rest_count, updated_at) VALUES (?, 0, 0, ?)",
                (target_day, now),
            )
            conn.execute(
                "UPDATE daily_stats SET rest_count = rest_count + ?, updated_at = ? "
                "WHERE date = ?",
                (delta, now, target_day),
            )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_day(self, day: Optional[str] = None) -> DailyStats:
        """查询某日统计；无记录时返回全零记录。

        Args:
            day: 目标日期；None 表示今天。

        Returns:
            对应的 :class:`DailyStats`。
        """
        target_day = day or today_str()
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT date, eye_seconds, rest_count, updated_at "
                "FROM daily_stats WHERE date = ?",
                (target_day,),
            ).fetchone()
        return self._row_to_stats(row) if row else DailyStats(date=target_day)

    def get_range(self, start_date: str, end_date: str) -> List[DailyStats]:
        """按日期闭区间查询统计，结果按日期升序（供图表使用）。

        Args:
            start_date: 起始日期 ``YYYY-MM-DD``（含）。
            end_date: 结束日期 ``YYYY-MM-DD``（含）。

        Returns:
            区间内已有记录的列表；无记录返回空列表。
        """
        if start_date > end_date:
            return []
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT date, eye_seconds, rest_count, updated_at "
                "FROM daily_stats WHERE date BETWEEN ? AND ? ORDER BY date ASC",
                (start_date, end_date),
            ).fetchall()
        return [self._row_to_stats(row) for row in rows]

    def get_recent_days(self, days: int) -> List[DailyStats]:
        """查询最近 N 个自然日（含今天）的统计，按日期升序。

        Args:
            days: 天数，<= 0 时返回空列表。

        Returns:
            已有记录的列表。
        """
        if days <= 0:
            return []
        end = date.today()
        start = end - timedelta(days=days - 1)
        return self.get_range(start.isoformat(), end.isoformat())

    def get_all(self) -> List[DailyStats]:
        """查询全部历史记录，按日期升序。

        Returns:
            全部记录；空库返回空列表。
        """
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT date, eye_seconds, rest_count, updated_at "
                "FROM daily_stats ORDER BY date ASC"
            ).fetchall()
        return [self._row_to_stats(row) for row in rows]

    def count_rows_before(self, cutoff_date: str) -> int:
        """统计严格早于 ``cutoff_date`` 的记录行数（供清理二次确认显示）。

        Args:
            cutoff_date: ``YYYY-MM-DD``；该日期本身不会被计入。

        Returns:
            将被删除的行数。
        """
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM daily_stats WHERE date < ?",
                (cutoff_date,),
            ).fetchone()
        return int(row["n"])

    def count_rows_in_range(self, start_date: str, end_date: str) -> int:
        """统计日期闭区间内的记录行数（供清理二次确认显示）。

        Args:
            start_date: 起始日期 ``YYYY-MM-DD``（含）。
            end_date: 结束日期 ``YYYY-MM-DD``（含）。

        Returns:
            将被删除的行数。
        """
        if start_date > end_date:
            return 0
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM daily_stats WHERE date BETWEEN ? AND ?",
                (start_date, end_date),
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # 清理（业务编排在 data/cleanup.py）
    # ------------------------------------------------------------------
    def delete_before(self, cutoff_date: str) -> int:
        """删除严格早于 ``cutoff_date`` 的记录。

        ISO 日期字符串字典序与时间序一致，可直接用 ``<`` 比较。

        Args:
            cutoff_date: ``YYYY-MM-DD``；该日期及之后的记录保留。

        Returns:
            实际删除的行数。
        """
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM daily_stats WHERE date < ?", (cutoff_date,)
            )
            return int(cursor.rowcount)

    def delete_range(self, start_date: str, end_date: str) -> int:
        """删除日期闭区间内的记录（供自定义范围清理使用）。

        Args:
            start_date: 起始日期 ``YYYY-MM-DD``（含）。
            end_date: 结束日期 ``YYYY-MM-DD``（含）。

        Returns:
            实际删除的行数。
        """
        if start_date > end_date:
            return 0
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM daily_stats WHERE date BETWEEN ? AND ?",
                (start_date, end_date),
            )
            return int(cursor.rowcount)

    def delete_all(self) -> int:
        """删除全部历史记录（含今日）。

        Returns:
            实际删除的行数。
        """
        with self._lock, self._connection() as conn:
            cursor = conn.execute("DELETE FROM daily_stats")
            return int(cursor.rowcount)

    def vacuum(self) -> None:
        """执行 VACUUM 回收磁盘空间。

        必须在 autocommit 连接上执行，否则 SQLite 会拒绝在事务内 VACUUM。
        调用方应在 ``delete_before`` / ``delete_all`` 之后调用本方法。

        Raises:
            sqlite3.Error: VACUUM 失败（如磁盘空间不足、数据库被占用）。
        """
        cfg.ensure_directories()
        with self._lock:
            conn = sqlite3.connect(
                str(self._path), timeout=10.0, isolation_level=None
            )
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()
        logger.info("已执行 VACUUM 回收空间: %s", self._path)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """提供一个自动提交/回滚/关闭的短连接。

        Yields:
            已设置 ``row_factory`` 的 SQLite 连接。

        Raises:
            Exception: 块内异常触发回滚后原样抛出。
        """
        cfg.ensure_directories()
        conn = sqlite3.connect(str(self._path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row_to_stats(row: sqlite3.Row) -> DailyStats:
        """把查询结果行转换为 :class:`DailyStats`。

        Args:
            row: 含 date / eye_seconds / rest_count / updated_at 的行。

        Returns:
            对应的数据对象。
        """
        return DailyStats(
            date=str(row["date"]),
            eye_seconds=int(row["eye_seconds"]),
            rest_count=int(row["rest_count"]),
            updated_at=str(row["updated_at"]),
        )


# 应用级单例；app.py 启动时需调用 stats_repository.initialize()。
# 不在 import 时自动建库，避免单元测试误创建真实数据库文件。
stats_repository = StatsRepository()
