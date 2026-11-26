"""Trigger and poll a trusted Jenkins job. This client never creates jobs."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_JOB = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_RESULT = re.compile(r"[A-Z_]{1,20}")


class JenkinsRejected(Exception):
    def __init__(self, status: int):
        super().__init__("Jenkins HTTP %s" % status)
        self.status = status


class JenkinsUnknown(Exception):
    pass


@dataclass(frozen=True)
class TriggerResult:
    queue_url: str
    status: int


@dataclass(frozen=True)
class PollResult:
    phase: str
    build_url: str
    message: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int = 12):
    opener = build_opener(_NoRedirect)
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(1_000_000)
            return getattr(response, "status", 200), dict(response.headers.items()), payload
    except HTTPError as err:
        payload = err.read(1_000_000) if err.fp else b""
        return err.code, dict(err.headers.items()), payload
    except URLError as err:
        raise JenkinsUnknown("连接 Jenkins 失败") from err


def _header(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


class JenkinsClient:
    def __init__(self, base_url: str, user: str, token: str, transport=None):
        self.base = (base_url or "").rstrip("/")
        self.user = user or ""
        self.token = token or ""
        self.transport = transport or urllib_transport

    def _auth_headers(self) -> dict[str, str]:
        raw = ("%s:%s" % (self.user, self.token)).encode("utf-8")
        return {
            "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "User-Agent": "inspect-platform-release/0",
        }

    def _same_origin(self, url: str) -> str:
        if not self.base:
            raise JenkinsUnknown("Jenkins 地址未配置，发布锁保留")
        absolute = urljoin(self.base + "/", url)
        got = urlsplit(absolute)
        base = urlsplit(self.base)
        if got.scheme != base.scheme or got.netloc != base.netloc:
            raise JenkinsUnknown("Jenkins 返回了配置地址之外的链接，已忽略")
        return absolute

    def _request(self, method: str, url: str, headers: dict[str, str]):
        if not self.base:
            raise JenkinsUnknown("Jenkins 地址未配置，发布锁保留")
        target = self._same_origin(url)
        try:
            status, response_headers, body = self.transport(method, target, headers, None)
        except JenkinsUnknown:
            raise
        except Exception as err:
            raise JenkinsUnknown("连接 Jenkins 失败") from err
        if not isinstance(response_headers, dict):
            response_headers = {}
        return status, response_headers, body if isinstance(body, bytes) else b""

    def _crumb(self, headers: dict[str, str]) -> dict[str, str]:
        merged = dict(headers)
        try:
            status, _hdrs, body = self._request("GET", self.base + "/crumbIssuer/api/json", headers)
        except JenkinsUnknown:
            return merged
        if status != 200 or not body:
            return merged
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return merged
        if not isinstance(payload, dict):
            return merged
        field = str(payload.get("crumbRequestField") or "Jenkins-Crumb")
        crumb = payload.get("crumb")
        if isinstance(crumb, str) and crumb:
            merged[field] = crumb
        return merged

    def trigger(self, job: str, params: dict[str, str]) -> TriggerResult:
        if not _JOB.fullmatch(job or ""):
            raise JenkinsRejected(400)
        for key, value in params.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise JenkinsRejected(400)
        query = urlencode(list(params.items()))
        headers = self._crumb(self._auth_headers())
        status, response_headers, _body = self._request(
            "POST",
            "%s/job/%s/buildWithParameters?%s" % (self.base, job, query),
            headers,
        )
        if status in (200, 201, 202):
            location = _header(response_headers, "Location")
            queue_url = ""
            if location:
                try:
                    queue_url = self._same_origin(location)
                except JenkinsUnknown:
                    queue_url = ""
            return TriggerResult(queue_url, status)
        if 400 <= status < 500:
            raise JenkinsRejected(status)
        raise JenkinsUnknown("Jenkins 返回 HTTP %s，发布结果未确认，发布锁保留" % status)

    def poll(self, queue_url: str, build_url: str) -> PollResult:
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
            except (UnicodeError, json.JSONDecodeError) as err:
                raise JenkinsUnknown("Jenkins 队列状态无法解析，发布锁保留") from err
            if not isinstance(payload, dict):
                raise JenkinsUnknown("Jenkins 队列状态无法解析，发布锁保留")
            if payload.get("cancelled"):
                return PollResult("failure", "", "Jenkins 队列项已取消，发布未完成")
            executable = payload.get("executable") or {}
            url = executable.get("url") if isinstance(executable, dict) else ""
            if not url:
                return PollResult("queued", "", "发布任务仍在 Jenkins 队列中")
            try:
                build = self._same_origin(str(url))
            except JenkinsUnknown:
                return PollResult("unknown", "", "Jenkins 返回了配置地址之外的构建链接，发布锁保留")
        else:
            return PollResult("unknown", "", "尚未拿到 Jenkins 队列地址，结果未知，发布锁保留")

        status, _headers, body = self._request(
            "GET", build.rstrip("/") + "/api/json", self._auth_headers()
        )
        if status != 200:
            raise JenkinsUnknown("暂时无法读取 Jenkins 构建，发布锁保留")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as err:
            raise JenkinsUnknown("Jenkins 构建状态无法解析，发布锁保留") from err
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
            return PollResult("building", link, "Jenkins 正在执行发布")
        result = payload.get("result")
        if result == "SUCCESS":
            return PollResult("success", link, "Jenkins 构建成功")
        if isinstance(result, str) and _RESULT.fullmatch(result):
            return PollResult("failure", link, "Jenkins 构建结果为 %s，发布未成功" % result)
        return PollResult("unknown", link, "Jenkins 构建结果尚未确认，发布锁保留")
