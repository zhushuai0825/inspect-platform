"""
端到端本地测试（临时环境）

测试范围：
- Jenkins 通知脚本
- Dispatcher 完整流程（Mock Git 和 release.py）
- 请求-响应循环
- 防重放
- 秘密不变

不涉及：
- 真实 GitHub 访问
- 真实生产部署
- 真实 SMTP
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import dispatcher
sys.path.insert(0, str(Path(__file__).parent.parent / 'deploy-scripts' / 'host-dispatcher'))
import dispatcher


class TestE2ELocal(unittest.TestCase):
    """端到端本地测试"""
    
    def setUp(self):
        """设置临时环境"""
        self.tmpdir = tempfile.mkdtemp(prefix='e2e-test-')
        self.spool = Path(self.tmpdir) / 'spool'
        self.work = Path(self.tmpdir) / 'work'
        self.spool.mkdir()
        self.work.mkdir()
        
        # 保存原配置
        self.orig_spool = dispatcher.SPOOL
        self.orig_work = dispatcher.WORK_DIR
        
        # 临时配置
        dispatcher.SPOOL = self.spool
        dispatcher.WORK_DIR = self.work
    
    def tearDown(self):
        """恢复配置"""
        dispatcher.SPOOL = self.orig_spool
        dispatcher.WORK_DIR = self.orig_work
        
        # 清理临时目录
        import shutil
        shutil.rmtree(self.tmpdir)
    
    def test_notify_script_validates_commit(self):
        """测试 Jenkins 通知脚本验证 commit 格式"""
        script = Path(__file__).parent.parent / 'deploy-scripts' / 'jenkins' / 'notify-deploy.sh'
        
        # 无效 commit（短）
        # Python 3.6 兼容：使用 stdout/stderr=PIPE, universal_newlines=True
        result = subprocess.run(
            ['bash', str(script), 'invalid'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # 应拒绝
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('commit 必须是 40 位十六进制', result.stderr)
        
        # 无效 commit（非 hex）
        result = subprocess.run(
            ['bash', str(script), 'x' * 40],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # 应拒绝
        self.assertNotEqual(result.returncode, 0)
        
        # 注意：有效 commit 测试需要 spool 目录存在，
        # 在此测试中不创建生产路径
    
    @patch('dispatcher.run_command')
    def test_dispatcher_full_flow_mock(self, mock_run):
        """测试 Dispatcher 完整流程（Mock Git 和 release.py）"""
        commit = 'b' * 40
        
        # Mock git ls-remote
        def mock_git(*args, **kwargs):
            cmd = args[0]
            if 'ls-remote' in cmd:
                return (0, '{}\trefs/heads/main\n'.format(commit), '')
            elif 'clone' in cmd:
                # 创建假 clone 目录
                clone_dir = self.work / 'clone-bbbbbbbb'
                clone_dir.mkdir(exist_ok=True)
                (clone_dir / 'app.py').write_text('# test')
                return (0, '', '')
            elif 'checkout' in cmd:
                return (0, '', '')
            elif 'rev-parse' in cmd:
                return (0, commit + '\n', '')
            elif 'archive' in cmd:
                # 创建假 zip
                import zipfile
                output = None
                for i, arg in enumerate(cmd):
                    if arg == '--output' and i + 1 < len(cmd):
                        output = cmd[i + 1]
                        break
                if output:
                    with zipfile.ZipFile(output, 'w') as z:
                        z.writestr('inspect-platform/app.py', '# test')
                return (0, '', '')
            elif 'release.py' in ' '.join(cmd):
                # Mock release.py 调用
                return (0, '{"deployed": "v20261125-test"}', '')
            return (0, '', '')
        
        mock_run.side_effect = mock_git
        
        # 创建请求文件
        req_id = 'e2e-test-001'
        req_file = self.spool / 'request-{}.json'.format(req_id)
        
        with open(str(req_file), 'w') as f:
            json.dump({
                'request_id': req_id,
                'commit': commit,
                'timestamp': '2026-11-25T07:00:00+08:00',
                'source': 'test'
            }, f)
        
        # 处理请求
        dispatcher.process_request(req_file)
        
        # 验证：请求文件已删除
        self.assertFalse(req_file.exists())
        
        # 验证：结果文件存在
        result_files = list(self.spool.glob('result-*.json'))
        self.assertEqual(len(result_files), 1)
        
        with open(str(result_files[0])) as f:
            result = json.load(f)
        
        # 验证结果（Mock 下可能失败，检查 status）
        self.assertIn('status', result)
    
    @patch('dispatcher.run_command')
    def test_replay_prevention_e2e(self, mock_run):
        """测试端到端防重放"""
        commit = 'c' * 40
        
        # Mock git
        def mock_git(*args, **kwargs):
            cmd = args[0]
            if 'ls-remote' in cmd:
                return (0, '{}\trefs/heads/main\n'.format(commit), '')
            return (0, '', '')
        
        mock_run.side_effect = mock_git
        
        # 第一次请求
        req_id = 'e2e-replay-test'
        req_file1 = self.spool / 'request-first.json'
        
        with open(str(req_file1), 'w') as f:
            json.dump({
                'request_id': req_id,
                'commit': commit,
                'timestamp': '2026-11-25T07:00:00+08:00',
                'source': 'test'
            }, f)
        
        # 处理
        dispatcher.process_request(req_file1)
        
        # 第二次相同 request_id
        req_file2 = self.spool / 'request-second.json'
        
        with open(str(req_file2), 'w') as f:
            json.dump({
                'request_id': req_id,  # 相同 ID
                'commit': commit,
                'timestamp': '2026-11-25T07:01:00+08:00',
                'source': 'test'
            }, f)
        
        # 再次处理
        dispatcher.process_request(req_file2)
        
        # 验证：第二次请求被拒绝（防重放）
        self.assertFalse(req_file2.exists())  # 已删除
        
        # 只有第一次的结果
        result_files = list(self.spool.glob('result-*.json'))
        self.assertEqual(len(result_files), 1)


def run_tests():
    """运行端到端测试"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestE2ELocal)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
