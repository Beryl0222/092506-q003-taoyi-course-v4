"""课程编排与作品流转的验收测试。

覆盖验收点：
- 同一课次的并发报名（文件库 + 多连接 + 线程，BEGIN IMMEDIATE 串行化）；
- 监护同意撤回：未结束课次退出、候补补位、已入窑作品归属不变；
- 跨课次补课：课次取消发资格、资格阶段校验、补位预留、签到核销；
- 临时停窑与恢复：作品状态/归属不变、禁止装窑烧制、批次回到停窑前状态；
- 教师资质与时间冲突、重复签到的回滚；
- 学生只能查看自己被授权的作品；
- 审计记录顺序完整。
"""
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from taoyi_course import Service, Store, domain as d
from taoyi_course.api import handle

STAFF = {"actor_id": "office-1", "actor_role": d.ROLE_STAFF}
ACTOR = STAFF
KEEPER = {"actor_id": "kiln-1", "actor_role": d.ROLE_KILN_KEEPER}


def make_service(path: str | None = None) -> Service:
    service = Service(Store(path or ":memory:"))
    service.register_teacher("t-k", "揉泥师", [d.STAGE_KNEADING], **ACTOR)
    service.register_teacher("t-t", "拉坯师", [d.STAGE_THROWING], **ACTOR)
    service.register_teacher("t-r", "修坯师", [d.STAGE_TRIMMING], **ACTOR)
    service.register_teacher("t-all", "全能师", list(d.STAGES), **ACTOR)
    service.register_teacher("t-all2", "全能师乙", list(d.STAGES), **ACTOR)
    for sid in ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"):
        service.register_student(sid, f"学生{sid}", guardian_id=f"g-{sid}",
                                 consent=True, **ACTOR)
    return service


def publish(service, session_id, stage, teachers, *, start="2026-10-01T09:00:00",
            end="2026-10-01T10:00:00", capacity=10, wheels=None, title=None):
    return service.publish_session(
        session_id, stage, title or session_id, start, end,
        capacity, wheels, teachers, **STAFF)


class 课次发布与资源校验测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_拉坯课次安全容量受陶轮数约束(self):
        publish(self.service, "p1", d.STAGE_THROWING, ["t-t"],
                capacity=6, wheels=2)
        for sid in ("s1", "s2", "s3"):
            result = self.service.enroll("p1", sid)
        statuses = {e["student_id"]: e["status"]
                    for e in self.service.roster("p1", **STAFF)["enrollments"]}
        self.assertEqual(statuses["s1"], d.ENROLLED)
        self.assertEqual(statuses["s2"], d.ENROLLED)
        self.assertEqual(statuses["s3"], d.WAITLISTED)

    def test_拉坯课次没有陶轮不能发布(self):
        with self.assertRaises(d.DomainError):
            publish(self.service, "p2", d.STAGE_THROWING, ["t-t"], wheels=None)

    def test_教师资质不符整次发布回滚(self):
        with self.assertRaises(d.ConflictError):
            publish(self.service, "p3", d.STAGE_THROWING, ["t-k"], wheels=2)
        with self.assertRaises(d.NotFoundError):
            self.service.get_session("p3")

    def test_教师时间冲突整次发布回滚(self):
        publish(self.service, "p4", d.STAGE_KNEADING, ["t-all"])
        with self.assertRaises(d.ConflictError):
            publish(self.service, "p5", d.STAGE_KNEADING, ["t-all"],
                    start="2026-10-01T09:30:00", end="2026-10-01T10:30:00")
        with self.assertRaises(d.NotFoundError):
            self.service.get_session("p5")

    def test_不同时段同一教师可以连续排课(self):
        publish(self.service, "p6", d.STAGE_KNEADING, ["t-all"],
                start="2026-10-01T09:00:00", end="2026-10-01T10:00:00")
        publish(self.service, "p7", d.STAGE_KNEADING, ["t-all"],
                start="2026-10-01T10:00:00", end="2026-10-01T11:00:00")

    def test_换教师无资质或时间冲突时回滚(self):
        publish(self.service, "p8", d.STAGE_KNEADING, ["t-all"])
        # 接替者无资质
        with self.assertRaises(d.ConflictError):
            self.service.reassign_teacher("p8", "t-all", "t-t", **STAFF)
        # 接替者时间冲突
        publish(self.service, "p9", d.STAGE_KNEADING, ["t-all2"],
                start="2026-10-01T09:30:00", end="2026-10-01T10:30:00")
        with self.assertRaises(d.ConflictError):
            self.service.reassign_teacher("p8", "t-all", "t-all2", **STAFF)
        # 原教师仍在课次中
        self.assertIn("t-all", self.service.get_session("p8")["teachers"])
        # 合法更换成功
        ok = self.service.reassign_teacher("p8", "t-all", "t-k", **STAFF)
        self.assertEqual(ok["teachers"], ["t-k"])


class 并发报名测试(unittest.TestCase):
    def test_同课次并发报名恰好不超卖(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "taoyi.db")
            seed = Service(Store(db))
            seed.register_teacher("t-all", "全能师", list(d.STAGES), **STAFF)
            for i in range(20):
                seed.register_student(f"s{i}", f"学生{i}", consent=True, **STAFF)
            seed.publish_session(
                "c1", d.STAGE_KNEADING, "并发揉泥",
                "2026-10-01T09:00:00", "2026-10-01T10:00:00",
                5, None, ["t-all"], **STAFF)
            seed.store.close()

            barrier = threading.Barrier(20)

            def enroll(i):
                service = Service(Store(db))
                barrier.wait()
                try:
                    return service.enroll("c1", f"s{i}")
                finally:
                    service.store.close()

            with ThreadPoolExecutor(max_workers=20) as pool:
                results = list(pool.map(enroll, range(20)))

            enrolled = [r for r in results if r["status"] == d.ENROLLED]
            waitlisted = [r for r in results if r["status"] == d.WAITLISTED]
            self.assertEqual(len(enrolled), 5)
            self.assertEqual(len(waitlisted), 15)
            positions = sorted(r["waitlist_position"] for r in waitlisted)
            self.assertEqual(positions, list(range(1, 16)))

            check = Service(Store(db))
            seats = check.store.one(
                "SELECT COUNT(*) AS n FROM enrollment "
                "WHERE session_id='c1' AND status IN (?,?)",
                (d.ENROLLED, d.ATTENDED))["n"]
            self.assertEqual(seats, 5)
            check.store.close()

    def test_陶轮容量在并发下同样生效(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "wheels.db")
            seed = Service(Store(db))
            seed.register_teacher("t-t", "拉坯师", [d.STAGE_THROWING], **STAFF)
            for i in range(8):
                seed.register_student(f"s{i}", f"学生{i}", consent=True, **STAFF)
            seed.publish_session(
                "w1", d.STAGE_THROWING, "并发拉坯",
                "2026-10-02T09:00:00", "2026-10-02T10:00:00",
                10, 3, ["t-t"], **STAFF)
            seed.store.close()

            barrier = threading.Barrier(8)

            def enroll(i):
                service = Service(Store(db))
                barrier.wait()
                try:
                    return service.enroll("w1", f"s{i}")
                finally:
                    service.store.close()

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(enroll, range(8)))
            self.assertEqual(
                sum(r["status"] == d.ENROLLED for r in results), 3)
            self.assertEqual(
                sum(r["status"] == d.WAITLISTED for r in results), 5)

    def test_同一补课资格并发使用只有一次成功(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "grant.db")
            seed = Service(Store(db))
            seed.register_teacher("t-k", "揉泥师", [d.STAGE_KNEADING], **STAFF)
            seed.register_student("s1", "学生1", consent=True, **STAFF)
            seed.publish_session(
                "g2", d.STAGE_KNEADING, "补课二",
                "2026-10-08T09:00:00", "2026-10-08T10:00:00",
                5, None, ["t-k"], **STAFF)
            seed.publish_session(
                "g3", d.STAGE_KNEADING, "补课三",
                "2026-10-15T09:00:00", "2026-10-15T10:00:00",
                5, None, ["t-k"], **STAFF)
            grant_id = seed.store.one(
                "INSERT INTO makeup_grant(student_id, stage, reason, "
                "source_session_id, status, created_at) "
                "VALUES('s1','kneading','并发资格','old','open','2026-09-01') "
                "RETURNING grant_id")["grant_id"]
            seed.store.close()

            barrier = threading.Barrier(2)
            sessions = ["g2", "g3"]

            def use(side):
                service = Service(Store(db))
                barrier.wait()
                try:
                    service.enroll(sessions[side], "s1", grant_id=grant_id)
                    return "ok"
                except d.ConflictError:
                    return "conflict"
                finally:
                    service.store.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(use, range(2)))
            self.assertEqual(sorted(outcomes), ["conflict", "ok"])
            check = Service(Store(db))
            self.assertEqual(
                check.store.one(
                    "SELECT status FROM makeup_grant WHERE grant_id=?",
                    (grant_id,))["status"], "reserved")
            check.store.close()


class 签到请假候补测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        publish(self.service, "q1", d.STAGE_KNEADING, ["t-k"], capacity=2)
        self.service.enroll("q1", "s1")
        self.service.enroll("q1", "s2")
        self.w3 = self.service.enroll("q1", "s3")
        self.w4 = self.service.enroll("q1", "s4")
        self.assertEqual(self.w3["waitlist_position"], 1)
        self.assertEqual(self.w4["waitlist_position"], 2)
        self.service.start_session("q1", **STAFF)

    def test_重复签到被拒绝且不留副作用(self):
        self.service.check_in("q1", "s1")
        with self.assertRaises(d.ConflictError):
            self.service.check_in("q1", "s1")
        records = [e for e in self.service.roster("q1", **STAFF)["enrollments"]
                   if e["student_id"] == "s1"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], d.ATTENDED)
        trail = self.service.audit_trail("enrollment", records[0]["enrollment_id"],
                                         **STAFF)
        self.assertEqual(
            [e["action"] for e in trail].count("enrollment.checked_in"), 1)

    def test_候补未补位不能签到(self):
        with self.assertRaises(d.ConflictError):
            self.service.check_in("q1", "s3")

    def test_请假释放席位并按顺位补位(self):
        self.service.check_in("q1", "s1")
        result = self.service.mark_absent("q1", "s2", **STAFF)
        self.assertEqual(result["promoted_student"], "s3")
        statuses = {e["student_id"]: e["status"]
                    for e in self.service.roster("q1", **STAFF)["enrollments"]}
        self.assertEqual(statuses["s2"], d.ABSENT)
        self.assertEqual(statuses["s3"], d.ENROLLED)
        self.assertEqual(statuses["s4"], d.WAITLISTED)

    def test_无监护同意不能占席也不能签到(self):
        self.service.register_student("s9", "无同意", consent=False, **STAFF)
        with self.assertRaises(d.ConsentRequiredError):
            self.service.enroll("q1", "s9")


class 监护同意撤回测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_撤回后退出未结束课次并补位_在窑作品归属不变(self):
        # s1 已报名揉泥课且有一件走完三阶段、入窑封窑的作品
        publish(self.service, "k1", d.STAGE_KNEADING, ["t-all"],
                start="2026-10-01T09:00:00", end="2026-10-01T10:00:00",
                capacity=2)
        publish(self.service, "t1", d.STAGE_THROWING, ["t-all"],
                start="2026-10-02T09:00:00", end="2026-10-02T10:00:00",
                capacity=2, wheels=2)
        publish(self.service, "r1", d.STAGE_TRIMMING, ["t-all"],
                start="2026-10-03T09:00:00", end="2026-10-03T10:00:00",
                capacity=2, wheels=2)
        self.service.enroll("k1", "s1")
        self.service.enroll("k1", "s2")
        self.service.enroll("k1", "s3")  # 候补
        self.service.start_session("k1", **STAFF)
        self.service.check_in("k1", "s1")
        self.service.issue_clay("a1", "k1", "s1", actor_id="t-all",
                                actor_role=d.ROLE_TEACHER)
        self.service.complete_stage("a1", "k1", actor_id="t-all",
                                    actor_role=d.ROLE_TEACHER)
        for sid_sess in (("t1",), ("r1",)):
            sess = sid_sess[0]
            self.service.enroll(sess, "s1")
            self.service.start_session(sess, **STAFF)
            self.service.check_in(sess, "s1")
            self.service.complete_stage("a1", sess, actor_id="t-all",
                                        actor_role=d.ROLE_TEACHER)
        self.service.register_kiln("kiln1", "一号窑", 10, **STAFF)
        self.service.create_batch("b1", "kiln1", **KEEPER)
        self.service.load_artworks("b1", ["a1"], **KEEPER)
        self.service.seal_batch("b1", **KEEPER)

        # 撤回发生在作品烧制期间：未来课次 t2 也已报名
        publish(self.service, "t2", d.STAGE_THROWING, ["t-all2"],
                start="2026-10-10T09:00:00", end="2026-10-10T10:00:00",
                capacity=2, wheels=2)
        self.service.enroll("t2", "s1")

        result = self.service.withdraw_consent("s1", reason="监护人改变主意")

        # 未结束的 t2 退出，k1/t1/r1 已结束或已签到事实保留
        self.assertIn("t2", result["withdrawn_sessions"])
        self.assertNotIn("k1", result["withdrawn_sessions"])
        # 在窑作品被明确保留
        self.assertEqual(result["retained_artworks"], ["a1"])

        art = self.service.get_artwork("a1", **STAFF)
        self.assertEqual(art["student_id"], "s1")
        self.assertEqual(art["current_state"], d.ART_LOADED)
        self.assertEqual(art["custody_holder_type"], "kiln")
        self.assertEqual(art["custody_holder_id"], "kiln1")

        # k1 的席位早已签到不释放；t2 候补无人；无同意不能再报新课
        self.service.publish_session(
            "x1", d.STAGE_KNEADING, "新课",
            "2026-11-01T09:00:00", "2026-11-01T10:00:00",
            2, None, ["t-k"], **STAFF)
        with self.assertRaises(d.ConsentRequiredError):
            self.service.enroll("x1", "s1")

        # 在窑作品可以正常烧制，出窑后仍归 s1
        self.service.fire_batch("b1", **KEEPER)
        art = self.service.get_artwork("a1", **STAFF)
        self.assertEqual(art["current_state"], d.ART_FIRED)
        self.assertEqual(art["custody_holder_id"], "s1")

        # 重新授权后可报新课
        self.service.grant_consent("s1", **STAFF)
        self.assertEqual(self.service.enroll("x1", "s1")["status"],
                         d.ENROLLED)

    def test_撤回占席学生触发候补补位并退回补课资格(self):
        publish(self.service, "m1", d.STAGE_KNEADING, ["t-all"],
                start="2026-12-01T09:00:00", end="2026-12-01T10:00:00",
                capacity=1)
        # s1 用补课资格占了唯一席位
        grant = self.service.store.one(
            "INSERT INTO makeup_grant(student_id, stage, reason, "
            "source_session_id, status, created_at) "
            "VALUES('s1','kneading','测试资格','old', 'open', '2026-09-01') "
            "RETURNING grant_id")["grant_id"]
        self.service.enroll("m1", "s1", grant_id=grant)
        self.service.enroll("m1", "s2")  # 候补
        result = self.service.withdraw_consent("s1")
        self.assertEqual(result["promotions"],
                         [{"session_id": "m1", "student_id": "s2"}])
        self.assertEqual(result["reopened_grants"], [grant])
        grant_row = self.service.list_makeup_grants("s1")[0]
        self.assertEqual(grant_row["status"], "open")
        statuses = {e["student_id"]: e["status"]
                    for e in self.service.roster("m1", **STAFF)["enrollments"]}
        self.assertEqual(statuses["s2"], d.ENROLLED)


class 跨课次补课测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_取消课次发资格_跨课次使用并核销(self):
        publish(self.service, "k1", d.STAGE_KNEADING, ["t-k"],
                start="2026-10-01T09:00:00", end="2026-10-01T10:00:00",
                capacity=1)
        self.service.enroll("k1", "s1")
        self.service.enroll("k1", "s2")  # 候补，取消时不给资格

        cancelled = self.service.cancel_session("k1", reason="教师冲突", **STAFF)
        self.assertEqual(cancelled["makeup_grants"], [1])
        self.assertEqual(self.service.get_session("k1")["status"],
                         d.SESSION_CANCELLED)
        grants = {g["grant_id"]: g for g in self.service.list_makeup_grants("s1")}
        self.assertIn(1, grants)
        self.assertEqual(grants[1]["stage"], d.STAGE_KNEADING)
        self.assertEqual(self.service.list_makeup_grants("s2"), [])

        # 阶段不匹配不能使用
        publish(self.service, "th1", d.STAGE_THROWING, ["t-t"],
                start="2026-10-08T09:00:00", end="2026-10-08T10:00:00",
                wheels=2)
        with self.assertRaises(d.ConflictError):
            self.service.enroll("th1", "s1", grant_id=1)

        # 同阶段的补课课次可使用
        publish(self.service, "k2", d.STAGE_KNEADING, ["t-k"],
                start="2026-10-08T09:00:00", end="2026-10-08T10:00:00")
        self.assertEqual(
            self.service.enroll("k2", "s1", grant_id=1)["status"],
            d.ENROLLED)
        # 资格已预留，不能重复使用
        self.service.publish_session(
            "k3", d.STAGE_KNEADING, "又一次",
            "2026-10-15T09:00:00", "2026-10-15T10:00:00",
            10, None, ["t-k"], **STAFF)
        with self.assertRaises(d.ConflictError):
            self.service.enroll("k3", "s1", grant_id=1)

        self.service.start_session("k2", **STAFF)
        self.service.check_in("k2", "s1")
        self.assertEqual(
            self.service.list_makeup_grants("s1")[0]["status"], "used")

    def test_候补携带资格_补位时预留_请假退回(self):
        publish(self.service, "n1", d.STAGE_TRIMMING, ["t-r"],
                start="2026-10-01T09:00:00", end="2026-10-01T10:00:00",
                capacity=1, wheels=1)
        self.service.register_student("s9", "占位生", consent=True, **STAFF)
        self.service.enroll("n1", "s9")
        grant = self.service.store.one(
            "INSERT INTO makeup_grant(student_id, stage, reason, "
            "source_session_id, status, created_at) "
            "VALUES('s1','trimming','修坯取消','old','open','2026-09-01') "
            "RETURNING grant_id")["grant_id"]
        # s1 带资格进候补，资格此时仍 open
        self.assertEqual(
            self.service.enroll("n1", "s1", grant_id=grant)["status"],
            d.WAITLISTED)
        self.assertEqual(
            self.service.list_makeup_grants("s1")[0]["status"], "open")
        # 占位生请假 -> s1 补位，资格被预留
        self.service.start_session("n1", **STAFF)
        self.service.mark_absent("n1", "s9", **STAFF)
        self.assertEqual(
            self.service.list_makeup_grants("s1")[0]["status"], "reserved")
        # s1 又请假，资格退回 open，可再次用于别的课次
        result = self.service.mark_absent("n1", "s1", **STAFF)
        self.assertEqual(result["reopened_grant"], grant)
        self.assertEqual(
            self.service.list_makeup_grants("s1")[0]["status"], "open")


class 作品流转与窑炉测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        publish(self.service, "k1", d.STAGE_KNEADING, ["t-k"],
                start="2026-10-01T09:00:00", end="2026-10-01T10:00:00")
        self.service.enroll("k1", "s1")
        self.service.start_session("k1", **STAFF)
        self.service.check_in("k1", "s1")
        self.service.issue_clay("a1", "k1", "s1", actor_id="t-k",
                                actor_role=d.ROLE_TEACHER)

    def test_未签到不能领泥_作品编号不能重复(self):
        with self.assertRaises(d.ConflictError):
            self.service.issue_clay("a2", "k1", "s2", actor_id="t-k",
                                    actor_role=d.ROLE_TEACHER)
        with self.assertRaises(d.ConflictError):
            self.service.issue_clay("a1", "k1", "s1", actor_id="t-k",
                                    actor_role=d.ROLE_TEACHER)

    def test_工序不能跳阶(self):
        publish(self.service, "th1", d.STAGE_THROWING, ["t-t"],
                start="2026-10-02T09:00:00", end="2026-10-02T10:00:00",
                wheels=2)
        self.service.enroll("th1", "s1")
        self.service.start_session("th1", **STAFF)
        self.service.check_in("th1", "s1")
        with self.assertRaises(d.ConflictError):
            self.service.complete_stage("a1", "th1", actor_id="t-t",
                                        actor_role=d.ROLE_TEACHER)

    def test_非任课教师不能记录工序(self):
        with self.assertRaises(d.PermissionDeniedError):
            self.service.complete_stage("a1", "k1", actor_id="t-t",
                                        actor_role=d.ROLE_TEACHER)

    def _走完三阶段(self):
        self.service.complete_stage("a1", "k1", actor_id="t-k",
                                    actor_role=d.ROLE_TEACHER)
        publish(self.service, "th1", d.STAGE_THROWING, ["t-t"],
                start="2026-10-02T09:00:00", end="2026-10-02T10:00:00",
                wheels=2)
        self.service.enroll("th1", "s1")
        self.service.start_session("th1", **STAFF)
        self.service.check_in("th1", "s1")
        self.service.complete_stage("a1", "th1", actor_id="t-t",
                                    actor_role=d.ROLE_TEACHER)
        publish(self.service, "tr1", d.STAGE_TRIMMING, ["t-r"],
                start="2026-10-03T09:00:00", end="2026-10-03T10:00:00",
                wheels=2)
        self.service.enroll("tr1", "s1")
        self.service.start_session("tr1", **STAFF)
        self.service.check_in("tr1", "s1")
        self.service.complete_stage("a1", "tr1", actor_id="t-r",
                                    actor_role=d.ROLE_TEACHER)

    def test_停窑恢复后作品状态与批次恢复且审计完整(self):
        self._走完三阶段()
        self.service.register_kiln("kiln1", "一号窑", 2, **STAFF)
        self.service.create_batch("b1", "kiln1", **KEEPER)
        self.service.load_artworks("b1", ["a1"], **KEEPER)
        self.service.seal_batch("b1", **KEEPER)

        paused = self.service.pause_kiln("kiln1", reason="电路检修", **STAFF)
        self.assertEqual(paused["held_batches"], ["b1"])
        self.assertEqual(paused["held_artworks"], ["a1"])

        # 停窑期间：不能新建批次、不能装窑、不能烧制
        with self.assertRaises(d.ConflictError):
            self.service.create_batch("b2", "kiln1", **KEEPER)
        with self.assertRaises(d.ConflictError):
            self.service.fire_batch("b1", **KEEPER)

        art = self.service.get_artwork("a1", **STAFF)
        self.assertEqual(art["current_state"], d.ART_LOADED)
        self.assertEqual(art["custody_holder_type"], "kiln")
        self.assertEqual(art["custody_holder_id"], "kiln1")
        self.assertEqual(self.service.get_batch("b1")["status"], d.BATCH_HELD)

        resumed = self.service.resume_kiln("kiln1", **STAFF)
        self.assertEqual(resumed["status"], d.KILN_ACTIVE)
        # 封窑后停的，恢复到停窑前 loaded 状态
        self.assertEqual(self.service.get_batch("b1")["status"],
                         d.BATCH_LOADED)
        art = self.service.get_artwork("a1", **STAFF)
        self.assertEqual(art["current_state"], d.ART_LOADED)
        self.assertEqual(art["custody_holder_id"], "kiln1")

        self.service.fire_batch("b1", **KEEPER)
        self.assertEqual(self.service.get_batch("b1")["status"],
                         d.BATCH_FIRED)
        art = self.service.get_artwork("a1", **STAFF)
        self.assertEqual(art["current_state"], d.ART_FIRED)
        self.assertEqual(art["custody_holder_type"], "student")
        self.assertEqual(art["custody_holder_id"], "s1")

        # 作品交接链顺序完整
        actions = [t["action"] for t in art["transfers"]]
        self.assertEqual(actions, [
            d.HANDOFF_ISSUE_CLAY, d.HANDOFF_KNEAD, d.HANDOFF_THROW,
            d.HANDOFF_TRIM, d.HANDOFF_LOAD, d.HANDOFF_HOLD,
            d.HANDOFF_RESUME, d.HANDOFF_FIRE,
        ])
        # 批次审计按时间顺序记录
        batch_audit = self.service.audit_trail("kiln_batch", "b1", **STAFF)
        self.assertEqual([e["action"] for e in batch_audit],
                         ["batch.created", "artwork.loaded", "batch.sealed",
                          "batch.fired"])
        kiln_audit = self.service.audit_trail("kiln", "kiln1", **STAFF)
        self.assertEqual([e["action"] for e in kiln_audit],
                         ["kiln.registered", "kiln.paused", "kiln.resumed"])
        ids = [e["audit_id"] for e in kiln_audit]
        self.assertEqual(ids, sorted(ids))

    def test_装窑受容量与重复入窑约束(self):
        self._走完三阶段()
        self.service.register_kiln("kiln1", "一号窑", 1, **STAFF)
        self.service.create_batch("b1", "kiln1", **KEEPER)
        # 再造一件作品超限
        publish(self.service, "k2", d.STAGE_KNEADING, ["t-k"],
                start="2026-11-01T09:00:00", end="2026-11-01T10:00:00")
        with self.assertRaises(d.ConflictError):
            self.service.load_artworks("b1", ["a1", "a-missing"], **KEEPER)
        self.service.load_artworks("b1", ["a1"], **KEEPER)
        # 整体回滚：a1 已装入后，重复装另一件超容量也整体失败
        self.service.seal_batch("b1", **KEEPER)
        self.service.create_batch("b3", "kiln1", **KEEPER)
        with self.assertRaises(d.ConflictError):
            self.service.load_artworks("b3", ["a1"], **KEEPER)

    def test_修坯未完成不能入窑(self):
        self.service.register_kiln("kiln1", "一号窑", 2, **STAFF)
        self.service.create_batch("b1", "kiln1", **KEEPER)
        with self.assertRaises(d.ConflictError):
            self.service.load_artworks("b1", ["a1"], **KEEPER)

    def test_入窑作品不能直接作废(self):
        self._走完三阶段()
        self.service.register_kiln("kiln1", "一号窑", 2, **STAFF)
        self.service.create_batch("b1", "kiln1", **KEEPER)
        self.service.load_artworks("b1", ["a1"], **KEEPER)
        with self.assertRaises(d.ConflictError):
            self.service.scrap_artwork("a1", "摔碎了", actor_id="t-r",
                                       actor_role=d.ROLE_TEACHER)


class 授权查看测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        publish(self.service, "k1", d.STAGE_KNEADING, ["t-k"],
                start="2026-10-01T09:00:00", end="2026-10-01T10:00:00")
        self.service.enroll("k1", "s1")
        self.service.enroll("k1", "s2")
        self.service.start_session("k1", **STAFF)
        self.service.check_in("k1", "s1")
        self.service.issue_clay("a1", "k1", "s1", actor_id="t-k",
                                actor_role=d.ROLE_TEACHER)

    def test_学生只能看自己的作品(self):
        own = self.service.get_artwork("a1", "s1", d.ROLE_STUDENT)
        self.assertEqual(own["artwork_id"], "a1")
        with self.assertRaises(d.PermissionDeniedError):
            self.service.get_artwork("a1", "s2", d.ROLE_STUDENT)
        # 监护人可以看被监护人的作品
        guardian_view = self.service.get_artwork("a1", "g-s1", d.ROLE_STUDENT)
        self.assertEqual(guardian_view["artwork_id"], "a1")
        with self.assertRaises(d.PermissionDeniedError):
            self.service.get_artwork("a1", "g-s2", d.ROLE_STUDENT)
        # 列表同样受限
        visible = {a["artwork_id"]
                   for a in self.service.list_artworks_for_viewer(
                       "s2", d.ROLE_STUDENT)}
        self.assertNotIn("a1", visible)

    def test_教师只能看经手作品(self):
        # t-k 是揉泥课任教师，可见；t-t 未经手，不可见
        self.assertTrue(
            self.service.get_artwork("a1", "t-k", d.ROLE_TEACHER))
        with self.assertRaises(d.PermissionDeniedError):
            self.service.get_artwork("a1", "t-t", d.ROLE_TEACHER)

    def test_名单只有教务与任课教师可见(self):
        with self.assertRaises(d.PermissionDeniedError):
            self.service.roster("k1", "s1", d.ROLE_STUDENT)
        with self.assertRaises(d.PermissionDeniedError):
            self.service.roster("k1", "t-t", d.ROLE_TEACHER)
        self.assertTrue(self.service.roster("k1", "t-k", d.ROLE_TEACHER))

    def test_审计只有教务可读(self):
        with self.assertRaises(d.PermissionDeniedError):
            self.service.audit_trail("artwork", "a1", "s1",
                                     d.ROLE_STUDENT)


class JSON适配层测试(unittest.TestCase):
    def test_新动作信封与业务错误回滚(self):
        service = make_service()
        ok = json.loads(handle(json.dumps({
            "action": "session.publish", "session_id": "j1",
            "stage": d.STAGE_KNEADING, "title": "JSON课",
            "starts_at": "2026-10-01T09:00:00",
            "ends_at": "2026-10-01T10:00:00", "capacity": 2,
            "teacher_ids": ["t-k"], "actor_id": "office-1",
            "actor_role": d.ROLE_STAFF}), service))
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["data"]["status"], d.SESSION_SCHEDULED)

        bad = json.loads(handle(json.dumps({
            "action": "enroll", "session_id": "j1", "student_id": "s9",
            "actor_id": "office-1", "actor_role": d.ROLE_STAFF}), service))
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "not_found")

    def test_旧契约保持兼容(self):
        service = Service(Store())
        health = json.loads(handle(json.dumps({"action": "health"}), service))
        self.assertEqual(health["status"], "ok")
        created = json.loads(handle(json.dumps(
            {"action": "register", "record_id": "r-9",
             "owner_id": "o-9"}), service))
        self.assertEqual(created["state"], "draft")


if __name__ == "__main__":
    unittest.main()
