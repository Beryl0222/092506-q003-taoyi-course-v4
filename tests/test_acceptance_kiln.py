"""验收场景四：临时停窑与恢复后的作品状态、归属与审计记录。"""
import unittest

from taoyi_course.domain import (
    KilnHaltedError, PermissionDeniedError, Principal,
)
from tests.helpers import (
    STAFF, TEACHER, advance_to_handover, enroll_all, new_service,
    publish, seed_student, seed_teacher, student_principal,
)


class 窑炉停窑恢复验收(unittest.TestCase):
    def _world(self):
        service = new_service()
        seed_teacher(service)
        publish("s1", "wedging", service, capacity=4)
        seed_student(service, "stu-1")
        seed_student(service, "stu-2")
        enroll_all(service, "s1", ["stu-1", "stu-2"])
        advance_to_handover(service, "s1", "stu-1", "art-1")
        advance_to_handover(service, "s1", "stu-2", "art-2")
        service.register_kiln(STAFF, "kiln-1", "一号窑")
        return service

    def test_停窑期间装窑被拒且整体回滚_恢复后状态连续(self):
        service = self._world()

        service.halt_kiln(STAFF, "kiln-1", reason="element_maintenance")

        # 两件作品一起装窑被整体拒绝——不会出现一件入窑、一件留在教师手。
        with self.assertRaises(KilnHaltedError):
            service.load_kiln(TEACHER, "kiln-1", ["art-1", "art-2"],
                              batch_id="b-blocked")

        for artifact_id in ("art-1", "art-2"):
            view = service.get_artifact(STAFF, artifact_id)
            self.assertEqual(view["state"], "handed_over",
                             "停窑拒绝必须回滚，作品仍由教师保管")
            self.assertEqual(view["owner_student_id"],
                             "stu-1" if artifact_id == "art-1" else "stu-2")

        # 被拒绝的装窑尝试仍有审计可追溯。
        blocked = service.list_audit(STAFF, action="kiln.blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["detail"]["reason"], "halted")
        self.assertEqual(
            sorted(blocked[0]["detail"]["artifact_ids"]),
            ["art-1", "art-2"])

        # 恢复后原批作品原样装窑、开烧、完成，状态链路完整。
        service.resume_kiln(STAFF, "kiln-1", note="维护完成")
        service.load_kiln(TEACHER, "kiln-1", ["art-1", "art-2"],
                          batch_id="b-1")
        for artifact_id in ("art-1", "art-2"):
            self.assertEqual(service.get_artifact(
                STAFF, artifact_id)["state"], "kiln_loaded")

        service.start_firing(TEACHER, "b-1")
        service.complete_firing(TEACHER, "b-1")

        for artifact_id, owner in (("art-1", "stu-1"),
                                   ("art-2", "stu-2")):
            view = service.get_artifact(STAFF, artifact_id)
            self.assertEqual(view["state"], "fired")
            self.assertEqual(view["owner_student_id"], owner,
                             "烧制全程归属不可改变")
            states = [c["to_state"] for c in view["custody_chain"]]
            self.assertEqual(states, [
                "clay_issued", "thrown", "trimmed", "handed_over",
                "kiln_loaded", "firing", "fired"])

        audit_actions = [a["action"] for a in service.list_audit(
            STAFF, entity_id="kiln-1")]
        self.assertIn("kiln.halt", audit_actions)
        self.assertIn("kiln.resume", audit_actions)

    def test_停窑不影响窑内已有作品_恢复后继续烧制(self):
        service = self._world()
        service.load_kiln(TEACHER, "kiln-1", ["art-1"], batch_id="b-1")
        service.start_firing(TEACHER, "b-1")

        service.halt_kiln(STAFF, "kiln-1", reason="power_outage")
        # 烧制中的批次与作品状态原样保留。
        self.assertEqual(service.get_artifact(STAFF, "art-1")["state"],
                         "firing")
        with self.assertRaises(KilnHaltedError):
            service.load_kiln(TEACHER, "kiln-1", ["art-2"],
                              batch_id="b-2")

        service.resume_kiln(STAFF, "kiln-1")
        service.complete_firing(TEACHER, "b-1")
        self.assertEqual(service.get_artifact(STAFF, "art-1")["state"],
                         "fired")
        # 恢复后新批次可正常装窑。
        service.load_kiln(TEACHER, "kiln-1", ["art-2"], batch_id="b-2")
        self.assertEqual(service.get_artifact(STAFF, "art-2")["state"],
                         "kiln_loaded")

    def test_未交接作品不能装窑_整批回滚(self):
        service = self._world()
        # art-2 已 handed_over；再造一件停在修坯阶段的作品。
        seed_student(service, "stu-3")
        service.enroll(STAFF, "s1", "stu-3")
        service.issue_clay(TEACHER, "s1", "stu-3", artifact_id="art-3")
        service.transition_artifact(TEACHER, "art-3", "throw")
        service.transition_artifact(TEACHER, "art-3", "trim")

        with self.assertRaises(Exception):
            service.load_kiln(TEACHER, "kiln-1", ["art-2", "art-3"],
                              batch_id="b-bad")
        self.assertEqual(service.get_artifact(STAFF, "art-2")["state"],
                         "handed_over", "整批失败，第一件也要回滚")
        self.assertEqual(service.get_artifact(STAFF, "art-3")["state"],
                         "trimmed")

    def test_学生只能查看自己被授权的作品(self):
        service = self._world()
        # 本人可看（含完整责任链）。
        mine = service.get_artifact(student_principal("stu-1"), "art-1")
        self.assertEqual(mine["owner_student_id"], "stu-1")
        # 不能看别人的作品。
        with self.assertRaises(PermissionDeniedError):
            service.get_artifact(student_principal("stu-1"), "art-2")
        # 列表接口也只返回本人作品。
        mine_list = service.list_artifacts_for(
            student_principal("stu-1"), "stu-1")
        self.assertEqual([a["artifact_id"] for a in mine_list], ["art-1"])
        with self.assertRaises(PermissionDeniedError):
            service.list_artifacts_for(student_principal("stu-1"), "stu-2")
        # 无身份拒绝。
        with self.assertRaises(PermissionDeniedError):
            service.get_artifact(None, "art-1")


if __name__ == "__main__":
    unittest.main()
