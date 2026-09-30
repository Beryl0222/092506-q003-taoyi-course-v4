"""供进程内调用的轻量请求适配层。

请求是一段 JSON，形如 ``{"action": "...", "actor_id": ...,
"actor_role": ..., ...业务字段}``；响应同样是 JSON。

- 成功：``{"ok": true, "data": {...}}``
- 业务失败：``{"ok": false, "error": {"code": ..., "message": ...}}``，
  此时服务层事务已经整体回滚，调用方可据此提示或重试。

``health`` 与 ``register`` 保留最初骨架的裸响应格式，旧测试不受影响。
"""
import json

from .domain import DomainError
from .service import Service


def _actor(body: dict) -> dict:
    return {"actor_id": body.get("actor_id"),
            "actor_role": body.get("actor_role")}


def _a(body, name, default=None):
    return body.get(name, default)


# 每个动作：(service 方法名, 需要的字段映射函数)
def _dispatch(service: Service, body: dict):
    action = body["action"]

    if action == "health":
        return service.health()
    if action == "register":
        return service.register(str(body["record_id"]), str(body["owner_id"]))

    actor = _actor(body)

    if action == "teacher.register":
        return service.register_teacher(
            body["teacher_id"], body["name"], list(body["stages"]), **actor)
    if action == "student.register":
        return service.register_student(
            body["student_id"], body["name"],
            _a(body, "guardian_id"), bool(_a(body, "consent", False)),
            **actor)
    if action == "consent.grant":
        return service.grant_consent(body["student_id"], **actor)
    if action == "consent.withdraw":
        return service.withdraw_consent(
            body["student_id"], _a(body, "reason", ""), **actor)

    if action == "session.publish":
        return service.publish_session(
            body["session_id"], body["stage"], body["title"],
            body["starts_at"], body["ends_at"], int(body["capacity"]),
            _a(body, "wheels"), list(body["teacher_ids"]), **actor)
    if action == "session.get":
        return service.get_session(body["session_id"])
    if action == "session.list":
        return {"sessions": service.list_sessions(_a(body, "stage"))}
    if action == "session.start":
        return service.start_session(body["session_id"], **actor)
    if action == "session.complete":
        return service.complete_session(body["session_id"], **actor)
    if action == "session.cancel":
        return service.cancel_session(
            body["session_id"], _a(body, "reason", ""), **actor)
    if action == "teacher.reassign":
        return service.reassign_teacher(
            body["session_id"], body["remove_teacher_id"],
            body["add_teacher_id"], **actor)

    if action == "enroll":
        return service.enroll(
            body["session_id"], body["student_id"],
            _a(body, "grant_id"), **actor)
    if action == "checkin":
        return service.check_in(
            body["session_id"], body["student_id"], **actor)
    if action == "absent":
        return service.mark_absent(
            body["session_id"], body["student_id"], **actor)
    if action == "roster":
        return service.roster(body["session_id"], **actor)
    if action == "makeup.list":
        return {"grants": service.list_makeup_grants(body["student_id"])}

    if action == "artwork.issue_clay":
        return service.issue_clay(
            body["artwork_id"], body["session_id"], body["student_id"],
            **actor)
    if action == "artwork.complete_stage":
        return service.complete_stage(
            body["artwork_id"], body["session_id"], **actor)
    if action == "artwork.get":
        return service.get_artwork(
            body["artwork_id"], body["actor_id"], body["actor_role"])
    if action == "artwork.list_mine":
        return {"artworks": service.list_artworks_for_viewer(
            body["actor_id"], body["actor_role"])}
    if action == "artwork.return":
        return service.return_artwork(body["artwork_id"], **actor)
    if action == "artwork.scrap":
        return service.scrap_artwork(
            body["artwork_id"], body["reason"], **actor)

    if action == "kiln.register":
        return service.register_kiln(
            body["kiln_id"], body["name"], int(body["capacity"]), **actor)
    if action == "kiln.get":
        return service.get_kiln(body["kiln_id"])
    if action == "kiln.pause":
        return service.pause_kiln(
            body["kiln_id"], _a(body, "reason", ""), **actor)
    if action == "kiln.resume":
        return service.resume_kiln(body["kiln_id"], **actor)
    if action == "batch.create":
        return service.create_batch(
            body["batch_id"], body["kiln_id"], **actor)
    if action == "batch.get":
        return service.get_batch(body["batch_id"])
    if action == "batch.load":
        return service.load_artworks(
            body["batch_id"], list(body["artwork_ids"]), **actor)
    if action == "batch.seal":
        return service.seal_batch(body["batch_id"], **actor)
    if action == "batch.fire":
        return service.fire_batch(body["batch_id"], **actor)

    if action == "audit.get":
        return {"events": service.audit_trail(
            body["entity_type"], body["entity_id"],
            body.get("actor_id"), body.get("actor_role"))}

    raise ValueError(f"不支持的请求动作：{action}")


def handle(payload: str | dict, service: Service | None = None) -> str:
    service = service or Service()
    body = json.loads(payload) if isinstance(payload, str) else payload
    # 兼容旧契约：health/register 直接返回裸对象。
    if body.get("action") in ("health", "register"):
        data = _dispatch(service, body)
        return json.dumps(data, ensure_ascii=False)
    try:
        data = _dispatch(service, body)
    except DomainError as exc:
        return json.dumps(
            {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
            ensure_ascii=False)
    except (KeyError, TypeError) as exc:
        return json.dumps(
            {"ok": False, "error": {"code": "bad_request",
                                    "message": f"请求字段缺失或类型错误：{exc}"}},
            ensure_ascii=False)
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False)
