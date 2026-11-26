#!/usr/bin/env python3
"""受控暂存：候选可被 uid 65534 读取，临时 data 只归 nobody 写。

生产调用方是 root 的 run-candidate-tests。暂存目录不在控制面
/opt/inspect-deploy，也不在应用 data。不挂 Docker socket，不把
生产树改成世界可写。Python 3.6 兼容。
"""
from __future__ import print_function

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NOBODY_UID = 65534
NOBODY_GID = 65534
PRODUCTION_STAGE_ROOT = '/var/lib/inspect-deploy/runner-stage'
RUNNER_IMAGE = 'inspect-platform-runner:v1'
FORBIDDEN_STAGE_PREFIXES = (
    '/opt/inspect-deploy',
    '/opt/inspect-platform',
    '/opt/inspect-ops',
)
FORBIDDEN_CANDIDATE_PREFIXES = (
    '/opt/inspect-platform/data',
)
FORBIDDEN_BASENAMES = ('secret.env', 'mail.env', 'platform.db')


class StageError(Exception):
    pass


def _real_path(path):
    """解析已存在的祖先，避免暂存路径经软链进入生产树。"""
    path = os.path.abspath(str(path))
    missing = []
    existing = path
    while not os.path.exists(existing):
        parent = os.path.dirname(existing)
        if parent == existing:
            break
        missing.append(os.path.basename(existing))
        existing = parent
    real_existing = os.path.realpath(existing)
    for part in reversed(missing):
        real_existing = os.path.join(real_existing, part)
    return os.path.normpath(real_existing)


def _is_under(path, prefix):
    path_s = str(path)
    return path_s == prefix or path_s.startswith(prefix + os.sep)


def assert_stage_inputs(candidate_src, staging_root):
    """拒绝会把生产秘密或控制面拿去挂载的输入。不需要 root。"""
    candidate = Path(candidate_src)
    staging = Path(staging_root)
    resolved_candidate = Path(_real_path(candidate))
    staging_abs = Path(_real_path(staging))
    for prefix in FORBIDDEN_CANDIDATE_PREFIXES:
        if _is_under(resolved_candidate, prefix):
            raise StageError('refusing to stage production data')
    for prefix in FORBIDDEN_STAGE_PREFIXES:
        if _is_under(staging_abs, prefix):
            raise StageError('staging root is inside a production or control-plane tree')
    if candidate.is_symlink() or not candidate.is_dir():
        raise StageError('candidate must be a real directory')
    if staging.is_symlink():
        raise StageError('staging root is a symlink')
    if _is_under(staging_abs, str(resolved_candidate)):
        raise StageError('staging root is inside the candidate')
    return resolved_candidate, staging_abs


def assert_tree_copyable(candidate_src):
    """遍历候选树并拒绝软链、逃逸和秘密文件名。不复制。"""
    candidate_src = Path(candidate_src)
    source_resolved = Path(_real_path(candidate_src))
    found = []
    for dirpath, dirnames, filenames in os.walk(str(candidate_src), followlinks=False):
        kept = []
        for name in dirnames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise StageError('symlink directory rejected: {}'.format(path))
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise StageError('symlink rejected: {}'.format(path))
            if not path.is_file():
                raise StageError('non-regular file rejected: {}'.format(path))
            if name in FORBIDDEN_BASENAMES:
                raise StageError('forbidden filename in candidate: {}'.format(name))
            resolved = Path(_real_path(path))
            try:
                resolved.relative_to(source_resolved)
            except ValueError:
                raise StageError('path escape: {}'.format(path))
            found.append(path)
    return found


def _copy_tree(candidate_src, destination):
    candidate_src = Path(candidate_src)
    destination = Path(destination)
    os.mkdir(str(destination), 0o755)
    os.chmod(str(destination), 0o755)
    files = assert_tree_copyable(candidate_src)
    for path in files:
        rel = Path(_real_path(path)).relative_to(Path(_real_path(candidate_src)))
        target = destination / rel
        if not target.parent.exists():
            os.makedirs(str(target.parent), 0o755)
        # makedirs 受 umask 影响，父目录统一收成 0755（可遍历、不可写）。
        parent = target.parent
        while parent != destination:
            os.chmod(str(parent), 0o755)
            parent = parent.parent
        shutil.copyfile(str(path), str(target))
        os.chmod(str(target), 0o644)


def stage_for_runner(candidate_src, staging_root, nobody_uid=NOBODY_UID, nobody_gid=NOBODY_GID):
    """
    复制候选到 staging_root，并创建只属于 nobody 的 data 目录。

    返回的 mounts 只有这两个目录。调用方必须是 root，否则无法 chown 给 65534。
    chmod 755 不能让 nobody 写入 data，所以 data 是 0700 且属主为 nobody。
    """
    resolved_candidate, _staging_abs = assert_stage_inputs(candidate_src, staging_root)
    if os.geteuid() != 0:
        raise StageError('stage_for_runner requires root to chown the data directory')
    staging_root = Path(staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)
    os.chmod(str(staging_root), 0o755)
    # 再次确认 chmod 没有把禁止树改成可写。assert 已拒绝那些前缀。
    mode = os.stat(str(staging_root)).st_mode & 0o022
    if mode:
        raise StageError('staging root became group or world writable')
    run_dir = Path(tempfile.mkdtemp(prefix='run-', dir=str(staging_root)))
    os.chmod(str(run_dir), 0o755)
    candidate = run_dir / 'candidate'
    data = run_dir / 'data'
    try:
        _copy_tree(resolved_candidate, candidate)
        os.mkdir(str(data), 0o755)
        os.chown(str(data), nobody_uid, nobody_gid)
        os.chmod(str(data), 0o700)
    except Exception:
        shutil.rmtree(str(run_dir), ignore_errors=True)
        raise
    staged = {
        'run_dir': str(run_dir),
        'candidate': str(candidate),
        'data': str(data),
        'mounts': [
            {'host': str(candidate), 'container': '/candidate', 'mode': 'ro'},
            {'host': str(data), 'container': '/data', 'mode': 'rw'},
        ],
    }
    validate_mounts(staged['mounts'])
    return staged


def validate_mounts(mounts):
    """挂载列表只能是暂存候选（只读）和暂存 data（读写）。"""
    if len(mounts) != 2:
        raise StageError('runner mount list must be exactly candidate and data')
    seen = set()
    for mount in mounts:
        host = os.path.abspath(mount['host'])
        container = mount['container']
        mode = mount['mode']
        if 'docker.sock' in host or host.endswith('docker.sock'):
            raise StageError('refusing to mount the docker socket')
        for prefix in FORBIDDEN_CANDIDATE_PREFIXES + FORBIDDEN_STAGE_PREFIXES:
            if _is_under(host, prefix):
                raise StageError('forbidden mount {}'.format(host))
        if container in seen:
            raise StageError('duplicate mount {}'.format(container))
        seen.add(container)
        if container == '/candidate' and mode != 'ro':
            raise StageError('candidate mount must be read-only')
        elif container == '/data' and mode != 'rw':
            raise StageError('data mount must be writable')
        elif container not in ('/candidate', '/data'):
            raise StageError('unexpected mount point {}'.format(container))
    if seen != set(['/candidate', '/data']):
        raise StageError('mount list missing candidate or data')
    return True


def docker_command(staged, image=RUNNER_IMAGE):
    """构造隔离 docker 参数。不传递宿主环境，不挂 socket。"""
    validate_mounts(staged['mounts'])
    command = [
        'docker', 'run', '--rm',
        '--read-only',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
        '--network', 'none',
        '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges',
        '--pids-limit', '100',
        '--memory', '384m',
        '--cpus', '0.5',
        '--user', '{}:{}'.format(NOBODY_UID, NOBODY_GID),
    ]
    for mount in staged['mounts']:
        command.extend([
            '-v', '{}:{}:{}'.format(mount['host'], mount['container'], mount['mode']),
        ])
    command.extend(['-e', 'QA_DATA_DIR=/data', image, '/candidate'])
    joined = ' '.join(command)
    if 'docker.sock' in joined or 'secret.env' in joined or 'mail.env' in joined:
        raise StageError('docker command includes a forbidden path')
    return command


def run_candidate(candidate_src, staging_root=None):
    """root 入口。忽略测试路径覆盖。镜像不存在则失败，不在宿主上执行候选代码。"""
    if os.geteuid() != 0:
        sys.stderr.write('ERROR: refusing to execute candidate tests as a non-root host user\n')
        return 1
    # root 不接受暂存路径注入。生产路径固定。
    if staging_root is not None:
        sys.stderr.write('ERROR: root runner does not accept a staging-root override\n')
        return 1
    try:
        inspect = subprocess.call(
            ['docker', 'image', 'inspect', RUNNER_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        sys.stderr.write('ERROR: docker is not available\n')
        return 2
    if inspect != 0:
        sys.stderr.write('ERROR: Runner image not found: {}\n'.format(RUNNER_IMAGE))
        return 2
    staged = None
    try:
        staged = stage_for_runner(candidate_src, PRODUCTION_STAGE_ROOT)
        return subprocess.call(docker_command(staged))
    finally:
        if staged is not None:
            shutil.rmtree(staged['run_dir'], ignore_errors=True)


def main(argv):
    if len(argv) < 2:
        sys.stderr.write('usage: stage_runner.py run CANDIDATE\n')
        return 2
    action = argv[1]
    if action == 'run':
        if len(argv) != 3:
            sys.stderr.write('usage: stage_runner.py run CANDIDATE\n')
            return 2
        return run_candidate(argv[2])
    sys.stderr.write('unknown action\n')
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
