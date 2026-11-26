#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 dispatcher 控制面路径策略

验证：
1. Root模式无条件固定 /opt/inspect-deploy/bin/release.py
2. Root模式拒绝CONFIG/env覆盖
3. 非root测试模式要求显式RELEASE_CLI
4. 验证完整祖先链（owner/mode/symlink）
"""

import os
import sys
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

# 添加dispatcher到路径
sys.path.insert(0, str(Path(__file__).parent.parent / 'deploy-scripts' / 'host-dispatcher'))
import dispatcher

class TestDispatcherControlPlane(unittest.TestCase):
    """测试dispatcher控制面路径策略"""
    
    def setUp(self):
        """测试前准备"""
        self.test_dir = tempfile.mkdtemp(prefix='test_dispatcher_')
        self.addCleanup(shutil.rmtree, self.test_dir)
        
        # 保存原始env
        self.old_release_cli = os.environ.get('RELEASE_CLI')
        self.addCleanup(self._restore_env)
    
    def _restore_env(self):
        """恢复环境变量"""
        if self.old_release_cli:
            os.environ['RELEASE_CLI'] = self.old_release_cli
        elif 'RELEASE_CLI' in os.environ:
            del os.environ['RELEASE_CLI']
    
    def test_verify_trusted_path_success(self):
        """测试：可信路径验证通过"""
        # 创建测试文件（模式0o755，非root环境）
        test_file = Path(self.test_dir) / 'trusted.py'
        test_file.write_text('# test')
        test_file.chmod(0o755)
        
        # 非root环境：owner检查跳过，只验证mode和symlink
        result = dispatcher.verify_trusted_path(str(test_file), 'test file')
        self.assertTrue(result)
    
    def test_verify_trusted_path_reject_symlink(self):
        """测试：拒绝符号链接"""
        # 创建目标文件和符号链接
        target = Path(self.test_dir) / 'target.py'
        target.write_text('# target')
        target.chmod(0o755)
        
        link = Path(self.test_dir) / 'link.py'
        link.symlink_to(target)
        
        # 验证应该失败
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(link), 'symlink')
        
        self.assertIn('symlink', str(cm.exception).lower())
    
    def test_verify_trusted_path_reject_world_writable(self):
        """测试：拒绝组写/他人写文件"""
        # 创建组写文件
        test_file = Path(self.test_dir) / 'writable.py'
        test_file.write_text('# test')
        test_file.chmod(0o777)  # 组写+他人写
        
        # 验证应该失败
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(test_file), 'writable file')
        
        self.assertIn('group/world-writable', str(cm.exception).lower())
    
    def test_verify_trusted_path_reject_ancestor_writable(self):
        """测试：拒绝祖先目录可写"""
        # 创建嵌套目录结构
        sub1 = Path(self.test_dir) / 'sub1'
        sub1.mkdir(mode=0o755)
        
        sub2 = sub1 / 'sub2'
        sub2.mkdir(mode=0o755)
        # 显式chmod确保权限
        sub2.chmod(0o777)
        
        test_file = sub2 / 'file.py'
        test_file.write_text('# test')
        test_file.chmod(0o755)
        
        # 验证应该失败（祖先不安全）
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(test_file), 'file in unsafe ancestor')
        
        self.assertIn('ancestor', str(cm.exception).lower())
        self.assertIn('group/world-writable', str(cm.exception).lower())
    
    def test_verify_trusted_path_ancestor_symlink(self):
        """测试：拒绝祖先是符号链接"""
        # 创建目标目录
        target_dir = Path(self.test_dir) / 'target_dir'
        target_dir.mkdir(mode=0o755)
        
        # 创建符号链接目录
        link_dir = Path(self.test_dir) / 'link_dir'
        link_dir.symlink_to(target_dir)
        
        # 在链接目录下创建文件
        test_file = link_dir / 'file.py'
        test_file.write_text('# test')
        test_file.chmod(0o755)
        
        # 验证应该失败（祖先是符号链接）
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(test_file), 'file in symlink dir')
        
        self.assertIn('symlink', str(cm.exception).lower())
    
    def test_verify_trusted_path_path_traversal(self):
        """测试：拒绝路径穿越"""
        # 创建嵌套目录
        inner_dir = Path(self.test_dir) / 'inner'
        inner_dir.mkdir(mode=0o755)
        
        safe_file = inner_dir / 'safe.py'
        safe_file.write_text('# safe')
        safe_file.chmod(0o755)
        
        # 使用 ../ 路径（应被检测并拒绝）
        traversal_path = inner_dir / '..' / 'inner' / 'safe.py'
        
        # 验证应该失败
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(traversal_path), 'traversal')
        
        self.assertIn('traversal', str(cm.exception).lower())
    
    def test_root_mode_fixed_path(self):
        """Root 分支必须调用产品函数。euid 在这里打桩；真实 euid 0 由 proof 覆盖。"""
        os.environ['RELEASE_CLI'] = '/tmp/fake.py'
        os.environ['RELEASE_ROOT'] = '/tmp/evil-root'
        os.environ['RELEASE_TEST_SANDBOX'] = '/tmp/a:/tmp/b'
        old_config = dispatcher.CONFIG.copy()
        dispatcher.CONFIG['release_cli'] = '/tmp/another_fake.py'
        dispatcher.CONFIG['RELEASE_ROOT'] = '/tmp/sandbox'
        dispatcher.CONFIG['release_root'] = '/tmp/sandbox'
        try:
            with patch('dispatcher.os.geteuid', return_value=0):
                decision = dispatcher.resolve_release_invocation()
            self.assertEqual(decision['release_py'], '/opt/inspect-deploy/bin/release.py')
            self.assertEqual(
                decision['command_prefix'],
                ['/usr/bin/python3', '/opt/inspect-deploy/bin/release.py'],
            )
            self.assertFalse(decision['is_test_sandbox'])
            self.assertFalse(decision['use_sudo'])
            env = dispatcher.release_child_env(False)
            self.assertNotIn('RELEASE_CLI', env)
            self.assertNotIn('RELEASE_ROOT', env)
            self.assertNotIn('RELEASE_TEST_SANDBOX', env)
        finally:
            dispatcher.CONFIG.clear()
            dispatcher.CONFIG.update(old_config)

    def test_nonroot_without_release_cli_uses_sudo_helper(self):
        """非 root 且没有 RELEASE_CLI 时走固定 helper，不能因 CONFIG 进入测试注入。"""
        if 'RELEASE_CLI' in os.environ:
            del os.environ['RELEASE_CLI']
        old_config = dispatcher.CONFIG.copy()
        dispatcher.CONFIG['RELEASE_ROOT'] = '/tmp/sandbox'
        dispatcher.CONFIG['release_cli'] = '/tmp/not-explicit.py'
        try:
            with patch('dispatcher.os.geteuid', return_value=1000):
                decision = dispatcher.resolve_release_invocation()
            self.assertFalse(decision['is_test_sandbox'])
            self.assertTrue(decision['use_sudo'])
            self.assertEqual(decision['release_py'], '/opt/inspect-deploy/bin/release.py')
            self.assertEqual(
                decision['command_prefix'],
                ['sudo', '-n', '--', '/opt/inspect-deploy/bin/inspect-release'],
            )
        finally:
            dispatcher.CONFIG.clear()
            dispatcher.CONFIG.update(old_config)

    def test_nonroot_explicit_release_cli_allowed(self):
        """非 root 且显式 RELEASE_CLI 才允许测试注入，并且不 sudo。"""
        test_release = Path(self.test_dir) / 'test_release.py'
        test_release.write_text('# test')
        test_release.chmod(0o755)
        os.environ['RELEASE_CLI'] = str(test_release)
        with patch('dispatcher.os.geteuid', return_value=1000):
            decision = dispatcher.resolve_release_invocation()
        self.assertTrue(decision['is_test_sandbox'])
        self.assertFalse(decision['use_sudo'])
        self.assertEqual(decision['release_py'], str(test_release))
        self.assertEqual(decision['command_prefix'], [sys.executable, str(test_release)])

if __name__ == '__main__':
    unittest.main()
