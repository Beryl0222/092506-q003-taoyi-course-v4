"""课程编排与作品流转的应用服务。

服务层负责所有业务规则与授权；存储层只在单个 ``BEGIN IMMEDIATE`` 事务内
读写，保证“查容量/查冲突 → 写入”的原子性，业务拒绝时整体回滚。
需要在“被回滚的拒绝”上也保留痕迹的场景（重复签到、停窑装窑），在事务
回滚之后补写一条审计，保证审计记录不因业务回滚而消失。
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable

from . import domain as d
from .domain import (
    ARTIFACT_TRANSITIONS, DAMAGED, OUT_OF_STUDENT_HANDS, STAGES,
    TERMINAL_ARTIFACT_STATES, ConsentRequiredError, ConsentRevokedError,
    DuplicateCheckinError, DuplicateError, InvalidTransitionError,
    KilnHaltedError, NotFoundError, PermissionDeniedError, Principal,
    CapacityError, QualificationError, TeacherConflictError,
)
from .store import Store


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # -- 鉴权辅助 -----------------------------------------------------------

    @staticmethod
    def _require_staff(actor: Principal) -> None:
        if not actor.is_staff:
            raise PermissionDeniedError("仅教务员可执行该操作")

    @staticmethod
    def _require_staff_or_teacher(actor: Principal) -> None:
        if not (actor.is_staff or actor.is_teacher):
            raise PermissionDeniedError("仅教务员或指导教师可执行该操作")

    @staticmethod
    def _require_staff_or_self(actor: Principal, student_id: str) -> None:
        if actor.is_staff:
            return
        if actor.student_id == student_id and actor.role in (
                "student", "guardian"):
            return
        raise PermissionDeniedError("无权替该学生操作")

    def _require_session_teacher(self, conn, actor: Principal,
                                 session_id: str) -> None:
        if actor.is_staff:
            return
        if not self.store.teacher_assigned(conn, session_id, actor.user_id):
            raise PermissionDeniedError(
                f"教师 {actor.user_id} 未安排到课次 {session_id}")

    # -- 旧版登记（兼容基线）-----------------------------------------------

    def health(self) -> dict[str, str]:
        return {"service": "taoyi_course", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str]:
        record = self.store.save(d.Record(record_id, owner_id))
        return {"record_id": record.record_id, "owner_id": record.owner_id,
                "state": record.state, "created_at": record.created_at}

    def find(self, record_id: str) -> dict[str, str] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    # -- 基础档案 -----------------------------------------------------------

    def register_teacher(self, actor: Principal, teacher_id: str, name: str,
                         qualifications: Iterable[str]) -> dict:
        self._require_staff(actor)
        quals = tuple(qualifications)
        unknown = [q for q in quals if q not in STAGES]
        if unknown:
            raise d.DomainError(f"未知阶段资质: {','.join(unknown)}")
        with self.store.transaction() as conn:
            if self.store.get_teacher(teacher_id):
                raise DuplicateError("教师已存在", resource="teacher")
            self.store.create_teacher(conn, teacher_id, name, list(quals))
            self.store.audit(conn, actor.user_id, "teacher.register",
                             entity_type="teacher", entity_id=teacher_id,
                             detail={"qualifications": list(quals)})
        return {"teacher_id": teacher_id, "name": name,
                "qualifications": list(quals)}

    def register_student(self, actor: Principal, student_id: str,
                         name: str) -> dict:
        self._require_staff(actor)
        with self.store.transaction() as conn:
            if self.store.get_student(student_id):
                raise DuplicateError("学生已存在", resource="student")
            self.store.create_student(conn, student_id, name)
            self.store.audit(conn, actor.user_id, "student.register",
                             entity_type="student", entity_id=student_id)
        return {"student_id": student_id, "name": name}

    def grant_consent(self, actor: Principal, student_id: str) -> dict:
        """监护人授予陶艺课参与同意。"""
        self._require_guardian_for(actor, student_id)
        with self.store.transaction() as conn:
            if not self.store.get_student(student_id):
                raise NotFoundError("学生不存在")
            at = d.now_iso()
            self.store.set_consent(conn, student_id, True, at)
            self.store.audit(conn, actor.user_id, d.A_CONSENT_GRANT,
                             entity_type="student", entity_id=student_id,
                             detail={"student_id": student_id}, at=at)
        return {"student_id": student_id, "consent": True}

    def withdraw_consent(self, actor: Principal, student_id: str) -> dict:
        """监护人撤回同意。

        * 已报名/候补的课次全部撤回，空出名额由候补队首自动替补；
        * 已交给教师或已入窑（含烧制完成）的作品**归属冻结**，继续烧制并
          在责任链上记录“同意撤回、作品在制”，不会失去归属；
        * 仍在学生手中、尚未交出的未完成作品终止流转（归还学生）。
        """
        self._require_guardian_for(actor, student_id)
        with self.store.transaction() as conn:
            if not self.store.get_student(student_id):
                raise NotFoundError("学生不存在")
            at = d.now_iso()
            self.store.set_consent(conn, student_id, False, at)
            affected_sessions: list[str] = []
            promotions: list[dict] = []
            held_artifacts: list[str] = []
            returned_artifacts: list[str] = []

            for row in self.store.list_student_enrollments(student_id):
                if row["status"] not in (d.ENROLLED, d.WAITLISTED):
                    continue
                session_id = row["session_id"]
                self.store.withdraw_enrollment(
                    conn, session_id, student_id, "consent_withdrawn", at)
                affected_sessions.append(session_id)
                self.store.audit(
                    conn, actor.user_id, d.A_CONSENT_WITHDRAW,
                    entity_type="enrollment",
                    entity_id=f"{session_id}:{student_id}",
                    session_id=session_id,
                    detail={"student_id": student_id,
                            "previous_status": row["status"]}, at=at)
                if row["status"] == d.ENROLLED and row["session_status"] == \
                        "open":
                    promoted = self._promote_waitlist_head(
                        conn, session_id, actor.user_id, at,
                        reason="consent_withdrawn")
                    if promoted:
                        promotions.append(promoted)

            for art in self.store.list_artifacts(
                    owner_student_id=student_id):
                if art["state"] in TERMINAL_ARTIFACT_STATES:
                    # fired 成品归属学生；damaged 已终止。都不再改动。
                    continue
                if art["state"] in OUT_OF_STUDENT_HANDS:
                    held_artifacts.append(art["artifact_id"])
                    seq = self.store.next_custody_seq(conn, art["artifact_id"])
                    self.store.append_custody(
                        conn, art["artifact_id"], seq,
                        "consent_withdrawn_hold", art["state"],
                        art["state"], actor.user_id, actor.role,
                        "监护同意撤回，作品已在流转中，归属保留至烧制完成",
                        at)
                else:
                    self.store.update_artifact_state(
                        conn, art["artifact_id"], d.RETURNED, at)
                    seq = self.store.next_custody_seq(conn, art["artifact_id"])
                    self.store.append_custody(
                        conn, art["artifact_id"], seq,
                        "consent_withdrawn_return", art["state"],
                        d.RETURNED,
                        actor.user_id, actor.role,
                        "监护同意撤回，未交出作品归还学生并终止流转", at)
                    returned_artifacts.append(art["artifact_id"])

        return {"student_id": student_id, "consent": False,
                "withdrawn_sessions": affected_sessions,
                "promotions": promotions,
                "held_artifacts": held_artifacts,
                "returned_artifacts": returned_artifacts}

    def _require_guardian_for(self, actor: Principal,
                              student_id: str) -> None:
        if actor.is_staff:
            return
        if actor.role == "guardian" and actor.student_id == student_id:
            return
        raise PermissionDeniedError("仅监护人本人或教务员可变更该同意")

    # -- 课次发布与教师安排 -------------------------------------------------

    def publish_session(self, actor: Principal, stage: str,
                        scheduled_start: str, scheduled_end: str,
                        capacity: int, *, wheel_count: int = 0,
                        session_id: str | None = None,
                        teacher_ids: Iterable[str] | None = None,
                        note: str = "") -> dict:
        self._require_staff(actor)
        if stage not in STAGES:
            raise d.DomainError(f"未知课程阶段: {stage}")
        if capacity <= 0:
            raise d.DomainError("安全容量必须为正整数")
        if wheel_count < 0 or wheel_count > capacity:
            raise d.DomainError("陶轮数量不能为负且不能超过安全容量")
        if scheduled_end <= scheduled_start:
            raise d.DomainError("课次结束时间必须晚于开始时间")
        session_id = session_id or _uid("s")
        with self.store.transaction() as conn:
            if self.store.get_session(session_id):
                raise DuplicateError("课次已存在", resource="session")
            self.store.insert_session(conn, {
                "session_id": session_id, "stage": stage,
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end, "capacity": capacity,
                "wheel_count": wheel_count, "note": note,
                "created_by": actor.user_id})
            self.store.audit(conn, actor.user_id, d.A_SESSION_PUBLISH,
                             entity_type="session", entity_id=session_id,
                             session_id=session_id,
                             detail={"stage": stage, "capacity": capacity,
                                     "wheel_count": wheel_count})
            for tid in teacher_ids or []:
                self._assign_teacher_in_tx(conn, actor, session_id, tid,
                                           stage, scheduled_start,
                                           scheduled_end)
        return self.get_session(actor, session_id)

    def assign_teacher(self, actor: Principal, session_id: str,
                       teacher_id: str) -> dict:
        self._require_staff(actor)
        with self.store.transaction() as conn:
            session = self._require_session(conn, session_id)
            self._assign_teacher_in_tx(
                conn, actor, session_id, teacher_id, session["stage"],
                session["scheduled_start"], session["scheduled_end"])
        return {"session_id": session_id, "teacher_id": teacher_id}

    def _assign_teacher_in_tx(self, conn, actor: Principal, session_id: str,
                              teacher_id: str, stage: str, start: str,
                              end: str) -> None:
        teacher = self.store.get_teacher(teacher_id)
        if not teacher:
            raise NotFoundError(f"教师不存在: {teacher_id}")
        if stage not in teacher["qualifications"]:
            raise QualificationError(
                f"教师 {teacher_id} 不具备{d.STAGE_NAMES[stage]}资质")
        if self.store.teacher_assigned(conn, session_id, teacher_id):
            raise DuplicateError("教师已安排到该课次", resource="teacher")
        clash = self._find_teacher_clash(conn, teacher_id, start, end,
                                         exclude_session=session_id)
        if clash:
            # 安排被整体回滚；冲突事件在回滚后补写（见 assign_teacher 外层）。
            raise TeacherConflictError(
                f"教师 {teacher_id} 与课次 {clash['session_id']} 时间冲突",
                teacher_id=teacher_id, session_id=session_id,
                conflicting_session_id=clash["session_id"])
        self.store.assign_teacher(conn, session_id, teacher_id, stage,
                                  d.now_iso())
        self.store.audit(conn, actor.user_id, d.A_TEACHER_ASSIGN,
                         entity_type="session_teacher",
                         entity_id=f"{session_id}:{teacher_id}",
                         session_id=session_id,
                         detail={"teacher_id": teacher_id})

    def assign_teacher_guarded(self, actor: Principal, session_id: str,
                               teacher_id: str) -> dict:
        """与 :meth:`assign_teacher` 相同，但冲突在回滚后仍保留审计。"""
        try:
            return self.assign_teacher(actor, session_id, teacher_id)
        except TeacherConflictError as exc:
            self.store.audit_now(
                actor.user_id, d.A_TEACHER_CONFLICT,
                entity_type="session_teacher",
                entity_id=f"{session_id}:{teacher_id}",
                session_id=session_id,
                detail={"teacher_id": teacher_id,
                        "conflicting_session_id":
                            exc.conflicting_session_id})
            raise

    @staticmethod
    def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
        return a_start < b_end and b_start < a_end

    def _find_teacher_clash(self, conn, teacher_id: str, start: str, end: str,
                            *, exclude_session: str) -> dict | None:
        for other in self.store.list_teacher_sessions(teacher_id):
            if other["session_id"] == exclude_session:
                continue
            if other["status"] == "cancelled":
                continue
            if self._overlaps(start, end, other["scheduled_start"],
                              other["scheduled_end"]):
                return other
        return None

    def _require_session(self, conn, session_id: str) -> dict:
        session = self.store.get_session(session_id)
        if not session:
            raise NotFoundError("课次不存在")
        return session

    def get_session(self, actor: Principal | None, session_id: str) -> dict:
        session = self.store.get_session(session_id)
        if not session:
            raise NotFoundError("课次不存在")
        session["teachers"] = self.store.list_session_teachers(session_id)
        enrolled = self.store.list_enrollments(session_id, d.ENROLLED)
        waitlisted = self.store.list_enrollments(session_id, d.WAITLISTED)
        effective_capacity = self._effective_capacity(session)
        session["enrolled"] = [e["student_id"] for e in enrolled]
        session["waitlist"] = [
            {"student_id": e["student_id"], "position": e["waitlist_position"]}
            for e in sorted(waitlisted,
                            key=lambda e: e["waitlist_position"])]
        session["seats_taken"] = len(enrolled)
        session["effective_capacity"] = effective_capacity
        session["seats_available"] = effective_capacity - len(enrolled)
        return session

    @staticmethod
    def _effective_capacity(session: dict) -> int:
        """班级人数受安全容量约束；拉坯课还要同时受陶轮工位约束。"""
        if session["stage"] == "throwing" and session["wheel_count"]:
            return min(session["capacity"], session["wheel_count"])
        return session["capacity"]

    def list_sessions(self, actor: Principal, **filters: Any) -> list[dict]:
        return self.store.list_sessions(**filters)

    # -- 报名（并发安全）与候补 ----------------------------------------------

    def enroll(self, actor: Principal, session_id: str, student_id: str,
               *, credit_id: str | None = None) -> dict:
        """学生报名课次。满员进候补；空名额按候补队首自动替补。

        并发报名在单个 ``BEGIN IMMEDIATE`` 事务内完成“计数 → 占位”，
        超出安全容量的请求要么成功占座要么进入候补，绝不超卖；缺同意、
        重复报名等拒绝在写入前抛出，事务整体回滚。
        """
        self._require_staff_or_self(actor, student_id)
        with self.store.transaction() as conn:
            at = d.now_iso()
            session = self._require_open_session(conn, session_id)
            if not self.store.get_student(student_id):
                raise NotFoundError("学生不存在")
            if not self.store.consent_granted(conn, student_id):
                raise ConsentRequiredError("缺少监护人陶艺课参与同意")

            existing = self.store.get_enrollment(conn, session_id, student_id)
            if existing and existing["status"] == d.ENROLLED:
                raise DuplicateError("学生已报名该课次",
                                     resource="enrollment")
            if existing and existing["status"] == d.WAITLISTED:
                raise DuplicateError("学生已在该课次候补队列",
                                     resource="enrollment")

            taken = self.store.count_enrolled(conn, session_id)
            effective_capacity = self._effective_capacity(session)
            if taken >= effective_capacity and credit_id:
                # 补课必须真正排入课次；满员时直接拒绝、不核销额度，
                # 由教务另选时间，而不是把补课学生放进候补。
                raise CapacityError("目标课次已满，补课无法排入")

            makeup_used = None
            if credit_id:
                makeup_used = self._consume_makeup_in_tx(
                    conn, actor, credit_id, student_id, session, at)

            if taken < effective_capacity:
                if existing is not None:
                    self.store.reactivate_enrollment(
                        conn, session_id, student_id, d.ENROLLED, at)
                else:
                    self.store.insert_enrollment(
                        conn, session_id, student_id, d.ENROLLED, at)
                status = d.ENROLLED
                self.store.audit(conn, actor.user_id, d.A_ENROLL,
                                 entity_type="enrollment",
                                 entity_id=f"{session_id}:{student_id}",
                                 session_id=session_id,
                                 detail={"student_id": student_id,
                                         "makeup_credit_id": makeup_used},
                                 at=at)
            else:
                position = self.store.next_waitlist_position(conn, session_id)
                if existing is not None:
                    self.store.reactivate_enrollment(
                        conn, session_id, student_id, d.WAITLISTED, at,
                        waitlist_position=position)
                else:
                    self.store.insert_enrollment(
                        conn, session_id, student_id, d.WAITLISTED, at,
                        waitlist_position=position)
                status = d.WAITLISTED
                self.store.audit(conn, actor.user_id, d.A_WAITLIST,
                                 entity_type="enrollment",
                                 entity_id=f"{session_id}:{student_id}",
                                 session_id=session_id,
                                 detail={"student_id": student_id,
                                         "position": position,
                                         "makeup_credit_id": makeup_used},
                                 at=at)
        return {"session_id": session_id, "student_id": student_id,
                "status": status,
                **({"makeup_credit_id": makeup_used} if makeup_used else {})}

    def _require_open_session(self, conn, session_id: str) -> dict:
        session = self._require_session(conn, session_id)
        if session["status"] != "open":
            raise d.DomainError(
                f"课次当前状态为 {session['status']}，不可报名")
        return session

    def _consume_makeup_in_tx(self, conn, actor: Principal, credit_id: str,
                              student_id: str, session: dict,
                              at: str) -> str:
        credit = self.store.get_makeup_credit(credit_id)
        if not credit:
            raise NotFoundError("补课额度不存在")
        if credit["student_id"] != student_id:
            raise PermissionDeniedError("补课额度不属于该学生")
        if credit["state"] != "granted":
            raise d.DomainError("补课额度已使用或失效")
        if credit["stage"] != session["stage"]:
            raise d.DomainError("补课额度的课程阶段与目标课次不一致")
        self.store.use_makeup_credit(conn, credit_id,
                                     session["session_id"], at)
        self.store.audit(conn, actor.user_id, d.A_MAKEUP_ENROLL,
                         entity_type="makeup_credit", entity_id=credit_id,
                         session_id=session["session_id"],
                         detail={"student_id": student_id}, at=at)
        return credit_id

    def withdraw_enrollment(self, actor: Principal, session_id: str,
                            student_id: str, *, reason: str = "voluntary",
                            grant_makeup: bool = False) -> dict:
        """主动退课/教务退课；空出名额触发候补替补，可选发放补课额度。"""
        self._require_staff_or_self(actor, student_id)
        with self.store.transaction() as conn:
            at = d.now_iso()
            session = self._require_session(conn, session_id)
            enrollment = self.store.get_enrollment(conn, session_id,
                                                   student_id)
            if not enrollment or enrollment["status"] not in (
                    d.ENROLLED, d.WAITLISTED):
                raise d.DomainError("该报名不在可撤回状态")
            previous = enrollment["status"]
            self.store.withdraw_enrollment(conn, session_id, student_id,
                                           reason, at)
            self.store.audit(conn, actor.user_id, d.A_WITHDRAW,
                             entity_type="enrollment",
                             entity_id=f"{session_id}:{student_id}",
                             session_id=session_id,
                             detail={"student_id": student_id, "reason": reason,
                                     "previous_status": previous}, at=at)
            credit_id = None
            promoted = None
            if grant_makeup and previous == d.ENROLLED:
                # 候补未实际占座，退课不产生补课额度。
                credit_id = _uid("mk")
                self.store.grant_makeup(conn, {
                    "credit_id": credit_id, "student_id": student_id,
                    "stage": session["stage"],
                    "source_session_id": session_id,
                    "reason": reason, "created_at": at})
                self.store.audit(conn, actor.user_id, d.A_MAKEUP_GRANT,
                                 entity_type="makeup_credit",
                                 entity_id=credit_id,
                                 session_id=session_id,
                                 detail={"student_id": student_id,
                                         "stage": session["stage"]}, at=at)
            if previous == d.ENROLLED and session["status"] == "open":
                promoted = self._promote_waitlist_head(
                    conn, session_id, actor.user_id, at, reason=reason)
        return {"session_id": session_id, "student_id": student_id,
                "status": d.WITHDRAWN,
                **({"makeup_credit_id": credit_id} if credit_id else {}),
                **({"promoted": promoted} if promoted else {})}

    def _promote_waitlist_head(self, conn, session_id: str, actor_id: str,
                               at: str, *, reason: str) -> dict | None:
        head = self.store.pop_waitlist_head(conn, session_id)
        if not head:
            return None
        if not self.store.consent_granted(conn, head["student_id"]):
            # 队首已失去同意：移出候补，继续尝试下一位。
            self.store.withdraw_enrollment(
                conn, session_id, head["student_id"],
                "consent_missing_at_promotion", at)
            return self._promote_waitlist_head(conn, session_id, actor_id, at,
                                               reason=reason)
        self.store.promote_enrollment(conn, session_id, head["student_id"], at)
        self.store.audit(conn, actor_id, d.A_PROMOTE,
                         entity_type="enrollment",
                         entity_id=f"{session_id}:{head['student_id']}",
                         session_id=session_id,
                         detail={"student_id": head["student_id"],
                                 "from_position": head["waitlist_position"],
                                 "reason": reason}, at=at)
        return {"student_id": head["student_id"],
                "from_waitlist_position": head["waitlist_position"]}

    def cancel_session(self, actor: Principal, session_id: str,
                       *, reason: str = "") -> dict:
        """临时取消整节课：在读学生获同阶段补课额度；候补报名一并撤回。

        不触发候补替补（课次已取消），学生可凭额度跨课次补课。
        """
        self._require_staff(actor)
        with self.store.transaction() as conn:
            at = d.now_iso()
            session = self._require_session(conn, session_id)
            if session["status"] == "cancelled":
                raise d.DomainError("课次已取消")
            self.store.set_session_status(conn, session_id, "cancelled")
            credits = []
            for enrollment in self.store.list_enrollments(session_id,
                                                          d.ENROLLED):
                credit_id = _uid("mk")
                self.store.grant_makeup(conn, {
                    "credit_id": credit_id,
                    "student_id": enrollment["student_id"],
                    "stage": session["stage"],
                    "source_session_id": session_id,
                    "reason": reason or "session_cancelled",
                    "created_at": at})
                credits.append({"credit_id": credit_id,
                                "student_id": enrollment["student_id"]})
            for entry in self.store.list_enrollments(session_id,
                                                     d.WAITLISTED):
                self.store.withdraw_enrollment(
                    conn, session_id, entry["student_id"],
                    "session_cancelled", at)
            self.store.audit(conn, actor.user_id, "session.cancel",
                             entity_type="session", entity_id=session_id,
                             session_id=session_id,
                             detail={"reason": reason, "credits": credits},
                             at=at)
        return {"session_id": session_id, "status": "cancelled",
                "makeup_credits": credits}

    # -- 签到 / 请假 --------------------------------------------------------

    def checkin(self, actor: Principal, session_id: str,
                student_id: str) -> dict:
        """到场签到。重复签到显式拒绝、数据不变，并在回滚后保留一条审计。"""
        self._require_staff_or_teacher(actor)
        with self.store.transaction() as conn:
            at = d.now_iso()
            session = self._require_session(conn, session_id)
            if session["status"] == "cancelled":
                raise d.DomainError("课次已取消，不能签到")
            self._require_session_teacher(conn, actor, session_id)
            enrollment = self.store.get_enrollment(conn, session_id,
                                                   student_id)
            if not enrollment or enrollment["status"] != d.ENROLLED:
                raise d.DomainError("学生不在该课次的已报名名单中")
            if not self.store.consent_granted(conn, student_id):
                raise ConsentRevokedError("监护同意已撤回，不能签到")
            if self.store.get_attendance(session_id, student_id):
                raise DuplicateCheckinError(
                    "学生已在该课次签到，请勿重复操作",
                    session_id=session_id, student_id=student_id)
            self.store.mark_attendance(conn, session_id, student_id,
                                       "present", actor.user_id, at)
            self.store.audit(conn, actor.user_id, d.A_CHECKIN,
                             entity_type="attendance",
                             entity_id=f"{session_id}:{student_id}",
                             session_id=session_id,
                             detail={"student_id": student_id}, at=at)
        return {"session_id": session_id, "student_id": student_id,
                "attendance": "present"}

    def checkin_guarded(self, actor: Principal, session_id: str,
                        student_id: str) -> dict:
        """签到入口：重复签到在回滚后仍写入审计，再把错误交回调用方。"""
        try:
            return self.checkin(actor, session_id, student_id)
        except DuplicateCheckinError:
            self.store.audit_now(
                actor.user_id, d.A_CHECKIN_DUP,
                entity_type="attendance",
                entity_id=f"{session_id}:{student_id}",
                session_id=session_id,
                detail={"student_id": student_id,
                        "reason": "duplicate_checkin_rejected"})
            raise

    def mark_absence(self, actor: Principal, session_id: str,
                     student_id: str, *, reason: str = "leave",
                     grant_makeup: bool = True) -> dict:
        """登记请假：记录缺勤并按规则发放同阶段补课额度。"""
        self._require_staff_or_teacher(actor)
        with self.store.transaction() as conn:
            at = d.now_iso()
            session = self._require_session(conn, session_id)
            self._require_session_teacher(conn, actor, session_id)
            enrollment = self.store.get_enrollment(conn, session_id,
                                                   student_id)
            if not enrollment or enrollment["status"] != d.ENROLLED:
                raise d.DomainError("学生不在该课次的已报名名单中")
            existing = self.store.get_attendance(session_id, student_id)
            if existing and existing["status"] == "present":
                raise DuplicateCheckinError(
                    "学生已签到，不能改记请假", session_id=session_id,
                    student_id=student_id)
            if existing and existing["status"] == "absent":
                raise DuplicateCheckinError(
                    "该学生已登记请假，不能重复发放补课",
                    session_id=session_id, student_id=student_id)
            self.store.mark_attendance(conn, session_id, student_id,
                                       "absent", actor.user_id, at)
            credit_id = None
            if grant_makeup:
                credit_id = _uid("mk")
                self.store.grant_makeup(conn, {
                    "credit_id": credit_id, "student_id": student_id,
                    "stage": session["stage"],
                    "source_session_id": session_id, "reason": reason,
                    "created_at": at})
            self.store.audit(conn, actor.user_id, d.A_ABSENCE,
                             entity_type="attendance",
                             entity_id=f"{session_id}:{student_id}",
                             session_id=session_id,
                             detail={"student_id": student_id, "reason": reason,
                                     "makeup_credit_id": credit_id}, at=at)
        result = {"session_id": session_id, "student_id": student_id,
                  "attendance": "absent"}
        if credit_id:
            result["makeup_credit_id"] = credit_id
        return result

    def list_makeup_credits(self, actor: Principal,
                            student_id: str) -> list[dict]:
        if not actor.is_staff and actor.student_id != student_id:
            raise PermissionDeniedError("只能查看本人的补课额度")
        return self.store.list_makeup_credits(student_id)

    # -- 作品：领泥 → 拉坯 → 修坯 → 交接 → 入窑 → 烧制 -----------------------

    def issue_clay(self, actor: Principal, session_id: str,
                   student_id: str, *, artifact_id: str | None = None,
                   name: str = "") -> dict:
        """揉泥阶段领泥，创建作品，第一棒责任记到学生名下。"""
        self._require_staff_or_teacher(actor)
        artifact_id = artifact_id or _uid("art")
        with self.store.transaction() as conn:
            at = d.now_iso()
            session = self._require_session(conn, session_id)
            if session["status"] != "open":
                raise d.DomainError("课次已取消，不能领泥")
            if session["stage"] != "wedging":
                raise d.DomainError("只有揉泥课次可以领泥")
            enrollment = self.store.get_enrollment(conn, session_id,
                                                   student_id)
            if not enrollment or enrollment["status"] != d.ENROLLED:
                raise d.DomainError("学生未报名该揉泥课次")
            if not self.store.consent_granted(conn, student_id):
                raise ConsentRevokedError("监护同意已撤回，不能领泥")
            if self.store.get_artifact_row(conn, artifact_id):
                raise DuplicateError("作品编号已存在", resource="artifact")
            self.store.insert_artifact(conn, {
                "artifact_id": artifact_id,
                "owner_student_id": student_id,
                "origin_session_id": session_id, "name": name,
                "state": d.CLAY_ISSUED}, at)
            self.store.append_custody(
                conn, artifact_id, 0, "issue_clay", None, d.CLAY_ISSUED,
                student_id, "student", f"揉泥课次 {session_id} 领泥", at)
            self.store.audit(conn, actor.user_id, d.A_ARTIFACT_CREATE,
                             entity_type="artifact", entity_id=artifact_id,
                             session_id=session_id,
                             detail={"student_id": student_id,
                                     "name": name}, at=at)
        return self.get_artifact(actor, artifact_id)

    def transition_artifact(self, actor: Principal, artifact_id: str,
                            action: str, *, note: str = "",
                            custodian_id: str | None = None) -> dict:
        """推进作品状态（拉坯/修坯/交给教师），每步登记责任交接。"""
        self._require_staff_or_teacher(actor)
        if action != "mark_damaged" and action not in ARTIFACT_TRANSITIONS:
            raise InvalidTransitionError(f"未知作品动作: {action}")
        with self.store.transaction() as conn:
            at = d.now_iso()
            artifact = self.store.get_artifact_row(conn, artifact_id)
            if not artifact:
                raise NotFoundError("作品不存在")
            current = artifact["state"]
            if action == "mark_damaged":
                if current in TERMINAL_ARTIFACT_STATES:
                    raise InvalidTransitionError("终态作品不能再标记损坏")
                new_state = DAMAGED
            else:
                required, new_state = ARTIFACT_TRANSITIONS[action]
                if current != required:
                    raise InvalidTransitionError(
                        f"作品当前状态 {current} 不能执行动作 {action}"
                        f"（要求 {required}）")
            custodian = custodian_id or actor.user_id
            self.store.update_artifact_state(conn, artifact_id, new_state, at)
            seq = self.store.next_custody_seq(conn, artifact_id)
            self.store.append_custody(
                conn, artifact_id, seq, action, current, new_state,
                custodian, actor.role, note, at)
            self.store.audit(conn, actor.user_id, d.A_ARTIFACT_TRANSITION,
                             entity_type="artifact", entity_id=artifact_id,
                             session_id=artifact["origin_session_id"],
                             detail={"action": action, "from": current,
                                     "to": new_state,
                                     "custodian_id": custodian}, at=at)
        return self.get_artifact(actor, artifact_id)

    # -- 窑炉周转与停窑/恢复 -------------------------------------------------

    def register_kiln(self, actor: Principal, kiln_id: str,
                      name: str) -> dict:
        self._require_staff(actor)
        with self.store.transaction() as conn:
            if self.store.get_kiln(conn, kiln_id):
                raise DuplicateError("窑炉已存在", resource="kiln")
            self.store.create_kiln(conn, kiln_id, name)
            self.store.audit(conn, actor.user_id, "kiln.register",
                             entity_type="kiln", entity_id=kiln_id)
        return {"kiln_id": kiln_id, "name": name, "state": d.KILN_OPEN}

    def halt_kiln(self, actor: Principal, kiln_id: str,
                  *, reason: str = "") -> dict:
        """临时停窑：拒绝新的装窑/开烧；窑内作品原样保留，归属与状态不变。"""
        self._require_staff(actor)
        with self.store.transaction() as conn:
            at = d.now_iso()
            kiln = self.store.get_kiln(conn, kiln_id)
            if not kiln:
                raise NotFoundError("窑炉不存在")
            if kiln["state"] == d.KILN_HALTED:
                raise d.DomainError("窑炉已处于停窑状态")
            self.store.update_kiln_state(conn, kiln_id, d.KILN_HALTED, at)
            self.store.audit(conn, actor.user_id, d.A_KILN_HALT,
                             entity_type="kiln", entity_id=kiln_id,
                             detail={"reason": reason}, at=at)
        return {"kiln_id": kiln_id, "state": d.KILN_HALTED}

    def resume_kiln(self, actor: Principal, kiln_id: str,
                    *, note: str = "") -> dict:
        """恢复窑炉：被挡住的交接可继续，已入窑作品无需重新归属。"""
        self._require_staff(actor)
        with self.store.transaction() as conn:
            at = d.now_iso()
            kiln = self.store.get_kiln(conn, kiln_id)
            if not kiln:
                raise NotFoundError("窑炉不存在")
            if kiln["state"] == d.KILN_OPEN:
                raise d.DomainError("窑炉未处于停窑状态")
            self.store.update_kiln_state(conn, kiln_id, d.KILN_OPEN, at)
            self.store.audit(conn, actor.user_id, d.A_KILN_RESUME,
                             entity_type="kiln", entity_id=kiln_id,
                             detail={"note": note}, at=at)
        return {"kiln_id": kiln_id, "state": d.KILN_OPEN}

    def load_kiln(self, actor: Principal, kiln_id: str,
                  artifact_ids: Iterable[str], *,
                  batch_id: str | None = None) -> dict:
        """把已交给教师的作品装窑。

        停窑期间拒绝并整体回滚（不会出现半装批次），回滚后补写一条
        ``kiln.blocked`` 审计；恢复窑炉后原样重试即可。
        """
        self._require_staff_or_teacher(actor)
        artifact_ids = list(artifact_ids)
        if not artifact_ids:
            raise d.DomainError("装窑清单不能为空")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise d.DomainError("装窑清单存在重复作品")
        batch_id = batch_id or _uid("batch")
        try:
            with self.store.transaction() as conn:
                at = d.now_iso()
                kiln = self.store.get_kiln(conn, kiln_id)
                if not kiln:
                    raise NotFoundError("窑炉不存在")
                if kiln["state"] == d.KILN_HALTED:
                    raise KilnHaltedError(
                        f"窑炉 {kiln_id} 临时停窑，不能装窑；作品继续由"
                        "教师保管，恢复后可重试")
                if self.store.get_batch(conn, batch_id):
                    raise DuplicateError("装窑批次已存在", resource="batch")
                loaded = []
                for artifact_id in artifact_ids:
                    artifact = self.store.get_artifact_row(conn, artifact_id)
                    if not artifact:
                        raise NotFoundError(f"作品不存在: {artifact_id}")
                    if artifact["state"] != d.HANDED_OVER:
                        raise InvalidTransitionError(
                            f"作品 {artifact_id} 状态 {artifact['state']}，"
                            "必须已交给教师才能装窑")
                    if self.store.artifact_in_open_batch(conn, artifact_id):
                        raise DuplicateError(
                            f"作品 {artifact_id} 已在未完成的装窑批次中",
                            resource="batch_artifact")
                    loaded.append(artifact)
                self.store.insert_batch(conn, batch_id, kiln_id, at)
                for artifact in loaded:
                    self.store.add_artifact_to_batch(
                        conn, batch_id, artifact["artifact_id"], at)
                    self.store.update_artifact_state(
                        conn, artifact["artifact_id"], d.KILN_LOADED, at)
                    seq = self.store.next_custody_seq(
                        conn, artifact["artifact_id"])
                    self.store.append_custody(
                        conn, artifact["artifact_id"], seq, "load_kiln",
                        d.HANDED_OVER, d.KILN_LOADED, actor.user_id,
                        actor.role,
                        f"装入窑炉 {kiln_id} / 批次 {batch_id}", at)
                self.store.audit(conn, actor.user_id, d.A_BATCH_LOAD,
                                 entity_type="firing_batch",
                                 entity_id=batch_id,
                                 detail={"kiln_id": kiln_id,
                                         "artifact_ids": artifact_ids}, at=at)
        except KilnHaltedError:
            self.store.audit_now(
                actor.user_id, d.A_KILN_BLOCK, entity_type="kiln",
                entity_id=kiln_id,
                detail={"reason": "halted", "attempted_batch_id": batch_id,
                        "artifact_ids": artifact_ids})
            raise
        return {"batch_id": batch_id, "kiln_id": kiln_id,
                "state": d.BATCH_LOADED, "artifact_ids": artifact_ids}

    def start_firing(self, actor: Principal, batch_id: str) -> dict:
        self._require_staff_or_teacher(actor)
        with self.store.transaction() as conn:
            at = d.now_iso()
            batch = self.store.get_batch(conn, batch_id)
            if not batch:
                raise NotFoundError("装窑批次不存在")
            kiln = self.store.get_kiln(conn, batch["kiln_id"])
            if kiln["state"] == d.KILN_HALTED:
                raise KilnHaltedError("窑炉临时停窑，不能开烧")
            if batch["state"] != d.BATCH_LOADED:
                raise InvalidTransitionError(
                    f"批次状态 {batch['state']} 不能开烧")
            self._set_batch_state(conn, actor, batch, d.BATCH_FIRING,
                                  d.A_BATCH_START, "start_firing", d.FIRING,
                                  d.KILN_LOADED, at)
        return {"batch_id": batch_id, "state": d.BATCH_FIRING}

    def complete_firing(self, actor: Principal, batch_id: str) -> dict:
        """烧制完成：批次内作品统一变为 fired，责任链记录最后一次交接。"""
        self._require_staff_or_teacher(actor)
        with self.store.transaction() as conn:
            at = d.now_iso()
            batch = self.store.get_batch(conn, batch_id)
            if not batch:
                raise NotFoundError("装窑批次不存在")
            if batch["state"] != d.BATCH_FIRING:
                raise InvalidTransitionError(
                    f"批次状态 {batch['state']} 不能完成烧制")
            self._set_batch_state(conn, actor, batch, d.BATCH_COMPLETED,
                                  d.A_BATCH_COMPLETE, "complete_firing",
                                  d.FIRED, d.FIRING, at)
        return {"batch_id": batch_id, "state": d.BATCH_COMPLETED}

    def _set_batch_state(self, conn, actor: Principal, batch: dict,
                         new_batch_state: str, audit_action: str,
                         custody_action: str,
                         new_artifact_state: str, old_artifact_state: str,
                         at: str) -> None:
        batch_id = batch["batch_id"]
        artifact_ids = self.store.list_batch_artifacts(batch_id)
        for artifact_id in artifact_ids:
            artifact = self.store.get_artifact_row(conn, artifact_id)
            if artifact["state"] != old_artifact_state:
                raise InvalidTransitionError(
                    f"作品 {artifact_id} 状态 {artifact['state']} 与批次"
                    f" {new_batch_state} 不一致，事务回滚")
        self.store.update_batch_state(conn, batch_id, new_batch_state, at)
        for artifact_id in artifact_ids:
            self.store.update_artifact_state(conn, artifact_id,
                                             new_artifact_state, at)
            seq = self.store.next_custody_seq(conn, artifact_id)
            self.store.append_custody(
                conn, artifact_id, seq, custody_action, old_artifact_state,
                new_artifact_state, actor.user_id, actor.role,
                f"批次 {batch_id}", at)
        self.store.audit(conn, actor.user_id, audit_action,
                         entity_type="firing_batch", entity_id=batch_id,
                         detail={"artifact_ids": artifact_ids}, at=at)

    # -- 作品查看授权 --------------------------------------------------------

    def get_artifact(self, actor: Principal | None,
                     artifact_id: str) -> dict:
        artifact = self.store.find_artifact(artifact_id)
        if not artifact:
            raise NotFoundError("作品不存在")
        self._authorize_artifact_read(actor, artifact)
        return self._artifact_view(artifact)

    def list_artifacts_for(self, actor: Principal,
                           student_id: str) -> list[dict]:
        """学生/监护人只能列出本人名下作品；教师与教务员可代查。"""
        if not actor.is_staff and not actor.is_teacher \
                and actor.student_id != student_id:
            raise PermissionDeniedError("只能查看本人名下的作品")
        return [self._artifact_view(a)
                for a in self.store.list_artifacts(
                    owner_student_id=student_id)]

    def _artifact_view(self, artifact: dict) -> dict:
        return {
            "artifact_id": artifact["artifact_id"],
            "owner_student_id": artifact["owner_student_id"],
            "session_id": artifact["origin_session_id"],
            "origin_session_id": artifact["origin_session_id"],
            "state": artifact["state"], "name": artifact["name"],
            "created_at": artifact["created_at"],
            "updated_at": artifact["updated_at"],
            "custody_chain": self.store.list_custody(
                artifact["artifact_id"])}

    @staticmethod
    def _authorize_artifact_read(actor: Principal | None,
                                 artifact: dict) -> None:
        if actor is None:
            raise PermissionDeniedError("缺少访问身份")
        if actor.is_staff or actor.is_teacher:
            return
        if actor.student_id == artifact["owner_student_id"]:
            return
        raise PermissionDeniedError("无权查看该作品信息")

    # -- 审计查询 ------------------------------------------------------------

    def list_audit(self, actor: Principal, **filters: Any) -> list[dict]:
        self._require_staff(actor)
        return self.store.list_audit(**filters)
