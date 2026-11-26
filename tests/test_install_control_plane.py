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


def sudo_works():
    """返回 (可用, 原因, sudo 路径或 None)。找不到 sudo 时不抛异常。"""
    if os.geteuid() == 0:
        return True, 'already root', None
    sudo = shutil.which('sudo')
    if not sudo:
        return False, 'sudo executable not found', None
    try:
        proc = subprocess.run(
            [sudo, '-n', 'true'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError as exc:
        return False, 'sudo not executable: {}'.format(exc), None
    if proc.returncode != 0:
        return False, 'passwordless sudo to root is not available', sudo
    return True, 'passwordless sudo', sudo


def _snapshot(paths, sudo_bin):
    cmd = [sys.executable, __file__, '--snapshot'] + list(paths)
    if sudo_bin:
        cmd = [sudo_bin, '-n'] + cmd
    proc = _run(cmd)
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
    ok, _reason, sudo_bin = sudo_works()
    if not ok:
        return
    if not PREEXISTING_USER and _exists('inspect-dispatcher', 'passwd'):
        cmd = ['userdel', 'inspect-dispatcher']
        if sudo_bin:
            cmd = [sudo_bin, '-n'] + cmd
        _run(cmd)
    if not PREEXISTING_GROUP and _exists('inspect-deploy', 'group'):
        cmd = ['groupdel', 'inspect-deploy']
        if sudo_bin:
            cmd = [sudo_bin, '-n'] + cmd
        _run(cmd)


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
        ok, reason, sudo_bin = sudo_works()
        if not ok:
            self.skipTest(reason)
        self.sudo = sudo_bin
        self.root = tempfile.mkdtemp(prefix='install-cp-')
        self.addCleanup(self._remove_root)

    def _as_root(self, cmd):
        if self.sudo:
            cmd = [self.sudo, '-n'] + cmd
        return _run(cmd)

    def _remove_root(self):
        if os.path.isdir(self.root):
            proc = self._as_root(['rm', '-rf', self.root])
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr)

    def _snap(self, paths):
        return _snapshot(paths, self.sudo)

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
        return self._as_root([
            'env', 'INSTALL_ROOT=' + self.root,
            'bash', INSTALL,
        ])

    def _assert_accounts_unchanged(self):
        self.assertEqual(_exists('inspect-dispatcher', 'passwd'), PREEXISTING_USER)
        self.assertEqual(_exists('inspect-deploy', 'group'), PREEXISTING_GROUP)

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
        proc = self._as_root(['bash', '-c', script, 'bash', self.root, str(owner)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before = self._snap(self._paths())

        result = self._run_install()

        after = self._snap(self._paths())
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('Leaving it unchanged.', result.stderr)
        self.assertEqual(before, after)
        self.assertIsNone(after[self._paths()[6]])
        self.assertFalse(os.path.lexists(os.path.join(self.root, 'opt', 'inspect-deploy')))
        self.assertFalse(os.path.lexists(os.path.join(self.root, 'var', 'lib', 'inspect-deploy')))
        self._assert_accounts_unchanged()
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
        proc = self._as_root(['bash', '-c', script, 'bash', self.root, str(owner)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before = self._snap(self._paths())

        result = self._run_install()

        after = self._snap(self._paths())
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in self._paths():
            if path.endswith('deploy-spool'):
                self.assertIsNone(after[path])
            else:
                self.assertEqual(before[path], after[path], path)
        spool = os.path.join(self.root, 'var', 'lib', 'inspect-deploy', 'spool')
        spool_meta = self._snap([spool])[spool]
        self.assertIsNotNone(spool_meta)
        self.assertEqual(spool_meta['mode'], 0o750)
        self._assert_accounts_unchanged()
        if PREEXISTING_USER and PREEXISTING_GROUP:
            passwd = _run(['getent', 'passwd', 'inspect-dispatcher'])
            group = _run(['getent', 'group', 'inspect-deploy'])
            self.assertEqual(passwd.returncode, 0, passwd.stderr)
            self.assertEqual(group.returncode, 0, group.stderr)
            self.assertEqual(spool_meta['uid'], int(passwd.stdout.split(':')[2]))
            self.assertEqual(spool_meta['gid'], int(group.stdout.split(':')[2]))
        else:
            self.assertEqual(spool_meta['uid'], 0)
            self.assertEqual(spool_meta['gid'], 0)
        config_path = os.path.join(
            self.root, 'opt', 'inspect-deploy', 'config', 'deploy-config.json')
        config_proc = self._as_root(['cat', config_path])
        self.assertEqual(config_proc.returncode, 0, config_proc.stderr)
        config = json.loads(config_proc.stdout)
        self.assertEqual(config['spool_dir'], '/var/lib/inspect-deploy/spool')
        self.assertNotIn('/opt/inspect-platform/data', config['spool_dir'])
        self.assertEqual(config['log_file'], '/var/lib/inspect-deploy/log/dispatcher.log')
        log_dir = os.path.join(self.root, 'var', 'lib', 'inspect-deploy', 'log')
        self.assertEqual(self._as_root(['test', '-d', log_dir]).returncode, 0)
        unit_path = os.path.join(
            self.root, 'opt', 'inspect-deploy', 'systemd',
            'inspect-deploy-dispatcher.service')
        with open(unit_path) as handle:
            unit = handle.read()
        self.assertIn('ReadWritePaths=/var/lib/inspect-deploy', unit)
        self.assertNotIn('/opt/inspect-platform/data/deploy-spool', unit)
        sudoers = os.path.join(self.root, 'etc', 'sudoers.d', 'inspect-deploy')
        sudoers_meta = self._snap([sudoers])[sudoers]
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
        proc = self._as_root(['bash', '-c', script, 'bash', self.root, str(owner)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ops = os.path.join(self.root, 'opt', 'inspect-ops')
        state = os.path.join(ops, 'state')
        before = self._snap([ops, state])

        result = self._run_install()

        after = self._snap([ops, state])
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
        self._assert_accounts_unchanged()
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
        proc = self._as_root(['bash', '-c', script, 'bash', self.root])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before = self._snap([target, os.path.join(target, 'marker'), link])

        result = self._run_install()

        after = self._snap([target, os.path.join(target, 'marker'), link])
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('Leaving it unchanged.', result.stderr)
        self.assertIn('symlink', result.stderr)
        self.assertEqual(before, after)
        self.assertFalse(os.path.lexists(
            os.path.join(self.root, 'opt', 'inspect-platform', 'data', 'deploy-spool')))
        self._assert_accounts_unchanged()
        self._assert_host_untouched()

    def _assert_prefix_rejected(self, install_root, polluted):
        before = {}
        for path in polluted:
            before[path] = os.path.lexists(path)
        result = self._as_root(['env', 'INSTALL_ROOT=' + install_root, 'bash', INSTALL])
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('Leaving it unchanged.', result.stderr)
        for path in polluted:
            self.assertEqual(os.path.lexists(path), before[path], path)
        self._assert_accounts_unchanged()
        self._assert_host_untouched()
        return result

    def test_parent_prefix_opt_is_rejected(self):
        result = self._assert_prefix_rejected('/opt', (
            '/opt/opt',
            '/opt/var',
            '/opt/etc',
            '/opt/inspect-deploy',
            '/var/lib/inspect-deploy',
            '/etc/sudoers.d/inspect-deploy',
        ))
        self.assertIn('parent of', result.stderr)

    def test_parent_prefix_var_lib_and_etc_are_rejected(self):
        self._assert_prefix_rejected('/var/lib', (
            '/var/lib/opt',
            '/var/lib/var',
            '/var/lib/etc',
            '/var/lib/inspect-deploy',
        ))
        self._assert_prefix_rejected('/etc', (
            '/etc/opt',
            '/etc/var',
            '/etc/etc',
            '/etc/sudoers.d/inspect-deploy',
        ))

    def test_symlink_to_opt_prefix_is_rejected(self):
        link = os.path.join(self.root, 'link-to-opt')
        os.symlink('/opt', link)
        self._assert_prefix_rejected(link, (
            '/opt/opt',
            '/opt/var',
            '/opt/etc',
            '/opt/inspect-deploy',
        ))

    def test_service_user_appends_log_and_parent_stays_unwritable(self):
        uid = os.getuid()
        gid = os.getgid()
        base = os.path.join(self.root, 'perm-layout')
        script = """
import os
import sys
base = sys.argv[1]
uid = int(sys.argv[2])
gid = int(sys.argv[3])
dispatcher_dir = sys.argv[4]
parent = os.path.join(base, 'inspect-deploy')
logdir = os.path.join(parent, 'log')
os.makedirs(logdir)
os.chown(parent, 0, gid)
os.chmod(parent, 0o750)
os.chown(logdir, uid, gid)
os.chmod(logdir, 0o750)
if os.stat(parent).st_mode & 0o020:
    raise SystemExit('parent became group-writable')
os.setgid(gid)
os.setuid(uid)
sys.path.insert(0, dispatcher_dir)
import dispatcher
good = os.path.join(logdir, 'dispatcher.log')
bad = os.path.join(parent, 'dispatcher.log')
dispatcher.LOG_FILE = dispatcher.Path(good)
dispatcher.log('writable-log')
dispatcher.log('second-line')
handle = open(good)
text = handle.read()
handle.close()
if 'writable-log' not in text or 'second-line' not in text:
    raise SystemExit('append did not land in the service log')
dispatcher.LOG_FILE = dispatcher.Path(bad)
try:
    dispatcher.log('should-fail')
except PermissionError:
    pass
else:
    raise SystemExit('log in the 0750 parent was writable')
"""
        proc = self._as_root([
            sys.executable, '-c', script, base, str(uid), str(gid), DISPATCHER_DIR,
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        parent = os.path.join(base, 'inspect-deploy')
        parent_meta = self._snap([parent])[parent]
        self.assertEqual(parent_meta['uid'], 0)
        self.assertEqual(parent_meta['mode'], 0o750)

    def test_unprefixed_install_creates_accounts_only_inside_namespace(self):
        work = tempfile.mkdtemp(prefix='install-ns-')
        self.addCleanup(lambda: self._as_root(['rm', '-rf', work]))
        script = r"""
set -euo pipefail
work="$1"
install_sh="$2"
mkdir -p "$work/opt" "$work/varlib" "$work/etc/sudoers.d"
cp -a /etc/passwd /etc/group /etc/shadow /etc/gshadow /etc/login.defs /etc/nsswitch.conf "$work/etc/"
cp -a /etc/sudoers "$work/etc/sudoers"
if [ -d /etc/sudoers.d ]; then
    cp -a /etc/sudoers.d/. "$work/etc/sudoers.d/" || true
fi
unshare --mount bash -c "
set -euo pipefail
mount --bind '$work/etc' /etc
mount --bind '$work/opt' /opt
mount --bind '$work/varlib' /var/lib
env -u INSTALL_ROOT bash '$install_sh'
getent passwd inspect-dispatcher
getent group inspect-deploy
test -d /opt/inspect-deploy/bin
test -d /var/lib/inspect-deploy/spool
test ! -e /opt/inspect-platform/data/deploy-spool
"
"""
        result = self._as_root(['bash', '-c', script, 'bash', work, INSTALL])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('inspect-dispatcher', result.stdout)
        self.assertIn('inspect-deploy', result.stdout)
        opt_bin = os.path.join(work, 'opt', 'inspect-deploy', 'bin')
        spool = os.path.join(work, 'varlib', 'inspect-deploy', 'spool')
        self.assertEqual(self._as_root(['test', '-d', opt_bin]).returncode, 0)
        self.assertEqual(self._as_root(['test', '-d', spool]).returncode, 0)
        spool_meta = self._snap([spool])[spool]
        ns_uid = None
        for line in result.stdout.splitlines():
            if line.startswith('inspect-dispatcher:'):
                ns_uid = int(line.split(':')[2])
        self.assertIsNotNone(ns_uid)
        self.assertEqual(spool_meta['uid'], ns_uid)
        self.assertNotEqual(ns_uid, 0)
        parent = os.path.join(work, 'varlib', 'inspect-deploy')
        log_dir = os.path.join(parent, 'log')
        parent_meta = self._snap([parent])[parent]
        log_meta = self._snap([log_dir])[log_dir]
        self.assertEqual(parent_meta['uid'], 0)
        self.assertEqual(parent_meta['mode'], 0o750)
        self.assertEqual(parent_meta['mode'] & 0o020, 0)
        self.assertEqual(log_meta['uid'], ns_uid)
        self.assertEqual(log_meta['mode'], 0o750)
        cfg = self._as_root(['cat', os.path.join(
            work, 'opt', 'inspect-deploy', 'config', 'deploy-config.json')])
        self.assertEqual(cfg.returncode, 0, cfg.stderr)
        self.assertEqual(
            json.loads(cfg.stdout)['log_file'],
            '/var/lib/inspect-deploy/log/dispatcher.log')
        self._assert_accounts_unchanged()
        self.assertFalse(os.path.lexists('/opt/inspect-deploy'))
        self.assertFalse(os.path.lexists('/var/lib/inspect-deploy'))
        self.assertFalse(os.path.lexists('/etc/sudoers.d/inspect-deploy'))
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
        self.assertEqual(
            dispatcher.DEFAULT_CONFIG['log_file'],
            '/var/lib/inspect-deploy/log/dispatcher.log',
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
