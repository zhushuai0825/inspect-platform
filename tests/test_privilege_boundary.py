#!/usr/bin/env python3
"""在有免密 sudo 时，用真实 root 进程跑特权边界证明。"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

PROOF = Path(__file__).parent / 'proof_privilege_boundary.py'


def sudo_python_is_root():
    if os.geteuid() == 0:
        return True
    proc = subprocess.run(
        ['sudo', '-n', sys.executable, '-c', 'import os; raise SystemExit(0 if os.geteuid()==0 else 1)'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode == 0


class TestPrivilegeBoundaryProof(unittest.TestCase):
    def test_real_root_process_locks_path_stage_and_sudo(self):
        if not sudo_python_is_root():
            self.skipTest('passwordless sudo to root is not available')
        cmd = [sys.executable, str(PROOF)] if os.geteuid() == 0 else ['sudo', '-n', sys.executable, str(PROOF)]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
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
