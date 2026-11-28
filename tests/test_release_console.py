# -*- coding: utf-8 -*-
"""Publish log tests. Python 3.11 runs them; Python 3.6 imports this file and skips.

No live Jenkins, GitHub, or production inspect calls.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SOURCE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if SOURCE not in sys.path:
    sys.path.insert(0, SOURCE)

PY311 = sys.version_info >= (3, 11)
if PY311:
    import core
    import app
    import admin
    from release_console.config import Settings, load
    from release_console.errors import Conflict, NotWired, PublishRejected, ValidationError
    from release_console.httpapi import build_service
    from release_console import httpapi
    from release_console.inspect_hook import run_existing_suite
    from release_console.jenkins_client import JenkinsClient, JenkinsUnknown
    from release_console.service import PublishService
    from release_console.store import Store


TOKEN = "test-token-not-real"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40


def _settings(db_path, **overrides):
    values = {
        "publish_host": "8.146.206.64",
        "host_valid": True,
        "jenkins_url": "http://jenkins.test/jenkins",
        "jenkins_user": "admin",
        "jenkins_token": TOKEN,
        "frontend_job": "frontend-publish",
        "backend_job": "backend-publish",
        "inspect_after_publish": True,
        "db_path": db_path,
    }
    values.update(overrides)
    return Settings(**values)


class FakeJenkins(object):
    def __init__(self):
        self.calls = []
        self.mode = "accept"
        self.location = "http://jenkins.test/jenkins/queue/item/7/"
        self.build_url = "http://jenkins.test/jenkins/job/frontend-publish/15/"
        self.release = None
        self.started = None
        self.hold_posts = 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if "crumbIssuer" in url:
            return (404, {}, b"")
        if method == "POST":
            if self.hold_posts:
                self.hold_posts -= 1
                self.started.set()
                self.release.wait(3)
            if self.mode == "reject":
                return (403, {}, b"denied")
            if self.mode == "down":
                raise OSError("no route")
            if self.mode == "http500":
                return (500, {}, b"")
            if self.mode == "leak":
                raise JenkinsUnknown("token " + TOKEN)
            return (201, {"Location": self.location}, b"")
        if "/queue/item/" in url:
            if self.mode == "queued":
                return (200, {}, b'{"cancelled": false}')
            if self.mode == "cancelled":
                return (200, {}, b'{"cancelled": true}')
            payload = {"cancelled": False, "executable": {"url": self.build_url}}
            return (200, {}, json.dumps(payload).encode("utf-8"))
        if "/api/json" in url:
            if self.mode == "building":
                payload = {"building": True, "result": None, "url": self.build_url}
            elif self.mode == "failure":
                payload = {"building": False, "result": "FAILURE", "url": self.build_url}
            elif self.mode == "foreign-build":
                payload = {"building": False, "result": "SUCCESS", "url": "http://evil.example/job/1/"}
            else:
                payload = {"building": False, "result": "SUCCESS", "url": self.build_url}
            return (200, {}, json.dumps(payload).encode("utf-8"))
        return (500, {}, b"")


@unittest.skipUnless(PY311, "release console requires Python 3.11")
class ReleaseConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "release_console.db")
        self.store = Store(self.db)
        self.store.init()
        self.fake = FakeJenkins()
        self.inspected = []
        self.settings = _settings(self.db)
        self.service = self._service()
        self._env = {}

    def tearDown(self):
        httpapi.TRANSPORT = None
        httpapi.INSPECT = None
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _service(self, **overrides):
        settings = self.settings if not overrides else _settings(self.db, **overrides)
        if overrides:
            self.settings = settings
        client = JenkinsClient(settings.jenkins_url, settings.jenkins_user, settings.jenkins_token, self.fake)
        return PublishService(self.store, client, settings, self._inspect)

    def _inspect(self, run_id):
        self.inspected.append(run_id)
        return {"id": run_id, "status": "passed"}

    def _publish(self, service="frontend", version="2026.11.26", commit="", artifact="web-2026"):
        return self.service.publish({
            "service": service,
            "actor": "朱帅",
            "version": version,
            "commit": commit,
            "artifact": artifact,
        })

    def _succeed(self, record_id):
        self.fake.mode = "success"
        return self.service.refresh(record_id)

    def test_unconfigured_publish_writes_nothing_and_does_not_call_jenkins(self):
        self.service = self._service(frontend_job="", backend_job="", jenkins_token="")
        with self.assertRaises(NotWired):
            self._publish()
        self.assertEqual(self.store.list_all(), [])
        self.assertEqual(self.fake.calls, [])
        status = self.service.status()
        self.assertEqual(status["records"], [])
        self.assertFalse(status["services"]["frontend"]["wired"])

    def test_invalid_job_name_is_unwired(self):
        self.service = self._service(frontend_job="../other")
        with self.assertRaises(NotWired):
            self._publish()
        self.assertEqual(self.fake.calls, [])

    def test_publish_submits_identifiers_only(self):
        row = self._publish(commit=COMMIT_A)
        self.assertEqual(row["state"], "deploying")
        self.assertEqual(row["host"], "8.146.206.64")
        self.assertNotIn("jenkins_queue_url", row)
        self.assertTrue(row["jenkins_build_url"] == "")
        post = [item for item in self.fake.calls if item[0] == "POST"]
        self.assertEqual(len(post), 1)
        url = post[0][1]
        self.assertIn("/job/frontend-publish/buildWithParameters?", url)
        self.assertIn("ACTION=deploy", url)
        self.assertIn("COMMIT=" + COMMIT_A, url)
        self.assertIn("SERVICE=frontend", url)
        self.assertIn("TICKET=" + row["id"], url)
        self.assertNotIn("github.com", url)
        self.assertNotIn("repo", url.lower())
        self.assertNotIn(TOKEN, url)
        self.assertNotIn(TOKEN, json.dumps(self.service.status()))

    def test_client_cannot_choose_host_or_repository(self):
        with self.assertRaises(ValidationError):
            self.service.publish({
                "service": "frontend",
                "actor": "朱帅",
                "version": "1",
                "host": "10.0.0.8",
                "repo_url": "https://github.com/example/app",
            })
        self.assertEqual(self.store.list_all(), [])

    def test_commit_or_version_required(self):
        with self.assertRaises(ValidationError):
            self._publish(version="", commit="", artifact="only-artifact")
        with self.assertRaises(ValidationError):
            self._publish(commit="not-a-sha")
        self.assertEqual(self.store.list_all(), [])

    def test_refresh_success_runs_inspect_once(self):
        row = self._publish()
        done = self._succeed(row["id"])
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["inspect_status"], "passed")
        self.assertEqual(done["inspect_run_id"], "rel-" + row["id"])
        self.assertIn("/job/frontend-publish/15/", done["jenkins_build_url"])
        again = self.service.refresh(row["id"])
        self.assertEqual(self.inspected, ["rel-" + row["id"]])
        self.assertEqual(again["inspect_run_id"], done["inspect_run_id"])

    def test_refresh_failure_runs_inspect_and_is_not_success(self):
        row = self._publish()
        self.fake.mode = "failure"
        done = self.service.refresh(row["id"])
        self.assertEqual(done["state"], "failed")
        self.assertEqual(len(self.inspected), 1)
        self.assertNotEqual(done["state"], "succeeded")

    def test_jenkins_reject_does_not_inspect(self):
        self.fake.mode = "reject"
        with self.assertRaises(PublishRejected):
            self._publish()
        rows = self.store.list_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIn("未切换", rows[0]["result_message"])
        self.assertEqual(rows[0]["inspect_status"], "skipped")
        self.assertEqual(self.inspected, [])

    def test_jenkins_outage_keeps_the_lock(self):
        self.fake.mode = "down"
        row = self._publish()
        self.assertEqual(row["state"], "deploying")
        self.assertTrue(row["can_abandon"])
        with self.assertRaises(Conflict):
            self._publish(version="2")
        self.assertEqual(self.inspected, [])

    def test_http_500_keeps_the_lock(self):
        self.fake.mode = "http500"
        row = self._publish()
        self.assertEqual(row["state"], "deploying")
        self.assertIn("未确认", row["result_message"])
        with self.assertRaises(Conflict):
            self._publish(version="2")

    def test_external_queue_link_is_not_stored(self):
        self.fake.location = "http://evil.example/queue/item/1/"
        row = self._publish()
        self.assertEqual(row["state"], "deploying")
        self.assertTrue(row["can_abandon"])
        stored = self.store.get(row["id"])
        self.assertEqual(stored["jenkins_queue_url"], "")
        self.assertNotIn("evil.example", json.dumps(row))

    def test_external_build_link_is_not_stored(self):
        row = self._publish()
        self.fake.mode = "foreign-build"
        done = self.service.refresh(row["id"])
        self.assertEqual(done["state"], "succeeded")
        self.assertNotIn("evil.example", done["jenkins_build_url"])
        self.assertIn("jenkins.test", done["jenkins_build_url"])

    def test_abandon_only_without_queue_and_does_not_inspect(self):
        self.fake.mode = "down"
        row = self._publish()
        abandoned = self.service.abandon(row["id"], {"actor": "朱帅"})
        self.assertEqual(abandoned["state"], "failed")
        self.assertEqual(abandoned["inspect_status"], "skipped")
        self.assertEqual(self.inspected, [])
        self._publish(version="2")

    def test_abandon_rejected_after_queue_exists(self):
        row = self._publish()
        with self.assertRaises(ValidationError):
            self.service.abandon(row["id"], {"actor": "朱帅"})
        self.assertEqual(self.store.get(row["id"])["state"], "deploying")

    def test_same_service_lock_is_held_during_trigger(self):
        self.fake.started = threading.Event()
        self.fake.release = threading.Event()
        self.fake.hold_posts = 1
        found = []

        def first():
            try:
                found.append(("ok", self._publish()))
            except Exception as err:  # pragma: no cover - failure path
                found.append(("error", type(err).__name__))

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(self.fake.started.wait(2))
        with self.assertRaises(Conflict):
            self._publish(version="2")
        self._publish(service="backend", version="9", artifact="api-9")
        self.fake.release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(found[0][0], "ok")
        states = {(row["service"], row["state"]) for row in self.store.list_all()}
        self.assertIn(("frontend", "deploying"), states)
        self.assertIn(("backend", "deploying"), states)

    def test_rollback_requires_an_earlier_success(self):
        row = self._publish(commit=COMMIT_A, version="1")
        self._succeed(row["id"])
        with self.assertRaises(ValidationError):
            self.service.rollback(row["id"], {"actor": "朱帅"})
        self.assertEqual(len(self.store.list_all()), 1)

    def test_rollback_restores_previous_success(self):
        first = self._publish(commit=COMMIT_A, version="1", artifact="web-1")
        self._succeed(first["id"])
        second = self._publish(commit=COMMIT_B, version="2", artifact="web-2")
        self._succeed(second["id"])
        self.inspected = []
        self.fake.calls = []
        started = self.service.rollback(second["id"], {"actor": "值班"})
        self.assertEqual(started["state"], "deploying")
        self.assertEqual(started["commit_sha"], COMMIT_A)
        self.assertEqual(started["version"], "1")
        post = [item[1] for item in self.fake.calls if item[0] == "POST"]
        self.assertEqual(len(post), 1)
        self.assertIn("ACTION=rollback", post[0])
        self.assertIn("COMMIT=" + COMMIT_A, post[0])
        self.fake.mode = "success"
        done = self.service.refresh(started["id"])
        self.assertEqual(done["state"], "rolled_back")
        self.assertEqual(self.store.get(second["id"])["state"], "rolled_back")
        self.assertEqual(self.store.get(first["id"])["state"], "succeeded")
        status = self.service.status()
        self.assertEqual(status["services"]["frontend"]["current_id"], started["id"])
        self.assertEqual(len(self.inspected), 1)

    def test_failed_rollback_keeps_current_success(self):
        first = self._publish(commit=COMMIT_A, version="1")
        self._succeed(first["id"])
        second = self._publish(commit=COMMIT_B, version="2")
        self._succeed(second["id"])
        started = self.service.rollback(second["id"], {"actor": "值班"})
        self.fake.mode = "failure"
        done = self.service.refresh(started["id"])
        self.assertEqual(done["state"], "failed")
        self.assertEqual(self.store.get(second["id"])["state"], "succeeded")
        self.assertEqual(self.service.status()["services"]["frontend"]["current_id"], second["id"])

    def test_third_success_rolls_back_one_step(self):
        ids = []
        for commit, version in ((COMMIT_A, "1"), (COMMIT_B, "2"), (COMMIT_C, "3")):
            row = self._publish(commit=commit, version=version, artifact=version)
            self._succeed(row["id"])
            ids.append(row["id"])
        started = self.service.rollback(ids[2], {"actor": "值班"})
        self.assertEqual(started["commit_sha"], COMMIT_B)
        self.fake.mode = "success"
        done = self.service.refresh(started["id"])
        self.assertEqual(done["state"], "rolled_back")
        self.assertTrue(self.service.status()["services"]["frontend"]["can_rollback"])

    def test_inspect_can_be_disabled(self):
        self.service = self._service(inspect_after_publish=False)
        row = self._publish()
        done = self._succeed(row["id"])
        self.assertEqual(done["inspect_status"], "disabled")
        self.assertEqual(self.inspected, [])

    def test_inspect_error_does_not_rewrite_publish_state(self):
        def broken(_run_id):
            raise RuntimeError("inspect down")

        self.service.inspect = broken
        row = self._publish()
        done = self._succeed(row["id"])
        self.assertEqual(done["state"], "succeeded")
        self.assertEqual(done["inspect_status"], "error")

    def test_hook_uses_existing_suite_without_editing_builtins(self):
        before = list(core.BUILTIN_ORDER)
        targets = dict(core.BUILTINS)
        with mock.patch("core.execute", return_value={"id": "rel-pub0123456789ab", "status": "passed"}) as run:
            with mock.patch("core.save_task") as save:
                result = run_existing_suite("rel-pub0123456789ab")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(run.call_args.kwargs["trigger"], "jenkins")
        self.assertEqual(run.call_args.kwargs["run_id"], "rel-pub0123456789ab")
        save.assert_not_called()
        self.assertEqual(core.BUILTIN_ORDER, before)
        self.assertEqual(core.BUILTINS, targets)

    def test_secret_file_does_not_override_a_set_environment(self):
        secret = os.path.join(self.tmp.name, "secret.env")
        with open(secret, "w") as handle:
            handle.write("JENKINS_API_TOKEN=from-file\nRELEASE_JENKINS_FRONTEND_DEPLOY_JOB=from-file-job\n")
        loaded = load(
            {
                "JENKINS_URL": "http://jenkins.test/jenkins",
                "JENKINS_USER": "admin",
                "JENKINS_API_TOKEN": TOKEN,
                "RELEASE_DB_PATH": self.db,
            },
            secret_file=secret,
        )
        self.assertEqual(loaded.jenkins_token, TOKEN)
        self.assertEqual(loaded.frontend_job, "from-file-job")
        self.assertNotIn(TOKEN, repr(loaded))
        self.assertNotIn("from-file", repr(loaded).replace("from-file-job", ""))
        with open(os.path.join(SOURCE, "deploy", "release-console.env.example"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("JENKINS_API_TOKEN=", text)
        self.assertNotIn("ghp_", text)
        self.assertNotIn("from-file", text)

    def test_templates_stay_on_an_isolated_agent(self):
        for name in ("frontend-publish.Jenkinsfile", "backend-publish.Jenkinsfile"):
            with open(os.path.join(SOURCE, "jenkins", "templates", name), encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("agent none", text)
            self.assertIn("inspect-isolated", text)
            self.assertNotIn("agent any", text)
            self.assertNotIn("github.com", text)
            self.assertIn("UNFINISHED", text)

    def test_public_app_does_not_import_release_console(self):
        with open(os.path.join(SOURCE, "app.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("import release_console", source)
        self.assertNotIn("from release_console", source)
        self.assertIn("/api/releases", source)

    def _remember_env(self, key, value):
        self._env.setdefault(key, os.environ.get(key))
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _wire_env(self):
        secret = os.path.join(self.tmp.name, "empty.env")
        open(secret, "w").close()
        assignments = {
            "RELEASE_DB_PATH": self.db,
            "RELEASE_SECRET_FILE": secret,
            "RELEASE_PUBLISH_HOST": "8.146.206.64",
            "RELEASE_INSPECT_AFTER_PUBLISH": "1",
            "JENKINS_URL": "http://jenkins.test/jenkins",
            "JENKINS_USER": "admin",
            "JENKINS_API_TOKEN": TOKEN,
            "RELEASE_JENKINS_FRONTEND_DEPLOY_JOB": "frontend-publish",
            "RELEASE_JENKINS_BACKEND_DEPLOY_JOB": "backend-publish",
            "QA_DATA_DIR": self.tmp.name,
        }
        for key, value in assignments.items():
            self._remember_env(key, value)
        httpapi.TRANSPORT = self.fake
        httpapi.INSPECT = self._inspect

    def _http(self, server, path, expected, body=None, headers=None):
        merged = headers or {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        request = Request("http://127.0.0.1:%s%s" % (server.server_port, path), data=data, headers=merged)
        try:
            response = urlopen(request, timeout=5)
            code = response.status
            raw = response.read()
        except HTTPError as err:
            code = err.code
            raw = err.read()
        self.assertEqual(code, expected, (path, raw[:300]))
        if path.startswith("/api") or path.startswith("/qa/api"):
            return json.loads(raw.decode("utf-8"))
        return raw

    def test_admin_publish_is_loopback_only_and_public_dashboard_rejects_it(self):
        old = (core.DATA, core.DB, core.REPORTS, core.SPOOL, app.REPORT_DIR)
        core.DATA = self.tmp.name
        core.DB = os.path.join(self.tmp.name, "platform.db")
        core.REPORTS = os.path.join(self.tmp.name, "reports")
        core.SPOOL = os.path.join(self.tmp.name, "spool")
        app.REPORT_DIR = core.REPORTS
        core.init()
        self._wire_env()
        public = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        private = app.ThreadingHTTPServer(("127.0.0.1", 0), admin.AdminHandler)
        for server in (public, private):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self._remember_env("RELEASE_PUBLISH_PASSWORD", None)
            wrong = "wrong-%s" % os.urandom(8).hex()
            for path in ("/qa/api/releases", "/api/releases"):
                denied = self._http(public, path, 403, {
                    "service": "frontend", "actor": "朱帅", "version": "1", "password": wrong,
                }, {"X-QA-Admin": "1"})
                self.assertIn("未执行", denied["error"])
                self.assertNotIn(TOKEN, json.dumps(denied))
                self.assertNotIn(wrong, json.dumps(denied))
            self.assertEqual(self.store.list_all(), [])
            self.assertEqual(self.fake.calls, [])
            for path in ("/qa/publish", "/publish"):
                self._http(public, path, 404, {"service": "frontend"}, {"X-QA-Admin": "1"})
            self._http(private, "/api/releases", 403, headers={"Host": "attacker.example"})
            self._http(private, "/api/releases", 403, {"service": "frontend"}, {"X-QA-Admin": "1", "Origin": "https://attacker.example"})
            self._http(private, "/api/releases", 403, {"service": "frontend", "actor": "朱帅", "version": "1"})
            page = self._http(private, "/publish", 200)
            self.assertIn("发布记录".encode("utf-8"), page)
            self.assertNotIn(b"github.com", page)
            status = self._http(private, "/api/releases", 200)
            self.assertEqual(status["data"]["records"], [])
            self.assertNotIn(TOKEN, json.dumps(status))
            created = self._http(private, "/api/releases", 200, {
                "service": "frontend", "actor": "朱帅", "version": "2026.11.26", "commit": "", "artifact": "web-1",
            }, {"X-QA-Admin": "1"})
            self.assertEqual(created["data"]["state"], "deploying")
            self.fake.mode = "success"
            refreshed = self._http(private, "/api/releases/%s/refresh" % created["data"]["id"], 200, {}, {"X-QA-Admin": "1"})
            self.assertEqual(refreshed["data"]["state"], "succeeded")
            self.assertEqual(refreshed["data"]["inspect_run_id"], "rel-" + created["data"]["id"])
            tasks = self._http(private, "/api/tasks", 200)
            self.assertEqual([item["id"] for item in tasks["data"][:7]], list(core.BUILTIN_ORDER))
        finally:
            public.shutdown()
            private.shutdown()
            public.server_close()
            private.server_close()
            core.DATA, core.DB, core.REPORTS, core.SPOOL, app.REPORT_DIR = old

    def test_admin_without_jobs_does_not_invent_a_record(self):
        self._wire_env()
        self._remember_env("RELEASE_JENKINS_FRONTEND_DEPLOY_JOB", "")
        self._remember_env("RELEASE_JENKINS_BACKEND_DEPLOY_JOB", "")
        # Empty string is treated as unset by config. Point the secret file at blanks.
        secret = os.environ["RELEASE_SECRET_FILE"]
        with open(secret, "w") as handle:
            handle.write("RELEASE_JENKINS_FRONTEND_DEPLOY_JOB=\nRELEASE_JENKINS_BACKEND_DEPLOY_JOB=\n")
        private = app.ThreadingHTTPServer(("127.0.0.1", 0), admin.AdminHandler)
        threading.Thread(target=private.serve_forever, daemon=True).start()
        try:
            denied = self._http(private, "/api/releases", 409, {
                "service": "backend", "actor": "朱帅", "version": "1",
            }, {"X-QA-Admin": "1"})
            self.assertIn("未创建", denied["error"])
            status = self._http(private, "/api/releases", 200)
            self.assertEqual(status["data"]["records"], [])
            self.assertEqual(self.fake.calls, [])
        finally:
            private.shutdown()
            private.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
