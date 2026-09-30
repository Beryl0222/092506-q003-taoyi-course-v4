"""课次、报名、作品交接与窑炉周转的领域模型。

状态约定：

* 报名 enrollment: ``enrolled``（已占名额）/ ``waitlisted``（候补）/
  ``withdrawn``（撤回，含请假或监护同意撤回）。
* 作品 artifact: ``clay_issued``（领泥）→ ``thrown``（拉坯）→
  ``trimmed``（修坯）→ ``handed_over``（交给教师）→ ``kiln_loaded``（入窑）
  → ``firing``（烧制中）→ ``fired``（烧制完成）。
  窑炉临时停窑时入窑被拒绝，作品保持 ``handed_over`` 由教师保管；已入窑作品
  保持 ``kiln_loaded``/``firing``，窑炉恢复后继续烧制，归属始终不变。流转中
  发现损坏可置 ``damaged``；监护同意撤回时仍在学生手中的在制品置
  ``returned``（归还学生），均为终态。
* 窑炉 kiln: ``open``（可装窑）/ ``halted``（临时停窑）。
* 装窑批次 firing_batch: ``loaded`` → ``firing`` → ``completed``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- 课程分阶段 -----------------------------------------------------------

# 揉泥（领泥）、拉坯、修坯依次进行；修坯完成后作品才具备入窑交接资格。
STAGES = ("wedging", "throwing", "trimming")
STAGE_NAMES = {
    "wedging": "揉泥",
    "throwing": "拉坯",
    "trimming": "修坯",
}

# --- 报名状态 -------------------------------------------------------------

ENROLLED = "enrolled"
WAITLISTED = "waitlisted"
WITHDRAWN = "withdrawn"

# --- 作品状态机 -----------------------------------------------------------

CLAY_ISSUED = "clay_issued"
THROWN = "thrown"
TRIMMED = "trimmed"
HANDED_OVER = "handed_over"
KILN_LOADED = "kiln_loaded"
FIRING = "firing"
FIRED = "fired"
DAMAGED = "damaged"
RETURNED = "returned"

#: 允许的作品交接动作 → (前置状态, 后继状态)。
ARTIFACT_TRANSITIONS = {
    "throw": (CLAY_ISSUED, THROWN),
    "trim": (THROWN, TRIMMED),
    "hand_over": (TRIMMED, HANDED_OVER),
    "load_kiln": (HANDED_OVER, KILN_LOADED),
    "start_firing": (KILN_LOADED, FIRING),
    "complete_firing": (FIRING, FIRED),
    "mark_damaged": None,  # 任意非终态可标记损坏
}

#: 作品已经离开学生之手、归属不可再因报名变动而改变的状态。
IN_KILN_STATES = (KILN_LOADED, FIRING, FIRED)
#: 作品已经离开学生之手（含教师待烧保管），归属冻结。
OUT_OF_STUDENT_HANDS = (HANDED_OVER,) + IN_KILN_STATES
TERMINAL_ARTIFACT_STATES = (FIRED, DAMAGED, RETURNED)

# --- 窑炉与批次 -----------------------------------------------------------

KILN_OPEN = "open"
KILN_HALTED = "halted"

BATCH_LOADED = "loaded"
BATCH_FIRING = "firing"
BATCH_COMPLETED = "completed"

# --- 审计动作 -------------------------------------------------------------

A_SESSION_PUBLISH = "session.publish"
A_TEACHER_ASSIGN = "session.teacher_assign"
A_TEACHER_CONFLICT = "session.teacher_conflict"
A_ENROLL = "enrollment.create"
A_WAITLIST = "enrollment.waitlist"
A_PROMOTE = "enrollment.promote"
A_WITHDRAW = "enrollment.withdraw"
A_CONSENT_GRANT = "consent.grant"
A_CONSENT_WITHDRAW = "consent.withdraw"
A_CHECKIN = "session.checkin"
A_CHECKIN_DUP = "session.checkin_duplicate"
A_ABSENCE = "session.absence"
A_MAKEUP_GRANT = "makeup.grant"
A_MAKEUP_ENROLL = "makeup.enroll"
A_ARTIFACT_CREATE = "artifact.create"
A_ARTIFACT_TRANSITION = "artifact.transition"
A_KILN_HALT = "kiln.halt"
A_KILN_RESUME = "kiln.resume"
A_BATCH_LOAD = "batch.load"
A_BATCH_START = "batch.start"
A_BATCH_COMPLETE = "batch.complete"
A_KILN_BLOCK = "kiln.blocked"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- 基础记录（保留既有登记能力）------------------------------------------


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str = "draft"
    created_at: str = ""

    def with_timestamp(self) -> "Record":
        return Record(self.record_id, self.owner_id, self.state,
                      self.created_at or now_iso())


# --- 调用身份 -------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """请求身份。

    ``role`` 为 ``staff``（教务员）/ ``teacher`` / ``student`` /
    ``guardian``；``student_id`` 用于监护人或学生身份与作品归属做授权比对。
    """

    user_id: str
    role: str = "staff"
    student_id: str | None = None

    @property
    def is_staff(self) -> bool:
        return self.role == "staff"

    @property
    def is_teacher(self) -> bool:
        return self.role == "teacher"


# --- 领域异常 -------------------------------------------------------------


class DomainError(Exception):
    """所有可预期的业务拒绝都继承该异常，``code`` 供 API 层映射。"""

    code = "domain_error"


class NotFoundError(DomainError):
    code = "not_found"


class DuplicateError(DomainError):
    code = "duplicate"

    def __init__(self, message: str, resource: str = "") -> None:
        super().__init__(message)
        self.resource = resource


class CapacityError(DomainError):
    code = "capacity_exceeded"


class ConsentRequiredError(DomainError):
    code = "consent_required"


class ConsentRevokedError(DomainError):
    code = "consent_revoked"


class TeacherConflictError(DomainError):
    code = "teacher_conflict"

    def __init__(self, message: str, *, teacher_id: str = "",
                 session_id: str = "", conflicting_session_id: str = "") -> None:
        super().__init__(message)
        self.teacher_id = teacher_id
        self.session_id = session_id
        self.conflicting_session_id = conflicting_session_id


class InvalidTransitionError(DomainError):
    code = "invalid_transition"


class KilnHaltedError(DomainError):
    code = "kiln_halted"


class DuplicateCheckinError(DomainError):
    code = "duplicate_checkin"

    def __init__(self, message: str, *, session_id: str = "",
                 student_id: str = "") -> None:
        super().__init__(message)
        self.session_id = session_id
        self.student_id = student_id


class PermissionDeniedError(DomainError):
    code = "permission_denied"


class QualificationError(DomainError):
    code = "teacher_not_qualified"


# --- 读模型行 -------------------------------------------------------------


@dataclass(frozen=True)
class Teacher:
    teacher_id: str
    name: str
    qualifications: tuple[str, ...] = field(default_factory=tuple)

    def qualified_for(self, stage: str) -> bool:
        return stage in self.qualifications
