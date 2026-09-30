"""验收场景一/二：同一课次并发报名、监护同意撤回。"""
import unittest
from concurrent.futures import ThreadPoolExecutor

from taoyi_course.domain import (
    ConsentRequiredError, DuplicateError, Principal,
)
from tests.helpers import (
    STAFF, TEACHER, enroll_all, guardian_principal, new_service,
    publish, seed_student, seed_students, seed_teacher, student_principal,
)


class 并发报名验收(unittest.TestCase):
    def test_并发报名不超卖_满员自动候补(self):
        service = new_service()
        seed_teacher(service)
        publish("s-wedge", "wedging", service, capacity=2)
        ids = [f"stu-{i}" for i in range(8)]
        seed_students(service, ids)

        outcomes: list[dict] = []
        errors: list[Exception] = []

        def go(sid: str):
            try:
                outcomes.append(service.enroll(STAFF, "s-wedge", sid))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(go, ids))

        self.assertEqual(errors, [])
        enrolled = sorted(o["student_id"] for o in outcomes
                          if o["status"] == "enrolled")
        waitlisted = sorted(o["student_id"] for o in outcomes
                            if o["status"] == "waitlisted")
        self.assertEqual(len(enrolled), 2, "安全容量为 2，只能有 2 人占座")
        self.assertEqual(len(waitlisted), 6, "其余 6 人必须进候补而非报错")
        self.assertEqual(len(set(enrolled + waitlisted)), 8)

        view = service.get_session(STAFF, "s-wedge")
        self.assertEqual(view["seats_taken"], 2)
        self.assertEqual(view["seats_available"], 0)

    def test_拉坯课同时受陶轮工位约束(self):
        service = new_service()
        seed_teacher(service)
        # 安全容量 5，但只有 2 台陶轮 → 有效工位 2。
        publish("s-throw", "throwing", service, capacity=5, wheel_count=2)
        ids = [f"stu-{i}" for i in range(4)]
        seed_students(service, ids)
        results = enroll_all(service, "s-throw", ids)
        self.assertEqual(
            [r["status"] for r in results].count("enrolled"), 2)
        self.assertEqual(
            [r["status"] for r in results].count("waitlisted"), 2)
        view = service.get_session(STAFF, "s-throw")
        self.assertEqual(view["effective_capacity"], 2)

    def test_同一学生并发重复报名只成功一次(self):
        service = new_service()
        seed_teacher(service)
        publish("s-dup", "wedging", service, capacity=10)
        seed_student(service, "stu-x")

        duplicates: list[Exception] = []

        def go(_):
            try:
                service.enroll(STAFF, "s-dup", "stu-x")
            except DuplicateError as exc:
                duplicates.append(exc)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(go, range(4)))

        self.assertEqual(len(duplicates), 3, "重复报名必须被明确拒绝")
        self.assertEqual(
            service.get_session(STAFF, "s-dup")["seats_taken"], 1)

    def test_候补按队首替补_撤回后自动补位(self):
        service = new_service()
        seed_teacher(service)
        publish("s-fill", "wedging", service, capacity=1)
        ids = ["a", "b", "c"]
        seed_students(service, ids)
        enroll_all(service, "s-fill", ids)
        self.assertEqual(
            service.get_session(STAFF, "s-fill")["waitlist"][0]
            ["student_id"], "b")

        result = service.withdraw_enrollment(STAFF, "s-fill", "a")
        self.assertEqual(result["promoted"]["student_id"], "b")
        view = service.get_session(STAFF, "s-fill")
        self.assertEqual(view["enrolled"], ["b"])
        self.assertEqual([w["student_id"] for w in view["waitlist"]], ["c"])

    def test_缺少监护同意不能报名(self):
        service = new_service()
        seed_teacher(service)
        publish("s-consent", "wedging", service)
        seed_student(service, "stu-no-consent", consent=False)
        with self.assertRaises(ConsentRequiredError):
            service.enroll(STAFF, "s-consent", "stu-no-consent")


class 监护同意撤回验收(unittest.TestCase):
    def test_撤回同意_已交出作品归属冻结并可继续烧制(self):
        service = new_service()
        seed_teacher(service)
        publish("s1", "wedging", service, capacity=1)
        seed_student(service, "owner")
        seed_student(service, "waiter")
        enroll_all(service, "s1", ["owner", "waiter"])
        service.issue_clay(TEACHER, "s1", "owner", artifact_id="art-1")
        service.transition_artifact(TEACHER, "art-1", "throw")
        service.transition_artifact(TEACHER, "art-1", "trim")
        service.transition_artifact(TEACHER, "art-1", "hand_over")

        result = service.withdraw_consent(
            guardian_principal("owner"), "owner")

        # 报名被撤回、候补自动替补……
        self.assertIn("s1", result["withdrawn_sessions"])
        self.assertEqual(result["promotions"][0]["student_id"], "waiter")
        # ……但已交出的作品没有失去归属，状态仍是 handed_over。
        self.assertEqual(result["held_artifacts"], ["art-1"])
        view = service.get_artifact(STAFF, "art-1")
        self.assertEqual(view["owner_student_id"], "owner")
        self.assertEqual(view["state"], "handed_over")
        actions = [c["action"] for c in view["custody_chain"]]
        self.assertIn("consent_withdrawn_hold", actions)

        # 重新授予同意不影响作品；作品继续走完入窑与烧制，归属不变。
        service.grant_consent(guardian_principal("owner"), "owner")
        service.register_kiln(STAFF, "kiln-1", "一号窑")
        service.load_kiln(TEACHER, "kiln-1", ["art-1"], batch_id="b-1")
        service.start_firing(TEACHER, "b-1")
        service.complete_firing(TEACHER, "b-1")
        fired = service.get_artifact(STAFF, "art-1")
        self.assertEqual(fired["state"], "fired")
        self.assertEqual(fired["owner_student_id"], "owner")

    def test_撤回同意_在制未交出作品归还学生(self):
        service = new_service()
        seed_teacher(service)
        publish("s1", "wedging", service)
        seed_student(service, "owner")
        enroll_all(service, "s1", ["owner"])
        service.issue_clay(TEACHER, "s1", "owner", artifact_id="art-raw")
        service.transition_artifact(TEACHER, "art-raw", "throw")

        result = service.withdraw_consent(
            guardian_principal("owner"), "owner")
        self.assertEqual(result["returned_artifacts"], ["art-raw"])
        view = service.get_artifact(STAFF, "art-raw")
        self.assertEqual(view["state"], "returned")
        self.assertEqual(view["owner_student_id"], "owner")

    def test_撤回后不能签到_重新同意后可再报名(self):
        service = new_service()
        seed_teacher(service)
        publish("s1", "wedging", service, capacity=1)
        seed_student(service, "owner")
        seed_student(service, "waiter")
        enroll_all(service, "s1", ["owner", "waiter"])

        service.withdraw_consent(guardian_principal("owner"), "owner")
        from taoyi_course.domain import ConsentRevokedError, DomainError
        # 撤回后报名已退出名单，签到以“不在名单”被明确拒绝（而非误签成功）。
        with self.assertRaises((ConsentRevokedError, DomainError)):
            service.checkin(TEACHER, "s1", "owner")

        # 重新同意后允许重新报名（此前撤回记录不应挡住）。
        service.grant_consent(guardian_principal("owner"), "owner")
        again = service.enroll(STAFF, "s1", "owner")
        self.assertEqual(again["status"], "waitlisted",
                         "空位已由 waiter 替补，owner 只能进候补")

    def test_非监护人不能撤回同意(self):
        service = new_service()
        seed_teacher(service)
        publish("s1", "wedging", service)
        seed_student(service, "owner")
        enroll_all(service, "s1", ["owner"])
        from taoyi_course.domain import PermissionDeniedError
        other_guardian = Principal("guardian-of-other", "guardian",
                                   student_id="other")
        with self.assertRaises(PermissionDeniedError):
            service.withdraw_consent(other_guardian, "owner")


if __name__ == "__main__":
    unittest.main()
