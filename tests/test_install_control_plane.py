#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实跑 INSTALL-CONTROL-PLANE.sh。

已存在且不符合前提的生产目录必须原样留下，脚本失败退出。
成功安装时也不在 /opt/inspect-platform/data 下创建 spool。
Python 3.6 兼容：不用 f-string，不用 capture_output。
"""
from __future__ import print_function

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, 'control-plane', 'INSTALL-CONTROL-PLANE.sh')
NOTIFY = os.path.join(REPO, 'deploy-scripts', 'jenkins', 'notify-deploy.sh')
TRIGGER = os.path.join(REPO, 'deploy-scripts', 'jenkins-scripts', 'trigger-deploy.sh')
DISPATCHER_DIR = os.path.join(REPO, 'deploy-scripts', 'host-dispatcher')
COMMIT = '0123456789abcdef0123456789abcdef01234567'
HOST_PATHS = (
    '/opt/inspect-deploy',
    '/opt/inspect-platform',
    '/opt/inspect-ops',
    '/var/lib/inspect-deploy',
    '/etc/sudoers.d/inspect-deploy',
    '/etc/systemd/system/inspect-deploy-dispatcher.service',
)
PREEXISTING_USER = False
PREEXISTING_GROUP = False
HOST_BEFORE = None


def _run(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )


def _exists(name, kind):
    proc = _run(['getent', kind, name])
    return proc.returncode == 0


def _snapshot(paths):
    proc = _run(['sudo', '-n', sys.executable, __file__, '--snapshot'] + list(paths))
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def _host_presence():
    found = {}
    for path in HOST_PATHS:
        found[path] = os.path.lexists(path)
    return found


def setUpModule():
    global PREEXISTING_USER, PREEXISTING_GROUP, HOST_BEFORE
    PREEXISTING_USER = _exists('inspect-dispatcher', 'passwd')
    PREEXISTING_GROUP = _exists('inspect-deploy', 'group')
    HOST_BEFORE = _host_presence()


def tearDownModule():
    if not PREEXISTING_USER and _exists('inspect-dispatcher', 'passwd'):
        _run(['sudo', '-n', 'userdel', 'inspect-dispatcher'])
    if not PREEXISTING_GROUP and _exists('inspect-deploy', 'group'):
        _run(['sudo', '-n', 'groupdel', 'inspect-deploy'])


def _snapshot_main(paths):
    out = {}
    for path in paths:
        if not os.path.lexists(path):
            out[path] = None
            continue
        st = os.lstat(path)
        item = {
            'mode': stat.S_IMODE(st.st_mode),
            'uid': st.st_uid,
            'gid': st.st_gid,
            'ino': st.st_ino,
            'mtime_ns': st.st_mtime_ns,
            'size': st.st_size,
            'islink': stat.S_ISLNK(st.st_mode),
        }
        if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            digest = hashlib.sha256()
            with open(path, 'rb') as handle:
                digest.update(handle.read())
            item['sha256'] = digest.hexdigest()
        out[path] = item
    json.dump(out, sys.stdout)
    return 0


class TestInstallControlPlane(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='install-cp-')
        self.addCleanup(self._remove_root)

    def _remove_root(self):
        if os.path.isdir(self.root):
            proc = _run(['sudo', '-n', 'rm', '-rf', self.root])
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr)

    def _paths(self):
        base = self.root
        return [
            os.path.join(base, 'opt', 'inspect-platform'),
            os.path.join(base, 'opt', 'inspect-platform', 'app.py'),
            os.path.join(base, 'opt', 'inspect-platform', 'data'),
            os.path.join(base, 'opt', 'inspect-platform', 'data', 'secret.env'),
            os.path.join(base, 'opt', 'inspect-ops'),
            os.path.join(base, 'opt', 'inspect-ops', 'state'),
            os.path.join(base, 'opt', 'inspect-platform', 'data', 'deploy-spool'),
        ]

    def _assert_host_untouched(self):
        self.assertEqual(_host_presence(), HOST_BEFORE)

    def _run_install(self):
        return _run([
            'sudo', '-n', 'env', 'INSTALL_ROOT=' + self.root,
            'bash', INSTALL,
        ])

    def test_mismatched_production_dirs_stay_and_fail_closed(self):
        owner = os.getuid() if os.getuid() != 0 else 1000
        script = """
set -euo pipefail
root="$1"
owner="$2"
mkdir -p "$root/opt/inspect-platform/data" "$root/opt/inspect-ops"
printf 'print(1)\\n' > "$root/opt/inspect-platform/app.py"
printf 'fixture-not-a-secret\\n' > "$root/opt/inspect-platform/data/secret.env"
chown "$owner:$owner" "$root/opt/inspect-platform" "$root/opt/inspect-platform/app.py"
chown "$owner:$owner" "$root/opt/inspect-platform/data" "$root/opt/inspect-ops"
chmod 777 "$root/opt/inspect-platform" "$root/opt/inspect-platform/data"
chmod 755 "$root/opt/inspect-ops"
chmod 644 "$root/opt/inspect-platform/app.py"
chown root:root "$root/opt/inspect-platform/data/secret.env"
chmod 600 "$root/opt/inspect-platform/data/secret.env"
"""
        proc = _run(['sudo', '-n', 'bash', '-c', script, 'bash', self.root, str(owner)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before = _snapshot(self._paths())
        users_before = _exists('inspect-dispatcher', 'passwd')

        result = self._run_install()

        after = _snapshot(self._paths())
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('Leaving it unchanged.', result.stderr)
        self.assertEqual(before, after)
        self.assertIsNone(after[self._paths()[6]])
        self.assertFalse(os.path.lexists(os.path.join(self.root, 'opt', 'inspect-deploy')))
        self.assertFalse(os.path.lexists(os.path.join(self.root, 'var', 'lib', 'inspect-deploy')))
        self.assertEqual(_exists('inspect-dispatcher', 'passwd'), users_before)
        self._assert_host_untouched()

    def test_live_layout_keeps_dirs_and_spool_is_not_under_data(self):
        owner = os.getuid() if os.getuid() != 0 else 1000
        script = """
set -euo pipefail
root="$1"
owner="$2"
mkdir -p "$root/opt/inspect-platform/data" "$root/opt/inspect-ops/state"
printf 'print(1)\\n' > "$root/opt/inspect-platform/app.py"
printf 'fixture-not-a-secret\\n' > "$root/opt/inspect-platform/data/secret.env"
chown "$owner:$owner" "$root/opt/inspect-platform" "$root/opt/inspect-platform/app.py"
chown root:root "$root/opt/inspect-platform/data" "$root/opt/inspect-platform/data/secret.env"
chown root:root "$root/opt/inspect-ops" "$root/opt/inspect-ops/state"
chmod 755 "$root/opt/inspect-platform" "$root/opt/inspect-platform/data"
chmod 644 "$root/opt/inspect-platform/app.py"
chmod 600 "$root/opt/inspect-platform/data/secret.env"
chmod 700 "$root/opt/inspect-ops" "$root/opt/inspect-ops/state"
"""
        proc = _run(['sudo', '-n', 'bash', '-c', script, 'bash', self.root, str(owner)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before = _snapshot(self._paths())

        result = self._run_install()

        after = _snapshot(self._paths())
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self._paths():
            if path.endswith('deploy-spool'):
                self.assertIsNone(after[path])
            else:
                self.assertEqual(before[path], after[path], path)
        spool = os.path.join(self.root, 'var', 'lib', 'inspect-deploy', 'spool')
        spool_meta = _snapshot([spool])[spool]
        self.assertIsNotNone(spool_meta)
        self.assertEqual(spool_meta['mode'], 0o750)
        passwd = _run(['getent', 'passwd', 'inspect-dispatcher'])
        group = _run(['getent', 'group', 'inspect-deploy'])
        self.assertEqual(passwd.returncode, 0, passwd.stderr)
        self.assertEqual(group.returncode, 0, group.stderr)
        uid = int(passwd.stdout.split(':')[2])
        gid = int(group.stdout.split(':')[2])
        self.assertEqual(spool_meta['uid'], uid)
        self.assertEqual(spool_meta['gid'], gid)
        config_path = os.path.join(
            self.root, 'opt', 'inspect-deploy', 'config', 'deploy-config.json')
        config_proc = _run(['sudo', '-n', 'cat', config_path])
        self.assertEqual(config_proc.returncode, 0, config_proc.stderr)
        config = json.loads(config_proc.stdout)
        self.assertEqual(config['spool_dir'], '/var/lib/inspect-deploy/spool')
        self.assertNotIn('/opt/inspect-platform/data', config['spool_dir'])
        unit_path = os.path.join(
            self.root, 'opt', 'inspect-deploy', 'systemd',
            'inspect-deploy-dispatcher.service')
        with open(unit_path) as handle:
            unit = handle.read()
        self.assertIn('ReadWritePaths=/var/lib/inspect-deploy', unit)
        self.assertNotIn('/opt/inspect-platform/data/deploy-spool', unit)
        sudoers = os.path.join(self.root, 'etc', 'sudoers.d', 'inspect-deploy')
        sudoers_meta = _snapshot([sudoers])[sudoers]
        self.assertIsNotNone(sudoers_meta)
        self.assertEqual(sudoers_meta['mode'], 0o440)
        self.assertFalse(os.path.lexists('/etc/sudoers.d/inspect-deploy') and not HOST_BEFORE['/etc/sudoers.d/inspect-deploy'])
        self._assert_host_untouched()

    def test_missing_state_does_not_change_ops_owner_or_mode(self):
        owner = os.getuid() if os.getuid() != 0 else 1000
        script = """
set -euo pipefail
root="$1"
owner="$2"
mkdir -p "$root/opt/inspect-platform/data" "$root/opt/inspect-ops"
printf 'print(1)\\n' > "$root/opt/inspect-platform/app.py"
printf 'fixture-not-a-secret\\n' > "$root/opt/inspect-platform/data/secret.env"
chown "$owner:$owner" "$root/opt/inspect-platform" "$root/opt/inspect-platform/app.py"
chown root:root "$root/opt/inspect-platform/data" "$root/opt/inspect-platform/data/secret.env"
chown root:root "$root/opt/inspect-ops"
chmod 755 "$root/opt/inspect-platform" "$root/opt/inspect-platform/data"
chmod 644 "$root/opt/inspect-platform/app.py"
chmod 600 "$root/opt/inspect-platform/data/secret.env"
chmod 700 "$root/opt/inspect-ops"
"""
        proc = _run(['sudo', '-n', 'bash', '-c', script, 'bash', self.root, str(owner)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ops = os.path.join(self.root, 'opt', 'inspect-ops')
        state = os.path.join(ops, 'state')
        before = _snapshot([ops, state])

        result = self._run_install()

        after = _snapshot([ops, state])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before[ops]['mode'], after[ops]['mode'])
        self.assertEqual(before[ops]['uid'], after[ops]['uid'])
        self.assertEqual(before[ops]['gid'], after[ops]['gid'])
        self.assertEqual(before[ops]['ino'], after[ops]['ino'])
        self.assertEqual(after[ops]['mode'], 0o700)
        self.assertIsNotNone(after[state])
        self.assertEqual(after[state]['mode'], 0o700)
        self.assertEqual(after[state]['uid'], 0)
        self.assertEqual(after[state]['gid'], 0)
        self.assertFalse(os.path.lexists(
            os.path.join(self.root, 'opt', 'inspect-platform', 'data', 'deploy-spool')))
        self._assert_host_untouched()

    def test_symlink_platform_fails_without_following_it(self):
        target = os.path.join(self.root, 'elsewhere')
        link = os.path.join(self.root, 'opt', 'inspect-platform')
        script = """
set -euo pipefail
root="$1"
mkdir -p "$root/elsewhere" "$root/opt"
printf 'keep\\n' > "$root/elsewhere/marker"
chmod 755 "$root/elsewhere"
ln -s "$root/elsewhere" "$root/opt/inspect-platform"
"""
        proc = _run(['sudo', '-n', 'bash', '-c', script, 'bash', self.root])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before = _snapshot([target, os.path.join(target, 'marker'), link])

        result = self._run_install()

        after = _snapshot([target, os.path.join(target, 'marker'), link])
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('Leaving it unchanged.', result.stderr)
        self.assertIn('symlink', result.stderr)
        self.assertEqual(before, after)
        self.assertFalse(os.path.lexists(
            os.path.join(self.root, 'opt', 'inspect-platform', 'data', 'deploy-spool')))
        self._assert_host_untouched()


class TestSpoolNotUnderAppData(unittest.TestCase):
    def test_notify_refuses_without_creating_directory(self):
        forbidden = '/opt/inspect-platform/data/deploy-spool'
        existed = os.path.lexists(forbidden)
        proc = _run([
            'env', 'SPOOL_DIR=' + forbidden, 'bash', NOTIFY, COMMIT,
        ])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('must not be under /opt/inspect-platform/data', proc.stderr)
        self.assertEqual(os.path.lexists(forbidden), existed)

    def test_notify_writes_outside_app_data(self):
        spool = tempfile.mkdtemp(prefix='notify-spool-')
        self.addCleanup(shutil.rmtree, spool)
        proc = _run(['env', 'SPOOL_DIR=' + spool, 'bash', NOTIFY, COMMIT])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = os.listdir(spool)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith('request-'))

    def test_trigger_refuses_without_creating_directory(self):
        forbidden = '/opt/inspect-platform/data/deploy-spool'
        existed = os.path.lexists(forbidden)
        proc = _run([
            'env', 'SPOOL_DIR=' + forbidden,
            'bash', TRIGGER, COMMIT, 'main', '/tmp/artifact.zip',
        ])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('must not be under /opt/inspect-platform/data', proc.stderr)
        self.assertEqual(os.path.lexists(forbidden), existed)

    def test_dispatcher_default_and_guard_do_not_create_app_data(self):
        sys.path.insert(0, DISPATCHER_DIR)
        try:
            import dispatcher
        finally:
            sys.path.pop(0)
        self.assertEqual(
            dispatcher.DEFAULT_CONFIG['spool_dir'],
            '/var/lib/inspect-deploy/spool',
        )
        forbidden = '/opt/inspect-platform/data/deploy-spool'
        existed = os.path.lexists(forbidden)
        with self.assertRaises(ValueError):
            dispatcher.assert_spool_outside_app_data(forbidden)
        nested = '/opt/inspect-platform/data/foo/../deploy-spool'
        with self.assertRaises(ValueError):
            dispatcher.assert_spool_outside_app_data(nested)
        dispatcher.assert_spool_outside_app_data('/var/lib/inspect-deploy/spool')
        self.assertEqual(os.path.lexists(forbidden), existed)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--snapshot':
        sys.exit(_snapshot_main(sys.argv[2:]))
    unittest.main()
