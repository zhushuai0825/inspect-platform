#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inspect platform API + static UI. Compatible with Python 3.6."""
from __future__ import print_function

import core
import notifications
import hmac
import json
import os
import shutil
import subprocess
import time
import threading
import re
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
STATIC_DIR = os.path.join(BASE_DIR, "static")
SECRET_FILE = os.path.join(DATA_DIR, "secret.env")

HOST = os.environ.get("QA_BIND", "127.0.0.1")
PORT = int(os.environ.get("QA_PORT", "8765"))
PUBLIC_PREFIX = "/qa"
TRIGGER_LOCK = threading.Lock()
LAST_TRIGGER = 0
TRIGGER_COOLDOWN = 60


def _now_shanghai():
    # Server timezone is Asia/Shanghai; keep naive local timestamps labeled +08:00
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def ensure_dirs():
    for path in (DATA_DIR, REPORT_DIR, STATIC_DIR):
        if not os.path.isdir(path):
            os.makedirs(path, mode=0o755)


def load_env_file(path):
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path, "r") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


ENV = {}


def boot_config():
    global ENV
    ensure_dirs()
    core.init()
    ENV = load_env_file(SECRET_FILE)
    # Allow process env to override
    for key in (
        "JENKINS_URL",
        "JENKINS_USER",
        "JENKINS_API_TOKEN",
        "JENKINS_JOB",
    ):
        if os.environ.get(key):
            ENV[key] = os.environ[key]
    if not ENV.get("JENKINS_URL"):
        ENV["JENKINS_URL"] = "http://127.0.0.1:8088/jenkins"
    if not ENV.get("JENKINS_JOB"):
        ENV["JENKINS_JOB"] = "site-http-smoke"
    if not ENV.get("JENKINS_USER"):
        ENV["JENKINS_USER"] = "admin"


def list_reports():
    items = []
    if not os.path.isdir(REPORT_DIR):
        return items
    for name in os.listdir(REPORT_DIR):
        if not name.endswith(".json") or name == "latest.json":
            continue
        path = os.path.join(REPORT_DIR, name)
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
            items.append(
                {
                    "id": data.get("id") or name[:-5],
                    "suite_name": data.get("suite_name"),
                    "started_at": data.get("started_at"),
                    "finished_at": data.get("finished_at"),
                    "duration_ms": data.get("duration_ms"),
                    "trigger": data.get("trigger"),
                    "status": data.get("status"),
                    "summary": data.get("summary") or {},
                    "jenkins_build": data.get("jenkins_build"),
                }
            )
        except Exception:
            continue
    items.sort(key=lambda x: x.get("started_at") or x.get("id") or "", reverse=True)
    return items


def read_report(report_id):
    # prevent path traversal
    if not re.match(r"^[A-Za-z0-9_-]{1,80}$", report_id or ""):
        return None
    path = os.path.join(REPORT_DIR, report_id + ".json")
    if not os.path.isfile(path):
        return None
    with open(path, "r") as fh:
        return json.load(fh)


def overview_payload():
    reports = list_reports()
    latest = reports[0] if reports else None
    latest_full = read_report(latest["id"]) if latest else None
    # next daily 09:00 Asia/Shanghai
    now = datetime.now()
    next_run = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now >= next_run:
        next_run = next_run + timedelta(days=1)
    return {
        "server_time": _now_shanghai(),
        "timezone": "Asia/Shanghai",
        "schedule": "每天 09:00",
        "next_run_at": next_run.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "latest": latest_full,
        "trigger_cooldown_sec": TRIGGER_COOLDOWN,
        "recent": reports[:20],
        "tasks": core.tasks(),
        "events": core.events(5),
        "mail_status": notifications.summary(),
        "totals": {
            "runs": len(reports),
            "failed_runs": sum(1 for r in reports if r.get("status") == "failed"),
        },
        "jenkins": {
            "url": "/jenkins/",
            "job": ENV.get("JENKINS_JOB", "site-http-smoke"),
            "job_url": "/jenkins/job/%s/" % ENV.get("JENKINS_JOB", "site-http-smoke"),
        },
        "suite": {
            "id": "site-http-smoke",
            "name": "站点 HTTP 冒烟巡检",
            "description": "检查个人主页、简历、运维入口、应用页与 Jenkins 登录页是否可访问。",
            "schedule": "每天 09:00（Asia/Shanghai），也可手动触发。",
        },
    }


def run_inspect(trigger):
    report = core.execute(trigger=trigger)
    return {"ok": report["status"] != "failed", "exit_code": 1 if report["status"] == "failed" else 0, "report": report}


def _basic_auth_header(user, token):
    import base64

    raw = ("%s:%s" % (user, token)).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def jenkins_request(path, method="GET", data=None, form=False):
    base = ENV.get("JENKINS_URL", "").rstrip("/")
    user = ENV.get("JENKINS_USER") or "admin"
    token = ENV.get("JENKINS_API_TOKEN") or ""
    if not base or not token:
        raise RuntimeError("尚未配置 Jenkins API Token")
    url = base + path
    headers = {
        "Authorization": _basic_auth_header(user, token),
        "User-Agent": "inspect-platform/1.0",
    }
    # crumb
    crumb_url = base + "/crumbIssuer/api/json"
    try:
        req = Request(crumb_url, headers=headers)
        with urlopen(req, timeout=8) as resp:
            crumb = json.loads(resp.read().decode("utf-8"))
            headers[crumb.get("crumbRequestField") or "Jenkins-Crumb"] = crumb.get("crumb")
    except Exception:
        pass
    body = None
    if data is not None:
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = data if isinstance(data, bytes) else data.encode("utf-8")
        else:
            headers["Content-Type"] = "application/json"
            body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=12) as resp:
            raw = resp.read()
            code = getattr(resp, "status", 200)
            return code, raw
    except HTTPError as err:
        raw = err.read() if err.fp else b""
        raise RuntimeError("Jenkins HTTP %s" % err.code)


def trigger_jenkins(trigger):
    job = ENV.get("JENKINS_JOB", "site-http-smoke")
    qs = "TRIGGER=" + quote(trigger or "manual")
    path = "/job/%s/buildWithParameters?%s" % (quote(job, safe=""), qs)
    try:
        code, _raw = jenkins_request(path, method="POST")
    except RuntimeError as err:
        if "not parameterized" not in str(err) and "400" not in str(err):
            raise
        path = "/job/%s/build" % quote(job, safe="")
        code, _raw = jenkins_request(path, method="POST")
    return {"queued": True, "http_status": code, "job": job}


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "inspect-platform/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (_now_shanghai(), fmt % args))

    def _path(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        # Normalize prefix so both /qa/api and /api work
        if path == PUBLIC_PREFIX:
            path = "/"
        elif path.startswith(PUBLIC_PREFIX + "/"):
            path = path[len(PUBLIC_PREFIX):]
        return path, parsed.query

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send(self, code, body, content_type="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body or b""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        if headers:
            for k, v in headers:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _static_file(self, rel):
        if not rel or rel == "/":
            rel = "/index.html"
        rel = rel.split("?", 1)[0]
        # Strict file allow-list: no SPA fallback to credential/config paths.
        if rel not in ("/index.html", "/styles.css", "/app.js"):
            self._send(404, {"ok": False, "error": "资源不存在"})
            return
        path = os.path.join(STATIC_DIR, rel.lstrip("/"))
        if not os.path.isfile(path):
            self._send(404, {"ok": False, "error": "资源不存在"})
            return
        ext = os.path.splitext(path)[1].lower()
        types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        with open(path, "rb") as fh:
            data = fh.read()
        self._send(200, data, types.get(ext, "application/octet-stream"))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, query = self._path()
        if path in ("/api/health",):
            self._send(
                200,
                {"ok": True, "service": "inspect-platform", "time": _now_shanghai()},
            )
            return
        if path.startswith("/api/"):
            if path == "/api/tasks":
                self._send(200, {"ok": True, "data": core.tasks()})
                return
            if path == "/api/events":
                self._send(200, {"ok": True, "data": core.events(100), "external_notifications": notifications.summary()["enabled"], "mail": notifications.summary()})
                return
            if path == "/api/overview":
                self._send(200, {"ok": True, "data": overview_payload()})
                return
            if path == "/api/reports":
                qs = parse_qs(query or "")
                limit = 50
                try:
                    limit = int((qs.get("limit") or ["50"])[0])
                except Exception:
                    limit = 50
                self._send(200, {"ok": True, "data": list_reports()[: max(1, min(limit, 200))]})
                return
            if path.startswith("/api/reports/"):
                rid = path[len("/api/reports/") :].strip("/")
                data = read_report(rid)
                if not data:
                    self._send(404, {"ok": False, "error": "报告不存在"})
                    return
                self._send(200, {"ok": True, "data": data})
                return
            self._send(404, {"ok": False, "error": "接口不存在"})
            return
        self._static_file(path)

    def do_POST(self):
        global LAST_TRIGGER
        path, _query = self._path()
        if path != "/api/runs":
            # Login/logout and the old internal runner are deliberately not public APIs.
            self._send(404, {"ok": False, "error": "接口不存在"})
            return
        if (self.headers.get("Content-Type") or "").split(";", 1)[0] != "application/json":
            self._send(415, {"ok": False, "error": "仅接受 JSON 请求"})
            return
        # Same-origin browser requests only; command/URL/job cannot be supplied.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host") or ""
        try:
            invalid_origin = bool(origin and urlparse(origin).netloc != host)
        except ValueError:
            invalid_origin = True
        if invalid_origin:
            self._send(403, {"ok": False, "error": "不接受跨站触发"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 0 or length > 2048:
            self._send(413, {"ok": False, "error": "请求体过大或无效"})
            return
        body = self._read_json()
        if not isinstance(body, dict) or any(k not in ("trigger", "mode") for k in body):
            self._send(400, {"ok": False, "error": "仅支持触发现有固定巡检任务"})
            return
        if body.get("mode", "jenkins") != "jenkins" or body.get("trigger", "manual") != "manual":
            self._send(400, {"ok": False, "error": "仅支持固定任务的手动触发"})
            return
        wait = core.reserve_trigger(TRIGGER_COOLDOWN)
        if wait:
            self._send(429, {"ok": False, "error": "触发过于频繁，请稍后重试", "retry_after_sec": wait})
            return
        try:
            queued = trigger_jenkins("manual")
            self._send(200, {"ok": True, "mode": "jenkins", "data": queued})
        except Exception:
            # Retain the original fixed-script fallback, but never expose exceptions or credentials.
            try:
                result = run_inspect("manual")
                self._send(200, {
                    "ok": True, "mode": "local-fallback",
                    "warning": "Jenkins 暂不可用，已使用本机执行固定巡检。",
                    "data": {"report": result.get("report"), "exit_code": result.get("exit_code")},
                })
            except Exception:
                self._send(503, {"ok": False, "error": "巡检执行失败，请检查服务器服务日志"})


def main():
    boot_config()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("inspect-platform listening on %s:%s (prefix %s)" % (HOST, PORT, PUBLIC_PREFIX))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
