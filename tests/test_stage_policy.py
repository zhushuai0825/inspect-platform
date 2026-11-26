#!/usr/bin/env python3
"""不需要 root 的暂存策略测试。真实 nobody/chown 在 proof_privilege_boundary.py。"""
import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'control-plane' / 'bin'))
import stage_runner


class TestStagePolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='stage-policy-'))
        self.addCleanup(shutil.rmtree, str(self.tmp))

    def test_rejects_production_data_candidate(self):
        with self.assertRaises(stage_runner.StageError):
            stage_runner.assert_stage_inputs(
                '/opt/inspect-platform/data',
                str(self.tmp / 'stage'),
            )

    def test_rejects_control_plane_staging_root(self):
        candidate = self.tmp / 'candidate'
        candidate.mkdir()
        with self.assertRaises(stage_runner.StageError):
            stage_runner.assert_stage_inputs(str(candidate), '/opt/inspect-deploy/stage')

    def test_rejects_symlink_and_secret_name(self):
        candidate = self.tmp / 'candidate'
        candidate.mkdir()
        outside = self.tmp / 'outside'
        outside.mkdir()
        secret = outside / 'secret.env'
        secret.write_text('not-a-real-secret\n')
        (candidate / 'secret.env').symlink_to(secret)
        with self.assertRaises(stage_runner.StageError) as caught:
            stage_runner.assert_tree_copyable(str(candidate))
        self.assertTrue('symlink' in str(caught.exception) or 'forbidden' in str(caught.exception))

    def test_docker_command_has_no_socket_or_production_mount(self):
        staged = {
            'mounts': [
                {
                    'host': '/var/lib/inspect-deploy/runner-stage/run/candidate',
                    'container': '/candidate',
                    'mode': 'ro',
                },
                {
                    'host': '/var/lib/inspect-deploy/runner-stage/run/data',
                    'container': '/data',
                    'mode': 'rw',
                },
            ],
        }
        command = stage_runner.docker_command(staged)
        text = ' '.join(command)
        self.assertNotIn('docker.sock', text)
        self.assertNotIn('secret.env', text)
        self.assertNotIn('/opt/inspect-platform/data', text)
        self.assertIn('--user', command)
        self.assertIn('65534:65534', command)
        self.assertIn('--network', command)
        self.assertIn('none', command)
        self.assertIn('--read-only', command)

    def test_non_root_stage_refuses_before_chmod(self):
        if os.geteuid() == 0:
            self.skipTest('this case is the non-root refusal')
        candidate = self.tmp / 'candidate'
        candidate.mkdir()
        (candidate / 'app.py').write_text('print("ok")\n')
        with self.assertRaises(stage_runner.StageError) as caught:
            stage_runner.stage_for_runner(str(candidate), str(self.tmp / 'stage'))
        self.assertIn('requires root', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
