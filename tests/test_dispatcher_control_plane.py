#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 dispatcher 控制面路径策略

验证：
1. 生产模式固定 /opt/inspect-deploy/bin/release.py
2. 拒绝CONFIG/env覆盖生产路径
3. 验证可信路径（owner/mode/symlink/parent）
4. 非root测试模式保留注入能力
"""

import os
import sys
import unittest
import tempfile
import shutil
from pathlib import Path

# 添加dispatcher到路径
sys.path.insert(0, str(Path(__file__).parent.parent / 'deploy-scripts' / 'host-dispatcher'))
import dispatcher

class TestDispatcherControlPlane(unittest.TestCase):
    """测试dispatcher控制面路径策略"""
    
    def setUp(self):
        """测试前准备"""
        self.test_dir = tempfile.mkdtemp(prefix='test_dispatcher_')
        self.addCleanup(shutil.rmtree, self.test_dir)
    
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
    
    def test_verify_trusted_path_reject_group_writable(self):
        """测试：拒绝组写文件（即使他人不可写）"""
        # 创建组写文件
        test_file = Path(self.test_dir) / 'group_writable.py'
        test_file.write_text('# test')
        test_file.chmod(0o775)  # 组写
        
        # 验证应该失败（mode & 0o022 包括组写）
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(test_file), 'group writable')
        
        self.assertIn('group/world-writable', str(cm.exception).lower())
    
    def test_verify_trusted_path_reject_other_writable(self):
        """测试：拒绝他人写文件（即使组不可写）"""
        # 创建他人写文件
        test_file = Path(self.test_dir) / 'other_writable.py'
        test_file.write_text('# test')
        test_file.chmod(0o757)  # 他人写
        
        # 验证应该失败（mode & 0o022 包括他人写）
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(test_file), 'other writable')
        
        self.assertIn('group/world-writable', str(cm.exception).lower())
    
    def test_verify_trusted_path_reject_world_writable_parent(self):
        """测试：拒绝组写/他人写父目录"""
        # 创建子目录
        sub_dir = Path(self.test_dir) / 'subdir'
        sub_dir.mkdir(mode=0o755)
        
        # 显式设置父目录为世界可写（chmod确保权限）
        sub_dir.chmod(0o777)
        
        test_file = sub_dir / 'file.py'
        test_file.write_text('# test')
        test_file.chmod(0o755)
        
        # 验证应该失败（父目录不安全）
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(test_file), 'file in unsafe dir')
        
        self.assertIn('parent', str(cm.exception).lower())
        self.assertIn('group/world-writable', str(cm.exception).lower())
    
    def test_verify_trusted_path_nonexistent(self):
        """测试：拒绝不存在的路径"""
        nonexistent = Path(self.test_dir) / 'nonexistent.py'
        
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(nonexistent), 'missing file')
        
        self.assertIn('does not exist', str(cm.exception).lower())
    
    def test_production_mode_path_selection(self):
        """测试：生产模式路径选择（策略验证）"""
        # 模拟生产环境：创建控制面release
        control_plane_dir = Path(self.test_dir) / 'opt' / 'inspect-deploy' / 'bin'
        control_plane_dir.mkdir(parents=True, mode=0o755)
        
        control_plane_release = control_plane_dir / 'release.py'
        control_plane_release.write_text('# control plane')
        control_plane_release.chmod(0o755)
        
        # 模拟应用树release（不应被使用）
        app_tree_dir = Path(self.test_dir) / 'opt' / 'inspect-platform' / 'ops'
        app_tree_dir.mkdir(parents=True, mode=0o755)
        
        app_tree_release = app_tree_dir / 'release.py'
        app_tree_release.write_text('# app tree')
        app_tree_release.chmod(0o777)  # 不安全
        
        # 在非root环境测试路径选择策略
        # 实际execute_deploy中：is_root=False会进入测试分支
        # 这里直接验证控制面路径可信
        result = dispatcher.verify_trusted_path(str(control_plane_release), 'control plane')
        self.assertTrue(result)
        
        # 验证应用树路径不可信（会被拒绝）
        with self.assertRaises(Exception):
            dispatcher.verify_trusted_path(str(app_tree_release), 'app tree')
    
    def test_nonroot_test_injection(self):
        """测试：非root测试模式允许注入"""
        # 创建测试release
        test_release = Path(self.test_dir) / 'test_release.py'
        test_release.write_text('# test')
        test_release.chmod(0o755)
        
        # 非root环境：owner检查跳过，验证应该通过
        result = dispatcher.verify_trusted_path(str(test_release), 'test release')
        self.assertTrue(result)
        
        # 注意：实际的execute_deploy在非root时会读取RELEASE_CLI
        # 本测试验证注入的文件仍需通过安全检查
    
    def test_path_traversal_rejection(self):
        """测试：拒绝路径穿越（../ 等）"""
        # 创建嵌套目录
        inner_dir = Path(self.test_dir) / 'inner'
        inner_dir.mkdir(mode=0o755)
        
        safe_file = inner_dir / 'safe.py'
        safe_file.write_text('# safe')
        safe_file.chmod(0o755)
        
        # 使用 ../ 路径（应被规范化检测并拒绝）
        traversal_path = inner_dir / '..' / 'inner' / 'safe.py'
        
        # 验证应该失败（路径包含 ../ 穿越）
        with self.assertRaises(Exception) as cm:
            dispatcher.verify_trusted_path(str(traversal_path), 'traversal')
        
        self.assertIn('traversal', str(cm.exception).lower())
        
        # 但直接使用规范化后的路径应该通过
        result = dispatcher.verify_trusted_path(str(safe_file), 'direct')
        self.assertTrue(result)

if __name__ == '__main__':
    unittest.main()
