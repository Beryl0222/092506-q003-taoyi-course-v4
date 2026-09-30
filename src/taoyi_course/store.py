"""课次与作品交接的本地持久化边界。

使用 SQLite 保存全部领域数据。写操作统一走 ``BEGIN IMMEDIATE`` 事务：
- 同一课次的并发报名在文件库 + 多连接下被串行化，容量判定不会超卖；
- 候补补位、同意撤回等“改一处、牵一串”的操作在同一个事务里提交或整体回滚。
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .domain import Record, now_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS class_session (
    record_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teacher (
    teacher_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    stages TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS student (
    student_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    guardian_id TEXT,
    consent INTEGER NOT NULL DEFAULT 0,
    consent_updated_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
    session_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    title TEXT,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    wheels INTEGER,
    status TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL,
    CHECK(ends_at > starts_at)
);

CREATE TABLE IF NOT EXISTS session_teacher (
    session_id TEXT NOT NULL REFERENCES session(session_id),
    teacher_id TEXT NOT NULL REFERENCES teacher(teacher_id),
    role TEXT NOT NULL DEFAULT 'lead',
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(session_id, teacher_id)
);

CREATE TABLE IF NOT EXISTS enrollment (
    enrollment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES session(session_id),
    student_id TEXT NOT NULL REFERENCES student(student_id),
    status TEXT NOT NULL,
    waitlist_position INTEGER,
    check_in_at TEXT,
    makeup_grant_id INTEGER,
    origin_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 同一学生在同一课次中只能保留一条“活跃”记录：并发报名靠它兜底。
CREATE UNIQUE INDEX IF NOT EXISTS enrollment_active_ux
    ON enrollment(session_id, student_id)
    WHERE status IN ('enrolled', 'waitlisted', 'attended');
CREATE INDEX IF NOT EXISTS enrollment_session_idx
    ON enrollment(session_id, status);
CREATE INDEX IF NOT EXISTS enrollment_student_idx
    ON enrollment(student_id, status);

CREATE TABLE IF NOT EXISTS makeup_grant (
    grant_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL REFERENCES student(student_id),
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    used_session_id TEXT,
    used_enrollment_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS makeup_grant_student_idx
    ON makeup_grant(student_id, status);

CREATE TABLE IF NOT EXISTS artwork (
    artwork_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES student(student_id),
    origin_session_id TEXT,
    current_state TEXT NOT NULL,
    custody_holder_type TEXT NOT NULL,
    custody_holder_id TEXT,
    scrapped_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artwork_student_idx ON artwork(student_id);

CREATE TABLE IF NOT EXISTS artwork_transfer (
    transfer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    artwork_id TEXT NOT NULL REFERENCES artwork(artwork_id),
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    from_holder_type TEXT,
    from_holder_id TEXT,
    to_holder_type TEXT,
    to_holder_id TEXT,
    actor_id TEXT,
    session_id TEXT,
    batch_id TEXT,
    note TEXT,
    UNIQUE(artwork_id, seq)
);

CREATE TABLE IF NOT EXISTS kiln (
    kiln_id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    paused_at TEXT,
    resumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kiln_batch (
    batch_id TEXT PRIMARY KEY,
    kiln_id TEXT NOT NULL REFERENCES kiln(kiln_id),
    status TEXT NOT NULL,
    held_from_status TEXT,
    created_at TEXT NOT NULL,
    loaded_at TEXT,
    held_at TEXT,
    resumed_at TEXT,
    fired_at TEXT
);
CREATE INDEX IF NOT EXISTS kiln_batch_kiln_idx ON kiln_batch(kiln_id, status);

CREATE TABLE IF NOT EXISTS batch_item (
    batch_id TEXT NOT NULL REFERENCES kiln_batch(batch_id),
    artwork_id TEXT NOT NULL REFERENCES artwork(artwork_id),
    state_on_load TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    unloaded_at TEXT,
    PRIMARY KEY(batch_id, artwork_id)
);
-- 一件作品同一时间只能出现在一个尚未出窑的批次里。
CREATE UNIQUE INDEX IF NOT EXISTS batch_item_open_ux
    ON batch_item(artwork_id) WHERE unloaded_at IS NULL;

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_id TEXT,
    actor_role TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_entity_idx
    ON audit_log(entity_type, entity_id, audit_id);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # check_same_thread=False：允许压测线程各自持有连接访问同一个文件库。
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # 显式管理事务（手动 BEGIN IMMEDIATE），关闭 sqlite3 的隐式事务。
        self.connection.isolation_level = None
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        if self.path != ":memory:":
            # WAL 让读写并发更顺畅；多个 :memory: 连接本来就互不可见。
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)

    # -- 事务边界 -----------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # -- 小工具 -------------------------------------------------------------

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, params).fetchall()

    def insert_audit(self, conn: sqlite3.Connection, *, actor_id: str | None,
                     actor_role: str | None, action: str, entity_type: str,
                     entity_id: str, detail: dict[str, Any] | None = None,
                     at: str | None = None) -> int:
        cur = conn.execute(
            "INSERT INTO audit_log(at, actor_id, actor_role, action, "
            "entity_type, entity_id, detail) VALUES(?,?,?,?,?,?,?)",
            (at or now_iso(), actor_id, actor_role, action, entity_type,
             str(entity_id), json.dumps(detail or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    # -- 早期骨架保留的通用记录 ---------------------------------------------

    def save(self, record: Record) -> Record:
        value = record.with_timestamp()
        self.connection.execute(
            "INSERT INTO class_session(record_id, owner_id, state, created_at) "
            "VALUES(?,?,?,?)",
            (value.record_id, value.owner_id, value.state, value.created_at),
        )
        self.connection.commit()
        return value

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, created_at "
            "FROM class_session WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    def close(self) -> None:
        self.connection.close()
