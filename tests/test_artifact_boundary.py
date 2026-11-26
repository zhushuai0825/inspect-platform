#!/usr/bin/env python3
"""钉住源码软链和路径逃逸：调用真实 create_artifact。"""
import os
import sys
import tempfile
import shutil
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'deploy-scripts' / 'host-dispatcher'))
import dispatcher

SENTINEL = b'SENTINEL_OUTSIDE_PRIVATE_NOT_A_REAL_SECRET'


class TestArtifactBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='artifact-boundary-'))
        self.addCleanup(shutil.rmtree, str(self.tmp))
        self.old_work = dispatcher.WORK_DIR
        dispatcher.WORK_DIR = self.tmp / 'work'
        dispatcher.WORK_DIR.mkdir()
        self.source = self.tmp / 'source'
        self.source.mkdir()
        outside = self.tmp / 'outside'
        outside.mkdir()
        self.secret = outside / 'private.txt'
        self.secret.write_bytes(SENTINEL)
        os.chmod(str(self.secret), 0o644)

    def tearDown(self):
        dispatcher.WORK_DIR = self.old_work

    def test_dangling_symlink_rejected(self):
        link = self.source / 'app.py'
        link.symlink_to(self.tmp / 'does-not-exist')
        with self.assertRaises(Exception) as caught:
            dispatcher.create_artifact(self.source, 'd' * 40)
        self.assertIn('软链', str(caught.exception))

    def test_symlink_file_rejected_and_not_packed(self):
        link = self.source / 'app.py'
        link.symlink_to(self.secret)
        with self.assertRaises(Exception) as caught:
            dispatcher.create_artifact(self.source, 'a' * 40)
        self.assertIn('软链', str(caught.exception))
        self.assert_sentinel_absent()

    def test_symlink_parent_escape_rejected(self):
        outside_dir = self.tmp / 'outside-dir'
        outside_dir.mkdir()
        (outside_dir / 'app.js').write_bytes(SENTINEL)
        (self.source / 'admin-static').symlink_to(outside_dir)
        with self.assertRaises(Exception) as caught:
            dispatcher.create_artifact(self.source, 'b' * 40)
        message = str(caught.exception)
        self.assertTrue('软链' in message or '逃逸' in message, message)
        self.assert_sentinel_absent()

    def test_regular_file_does_not_include_outside_bytes(self):
        (self.source / 'app.py').write_text('# app only\n')
        artifact, _hash_file = dispatcher.create_artifact(self.source, 'c' * 40)
        with zipfile.ZipFile(str(artifact)) as zipped:
            names = zipped.namelist()
            self.assertIn('inspect-platform/app.py', names)
            self.assertNotIn(SENTINEL, zipped.read('inspect-platform/app.py'))
        self.assertNotIn(SENTINEL, artifact.read_bytes())

    def assert_sentinel_absent(self):
        for path in self.tmp.rglob('*.zip'):
            self.assertNotIn(SENTINEL, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
