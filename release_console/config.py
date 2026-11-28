"""Publish-console settings. Secret values stay in the environment or secret.env."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_HOST = "8.146.206.64"
_HOST = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*"
)
_JOB = re.compile(r"[A-Za-z0-9_.-]{1,80}")


def default_secret_path(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    data = env.get("QA_DATA_DIR") or os.path.join(ROOT, "data")
    return os.path.join(data, "secret.env")


def default_db_path(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    data = env.get("QA_DATA_DIR") or os.path.join(ROOT, "data")
    return os.path.join(data, "release_console.db")


def parse_env_file(path: str) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        handle = open(path, "r", encoding="utf-8")
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


def _http_base(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    if parts.username or parts.password:
        return ""
    return text.rstrip("/")


def _host_ok(value: str) -> bool:
    if not value or len(value) > 253 or not _HOST.fullmatch(value):
        return False
    return True


def _job_ok(value: str) -> str:
    text = (value or "").strip()
    if _JOB.fullmatch(text):
        return text
    return ""


def _flag(value: str, default: bool) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class Settings:
    publish_host: str
    host_valid: bool
    jenkins_url: str
    jenkins_user: str
    jenkins_token: str
    frontend_job: str
    backend_job: str
    inspect_after_publish: bool
    db_path: str

    def __repr__(self) -> str:
        return (
            "Settings(host=%s, host_valid=%s, jenkins_url=%s, user_set=%s, token_set=%s, "
            "frontend_job=%s, backend_job=%s, inspect_after_publish=%s, db_path=%s)"
            % (
                self.publish_host,
                self.host_valid,
                self.jenkins_url,
                bool(self.jenkins_user),
                bool(self.jenkins_token),
                self.frontend_job,
                self.backend_job,
                self.inspect_after_publish,
                self.db_path,
            )
        )

    def job_for(self, service: str) -> str:
        if service == "frontend":
            raw = self.frontend_job
        elif service == "backend":
            raw = self.backend_job
        else:
            raw = ""
        if raw and _JOB.fullmatch(raw):
            return raw
        return ""

    def credentials_ready(self) -> bool:
        return bool(self.jenkins_url and self.jenkins_user and self.jenkins_token)

    def wired(self, service: str) -> bool:
        return bool(self.host_valid and self.credentials_ready() and self.job_for(service))


def load(environ: dict[str, str] | None = None, secret_file: str | None = None) -> Settings:
    env = os.environ if environ is None else environ
    if secret_file is None:
        secret_file = env.get("RELEASE_SECRET_FILE", default_secret_path(env))
    file_vals = parse_env_file(secret_file) if secret_file else {}

    def pick(key: str, default: str = "") -> str:
        raw = env.get(key)
        if raw is not None and raw != "":
            return raw
        saved = file_vals.get(key)
        if saved:
            return saved
        return default

    host = pick("RELEASE_PUBLISH_HOST", DEFAULT_HOST).strip()
    return Settings(
        publish_host=host,
        host_valid=_host_ok(host),
        jenkins_url=_http_base(pick("JENKINS_URL")),
        jenkins_user=pick("JENKINS_USER").strip(),
        jenkins_token=pick("JENKINS_API_TOKEN"),
        frontend_job=_job_ok(pick("RELEASE_JENKINS_FRONTEND_DEPLOY_JOB")),
        backend_job=_job_ok(pick("RELEASE_JENKINS_BACKEND_DEPLOY_JOB")),
        inspect_after_publish=_flag(pick("RELEASE_INSPECT_AFTER_PUBLISH", "1"), True),
        db_path=pick("RELEASE_DB_PATH", default_db_path(env)),
    )
