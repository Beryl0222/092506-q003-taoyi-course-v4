"""护栏规则：教师冲突与资质、重复签到、作品状态机、审计完整性。"""
import json
import unittest

from taoyi_course.api import handle
from taoyi_course.domain import (
    DuplicateCheckinError, InvalidTransitionError,
    PermissionDeniedError, Principal, QualificationError,
    TeacherConflictError,
)
from taoyi_course.service import Service
from taoyi_course.store import Store
from tests.helpers import (
    STAFF, TEACHER, OTHER_TEACHER, enroll_all, new_service, publish,
    seed_student, seed_teacher,
)


class 教师安排护栏(unittest.TestCase):
    def test_时间冲突的教师安排整体回滚并留审计(self):
        service = new_service()
        seed_teacher(service, "teacher-1")
        publish("s1", "wedging", service)
        publish("s2", "throwing", service, wheel_count=4,
                start="2026-09-01T09:30:00+08:00",
                end="2026-09-01T10:30:00+08:00",
                teacher_ids=())

        with self.assertRaises(TeacherConflictError) as ctx:
            service.assign_teacher_guarded(STAFF, "s2", "teacher-1")
        self.assertEqual(ctx.exception.conflicting_session_id, "s1")

        # 回滚：s2 的教师名单里没有 teacher-1。
        teachers = [t["teacher_id"]
                    for t in service.get_session(STAFF, "s2")["teachers"]]
        self.assertNotIn("teacher-1", teachers)

        # 冲突事件仍有审计（在业务事务回滚后补写）。
        conflicts = service.list_audit(STAFF,
                                       action="session.teacher_conflict")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(
            conflicts[0]["detail"]["conflicting_session_id"], "s1")

        # 取消的课次不再占用教师时间：s1 取消后可安排到 s2。
        service.cancel_session(STAFF, "s1")
        service.assign_teacher(STAFF, "s2", "teacher-1")
        self.assertIn(
            "teacher-1",
            [t["teacher_id"]
             for t in service.get_session(STAFF, "s2")["teachers"]])

    def test_不具备阶段资质不能安排(self):
        service = new_service()
        seed_teacher(service, "teacher-1", qualifications=("wedging",))
        publish("s-throw", "throwing", service, wheel_count=2,
                teacher_ids=())
        with self.assertRaises(QualificationError):
            service.assign_teacher(STAFF, "s-throw", "teacher-1")

    def test_未安排到课次的教师不能签到(self):
        service = new_service()
        seed_teacher(service, "teacher-1")
        seed_teacher(service, "teacher-2")
        publish("s1", "wedging", service, teacher_ids=("teacher-1",))
        seed_student(service, "stu-1")
        service.enroll(STAFF, "s1", "stu-1")
        with self.assertRaises(PermissionDeniedError):
            service.checkin(OTHER_TEACHER, "s1", "stu-1")


class 签到与状态机护栏(unittest.TestCase):
    def _ready(self):
        service = new_service()
        seed_teacher(service)
        publish("s1", "wedging", service)
        seed_student(service, "stu-1")
        service.enroll(STAFF, "s1", "stu-1")
        return service

    def test_重复签到被拒绝_出勤记录不变_审计可查(self):
        service = self._ready()
        first = service.checkin_guarded(TEACHER, "s1", "stu-1")
        self.assertEqual(first["attendance"], "present")

        with self.assertRaises(DuplicateCheckinError):
            service.checkin_guarded(TEACHER, "s1", "stu-1")

        # 仍然只有一次签到记录。
        self.assertIsNotNone(
            service.store.get_attendance("s1", "stu-1"))
        dup_audits = service.list_audit(
            STAFF, action="session.checkin_duplicate")
        self.assertEqual(len(dup_audits), 1)
        checkins = service.list_audit(STAFF, action="session.checkin")
        self.assertEqual(len(checkins), 1)

    def test_已签到不能改记请假(self):
        service = self._ready()
        service.checkin(TEACHER, "s1", "stu-1")
        with self.assertRaises(DuplicateCheckinError):
            service.mark_absence(TEACHER, "s1", "stu-1")

    def test_作品不能跳阶段流转(self):
        service = self._ready()
        service.issue_clay(TEACHER, "s1", "stu-1", artifact_id="art-1")
        with self.assertRaises(InvalidTransitionError):
            service.transition_artifact(TEACHER, "art-1", "trim")
        with self.assertRaises(InvalidTransitionError):
            service.transition_artifact(TEACHER, "art-1", "load_kiln")

    def test_责任链完整记录每次交接(self):
        service = self._ready()
        service.issue_clay(TEACHER, "s1", "stu-1", artifact_id="art-1")
        service.transition_artifact(TEACHER, "art-1", "throw",
                                    custodian_id="teacher-1")
        view = service.get_artifact(STAFF, "art-1")
        self.assertEqual([c["action"] for c in view["custody_chain"]],
                         ["issue_clay", "throw"])
        self.assertEqual(view["custody_chain"][1]["custodian_id"],
                         "teacher-1")


class 审计与API适配(unittest.TestCase):
    def test_审计只对教务员开放(self):
        service = self._ready_like()
        with self.assertRaises(PermissionDeniedError):
            service.list_audit(TEACHER)

    def _ready_like(self):
        service = new_service()
        seed_teacher(service)
        publish("s1", "wedging", service)
        seed_student(service, "stu-1")
        service.enroll(STAFF, "s1", "stu-1")
        return service

    def test_api_领域错误映射为错误信封(self):
        service = Service(Store())
        payload = json.dumps({
            "action": "enroll",
            "actor": {"user_id": "staff-1", "role": "staff"},
            "session_id": "missing", "student_id": "stu-x"})
        body = json.loads(handle(payload, service))
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "not_found")

    def test_api_完整流程_停窑返回kiln_halted码(self):
        service = new_service()
        seed_teacher(service)
        publish("s1", "wedging", service)
        seed_student(service, "stu-1")
        service.enroll(STAFF, "s1", "stu-1")
        service.issue_clay(TEACHER, "s1", "stu-1", artifact_id="art-1")
        for action in ("throw", "trim", "hand_over"):
            service.transition_artifact(TEACHER, "art-1", action)
        service.register_kiln(STAFF, "kiln-1", "一号窑")
        service.halt_kiln(STAFF, "kiln-1")

        payload = json.dumps({
            "action": "kiln.load",
            "actor": {"user_id": "teacher-1", "role": "teacher"},
            "kiln_id": "kiln-1", "artifact_ids": ["art-1"]})
        body = json.loads(handle(payload, service))
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "kiln_halted")

    def test_api_学生越权查看作品被拒(self):
        service = self._ready_like()
        service.issue_clay(TEACHER, "s1", "stu-1", artifact_id="art-1")
        payload = json.dumps({
            "action": "artifact.get",
            "actor": {"user_id": "stu-2", "role": "student",
                      "student_id": "stu-2"},
            "artifact_id": "art-1"})
        body = json.loads(handle(payload, service))
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "permission_denied")

    def test_api_作品流转用transition字段_审计用filter_action字段(self):
        service = self._ready_like()
        service.issue_clay(TEACHER, "s1", "stu-1", artifact_id="art-1")
        body = json.loads(handle(json.dumps({
            "action": "artifact.transition",
            "actor": {"user_id": "teacher-1", "role": "teacher"},
            "artifact_id": "art-1", "transition": "throw"}), service))
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"]["state"], "thrown")

        audit_body = json.loads(handle(json.dumps({
            "action": "audit.list",
            "actor": {"user_id": "staff-1", "role": "staff"},
            "filter_action": "artifact.transition"}), service))
        self.assertTrue(audit_body["ok"])
        self.assertTrue(
            all(a["action"] == "artifact.transition"
                for a in audit_body["result"]))


if __name__ == "__main__":
    unittest.main()
