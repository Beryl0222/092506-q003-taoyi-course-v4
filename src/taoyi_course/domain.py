"""课次与作品交接的基础领域对象。

阶段（揉泥 → 拉坯 → 修坯 → …）与作品生命周期都在这里集中定义，
服务层、存储层和 JSON 适配层共用同一套字符串，避免各层拼写漂移。
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# 课程阶段：一个课次只对应其中一个阶段；完整作品需要依次走完三种课次。
# ---------------------------------------------------------------------------
STAGE_KNEADING = "kneading"   # 揉泥
STAGE_THROWING = "throwing"   # 拉坯（需要陶轮）
STAGE_TRIMMING = "trimming"   # 修坯（需要陶轮）
STAGES = (STAGE_KNEADING, STAGE_THROWING, STAGE_TRIMMING)

# 阶段推进顺序：揉泥 → 拉坯 → 修坯
STAGE_ORDER = {STAGE_KNEADING: 0, STAGE_THROWING: 1, STAGE_TRIMMING: 2}

# 哪些阶段需要占用陶轮
WHEEL_STAGES = frozenset({STAGE_THROWING, STAGE_TRIMMING})


# ---------------------------------------------------------------------------
# 课次状态
# ---------------------------------------------------------------------------
SESSION_SCHEDULED = "scheduled"   # 已发布、报名中
SESSION_IN_PROGRESS = "in_progress"
SESSION_COMPLETED = "completed"
SESSION_CANCELLED = "cancelled"   # 整次取消（如教师冲突无法调整），触发回滚/补课

# ---------------------------------------------------------------------------
# 报名状态
# ---------------------------------------------------------------------------
ENROLLED = "enrolled"         # 已占席
WAITLISTED = "waitlisted"     # 候补队列中
ATTENDED = "attended"         # 已签到（不可重复）
ABSENT = "absent"             # 请假（释放席位，进入候补补位）
WITHDRAWN = "withdrawn"       # 监护同意撤回，退出后续课程
MOVED = "moved"               # 已转到补课课次，原席位释放
CANCELLED_ENROLLMENT = "cancelled"  # 课次取消时候补记录作废

# ---------------------------------------------------------------------------
# 作品状态：领泥 → 揉泥 → 拉坯 → 修坯 → 入窑 → 烧制
# ---------------------------------------------------------------------------
ART_CLAY_ISSUED = "clay_issued"   # 领泥
ART_KNEAD_DONE = "knead_done"     # 揉泥完成
ART_THROWN = "thrown"             # 拉坯成型
ART_TRIMMED = "trimmed"           # 修坯完成
ART_LOADED = "loaded"             # 已入窑（随批次）
ART_FIRED = "fired"               # 烧制完成
ART_RETURNED = "returned"         # 已发还
ART_SCRAPPED = "scrapped"         # 作品损毁/作废（须记录原因）

# 作品状态的标准推进顺序
ARTWORK_FLOW = (
    ART_CLAY_ISSUED,
    ART_KNEAD_DONE,
    ART_THROWN,
    ART_TRIMMED,
    ART_LOADED,
    ART_FIRED,
    ART_RETURNED,
)

# 阶段产出的作品里程碑
STAGE_ART_STATE = {
    STAGE_KNEADING: ART_KNEAD_DONE,
    STAGE_THROWING: ART_THROWN,
    STAGE_TRIMMING: ART_TRIMMED,
}

# 进入某状态前作品必须已经具备的前置状态
ARTWORK_PREREQ = {
    ART_CLAY_ISSUED: None,
    ART_KNEAD_DONE: ART_CLAY_ISSUED,
    ART_THROWN: ART_KNEAD_DONE,
    ART_TRIMMED: ART_THROWN,
    ART_LOADED: ART_TRIMMED,
    ART_FIRED: ART_LOADED,
    ART_RETURNED: ART_FIRED,
}

# ---------------------------------------------------------------------------
# 窑炉 / 烧制批次状态
# ---------------------------------------------------------------------------
KILN_ACTIVE = "active"       # 可正常装窑
KILN_PAUSED = "paused"       # 临时停窑：未入窑作品禁止装窑，在窑作品保持归属
KILN_DECOMMISSIONED = "decommissioned"

BATCH_READY = "ready"         # 已建批次、可继续装载
BATCH_LOADED = "loaded"       # 满窑/封窑，烧制中
BATCH_FIRING = "firing"
BATCH_FIRED = "fired"        # 烧制成品出窑
BATCH_HELD = "held"          # 停窑时烧制中断，挂起等待窑炉恢复


# ---------------------------------------------------------------------------
# 责任交接动作（作品 custody_transfer 的动作词汇）
# ---------------------------------------------------------------------------
HANDOFF_ISSUE_CLAY = "issue_clay"     # 教师向学生发泥
HANDOFF_KNEAD = "complete_knead"
HANDOFF_THROW = "complete_throw"
HANDOFF_TRIM = "complete_trim"
HANDOFF_LOAD = "load_kiln"            # 送入窑炉：保管人变为窑炉
HANDOFF_FIRE = "fire"                 # 烧制完成：随批次出窑
HANDOFF_RETURN = "return"             # 发还学生/监护人
HANDOFF_SCRAP = "scrap"               # 损毁作废
HANDOFF_HOLD = "hold_for_kiln"        # 停窑挂起
HANDOFF_RESUME = "resume_kiln"        # 窑炉恢复后继续


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
ROLE_STAFF = "staff"        # 教务员
ROLE_TEACHER = "teacher"    # 指导教师
ROLE_STUDENT = "student"    # 学生（含其监护人，仅能看授权作品）
ROLE_KILN_KEEPER = "kiln_keeper"  # 窑炉管理员

STUDENT_ROLES = frozenset({ROLE_STUDENT})


class DomainError(Exception):
    """所有业务校验失败的基类，消息直接面向调用方。"""

    code = "domain_error"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    """席位已满、时间冲突、状态机非法、重复签到等可预期冲突。"""

    code = "conflict"


class PermissionDeniedError(DomainError):
    code = "permission_denied"


class ConsentRequiredError(DomainError):
    """监护同意缺失或已撤回时拒绝占席/继续操作。"""

    code = "consent_required"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Record:
    """早期骨架保留下来的通用记录，register/find 仍在使用。"""

    record_id: str
    owner_id: str
    state: str = "draft"
    created_at: str = ""

    def with_timestamp(self) -> "Record":
        return Record(self.record_id, self.owner_id, self.state,
                      self.created_at or now_iso())


@dataclass
class AuditEvent:
    audit_id: int | None
    at: str
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    detail: dict = field(default_factory=dict)
