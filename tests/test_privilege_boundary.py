#!/usr/bin/env python3
"""在有免密 sudo 时，用真实 root 进程跑特权边界证明。"""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

PROOF = Path(__file__).parent / 'proof_privilege_boundary.py'


def sudo_escalation():
    """返回 (可以提权, 原因, sudo 绝对路径或 None)。

    没有 sudo，或 sudo 存在但不能无密码变成 root 时，返回 False。
    不把「找不到可执行文件」抛成测试 ERROR。
    """
    if os.geteuid() == 0:
        return True, 'already root', None
    sudo = shutil.which('sudo')
    if not sudo:
        return False, 'sudo executable not found', None
    try:
        proc = subprocess.run(
            [sudo, '-n', sys.executable, '-c', 'import os; raise SystemExit(0 if os.geteuid()==0 else 1)'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError as exc:
        return False, 'sudo not executable: {}'.format(exc), sudo
    if proc.returncode != 0:
        return False, 'passwordless sudo to root is not available', sudo
    return True, 'passwordless sudo', sudo


class TestPrivilegeBoundaryProof(unittest.TestCase):
    def test_real_root_process_locks_path_stage_and_sudo(self):
        can_escalate, reason, sudo = sudo_escalation()
        if not can_escalate:
            self.skipTest(reason)
        if os.geteuid() == 0:
            cmd = [sys.executable, str(PROOF)]
        else:
            cmd = [sudo, '-n', sys.executable, str(PROOF)]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=60,
            )
        except OSError as exc:
            self.skipTest('sudo disappeared before the proof: {}'.format(exc))
        output = proc.stdout + '\n' + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        for token in (
            'ROOT_PATH_FIXED',
            'NONROOT_EXPLICIT_CLI',
            'NONROOT_SUDO_FIXED',
            'CHMOD_755_DATA_NOT_WRITABLE',
            'CANDIDATE_TREE_MODE_UNCHANGED',
            'NOBODY_CANDIDATE_READ_OK',
            'NOBODY_DATA_WRITE_OK',
            'NOBODY_SECRET_DENIED',
            'SUDO_HELPER_EUID_0',
            'HELPER_REJECTS_UNKNOWN',
            'SUDO_EVIL_DENIED',
            'SUDO_PYTHON_DENIED',
            'NNP_SUDO_BLOCKED',
            'EMPTY_BOUND_SUDO_BLOCKED',
            'PROOF_OK',
        ):
            self.assertIn(token, proc.stdout, output)


if __name__ == '__main__':
    unittest.main()
