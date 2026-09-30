"""课次、报名、作品交接与窑炉周转的 SQLite 持久化边界。

所有“先查后写”的业务编排都必须在 :meth:`Store.transaction` 提供的
``BEGIN IMMEDIATE`` 事务内完成：写锁在事务开始时即获取，配合进程内互斥锁，
保证并发报名下容量计数不会超卖，异常时整体回滚。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .domain import Record, now_iso


SCHEMA = """
CREATE TABLE IF NOT EXISTS class_session_legacy (
    record_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teacher (
    teacher_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    qualifications TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS student (
    student_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consent (
    student_id TEXT PRIMARY KEY,
    granted INTEGER NOT NULL,
    granted_at TEXT,
    revoked_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session (
    session_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    scheduled_start TEXT NOT NULL,
    scheduled_end TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    wheel_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    makeup_for_session TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_teacher (
    session_id TEXT NOT NULL,
    teacher_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, teacher_id)
);
CREATE TABLE IF NOT EXISTS enrollment (
    session_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    status TEXT NOT NULL,
    waitlist_position INTEGER,
    enrolled_at TEXT,
    waitlisted_at TEXT,
    withdrawn_at TEXT,
    withdraw_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, student_id)
);
CREATE TABLE IF NOT EXISTS attendance (
    session_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    status TEXT NOT NULL,
    marked_by TEXT NOT NULL,
    marked_at TEXT NOT NULL,
    PRIMARY KEY (session_id, student_id)
);
CREATE TABLE IF NOT EXISTS makeup_credit (
    credit_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'granted',
    target_session_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS artifact (
    artifact_id TEXT PRIMARY KEY,
    owner_student_id TEXT NOT NULL,
    origin_session_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_custody (
    artifact_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    action TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    custodian_id TEXT NOT NULL,
    custodian_role TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (artifact_id, seq)
);
CREATE TABLE IF NOT EXISTS kiln (
    kiln_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    halted_at TEXT,
    resumed_at TEXT
);
CREATE TABLE IF NOT EXISTS firing_batch (
    batch_id TEXT PRIMARY KEY,
    kiln_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'loaded',
    loaded_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS batch_artifact (
    batch_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, artifact_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT '',
    entity_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        # 单连接 + 进程锁：SQLite 同一连接天然串行化，锁保证跨线程事务不交错。
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    # -- 事务边界 -----------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """打开 ``BEGIN IMMEDIATE`` 事务；异常回滚，正常提交。"""
        conn = self.connection
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    # -- 通用 ---------------------------------------------------------------

    @staticmethod
    def _row(conn: sqlite3.Connection, sql: str,
             params: tuple = ()) -> sqlite3.Row | None:
        return conn.execute(sql, params).fetchone()

    @staticmethod
    def _rows(conn: sqlite3.Connection, sql: str,
              params: tuple = ()) -> list[sqlite3.Row]:
        return list(conn.execute(sql, params).fetchall())

    # -- 旧版登记（兼容基线）-----------------------------------------------

    def save(self, record: Record) -> Record:
        value = record.with_timestamp()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO class_session_legacy"
                "(record_id, owner_id, state, created_at) VALUES(?,?,?,?)",
                (value.record_id, value.owner_id, value.state, value.created_at),
            )
        return value

    def get(self, record_id: str) -> Record | None:
        row = self._row(
            self.connection,
            "SELECT record_id, owner_id, state, created_at "
            "FROM class_session_legacy WHERE record_id=?",
            (record_id,),
        )
        return Record(**dict(row)) if row else None

    # -- 教师 / 学生 / 同意 -------------------------------------------------

    def create_teacher(self, conn: sqlite3.Connection, teacher_id: str,
                       name: str, qualifications: list[str]) -> None:
        conn.execute(
            "INSERT INTO teacher(teacher_id, name, qualifications, created_at)"
            " VALUES(?,?,?,?)",
            (teacher_id, name, json.dumps(qualifications, ensure_ascii=False),
             now_iso()),
        )

    def get_teacher(self, teacher_id: str) -> dict | None:
        row = self._row(self.connection,
                        "SELECT * FROM teacher WHERE teacher_id=?",
                        (teacher_id,))
        if not row:
            return None
        data = dict(row)
        data["qualifications"] = json.loads(data["qualifications"])
        return data

    def create_student(self, conn: sqlite3.Connection, student_id: str,
                       name: str) -> None:
        conn.execute(
            "INSERT INTO student(student_id, name, created_at) VALUES(?,?,?)",
            (student_id, name, now_iso()),
        )

    def get_student(self, student_id: str) -> dict | None:
        row = self._row(self.connection,
                        "SELECT * FROM student WHERE student_id=?",
                        (student_id,))
        return dict(row) if row else None

    def set_consent(self, conn: sqlite3.Connection, student_id: str,
                    granted: bool, at: str) -> None:
        existing = self._row(conn,
                             "SELECT granted FROM consent WHERE student_id=?",
                             (student_id,))
        if existing is None:
            conn.execute(
                "INSERT INTO consent(student_id, granted, granted_at,"
                " revoked_at, updated_at) VALUES(?,?,?,?,?)",
                (student_id, int(granted), at if granted else None,
                 None if granted else at, at),
            )
        else:
            conn.execute(
                "UPDATE consent SET granted=?, granted_at=CASE WHEN ?=1"
                " THEN ? ELSE granted_at END, revoked_at=CASE WHEN ?=0"
                " THEN ? ELSE revoked_at END, updated_at=?"
                " WHERE student_id=?",
                (int(granted), int(granted), at, int(granted), at, at,
                 student_id),
            )

    def consent_granted(self, conn: sqlite3.Connection,
                        student_id: str) -> bool:
        row = self._row(conn, "SELECT granted FROM consent WHERE student_id=?",
                        (student_id,))
        return bool(row and row["granted"])

    # -- 课次 ---------------------------------------------------------------

    def insert_session(self, conn: sqlite3.Connection, session: dict) -> None:
        conn.execute(
            "INSERT INTO session(session_id, stage, scheduled_start,"
            " scheduled_end, capacity, wheel_count, status,"
            " makeup_for_session, note, created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (session["session_id"], session["stage"],
             session["scheduled_start"], session["scheduled_end"],
             session["capacity"], session.get("wheel_count", 0),
             session.get("status", "open"),
             session.get("makeup_for_session"),
             session.get("note", ""), session["created_by"], now_iso()),
        )

    def get_session(self, session_id: str) -> dict | None:
        row = self._row(self.connection,
                        "SELECT * FROM session WHERE session_id=?",
                        (session_id,))
        return dict(row) if row else None

    def list_sessions(self, *, stage: str | None = None,
                      status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM session WHERE 1=1"
        params: list[Any] = []
        if stage:
            sql += " AND stage=?"
            params.append(stage)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY scheduled_start"
        return [dict(r) for r in self._rows(self.connection, sql, tuple(params))]

    def set_session_status(self, conn: sqlite3.Connection, session_id: str,
                           status: str) -> None:
        conn.execute("UPDATE session SET status=? WHERE session_id=?",
                     (status, session_id))

    def assign_teacher(self, conn: sqlite3.Connection, session_id: str,
                       teacher_id: str, stage: str, at: str) -> None:
        conn.execute(
            "INSERT INTO session_teacher(session_id, teacher_id, stage,"
            " created_at) VALUES(?,?,?,?)",
            (session_id, teacher_id, stage, at),
        )

    def teacher_assigned(self, conn: sqlite3.Connection, session_id: str,
                         teacher_id: str) -> bool:
        return self._row(
            conn,
            "SELECT 1 FROM session_teacher WHERE session_id=? AND teacher_id=?",
            (session_id, teacher_id),
        ) is not None

    def list_session_teachers(self, session_id: str) -> list[dict]:
        return [dict(r) for r in self._rows(
            self.connection,
            "SELECT st.teacher_id, t.name, st.stage FROM session_teacher st"
            " JOIN teacher t ON t.teacher_id=st.teacher_id"
            " WHERE st.session_id=? ORDER BY st.teacher_id",
            (session_id,))]

    def list_teacher_sessions(self, teacher_id: str) -> list[dict]:
        return [dict(r) for r in self._rows(
            self.connection,
            "SELECT s.* FROM session s JOIN session_teacher st"
            " ON st.session_id=s.session_id WHERE st.teacher_id=?",
            (teacher_id,))]

    # -- 报名 / 候补 --------------------------------------------------------

    def get_enrollment(self, conn: sqlite3.Connection, session_id: str,
                       student_id: str) -> dict | None:
        row = self._row(
            conn, "SELECT * FROM enrollment WHERE session_id=? AND student_id=?",
            (session_id, student_id))
        return dict(row) if row else None

    def insert_enrollment(self, conn: sqlite3.Connection, session_id: str,
                          student_id: str, status: str, at: str,
                          waitlist_position: int | None = None) -> None:
        conn.execute(
            "INSERT INTO enrollment(session_id, student_id, status,"
            " waitlist_position, enrolled_at, waitlisted_at, created_at,"
            " updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, student_id, status, waitlist_position,
             at if status == "enrolled" else None,
             at if status == "waitlisted" else None, at, at),
        )

    def reactivate_enrollment(self, conn: sqlite3.Connection, session_id: str,
                              student_id: str, status: str, at: str,
                              waitlist_position: int | None = None) -> None:
        """曾经撤回的报名重新占位（退课后再报名）。"""
        conn.execute(
            "UPDATE enrollment SET status=?, waitlist_position=?,"
            " enrolled_at=?, waitlisted_at=?, withdrawn_at=NULL,"
            " withdraw_reason='', updated_at=?"
            " WHERE session_id=? AND student_id=?",
            (status, waitlist_position, at if status == "enrolled" else None,
             at if status == "waitlisted" else None, at,
             session_id, student_id),
        )

    def count_enrolled(self, conn: sqlite3.Connection, session_id: str) -> int:
        row = self._row(
            conn,
            "SELECT COUNT(*) AS n FROM enrollment WHERE session_id=?"
            " AND status='enrolled'",
            (session_id,))
        return int(row["n"])

    def next_waitlist_position(self, conn: sqlite3.Connection,
                               session_id: str) -> int:
        row = self._row(
            conn,
            "SELECT COALESCE(MAX(waitlist_position), 0) AS m FROM enrollment"
            " WHERE session_id=?",
            (session_id,))
        return int(row["m"]) + 1

    def pop_waitlist_head(self, conn: sqlite3.Connection,
                          session_id: str) -> dict | None:
        """取候补队首且仍处于候补状态的报名（不修改状态）。"""
        row = self._row(
            conn,
            "SELECT * FROM enrollment WHERE session_id=? AND status='waitlisted'"
            " ORDER BY waitlist_position LIMIT 1",
            (session_id,))
        return dict(row) if row else None

    def promote_enrollment(self, conn: sqlite3.Connection, session_id: str,
                           student_id: str, at: str) -> None:
        conn.execute(
            "UPDATE enrollment SET status='enrolled', enrolled_at=?,"
            " updated_at=? WHERE session_id=? AND student_id=?",
            (at, at, session_id, student_id))

    def withdraw_enrollment(self, conn: sqlite3.Connection, session_id: str,
                            student_id: str, reason: str, at: str) -> None:
        conn.execute(
            "UPDATE enrollment SET status='withdrawn', withdrawn_at=?,"
            " withdraw_reason=?, updated_at=? WHERE session_id=? AND student_id=?",
            (at, reason, at, session_id, student_id))

    def list_enrollments(self, session_id: str,
                         status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM enrollment WHERE session_id=?"
        params: list[Any] = [session_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY COALESCE(waitlist_position, 0), student_id"
        return [dict(r) for r in self._rows(self.connection, sql,
                                            tuple(params))]

    def list_student_enrollments(self, student_id: str,
                                 status: str | None = None) -> list[dict]:
        sql = ("SELECT e.*, s.stage, s.scheduled_start, s.scheduled_end,"
               " s.status AS session_status FROM enrollment e"
               " JOIN session s ON s.session_id=e.session_id"
               " WHERE e.student_id=?")
        params: list[Any] = [student_id]
        if status:
            sql += " AND e.status=?"
            params.append(status)
        sql += " ORDER BY s.scheduled_start"
        return [dict(r) for r in self._rows(self.connection, sql,
                                            tuple(params))]

    # -- 签到 / 请假 --------------------------------------------------------

    def mark_attendance(self, conn: sqlite3.Connection, session_id: str,
                        student_id: str, status: str, marked_by: str,
                        at: str) -> None:
        conn.execute(
            "INSERT INTO attendance(session_id, student_id, status,"
            " marked_by, marked_at) VALUES(?,?,?,?,?)",
            (session_id, student_id, status, marked_by, at))

    def get_attendance(self, session_id: str, student_id: str) -> dict | None:
        row = self._row(
            self.connection,
            "SELECT * FROM attendance WHERE session_id=? AND student_id=?",
            (session_id, student_id))
        return dict(row) if row else None

    # -- 补课额度 -----------------------------------------------------------

    def grant_makeup(self, conn: sqlite3.Connection, credit: dict) -> None:
        conn.execute(
            "INSERT INTO makeup_credit(credit_id, student_id, stage,"
            " source_session_id, state, reason, created_at)"
            " VALUES(?,?,?,?,'granted',?,?)",
            (credit["credit_id"], credit["student_id"], credit["stage"],
             credit["source_session_id"], credit.get("reason", ""),
             credit["created_at"]))

    def get_makeup_credit(self, credit_id: str) -> dict | None:
        row = self._row(self.connection,
                        "SELECT * FROM makeup_credit WHERE credit_id=?",
                        (credit_id,))
        return dict(row) if row else None

    def find_granted_makeup(self, conn: sqlite3.Connection, student_id: str,
                            stage: str) -> dict | None:
        row = self._row(
            conn,
            "SELECT * FROM makeup_credit WHERE student_id=? AND stage=?"
            " AND state='granted' ORDER BY created_at LIMIT 1",
            (student_id, stage))
        return dict(row) if row else None

    def use_makeup_credit(self, conn: sqlite3.Connection, credit_id: str,
                          target_session_id: str, at: str) -> None:
        conn.execute(
            "UPDATE makeup_credit SET state='used', target_session_id=?,"
            " used_at=? WHERE credit_id=? AND state='granted'",
            (target_session_id, at, credit_id))

    def list_makeup_credits(self, student_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM makeup_credit"
        params: tuple = ()
        if student_id:
            sql += " WHERE student_id=?"
            params = (student_id,)
        sql += " ORDER BY created_at"
        return [dict(r) for r in self._rows(self.connection, sql, params)]

    # -- 作品与责任链 -------------------------------------------------------

    def insert_artifact(self, conn: sqlite3.Connection, artifact: dict,
                        at: str) -> None:
        conn.execute(
            "INSERT INTO artifact(artifact_id, owner_student_id,"
            " origin_session_id, name, state, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (artifact["artifact_id"], artifact["owner_student_id"],
             artifact["origin_session_id"], artifact.get("name", ""),
             artifact["state"], at, at))

    def find_artifact(self, artifact_id: str) -> dict | None:
        row = self._row(self.connection,
                        "SELECT * FROM artifact WHERE artifact_id=?",
                        (artifact_id,))
        return dict(row) if row else None

    def get_artifact_row(self, conn: sqlite3.Connection,
                         artifact_id: str) -> dict | None:
        row = self._row(conn, "SELECT * FROM artifact WHERE artifact_id=?",
                        (artifact_id,))
        return dict(row) if row else None

    def update_artifact_state(self, conn: sqlite3.Connection, artifact_id: str,
                              state: str, at: str) -> None:
        conn.execute(
            "UPDATE artifact SET state=?, updated_at=? WHERE artifact_id=?",
            (state, at, artifact_id))

    def next_custody_seq(self, conn: sqlite3.Connection,
                         artifact_id: str) -> int:
        row = self._row(
            conn,
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq"
            " FROM artifact_custody WHERE artifact_id=?",
            (artifact_id,))
        return int(row["next_seq"])

    def append_custody(self, conn: sqlite3.Connection, artifact_id: str,
                       seq: int, action: str, from_state: str | None,
                       to_state: str, custodian_id: str, custodian_role: str,
                       note: str, at: str) -> None:
        conn.execute(
            "INSERT INTO artifact_custody(artifact_id, seq, action,"
            " from_state, to_state, custodian_id, custodian_role, note, at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (artifact_id, seq, action, from_state, to_state, custodian_id,
             custodian_role, note, at))

    def list_custody(self, artifact_id: str) -> list[dict]:
        return [dict(r) for r in self._rows(
            self.connection,
            "SELECT * FROM artifact_custody WHERE artifact_id=?"
            " ORDER BY seq", (artifact_id,))]

    def list_artifacts(self, *, owner_student_id: str | None = None,
                       session_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM artifact WHERE 1=1"
        params: list[Any] = []
        if owner_student_id:
            sql += " AND owner_student_id=?"
            params.append(owner_student_id)
        if session_id:
            sql += " AND origin_session_id=?"
            params.append(session_id)
        sql += " ORDER BY created_at, artifact_id"
        return [dict(r) for r in self._rows(self.connection, sql,
                                            tuple(params))]

    # -- 窑炉与批次 ---------------------------------------------------------

    def create_kiln(self, conn: sqlite3.Connection, kiln_id: str,
                    name: str) -> None:
        conn.execute(
            "INSERT INTO kiln(kiln_id, name, state) VALUES(?,?,'open')",
            (kiln_id, name))

    def get_kiln(self, conn: sqlite3.Connection, kiln_id: str) -> dict | None:
        row = self._row(conn, "SELECT * FROM kiln WHERE kiln_id=?",
                        (kiln_id,))
        return dict(row) if row else None

    def update_kiln_state(self, conn: sqlite3.Connection, kiln_id: str,
                          state: str, at: str) -> None:
        if state == "halted":
            conn.execute(
                "UPDATE kiln SET state='halted', halted_at=? WHERE kiln_id=?",
                (at, kiln_id))
        else:
            conn.execute(
                "UPDATE kiln SET state='open', resumed_at=? WHERE kiln_id=?",
                (at, kiln_id))

    def insert_batch(self, conn: sqlite3.Connection, batch_id: str,
                     kiln_id: str, at: str) -> None:
        conn.execute(
            "INSERT INTO firing_batch(batch_id, kiln_id, state, loaded_at)"
            " VALUES(?,?,'loaded',?)",
            (batch_id, kiln_id, at))

    def get_batch(self, conn: sqlite3.Connection, batch_id: str) -> dict | None:
        row = self._row(conn, "SELECT * FROM firing_batch WHERE batch_id=?",
                        (batch_id,))
        return dict(row) if row else None

    def update_batch_state(self, conn: sqlite3.Connection, batch_id: str,
                           state: str, at: str) -> None:
        if state == "firing":
            conn.execute(
                "UPDATE firing_batch SET state='firing', started_at=?"
                " WHERE batch_id=?", (at, batch_id))
        elif state == "completed":
            conn.execute(
                "UPDATE firing_batch SET state='completed', completed_at=?"
                " WHERE batch_id=?", (at, batch_id))
        else:
            conn.execute("UPDATE firing_batch SET state=? WHERE batch_id=?",
                         (state, batch_id))

    def artifact_in_open_batch(self, conn: sqlite3.Connection,
                               artifact_id: str) -> dict | None:
        row = self._row(
            conn,
            "SELECT fb.* FROM batch_artifact ba JOIN firing_batch fb"
            " ON fb.batch_id=ba.batch_id WHERE ba.artifact_id=?"
            " AND fb.state != 'completed'",
            (artifact_id,))
        return dict(row) if row else None

    def add_artifact_to_batch(self, conn: sqlite3.Connection, batch_id: str,
                              artifact_id: str, at: str) -> None:
        conn.execute(
            "INSERT INTO batch_artifact(batch_id, artifact_id, loaded_at)"
            " VALUES(?,?,?)", (batch_id, artifact_id, at))

    def list_batch_artifacts(self, batch_id: str) -> list[str]:
        return [r["artifact_id"] for r in self._rows(
            self.connection,
            "SELECT artifact_id FROM batch_artifact WHERE batch_id=?"
            " ORDER BY artifact_id", (batch_id,))]

    # -- 审计 ---------------------------------------------------------------

    def audit(self, conn: sqlite3.Connection, actor_id: str, action: str,
              *, entity_type: str = "", entity_id: str = "",
              session_id: str = "", detail: dict | None = None,
              at: str | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_log(at, actor_id, action, entity_type,"
            " entity_id, session_id, detail) VALUES(?,?,?,?,?,?,?)",
            (at or now_iso(), actor_id, action, entity_type, entity_id,
             session_id, json.dumps(detail or {}, ensure_ascii=False,
                                    sort_keys=True)))

    def audit_now(self, actor_id: str, action: str, *,
                  entity_type: str = "", entity_id: str = "",
                  session_id: str = "", detail: dict | None = None) -> None:
        """独立短事务写审计。

        用于业务事务必须回滚（如重复签到、停窑装窑被拒）但审计不能丢失
        的场景。
        """
        with self.transaction() as conn:
            self.audit(conn, actor_id, action, entity_type=entity_type,
                       entity_id=entity_id, session_id=session_id,
                       detail=detail)

    def list_audit(self, *, entity_id: str | None = None,
                   session_id: str | None = None, action: str | None = None,
                   limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if entity_id:
            sql += " AND entity_id=?"
            params.append(entity_id)
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        if action:
            sql += " AND action=?"
            params.append(action)
        sql += " ORDER BY audit_id LIMIT ?"
        params.append(limit)
        result = []
        for row in self._rows(self.connection, sql, tuple(params)):
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def close(self) -> None:
        with self._lock:
            self.connection.close()
