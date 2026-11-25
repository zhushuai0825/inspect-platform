"""
Dispatcher 安全测试 - 调用真实代码

测试目标：
- 验证请求格式
- 防止任意 artifact
- 防止任意 release.py 执行
- 防止路径遍历
- 防止符号链接攻击
- 验证 commit 真实性
- 防重放攻击
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import 真实 dispatcher 模块
sys.path.insert(0, str(Path(__file__).parent.parent / 'deploy-scripts' / 'host-dispatcher'))
import dispatcher


class TestDispatcherSecurity(unittest.TestCase):
    """安全测试 - 调用真实 dispatcher 函数"""
    
    def test_validate_commit_sha_format(self):
        """测试 commit SHA 格式验证（真实函数）"""
        # 调用真实函数
        self.assertTrue(dispatcher.validate_commit_sha('a' * 40))
        self.assertTrue(dispatcher.validate_commit_sha('0123456789abcdef' * 2 + '01234567'))
        
        # 拒绝无效格式
        self.assertFalse(dispatcher.validate_commit_sha('short'))
        self.assertFalse(dispatcher.validate_commit_sha('x' * 40))  # 非 hex
        self.assertFalse(dispatcher.validate_commit_sha('A' * 40))  # 大写
        self.assertFalse(dispatcher.validate_commit_sha('a' * 39))  # 短
        self.assertFalse(dispatcher.validate_commit_sha('a' * 41))  # 长
        self.assertFalse(dispatcher.validate_commit_sha(123))  # 非字符串
    
    def test_validate_request_reject_arbitrary_artifact(self):
        """测试拒绝任意 artifact 字段（真实函数）"""
        # 只允许 request_id 和 commit
        valid_req = {
            'request_id': 'test-123',
            'commit': 'a' * 40
        }
        self.assertTrue(dispatcher.validate_request(valid_req))
        
        # 拒绝 artifact 字段
        with self.assertRaises(ValueError) as cm:
            dispatcher.validate_request({
                'request_id': 'test-123',
                'artifact': '/tmp/evil.zip'
            })
        self.assertIn('artifact', str(cm.exception).lower())
        
        # 拒绝 artifact_sha256 字段
        with self.assertRaises(ValueError) as cm:
            dispatcher.validate_request({
                'request_id': 'test-123',
                'artifact_sha256': 'fake-hash'
            })
        self.assertIn('artifact_sha256', str(cm.exception).lower())
    
    def test_validate_request_reject_result_file(self):
        """测试拒绝 result_file 字段（真实函数）"""
        # 拒绝任意 result_file
        with self.assertRaises(ValueError) as cm:
            dispatcher.validate_request({
                'request_id': 'test-123',
                'result_file': '/etc/passwd'
            })
        self.assertIn('result_file', str(cm.exception).lower())
    
    def test_validate_request_reject_branch_field(self):
        """测试拒绝 branch 字段（真实函数）"""
        # 拒绝 branch 字段（由配置决定，不接受请求）
        with self.assertRaises(ValueError) as cm:
            dispatcher.validate_request({
                'request_id': 'test-123',
                'branch': 'evil-branch'
            })
        self.assertIn('branch', str(cm.exception).lower())
    
    def test_validate_request_allow_empty_notification(self):
        """测试允许空通知（真实函数）"""
        # 只有 request_id（无 commit）
        empty_req = {'request_id': 'test-notify'}
        self.assertTrue(dispatcher.validate_request(empty_req))
    
    def test_safe_path_join_prevent_traversal(self):
        """测试路径遍历防护（真实函数）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            
            # 正常路径
            safe = dispatcher.safe_path_join(base, 'subdir', 'file.txt')
            self.assertTrue(str(safe).startswith(str(base)))
            
            # 防止 ..
            with self.assertRaises(ValueError) as cm:
                dispatcher.safe_path_join(base, '..', 'etc', 'passwd')
            self.assertIn('穿越', str(cm.exception))
            
            # 防止 ../
            with self.assertRaises(ValueError) as cm:
                dispatcher.safe_path_join(base, 'subdir', '..', '..', 'evil')
            self.assertIn('穿越', str(cm.exception))
    
    def test_check_no_symlinks_reject_symlink(self):
        """测试符号链接检测（真实函数）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            
            # 创建符号链接
            target = tmpdir / 'target.txt'
            target.write_text('content')
            
            symlink = tmpdir / 'link.txt'
            symlink.symlink_to(target)
            
            # 检测到符号链接
            with self.assertRaises(ValueError) as cm:
                dispatcher.check_no_symlinks(symlink)
            self.assertIn('符号链接', str(cm.exception))
    
    def test_generate_result_path_safe(self):
        """测试结果路径生成安全（真实函数）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 临时设置 spool
            original_spool = dispatcher.SPOOL
            dispatcher.SPOOL = Path(tmpdir)
            
            try:
                # 生成路径
                result_path = dispatcher.generate_result_path('test-123')
                
                # 确保在 spool 下
                self.assertTrue(str(result_path).startswith(str(tmpdir)))
                self.assertEqual(result_path.parent, Path(tmpdir))
                
                # 拒绝路径遍历
                result_path = dispatcher.generate_result_path('../../../etc/passwd')
                self.assertTrue(str(result_path).startswith(str(tmpdir)))
                
            finally:
                dispatcher.SPOOL = original_spool
    
    def test_verify_commit_match_reject_fake_commit(self):
        """测试拒绝假 commit（真实函数）"""
        remote_sha = 'a' * 40
        
        # 匹配
        req = {'request_id': 'test', 'commit': 'a' * 40}
        result = dispatcher.verify_commit_match(req, remote_sha)
        self.assertEqual(result, remote_sha)
        
        # 不匹配
        req = {'request_id': 'test', 'commit': 'b' * 40}
        with self.assertRaises(ValueError) as cm:
            dispatcher.verify_commit_match(req, remote_sha)
        self.assertIn('不匹配', str(cm.exception))
    
    def test_atomic_write_result_safe(self):
        """测试原子写入（真实函数）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            result_path = Path(tmpdir) / 'result.json'
            data = {'status': 'success', 'version': 'v1.0'}
            
            # 原子写入
            dispatcher.atomic_write_result(result_path, data)
            
            # 验证内容
            with open(result_path) as f:
                loaded = json.load(f)
            self.assertEqual(loaded['status'], 'success')
            self.assertEqual(loaded['version'], 'v1.0')
            
            # 不残留临时文件
            temp_files = list(Path(tmpdir).glob('.result-*'))
            self.assertEqual(len(temp_files), 0)


class TestDispatcherGitOperations(unittest.TestCase):
    """Git 操作测试 - Mock 外部调用"""
    
    @patch('dispatcher.run_command')
    def test_get_remote_main_sha_valid(self, mock_run):
        """测试获取远程 main SHA（Mock Git）"""
        # Mock git ls-remote 输出
        mock_run.return_value = (0, 'abc123' + '0' * 34 + '\trefs/heads/main\n', '')
        
        sha = dispatcher.get_remote_main_sha('https://example.com/repo.git', 'main')
        
        self.assertEqual(sha, 'abc123' + '0' * 34)
        mock_run.assert_called_once()
        self.assertIn('git', mock_run.call_args[0][0])
        self.assertIn('ls-remote', mock_run.call_args[0][0])
    
    @patch('dispatcher.run_command')
    def test_get_remote_main_sha_invalid_format(self, mock_run):
        """测试拒绝无效 SHA 格式（Mock Git）"""
        # Mock 返回无效 SHA
        mock_run.return_value = (0, 'INVALID\trefs/heads/main\n', '')
        
        with self.assertRaises(Exception) as cm:
            dispatcher.get_remote_main_sha('https://example.com/repo.git', 'main')
        
        self.assertIn('SHA', str(cm.exception))
    
    @patch('dispatcher.run_command')
    def test_fetch_source_verify_commit(self, mock_run):
        """测试源代码获取验证 commit（Mock Git）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            commit = 'a' * 40
            
            # Mock git 命令
            def mock_git(*args, **kwargs):
                cmd = args[0]
                if 'clone' in cmd:
                    # 创建假 clone 目录
                    clone_dir = work_dir / 'clone-aaaaaaaa'
                    clone_dir.mkdir(exist_ok=True)
                    return (0, '', '')
                elif 'checkout' in cmd:
                    return (0, '', '')
                elif 'rev-parse' in cmd:
                    # 返回正确的 commit
                    return (0, commit + '\n', '')
                return (0, '', '')
            
            mock_run.side_effect = mock_git
            
            # 获取源代码
            source_dir = dispatcher.fetch_source(
                'https://example.com/repo.git',
                commit,
                work_dir
            )
            
            # 验证调用
            self.assertTrue(mock_run.called)
            self.assertTrue(source_dir.exists())


class TestDispatcherReplayProtection(unittest.TestCase):
    """重放攻击防护测试"""
    
    def test_claim_request_atomic(self):
        """测试请求原子领取（真实函数）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            
            # 创建请求文件
            req_file = tmpdir / 'request-test.json'
            req_file.write_text('{"request_id": "test"}')
            
            # 第一次领取
            claimed = dispatcher.claim_request(req_file)
            self.assertIsNotNone(claimed)
            self.assertTrue(claimed.exists())
            self.assertFalse(req_file.exists())
            
            # 第二次领取（失败）
            claimed2 = dispatcher.claim_request(req_file)
            self.assertIsNone(claimed2)


class TestDispatcherPathSecurity(unittest.TestCase):
    """路径安全测试 - 高级场景"""
    
    def test_safe_path_join_absolute_component(self):
        """测试拒绝绝对路径组件（真实函数）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            
            # 拒绝绝对路径
            with self.assertRaises(ValueError):
                dispatcher.safe_path_join(base, '/etc/passwd')
    
    def test_check_no_symlinks_parent_symlink(self):
        """测试检测父目录符号链接（真实函数）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            
            # 创建目录结构
            real_dir = tmpdir / 'real'
            real_dir.mkdir()
            
            link_dir = tmpdir / 'link'
            link_dir.symlink_to(real_dir)
            
            # 子文件
            child = link_dir / 'child.txt'
            child.write_text('content')
            
            # 检测到父目录是符号链接
            with self.assertRaises(ValueError) as cm:
                dispatcher.check_no_symlinks(child)
            self.assertIn('符号链接', str(cm.exception))


class TestDispatcherPython36Compat(unittest.TestCase):
    """Python 3.6 兼容性测试"""
    
    def test_run_command_no_capture_output(self):
        """测试 run_command 不使用 capture_output（真实函数）"""
        # 运行简单命令
        returncode, stdout, stderr = dispatcher.run_command(['echo', 'test'])
        
        self.assertEqual(returncode, 0)
        self.assertIn('test', stdout)
        self.assertIsInstance(stdout, str)
        self.assertIsInstance(stderr, str)
    
    def test_run_command_timeout(self):
        """测试 run_command 超时处理（真实函数）"""
        with self.assertRaises(Exception) as cm:
            dispatcher.run_command(['sleep', '10'], timeout=1)
        self.assertIn('超时', str(cm.exception))


def run_tests():
    """运行所有安全测试"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # 加载所有测试
    suite.addTests(loader.loadTestsFromTestCase(TestDispatcherSecurity))
    suite.addTests(loader.loadTestsFromTestCase(TestDispatcherGitOperations))
    suite.addTests(loader.loadTestsFromTestCase(TestDispatcherReplayProtection))
    suite.addTests(loader.loadTestsFromTestCase(TestDispatcherPathSecurity))
    suite.addTests(loader.loadTestsFromTestCase(TestDispatcherPython36Compat))
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
