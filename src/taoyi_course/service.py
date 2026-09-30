"""课程编排与作品流转的应用服务。

一个 ``Service`` 实例对应一个 :class:`~taoyi_course.store.Store`。
所有写操作都在 ``BEGIN IMMEDIATE`` 事务内完成“校验 + 落库 + 审计”，
任一环节失败整体回滚；调用方拿到的结果要么全部生效，要么完全不变。
"""
import functools
import json
import sqlite3
from datetime import datetime

from . import domain as d
from .domain import (
    ABSENT, ART_CLAY_ISSUED, ART_LOADED, BATCH_FIRED, BATCH_FIRING,
    BATCH_HELD, BATCH_LOADED, BATCH_READY, ENROLLED, KILN_ACTIVE, KILN_PAUSED,
    MOVED, ROLE_KILN_KEEPER, ROLE_STAFF, ROLE_STUDENT, ROLE_TEACHER,
    SESSION_CANCELLED, SESSION_COMPLETED, SESSION_IN_PROGRESS,
    SESSION_SCHEDULED, STAGES, STAGE_ART_STATE, STAGE_ORDER, WAITLISTED,
    WHEEL_STAGES, WITHDRAWN, ATTENDED,
)
from .store import Store
from .domain import Record


# 报名后仍占席的状态
SEAT_STATUSES = (ENROLLED, ATTENDED)
# 还可能参与课次的活跃状态
ACTIVE_ENROLLMENT = (ENROLLED, WAITLISTED, ATTENDED)
# 未结束、需要参与冲突判定的课次
OPEN_SESSION_STATUSES = (SESSION_SCHEDULED, SESSION_IN_PROGRESS)


def _conflict(func):
    """把唯一索引等完整性冲突翻译成业务 ConflictError。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sqlite3.IntegrityError as exc:
            raise d.ConflictError(f"数据冲突，操作已回滚：{exc}") from exc

    return wrapper


def _parse_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise d.DomainError(f"时间格式应为 ISO-8601：{value!r}") from exc


class Service:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # ======================================================================
    # 基础 / 兼容旧骨架
    # ======================================================================

    def health(self) -> dict[str, str]:
        return {"service": "taoyi_course", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str]:
        record = self.store.save(Record(record_id, owner_id))
        return {"record_id": record.record_id, "owner_id": record.owner_id,
                "state": record.state, "created_at": record.created_at}

    def find(self, record_id: str) -> dict[str, str] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    # ======================================================================
    # 内部小工具
    # ======================================================================

    def _audit(self, conn, *, action, entity_type, entity_id,
               detail=None, actor_id=None, actor_role=None) -> None:
        conn.execute(
            "INSERT INTO audit_log(at, actor_id, actor_role, action, "
            "entity_type, entity_id, detail) VALUES(?,?,?,?,?,?,?)",
            (d.now_iso(), actor_id, actor_role, action, entity_type,
             str(entity_id), json.dumps(detail or {}, ensure_ascii=False)),
        )

    def _must_get(self, table: str, id_col: str, entity_id: str,
                  label: str) -> sqlite3.Row:
        row = self.store.one(
            f"SELECT * FROM {table} WHERE {id_col}=?", (entity_id,))
        if row is None:
            raise d.NotFoundError(f"{label}不存在：{entity_id}")
        return row

    def _require_staff(self, actor_role: str | None) -> None:
        if actor_role != ROLE_STAFF:
            raise d.PermissionDeniedError("只有教务员可以执行该操作")

    def _effective_capacity(self, conn, session_row) -> int:
        """安全容量：人数与陶轮数取较小值（拉坯/修坯人手一轮）。"""
        cap = session_row["capacity"]
        if session_row["stage"] in WHEEL_STAGES:
            wheels = session_row["wheels"] or 0
            cap = min(cap, wheels)
        return cap

    def _seat_count(self, conn, session_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM enrollment "
            "WHERE session_id=? AND status IN (?,?)",
            (session_id, ENROLLED, ATTENDED)).fetchone()
        return int(row["n"])

    def _teacher_overlaps(self, conn, teacher_id: str,
                          starts_at: str, ends_at: str,
                          exclude_session: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT s.session_id, s.starts_at, s.ends_at FROM session s "
            "JOIN session_teacher st ON st.session_id=s.session_id "
            "WHERE st.teacher_id=? AND s.status IN (?,?) "
            "AND s.starts_at < ? AND s.ends_at > ?")
        params: list = [teacher_id, SESSION_SCHEDULED, SESSION_IN_PROGRESS,
                        ends_at, starts_at]
        if exclude_session:
            sql += " AND s.session_id != ?"
            params.append(exclude_session)
        return conn.execute(sql, params).fetchall()

    def _student_overlaps(self, conn, student_id: str,
                          starts_at: str, ends_at: str,
                          exclude_session: str | None = None) -> bool:
        sql = (
            "SELECT 1 FROM enrollment e JOIN session s ON s.session_id=e.session_id "
            "WHERE e.student_id=? AND e.status IN (?,?) "
            "AND s.status IN (?,?) "
            "AND s.starts_at < ? AND s.ends_at > ?")
        params: list = [student_id, ENROLLED, ATTENDED,
                        SESSION_SCHEDULED, SESSION_IN_PROGRESS,
                        ends_at, starts_at]
        if exclude_session:
            sql += " AND s.session_id != ?"
            params.append(exclude_session)
        sql += " LIMIT 1"
        return conn.execute(sql, params).fetchone() is not None

    def _promote_waitlist(self, conn, session_id: str) -> str | None:
        """席位释放后按候补顺位补位；返回补位学生 ID。"""
        candidates = conn.execute(
            "SELECT e.enrollment_id, e.student_id, e.makeup_grant_id "
            "FROM enrollment e JOIN student st ON st.student_id=e.student_id "
            "WHERE e.session_id=? AND e.status=? AND st.consent=1 "
            "ORDER BY e.waitlist_position ASC",
            (session_id, WAITLISTED)).fetchall()
        for cand in candidates:
            conn.execute(
                "UPDATE enrollment SET status=?, updated_at=? "
                "WHERE enrollment_id=? AND status=?",
                (ENROLLED, d.now_iso(), cand["enrollment_id"], WAITLISTED))
            grant_id = cand["makeup_grant_id"]
            if grant_id is not None:
                # 候补时登记的补课资格在真正占席这一刻预留。
                grow = conn.execute(
                    "SELECT status FROM makeup_grant WHERE grant_id=?",
                    (grant_id,)).fetchone()
                if grow["status"] == "open":
                    conn.execute(
                        "UPDATE makeup_grant SET status='reserved', "
                        "used_session_id=?, used_enrollment_id=? "
                        "WHERE grant_id=?",
                        (session_id, cand["enrollment_id"], grant_id))
            self._audit(
                conn, action="enrollment.promoted",
                entity_type="enrollment", entity_id=cand["enrollment_id"],
                detail={"session_id": session_id,
                        "student_id": cand["student_id"],
                        "grant_id": grant_id,
                        "reason": "顺位补位"})
            return cand["student_id"]
        return None

    def _session_dict(self, row: sqlite3.Row) -> dict:
        return {
            "session_id": row["session_id"], "stage": row["stage"],
            "title": row["title"], "starts_at": row["starts_at"],
            "ends_at": row["ends_at"], "capacity": row["capacity"],
            "wheels": row["wheels"], "status": row["status"],
            "created_by": row["created_by"], "created_at": row["created_at"],
        }

    # ======================================================================
    # 人员建档
    # ======================================================================

    @_conflict
    def register_teacher(self, teacher_id: str, name: str, stages: list[str],
                         actor_id: str | None = None,
                         actor_role: str | None = ROLE_STAFF) -> dict:
        self._require_staff(actor_role)
        invalid = [s for s in stages if s not in STAGES]
        if invalid:
            raise d.DomainError(f"未知的教学阶段：{invalid}")
        if not stages:
            raise d.DomainError("教师至少需要一个阶段的资质")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO teacher(teacher_id, name, stages, created_at) "
                "VALUES(?,?,?,?)",
                (teacher_id, name, json.dumps(stages, ensure_ascii=False),
                 d.now_iso()))
            self._audit(conn, action="teacher.registered",
                        entity_type="teacher", entity_id=teacher_id,
                        detail={"name": name, "stages": stages},
                        actor_id=actor_id, actor_role=actor_role)
        return {"teacher_id": teacher_id, "name": name, "stages": list(stages)}

    @_conflict
    def register_student(self, student_id: str, name: str,
                         guardian_id: str | None = None,
                         consent: bool = False,
                         actor_id: str | None = None,
                         actor_role: str | None = ROLE_STAFF) -> dict:
        self._require_staff(actor_role)
        ts = d.now_iso()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO student(student_id, name, guardian_id, consent, "
                "consent_updated_at, created_at) VALUES(?,?,?,?,?,?)",
                (student_id, name, guardian_id, 1 if consent else 0,
                 ts if consent else None, ts))
            self._audit(conn, action="student.registered",
                        entity_type="student", entity_id=student_id,
                        detail={"name": name, "guardian_id": guardian_id,
                                "consent": bool(consent)},
                        actor_id=actor_id, actor_role=actor_role)
        return {"student_id": student_id, "name": name,
                "guardian_id": guardian_id, "consent": bool(consent)}

    def grant_consent(self, student_id: str, actor_id: str | None = None,
                      actor_role: str | None = ROLE_STAFF) -> dict:
        self._must_get("student", "student_id", student_id, "学生")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE student SET consent=1, consent_updated_at=? "
                "WHERE student_id=?", (d.now_iso(), student_id))
            self._audit(conn, action="consent.granted",
                        entity_type="student", entity_id=student_id,
                        actor_id=actor_id, actor_role=actor_role)
        return {"student_id": student_id, "consent": True}

    # ======================================================================
    # 课次发布（分阶段）
    # ======================================================================

    @_conflict
    def publish_session(self, session_id: str, stage: str, title: str,
                        starts_at: str, ends_at: str, capacity: int,
                        wheels: int | None, teacher_ids: list[str],
                        actor_id: str | None = None,
                        actor_role: str | None = ROLE_STAFF) -> dict:
        """教务员发布一个阶段课次，并一次性完成安全容量与教师校验。"""
        self._require_staff(actor_role)
        if stage not in STAGES:
            raise d.DomainError(f"未知阶段：{stage}")
        start, end = _parse_iso(starts_at), _parse_iso(ends_at)
        if end <= start:
            raise d.DomainError("下课时间必须晚于上课时间")
        if not isinstance(capacity, int) or capacity <= 0:
            raise d.DomainError("容量必须是正整数")
        if stage in WHEEL_STAGES and not (isinstance(wheels, int) and wheels > 0):
            raise d.DomainError("拉坯/修坯课次必须配备至少一台陶轮")
        if not teacher_ids:
            raise d.DomainError("课次至少安排一名持证指导教师")

        ts = d.now_iso()
        with self.store.transaction() as conn:
            # 教师存在、资质匹配、时间无冲突，任一不满足整次发布回滚。
            for tid in teacher_ids:
                trow = conn.execute(
                    "SELECT * FROM teacher WHERE teacher_id=?",
                    (tid,)).fetchone()
                if trow is None:
                    raise d.NotFoundError(f"教师不存在：{tid}")
                t_stages = json.loads(trow["stages"])
                if stage not in t_stages:
                    raise d.ConflictError(
                        f"教师 {tid} 不具备 {stage} 阶段资质，发布回滚")
                clashes = self._teacher_overlaps(
                    conn, tid, starts_at, ends_at)
                if clashes:
                    c = clashes[0]
                    raise d.ConflictError(
                        f"教师 {tid} 在 {c['starts_at']}~{c['ends_at']} "
                        f"已有课次 {c['session_id']}，时间冲突，发布回滚")

            conn.execute(
                "INSERT INTO session(session_id, stage, title, starts_at, "
                "ends_at, capacity, wheels, status, created_by, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (session_id, stage, title, starts_at, ends_at, capacity,
                 wheels, SESSION_SCHEDULED, actor_id, ts))
            for tid in teacher_ids:
                conn.execute(
                    "INSERT INTO session_teacher(session_id, teacher_id, "
                    "role, assigned_at) VALUES(?,?,?,?)",
                    (session_id, tid, "lead", ts))
            self._audit(conn, action="session.published",
                        entity_type="session", entity_id=session_id,
                        detail={"stage": stage, "title": title,
                                "starts_at": starts_at, "ends_at": ends_at,
                                "capacity": capacity, "wheels": wheels,
                                "teachers": list(teacher_ids)},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict:
        row = self._must_get("session", "session_id", session_id, "课次")
        result = self._session_dict(row)
        teachers = self.store.all(
            "SELECT teacher_id FROM session_teacher WHERE session_id=? "
            "ORDER BY teacher_id", (session_id,))
        result["teachers"] = [r["teacher_id"] for r in teachers]
        return result

    def list_sessions(self, stage: str | None = None) -> list[dict]:
        if stage:
            rows = self.store.all(
                "SELECT * FROM session WHERE stage=? ORDER BY starts_at",
                (stage,))
        else:
            rows = self.store.all(
                "SELECT * FROM session ORDER BY starts_at")
        return [self._session_dict(r) for r in rows]

    @_conflict
    def reassign_teacher(self, session_id: str, remove_id: str, add_id: str,
                         actor_id: str | None = None,
                         actor_role: str | None = ROLE_STAFF) -> dict:
        """处理突发教师冲突：换下冲突教师；新课程仍有资质/时间冲突则回滚。"""
        self._require_staff(actor_role)
        with self.store.transaction() as conn:
            srow = conn.execute(
                "SELECT * FROM session WHERE session_id=?",
                (session_id,)).fetchone()
            if srow is None:
                raise d.NotFoundError(f"课次不存在：{session_id}")
            if srow["status"] not in OPEN_SESSION_STATUSES:
                raise d.ConflictError("课次已结束或取消，不能调整教师")
            if not conn.execute(
                    "SELECT 1 FROM session_teacher WHERE session_id=? "
                    "AND teacher_id=?", (session_id, remove_id)).fetchone():
                raise d.NotFoundError(f"课次中没有教师 {remove_id}")
            trow = conn.execute(
                "SELECT * FROM teacher WHERE teacher_id=?", (add_id,)).fetchone()
            if trow is None:
                raise d.NotFoundError(f"教师不存在：{add_id}")
            if srow["stage"] not in json.loads(trow["stages"]):
                raise d.ConflictError(
                    f"接替教师 {add_id} 无 {srow['stage']} 资质，调整回滚")
            clashes = self._teacher_overlaps(
                conn, add_id, srow["starts_at"], srow["ends_at"],
                exclude_session=session_id)
            if clashes:
                raise d.ConflictError(
                    f"接替教师 {add_id} 时间冲突，调整回滚")
            conn.execute(
                "DELETE FROM session_teacher WHERE session_id=? AND teacher_id=?",
                (session_id, remove_id))
            conn.execute(
                "INSERT INTO session_teacher(session_id, teacher_id, role, "
                "assigned_at) VALUES(?,?,?,?)",
                (session_id, add_id, "lead", d.now_iso()))
            self._audit(conn, action="teacher.reassigned",
                        entity_type="session", entity_id=session_id,
                        detail={"removed": remove_id, "added": add_id},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_session(session_id)

    def start_session(self, session_id: str, actor_id: str | None = None,
                      actor_role: str | None = ROLE_STAFF) -> dict:
        self._require_staff(actor_role)
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM session WHERE session_id=?",
                (session_id,)).fetchone()
            if row is None:
                raise d.NotFoundError(f"课次不存在：{session_id}")
            if row["status"] != SESSION_SCHEDULED:
                raise d.ConflictError(
                    f"课次状态为 {row['status']}，不能开始")
            conn.execute(
                "UPDATE session SET status=? WHERE session_id=?",
                (SESSION_IN_PROGRESS, session_id))
            self._audit(conn, action="session.started",
                        entity_type="session", entity_id=session_id,
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_session(session_id)

    def complete_session(self, session_id: str, actor_id: str | None = None,
                         actor_role: str | None = ROLE_STAFF) -> dict:
        """结课：结束后不再参与时间冲突与报名。"""
        self._require_staff(actor_role)
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM session WHERE session_id=?",
                (session_id,)).fetchone()
            if row is None:
                raise d.NotFoundError(f"课次不存在：{session_id}")
            if row["status"] != SESSION_IN_PROGRESS:
                raise d.ConflictError(
                    f"课次状态为 {row['status']}，不能结课")
            conn.execute(
                "UPDATE session SET status=? WHERE session_id=?",
                (SESSION_COMPLETED, session_id))
            self._audit(conn, action="session.completed",
                        entity_type="session", entity_id=session_id,
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_session(session_id)

    # ======================================================================
    # 报名 / 候补 / 签到 / 请假 / 撤回同意
    # ======================================================================

    @_conflict
    def enroll(self, session_id: str, student_id: str,
               grant_id: int | None = None,
               actor_id: str | None = None,
               actor_role: str | None = ROLE_STAFF) -> dict:
        with self.store.transaction() as conn:
            srow = conn.execute(
                "SELECT * FROM session WHERE session_id=?",
                (session_id,)).fetchone()
            if srow is None:
                raise d.NotFoundError(f"课次不存在：{session_id}")
            if srow["status"] not in OPEN_SESSION_STATUSES:
                raise d.ConflictError(
                    f"课次已 {srow['status']}，不再接受报名")
            student = conn.execute(
                "SELECT * FROM student WHERE student_id=?",
                (student_id,)).fetchone()
            if student is None:
                raise d.NotFoundError(f"学生不存在：{student_id}")
            if not student["consent"]:
                raise d.ConsentRequiredError(
                    f"学生 {student_id} 缺少有效监护同意，不能占席")
            if conn.execute(
                    "SELECT 1 FROM enrollment WHERE session_id=? AND student_id=? "
                    "AND status IN (?,?,?)",
                    (session_id, student_id, *ACTIVE_ENROLLMENT)).fetchone():
                raise d.ConflictError("学生已在该课次名单/候补队列中")
            if self._student_overlaps(conn, student_id,
                                      srow["starts_at"], srow["ends_at"],
                                      exclude_session=session_id):
                raise d.ConflictError("学生在同一时段已有其他课次")

            reserved_grant = None
            if grant_id is not None:
                grow = conn.execute(
                    "SELECT * FROM makeup_grant WHERE grant_id=?",
                    (grant_id,)).fetchone()
                if grow is None:
                    raise d.NotFoundError(f"补课资格不存在：{grant_id}")
                if grow["student_id"] != student_id:
                    raise d.PermissionDeniedError("补课资格不属于该学生")
                if grow["status"] != "open":
                    raise d.ConflictError("补课资格已使用或已失效")
                if grow["stage"] != srow["stage"]:
                    raise d.ConflictError(
                        f"补课资格阶段为 {grow['stage']}，"
                        f"与课次 {srow['stage']} 不一致")
                dup = conn.execute(
                    "SELECT 1 FROM enrollment e JOIN session s "
                    "ON s.session_id=e.session_id "
                    "WHERE e.makeup_grant_id=? AND e.student_id=? "
                    "AND s.status IN (?,?) AND e.status IN (?,?,?) LIMIT 1",
                    (grant_id, student_id, SESSION_SCHEDULED,
                     SESSION_IN_PROGRESS, *ACTIVE_ENROLLMENT)).fetchone()
                if dup:
                    raise d.ConflictError("该补课资格已关联到其他进行中的课次")

            seats = self._seat_count(conn, session_id)
            cap = self._effective_capacity(conn, srow)
            ts = d.now_iso()
            if seats < cap:
                status = ENROLLED
                position = None
                if grant_id is not None:
                    # 只有真正占席才预留补课资格，候补期间资格保持可用。
                    conn.execute(
                        "UPDATE makeup_grant SET status='reserved', "
                        "used_session_id=?, used_enrollment_id=NULL "
                        "WHERE grant_id=? AND status='open'",
                        (session_id, grant_id))
                    reserved_grant = grant_id
                    self._audit(conn, action="makeup.reserved",
                                entity_type="makeup_grant", entity_id=grant_id,
                                detail={"session_id": session_id},
                                actor_id=actor_id, actor_role=actor_role)
            else:
                status = WAITLISTED
                position = (conn.execute(
                    "SELECT COALESCE(MAX(waitlist_position),0)+1 AS p "
                    "FROM enrollment WHERE session_id=? AND status=?",
                    (session_id, WAITLISTED)).fetchone()["p"])

            # 候补时也保留 grant_id，补位成功后再预留（见 _promote_waitlist）。
            cur = conn.execute(
                "INSERT INTO enrollment(session_id, student_id, status, "
                "waitlist_position, created_at, updated_at, makeup_grant_id, "
                "origin_session_id) VALUES(?,?,?,?,?,?,?,?)",
                (session_id, student_id, status, position, ts, ts,
                 grant_id, None))
            enrollment_id = int(cur.lastrowid)
            if reserved_grant is not None:
                conn.execute(
                    "UPDATE makeup_grant SET used_enrollment_id=? "
                    "WHERE grant_id=?", (enrollment_id, reserved_grant))

            self._audit(conn, action=f"enrollment.{status}",
                        entity_type="enrollment", entity_id=enrollment_id,
                        detail={"session_id": session_id,
                                "student_id": student_id,
                                "waitlist_position": position,
                                "grant_id": grant_id},
                        actor_id=actor_id, actor_role=actor_role)

            return {"enrollment_id": enrollment_id, "session_id": session_id,
                    "student_id": student_id, "status": status,
                    "waitlist_position": position,
                    "effective_capacity": cap,
                    "grant_id": reserved_grant}

    @_conflict
    def check_in(self, session_id: str, student_id: str,
                 actor_id: str | None = None,
                 actor_role: str | None = ROLE_STAFF) -> dict:
        with self.store.transaction() as conn:
            erow = conn.execute(
                "SELECT * FROM enrollment WHERE session_id=? AND student_id=? "
                "ORDER BY enrollment_id DESC LIMIT 1",
                (session_id, student_id)).fetchone()
            if erow is None:
                raise d.NotFoundError("找不到该学生在本课次的报名记录")
            if erow["status"] == ATTENDED:
                raise d.ConflictError(
                    f"学生 {student_id} 已在 {erow['check_in_at']} 签到，"
                    "禁止重复签到")
            if erow["status"] == WAITLISTED:
                raise d.ConflictError("候补尚未补位，不能签到")
            if erow["status"] != ENROLLED:
                raise d.ConflictError(
                    f"报名状态为 {erow['status']}，不能签到")
            srow = conn.execute(
                "SELECT status FROM session WHERE session_id=?",
                (session_id,)).fetchone()
            if srow["status"] not in OPEN_SESSION_STATUSES:
                raise d.ConflictError("课次未在进行，不能签到")
            # 签到当场再次确认监护同意仍然有效。
            student = conn.execute(
                "SELECT consent FROM student WHERE student_id=?",
                (student_id,)).fetchone()
            if not student["consent"]:
                raise d.ConsentRequiredError(
                    "监护同意已撤回，不能签到；请联系教务员办理退出")

            ts = d.now_iso()
            conn.execute(
                "UPDATE enrollment SET status=?, check_in_at=?, updated_at=? "
                "WHERE enrollment_id=?",
                (ATTENDED, ts, ts, erow["enrollment_id"]))
            detail = {"session_id": session_id, "student_id": student_id}
            if erow["makeup_grant_id"] is not None:
                conn.execute(
                    "UPDATE makeup_grant SET status='used' WHERE grant_id=?",
                    (erow["makeup_grant_id"],))
                detail["grant_id"] = erow["makeup_grant_id"]
                self._audit(conn, action="makeup.used",
                            entity_type="makeup_grant",
                            entity_id=erow["makeup_grant_id"],
                            detail={"session_id": session_id},
                            actor_id=actor_id, actor_role=actor_role)
            self._audit(conn, action="enrollment.checked_in",
                        entity_type="enrollment",
                        entity_id=erow["enrollment_id"], detail=detail,
                        actor_id=actor_id, actor_role=actor_role)
        return {"session_id": session_id, "student_id": student_id,
                "status": ATTENDED, "check_in_at": ts}

    @_conflict
    def mark_absent(self, session_id: str, student_id: str,
                    actor_id: str | None = None,
                    actor_role: str | None = ROLE_STAFF) -> dict:
        """学生请假：释放席位并立即触发候补补位（同一事务）。"""
        with self.store.transaction() as conn:
            erow = conn.execute(
                "SELECT * FROM enrollment WHERE session_id=? AND student_id=? "
                "AND status=?",
                (session_id, student_id, ENROLLED)).fetchone()
            if erow is None:
                raise d.NotFoundError(
                    "找不到该学生在本课次的已占席报名（可能已签到或已退出）")
            conn.execute(
                "UPDATE enrollment SET status=?, updated_at=? "
                "WHERE enrollment_id=?",
                (ABSENT, d.now_iso(), erow["enrollment_id"]))
            reopened_grant = None
            if erow["makeup_grant_id"] is not None:
                # 用补课资格占的席因请假退出，资格退回、可用于再次补课。
                conn.execute(
                    "UPDATE makeup_grant SET status='open', "
                    "used_session_id=NULL, used_enrollment_id=NULL "
                    "WHERE grant_id=? AND status='reserved'",
                    (erow["makeup_grant_id"],))
                reopened_grant = erow["makeup_grant_id"]
            promoted = self._promote_waitlist(conn, session_id)
            self._audit(conn, action="enrollment.absent",
                        entity_type="enrollment",
                        entity_id=erow["enrollment_id"],
                        detail={"session_id": session_id,
                                "student_id": student_id,
                                "promoted_student": promoted,
                                "reopened_grant": reopened_grant},
                        actor_id=actor_id, actor_role=actor_role)
        return {"session_id": session_id, "student_id": student_id,
                "status": ABSENT, "promoted_student": promoted,
                "reopened_grant": reopened_grant}

    @_conflict
    def withdraw_consent(self, student_id: str, reason: str = "",
                         actor_id: str | None = None,
                         actor_role: str | None = None) -> dict:
        """监护人撤回同意。

        - 学生在所有未结束课次中的报名/候补一律退出并释放席位、候补补位；
        - 已签到（课次已参加）的历史记录保留；
        - 学生名下作品（含已入窑、烧制中的）归属与保管链一律不变，继续流转；
        - 被释放的已预留补课资格恢复为可用。
        """
        self._must_get("student", "student_id", student_id, "学生")
        affected_sessions: list[str] = []
        promoted: list[dict] = []
        retained_artworks: list[str] = []
        reopened_grants: list[int] = []
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE student SET consent=0, consent_updated_at=? "
                "WHERE student_id=?", (d.now_iso(), student_id))

            rows = conn.execute(
                "SELECT e.enrollment_id, e.session_id, e.status, "
                "e.makeup_grant_id FROM enrollment e JOIN session s "
                "ON s.session_id=e.session_id WHERE e.student_id=? "
                "AND s.status IN (?,?) AND e.status IN (?,?,?)",
                (student_id, SESSION_SCHEDULED, SESSION_IN_PROGRESS,
                 ENROLLED, WAITLISTED, ATTENDED)).fetchall()
            for r in rows:
                # 正在进行且已签到的，保留出勤事实；其余占席/候补退出。
                new_status = (ATTENDED if r["status"] == ATTENDED
                              else WITHDRAWN)
                if new_status == WITHDRAWN:
                    conn.execute(
                        "UPDATE enrollment SET status=?, updated_at=? "
                        "WHERE enrollment_id=?",
                        (WITHDRAWN, d.now_iso(), r["enrollment_id"]))
                    affected_sessions.append(r["session_id"])
                    if r["status"] == ENROLLED:
                        who = self._promote_waitlist(conn, r["session_id"])
                        if who:
                            promoted.append(
                                {"session_id": r["session_id"],
                                 "student_id": who})
                    if r["makeup_grant_id"] is not None:
                        grow = conn.execute(
                            "SELECT status FROM makeup_grant WHERE grant_id=?",
                            (r["makeup_grant_id"],)).fetchone()
                        if grow and grow["status"] == "reserved":
                            conn.execute(
                                "UPDATE makeup_grant SET status='open', "
                                "used_session_id=NULL, used_enrollment_id=NULL "
                                "WHERE grant_id=?",
                                (r["makeup_grant_id"],))
                            reopened_grants.append(r["makeup_grant_id"])

            art_rows = conn.execute(
                "SELECT artwork_id FROM artwork WHERE student_id=? "
                "AND current_state NOT IN ('scrapped','returned')",
                (student_id,)).fetchall()
            retained_artworks = [r["artwork_id"] for r in art_rows]

            self._audit(conn, action="consent.withdrawn",
                        entity_type="student", entity_id=student_id,
                        detail={"reason": reason,
                                "withdrawn_sessions": affected_sessions,
                                "promotions": promoted,
                                "reopened_grants": reopened_grants,
                                "retained_artworks": retained_artworks},
                        actor_id=actor_id, actor_role=actor_role)
        return {"student_id": student_id, "consent": False,
                "withdrawn_sessions": affected_sessions,
                "promotions": promoted,
                "reopened_grants": reopened_grants,
                "retained_artworks": retained_artworks}

    # ======================================================================
    # 课次取消与跨课次补课
    # ======================================================================

    @_conflict
    def cancel_session(self, session_id: str, reason: str = "",
                       actor_id: str | None = None,
                       actor_role: str | None = ROLE_STAFF) -> dict:
        """整次取消（如教师突发无法到岗）：占席学生获补课资格，整体回滚式处理。"""
        self._require_staff(actor_role)
        grants: list[int] = []
        with self.store.transaction() as conn:
            srow = conn.execute(
                "SELECT * FROM session WHERE session_id=?",
                (session_id,)).fetchone()
            if srow is None:
                raise d.NotFoundError(f"课次不存在：{session_id}")
            if srow["status"] not in OPEN_SESSION_STATUSES:
                raise d.ConflictError(
                    f"课次状态为 {srow['status']}，不能取消")

            actives = conn.execute(
                "SELECT * FROM enrollment WHERE session_id=? AND status IN (?,?,?)",
                (session_id, ENROLLED, WAITLISTED, ATTENDED)).fetchall()
            ts = d.now_iso()
            for e in actives:
                returned_grant = None
                new_grant = None
                used_grant = e["makeup_grant_id"]
                if e["status"] == ENROLLED and used_grant is not None:
                    # 本来就是凭补课资格占席且课没上成：退回原资格即可，不重复补偿。
                    grow = conn.execute(
                        "SELECT status FROM makeup_grant WHERE grant_id=?",
                        (used_grant,)).fetchone()
                    if grow and grow["status"] == "reserved":
                        conn.execute(
                            "UPDATE makeup_grant SET status='open', "
                            "used_session_id=NULL, used_enrollment_id=NULL "
                            "WHERE grant_id=?", (used_grant,))
                        returned_grant = used_grant
                elif e["status"] in (ENROLLED, ATTENDED):
                    # 普通报名占席者、以及已签到（课中上到一半取消）者，发补课资格。
                    cur = conn.execute(
                        "INSERT INTO makeup_grant(student_id, stage, reason, "
                        "source_session_id, status, created_at) "
                        "VALUES(?,?,?,?,'open',?)",
                        (e["student_id"], srow["stage"],
                         f"课次取消：{reason}" if reason else "课次取消",
                         session_id, ts))
                    new_grant = int(cur.lastrowid)
                    grants.append(new_grant)
                # 已签到的出勤事实保留；占席学生转待补课；候补直接作废。
                if e["status"] == WAITLISTED:
                    new_status = d.CANCELLED_ENROLLMENT
                elif e["status"] == ATTENDED:
                    new_status = ATTENDED
                else:
                    new_status = MOVED
                conn.execute(
                    "UPDATE enrollment SET status=?, updated_at=? "
                    "WHERE enrollment_id=?",
                    (new_status, ts, e["enrollment_id"]))
                self._audit(conn, action="enrollment.session_cancelled",
                            entity_type="enrollment",
                            entity_id=e["enrollment_id"],
                            detail={"session_id": session_id,
                                    "student_id": e["student_id"],
                                    "new_status": new_status,
                                    "returned_grant": returned_grant,
                                    "new_grant": new_grant})

            conn.execute(
                "UPDATE session SET status=? WHERE session_id=?",
                (SESSION_CANCELLED, session_id))
            self._audit(conn, action="session.cancelled",
                        entity_type="session", entity_id=session_id,
                        detail={"reason": reason,
                                "grants_issued": grants,
                                "affected": len(actives)},
                        actor_id=actor_id, actor_role=actor_role)
        return {"session_id": session_id, "status": SESSION_CANCELLED,
                "makeup_grants": grants}

    def list_makeup_grants(self, student_id: str) -> list[dict]:
        rows = self.store.all(
            "SELECT * FROM makeup_grant WHERE student_id=? ORDER BY grant_id",
            (student_id,))
        return [dict(r) for r in rows]

    def roster(self, session_id: str, actor_id: str | None = None,
               actor_role: str | None = None) -> dict:
        """查看名单：教务员与本课次指导教师可见。"""
        rows = self.store.all(
            "SELECT e.enrollment_id, e.student_id, e.status, "
            "e.waitlist_position, e.check_in_at, e.makeup_grant_id, "
            "st.name AS student_name "
            "FROM enrollment e JOIN student st ON st.student_id=e.student_id "
            "WHERE e.session_id=? ORDER BY e.enrollment_id",
            (session_id,))
        if actor_role not in (ROLE_STAFF, ROLE_TEACHER, ROLE_KILN_KEEPER):
            raise d.PermissionDeniedError("无权查看课次名单")
        if actor_role == ROLE_TEACHER:
            assigned = self.store.one(
                "SELECT 1 FROM session_teacher WHERE session_id=? AND teacher_id=?",
                (session_id, actor_id))
            if not assigned:
                raise d.PermissionDeniedError("只能查看自己任课课次的名单")
        return {"session_id": session_id,
                "enrollments": [dict(r) for r in rows]}

    # ======================================================================
    # 作品流转：领泥 → 揉泥 → 拉坯 → 修坯 → 入窑 → 烧制 → 发还
    # ======================================================================

    def _append_transfer(self, conn, artwork_id: str, action: str, *,
                         from_type, from_id, to_type, to_id,
                         actor_id, session_id=None, batch_id=None,
                         note=None) -> int:
        ts = d.now_iso()
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS s FROM artwork_transfer "
            "WHERE artwork_id=?", (artwork_id,)).fetchone()["s"]
        conn.execute(
            "INSERT INTO artwork_transfer(artwork_id, seq, at, action, "
            "from_holder_type, from_holder_id, to_holder_type, to_holder_id, "
            "actor_id, session_id, batch_id, note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (artwork_id, seq, ts, action, from_type, from_id, to_type, to_id,
             actor_id, session_id, batch_id, note))
        return seq

    def _require_session_teacher(self, conn, session_id: str,
                                 actor_id, actor_role) -> None:
        if actor_role == ROLE_STAFF:
            return
        if actor_role != ROLE_TEACHER:
            raise d.PermissionDeniedError("只有任课教师或教务员可以记录作品工序")
        if not conn.execute(
                "SELECT 1 FROM session_teacher WHERE session_id=? AND teacher_id=?",
                (session_id, actor_id)).fetchone():
            raise d.PermissionDeniedError("教师不属于该课次")

    @_conflict
    def issue_clay(self, artwork_id: str, session_id: str, student_id: str,
                   actor_id: str | None = None,
                   actor_role: str | None = ROLE_TEACHER) -> dict:
        """在揉泥课次上向已签到学生领泥，作品建档，初始保管人为学生。"""
        with self.store.transaction() as conn:
            self._require_session_teacher(conn, session_id,
                                          actor_id, actor_role)
            srow = conn.execute(
                "SELECT * FROM session WHERE session_id=?",
                (session_id,)).fetchone()
            if srow is None:
                raise d.NotFoundError(f"课次不存在：{session_id}")
            if srow["stage"] != d.STAGE_KNEADING:
                raise d.ConflictError("只能在揉泥课次领泥")
            erow = conn.execute(
                "SELECT * FROM enrollment WHERE session_id=? AND student_id=? "
                "AND status=?",
                (session_id, student_id, ATTENDED)).fetchone()
            if erow is None:
                raise d.ConflictError("学生未在该课次签到，不能领泥")
            if conn.execute(
                    "SELECT 1 FROM artwork WHERE artwork_id=?",
                    (artwork_id,)).fetchone():
                raise d.ConflictError(f"作品编号已存在：{artwork_id}")
            ts = d.now_iso()
            conn.execute(
                "INSERT INTO artwork(artwork_id, student_id, "
                "origin_session_id, current_state, custody_holder_type, "
                "custody_holder_id, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (artwork_id, student_id, session_id, ART_CLAY_ISSUED,
                 "student", student_id, ts, ts))
            self._append_transfer(
                conn, artwork_id, d.HANDOFF_ISSUE_CLAY,
                from_type="material", from_id="clay_store",
                to_type="student", to_id=student_id,
                actor_id=actor_id, session_id=session_id,
                note="揉泥课领泥")
            self._audit(conn, action="artwork.issued",
                        entity_type="artwork", entity_id=artwork_id,
                        detail={"session_id": session_id,
                                "student_id": student_id},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_artwork(artwork_id, actor_id="system",
                                actor_role=ROLE_STAFF)

    @_conflict
    def complete_stage(self, artwork_id: str, session_id: str,
                       actor_id: str | None = None,
                       actor_role: str | None = ROLE_TEACHER) -> dict:
        """在对应阶段课次上记录工序完成（揉泥/拉坯/修坯），不可跳阶。"""
        with self.store.transaction() as conn:
            self._require_session_teacher(conn, session_id,
                                          actor_id, actor_role)
            art = conn.execute(
                "SELECT * FROM artwork WHERE artwork_id=?",
                (artwork_id,)).fetchone()
            if art is None:
                raise d.NotFoundError(f"作品不存在：{artwork_id}")
            srow = conn.execute(
                "SELECT * FROM session WHERE session_id=?",
                (session_id,)).fetchone()
            if srow is None:
                raise d.NotFoundError(f"课次不存在：{session_id}")
            if srow["stage"] not in STAGE_ART_STATE:
                raise d.ConflictError("该课次阶段不产生作品工序记录")
            target = STAGE_ART_STATE[srow["stage"]]
            expected = d.ARTWORK_PREREQ[target]
            if art["current_state"] != expected:
                raise d.ConflictError(
                    f"作品当前为 {art['current_state']}，"
                    f"完成 {srow['stage']} 前应处于 {expected}，禁止跳阶")
            erow = conn.execute(
                "SELECT * FROM enrollment WHERE session_id=? AND student_id=? "
                "AND status=?",
                (session_id, art["student_id"], ATTENDED)).fetchone()
            if erow is None:
                raise d.ConflictError("作品作者未在该课次签到，不能记录工序")
            conn.execute(
                "UPDATE artwork SET current_state=?, updated_at=? "
                "WHERE artwork_id=?",
                (target, d.now_iso(), artwork_id))
            action = {d.STAGE_KNEADING: d.HANDOFF_KNEAD,
                      d.STAGE_THROWING: d.HANDOFF_THROW,
                      d.STAGE_TRIMMING: d.HANDOFF_TRIM}[srow["stage"]]
            # 学生在教师指导下完成，保管人仍为学生，教师作为交接责任人留痕。
            self._append_transfer(
                conn, artwork_id, action,
                from_type="student", from_id=art["student_id"],
                to_type="student", to_id=art["student_id"],
                actor_id=actor_id, session_id=session_id,
                note=f"{srow['stage']} 工序完成")
            self._audit(conn, action="artwork.stage_completed",
                        entity_type="artwork", entity_id=artwork_id,
                        detail={"session_id": session_id, "to_state": target},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_artwork(artwork_id, actor_id="system",
                                actor_role=ROLE_STAFF)

    # ---------------- 窑炉与批次 ----------------

    @_conflict
    def register_kiln(self, kiln_id: str, name: str, capacity: int,
                      actor_id: str | None = None,
                      actor_role: str | None = ROLE_STAFF) -> dict:
        self._require_staff(actor_role)
        if not isinstance(capacity, int) or capacity <= 0:
            raise d.DomainError("窑炉容量必须是正整数")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO kiln(kiln_id, name, status, capacity, created_at) "
                "VALUES(?,?,?,?,?)",
                (kiln_id, name, KILN_ACTIVE, capacity, d.now_iso()))
            self._audit(conn, action="kiln.registered",
                        entity_type="kiln", entity_id=kiln_id,
                        detail={"name": name, "capacity": capacity},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_kiln(kiln_id)

    def get_kiln(self, kiln_id: str) -> dict:
        row = self._must_get("kiln", "kiln_id", kiln_id, "窑炉")
        return dict(row)

    @_conflict
    def create_batch(self, batch_id: str, kiln_id: str,
                     actor_id: str | None = None,
                     actor_role: str | None = ROLE_KILN_KEEPER) -> dict:
        with self.store.transaction() as conn:
            krow = conn.execute("SELECT * FROM kiln WHERE kiln_id=?",
                                (kiln_id,)).fetchone()
            if krow is None:
                raise d.NotFoundError(f"窑炉不存在：{kiln_id}")
            if krow["status"] != KILN_ACTIVE:
                raise d.ConflictError(
                    f"窑炉当前 {krow['status']}，不能新建烧制批次")
            ts = d.now_iso()
            conn.execute(
                "INSERT INTO kiln_batch(batch_id, kiln_id, status, created_at) "
                "VALUES(?,?,?,?)", (batch_id, kiln_id, BATCH_READY, ts))
            self._audit(conn, action="batch.created",
                        entity_type="kiln_batch", entity_id=batch_id,
                        detail={"kiln_id": kiln_id},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict:
        row = self._must_get("kiln_batch", "batch_id", batch_id, "烧制批次")
        result = dict(row)
        items = self.store.all(
            "SELECT artwork_id, state_on_load, loaded_at, unloaded_at "
            "FROM batch_item WHERE batch_id=? ORDER BY loaded_at",
            (batch_id,))
        result["items"] = [dict(r) for r in items]
        return result

    @_conflict
    def load_artworks(self, batch_id: str, artwork_ids: list[str],
                      actor_id: str | None = None,
                      actor_role: str | None = ROLE_KILN_KEEPER) -> dict:
        """装窑：作品必须修坯完成；受窑炉容量与停窑状态约束。"""
        if not artwork_ids:
            raise d.DomainError("至少装入一件作品")
        loaded: list[str] = []
        with self.store.transaction() as conn:
            brow = conn.execute(
                "SELECT * FROM kiln_batch WHERE batch_id=?",
                (batch_id,)).fetchone()
            if brow is None:
                raise d.NotFoundError(f"烧制批次不存在：{batch_id}")
            if brow["status"] != BATCH_READY:
                raise d.ConflictError(
                    f"批次状态为 {brow['status']}，不能继续装窑")
            krow = conn.execute(
                "SELECT * FROM kiln WHERE kiln_id=?",
                (brow["kiln_id"],)).fetchone()
            if krow["status"] != KILN_ACTIVE:
                raise d.ConflictError(
                    f"窑炉 {krow['status']}，禁止装窑；作品保持原保管状态")
            used = conn.execute(
                "SELECT COUNT(*) AS n FROM batch_item WHERE batch_id=? "
                "AND unloaded_at IS NULL", (batch_id,)).fetchone()["n"]
            if used + len(artwork_ids) > krow["capacity"]:
                raise d.ConflictError(
                    f"装窑数量超过窑炉容量（已装 {used}，"
                    f"容量 {krow['capacity']}，本次 {len(artwork_ids)}）")
            ts = d.now_iso()
            for aid in artwork_ids:
                art = conn.execute(
                    "SELECT * FROM artwork WHERE artwork_id=?",
                    (aid,)).fetchone()
                if art is None:
                    raise d.NotFoundError(f"作品不存在：{aid}")
                if art["current_state"] != d.ART_TRIMMED:
                    raise d.ConflictError(
                        f"作品 {aid} 当前为 {art['current_state']}，"
                        "须修坯完成才能入窑")
                if conn.execute(
                        "SELECT 1 FROM batch_item WHERE artwork_id=? "
                        "AND unloaded_at IS NULL", (aid,)).fetchone():
                    raise d.ConflictError(f"作品 {aid} 已在其他在窑批次中")
                conn.execute(
                    "INSERT INTO batch_item(batch_id, artwork_id, "
                    "state_on_load, loaded_at) VALUES(?,?,?,?)",
                    (batch_id, aid, d.ART_TRIMMED, ts))
                conn.execute(
                    "UPDATE artwork SET current_state=?, custody_holder_type=?, "
                    "custody_holder_id=?, updated_at=? WHERE artwork_id=?",
                    (ART_LOADED, "kiln", krow["kiln_id"], ts, aid))
                self._append_transfer(
                    conn, aid, d.HANDOFF_LOAD,
                    from_type="student", from_id=art["student_id"],
                    to_type="kiln", to_id=krow["kiln_id"],
                    actor_id=actor_id, batch_id=batch_id,
                    note="装窑入炉")
                loaded.append(aid)
            self._audit(conn, action="artwork.loaded",
                        entity_type="kiln_batch", entity_id=batch_id,
                        detail={"kiln_id": brow["kiln_id"],
                                "artworks": loaded},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_batch(batch_id)

    @_conflict
    def seal_batch(self, batch_id: str,
                   actor_id: str | None = None,
                   actor_role: str | None = ROLE_KILN_KEEPER) -> dict:
        """封窑待烧。"""
        with self.store.transaction() as conn:
            brow = conn.execute(
                "SELECT * FROM kiln_batch WHERE batch_id=?",
                (batch_id,)).fetchone()
            if brow is None:
                raise d.NotFoundError(f"烧制批次不存在：{batch_id}")
            if brow["status"] != BATCH_READY:
                raise d.ConflictError(f"批次状态为 {brow['status']}，不能封窑")
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM batch_item WHERE batch_id=? "
                "AND unloaded_at IS NULL", (batch_id,)).fetchone()["n"]
            if n == 0:
                raise d.ConflictError("空批次不能封窑")
            conn.execute(
                "UPDATE kiln_batch SET status=?, loaded_at=? WHERE batch_id=?",
                (BATCH_LOADED, d.now_iso(), batch_id))
            self._audit(conn, action="batch.sealed",
                        entity_type="kiln_batch", entity_id=batch_id,
                        detail={"items": n},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_batch(batch_id)

    @_conflict
    def pause_kiln(self, kiln_id: str, reason: str = "",
                   actor_id: str | None = None,
                   actor_role: str | None = ROLE_STAFF) -> dict:
        """临时停窑：在窑批次挂起，作品归属与保管位置不变。"""
        held_batches: list[str] = []
        held_artworks: list[str] = []
        with self.store.transaction() as conn:
            krow = conn.execute("SELECT * FROM kiln WHERE kiln_id=?",
                                (kiln_id,)).fetchone()
            if krow is None:
                raise d.NotFoundError(f"窑炉不存在：{kiln_id}")
            if krow["status"] != KILN_ACTIVE:
                raise d.ConflictError(f"窑炉已处于 {krow['status']} 状态")
            ts = d.now_iso()
            conn.execute(
                "UPDATE kiln SET status=?, paused_at=? WHERE kiln_id=?",
                (KILN_PAUSED, ts, kiln_id))
            batches = conn.execute(
                "SELECT * FROM kiln_batch WHERE kiln_id=? AND status IN (?,?)",
                (kiln_id, BATCH_LOADED, BATCH_FIRING)).fetchall()
            for b in batches:
                conn.execute(
                    "UPDATE kiln_batch SET status=?, held_from_status=?, "
                    "held_at=? WHERE batch_id=?",
                    (BATCH_HELD, b["status"], ts, b["batch_id"]))
                held_batches.append(b["batch_id"])
                items = conn.execute(
                    "SELECT artwork_id FROM batch_item WHERE batch_id=? "
                    "AND unloaded_at IS NULL", (b["batch_id"],)).fetchall()
                for it in items:
                    aid = it["artwork_id"]
                    self._append_transfer(
                        conn, aid, d.HANDOFF_HOLD,
                        from_type="kiln", from_id=kiln_id,
                        to_type="kiln", to_id=kiln_id,
                        actor_id=actor_id, batch_id=b["batch_id"],
                        note=f"停窑挂起：{reason}")
                    held_artworks.append(aid)
            self._audit(conn, action="kiln.paused",
                        entity_type="kiln", entity_id=kiln_id,
                        detail={"reason": reason,
                                "held_batches": held_batches,
                                "held_artworks": held_artworks},
                        actor_id=actor_id, actor_role=actor_role)
        return {"kiln_id": kiln_id, "status": KILN_PAUSED,
                "held_batches": held_batches,
                "held_artworks": held_artworks}

    @_conflict
    def resume_kiln(self, kiln_id: str,
                    actor_id: str | None = None,
                    actor_role: str | None = ROLE_STAFF) -> dict:
        """窑炉恢复：挂起批次回到停窑前状态，作品继续烧制，归属不变。"""
        resumed_batches: list[str] = []
        resumed_artworks: list[str] = []
        with self.store.transaction() as conn:
            krow = conn.execute("SELECT * FROM kiln WHERE kiln_id=?",
                                (kiln_id,)).fetchone()
            if krow is None:
                raise d.NotFoundError(f"窑炉不存在：{kiln_id}")
            if krow["status"] != KILN_PAUSED:
                raise d.ConflictError(f"窑炉未处于停窑状态：{krow['status']}")
            ts = d.now_iso()
            conn.execute(
                "UPDATE kiln SET status=?, resumed_at=? WHERE kiln_id=?",
                (KILN_ACTIVE, ts, kiln_id))
            held = conn.execute(
                "SELECT * FROM kiln_batch WHERE kiln_id=? AND status=?",
                (kiln_id, BATCH_HELD)).fetchall()
            for b in held:
                restore = b["held_from_status"] or BATCH_LOADED
                conn.execute(
                    "UPDATE kiln_batch SET status=?, resumed_at=? "
                    "WHERE batch_id=?",
                    (restore, ts, b["batch_id"]))
                resumed_batches.append(b["batch_id"])
                items = conn.execute(
                    "SELECT artwork_id FROM batch_item WHERE batch_id=? "
                    "AND unloaded_at IS NULL", (b["batch_id"],)).fetchall()
                for it in items:
                    aid = it["artwork_id"]
                    # 作品状态仍是 loaded、保管人仍是窑炉——这里只补恢复留痕。
                    self._append_transfer(
                        conn, aid, d.HANDOFF_RESUME,
                        from_type="kiln", from_id=kiln_id,
                        to_type="kiln", to_id=kiln_id,
                        actor_id=actor_id, batch_id=b["batch_id"],
                        note="窑炉恢复，继续烧制")
                    resumed_artworks.append(aid)
            self._audit(conn, action="kiln.resumed",
                        entity_type="kiln", entity_id=kiln_id,
                        detail={"resumed_batches": resumed_batches,
                                "resumed_artworks": resumed_artworks},
                        actor_id=actor_id, actor_role=actor_role)
        return {"kiln_id": kiln_id, "status": KILN_ACTIVE,
                "resumed_batches": resumed_batches,
                "resumed_artworks": resumed_artworks}

    @_conflict
    def fire_batch(self, batch_id: str,
                   actor_id: str | None = None,
                   actor_role: str | None = ROLE_KILN_KEEPER) -> dict:
        """烧制完成：作品出窑，保管权回到作者学生（即便其同意已撤回也不改变归属）。"""
        fired: list[str] = []
        with self.store.transaction() as conn:
            brow = conn.execute(
                "SELECT * FROM kiln_batch WHERE batch_id=?",
                (batch_id,)).fetchone()
            if brow is None:
                raise d.NotFoundError(f"烧制批次不存在：{batch_id}")
            if brow["status"] == BATCH_HELD:
                raise d.ConflictError("批次挂起中，须先恢复窑炉才能出窑")
            if brow["status"] not in (BATCH_LOADED, BATCH_FIRING):
                raise d.ConflictError(
                    f"批次状态为 {brow['status']}，不能完成烧制")
            krow = conn.execute(
                "SELECT status FROM kiln WHERE kiln_id=?",
                (brow["kiln_id"],)).fetchone()
            if krow["status"] != KILN_ACTIVE:
                raise d.ConflictError("窑炉停窑中，不能完成烧制")
            ts = d.now_iso()
            items = conn.execute(
                "SELECT bi.artwork_id, a.student_id FROM batch_item bi "
                "JOIN artwork a ON a.artwork_id=bi.artwork_id "
                "WHERE bi.batch_id=? AND bi.unloaded_at IS NULL",
                (batch_id,)).fetchall()
            for it in items:
                aid = it["artwork_id"]
                conn.execute(
                    "UPDATE artwork SET current_state=?, custody_holder_type=?, "
                    "custody_holder_id=?, updated_at=? WHERE artwork_id=?",
                    (d.ART_FIRED, "student", it["student_id"], ts, aid))
                conn.execute(
                    "UPDATE batch_item SET unloaded_at=? WHERE batch_id=? "
                    "AND artwork_id=?", (ts, batch_id, aid))
                self._append_transfer(
                    conn, aid, d.HANDOFF_FIRE,
                    from_type="kiln", from_id=brow["kiln_id"],
                    to_type="student", to_id=it["student_id"],
                    actor_id=actor_id, batch_id=batch_id,
                    note="烧制完成出窑")
                fired.append(aid)
            conn.execute(
                "UPDATE kiln_batch SET status=?, fired_at=? WHERE batch_id=?",
                (BATCH_FIRED, ts, batch_id))
            self._audit(conn, action="batch.fired",
                        entity_type="kiln_batch", entity_id=batch_id,
                        detail={"kiln_id": brow["kiln_id"],
                                "artworks": fired},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_batch(batch_id)

    @_conflict
    def return_artwork(self, artwork_id: str,
                       actor_id: str | None = None,
                       actor_role: str | None = ROLE_STAFF) -> dict:
        with self.store.transaction() as conn:
            art = conn.execute(
                "SELECT * FROM artwork WHERE artwork_id=?",
                (artwork_id,)).fetchone()
            if art is None:
                raise d.NotFoundError(f"作品不存在：{artwork_id}")
            if art["current_state"] != d.ART_FIRED:
                raise d.ConflictError(
                    f"作品状态为 {art['current_state']}，烧成品才能发还")
            conn.execute(
                "UPDATE artwork SET current_state=?, updated_at=? "
                "WHERE artwork_id=?",
                (d.ART_RETURNED, d.now_iso(), artwork_id))
            self._append_transfer(
                conn, artwork_id, d.HANDOFF_RETURN,
                from_type="student", from_id=art["student_id"],
                to_type="student", to_id=art["student_id"],
                actor_id=actor_id, note="成品发还作者")
            self._audit(conn, action="artwork.returned",
                        entity_type="artwork", entity_id=artwork_id,
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_artwork(artwork_id, actor_id="system",
                                actor_role=ROLE_STAFF)

    @_conflict
    def scrap_artwork(self, artwork_id: str, reason: str,
                      actor_id: str | None = None,
                      actor_role: str | None = ROLE_TEACHER) -> dict:
        if not reason:
            raise d.DomainError("作废作品必须填写原因")
        with self.store.transaction() as conn:
            art = conn.execute(
                "SELECT * FROM artwork WHERE artwork_id=?",
                (artwork_id,)).fetchone()
            if art is None:
                raise d.NotFoundError(f"作品不存在：{artwork_id}")
            if art["current_state"] in (d.ART_SCRAPPED, d.ART_RETURNED,
                                        d.ART_FIRED):
                raise d.ConflictError(
                    f"作品状态为 {art['current_state']}，不能作废")
            if art["current_state"] == ART_LOADED:
                raise d.ConflictError("作品已入窑，须出窑后再处理，不能直接作废")
            ts = d.now_iso()
            conn.execute(
                "UPDATE artwork SET current_state=?, scrapped_reason=?, "
                "updated_at=? WHERE artwork_id=?",
                (d.ART_SCRAPPED, reason, ts, artwork_id))
            self._append_transfer(
                conn, artwork_id, d.HANDOFF_SCRAP,
                from_type=art["custody_holder_type"],
                from_id=art["custody_holder_id"],
                to_type=art["custody_holder_type"],
                to_id=art["custody_holder_id"],
                actor_id=actor_id, note=f"作废：{reason}")
            self._audit(conn, action="artwork.scrapped",
                        entity_type="artwork", entity_id=artwork_id,
                        detail={"reason": reason,
                                "from_state": art["current_state"]},
                        actor_id=actor_id, actor_role=actor_role)
        return self.get_artwork(artwork_id, actor_id="system",
                                actor_role=ROLE_STAFF)

    # ======================================================================
    # 授权查看
    # ======================================================================

    def _artwork_dict(self, row: sqlite3.Row) -> dict:
        return {
            "artwork_id": row["artwork_id"],
            "student_id": row["student_id"],
            "origin_session_id": row["origin_session_id"],
            "current_state": row["current_state"],
            "custody_holder_type": row["custody_holder_type"],
            "custody_holder_id": row["custody_holder_id"],
            "scrapped_reason": row["scrapped_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_artwork(self, artwork_id: str, actor_id: str,
                    actor_role: str) -> dict:
        """按角色授权查看作品：学生/监护人只能看自己名下作品。"""
        conn = self.store.connection
        art = conn.execute(
            "SELECT * FROM artwork WHERE artwork_id=?",
            (artwork_id,)).fetchone()
        if art is None:
            raise d.NotFoundError(f"作品不存在：{artwork_id}")
        self._authorize_artwork(conn, art, actor_id, actor_role)
        return self._artwork_with_detail(conn, art)

    def _authorize_artwork(self, conn, art, actor_id, actor_role) -> None:
        if actor_role in (ROLE_STAFF, ROLE_KILN_KEEPER):
            return
        if actor_role == ROLE_STUDENT:
            owner = conn.execute(
                "SELECT guardian_id FROM student WHERE student_id=?",
                (art["student_id"],)).fetchone()
            if actor_id == art["student_id"]:
                return
            if owner and owner["guardian_id"] == actor_id:
                return
            raise d.PermissionDeniedError("只能查看自己名下的作品信息")
        if actor_role == ROLE_TEACHER:
            seen = conn.execute(
                "SELECT 1 FROM artwork_transfer t WHERE t.artwork_id=? "
                "AND t.actor_id=? LIMIT 1",
                (art["artwork_id"], actor_id)).fetchone()
            assigned = conn.execute(
                "SELECT 1 FROM session_teacher st WHERE st.teacher_id=? "
                "AND st.session_id=?",
                (actor_id, art["origin_session_id"])).fetchone()
            if not seen and not assigned:
                raise d.PermissionDeniedError("教师只能查看经手过的作品")
            return
        raise d.PermissionDeniedError("未知角色，无权查看作品")

    def _artwork_with_detail(self, conn, art) -> dict:
        result = self._artwork_dict(art)
        transfers = conn.execute(
            "SELECT seq, at, action, from_holder_type, from_holder_id, "
            "to_holder_type, to_holder_id, actor_id, session_id, batch_id, "
            "note FROM artwork_transfer WHERE artwork_id=? ORDER BY seq",
            (art["artwork_id"],)).fetchall()
        result["transfers"] = [dict(r) for r in transfers]
        return result

    def list_artworks_for_viewer(self, actor_id: str,
                                 actor_role: str) -> list[dict]:
        """学生/监护人列出自己被授权（名下）的作品；教职工按权限列全部。"""
        conn = self.store.connection
        if actor_role == ROLE_STUDENT:
            rows = conn.execute(
                "SELECT a.* FROM artwork a JOIN student s "
                "ON s.student_id=a.student_id "
                "WHERE a.student_id=? OR s.guardian_id=? "
                "ORDER BY a.created_at",
                (actor_id, actor_id)).fetchall()
        elif actor_role in (ROLE_STAFF, ROLE_KILN_KEEPER):
            rows = conn.execute(
                "SELECT * FROM artwork ORDER BY created_at").fetchall()
        elif actor_role == ROLE_TEACHER:
            rows = conn.execute(
                "SELECT DISTINCT a.* FROM artwork a "
                "LEFT JOIN artwork_transfer t "
                "ON t.artwork_id=a.artwork_id AND t.actor_id=? "
                "LEFT JOIN session_teacher st "
                "ON st.session_id=a.origin_session_id AND st.teacher_id=? "
                "WHERE t.artwork_id IS NOT NULL OR st.teacher_id IS NOT NULL "
                "ORDER BY a.created_at",
                (actor_id, actor_id)).fetchall()
        else:
            raise d.PermissionDeniedError("未知角色")
        return [self._artwork_dict(r) for r in rows]

    # ======================================================================
    # 审计
    # ======================================================================

    def audit_trail(self, entity_type: str, entity_id: str,
                    actor_id: str | None = None,
                    actor_role: str | None = None) -> list[dict]:
        """查询某实体的审计记录；仅教务员可用，按时间顺序返回。"""
        if actor_role != ROLE_STAFF:
            raise d.PermissionDeniedError("只有教务员可以查阅审计记录")
        rows = self.store.all(
            "SELECT audit_id, at, actor_id, actor_role, action, "
            "entity_type, entity_id, detail FROM audit_log "
            "WHERE entity_type=? AND entity_id=? ORDER BY audit_id",
            (entity_type, str(entity_id)))
        result = []
        for r in rows:
            item = dict(r)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result
