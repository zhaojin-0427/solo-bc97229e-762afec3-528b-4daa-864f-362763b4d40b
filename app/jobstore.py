"""作业持久化层：SQLite 本地存储，Web 进程与执行器进程共用。

设计要点：
- 提交即固定输入：schema_text / sql_text 在创建时写入，之后不再修改；
  重试是把同一份输入插入为一个新作业。
- 队列顺序即 id（AUTOINCREMENT）顺序，重启后天然保留。
- 所有进入终态的迁移都是条件 UPDATE（WHERE status=...），因此执行器
  迟到的结果写不进已取消/已超时的作业，终态不会被改写。
- 仅执行器进程（worker.py）会把作业推进到 running 及终态；Web 进程
  只做 queued -> cancelled 与 cancel_requested 标记，互不冲突。
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager

STATUS_QUEUED = "queued"            # 排队中
STATUS_RUNNING = "running"          # 执行中
STATUS_SUCCEEDED = "succeeded"      # 已完成
STATUS_FAILED = "failed"            # 分析失败（语法/溯源错误等）
STATUS_CANCELLED = "cancelled"      # 已取消（排队中或执行中被用户取消）
STATUS_INTERRUPTED = "interrupted"  # 已中断（执行器重启时仍在执行）
STATUS_TIMEOUT = "timeout"          # 超时中止（超过执行时限被强制停止）

TERMINAL_STATES = (
    STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED,
    STATUS_INTERRUPTED, STATUS_TIMEOUT,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_text      TEXT NOT NULL,
    sql_text         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued',
    error            TEXT,
    result_json      TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL
);
"""

DEFAULT_DB_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "lineage_jobs.db"))


def resolve_db_path(db_path=None):
    return db_path or os.environ.get("LINEAGE_DB") or DEFAULT_DB_PATH


class JobStore:
    """围绕单个 SQLite 文件的作业队列存取。每个方法使用独立连接，进程/线程安全。"""

    def __init__(self, db_path=None):
        self.db_path = resolve_db_path(db_path)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    # ---- 连接 ----

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def _conn(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ---- Web 侧：提交 / 查询 ----

    def create_job(self, schema_text, sql_text):
        """提交作业：固定输入文本，状态 queued，返回作业号。"""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (schema_text, sql_text, status, created_at)"
                " VALUES (?, ?, ?, ?)",
                (schema_text, sql_text, STATUS_QUEUED, time.time()))
            return cur.lastrowid

    def get_job(self, job_id):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            job = self._to_dict(row)
            job["queue_ahead"] = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = ? AND id < ?",
                (STATUS_QUEUED, job_id)).fetchone()["n"]
            return job

    def list_jobs(self, limit=200):
        """按提交倒序返回作业；queued 作业附带 queue_ahead（前方排队数）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT j.*,"
                " (SELECT COUNT(*) FROM jobs q"
                "   WHERE q.status = ? AND q.id < j.id) AS queue_ahead"
                " FROM jobs j ORDER BY j.id DESC LIMIT ?",
                (STATUS_QUEUED, limit)).fetchall()
            running = conn.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY id LIMIT 1",
                (STATUS_RUNNING,)).fetchone()
        jobs = []
        for row in rows:
            job = self._to_dict(row)
            job["queue_ahead"] = row["queue_ahead"]
            jobs.append(job)
        return jobs, (running["id"] if running else None)

    # ---- Web 侧：取消 / 重试 ----

    def cancel_job(self, job_id):
        """取消作业。

        返回 (code, message)：
        - ("cancelled",  ...) 排队中直接取消成功（终态）
        - ("cancelling", ...) 执行中已标记取消，执行器将中止计算并置为 cancelled
        - ("not_found",  ...) 作业不存在
        - ("terminal",   ...) 已处于终态，无法取消
        """
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = ?, cancel_requested = 1, finished_at = ?"
                " WHERE id = ? AND status = ?",
                (STATUS_CANCELLED, now, job_id, STATUS_QUEUED))
            if cur.rowcount:
                return "cancelled", "作业已取消"
            cur = conn.execute(
                "UPDATE jobs SET cancel_requested = 1"
                " WHERE id = ? AND status = ?",
                (job_id, STATUS_RUNNING))
            if cur.rowcount:
                return "cancelling", "已请求取消，执行器将尽快中止计算"
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return "not_found", "作业不存在"
            return "terminal", f"作业已处于终态（{row['status']}），无法取消"

    def retry_job(self, job_id):
        """以原输入重试为新作业。返回 (new_job_id, error)。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None, "作业不存在"
            if row["status"] not in TERMINAL_STATES:
                return None, "作业尚未结束，不能重试"
            cur = conn.execute(
                "INSERT INTO jobs (schema_text, sql_text, status, created_at)"
                " VALUES (?, ?, ?, ?)",
                (row["schema_text"], row["sql_text"], STATUS_QUEUED, time.time()))
            return cur.lastrowid, None

    # ---- 执行器侧：恢复 / 认领 / 完结 ----

    def recover_interrupted(self):
        """执行器启动时调用：把遗留的 running 作业标记为 interrupted。

        排队中的作业保持 queued，继续按提交顺序执行；已结束作业不受影响。
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE status = ?",
                (STATUS_INTERRUPTED, time.time(), STATUS_RUNNING))
            return cur.rowcount

    def sweep_cancelled_queued(self):
        """兜底：把排队期间被标记取消的作业落为 cancelled 终态。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?"
                " WHERE status = ? AND cancel_requested = 1",
                (STATUS_CANCELLED, time.time(), STATUS_QUEUED))

    def claim_next(self):
        """认领队首作业（id 最小、未取消的 queued），置为 running 并返回。

        单条 UPDATE 原子完成「选队首 + 占位」，没有可执行作业时返回 None。
        """
        now = time.time()
        with self._conn() as conn:
            row = conn.execute(
                "UPDATE jobs SET status = ?, started_at = ?"
                " WHERE id = (SELECT id FROM jobs"
                "              WHERE status = ? AND cancel_requested = 0"
                "              ORDER BY id LIMIT 1)"
                " RETURNING *",
                (STATUS_RUNNING, now, STATUS_QUEUED)).fetchone()
        return self._to_dict(row) if row else None

    def cancel_requested(self, job_id):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?",
                (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def finish_job(self, job_id, status, error=None, result=None):
        """终态迁移（仅执行器调用）。

        条件 UPDATE：只有仍处于 running 的作业才会被写入。若作业已被
        取消/超时等先行落为终态，返回 False —— 迟到结果直接丢弃，
        终态不会被改写。
        """
        assert status in TERMINAL_STATES
        result_json = (json.dumps(result, ensure_ascii=False)
                       if result is not None else None)
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = ?, error = ?, result_json = ?,"
                "                finished_at = ?"
                " WHERE id = ? AND status = ?",
                (status, error, result_json, time.time(),
                 job_id, STATUS_RUNNING))
            return cur.rowcount > 0

    # ---- 工具 ----

    @staticmethod
    def _to_dict(row):
        return {key: row[key] for key in row.keys()}
