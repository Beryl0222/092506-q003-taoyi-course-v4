"""测试共用的场景搭建辅助。"""
from taoyi_course.domain import Principal, STAGES
from taoyi_course.service import Service
from taoyi_course.store import Store

STAFF = Principal("staff-1", "staff")
TEACHER = Principal("teacher-1", "teacher")
OTHER_TEACHER = Principal("teacher-2", "teacher")


def student_principal(student_id: str) -> Principal:
    return Principal(student_id, "student", student_id=student_id)


def guardian_principal(student_id: str) -> Principal:
    return Principal(f"guardian-of-{student_id}", "guardian",
                     student_id=student_id)


def new_service() -> Service:
    return Service(Store())


def seed_teacher(service: Service, teacher_id: str = "teacher-1",
                 qualifications=STAGES) -> None:
    service.register_teacher(STAFF, teacher_id, f"教师{teacher_id}",
                             list(qualifications))


def seed_student(service: Service, student_id: str, *,
                 consent: bool = True, name: str | None = None) -> None:
    service.register_student(STAFF, student_id, name or f"学生{student_id}")
    if consent:
        service.grant_consent(guardian_principal(student_id), student_id)


def seed_students(service: Service, ids, *, consent: bool = True) -> None:
    for sid in ids:
        seed_student(service, sid, consent=consent)


def publish(session_id: str, stage: str, service: Service, *,
            capacity: int = 10, wheel_count: int = 0,
            start: str = "2026-09-01T09:00:00+08:00",
            end: str = "2026-09-01T10:00:00+08:00",
            teacher_ids=("teacher-1",)) -> dict:
    return service.publish_session(
        STAFF, stage, start, end, capacity, wheel_count=wheel_count,
        session_id=session_id, teacher_ids=list(teacher_ids))


def enroll_all(service: Service, session_id: str, student_ids) -> list[dict]:
    return [service.enroll(STAFF, session_id, sid) for sid in student_ids]


def advance_to_handover(service: Service, session_id: str, student_id: str,
                        artifact_id: str) -> dict:
    """领泥 → 拉坯 → 修坯 → 交给教师，返回 handed_over 的作品视图。"""
    service.issue_clay(TEACHER, session_id, student_id,
                       artifact_id=artifact_id, name="陶罐")
    service.transition_artifact(TEACHER, artifact_id, "throw")
    service.transition_artifact(TEACHER, artifact_id, "trim")
    return service.transition_artifact(TEACHER, artifact_id, "hand_over")
