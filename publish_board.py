#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Public workbench publish/rollback. Python 3.6 compatible.

Does not import release_console. The loopback admin keeps that 3.11 package.
Passwords are read from the environment or secret.env and are never returned.
"""
from __future__ import print_function

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, pathname2url

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HOST = "8.146.206.64"
SHANGHAI = timezone(timedelta(hours=8))
SERVICES = (("frontend", "前端"), ("backend", "后端"))
SERVICE_LABEL = dict(SERVICES)
_HOST = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*"
)
_JOB = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_ACTOR = re.compile(r"[\w][\w ._-]{0,39}", re.UNICODE)
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_STATUS = re.compile(r"[a-z_]{1,20}")
_RUN = re.compile(r"[A-Za-z0-9_-]{1,80}")
_ACTION = re.compile(r"^/api/releases/(pub[a-f0-9]{12})/(refresh|rollback|abandon)$")
_RESULT = re.compile(r"[A-Z_]{1,20}")
COLUMNS = (
    "id", "service", "action", "actor", "commit_sha", "version", "artifact", "host",
    "state", "created_at", "updated_at", "finished_at", "jenkins_job", "jenkins_queue_url",
    "jenkins_build_url", "result_message", "rollback_of", "restores_id", "inspect_run_id",
    "inspect_status", "inspect_error",
)
MUTABLE = {
    "state", "updated_at", "finished_at", "jenkins_queue_url", "jenkins_build_url",
    "result_message", "inspect_run_id", "inspect_status", "inspect_error",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS publishes (
  id TEXT PRIMARY KEY,
  service TEXT NOT NULL CHECK (service IN ('frontend','backend')),
  action TEXT NOT NULL CHECK (action IN ('deploy','rollback')),
  actor TEXT NOT NULL,
  commit_sha TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  artifact TEXT NOT NULL DEFAULT '',
  host TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('deploying','succeeded','failed','rolled_back')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT '',
  jenkins_job TEXT NOT NULL DEFAULT '',
  jenkins_queue_url TEXT NOT NULL DEFAULT '',
  jenkins_build_url TEXT NOT NULL DEFAULT '',
  result_message TEXT NOT NULL DEFAULT '',
  rollback_of TEXT NOT NULL DEFAULT '',
  restores_id TEXT NOT NULL DEFAULT '',
  inspect_run_id TEXT NOT NULL DEFAULT '',
  inspect_status TEXT NOT NULL DEFAULT '',
  inspect_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS publishes_service_created ON publishes(service, created_at DESC);
"""
TRANSPORT = None
INSPECT = None
_THREAD = threading.Lock()


class BoardError(Exception):
    def __init__(self, status, message, record=None):
        Exception.__init__(self, message)
        self.status = status
        self.record = record


class JenkinsRejected(BoardError):
    def __init__(self, status):
        BoardError.__init__(self, 502, "Jenkins HTTP %s" % status)
        self.http_status = status


class JenkinsUnknown(BoardError):
    def __init__(self, message):
        BoardError.__init__(self, 502, message)


class Settings(object):
    def __init__(self, **kwargs):
        self.publish_host = kwargs["publish_host"]
        self.host_valid = kwargs["host_valid"]
        self.jenkins_url = kwargs["jenkins_url"]
        self.jenkins_user = kwargs["jenkins_user"]
        self.jenkins_token = kwargs["jenkins_token"]
        self.frontend_job = kwargs["frontend_job"]
        self.backend_job = kwargs["backend_job"]
        self.inspect_after_publish = kwargs["inspect_after_publish"]
        self.db_path = kwargs["db_path"]
        self.password = kwargs["password"]

    def __repr__(self):
        return "Settings(password_set=%s, token_set=%s, host=%s)" % (
            bool(self.password), bool(self.jenkins_token), self.publish_host)

    def job_for(self, service):
        raw = self.frontend_job if service == "frontend" else self.backend_job if service == "backend" else ""
        if raw and _JOB.match(raw) and _JOB.match(raw).group(0) == raw:
            return raw
        return ""

    def credentials_ready(self):
        return bool(self.jenkins_url and self.jenkins_user and self.jenkins_token)

    def wired(self, service):
        return bool(self.host_valid and self.credentials_ready() and self.job_for(service))


def _full(pattern, text):
    if text is None:
        return False
    matched = pattern.match(text)
    return bool(matched and matched.group(0) == text)


def _data_dir(env):
    return env.get("QA_DATA_DIR") or os.path.join(ROOT, "data")


def _parse_file(path):
    found = {}
    try:
        handle = open(path, "r")
    except OSError:
        return found
    with handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            found[key.strip()] = value.strip().strip('"').strip("'")
    return found


def _pick(env, file_vals, key, default=""):
    raw = env.get(key)
    if raw:
        return raw
    saved = file_vals.get(key)
    if saved:
        return saved
    return default


def _http_base(value):
    text = (value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        return ""
    return text.rstrip("/")


def _flag(value, default):
    text = (value or "").strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def load_settings(env=None):
    env = os.environ if env is None else env
    secret_path = env.get("RELEASE_SECRET_FILE")
    if secret_path is None:
        secret_path = os.path.join(_data_dir(env), "secret.env")
    file_vals = _parse_file(secret_path) if secret_path else {}
    host = _pick(env, file_vals, "RELEASE_PUBLISH_HOST", DEFAULT_HOST).strip()
    job = lambda key: _pick(env, file_vals, key).strip()
    frontend = job("RELEASE_JENKINS_FRONTEND_DEPLOY_JOB")
    backend = job("RELEASE_JENKINS_BACKEND_DEPLOY_JOB")
    return Settings(
        publish_host=host,
        host_valid=bool(host) and len(host) <= 253 and _full(_HOST, host),
        jenkins_url=_http_base(_pick(env, file_vals, "JENKINS_URL")),
        jenkins_user=_pick(env, file_vals, "JENKINS_USER").strip(),
        jenkins_token=_pick(env, file_vals, "JENKINS_API_TOKEN"),
        frontend_job=frontend if _full(_JOB, frontend) else "",
        backend_job=backend if _full(_JOB, backend) else "",
        inspect_after_publish=_flag(_pick(env, file_vals, "RELEASE_INSPECT_AFTER_PUBLISH", "1"), True),
        db_path=_pick(env, file_vals, "RELEASE_DB_PATH", os.path.join(_data_dir(env), "release_console.db")),
        password=_pick(env, file_vals, "RELEASE_PUBLISH_PASSWORD"),
    )


def _stamp():
    return datetime.now(SHANGHAI).isoformat(timespec="microseconds")


def _scrub(text, settings):
    cleaned = " ".join(str(text).split())
    for secret in (settings.jenkins_token, settings.password):
        if secret and len(secret) >= 8 and secret in cleaned:
            cleaned = cleaned.replace(secret, "[redacted]")
    if len(cleaned) > 240:
        cleaned = cleaned[:240]
    return cleaned


def _redact(value, settings):
    if isinstance(value, dict):
        return dict((key, _redact(item, settings)) for key, item in value.items())
    if isinstance(value, list):
        return [_redact(item, settings) for item in value]
    if isinstance(value, str) or isinstance(value, type(u"")):
        cleaned = value
        for secret in (settings.password, settings.jenkins_token):
            if secret and len(secret) >= 8 and secret in cleaned:
                cleaned = cleaned.replace(secret, "[redacted]")
        return cleaned
    return value


def _passwords_match(provided, expected):
    if not isinstance(provided, str) or not provided or not expected:
        return False
    try:
        left = hashlib.sha256(provided.encode("utf-8")).digest()
        right = hashlib.sha256(expected.encode("utf-8")).digest()
    except UnicodeError:
        return False
    return hmac.compare_digest(left, right)


def require_password(provided, settings):
    if not settings.password:
        raise BoardError(403, "发布口令未配置，未执行操作")
    if not _passwords_match(provided, settings.password):
        raise BoardError(403, "口令不正确")


class Store(object):
    def __init__(self, path):
        self.path = path

    def _connect(self):
        parent = os.path.dirname(self.path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, 0o700)
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init(self):
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _rows(self, sql, args=()):
        conn = self._connect()
        try:
            found = conn.execute(sql, args).fetchall()
            return [dict(row) for row in found]
        finally:
            conn.close()

    def get(self, record_id):
        if not os.path.isfile(self.path):
            return None
        rows = self._rows("SELECT * FROM publishes WHERE id=?", (record_id,))
        return rows[0] if rows else None

    def list_all(self):
        if not os.path.isfile(self.path):
            return []
        return self._rows("SELECT * FROM publishes ORDER BY created_at DESC, id DESC")

    def list_service(self, service):
        if not os.path.isfile(self.path):
            return []
        return self._rows(
            "SELECT * FROM publishes WHERE service=? ORDER BY created_at DESC, id DESC", (service,))

    def insert_deploying(self, row):
        self.init()
        with _THREAD:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                busy = conn.execute(
                    "SELECT id FROM publishes WHERE service=? AND state='deploying'", (row["service"],)).fetchone()
                if busy:
                    conn.execute("ROLLBACK")
                    raise BoardError(409, "该服务已有发布正在进行，不能同时发布")
                conn.execute(
                    "INSERT INTO publishes (%s) VALUES (%s)" % (",".join(COLUMNS), ",".join("?" for _ in COLUMNS)),
                    tuple(row[key] for key in COLUMNS))
                conn.execute("COMMIT")
            except BoardError:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
        return self.get(row["id"])

    def update(self, record_id, **fields):
        if set(fields) - MUTABLE:
            raise ValueError("field cannot be updated")
        if not fields:
            return
        keys = list(fields)
        sql = "UPDATE publishes SET " + ", ".join(key + "=?" for key in keys) + " WHERE id=?"
        with _THREAD:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(sql, tuple(fields[key] for key in keys) + (record_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def finish(self, record_id, fields, supersede_id=""):
        if set(fields) - MUTABLE:
            raise ValueError("field cannot be updated")
        keys = list(fields)
        sql = "UPDATE publishes SET " + ", ".join(key + "=?" for key in keys) + " WHERE id=? AND state='deploying'"
        with _THREAD:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT state FROM publishes WHERE id=?", (record_id,)).fetchone()
                if current is None:
                    conn.execute("ROLLBACK")
                    raise BoardError(404, "发布记录不存在")
                if current["state"] != "deploying":
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(sql, tuple(fields[key] for key in keys) + (record_id,))
                if supersede_id and fields.get("state") == "rolled_back":
                    conn.execute(
                        "UPDATE publishes SET state='rolled_back', updated_at=?, result_message=? "
                        "WHERE id=? AND action='deploy' AND state='succeeded'",
                        (fields.get("updated_at", ""), "该次发布已回滚。", supersede_id))
                conn.execute("COMMIT")
                return True
            except BoardError:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(method, url, headers, body, timeout=12):
    opener = build_opener(_NoRedirect)
    request = Request(url, data=body, headers=headers, method=method)
    try:
        response = opener.open(request, timeout=timeout)
        try:
            return getattr(response, "status", 200), dict(response.headers.items()), response.read(1000000)
        finally:
            response.close()
    except HTTPError as err:
        payload = err.read(1000000) if err.fp else b""
        return err.code, dict(err.headers.items()), payload
    except URLError as err:
        raise JenkinsUnknown("连接 Jenkins 失败")


class JenkinsClient(object):
    def __init__(self, base_url, user, token, transport=None):
        self.base = (base_url or "").rstrip("/")
        self.user = user or ""
        self.token = token or ""
        self.transport = transport or urllib_transport

    def _auth_headers(self):
        raw = ("%s:%s" % (self.user, self.token)).encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii"), "User-Agent": "inspect-platform-release/0"}

    def _same_origin(self, url):
        if not self.base:
            raise JenkinsUnknown("Jenkins 地址未配置，发布锁保留")
        absolute = urljoin(self.base + "/", url)
        got = urlsplit(absolute)
        base = urlsplit(self.base)
        if got.scheme != base.scheme or got.netloc != base.netloc:
            raise JenkinsUnknown("Jenkins 返回了配置地址之外的链接，已忽略")
        return absolute

    def _request(self, method, url, headers):
        target = self._same_origin(url)
        try:
            status, response_headers, body = self.transport(method, target, headers, None)
        except JenkinsUnknown:
            raise
        except Exception:
            raise JenkinsUnknown("连接 Jenkins 失败")
        if not isinstance(response_headers, dict):
            response_headers = {}
        if not isinstance(body, bytes):
            body = b""
        return status, response_headers, body

    def _crumb(self, headers):
        merged = dict(headers)
        try:
            status, _hdrs, body = self._request("GET", self.base + "/crumbIssuer/api/json", headers)
        except JenkinsUnknown:
            return merged
        if status != 200 or not body:
            return merged
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            return merged
        if not isinstance(payload, dict):
            return merged
        crumb = payload.get("crumb")
        if isinstance(crumb, str) and crumb:
            merged[str(payload.get("crumbRequestField") or "Jenkins-Crumb")] = crumb
        return merged

    def trigger(self, job, params):
        if not _full(_JOB, job or ""):
            raise JenkinsRejected(400)
        for key, value in params.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise JenkinsRejected(400)
        headers = self._crumb(self._auth_headers())
        status, response_headers, _body = self._request(
            "POST", "%s/job/%s/buildWithParameters?%s" % (self.base, quote(job, safe=""), urlencode(list(params.items()))), headers)
        if status in (200, 201, 202):
            location = _header(response_headers, "Location")
            queue_url = ""
            if location:
                try:
                    queue_url = self._same_origin(location)
                except JenkinsUnknown:
                    queue_url = ""
            return queue_url, status
        if 400 <= status < 500:
            raise JenkinsRejected(status)
        raise JenkinsUnknown("Jenkins 返回 HTTP %s，发布结果未确认，发布锁保留" % status)

    def poll(self, queue_url, build_url):
        build = build_url or ""
        if build:
            build = self._same_origin(build)
        elif queue_url:
            queue = self._same_origin(queue_url).rstrip("/")
            status, _headers, body = self._request("GET", queue + "/api/json", self._auth_headers())
            if status != 200:
                raise JenkinsUnknown("暂时无法读取 Jenkins 队列，发布锁保留")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, ValueError):
                raise JenkinsUnknown("Jenkins 队列状态无法解析，发布锁保留")
            if not isinstance(payload, dict):
                raise JenkinsUnknown("Jenkins 队列状态无法解析，发布锁保留")
            if payload.get("cancelled"):
                return "failure", "", "Jenkins 队列项已取消，发布未完成"
            executable = payload.get("executable") or {}
            url = executable.get("url") if isinstance(executable, dict) else ""
            if not url:
                return "queued", "", "发布任务仍在 Jenkins 队列中"
            try:
                build = self._same_origin(str(url))
            except JenkinsUnknown:
                return "unknown", "", "Jenkins 返回了配置地址之外的构建链接，发布锁保留"
        else:
            return "unknown", "", "尚未拿到 Jenkins 队列地址，结果未知，发布锁保留"
        status, _headers, body = self._request("GET", build.rstrip("/") + "/api/json", self._auth_headers())
        if status != 200:
            raise JenkinsUnknown("暂时无法读取 Jenkins 构建，发布锁保留")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise JenkinsUnknown("Jenkins 构建状态无法解析，发布锁保留")
        if not isinstance(payload, dict):
            raise JenkinsUnknown("Jenkins 构建状态无法解析，发布锁保留")
        link = build
        reported = payload.get("url")
        if isinstance(reported, str) and reported:
            try:
                link = self._same_origin(reported)
            except JenkinsUnknown:
                link = build
        if payload.get("building"):
            return "building", link, "Jenkins 正在执行发布"
        result = payload.get("result")
        if result == "SUCCESS":
            return "success", link, "Jenkins 构建成功"
        if isinstance(result, str) and _full(_RESULT, result):
            return "failure", link, "Jenkins 构建结果为 %s，发布未成功" % result
        return "unknown", link, "Jenkins 构建结果尚未确认，发布锁保留"


def run_existing_suite(run_id):
    if not _full(_RUN, run_id):
        raise ValueError("invalid run id")
    import core
    report = core.execute(run_id=run_id, trigger="jenkins")
    return {"id": str(report.get("id") or run_id), "status": str(report.get("status") or "")}


def present(row):
    shown = {}
    for key in (
        "id", "service", "action", "actor", "commit_sha", "version", "artifact", "host", "state",
        "created_at", "updated_at", "finished_at", "jenkins_build_url", "result_message",
        "rollback_of", "restores_id", "inspect_run_id", "inspect_status", "inspect_error",
    ):
        shown[key] = row.get(key) or ""
    shown["can_abandon"] = bool(
        row.get("state") == "deploying" and not row.get("jenkins_queue_url") and not row.get("jenkins_build_url"))
    return shown


def _identity(row):
    return (row.get("commit_sha") or "", row.get("version") or "", row.get("artifact") or "")


def _live(rows):
    for row in rows:
        if row["action"] == "deploy" and row["state"] == "succeeded":
            return row
        if row["action"] == "rollback" and row["state"] == "rolled_back":
            return row
    return None


def _previous(rows, current):
    if current is None:
        return None
    current_identity = _identity(current)
    for row in rows:
        if row["id"] == current["id"] or _identity(row) == current_identity:
            continue
        if row["action"] == "deploy" and row["state"] == "succeeded":
            return row
        if row["action"] == "rollback" and row["state"] == "rolled_back":
            return row
    return None


def _status_from(settings, grouped):
    services = {}
    for key, label in SERVICES:
        rows = grouped.get(key) or []
        live = _live(rows)
        previous = _previous(rows, live)
        locked = any(row["state"] == "deploying" for row in rows)
        services[key] = {
            "label": label,
            "wired": settings.wired(key),
            "locked": locked,
            "current_id": live["id"] if live else "",
            "can_rollback": bool(previous) and not locked,
        }
    records = []
    for key, _label in SERVICES:
        records.extend(grouped.get(key) or [])
    records.sort(key=lambda row: (row.get("created_at") or "", row.get("id") or ""), reverse=True)
    return {
        "host": settings.publish_host if settings.host_valid else "",
        "host_valid": settings.host_valid,
        "inspect_after_publish": settings.inspect_after_publish,
        "unavailable": False,
        "services": services,
        "records": [present(row) for row in records],
    }


def _empty(settings, unavailable=False):
    services = {}
    for key, label in SERVICES:
        services[key] = {
            "label": label, "wired": settings.wired(key), "locked": False, "current_id": "", "can_rollback": False}
    return {
        "host": settings.publish_host if settings.host_valid else "",
        "host_valid": settings.host_valid,
        "inspect_after_publish": settings.inspect_after_publish,
        "unavailable": unavailable,
        "services": services,
        "records": [],
    }


def _read_only_rows(path):
    uri = "file:%s?mode=ro" % pathname2url(os.path.abspath(path))
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "publishes" not in names:
            return []
        found = conn.execute("SELECT * FROM publishes ORDER BY created_at DESC, id DESC").fetchall()
        return [dict(row) for row in found]
    finally:
        conn.close()


def public_view():
    settings = load_settings()
    if not settings.db_path or not os.path.isfile(settings.db_path):
        return _empty(settings, False)
    try:
        rows = _read_only_rows(settings.db_path)
    except Exception:
        print("publish_board read failed", flush=True)
        return _empty(settings, True)
    grouped = {"frontend": [], "backend": []}
    for row in rows:
        if row.get("service") in grouped:
            grouped[row["service"]].append(row)
    return _status_from(settings, grouped)


class Board(object):
    def __init__(self, store, jenkins, settings, inspect):
        self.store = store
        self.jenkins = jenkins
        self.settings = settings
        self.inspect = inspect

    def _text(self, value, label):
        if value is None:
            return ""
        if not isinstance(value, str) and not isinstance(value, type(u"")):
            raise BoardError(400, "%s必须是字符串" % label)
        if isinstance(value, bytes):
            raise BoardError(400, "%s必须是字符串" % label)
        text = value.strip()
        if any(ord(char) < 32 for char in text):
            raise BoardError(400, "%s包含不可见字符" % label)
        return text

    def _keys(self, raw, allowed):
        if not isinstance(raw, dict):
            raise BoardError(400, "请求体必须是 JSON 对象")
        if set(raw) - allowed:
            raise BoardError(400, "请求包含不支持的字段")

    def _require_wired(self, service):
        if not self.settings.host_valid:
            raise BoardError(409, "发布目标主机配置无效，未创建发布记录")
        if not self.settings.credentials_ready():
            raise BoardError(409, "Jenkins 地址或凭证未配置，未创建发布记录")
        if not self.settings.job_for(service):
            raise BoardError(409, "尚未配置%s发布任务，未创建发布记录" % SERVICE_LABEL[service])

    def _must(self, record_id):
        row = self.store.get(record_id)
        if row is None:
            raise BoardError(404, "发布记录不存在")
        return row

    def _blank(self, **values):
        row = dict((key, "") for key in COLUMNS)
        row.update(values)
        return row

    def _new_row(self, service, action, actor, commit, version, artifact, rollback_of="", restores_id=""):
        stamp = _stamp()
        return self._blank(
            id="pub" + uuid.uuid4().hex[:12], service=service, action=action, actor=actor,
            commit_sha=commit, version=version, artifact=artifact, host=self.settings.publish_host,
            state="deploying", created_at=stamp, updated_at=stamp, jenkins_job=self.settings.job_for(service),
            result_message="正在提交发布任务", rollback_of=rollback_of, restores_id=restores_id)

    def _trigger(self, record):
        params = {
            "ACTION": record["action"], "COMMIT": record["commit_sha"], "VERSION": record["version"],
            "ARTIFACT": record["artifact"], "SERVICE": record["service"], "TICKET": record["id"],
            "ACTOR": record["actor"],
        }
        try:
            queue_url, _status = self.jenkins.trigger(record["jenkins_job"], params)
        except JenkinsRejected as err:
            finished = _stamp()
            self.store.finish(record["id"], {
                "state": "failed", "updated_at": finished, "finished_at": finished,
                "result_message": "Jenkins 拒绝了发布任务（HTTP %s），服务器代码未切换" % err.http_status,
                "inspect_status": "skipped", "inspect_error": "发布未被接受，未启动巡检",
            })
            raise BoardError(502, self.store.get(record["id"])["result_message"], present(self._must(record["id"])))
        except JenkinsUnknown as err:
            self.store.update(record["id"], updated_at=_stamp(), result_message=_scrub(str(err), self.settings))
            return present(self._must(record["id"]))
        message = "发布任务已提交，等待 Jenkins 结果" if queue_url else "Jenkins 已接受请求，但没有返回队列地址，结果待确认"
        self.store.update(record["id"], jenkins_queue_url=queue_url, updated_at=_stamp(), result_message=message)
        return present(self._must(record["id"]))

    def _maybe_inspect(self, row):
        if row["inspect_status"] or row["inspect_run_id"]:
            return row
        if row["state"] not in ("succeeded", "failed", "rolled_back"):
            return row
        if not self.settings.inspect_after_publish:
            self.store.update(row["id"], inspect_status="disabled", updated_at=_stamp())
            return self._must(row["id"])
        run_id = "rel-" + row["id"]
        try:
            report = self.inspect(run_id)
        except Exception:
            self.store.update(row["id"], inspect_status="error", inspect_error="巡检未能启动，发布状态未改写", updated_at=_stamp())
            return self._must(row["id"])
        if not isinstance(report, dict):
            report = {}
        run = str(report.get("id") or run_id)
        status = str(report.get("status") or "unknown")
        if not _full(_RUN, run):
            run = run_id
        if not _full(_STATUS, status):
            status = "unknown"
        self.store.update(row["id"], inspect_run_id=run, inspect_status=status, inspect_error="", updated_at=_stamp())
        return self._must(row["id"])

    def publish(self, raw):
        self._keys(raw, {"service", "actor", "commit", "version", "artifact"})
        service = self._text(raw.get("service"), "服务")
        if service not in SERVICE_LABEL:
            raise BoardError(400, "服务只能是前端或后端")
        actor = self._text(raw.get("actor"), "操作者")
        if not _full(_ACTOR, actor):
            raise BoardError(400, "请填写操作者，最长 40 个字符")
        commit = self._text(raw.get("commit", ""), "提交").lower()
        if commit and not _full(_COMMIT, commit):
            raise BoardError(400, "提交 SHA 需要是 7 到 40 位小写十六进制，或留空")
        version = self._text(raw.get("version", ""), "版本")
        if version and not _full(_VERSION, version):
            raise BoardError(400, "版本只允许字母、数字、点、下划线和连字符")
        artifact = self._text(raw.get("artifact", ""), "制品标识")
        if artifact and not _full(_ARTIFACT, artifact):
            raise BoardError(400, "制品标识只允许字母、数字和 . _ + -，不能是路径")
        if not commit and not version:
            raise BoardError(400, "请填写版本或提交 SHA，二者至少一项")
        self._require_wired(service)
        record = self._new_row(service, "deploy", actor, commit, version, artifact)
        self.store.insert_deploying(record)
        return self._trigger(record)

    def refresh(self, record_id):
        row = self._must(record_id)
        if row["state"] != "deploying":
            return present(row)
        try:
            phase, build_url, message = self.jenkins.poll(row["jenkins_queue_url"], row["jenkins_build_url"])
        except JenkinsUnknown as err:
            self.store.update(record_id, updated_at=_stamp(), result_message=_scrub(str(err), self.settings))
            return present(self._must(record_id))
        stamp = _stamp()
        fields = {"updated_at": stamp}
        if build_url:
            fields["jenkins_build_url"] = build_url
        if phase == "success":
            fields["state"] = "rolled_back" if row["action"] == "rollback" else "succeeded"
            fields["finished_at"] = stamp
            fields["result_message"] = "已回滚到此前的成功发布" if row["action"] == "rollback" else message
            changed = self.store.finish(record_id, fields, row["rollback_of"] if row["action"] == "rollback" else "")
        elif phase == "failure":
            fields["state"] = "failed"
            fields["finished_at"] = stamp
            fields["result_message"] = message
            changed = self.store.finish(record_id, fields)
        else:
            fields["result_message"] = message
            self.store.update(record_id, **fields)
            return present(self._must(record_id))
        updated = self._must(record_id)
        if not changed:
            return present(updated)
        return present(self._maybe_inspect(updated))

    def rollback(self, record_id, raw):
        self._keys(raw, {"actor"})
        actor = self._text(raw.get("actor"), "操作者")
        if not _full(_ACTOR, actor):
            raise BoardError(400, "请填写操作者，最长 40 个字符")
        current = self._must(record_id)
        rows = self.store.list_service(current["service"])
        live = _live(rows)
        if live is None or live["id"] != current["id"]:
            raise BoardError(400, "只能回滚该服务当前生效的发布")
        if any(row["state"] == "deploying" for row in rows):
            raise BoardError(409, "该服务已有发布正在进行，不能回滚")
        previous = _previous(rows, live)
        if previous is None:
            raise BoardError(400, "没有更早的成功发布，不能回滚")
        self._require_wired(current["service"])
        record = self._new_row(
            current["service"], "rollback", actor, previous["commit_sha"], previous["version"],
            previous["artifact"], current["id"], previous["id"])
        self.store.insert_deploying(record)
        return self._trigger(record)

    def abandon(self, record_id, raw):
        self._keys(raw, {"actor"})
        actor = self._text(raw.get("actor"), "操作者")
        if not _full(_ACTOR, actor):
            raise BoardError(400, "请填写操作者，最长 40 个字符")
        row = self._must(record_id)
        if row["state"] != "deploying":
            raise BoardError(400, "只有仍在发布中的记录可以标记为未确认")
        if row["jenkins_queue_url"] or row["jenkins_build_url"]:
            raise BoardError(400, "Jenkins 已返回队列或构建，不能在这里标记为未发布")
        finished = _stamp()
        self.store.finish(record_id, {
            "state": "failed", "finished_at": finished, "updated_at": finished,
            "result_message": "未确认 Jenkins 已接收。%s 将其标记为未发布，请核对 Jenkins 是否仍有构建。" % actor,
            "inspect_status": "skipped", "inspect_error": "发布未确认，未启动巡检",
        })
        return present(self._must(record_id))


def _open_board():
    settings = load_settings()
    store = Store(settings.db_path)
    client = JenkinsClient(settings.jenkins_url, settings.jenkins_user, settings.jenkins_token, TRANSPORT)
    return Board(store, client, settings, INSPECT or run_existing_suite)


def _read_json(handler):
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except (TypeError, ValueError):
        raise BoardError(400, "请求体无效")
    if length < 0 or length > 8192:
        raise BoardError(413, "请求体上限为 8 KiB")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise BoardError(400, "请求体必须是 JSON")
    if not isinstance(data, dict):
        raise BoardError(400, "请求体必须是 JSON 对象")
    return data


def _same_origin(handler):
    origin = handler.headers.get("Origin")
    host = handler.headers.get("Host") or ""
    if not origin:
        return True
    try:
        return urlsplit(origin).netloc == host
    except ValueError:
        return False


def handle_get(handler):
    handler._send(200, {"ok": True, "data": public_view()})


def handle_post(handler, path):
    if (handler.headers.get("Content-Type") or "").split(";", 1)[0] != "application/json":
        handler._send(415, {"ok": False, "error": "仅接受 JSON 请求"})
        return
    try:
        body = _read_json(handler)
        provided = body.pop("password", None)
        if not _same_origin(handler):
            raise BoardError(403, "不接受跨站操作")
        settings = load_settings()
        require_password(provided, settings)
        del provided
        board = _open_board()
        if path == "/api/releases":
            payload = board.publish(body)
        else:
            match = _ACTION.match(path)
            if not match:
                raise BoardError(404, "接口不存在")
            record_id, action = match.group(1), match.group(2)
            if action == "refresh":
                payload = board.refresh(record_id)
            elif action == "rollback":
                payload = board.rollback(record_id, body)
            else:
                payload = board.abandon(record_id, body)
        handler._send(200, {"ok": True, "data": _redact(payload, load_settings())})
    except BoardError as err:
        settings = load_settings()
        message = _scrub(str(err), settings)
        body = {"ok": False, "error": message}
        if err.record:
            body["data"] = _redact(err.record, settings)
        handler._send(err.status, body)
    except Exception:
        print("publish_board error", flush=True)
        handler._send(500, {"ok": False, "error": "发布操作失败，请检查服务器服务日志"})
