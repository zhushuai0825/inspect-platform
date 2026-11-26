#!/usr/bin/env python3
"""真实 euid 证明。必须由 root 执行：sudo python3 tests/proof_privilege_boundary.py

不启动 systemd（本机 PID 1 不是 systemd），不构建 Docker 镜像，不读取生产秘密。
"""
from __future__ import print_function

import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'deploy-scripts' / 'host-dispatcher'))
sys.path.insert(0, str(REPO / 'control-plane' / 'bin'))

PROOF_USER = 'inspect-proof-40fc'
SUDOERS_PATH = '/etc/sudoers.d/inspect-proof-40fc'


def say(token):
    print(token)
    sys.stdout.flush()


def run(cmd, check=False):
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError('{} failed: {}'.format(cmd, proc.stderr))
    return proc


def as_nobody(args):
    return run([
        'setpriv',
        '--reuid=65534',
        '--regid=65534',
        '--init-groups',
        '--inh-caps=-all',
    ] + args)


def prove_root_path():
    os.environ['RELEASE_CLI'] = '/tmp/evil-release.py'
    os.environ['RELEASE_ROOT'] = '/tmp/evil-root'
    os.environ['RELEASE_STATE'] = '/tmp/evil-state'
    os.environ['RELEASE_TEST_SANDBOX'] = '/tmp/a:/tmp/b'
    os.environ['RELEASE_TEST_RUNNER_PATH'] = '/tmp/evil-runner'
    import dispatcher
    dispatcher.CONFIG['RELEASE_ROOT'] = '/tmp/from-config'
    dispatcher.CONFIG['release_root'] = '/tmp/from-config-root'
    dispatcher.CONFIG['release_cli'] = '/tmp/from-config.py'
    dispatcher.CONFIG['release_state'] = '/tmp/from-config-state'
    if os.geteuid() != 0:
        raise SystemExit('proof must run as root')
    decision = dispatcher.resolve_release_invocation()
    expected = ['/usr/bin/python3', '/opt/inspect-deploy/bin/release.py']
    if decision['command_prefix'] != expected or decision['is_test_sandbox'] or decision['use_sudo']:
        raise SystemExit('root invocation drifted: {}'.format(decision))
    child = dispatcher.release_child_env(False)
    for key in ('RELEASE_CLI', 'RELEASE_ROOT', 'RELEASE_STATE', 'RELEASE_TEST_SANDBOX', 'RELEASE_TEST_RUNNER_PATH'):
        if key in child:
            raise SystemExit('root child kept {}'.format(key))
    say('ROOT_PATH_FIXED')

    # 同一个函数在真实非 root 进程里再调用一次。
    snippet = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
os.environ["RELEASE_CLI"] = "/tmp/explicit-only.py"
os.environ["RELEASE_ROOT"] = "/tmp/should-not-select"
import dispatcher
dispatcher.CONFIG["RELEASE_ROOT"] = "/tmp/from-config"
dispatcher.CONFIG["release_cli"] = "/tmp/from-config.py"
decision = dispatcher.resolve_release_invocation()
assert os.geteuid() != 0
assert decision["is_test_sandbox"] is True
assert decision["use_sudo"] is False
assert decision["release_py"] == "/tmp/explicit-only.py", decision
os.environ.pop("RELEASE_CLI", None)
decision = dispatcher.resolve_release_invocation()
assert decision["is_test_sandbox"] is False, decision
assert decision["use_sudo"] is True, decision
assert decision["command_prefix"] == ["sudo", "-n", "--", "/opt/inspect-deploy/bin/inspect-release"]
print("NONROOT_EXPLICIT_CLI")
print("NONROOT_SUDO_FIXED")
'''
    proc = run([
        'setpriv', '--reuid=1000', '--regid=1000', '--init-groups', '--inh-caps=-all',
        sys.executable, '-c', snippet, str(REPO / 'deploy-scripts' / 'host-dispatcher'),
    ])
    if proc.returncode != 0 or 'NONROOT_SUDO_FIXED' not in proc.stdout:
        raise SystemExit('nonroot function call failed\n{}\n{}'.format(proc.stdout, proc.stderr))
    say('NONROOT_EXPLICIT_CLI')
    say('NONROOT_SUDO_FIXED')


def prove_stage():
    import stage_runner
    base = Path(tempfile.mkdtemp(prefix='stage-proof-'))
    try:
        candidate = base / 'candidate'
        nested = candidate / 'pkg'
        nested.mkdir(parents=True)
        (nested / 'app.py').write_text('CANDIDATE_OK\n')
        os.chmod(str(candidate), 0o700)
        os.chmod(str(nested), 0o700)
        os.chmod(str(nested / 'app.py'), 0o600)
        before = oct(candidate.stat().st_mode & 0o777)
        secret_dir = base / 'prod-data'
        secret_dir.mkdir()
        secret = secret_dir / 'secret.env'
        secret.write_text('SENTINEL_SECRET_NOT_FOR_CONTAINER\n')
        os.chmod(str(secret), 0o600)
        os.chown(str(secret), 0, 0)

        plain = base / 'data755'
        plain.mkdir()
        os.chmod(str(plain), 0o755)
        denied = as_nobody([
            sys.executable, '-c',
            'open(%r, "w").write("no")' % str(plain / 'x'),
        ])
        if denied.returncode == 0:
            raise SystemExit('chmod 755 data was writable by nobody:\n{}'.format(denied.stderr))
        say('CHMOD_755_DATA_NOT_WRITABLE')

        staged = stage_runner.stage_for_runner(str(candidate), str(base / 'stage'))
        if oct(candidate.stat().st_mode & 0o777) != before:
            raise SystemExit('staging chmodded the original candidate tree')
        mounts = ' '.join(item['host'] for item in staged['mounts'])
        if 'secret.env' in mounts or str(secret_dir) in mounts or 'docker.sock' in mounts:
            raise SystemExit('secret or socket entered mounts: {}'.format(staged['mounts']))
        data_stat = os.stat(staged['data'])
        if data_stat.st_uid != 65534 or (data_stat.st_mode & 0o777) != 0o700:
            raise SystemExit('data dir is not nobody:700 ({}, {})'.format(
                data_stat.st_uid, oct(data_stat.st_mode & 0o777)))
        if os.stat(staged['candidate']).st_mode & 0o022:
            raise SystemExit('staged candidate is group or world writable')
        say('CANDIDATE_TREE_MODE_UNCHANGED')

        probe = base / 'probe.py'
        probe.write_text(
            'import os, sys\n'
            'cand, data, secret = sys.argv[1:4]\n'
            'print(open(os.path.join(cand, "pkg", "app.py")).read().strip())\n'
            'open(os.path.join(data, "probe.txt"), "w").write("wrote")\n'
            'try:\n'
            '    open(secret).read()\n'
            '    print("SECRET_READ")\n'
            'except Exception:\n'
            '    print("SECRET_DENIED")\n'
        )
        os.chmod(str(probe), 0o644)
        # 祖先需要可遍历。base 在 /tmp 下，由 root 创建，umask 可能是 077。
        os.chmod(str(base), 0o755)
        os.chmod(str(base / 'stage'), 0o755)
        proc = as_nobody([
            sys.executable, str(probe), staged['candidate'], staged['data'], str(secret),
        ])
        text = proc.stdout + proc.stderr
        if proc.returncode != 0 or 'CANDIDATE_OK' not in proc.stdout or 'SECRET_DENIED' not in proc.stdout:
            raise SystemExit('nobody probe failed\n{}'.format(text))
        written = Path(staged['data']) / 'probe.txt'
        if written.read_text() != 'wrote':
            raise SystemExit('nobody did not write temp data')
        say('NOBODY_CANDIDATE_READ_OK')
        say('NOBODY_DATA_WRITE_OK')
        say('NOBODY_SECRET_DENIED')
    finally:
        shutil.rmtree(str(base), ignore_errors=True)


def _cap_bounding_empty_then_sudo(helper):
    """模拟 systemd CapabilityBoundingSet= 为空后再 sudo。"""
    code = r'''
import ctypes, os, pwd, sys
libc = ctypes.CDLL("libc.so.6", use_errno=True)
PR_SET_KEEPCAPS = 8
PR_CAPBSET_DROP = 24
CAP_SETUID = 7
CAP_SETPCAP = 8
def die(msg):
    sys.stderr.write(msg + "\n")
    raise SystemExit(1)
if libc.prctl(PR_SET_KEEPCAPS, 1, 0, 0, 0) != 0:
    die("keepcaps")
for cap in range(40):
    if cap in (CAP_SETUID, CAP_SETPCAP):
        continue
    if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0:
        die("drop")
pw = pwd.getpwnam(sys.argv[1])
os.setgroups([])
os.setgid(pw.pw_gid)
os.setuid(pw.pw_uid)
class H(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]
class D(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]
header = H(0x20080522, 0)
data = (D * 2)()
data[0].permitted = (1 << CAP_SETUID) | (1 << CAP_SETPCAP)
data[0].effective = (1 << CAP_SETPCAP)
if libc.capset(ctypes.byref(header), data) != 0:
    die("capset")
for cap in (CAP_SETUID, CAP_SETPCAP):
    if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0:
        die("final")
blank = (D * 2)()
if libc.capset(ctypes.byref(header), blank) != 0:
    die("clear")
os.execv("/usr/bin/sudo", ["sudo", "-n", sys.argv[2], "status"])
'''
    return run([sys.executable, '-c', code, PROOF_USER, helper])


def prove_sudo():
    created_opt = False
    created_user = False
    opt = Path('/opt/inspect-deploy')
    if opt.exists():
        raise SystemExit('refusing to replace existing /opt/inspect-deploy')
    try:
        if run(['id', PROOF_USER]).returncode != 0:
            run(['useradd', '--system', '--shell', '/usr/sbin/nologin', PROOF_USER], check=True)
            created_user = True
        bin_dir = opt / 'bin'
        bin_dir.mkdir(parents=True)
        created_opt = True
        os.chmod(str(opt), 0o755)
        os.chmod(str(bin_dir), 0o755)
        helper_src = REPO / 'control-plane' / 'bin' / 'inspect-release'
        helper = bin_dir / 'inspect-release'
        shutil.copy(str(helper_src), str(helper))
        os.chown(str(helper), 0, 0)
        os.chmod(str(helper), 0o755)
        # 真实 helper 会 exec 固定 release.py。这里放一个只打印 euid 并 chown 的桩，
        # 避免真实 release.py 进入生产路径。
        stub = bin_dir / 'release.py'
        stub.write_text(
            'import os, sys\n'
            'print("euid=%s" % os.geteuid())\n'
            'print("argv=%s" % sys.argv[1:])\n'
            'target = os.environ.get("PROOF_CHOWN_TARGET")\n'
            'if target:\n'
            '    os.chown(target, 65534, 65534)\n'
            '    print("chown=ok")\n'
        )
        os.chown(str(stub), 0, 0)
        os.chmod(str(stub), 0o755)
        data = opt / 'data-probe'
        data.mkdir()
        os.chmod(str(data), 0o755)
        evil = Path('/tmp/inspect-proof-evil.sh')
        evil.write_text('#!/bin/bash\necho EVIL $(id -u)\n')
        os.chmod(str(evil), 0o777)
        sudoers = (
            'Defaults:{user} !requiretty\n'
            'Defaults:{user} env_reset\n'
            'Defaults:{user} env_keep += "PROOF_CHOWN_TARGET"\n'
            '{user} ALL=(root) NOPASSWD: {helper}\n'
        ).format(user=PROOF_USER, helper=str(helper))
        with open(SUDOERS_PATH, 'w') as handle:
            handle.write(sudoers)
        os.chmod(SUDOERS_PATH, 0o440)
        checked = run(['visudo', '-cf', '/etc/sudoers'])
        if checked.returncode != 0:
            raise SystemExit('visudo failed\n{}'.format(checked.stderr))
        env = os.environ.copy()
        env['PROOF_CHOWN_TARGET'] = str(data)
        allowed = subprocess.run(
            ['runuser', '-u', PROOF_USER, '--', 'sudo', '-n', str(helper), 'status'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=env,
        )
        # env_reset 会丢掉 PROOF_CHOWN_TARGET，除非 sudoers env_keep。我们写了 env_keep。
        if allowed.returncode != 0 or 'euid=0' not in allowed.stdout:
            raise SystemExit('helper sudo failed\n{}\n{}'.format(allowed.stdout, allowed.stderr))
        if os.stat(str(data)).st_uid != 65534:
            raise SystemExit('root helper did not chown: {}'.format(allowed.stdout))
        say('SUDO_HELPER_EUID_0')
        say('RELEASE_PY_STUB_FOR_EUID_ONLY')

        rejected = run(['runuser', '-u', PROOF_USER, '--', 'sudo', '-n', str(helper), 'rm', '-rf', '/'])
        if rejected.returncode == 0 or 'RELEASE_HELPER_REJECTED' not in rejected.stdout:
            raise SystemExit('helper accepted a bad command\n{}\n{}'.format(rejected.stdout, rejected.stderr))
        say('HELPER_REJECTS_UNKNOWN')

        evil_run = run(['runuser', '-u', PROOF_USER, '--', 'sudo', '-n', str(evil)])
        if evil_run.returncode == 0 or 'EVIL' in evil_run.stdout:
            raise SystemExit('writable script was allowed:\n{}'.format(evil_run.stdout))
        say('SUDO_EVIL_DENIED')

        python_run = run([
            'runuser', '-u', PROOF_USER, '--', 'sudo', '-n',
            '/usr/bin/python3', '-c', 'import os; print(os.geteuid())',
        ])
        if python_run.returncode == 0:
            raise SystemExit('python -c was allowed via sudo')
        say('SUDO_PYTHON_DENIED')

        nnp = run([
            'runuser', '-u', PROOF_USER, '--',
            'setpriv', '--no-new-privs', 'sudo', '-n', str(helper), 'status',
        ])
        if nnp.returncode == 0 or 'euid=0' in nnp.stdout:
            raise SystemExit('NoNewPrivileges did not block sudo\n{}\n{}'.format(nnp.stdout, nnp.stderr))
        if 'no new privileges' not in (nnp.stderr + nnp.stdout).lower() and nnp.returncode == 0:
            raise SystemExit('unexpected nnp result\n{}'.format(nnp.stderr))
        say('NNP_SUDO_BLOCKED')

        bounded = _cap_bounding_empty_then_sudo(str(helper))
        if bounded.returncode == 0 or 'euid=0' in bounded.stdout or 'chown=ok' in bounded.stdout:
            raise SystemExit('empty bounding set still reached root\n{}\n{}'.format(
                bounded.stdout, bounded.stderr))
        say('EMPTY_BOUND_SUDO_BLOCKED')
    finally:
        if os.path.exists(SUDOERS_PATH):
            os.remove(SUDOERS_PATH)
        if created_opt and opt.exists():
            shutil.rmtree(str(opt), ignore_errors=True)
        evil = Path('/tmp/inspect-proof-evil.sh')
        if evil.exists():
            evil.unlink()
        if created_user:
            run(['userdel', PROOF_USER])


def main():
    if os.geteuid() != 0:
        sys.stderr.write('run as root\n')
        return 1
    prove_root_path()
    prove_stage()
    prove_sudo()
    say('PROOF_OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
