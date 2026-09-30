"""供进程内调用的轻量请求适配层。

请求体为 JSON，``action`` 决定分发；身份从 ``actor`` 字段构造：

    {"action": "enroll",
     "actor": {"user_id": "staff-1", "role": "staff"},
     "session_id": "s-1", "student_id": "stu-1"}

领域拒绝（容量、同意、冲突、重复签到、停窑、越权等）统一转成
``{"ok": false, "error": {"code", "message", ...}}``，不抛到进程边界；
未知动作仍抛出 :class:`ValueError`（沿用基线约定）。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .domain import DomainError, Principal
from .service import Service


def _principal(body: dict) -> Principal:
    actor = body.get("actor") or {}
    return Principal(
        user_id=str(actor.get("user_id") or "anonymous"),
        role=str(actor.get("role") or "staff"),
        student_id=actor.get("student_id"))


def _opt(body: dict, key: str, default: Any = None) -> Any:
    value = body.get(key, default)
    return default if value is None else value


def handle(payload: str, service: Service | None = None) -> str:
    service = service or Service()
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("请求体不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")

    action = body.get("action")

    # 旧版动作保持无信封响应，兼容既有调用方与基线测试。
    if action == "health":
        return json.dumps(service.health(), ensure_ascii=False)
    if action == "register":
        return json.dumps(service.register(str(body["record_id"]),
                                           str(body["owner_id"])),
                          ensure_ascii=False)

    handler = _ACTIONS.get(action)
    if handler is None:
        raise ValueError("不支持的请求动作")

    try:
        result = handler(service, _principal(body), body)
    except DomainError as exc:
        error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
        for extra in ("teacher_id", "session_id", "conflicting_session_id",
                      "student_id", "resource"):
            value = getattr(exc, extra, None)
            if value:
                error[extra] = value
        return json.dumps({"ok": False, "error": error}, ensure_ascii=False)

    return json.dumps({"ok": True, "result": result}, ensure_ascii=False,
                      default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, (tuple, set)):
        return list(value)
    raise TypeError(f"不可序列化的类型: {type(value)!r}")


# --- 各动作的入参转换 ------------------------------------------------------


def _h_register_teacher(svc: Service, actor: Principal,
                        body: dict) -> dict:
    return svc.register_teacher(actor, str(body["teacher_id"]),
                                str(body["name"]),
                                body.get("qualifications", []))


def _h_register_student(svc: Service, actor: Principal,
                        body: dict) -> dict:
    return svc.register_student(actor, str(body["student_id"]),
                                str(body["name"]))


def _h_grant_consent(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.grant_consent(actor, str(body["student_id"]))


def _h_withdraw_consent(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.withdraw_consent(actor, str(body["student_id"]))


def _h_publish_session(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.publish_session(
        actor, str(body["stage"]), str(body["scheduled_start"]),
        str(body["scheduled_end"]), int(body["capacity"]),
        wheel_count=int(body.get("wheel_count", 0)),
        session_id=body.get("session_id"),
        teacher_ids=body.get("teacher_ids"),
        note=str(body.get("note", "")))


def _h_assign_teacher(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.assign_teacher_guarded(actor, str(body["session_id"]),
                                      str(body["teacher_id"]))


def _h_get_session(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.get_session(actor, str(body["session_id"]))


def _h_list_sessions(svc: Service, actor: Principal, body: dict) -> list:
    return svc.list_sessions(
        actor, stage=body.get("stage"), status=body.get("status"))


def _h_cancel_session(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.cancel_session(actor, str(body["session_id"]),
                              reason=str(body.get("reason", "")))


def _h_enroll(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.enroll(actor, str(body["session_id"]),
                      str(body["student_id"]),
                      credit_id=body.get("credit_id"))


def _h_withdraw_enrollment(svc: Service, actor: Principal,
                           body: dict) -> dict:
    return svc.withdraw_enrollment(
        actor, str(body["session_id"]), str(body["student_id"]),
        reason=str(body.get("reason", "voluntary")),
        grant_makeup=bool(body.get("grant_makeup", False)))


def _h_checkin(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.checkin_guarded(actor, str(body["session_id"]),
                               str(body["student_id"]))


def _h_absence(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.mark_absence(
        actor, str(body["session_id"]), str(body["student_id"]),
        reason=str(body.get("reason", "leave")),
        grant_makeup=bool(body.get("grant_makeup", True)))


def _h_makeup_list(svc: Service, actor: Principal, body: dict) -> list:
    return svc.list_makeup_credits(actor, str(body["student_id"]))


def _h_issue_clay(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.issue_clay(actor, str(body["session_id"]),
                          str(body["student_id"]),
                          artifact_id=body.get("artifact_id"),
                          name=str(body.get("name", "")))


def _h_artifact_transition(svc: Service, actor: Principal,
                           body: dict) -> dict:
    # 状态机动作放在独立的 transition 字段，避免与 RPC 的 action 冲突。
    return svc.transition_artifact(
        actor, str(body["artifact_id"]), str(body["transition"]),
        note=str(body.get("note", "")),
        custodian_id=body.get("custodian_id"))


def _h_get_artifact(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.get_artifact(actor, str(body["artifact_id"]))


def _h_list_artifacts(svc: Service, actor: Principal, body: dict) -> list:
    return svc.list_artifacts_for(actor, str(body["student_id"]))


def _h_register_kiln(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.register_kiln(actor, str(body["kiln_id"]),
                             str(body["name"]))


def _h_halt_kiln(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.halt_kiln(actor, str(body["kiln_id"]),
                         reason=str(body.get("reason", "")))


def _h_resume_kiln(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.resume_kiln(actor, str(body["kiln_id"]),
                           note=str(body.get("note", "")))


def _h_load_kiln(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.load_kiln(actor, str(body["kiln_id"]),
                         list(body["artifact_ids"]),
                         batch_id=body.get("batch_id"))


def _h_start_firing(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.start_firing(actor, str(body["batch_id"]))


def _h_complete_firing(svc: Service, actor: Principal, body: dict) -> dict:
    return svc.complete_firing(actor, str(body["batch_id"]))


def _h_list_audit(svc: Service, actor: Principal, body: dict) -> list:
    # 过滤动作用独立字段，避免与 RPC 的 action 冲突。
    filters = {}
    if "entity_id" in body:
        filters["entity_id"] = body["entity_id"]
    if "session_id" in body:
        filters["session_id"] = body["session_id"]
    if "filter_action" in body:
        filters["action"] = body["filter_action"]
    if "limit" in body:
        filters["limit"] = body["limit"]
    return svc.list_audit(actor, **filters)


_ACTIONS: dict[str, Callable[[Service, Principal, dict], Any]] = {
    "teacher.register": _h_register_teacher,
    "student.register": _h_register_student,
    "consent.grant": _h_grant_consent,
    "consent.withdraw": _h_withdraw_consent,
    "session.publish": _h_publish_session,
    "session.assign_teacher": _h_assign_teacher,
    "session.get": _h_get_session,
    "session.list": _h_list_sessions,
    "session.cancel": _h_cancel_session,
    "enroll": _h_enroll,
    "enrollment.withdraw": _h_withdraw_enrollment,
    "checkin": _h_checkin,
    "absence": _h_absence,
    "makeup.list": _h_makeup_list,
    "artifact.issue_clay": _h_issue_clay,
    "artifact.transition": _h_artifact_transition,
    "artifact.get": _h_get_artifact,
    "artifact.list": _h_list_artifacts,
    "kiln.register": _h_register_kiln,
    "kiln.halt": _h_halt_kiln,
    "kiln.resume": _h_resume_kiln,
    "kiln.load": _h_load_kiln,
    "firing.start": _h_start_firing,
    "firing.complete": _h_complete_firing,
    "audit.list": _h_list_audit,
}
