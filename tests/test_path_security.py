#!/usr/bin/env python3
"""
路径安全黑盒测试
验证生产路径不能被测试路径绕过
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
RELEASE_PY = REPO_ROOT / 'ops' / 'release.py'


class TestPathSecurity(unittest.TestCase):
    """路径安全黑盒测试"""
    
    def test_reject_production_path_direct(self):
        """拒绝直接使用生产路径"""
        env = os.environ.copy()
        env['RELEASE_TEST_SANDBOX'] = '/opt/inspect-platform:/opt/inspect-ops/state'
        
        cmd = [sys.executable, str(RELEASE_PY), 'status']
        
        result = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        self.assertNotEqual(result.returncode, 0, 'Should reject production path')
        self.assertIn('PRODUCTION_PATH_REJECTED', result.stdout + result.stderr)
    
    def test_reject_production_subdir(self):
        """拒绝生产路径的子目录"""
        env = os.environ.copy()
        env['RELEASE_TEST_SANDBOX'] = '/opt/inspect-platform/data:/tmp/state'
        
        cmd = [sys.executable, str(RELEASE_PY), 'status']
        
        result = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        self.assertNotEqual(result.returncode, 0, 'Should reject production subdir')
        self.assertIn('PRODUCTION_PATH_REJECTED', result.stdout + result.stderr)
    
    def test_reject_production_path_with_parent(self):
        """拒绝通过 .. 访问生产路径"""
        env = os.environ.copy()
        # 尝试 /tmp/../opt/inspect-platform
        env['RELEASE_TEST_SANDBOX'] = '/tmp/../opt/inspect-platform:/tmp/state'
        
        cmd = [sys.executable, str(RELEASE_PY), 'status']
        
        result = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        self.assertNotEqual(result.returncode, 0, 'Should reject .. to production')
        self.assertIn('PRODUCTION_PATH_REJECTED', result.stdout + result.stderr)
    
    def test_reject_symlink_to_production(self):
        """拒绝符号链接到生产路径"""
        tmpdir = tempfile.mkdtemp(prefix='path-security-')
        try:
            # 创建符号链接到生产路径
            link_path = Path(tmpdir) / 'link-to-prod'
            try:
                link_path.symlink_to('/opt/inspect-platform')
            except OSError:
                # 如果创建符号链接失败（权限问题），跳过测试
                self.skipTest('Cannot create symlink')
            
            env = os.environ.copy()
            env['RELEASE_TEST_SANDBOX'] = '{}:/tmp/state'.format(link_path)
            
            cmd = [sys.executable, str(RELEASE_PY), 'status']
            
            result = subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            
            # 应该被拒绝（symlink 或 production path）
            self.assertNotEqual(result.returncode, 0, 'Should reject symlink to production')
            
        finally:
            import shutil
            shutil.rmtree(tmpdir)
    
    def test_accept_safe_temp_path(self):
        """接受安全的临时路径"""
        tmpdir = tempfile.mkdtemp(prefix='safe-path-')
        try:
            root = Path(tmpdir) / 'root'
            state = Path(tmpdir) / 'state'
            root.mkdir()
            state.mkdir()
            
            # 创建必要的数据
            (root / 'data').mkdir()
            (root / 'data' / 'secret.env').write_text('SECRET=test\n')
            (root / 'data' / 'mail.env').write_text('SMTP_PASSWORD=test\n')
            
            env = os.environ.copy()
            env['RELEASE_TEST_SANDBOX'] = '{}:{}'.format(root, state)
            
            cmd = [sys.executable, str(RELEASE_PY), 'status']
            
            result = subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            
            # 应该成功（或者返回 BASELINE_REQUIRED 因为没有 baseline）
            output = result.stdout + result.stderr
            # 不应该包含 PRODUCTION_PATH_REJECTED
            self.assertNotIn('PRODUCTION_PATH_REJECTED', output)
            
        finally:
            import shutil
            shutil.rmtree(tmpdir)
    
    def test_root_rejects_test_overrides(self):
        """Root 模式拒绝所有测试 overrides"""
        # 这个测试需要 root 权限，如果不是 root 就跳过
        if os.geteuid() != 0:
            self.skipTest('Not running as root')
        
        env = os.environ.copy()
        env['RELEASE_TEST_SANDBOX'] = '/tmp/root:/tmp/state'
        
        cmd = [sys.executable, str(RELEASE_PY), 'status']
        
        result = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        self.assertNotEqual(result.returncode, 0, 'Root should reject test overrides')
        self.assertIn('ROOT_PRODUCTION_REJECT_TEST_OVERRIDES', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
