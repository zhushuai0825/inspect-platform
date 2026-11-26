"""HTTP adapter used only by the loopback admin process."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from release_console import config as config_mod
from release_console.errors import Conflict, NotFound, NotWired, PublishRejected, ValidationError
from release_console.inspect_hook import run_existing_suite
from release_console.jenkins_client import JenkinsClient
from release_console.service import PublishService
from release_console.store import Store

TRANSPORT = None
INSPECT = None
_ACTION = re.compile(r"^/api/releases/(pub[a-f0-9]{12})/(refresh|rollback|abandon)$")


def build_service() -> PublishService:
    settings = config_mod.load()
    store = Store(settings.db_path)
    store.init()
    client = JenkinsClient(
        settings.jenkins_url,
        settings.jenkins_user,
        settings.jenkins_token,
        transport=TRANSPORT,
    )
    return PublishService(store, client, settings, INSPECT or run_existing_suite)


def _body(handler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except (TypeError, ValueError) as err:
        raise ValidationError("请求体无效") from err
    if length < 0 or length > 8192:
        raise ValidationError("请求体上限为 8 KiB")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as err:
        raise ValidationError("请求体必须是 JSON") from err
    if not isinstance(data, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    return data


def handle(handler) -> None:
    path = urlsplit(handler.path).path
    try:
        service = build_service()
        if handler.command == "GET" and path == "/api/releases":
            handler._send(200, {"ok": True, "data": service.status()})
            return
        if handler.command == "POST" and path == "/api/releases":
            handler._send(200, {"ok": True, "data": service.publish(_body(handler))})
            return
        match = _ACTION.fullmatch(path)
        if handler.command == "POST" and match:
            record_id, action = match.group(1), match.group(2)
            if action == "refresh":
                payload = service.refresh(record_id)
            elif action == "rollback":
                payload = service.rollback(record_id, _body(handler))
            else:
                payload = service.abandon(record_id, _body(handler))
            handler._send(200, {"ok": True, "data": payload})
            return
        handler._send(404, {"ok": False, "error": "接口不存在"})
    except NotWired as err:
        handler._send(409, {"ok": False, "error": str(err)})
    except Conflict as err:
        handler._send(409, {"ok": False, "error": str(err)})
    except ValidationError as err:
        handler._send(400, {"ok": False, "error": str(err)})
    except NotFound as err:
        handler._send(404, {"ok": False, "error": str(err)})
    except PublishRejected as err:
        handler._send(502, {"ok": False, "error": str(err), "data": err.record})
    except Exception as err:
        print("release_console error %s" % type(err).__name__, flush=True)
        handler._send(500, {"ok": False, "error": "发布操作失败，请检查本机管理日志"})
