"""Publish, refresh, rollback, and the per-service deploy lock."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from release_console.config import Settings
from release_console.errors import Conflict, NotFound, NotWired, PublishRejected, ValidationError
from release_console.jenkins_client import JenkinsRejected, JenkinsUnknown
from release_console.store import COLUMNS, Store

SHANGHAI = timezone(timedelta(hours=8))
SERVICES = {"frontend": "前端", "backend": "后端"}
_ACTOR = re.compile(r"[\w][\w ._-]{0,39}", re.UNICODE)
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_STATUS = re.compile(r"[a-z_]{1,20}")
_RUN = re.compile(r"[A-Za-z0-9_-]{1,80}")
HIDDEN = {"jenkins_queue_url"}


def present(row: dict[str, str]) -> dict[str, object]:
    shown: dict[str, object] = {key: value for key, value in row.items() if key not in HIDDEN}
    shown["can_abandon"] = bool(
        row.get("state") == "deploying"
        and not row.get("jenkins_queue_url")
        and not row.get("jenkins_build_url")
    )
    return shown


class PublishService:
    def __init__(self, store: Store, jenkins, settings: Settings, inspect):
        self.store = store
        self.jenkins = jenkins
        self.settings = settings
        self.inspect = inspect

    def _stamp(self) -> str:
        return datetime.now(SHANGHAI).isoformat(timespec="microseconds")

    def _scrub(self, text: str) -> str:
        cleaned = " ".join(str(text).split())
        token = self.settings.jenkins_token
        if token and token in cleaned:
            cleaned = cleaned.replace(token, "[redacted]")
        if len(cleaned) > 240:
            cleaned = cleaned[:240]
        return cleaned

    def _keys(self, raw: dict, allowed: set[str]) -> None:
        if not isinstance(raw, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        if set(raw) - allowed:
            raise ValidationError("请求包含不支持的字段")

    def _text(self, value: object, label: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValidationError("%s必须是字符串" % label)
        if any(ord(char) < 32 for char in value):
            raise ValidationError("%s包含不可见字符" % label)
        return value.strip()

    def _service_name(self, value: object) -> str:
        text = self._text(value, "服务")
        if text not in SERVICES:
            raise ValidationError("服务只能是前端或后端")
        return text

    def _actor(self, value: object) -> str:
        text = self._text(value, "操作者")
        if not _ACTOR.fullmatch(text):
            raise ValidationError("请填写操作者，最长 40 个字符")
        return text

    def _commit(self, value: object) -> str:
        text = self._text(value, "提交").lower()
        if text and not _COMMIT.fullmatch(text):
            raise ValidationError("提交 SHA 需要是 7 到 40 位小写十六进制，或留空")
        return text

    def _version(self, value: object) -> str:
        text = self._text(value, "版本")
        if text and not _VERSION.fullmatch(text):
            raise ValidationError("版本只允许字母、数字、点、下划线和连字符")
        return text

    def _artifact(self, value: object) -> str:
        text = self._text(value, "制品标识")
        if text and not _ARTIFACT.fullmatch(text):
            raise ValidationError("制品标识只允许字母、数字和 . _ + -，不能是路径")
        return text

    def _require_wired(self, service: str) -> None:
        label = SERVICES[service]
        if not self.settings.host_valid:
            raise NotWired("发布目标主机配置无效，未创建发布记录")
        if not self.settings.credentials_ready():
            raise NotWired("Jenkins 地址或凭证未配置，未创建发布记录")
        if not self.settings.job_for(service):
            raise NotWired("尚未配置%s发布任务，未创建发布记录" % label)

    def _must(self, record_id: str) -> dict[str, str]:
        row = self.store.get(record_id)
        if row is None:
            raise NotFound("发布记录不存在")
        return row

    def _identity(self, row: dict[str, str]) -> tuple[str, str, str]:
        return (row["commit_sha"], row["version"], row["artifact"])

    def _live(self, service: str) -> dict[str, str] | None:
        for row in self.store.list_service(service):
            if row["action"] == "deploy" and row["state"] == "succeeded":
                return row
            if row["action"] == "rollback" and row["state"] == "rolled_back":
                return row
        return None

    def _previous(self, service: str, current: dict[str, str] | None) -> dict[str, str] | None:
        if current is None:
            return None
        current_identity = self._identity(current)
        for row in self.store.list_service(service):
            if row["id"] == current["id"] or self._identity(row) == current_identity:
                continue
            if row["action"] == "deploy" and row["state"] == "succeeded":
                return row
            if row["action"] == "rollback" and row["state"] == "rolled_back":
                return row
        return None

    def _locked(self, service: str) -> bool:
        return any(row["state"] == "deploying" for row in self.store.list_service(service))

    def _blank(self, **values: str) -> dict[str, str]:
        row = {key: "" for key in COLUMNS}
        row.update(values)
        return row

    def _new_row(
        self,
        service: str,
        action: str,
        actor: str,
        commit: str,
        version: str,
        artifact: str,
        rollback_of: str = "",
        restores_id: str = "",
    ) -> dict[str, str]:
        stamp = self._stamp()
        return self._blank(
            id="pub" + uuid.uuid4().hex[:12],
            service=service,
            action=action,
            actor=actor,
            commit_sha=commit,
            version=version,
            artifact=artifact,
            host=self.settings.publish_host,
            state="deploying",
            created_at=stamp,
            updated_at=stamp,
            jenkins_job=self.settings.job_for(service),
            result_message="正在提交发布任务",
            rollback_of=rollback_of,
            restores_id=restores_id,
        )

    def _trigger(self, record: dict[str, str]) -> dict[str, object]:
        params = {
            "ACTION": record["action"],
            "COMMIT": record["commit_sha"],
            "VERSION": record["version"],
            "ARTIFACT": record["artifact"],
            "SERVICE": record["service"],
            "TICKET": record["id"],
            "ACTOR": record["actor"],
        }
        try:
            accepted = self.jenkins.trigger(record["jenkins_job"], params)
        except JenkinsRejected as err:
            finished = self._stamp()
            self.store.finish(
                record["id"],
                {
                    "state": "failed",
                    "updated_at": finished,
                    "finished_at": finished,
                    "result_message": "Jenkins 拒绝了发布任务（HTTP %s），服务器代码未切换" % err.status,
                    "inspect_status": "skipped",
                    "inspect_error": "发布未被接受，未启动巡检",
                },
            )
            saved = self._must(record["id"])
            raise PublishRejected(present(saved)) from err
        except JenkinsUnknown as err:
            self.store.update(
                record["id"],
                updated_at=self._stamp(),
                result_message=self._scrub(str(err) or "提交发布任务没有得到可确认的结果，发布锁保留"),
            )
            return present(self._must(record["id"]))
        message = (
            "发布任务已提交，等待 Jenkins 结果"
            if accepted.queue_url
            else "Jenkins 已接受请求，但没有返回队列地址，结果待确认"
        )
        self.store.update(
            record["id"],
            jenkins_queue_url=accepted.queue_url,
            updated_at=self._stamp(),
            result_message=message,
        )
        return present(self._must(record["id"]))

    def _maybe_inspect(self, row: dict[str, str]) -> dict[str, str]:
        if row["inspect_status"] or row["inspect_run_id"]:
            return row
        if row["state"] not in ("succeeded", "failed", "rolled_back"):
            return row
        if not self.settings.inspect_after_publish:
            self.store.update(row["id"], inspect_status="disabled", updated_at=self._stamp())
            return self._must(row["id"])
        run_id = "rel-" + row["id"]
        try:
            report = self.inspect(run_id)
        except Exception:
            self.store.update(
                row["id"],
                inspect_status="error",
                inspect_error="巡检未能启动，发布状态未改写",
                updated_at=self._stamp(),
            )
            return self._must(row["id"])
        if not isinstance(report, dict):
            report = {}
        run = str(report.get("id") or run_id)
        status = str(report.get("status") or "unknown")
        if not _RUN.fullmatch(run):
            run = run_id
        if not _STATUS.fullmatch(status):
            status = "unknown"
        self.store.update(
            row["id"],
            inspect_run_id=run,
            inspect_status=status,
            inspect_error="",
            updated_at=self._stamp(),
        )
        return self._must(row["id"])

    def status(self) -> dict[str, object]:
        services: dict[str, object] = {}
        for key, label in SERVICES.items():
            live = self._live(key)
            previous = self._previous(key, live)
            services[key] = {
                "label": label,
                "wired": self.settings.wired(key),
                "locked": self._locked(key),
                "current_id": live["id"] if live else "",
                "can_rollback": bool(previous) and not self._locked(key),
            }
        return {
            "host": self.settings.publish_host if self.settings.host_valid else "",
            "host_valid": self.settings.host_valid,
            "inspect_after_publish": self.settings.inspect_after_publish,
            "services": services,
            "records": [present(row) for row in self.store.list_all()],
        }

    def publish(self, raw: dict) -> dict[str, object]:
        self._keys(raw, {"service", "actor", "commit", "version", "artifact"})
        service = self._service_name(raw.get("service"))
        actor = self._actor(raw.get("actor"))
        commit = self._commit(raw.get("commit", ""))
        version = self._version(raw.get("version", ""))
        artifact = self._artifact(raw.get("artifact", ""))
        if not commit and not version:
            raise ValidationError("请填写版本或提交 SHA，二者至少一项")
        self._require_wired(service)
        record = self._new_row(service, "deploy", actor, commit, version, artifact)
        try:
            self.store.insert_deploying(record)
        except Conflict:
            raise
        return self._trigger(record)

    def refresh(self, record_id: str) -> dict[str, object]:
        row = self._must(record_id)
        if row["state"] != "deploying":
            return present(row)
        try:
            poll = self.jenkins.poll(row["jenkins_queue_url"], row["jenkins_build_url"])
        except JenkinsUnknown as err:
            self.store.update(record_id, updated_at=self._stamp(), result_message=self._scrub(str(err)))
            return present(self._must(record_id))
        stamp = self._stamp()
        fields: dict[str, str] = {"updated_at": stamp}
        if poll.build_url:
            fields["jenkins_build_url"] = poll.build_url
        match poll.phase:
            case "success":
                fields["state"] = "rolled_back" if row["action"] == "rollback" else "succeeded"
                fields["finished_at"] = stamp
                fields["result_message"] = (
                    "已回滚到此前的成功发布" if row["action"] == "rollback" else poll.message
                )
                changed = self.store.finish(
                    record_id,
                    fields,
                    supersede_id=row["rollback_of"] if row["action"] == "rollback" else "",
                )
            case "failure":
                fields["state"] = "failed"
                fields["finished_at"] = stamp
                fields["result_message"] = poll.message
                changed = self.store.finish(record_id, fields)
            case _:
                fields["result_message"] = poll.message
                self.store.update(record_id, **fields)
                return present(self._must(record_id))
        updated = self._must(record_id)
        if not changed:
            return present(updated)
        return present(self._maybe_inspect(updated))

    def rollback(self, record_id: str, raw: dict) -> dict[str, object]:
        self._keys(raw, {"actor"})
        actor = self._actor(raw.get("actor"))
        current = self._must(record_id)
        live = self._live(current["service"])
        if live is None or live["id"] != current["id"]:
            raise ValidationError("只能回滚该服务当前生效的发布")
        if self._locked(current["service"]):
            raise Conflict("该服务已有发布正在进行，不能回滚")
        previous = self._previous(current["service"], live)
        if previous is None:
            raise ValidationError("没有更早的成功发布，不能回滚")
        self._require_wired(current["service"])
        record = self._new_row(
            current["service"],
            "rollback",
            actor,
            previous["commit_sha"],
            previous["version"],
            previous["artifact"],
            rollback_of=current["id"],
            restores_id=previous["id"],
        )
        self.store.insert_deploying(record)
        return self._trigger(record)

    def abandon(self, record_id: str, raw: dict) -> dict[str, object]:
        self._keys(raw, {"actor"})
        actor = self._actor(raw.get("actor"))
        row = self._must(record_id)
        if row["state"] != "deploying":
            raise ValidationError("只有仍在发布中的记录可以标记为未确认")
        if row["jenkins_queue_url"] or row["jenkins_build_url"]:
            raise ValidationError("Jenkins 已返回队列或构建，不能在这里标记为未发布")
        finished = self._stamp()
        self.store.finish(
            record_id,
            {
                "state": "failed",
                "finished_at": finished,
                "updated_at": finished,
                "result_message": "未确认 Jenkins 已接收。%s 将其标记为未发布，请核对 Jenkins 是否仍有构建。"
                % actor,
                "inspect_status": "skipped",
                "inspect_error": "发布未确认，未启动巡检",
            },
        )
        return present(self._must(record_id))
