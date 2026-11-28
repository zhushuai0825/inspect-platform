# -*- coding: utf-8 -*-
"""Public workbench inspect-config writes. Python 3.6 compatible.

The password is generated at runtime. DNS for custom targets is mocked.
This file does not import release_console.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SOURCE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if SOURCE not in sys.path:
    sys.path.insert(0, SOURCE)

import app
import core

BUILTIN_URLS = {
    "home": "http://8.146.206.64/",
    "resume": "http://8.146.206.64/zhushuai/",
    "ops-hub": "http://8.146.206.64/apps/ops-hub.html",
    "ai-test": "http://8.146.206.64/apps/ai-test/",
    "games": "http://8.146.206.64/apps/games/",
    "qa-health": "http://8.146.206.64/qa/api/health",
    "jenkins-login": "http://8.146.206.64/jenkins/login",
}


def _fields(task, **changes):
    body = {
        "name": task["name"],
        "url": task["url"],
        "method": task["method"],
        "expected_status": task["expected_status"],
        "contains": task["contains"],
        "timeout_sec": task["timeout_sec"],
        "enabled": task["enabled"],
        "revision": task["revision"],
    }
    body.update(changes)
    return body


class PublicTaskConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.password = "fixture-" + uuid.uuid4().hex
        self._old_core = (core.DATA, core.DB, core.REPORTS, core.SPOOL)
        core.DATA = self.tmp.name
        core.DB = os.path.join(self.tmp.name, "platform.db")
        core.REPORTS = os.path.join(self.tmp.name, "reports")
        core.SPOOL = os.path.join(self.tmp.name, "spool")
        core.init()
        self._env = {}
        self._remember("RELEASE_PUBLISH_PASSWORD", None)
        self._remember("RELEASE_SECRET_FILE", os.path.join(self.tmp.name, "empty.env"))
        with open(os.environ["RELEASE_SECRET_FILE"], "w") as handle:
            handle.write("\n")
        self._old_reports = app.REPORT_DIR
        app.REPORT_DIR = core.REPORTS
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        app.REPORT_DIR = self._old_reports
        core.DATA, core.DB, core.REPORTS, core.SPOOL = self._old_core
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
        request = Request(
            "http://127.0.0.1:%s%s" % (self.server.server_port, path), data=data, headers=merged)
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

    def _tasks(self):
        return self._http("/qa/api/tasks", 200)["data"]

    def _ids(self):
        return [task["id"] for task in self._tasks()]

    def test_viewing_stays_open_and_lists_builtins(self):
        tasks = self._tasks()
        self.assertEqual([task["id"] for task in tasks[:7]], list(core.BUILTIN_ORDER))
        for task in tasks[:7]:
            self.assertEqual(task["url"], BUILTIN_URLS[task["id"]])
        page = self._http("/qa/", 200)
        self.assertIn("新建检查".encode("utf-8"), page)
        self.assertNotIn("公开访客不能新增".encode("utf-8"), page)
        self.assertNotIn(self.password.encode("utf-8"), page)

    def test_missing_or_wrong_password_does_not_change_tasks(self):
        before = json.dumps(self._tasks(), sort_keys=True)
        missing = self._http("/qa/api/tasks", 403, {
            "name": "不应创建", "url": "https://example.com/health", "method": "GET",
            "expected_status": [200], "contains": "", "timeout_sec": 2, "enabled": False,
        })
        self.assertIn("未配置", missing["error"])
        self.assertNotIn("data", missing)
        self._remember("RELEASE_PUBLISH_PASSWORD", self.password)
        wrong = "wrong-" + uuid.uuid4().hex
        denied = self._http("/api/tasks/home", 403, _fields(self._tasks()[0], name="被改掉", password=wrong))
        self.assertEqual(denied["error"], "口令不正确")
        self.assertNotIn(wrong, json.dumps(denied))
        self.assertNotIn(self.password, json.dumps(denied))
        header = self._http("/qa/api/tasks", 403, {
            "name": "不应创建", "url": "https://example.com/health", "method": "GET",
            "expected_status": [200], "contains": "", "timeout_sec": 2, "enabled": False,
        }, {"X-QA-Admin": "1"})
        self.assertEqual(header["error"], "口令不正确")
        self.assertEqual(json.dumps(self._tasks(), sort_keys=True), before)
        self.assertEqual(self._ids()[:7], list(core.BUILTIN_ORDER))

    def test_query_password_and_cross_origin_do_not_write(self):
        self._remember("RELEASE_PUBLISH_PASSWORD", self.password)
        before = self._ids()
        queried = self._http("/api/tasks?password=%s" % self.password, 403, {
            "name": "不应创建", "url": "https://example.com/health", "method": "GET",
            "expected_status": [200], "contains": "", "timeout_sec": 2, "enabled": False,
        })
        self.assertEqual(queried["error"], "口令不正确")
        crossed = self._http("/qa/api/tasks", 403, {
            "name": "不应创建", "url": "https://example.com/health", "method": "GET",
            "expected_status": [200], "contains": "", "timeout_sec": 2, "enabled": False,
            "password": self.password,
        }, {"Origin": "https://evil.example"})
        self.assertIn("跨站", crossed["error"])
        self.assertNotIn(self.password, json.dumps(crossed))
        self.assertEqual(self._ids(), before)

    def test_correct_password_creates_edits_and_toggles(self):
        self._remember("RELEASE_PUBLISH_PASSWORD", self.password)
        body = {
            "name": "外部健康检查", "url": "https://example.com/health", "method": "GET",
            "expected_status": [200, 204], "contains": "", "timeout_sec": 2, "enabled": False,
            "password": self.password,
        }
        with mock.patch.object(core, "resolve_public", return_value=["8.8.8.8"]):
            created = self._http("/qa/api/tasks", 200, body)
        task = created["data"]
        self.assertEqual(task["name"], "外部健康检查")
        self.assertFalse(task["enabled"])
        self.assertFalse(task["builtin"])
        self.assertNotIn(self.password, json.dumps(created))
        edited = _fields(task, name="外部健康检查已改", enabled=True, password=self.password)
        with mock.patch.object(core, "resolve_public", return_value=["8.8.8.8"]):
            updated = self._http("/api/tasks/%s" % task["id"], 200, edited)
        self.assertEqual(updated["data"]["name"], "外部健康检查已改")
        self.assertTrue(updated["data"]["enabled"])
        self.assertEqual(updated["data"]["revision"], 2)
        toggled = _fields(updated["data"], enabled=False, password=self.password)
        with mock.patch.object(core, "resolve_public", return_value=["8.8.8.8"]):
            off = self._http("/qa/api/tasks/%s" % task["id"], 200, toggled)
        self.assertFalse(off["data"]["enabled"])
        self.assertEqual(len(self._tasks()), 8)

    def test_builtin_url_stays_locked(self):
        self._remember("RELEASE_PUBLISH_PASSWORD", self.password)
        home = self._tasks()[0]
        self.assertEqual(home["id"], "home")
        renamed = self._http("/qa/api/tasks/home", 200, _fields(
            home, name="主页检查", enabled=False, timeout_sec=10, password=self.password))
        self.assertEqual(renamed["data"]["url"], BUILTIN_URLS["home"])
        self.assertEqual(renamed["data"]["name"], "主页检查")
        self.assertFalse(renamed["data"]["enabled"])
        denied = self._http("/api/tasks/home", 400, _fields(
            renamed["data"], url="http://127.0.0.1/admin", password=self.password))
        self.assertIn("固定", denied["error"])
        self.assertNotIn(self.password, json.dumps(denied))
        current = [task for task in self._tasks() if task["id"] == "home"][0]
        self.assertEqual(current["url"], BUILTIN_URLS["home"])
        for task in self._tasks():
            if task["id"] in BUILTIN_URLS:
                self.assertEqual(task["url"], BUILTIN_URLS[task["id"]])

    def test_workbench_still_loads_when_password_module_is_unavailable(self):
        original = app.Handler._release_module
        def unavailable(self):
            return None
        app.Handler._release_module = unavailable
        try:
            denied = self._http("/qa/api/tasks", 503, {
                "name": "不应创建", "url": "https://example.com/health", "method": "GET",
                "expected_status": [200], "contains": "", "timeout_sec": 2, "enabled": False,
                "password": self.password,
            })
            self.assertIn("未修改", denied["error"])
            self.assertEqual(len(self._tasks()), 7)
            page = self._http("/qa/", 200)
            self.assertIn("巡检任务".encode("utf-8"), page)
            overview = self._http("/qa/api/overview", 200)
            self.assertEqual(len(overview["data"]["tasks"]), 7)
            releases = self._http("/qa/api/releases", 200)
            self.assertEqual(releases["data"]["records"], [])
            self.assertTrue(releases["data"]["unavailable"])
        finally:
            app.Handler._release_module = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
