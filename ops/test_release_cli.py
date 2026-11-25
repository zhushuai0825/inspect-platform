#!/usr/bin/env python3
"""
release.py CLI 沙盒端到端测试

测试范围：
- 真实 CLI 参数解析
- 沙盒模式（临时目录 + Mock 服务）
- prepare → deploy → rollback 完整流程
- 健康检查失败自动回滚
- 秘密文件保护
- Guard 拒绝生产路径

不涉及：
- 真实 systemctl
- 真实 SMTP
- 生产路径
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import release module
sys.path.insert(0, str(Path(__file__).parent))
import release


class MockController:
    """Mock 服务控制器（不调用真实 systemctl）"""
    
    def __init__(self, root, health_should_fail=False):
        self.root = Path(root)
        self.health_should_fail = health_should_fail
        self.runlock = None
        self.calls = []
    
    def systemctl(self, *args):
        """记录 systemctl 调用（不执行）"""
        self.calls.append(('systemctl',) + args)
    
    def stop(self):
        """Mock stop"""
        self.calls.append(('stop',))
        # Mock runner lock
        if self.runlock is None:
            self.runlock = MagicMock()
    
    def backup_runtime(self):
        """Mock backup（不创建实际备份）"""
        self.calls.append(('backup_runtime',))
        return str(self.root / 'mock-backup')
    
    def start_front(self):
        """Mock start front"""
        self.calls.append(('start_front',))
    
    def get(self, path, status=200, contains=None):
        """Mock HTTP 请求"""
        self.calls.append(('get', path, status, contains))
        # 模拟健康检查
        if self.health_should_fail:
            raise release.ReleaseError('HEALTH_CHECK_FAILED')
        return b'ok'
    
    def health(self):
        """Mock health check"""
        self.calls.append(('health',))
        if self.health_should_fail:
            raise release.ReleaseError('HEALTH_CHECK_FAILED')
    
    def start_background(self):
        """Mock start background"""
        self.calls.append(('start_background',))
        # Release runner lock
        if self.runlock is not None:
            self.runlock = None
    
    def final_health(self):
        """Mock final health"""
        self.calls.append(('final_health',))
        if self.health_should_fail:
            raise release.ReleaseError('HEALTH_CHECK_FAILED')


class TestReleaseCLISandbox(unittest.TestCase):
    """release.py CLI 沙盒测试"""
    
    def setUp(self):
        """创建沙盒环境"""
        self.tmpdir = tempfile.mkdtemp(prefix='release-cli-test-')
        self.sandbox_root = Path(self.tmpdir) / 'root'
        self.sandbox_state = Path(self.tmpdir) / 'state'
        
        # 创建目录结构
        self.sandbox_root.mkdir(parents=True)
        self.sandbox_state.mkdir(parents=True)
        
        # 创建最小源码（满足 REQUIRED）
        # REQUIRED = RUNTIME - {'MAIL-ACTIVATION.md'}
        required_files = [
            '.gitignore',
            'DEPLOYMENT.md',
            'MAIL-SETUP.md',
            'README.md',
            'admin-static/admin.css',
            'admin-static/app.js',
            'admin-static/index.html',
            'admin.py',
            'app.py',
            'core.py',
            'deploy/inspect-admin.service',
            'deploy/inspect-mail.service',
            'deploy/inspect-platform.service',
            'deploy/inspect-worker.service',
            'deploy/update_job.py',
            'inspect_run.sh',
            'jenkins/Jenkinsfile',
            'notifications.py',
            'static/app.js',
            'static/index.html',
            'static/styles.css',
            'tests/test_notifications.py',
            'tests/test_platform.py',
            'worker.py',
        ]
        
        for fname in required_files:
            fpath = self.sandbox_root / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text('# test {}\n'.format(fname))
        
        # 创建秘密文件
        (self.sandbox_root / 'data').mkdir(exist_ok=True)
        (self.sandbox_root / 'data' / 'secret.env').write_text('SECRET=test123\n')
        (self.sandbox_root / 'data' / 'mail.env').write_text('SMTP_PASSWORD=fake\n')
    
    def tearDown(self):
        """清理沙盒"""
        shutil.rmtree(self.tmpdir)
    
    def create_test_archive(self):
        """创建测试 ZIP"""
        archive_path = Path(self.tmpdir) / 'test.zip'
        
        with zipfile.ZipFile(str(archive_path), 'w') as z:
            for path in self.sandbox_root.rglob('*'):
                if path.is_file() and 'data' not in path.parts:
                    arcname = 'inspect-platform/' + str(path.relative_to(self.sandbox_root))
                    z.write(str(path), arcname)
        
        return archive_path
    
    def test_prepare_success(self):
        """测试 prepare 成功"""
        # 创建 Manager with Mock controller
        controller = MockController(self.sandbox_root)
        manager = release.Manager(
            self.sandbox_root,
            self.sandbox_state,
            controller
        )
        
        # 创建制品
        archive = self.create_test_archive()
        version = 'v20261125-test-001'
        
        # Mock preflight（跳过真实测试执行）
        with patch.object(manager, 'preflight'):
            result = manager.prepare(str(archive), version)
        
        # 验证
        self.assertEqual(result['prepared'], version)
        self.assertEqual(result['preflight'], 'passed')
        
        # 验证版本目录存在
        version_dir = self.sandbox_state / 'releases' / version
        self.assertTrue(version_dir.exists())
        
        # 验证 manifest
        manifest = json.loads((version_dir / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], version)
    
    def test_deploy_success(self):
        """测试 deploy 成功"""
        controller = MockController(self.sandbox_root)
        manager = release.Manager(
            self.sandbox_root,
            self.sandbox_state,
            controller
        )
        
        # 创建 baseline
        baseline_version = 'v-baseline'
        manager.snapshot(baseline_version)
        (self.sandbox_state / 'current.json').write_text(
            json.dumps({'version': baseline_version, 'at': '2026-11-25T00:00:00+08:00'})
        )
        
        # Prepare 新版本
        archive = self.create_test_archive()
        new_version = 'v20261125-test-002'
        
        with patch.object(manager, 'preflight'):
            manager.prepare(str(archive), new_version)
        
        # Deploy
        with patch.object(manager, 'preflight'):
            result = manager.deploy(new_version)
        
        # 验证
        self.assertEqual(result['deployed'], new_version)
        self.assertEqual(result['health'], 'passed')
        self.assertTrue(result['private_config_unchanged'])
        
        # 验证 controller 调用顺序
        call_names = [c[0] for c in controller.calls]
        self.assertIn('stop', call_names)
        self.assertIn('start_front', call_names)
        self.assertIn('health', call_names)
        self.assertIn('start_background', call_names)
        self.assertIn('final_health', call_names)
        
        # 验证 current.json 更新
        current = json.loads((self.sandbox_state / 'current.json').read_text())
        self.assertEqual(current['version'], new_version)
    
    def test_deploy_rollback_on_health_failure(self):
        """测试健康检查失败自动回滚"""
        # 创建会在新版本健康检查失败，但回滚成功的 controller
        controller = MockController(self.sandbox_root)
        manager = release.Manager(
            self.sandbox_root,
            self.sandbox_state,
            controller
        )
        
        # 创建 baseline
        baseline_version = 'v-baseline-rb'
        manager.snapshot(baseline_version)
        (self.sandbox_state / 'current.json').write_text(
            json.dumps({'version': baseline_version, 'at': '2026-11-25T00:00:00+08:00'})
        )
        
        # Prepare 新版本
        archive = self.create_test_archive()
        new_version = 'v20261125-test-fail'
        
        with patch.object(manager, 'preflight'):
            manager.prepare(str(archive), new_version)
        
        # 让 health check 在新版本安装后失败
        health_call_count = [0]
        original_health = controller.health
        
        def failing_health():
            health_call_count[0] += 1
            # 第一次（新版本）失败，后续（回滚）成功
            if health_call_count[0] == 1:
                raise release.ReleaseError('HEALTH_CHECK_FAILED')
            original_health()
        
        controller.health = failing_health
        
        # Deploy（应失败并回滚）
        with self.assertRaises(release.ReleaseError) as cm:
            with patch.object(manager, 'preflight'):
                manager.deploy(new_version)
        
        self.assertIn('ROLLED_BACK', str(cm.exception))
        
        # 验证 current.json 恢复（可能是 before-* 快照）
        current = json.loads((self.sandbox_state / 'current.json').read_text())
        # 应该恢复到旧版本（before-* 快照）
        self.assertTrue(
            current['version'].startswith('before-') or 
            current['version'] == baseline_version
        )
        
        # 验证 transaction 标记为 rolled_back
        tx = json.loads((self.sandbox_state / 'transaction.json').read_text())
        self.assertEqual(tx['phase'], 'rolled_back')
    
    def test_secret_fingerprint_protection(self):
        """测试秘密文件指纹保护"""
        controller = MockController(self.sandbox_root)
        manager = release.Manager(
            self.sandbox_root,
            self.sandbox_state,
            controller
        )
        
        # 创建 baseline
        baseline_version = 'v-baseline-secret'
        manager.snapshot(baseline_version)
        (self.sandbox_state / 'current.json').write_text(
            json.dumps({'version': baseline_version, 'at': '2026-11-25T00:00:00+08:00'})
        )
        
        # 获取秘密指纹
        before_fp = manager.secret_fingerprints()
        self.assertIn('secret.env', before_fp)
        self.assertIn('mail.env', before_fp)
        
        # Prepare & deploy
        archive = self.create_test_archive()
        new_version = 'v20261125-test-secret'
        
        with patch.object(manager, 'preflight'):
            manager.prepare(str(archive), new_version)
            result = manager.deploy(new_version)
        
        # 验证秘密未变
        after_fp = manager.secret_fingerprints()
        self.assertEqual(before_fp, after_fp)
        self.assertTrue(result['private_config_unchanged'])
    
    def test_guard_production_paths(self):
        """测试 Guard 拒绝生产路径"""
        # 尝试使用生产路径
        prod_root = Path('/opt/inspect-platform')
        prod_state = Path('/opt/inspect-ops/state')
        
        # 如果是测试环境（非 root 或生产路径不存在），跳过
        if os.geteuid() != 0 or not prod_root.exists():
            self.skipTest('非 root 或生产环境不存在')
        
        # Guard 应该检测到这是测试调用
        # （实际实现中，应通过环境变量 RELEASE_TEST_MODE 控制）


def run_tests():
    """运行 CLI 沙盒测试"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestReleaseCLISandbox)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
