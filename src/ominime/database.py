"""
数据库模块

使用 SQLite 存储输入记录和统计数据
"""

import sqlite3
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path

from .config import config
from .time_utils import business_day_bounds_for_storage, business_today, storage_now


@dataclass
class InputRecord:
    """输入记录"""
    id: Optional[int]
    timestamp: datetime
    app_name: str
    app_bundle_id: str
    display_name: str
    content: str
    char_count: int
    session_id: str
    duration_seconds: float


@dataclass
class DailySummary:
    """每日汇总"""
    id: Optional[int]
    date: date
    app_name: str
    app_bundle_id: str
    display_name: str
    total_chars: int
    session_count: int
    total_time_seconds: float
    content_summary: Optional[str]
    suggestions: Optional[str]


@dataclass
class AppDailyStats:
    """应用每日统计"""
    app_name: str
    display_name: str
    total_chars: int
    session_count: int
    total_time_minutes: float
    sample_content: List[str]


@dataclass
class SubmissionContextRecord:
    """Enter 提交上下文记录"""
    id: Optional[int]
    submission_id: str
    input_record_id: Optional[int]
    timestamp: datetime
    app_name: str
    app_bundle_id: str
    window_title: Optional[str] = None
    focused_role: Optional[str] = None
    focused_subrole: Optional[str] = None
    focused_title: Optional[str] = None
    focused_description: Optional[str] = None
    focused_identifier: Optional[str] = None
    focused_frame_json: Optional[str] = None
    container_role: Optional[str] = None
    container_title: Optional[str] = None
    container_frame_json: Optional[str] = None
    ax_hierarchy_json: Optional[str] = None
    screenshot_path: Optional[str] = None
    screenshot_scope: Optional[str] = None
    qwen_analysis_json: Optional[str] = None
    qwen_raw_output: Optional[str] = None
    qwen_model: Optional[str] = None
    analysis_status: str = "pending"
    analysis_error: Optional[str] = None
    capture_status: str = "ok"
    capture_error: Optional[str] = None


@dataclass
class CaptureDiagnosticRecord:
    """Enter 捕获决策诊断记录"""
    id: Optional[int]
    timestamp: datetime
    app_name: str
    app_bundle_id: str
    event_type: str
    decision_action: str
    decision_reason: str
    selected_source: Optional[str] = None
    selected_confidence: Optional[float] = None
    physical_key_count: Optional[int] = None
    focused_role: Optional[str] = None
    focused_subrole: Optional[str] = None
    capture_status: str = "ok"
    diagnostics_json: Optional[str] = None


class Database:
    """
    数据库管理类
    """
    
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or config.db_path
        self._init_db()
        self.db_path.chmod(0o600)
    
    @contextmanager
    def _get_connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def _init_db(self):
        """初始化数据库表"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL")
            
            # 输入记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS input_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    app_name TEXT NOT NULL,
                    app_bundle_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    content TEXT,
                    char_count INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    duration_seconds REAL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 每日汇总表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL,
                    app_name TEXT NOT NULL,
                    app_bundle_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    total_chars INTEGER NOT NULL,
                    session_count INTEGER DEFAULT 1,
                    total_time_seconds REAL DEFAULT 0,
                    content_summary TEXT,
                    suggestions TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, app_bundle_id)
                )
            """)
            
            # 全局每日汇总表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS global_daily_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL UNIQUE,
                    total_chars INTEGER NOT NULL,
                    total_apps INTEGER NOT NULL,
                    total_sessions INTEGER NOT NULL,
                    total_time_seconds REAL DEFAULT 0,
                    main_activities TEXT,
                    summary TEXT,
                    suggestions TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_input_records_timestamp 
                ON input_records(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_input_records_app 
                ON input_records(app_bundle_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_summaries_date 
                ON daily_summaries(date)
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS submission_contexts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id TEXT NOT NULL UNIQUE,
                    input_record_id INTEGER,
                    timestamp DATETIME NOT NULL,
                    app_name TEXT NOT NULL,
                    app_bundle_id TEXT NOT NULL,
                    window_title TEXT,
                    focused_role TEXT,
                    focused_subrole TEXT,
                    focused_title TEXT,
                    focused_description TEXT,
                    focused_identifier TEXT,
                    focused_frame_json TEXT,
                    container_role TEXT,
                    container_title TEXT,
                    container_frame_json TEXT,
                    ax_hierarchy_json TEXT,
                    screenshot_path TEXT,
                    screenshot_scope TEXT,
                    qwen_analysis_json TEXT,
                    qwen_raw_output TEXT,
                    qwen_model TEXT,
                    analysis_status TEXT DEFAULT 'pending',
                    analysis_error TEXT,
                    capture_status TEXT NOT NULL,
                    capture_error TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(input_record_id) REFERENCES input_records(id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_submission_contexts_timestamp
                ON submission_contexts(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_submission_contexts_input_record
                ON submission_contexts(input_record_id)
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS capture_diagnostics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    app_name TEXT NOT NULL,
                    app_bundle_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    decision_action TEXT NOT NULL,
                    decision_reason TEXT NOT NULL,
                    selected_source TEXT,
                    selected_confidence REAL,
                    physical_key_count INTEGER,
                    focused_role TEXT,
                    focused_subrole TEXT,
                    capture_status TEXT NOT NULL,
                    diagnostics_json TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_capture_diagnostics_timestamp
                ON capture_diagnostics(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_capture_diagnostics_decision
                ON capture_diagnostics(decision_action)
            """)
    
    # ===== 输入记录操作 =====
    
    def save_input_record(self, record: InputRecord) -> int:
        """保存输入记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO input_records 
                (timestamp, app_name, app_bundle_id, display_name, content, 
                 char_count, session_id, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.timestamp.isoformat(),
                record.app_name,
                record.app_bundle_id,
                record.display_name,
                record.content,
                record.char_count,
                record.session_id,
                record.duration_seconds,
            ))
            return cursor.lastrowid
    
    def get_records_by_date(self, target_date: date) -> List[InputRecord]:
        """获取指定日期的所有记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            start, end = business_day_bounds_for_storage(target_date)
            
            cursor.execute("""
                SELECT * FROM input_records 
                WHERE timestamp >= ? AND timestamp < ?
                ORDER BY timestamp, id
            """, (start.isoformat(), end.isoformat()))
            
            return [self._row_to_input_record(row) for row in cursor.fetchall()]
    
    def get_records_by_app(self, app_bundle_id: str, target_date: Optional[date] = None) -> List[InputRecord]:
        """获取指定应用的记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            if target_date:
                start, end = business_day_bounds_for_storage(target_date)
                cursor.execute("""
                    SELECT * FROM input_records 
                    WHERE app_bundle_id = ? AND timestamp >= ? AND timestamp < ?
                    ORDER BY timestamp
                """, (app_bundle_id, start.isoformat(), end.isoformat()))
            else:
                cursor.execute("""
                    SELECT * FROM input_records 
                    WHERE app_bundle_id = ?
                    ORDER BY timestamp DESC
                    LIMIT 1000
                """, (app_bundle_id,))
            
            return [self._row_to_input_record(row) for row in cursor.fetchall()]

    def get_latest_input_record(self) -> Optional[InputRecord]:
        """获取最近一条输入记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM input_records
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            row = cursor.fetchone()
            return self._row_to_input_record(row) if row else None
    
    def _row_to_input_record(self, row) -> InputRecord:
        """将数据库行转换为 InputRecord"""
        return InputRecord(
            id=row['id'],
            timestamp=datetime.fromisoformat(row['timestamp']),
            app_name=row['app_name'],
            app_bundle_id=row['app_bundle_id'],
            display_name=row['display_name'],
            content=row['content'],
            char_count=row['char_count'],
            session_id=row['session_id'],
            duration_seconds=row['duration_seconds'],
        )

    # ===== 提交上下文操作 =====

    def save_submission_context(self, record: SubmissionContextRecord) -> int:
        """保存 Enter 提交上下文记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO submission_contexts
                (submission_id, input_record_id, timestamp, app_name, app_bundle_id,
                 window_title, focused_role, focused_subrole, focused_title,
                 focused_description, focused_identifier, focused_frame_json,
                 container_role, container_title, container_frame_json, ax_hierarchy_json,
                 screenshot_path, screenshot_scope, qwen_analysis_json, qwen_raw_output,
                 qwen_model, analysis_status, analysis_error, capture_status, capture_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(submission_id) DO UPDATE SET
                    input_record_id = excluded.input_record_id,
                    timestamp = excluded.timestamp,
                    app_name = excluded.app_name,
                    app_bundle_id = excluded.app_bundle_id,
                    window_title = excluded.window_title,
                    focused_role = excluded.focused_role,
                    focused_subrole = excluded.focused_subrole,
                    focused_title = excluded.focused_title,
                    focused_description = excluded.focused_description,
                    focused_identifier = excluded.focused_identifier,
                    focused_frame_json = excluded.focused_frame_json,
                    container_role = excluded.container_role,
                    container_title = excluded.container_title,
                    container_frame_json = excluded.container_frame_json,
                    ax_hierarchy_json = excluded.ax_hierarchy_json,
                    screenshot_path = excluded.screenshot_path,
                    screenshot_scope = excluded.screenshot_scope,
                    qwen_analysis_json = excluded.qwen_analysis_json,
                    qwen_raw_output = excluded.qwen_raw_output,
                    qwen_model = excluded.qwen_model,
                    analysis_status = excluded.analysis_status,
                    analysis_error = excluded.analysis_error,
                    capture_status = excluded.capture_status,
                    capture_error = excluded.capture_error
            """, (
                record.submission_id,
                record.input_record_id,
                record.timestamp.isoformat(),
                record.app_name,
                record.app_bundle_id,
                record.window_title,
                record.focused_role,
                record.focused_subrole,
                record.focused_title,
                record.focused_description,
                record.focused_identifier,
                record.focused_frame_json,
                record.container_role,
                record.container_title,
                record.container_frame_json,
                record.ax_hierarchy_json,
                record.screenshot_path,
                record.screenshot_scope,
                record.qwen_analysis_json,
                record.qwen_raw_output,
                record.qwen_model,
                record.analysis_status,
                record.analysis_error,
                record.capture_status,
                record.capture_error,
            ))
            if cursor.lastrowid:
                return cursor.lastrowid
            cursor.execute("SELECT id FROM submission_contexts WHERE submission_id = ?", (record.submission_id,))
            return cursor.fetchone()["id"]

    def get_submission_context(self, submission_id: str) -> Optional[SubmissionContextRecord]:
        """按 submission_id 查询上下文记录"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM submission_contexts WHERE submission_id = ?", (submission_id,))
            row = cursor.fetchone()
            return self._row_to_submission_context(row) if row else None

    def get_recent_submission_contexts(self, limit: int = 50) -> List[Dict]:
        """查询最近提交上下文，包含输入内容"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    c.*,
                    r.content,
                    r.char_count,
                    r.display_name,
                    r.created_at AS input_created_at
                FROM submission_contexts c
                LEFT JOIN input_records r ON r.id = c.input_record_id
                ORDER BY c.timestamp DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_submission_contexts_by_date(self, target_date: date, limit: int = 100) -> List[Dict]:
        """查询指定日期的提交上下文，包含输入内容"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            start, end = business_day_bounds_for_storage(target_date)
            cursor.execute("""
                SELECT
                    c.*,
                    r.content,
                    r.char_count,
                    r.display_name,
                    r.created_at AS input_created_at
                FROM submission_contexts c
                LEFT JOIN input_records r ON r.id = c.input_record_id
                WHERE c.timestamp >= ? AND c.timestamp < ?
                ORDER BY c.timestamp DESC
                LIMIT ?
            """, (start.isoformat(), end.isoformat(), limit))
            return [dict(row) for row in cursor.fetchall()]

    def _row_to_submission_context(self, row) -> SubmissionContextRecord:
        return SubmissionContextRecord(
            id=row["id"],
            submission_id=row["submission_id"],
            input_record_id=row["input_record_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            app_name=row["app_name"],
            app_bundle_id=row["app_bundle_id"],
            window_title=row["window_title"],
            focused_role=row["focused_role"],
            focused_subrole=row["focused_subrole"],
            focused_title=row["focused_title"],
            focused_description=row["focused_description"],
            focused_identifier=row["focused_identifier"],
            focused_frame_json=row["focused_frame_json"],
            container_role=row["container_role"],
            container_title=row["container_title"],
            container_frame_json=row["container_frame_json"],
            ax_hierarchy_json=row["ax_hierarchy_json"],
            screenshot_path=row["screenshot_path"],
            screenshot_scope=row["screenshot_scope"],
            qwen_analysis_json=row["qwen_analysis_json"],
            qwen_raw_output=row["qwen_raw_output"],
            qwen_model=row["qwen_model"],
            analysis_status=row["analysis_status"],
            analysis_error=row["analysis_error"],
            capture_status=row["capture_status"],
            capture_error=row["capture_error"],
        )

    # ===== 捕获诊断操作 =====

    def save_capture_diagnostic(self, record: CaptureDiagnosticRecord) -> int:
        """保存一次 Enter 捕获决策诊断。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO capture_diagnostics
                (timestamp, app_name, app_bundle_id, event_type, decision_action,
                 decision_reason, selected_source, selected_confidence,
                 physical_key_count, focused_role, focused_subrole, capture_status,
                 diagnostics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.timestamp.isoformat(),
                record.app_name,
                record.app_bundle_id,
                record.event_type,
                record.decision_action,
                record.decision_reason,
                record.selected_source,
                record.selected_confidence,
                record.physical_key_count,
                record.focused_role,
                record.focused_subrole,
                record.capture_status,
                record.diagnostics_json,
            ))
            return cursor.lastrowid

    def get_recent_capture_diagnostics(
        self, limit: int = 50, app_bundle_id: Optional[str] = None
    ) -> List[Dict]:
        """按应用可选筛选最近的捕获诊断，避免高频其他应用淹没 Safari 记录。"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if app_bundle_id:
                cursor.execute("""
                    SELECT *
                    FROM capture_diagnostics
                    WHERE app_bundle_id = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                """, (app_bundle_id, limit))
            else:
                cursor.execute("""
                    SELECT *
                    FROM capture_diagnostics
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    # ===== 每日汇总操作 =====
    
    def save_daily_summary(self, summary: DailySummary):
        """保存每日汇总（使用 upsert）"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO daily_summaries 
                (date, app_name, app_bundle_id, display_name, total_chars, 
                 session_count, total_time_seconds, content_summary, suggestions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, app_bundle_id) DO UPDATE SET
                    total_chars = excluded.total_chars,
                    session_count = excluded.session_count,
                    total_time_seconds = excluded.total_time_seconds,
                    content_summary = excluded.content_summary,
                    suggestions = excluded.suggestions
            """, (
                summary.date.isoformat(),
                summary.app_name,
                summary.app_bundle_id,
                summary.display_name,
                summary.total_chars,
                summary.session_count,
                summary.total_time_seconds,
                summary.content_summary,
                summary.suggestions,
            ))
    
    def get_daily_summaries(self, target_date: date) -> List[DailySummary]:
        """获取指定日期的所有应用汇总"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM daily_summaries 
                WHERE date = ?
                ORDER BY total_chars DESC
            """, (target_date.isoformat(),))
            
            return [self._row_to_daily_summary(row) for row in cursor.fetchall()]
    
    def _row_to_daily_summary(self, row) -> DailySummary:
        """将数据库行转换为 DailySummary"""
        return DailySummary(
            id=row['id'],
            date=date.fromisoformat(row['date']),
            app_name=row['app_name'],
            app_bundle_id=row['app_bundle_id'],
            display_name=row['display_name'],
            total_chars=row['total_chars'],
            session_count=row['session_count'],
            total_time_seconds=row['total_time_seconds'],
            content_summary=row['content_summary'],
            suggestions=row['suggestions'],
        )
    
    # ===== 全局汇总操作 =====
    
    def save_global_daily_summary(
        self,
        target_date: date,
        total_chars: int,
        total_apps: int,
        total_sessions: int,
        total_time_seconds: float,
        main_activities: str,
        summary: str,
        suggestions: str,
    ):
        """保存全局每日汇总"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO global_daily_summaries 
                (date, total_chars, total_apps, total_sessions, total_time_seconds,
                 main_activities, summary, suggestions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_chars = excluded.total_chars,
                    total_apps = excluded.total_apps,
                    total_sessions = excluded.total_sessions,
                    total_time_seconds = excluded.total_time_seconds,
                    main_activities = excluded.main_activities,
                    summary = excluded.summary,
                    suggestions = excluded.suggestions
            """, (
                target_date.isoformat(),
                total_chars,
                total_apps,
                total_sessions,
                total_time_seconds,
                main_activities,
                summary,
                suggestions,
            ))
    
    def get_global_daily_summary(self, target_date: date) -> Optional[Dict]:
        """获取全局每日汇总"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM global_daily_summaries 
                WHERE date = ?
            """, (target_date.isoformat(),))
            
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    
    # ===== 统计查询 =====
    
    def get_daily_stats(self, target_date: date) -> List[AppDailyStats]:
        """获取指定日期的应用统计"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            start, end = business_day_bounds_for_storage(target_date)
            
            # 按应用汇总
            cursor.execute("""
                SELECT 
                    app_bundle_id,
                    MAX(app_name) as app_name,
                    MAX(display_name) as display_name,
                    SUM(char_count) as total_chars,
                    COUNT(DISTINCT session_id) as session_count,
                    COALESCE(SUM(duration_seconds), 0) as total_duration_seconds
                FROM input_records 
                WHERE timestamp >= ? AND timestamp < ?
                GROUP BY app_bundle_id
                ORDER BY total_chars DESC
            """, (start.isoformat(), end.isoformat()))
            
            stats = []
            for row in cursor.fetchall():
                # 获取样本内容
                cursor.execute("""
                    SELECT content FROM input_records 
                    WHERE timestamp >= ? AND timestamp < ? 
                    AND app_bundle_id = ? AND content IS NOT NULL AND LENGTH(content) > 10
                    ORDER BY char_count DESC
                    LIMIT 5
                """, (start.isoformat(), end.isoformat(), row['app_bundle_id']))
                
                samples = [r['content'] for r in cursor.fetchall()]
                
                total_minutes = float(row['total_duration_seconds']) / 60.0
                
                stats.append(AppDailyStats(
                    app_name=row['app_name'],
                    display_name=row['display_name'],
                    total_chars=row['total_chars'],
                    session_count=row['session_count'],
                    total_time_minutes=total_minutes,
                    sample_content=samples,
                ))
            
            return stats
    
    def get_total_chars_today(self) -> int:
        """获取今日总字符数"""
        today = business_today()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            start, end = business_day_bounds_for_storage(today)
            
            cursor.execute("""
                SELECT COALESCE(SUM(char_count), 0) as total
                FROM input_records 
                WHERE timestamp >= ? AND timestamp < ?
            """, (start.isoformat(), end.isoformat()))
            
            return cursor.fetchone()['total']

    def get_recent_days_summary(self, days: int = 7) -> List[Dict]:
        """获取最近几天的汇总"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            result = []
            today = business_today()
            for offset in range(days):
                target_date = today - timedelta(days=offset)
                start, end = business_day_bounds_for_storage(target_date)
                cursor.execute("""
                    SELECT
                        COALESCE(SUM(char_count), 0) as total_chars,
                        COUNT(DISTINCT app_bundle_id) as app_count,
                        COUNT(DISTINCT session_id) as session_count
                    FROM input_records
                    WHERE timestamp >= ? AND timestamp < ?
                """, (start.isoformat(), end.isoformat()))
                row = cursor.fetchone()
                total_chars = row["total_chars"] or 0
                if total_chars <= 0:
                    continue
                result.append({
                    "day": target_date.isoformat(),
                    "total_chars": total_chars,
                    "app_count": row["app_count"],
                    "session_count": row["session_count"],
                })
            return result


# 单例数据库实例
_db_instance: Optional[Database] = None


def get_database() -> Database:
    """获取数据库单例"""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance


# 测试代码
if __name__ == "__main__":
    db = get_database()
    
    # 测试保存记录
    record = InputRecord(
        id=None,
        timestamp=storage_now(),
        app_name="Cursor",
        app_bundle_id="com.todesktop.230313mzl4w4u92",
        display_name="Cursor",
        content="Hello, World!",
        char_count=13,
        session_id="test-session-1",
        duration_seconds=5.0,
    )
    
    record_id = db.save_input_record(record)
    print(f"保存记录 ID: {record_id}")
    
    # 测试查询
    today = business_today()
    records = db.get_records_by_date(today)
    print(f"今日记录数: {len(records)}")
    
    stats = db.get_daily_stats(today)
    print(f"今日应用统计: {stats}")
