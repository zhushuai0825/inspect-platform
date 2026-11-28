# -*- coding: utf-8 -*-
"""Public workbench publish actions. Python 3.6 compatible.

The publish password is generated at runtime and never written into the tree.
Jenkins and the inspect suite are fakes. This file does not import release_console.
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SOURCE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if SOURCE not in sys.path:
    sys.path.insert(0, SOURCE)

import app
import core
import publish_board

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class FakeJenkins(object):
    def __init__(self):
        self.calls = []
        self.mode = "accept"
        self.location = "http://jenkins.test/jenkins/queue/item/7/"
        self.build_url = "http://jenkins.test/jenkins/job/frontend-publish/15/"

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if "crumbIssuer" in url:
            return (404, {}, b"")
        if method == "POST":
            if self.mode == "reject":
                return (403, {}, b"denied")
            if self.mode == "down":
                raise OSError("no route")
            return (201, {"Location": self.location}, b"")
        if "/queue/item/" in url:
            payload = {"cancelled": False, "executable": {"url": self.build_url}}
            return (200, {}, json.dumps(payload).encode("utf-8"))
        if "/api/json" in url:
            if self.mode == "failure":
                payload = {"building": False, "result": "FAILURE", "url": self.build_url}
            else:
                payload = {"building": False, "result": "SUCCESS", "url": self.build_url}
            return (200, {}, json.dumps(payload).encode("utf-8"))
        return (500, {}, b"")


class PublicPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "release_console.db")
        self.secret = os.path.join(self.tmp.name, "secret.env")
        self.password = "fixture-" + uuid.uuid4().hex
        self.fake = FakeJenkins()
        self.inspected = []
        self._env = {}
        self._old_transport = publish_board.TRANSPORT
        self._old_inspect = publish_board.INSPECT
        publish_board.TRANSPORT = self.fake
        publish_board.INSPECT = lambda run_id: self.inspected.append(run_id) or {"id": run_id, "status": "passed"}
        self._remember("QA_DATA_DIR", self.tmp.name)
        self._remember("RELEASE_DB_PATH", self.db)
        self._remember("RELEASE_SECRET_FILE", self.secret)
        self._remember("RELEASE_PUBLISH_HOST", "8.146.206.64")
        self._remember("RELEASE_INSPECT_AFTER_PUBLISH", "0")
        self._remember("JENKINS_URL", "http://jenkins.test/jenkins")
        self._remember("JENKINS_USER", "admin")
        self._remember("JENKINS_API_TOKEN", "jenkins-fixture-" + uuid.uuid4().hex)
        self._remember("RELEASE_JENKINS_FRONTEND_DEPLOY_JOB", "frontend-publish")
        self._remember("RELEASE_JENKINS_BACKEND_DEPLOY_JOB", "backend-publish")
        self._remember("RELEASE_PUBLISH_PASSWORD", None)
        with open(self.secret, "w") as handle:
            handle.write("\n")
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        publish_board.TRANSPORT = self._old_transport
        publish_board.INSPECT = self._old_inspect
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _remember(self, key, value):
        self._env.setdefault(key, os.environ.get(key))
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _http(self, path, expected, body=None, headers=None):
        merged = {} if headers is None else dict(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        request = Request("http://127.0.0.1:%s%s" % (self.server.server_port, path), data=data, headers=merged)
        try:
            response = urlopen(request, timeout=5)
            code = getattr(response, "status", 200)
            raw = response.read()
        except HTTPError as err:
            code = err.code
            raw = err.read()
        self.assertEqual(code, expected, (path, raw[:400]))
        if path.startswith("/api") or path.startswith("/qa/api"):
            return json.loads(raw.decode("utf-8"))
        return raw

    def _rows(self):
        if not os.path.exists(self.db):
            return []
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT id, state FROM publishes").fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def _set_password(self):
        self._remember("RELEASE_PUBLISH_PASSWORD", self.password)

    def _body(self, **extra):
        fields = {"service": "frontend", "actor": "朱帅", "version": "2026.11.26", "commit": "", "artifact": "web-1"}
        fields.update(extra)
        return fields

    def test_missing_database_is_an_empty_list(self):
        payload = self._http("/qa/api/releases", 200)
        self.assertEqual(payload["data"]["records"], [])
        self.assertFalse(payload["data"]["unavailable"])
        self.assertFalse(os.path.exists(self.db))
        self.assertNotIn("password", json.dumps(payload))

    def test_seeded_records_are_visible_without_a_password(self):
        store = publish_board.Store(self.db)
        store.init()
        stamp = "2026-11-26T09:00:00.000000+08:00"
        row = dict((key, "") for key in publish_board.COLUMNS)
        row.update({
            "id": "pub0123456789ab", "service": "backend", "action": "deploy", "actor": "朱帅",
            "commit_sha": COMMIT_A, "version": "2026.11.26", "artifact": "api-1",
            "host": "8.146.206.64", "state": "succeeded", "created_at": stamp, "updated_at": stamp,
            "finished_at": stamp, "jenkins_job": "backend-publish",
            "result_message": "Jenkins 构建成功",
        })
        store.insert_deploying(dict(row, state="deploying"))
        store.finish(row["id"], {"state": "succeeded", "updated_at": stamp, "finished_at": stamp, "result_message": "Jenkins 构建成功"})
        payload = self._http("/api/releases", 200)
        shown = payload["data"]["records"][0]
        self.assertEqual(shown["actor"], "朱帅")
        self.assertEqual(shown["service"], "backend")
        self.assertEqual(shown["version"], "2026.11.26")
        self.assertEqual(shown["commit_sha"], COMMIT_A)
        self.assertIn("+08:00", shown["created_at"])
        self.assertNotIn("jenkins_queue_url", shown)
        self.assertNotIn(self.password, json.dumps(payload))

    def test_corrupt_database_does_not_500(self):
        with open(self.db, "wb") as handle:
            handle.write(b"this is not a database")
        payload = self._http("/qa/api/releases", 200)
        self.assertEqual(payload["data"]["records"], [])
        self.assertTrue(payload["data"]["unavailable"])
        self.assertNotIn("this is not a database", json.dumps(payload))

    def test_unset_password_writes_nothing(self):
        denied = self._http("/qa/api/releases", 403, self._body())
        self.assertIn("未配置", denied["error"])
        self.assertNotIn("data", denied)
        self.assertFalse(os.path.exists(self.db))
        self.assertEqual(self.fake.calls, [])

    def test_wrong_password_writes_nothing_and_does_not_call_jenkins(self):
        self._set_password()
        wrong = "wrong-" + uuid.uuid4().hex
        denied = self._http("/api/releases", 403, self._body(password=wrong))
        self.assertEqual(denied["error"], "口令不正确")
        self.assertNotIn(wrong, json.dumps(denied))
        self.assertNotIn(self.password, json.dumps(denied))
        self.assertFalse(os.path.exists(self.db))
        self.assertEqual(self.fake.calls, [])

    def test_password_in_the_query_is_not_accepted(self):
        self._set_password()
        denied = self._http("/api/releases?password=%s" % self.password, 403, self._body())
        self.assertEqual(denied["error"], "口令不正确")
        self.assertFalse(os.path.exists(self.db))

    def test_cross_origin_is_rejected_before_a_write(self):
        self._set_password()
        denied = self._http("/qa/api/releases", 403, self._body(password=self.password), {"Origin": "https://evil.example"})
        self.assertIn("跨站", denied["error"])
        self.assertNotIn(self.password, json.dumps(denied))
        self.assertFalse(os.path.exists(self.db))
        self.assertEqual(self.fake.calls, [])

    def test_valid_password_publishes_refreshes_and_rolls_back(self):
        self._set_password()
        created = self._http("/qa/api/releases", 200, self._body(password=self.password))
        self.assertEqual(created["data"]["state"], "deploying")
        self.assertNotIn(self.password, json.dumps(created))
        self.assertTrue(self.fake.calls)
        for _method, url, headers, _body in self.fake.calls:
            self.assertNotIn(self.password, url)
            self.assertNotIn(self.password, json.dumps(headers))
        self.fake.mode = "success"
        refreshed = self._http("/api/releases/%s/refresh" % created["data"]["id"], 200, {"password": self.password})
        self.assertEqual(refreshed["data"]["state"], "succeeded")
        self.assertEqual(refreshed["data"]["inspect_status"], "disabled")
        self.assertEqual(self.inspected, [])
        self.assertNotIn(self.password, json.dumps(refreshed))
        second = self._http("/qa/api/releases", 200, self._body(version="2026.11.27", commit=COMMIT_B, password=self.password))
        self.fake.mode = "success"
        self._http("/api/releases/%s/refresh" % second["data"]["id"], 200, {"password": self.password})
        rolled = self._http("/api/releases/%s/rollback" % second["data"]["id"], 200, {"actor": "朱帅", "password": self.password})
        self.assertEqual(rolled["data"]["state"], "deploying")
        self.assertEqual(rolled["data"]["action"], "rollback")
        self.assertEqual(rolled["data"]["version"], "2026.11.26")
        self.fake.mode = "success"
        done = self._http("/api/releases/%s/refresh" % rolled["data"]["id"], 200, {"password": self.password})
        self.assertEqual(done["data"]["state"], "rolled_back")
        listing = self._http("/qa/api/releases", 200)
        states = dict((row["id"], row["state"]) for row in listing["data"]["records"])
        self.assertEqual(states[second["data"]["id"]], "rolled_back")
        self.assertNotIn(self.password, json.dumps(listing))

    def test_refresh_rollback_and_abandon_require_the_password(self):
        self._set_password()
        self.fake.mode = "down"
        created = self._http("/api/releases", 200, self._body(password=self.password))
        self.assertTrue(created["data"]["can_abandon"])
        before = len(self._rows())
        for path, body in (
            ("/api/releases/%s/refresh" % created["data"]["id"], {}),
            ("/api/releases/%s/rollback" % created["data"]["id"], {"actor": "朱帅"}),
            ("/api/releases/%s/abandon" % created["data"]["id"], {"actor": "朱帅"}),
        ):
            denied = self._http(path, 403, body)
            self.assertEqual(denied["error"], "口令不正确")
        self.assertEqual(len(self._rows()), before)
        self.assertEqual(self._rows()[0][1], "deploying")
        abandoned = self._http("/api/releases/%s/abandon" % created["data"]["id"], 200, {"actor": "朱帅", "password": self.password})
        self.assertEqual(abandoned["data"]["state"], "failed")
        self.assertEqual(abandoned["data"]["inspect_status"], "skipped")
        self.assertNotIn(self.password, json.dumps(abandoned))

    def test_jenkins_rejection_does_not_echo_the_password(self):
        self._set_password()
        self.fake.mode = "reject"
        denied = self._http("/api/releases", 502, self._body(password=self.password))
        self.assertIn("未切换", denied["error"])
        self.assertEqual(denied["data"]["state"], "failed")
        self.assertNotIn(self.password, json.dumps(denied))
        self.assertEqual(self._rows()[0][1], "failed")

    def test_invalid_publish_with_the_right_password_writes_nothing(self):
        self._set_password()
        denied = self._http("/api/releases", 400, self._body(version="", commit="", password=self.password))
        self.assertFalse(os.path.exists(self.db))
        self.assertEqual(self.fake.calls, [])
        self.assertNotIn(self.password, json.dumps(denied))

    def test_manual_inspect_trigger_does_not_need_the_publish_password(self):
        self._set_password()
        old_trigger = app.trigger_jenkins
        old_reserve = core.reserve_trigger
        app.trigger_jenkins = lambda trigger: {"queued": True, "http_status": 201, "job": "site-http-smoke"}
        core.reserve_trigger = lambda cooldown=60: 0
        try:
            result = self._http("/qa/api/runs", 200, {"trigger": "manual", "mode": "jenkins"})
        finally:
            app.trigger_jenkins = old_trigger
            core.reserve_trigger = old_reserve
        self.assertTrue(result["ok"])
        self.assertNotIn("口令", json.dumps(result, ensure_ascii=False))
        self.assertFalse(os.path.exists(self.db))

    def test_request_log_omits_a_password_query(self):
        import io
        from contextlib import redirect_stdout
        handler = app.Handler.__new__(app.Handler)
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.log_message('"%s" %s %s', "POST /api/releases?password=%s HTTP/1.1" % self.password, "403", "-")
        self.assertNotIn(self.password, buf.getvalue())
        self.assertIn("omitted", buf.getvalue())

    def test_page_has_the_actions_and_no_password_value(self):
        page = self._http("/qa/", 200)
        script = self._http("/qa/app.js", 200)
        self.assertIn("发布记录".encode("utf-8"), page)
        self.assertIn("发布到本服务器".encode("utf-8"), page)
        self.assertIn('type="password"'.encode("utf-8"), page)
        self.assertNotIn(b"value=", page[page.find(b'type="password"') - 80:page.find(b'type="password"') + 80])
        self.assertNotIn(self.password.encode("utf-8"), page)
        self.assertNotIn(self.password.encode("utf-8"), script)
        self.assertIn(b"askPassword", script)

    def test_builtins_are_unchanged(self):
        self.assertEqual(core.BUILTIN_ORDER, ["home", "resume", "ops-hub", "ai-test", "games", "qa-health", "jenkins-login"])
        self.assertEqual(core.BUILTINS["home"][1], "/")
        self.assertEqual(core.BUILTINS["resume"][1], "/zhushuai/")
        self.assertEqual(core.BUILTINS["ops-hub"][1], "/apps/ops-hub.html")
        self.assertEqual(core.BUILTINS["ai-test"][1], "/apps/ai-test/")
        self.assertEqual(core.BUILTINS["games"][1], "/apps/games/")
        self.assertEqual(core.BUILTINS["qa-health"][1], "/qa/api/health")
        self.assertEqual(core.BUILTINS["jenkins-login"][1], "/jenkins/login")
        with open(os.path.join(SOURCE, "app.py"), "r") as handle:
            source = handle.read()
        self.assertNotIn("import release_console", source)
        self.assertNotIn("from release_console", source)

    def test_secret_is_not_stored_in_the_tree(self):
        example = os.path.join(SOURCE, "deploy", "release-console.env.example")
        with open(example, "r") as handle:
            text = handle.read()
        self.assertIn("RELEASE_PUBLISH_PASSWORD=\n", text)
        for name in ("README.md", "release_console/WIRING.md", "static/index.html", "static/app.js", "publish_board.py", "app.py"):
            with open(os.path.join(SOURCE, name), "r") as handle:
                body = handle.read()
            self.assertNotIn(self.password, body)
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("RELEASE_PUBLISH_PASSWORD=") and not stripped.startswith("#"):
                    self.assertEqual(stripped, "RELEASE_PUBLISH_PASSWORD=")

    def test_second_service_is_not_blocked_by_the_first_lock(self):
        self._set_password()
        self.fake.mode = "down"
        first = self._http("/api/releases", 200, self._body(password=self.password))
        self.assertEqual(first["data"]["state"], "deploying")
        blocked = self._http("/api/releases", 409, self._body(version="2026.11.27", password=self.password))
        self.assertIn("不能同时发布", blocked["error"])
        other = self._http("/api/releases", 200, self._body(service="backend", version="2026.11.27", password=self.password))
        self.assertEqual(other["data"]["service"], "backend")
        self.assertEqual(other["data"]["state"], "deploying")

    @unittest.skipUnless(sys.version_info >= (3, 11), "admin store module is Python 3.11")
    def test_columns_match_the_admin_store(self):
        from release_console.store import COLUMNS as admin_columns
        self.assertEqual(publish_board.COLUMNS, admin_columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
