"""验收场景三：请假/停课触发补课，补课额度只能用于同阶段的其他课次。"""
import unittest

from taoyi_course.domain import DomainError, PermissionDeniedError
from tests.helpers import (
    STAFF, TEACHER, enroll_all, new_service, publish, seed_student,
    seed_students, seed_teacher,
)


class 跨课次补课验收(unittest.TestCase):
    def test_请假获得补课额度_跨课次使用后额度核销(self):
        service = new_service()
        seed_teacher(service)
        publish("s-a", "wedging", service,
                start="2026-09-01T09:00:00+08:00",
                end="2026-09-01T10:00:00+08:00")
        publish("s-b", "wedging", service,
                start="2026-09-08T09:00:00+08:00",
                end="2026-09-08T10:00:00+08:00")
        seed_student(service, "stu-1")
        service.enroll(STAFF, "s-a", "stu-1")

        absence = service.mark_absence(TEACHER, "s-a", "stu-1",
                                       reason="sick_leave")
        credit_id = absence["makeup_credit_id"]
        self.assertTrue(credit_id)

        credit = service.list_makeup_credits(STAFF, "stu-1")[0]
        self.assertEqual(credit["state"], "granted")
        self.assertEqual(credit["stage"], "wedging")
        self.assertEqual(credit["source_session_id"], "s-a")

        # 凭额度报另一个同阶段课次。
        result = service.enroll(STAFF, "s-b", "stu-1", credit_id=credit_id)
        self.assertEqual(result["status"], "enrolled")
        self.assertEqual(result["makeup_credit_id"], credit_id)

        used = service.list_makeup_credits(STAFF, "stu-1")[0]
        self.assertEqual(used["state"], "used")
        self.assertEqual(used["target_session_id"], "s-b")

        # 同一额度不能二次使用。
        with self.assertRaises(DomainError):
            service.enroll(STAFF, "s-b", "stu-1", credit_id=credit_id)

    def test_补课额度不能跨阶段使用(self):
        service = new_service()
        seed_teacher(service)
        publish("s-a", "wedging", service)
        publish("s-throw", "throwing", service, wheel_count=4,
                start="2026-09-02T09:00:00+08:00",
                end="2026-09-02T10:00:00+08:00")
        seed_student(service, "stu-1")
        service.enroll(STAFF, "s-a", "stu-1")
        credit_id = service.mark_absence(
            TEACHER, "s-a", "stu-1")["makeup_credit_id"]

        with self.assertRaises(DomainError):
            service.enroll(STAFF, "s-throw", "stu-1", credit_id=credit_id)
        # 被拒绝后额度仍然有效，可用于同阶段课次。
        self.assertEqual(
            service.list_makeup_credits(STAFF, "stu-1")[0]["state"],
            "granted")

    def test_整课临时取消_所有在读学生获补课额度_候补不获额度(self):
        service = new_service()
        seed_teacher(service)
        publish("s-cancel", "throwing", service, capacity=1, wheel_count=1)
        seed_students(service, ["a", "b"])
        enroll_all(service, "s-cancel", ["a", "b"])

        result = service.cancel_session(STAFF, "s-cancel",
                                        reason="teacher_unavailable")
        self.assertEqual(result["status"], "cancelled")
        credited = {c["student_id"] for c in result["makeup_credits"]}
        self.assertEqual(credited, {"a"}, "仅在读学生获额度，候补 b 不获")

        publish("s-makeup", "throwing", service, wheel_count=3,
                start="2026-09-15T09:00:00+08:00",
                end="2026-09-15T10:00:00+08:00")
        credit_id = result["makeup_credits"][0]["credit_id"]
        made_up = service.enroll(STAFF, "s-makeup", "a",
                                 credit_id=credit_id)
        self.assertEqual(made_up["status"], "enrolled")

        # 已取消课次不能再报名或签到。
        with self.assertRaises(DomainError):
            service.enroll(STAFF, "s-cancel", "a")
        with self.assertRaises(DomainError):
            service.checkin(TEACHER, "s-cancel", "a")

    def test_重复请假不重复发放补课额度(self):
        service = new_service()
        seed_teacher(service)
        publish("s-a", "wedging", service)
        seed_student(service, "stu-1")
        service.enroll(STAFF, "s-a", "stu-1")
        service.mark_absence(TEACHER, "s-a", "stu-1")
        with self.assertRaises(Exception):
            service.mark_absence(TEACHER, "s-a", "stu-1")
        credits = service.list_makeup_credits(STAFF, "stu-1")
        self.assertEqual(len(credits), 1)

    def test_候补退课不产生补课额度(self):
        service = new_service()
        seed_teacher(service)
        publish("s-a", "wedging", service, capacity=1)
        seed_student(service, "a")
        seed_student(service, "b")
        service.enroll(STAFF, "s-a", "a")
        service.enroll(STAFF, "s-a", "b")  # b 进候补
        result = service.withdraw_enrollment(STAFF, "s-a", "b",
                                             grant_makeup=True)
        self.assertNotIn("makeup_credit_id", result)
        self.assertEqual(service.list_makeup_credits(STAFF, "b"), [])

    def test_补课排入满员课次被拒且额度保持有效(self):
        service = new_service()
        seed_teacher(service)
        publish("s-a", "wedging", service)
        publish("s-full", "wedging", service, capacity=1,
                start="2026-09-20T09:00:00+08:00",
                end="2026-09-20T10:00:00+08:00")
        seed_student(service, "stu-1")
        seed_student(service, "occupy")
        service.enroll(STAFF, "s-a", "stu-1")
        service.enroll(STAFF, "s-full", "occupy")
        credit_id = service.mark_absence(
            TEACHER, "s-a", "stu-1")["makeup_credit_id"]

        from taoyi_course.domain import CapacityError
        with self.assertRaises(CapacityError):
            service.enroll(STAFF, "s-full", "stu-1", credit_id=credit_id)
        # 额度未被核销。
        credit = service.list_makeup_credits(STAFF, "stu-1")[0]
        self.assertEqual(credit["state"], "granted")

    def test_他人不能查看我的补课额度(self):
        service = new_service()
        seed_teacher(service)
        publish("s-a", "wedging", service)
        seed_student(service, "stu-1")
        service.enroll(STAFF, "s-a", "stu-1")
        service.mark_absence(TEACHER, "s-a", "stu-1")
        from tests.helpers import student_principal
        with self.assertRaises(PermissionDeniedError):
            service.list_makeup_credits(student_principal("stu-1"),
                                        "someone-else")


if __name__ == "__main__":
    unittest.main()
