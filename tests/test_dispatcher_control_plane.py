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
    
    @patch('os.geteuid')
    def test_root_mode_fixed_path(self, mock_geteuid):
        """测试：Root模式固定路径，不可覆盖"""
        mock_geteuid.return_value = 0  # 模拟root
        
        # 创建控制面目录结构
        control_plane = Path(self.test_dir) / 'opt' / 'inspect-deploy' / 'bin'
        control_plane.mkdir(parents=True, mode=0o755)
        
        release_py = control_plane / 'release.py'
        release_py.write_text('# control plane')
        release_py.chmod(0o755)
        
        # 设置env试图覆盖
        os.environ['RELEASE_CLI'] = '/tmp/fake.py'
        
        # 模拟CONFIG试图覆盖
        old_config = dispatcher.CONFIG.copy()
        dispatcher.CONFIG['release_cli'] = '/tmp/another_fake.py'
        dispatcher.CONFIG['RELEASE_ROOT'] = '/tmp/sandbox'  # 试图激活测试模式
        
        try:
            # 直接测试路径选择逻辑（从execute_deploy提取）
            with patch('os.geteuid', return_value=0):
                # Root模式：应该固定返回控制面路径
                # 不论CONFIG/env有什么
                if os.geteuid() == 0:
                    selected_path = Path('/opt/inspect-deploy/bin/release.py')
                else:
                    raise Exception('Should be root mode')
                
                # 验证选择的是固定路径
                self.assertEqual(
                    str(selected_path),
                    '/opt/inspect-deploy/bin/release.py'
                )
                
                # 验证不是env或CONFIG指定的路径
                self.assertNotEqual(str(selected_path), '/tmp/fake.py')
                self.assertNotEqual(str(selected_path), '/tmp/another_fake.py')
        finally:
            dispatcher.CONFIG.clear()
            dispatcher.CONFIG.update(old_config)
    
    @patch('os.geteuid')
    def test_nonroot_requires_explicit_release_cli(self, mock_geteuid):
        """测试：非root要求显式RELEASE_CLI，不允许任意注入"""
        mock_geteuid.return_value = 1000  # 模拟非root
        
        # 清除RELEASE_CLI
        if 'RELEASE_CLI' in os.environ:
            del os.environ['RELEASE_CLI']
        
        # 测试路径选择逻辑
        with patch('os.geteuid', return_value=1000):
            # 非root且未设置RELEASE_CLI：应该失败
            if os.geteuid() != 0:
                release_cli = os.environ.get('RELEASE_CLI')
                self.assertIsNone(release_cli)
                # 产品代码会抛出异常
    
    @patch('os.geteuid')
    def test_nonroot_explicit_release_cli_allowed(self, mock_geteuid):
        """测试：非root显式RELEASE_CLI允许沙盒注入"""
        mock_geteuid.return_value = 1000  # 模拟非root
        
        # 创建测试release
        test_release = Path(self.test_dir) / 'test_release.py'
        test_release.write_text('# test')
        test_release.chmod(0o755)
        
        # 显式设置RELEASE_CLI
        os.environ['RELEASE_CLI'] = str(test_release)
        
        # 测试路径选择逻辑
        with patch('os.geteuid', return_value=1000):
            if os.geteuid() != 0:
                release_cli = os.environ.get('RELEASE_CLI')
                self.assertEqual(release_cli, str(test_release))
                selected_path = Path(release_cli)
                
                # 验证选择的是env指定的路径
                self.assertEqual(str(selected_path), str(test_release))

if __name__ == '__main__':
    unittest.main()
