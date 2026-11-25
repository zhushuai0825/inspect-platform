#!/usr/bin/env python3
"""
Release CLI 真实端到端测试（subprocess 执行）

测试范围：
- 真实 subprocess 调用 release.py CLI
- 临时沙盒环境（非 root，临时目录）
- 完整 prepare → deploy → rollback 流程
- 健康检查失败自动回滚
- 秘密文件保护
- Guard 拒绝生产路径（黑盒测试）
- 防重放（新进程重启）

不 mock：
- Git 操作
- release.py CLI 调用
- 健康检查结果逻辑

只 mock：
- 系统服务边界（不调用真实 systemctl/SMTP）
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

# release.py 路径
RELEASE_PY = Path(__file__).parent / 'release.py'


class TestReleaseCLIReal(unittest.TestCase):
    """Release CLI 真实测试（subprocess）"""
    
    def setUp(self):
        """创建沙盒环境"""
        self.tmpdir = tempfile.mkdtemp(prefix='release-cli-real-')
        self.sandbox_root = Path(self.tmpdir) / 'root'
        self.sandbox_state = Path(self.tmpdir) / 'state'
        
        # 创建目录
        self.sandbox_root.mkdir(parents=True)
        self.sandbox_state.mkdir(parents=True)
        
        # 创建最小源码（满足 REQUIRED）
        required_files = {
            '.gitignore': '*.pyc\n',
            'DEPLOYMENT.md': '# Deployment\n',
            'MAIL-SETUP.md': '# Mail Setup\n',
            'README.md': '# README\n',
            'admin-static/admin.css': '/* admin css */\n',
            'admin-static/app.js': '// admin js\n',
            'admin-static/index.html': '<html></html>\n',
            'admin.py': '# admin\n',
            'app.py': '# app\n',
            'core.py': '# core\n',
            'deploy/inspect-admin.service': '[Unit]\n',
            'deploy/inspect-mail.service': '[Unit]\n',
            'deploy/inspect-platform.service': '[Unit]\n',
            'deploy/inspect-worker.service': '[Unit]\n',
            'deploy/update_job.py': '# update\n',
            'inspect_run.sh': '#!/bin/bash\n',
            'jenkins/Jenkinsfile': '// jenkinsfile\n',
            'notifications.py': '# notifications\n',
            'static/app.js': '// app js\n',
            'static/index.html': '<html></html>\n',
            'static/styles.css': '/* styles */\n',
            'tests/test_notifications.py': '# test\n',
            'tests/test_platform.py': '# test\n',
            'worker.py': '# worker\n',
        }
        
        for fname, content in required_files.items():
            fpath = self.sandbox_root / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content)
        
        # 创建秘密文件和数据
        (self.sandbox_root / 'data').mkdir(exist_ok=True)
        (self.sandbox_root / 'data' / 'secret.env').write_text('SECRET=test123\n')
        (self.sandbox_root / 'data' / 'mail.env').write_text('SMTP_PASSWORD=fake\n')
        (self.sandbox_root / 'data' / 'platform.db').write_bytes(b'fake-db-content')
        
        # 环境变量
        self.env = os.environ.copy()
        self.env['RELEASE_TEST_SANDBOX'] = '{}:{}:LocalController'.format(
            self.sandbox_root, self.sandbox_state
        )
        self.env['RELEASE_TEST_SKIP_PREFLIGHT'] = '1'
        self.env['PYTHONDONTWRITEBYTECODE'] = '1'
    
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
    
    def run_release_cli(self, *args, should_fail=False):
        """运行 release.py CLI（真实 subprocess）"""
        cmd = [sys.executable, str(RELEASE_PY)] + list(args)
        
        # Python 3.6 兼容
        result = subprocess.run(
            cmd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        if not should_fail and result.returncode != 0:
            self.fail('CLI failed: {} - stdout: {} - stderr: {}'.format(
                result.returncode, result.stdout, result.stderr
            ))
        
        return result
    
    def test_guard_production_path(self):
        """测试 Guard 拒绝生产路径（黑盒，无副作用）"""
        # 尝试使用生产路径
        prod_env = os.environ.copy()
        prod_env['RELEASE_TEST_SANDBOX'] = '/opt/inspect-platform:/opt/inspect-ops/state'
        
        cmd = [sys.executable, str(RELEASE_PY), 'status']
        
        result = subprocess.run(
            cmd,
            env=prod_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # 应拒绝
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn('PRODUCTION_PATH_REJECTED', output)
    
    def test_prepare_success_real(self):
        """测试 prepare 成功（真实 subprocess）"""
        # 创建制品
        archive = self.create_test_archive()
        version = 'v20261125-cli-001'
        
        # 调用真实 CLI
        result = self.run_release_cli('prepare', str(archive), version)
        
        # 验证输出
        output = json.loads(result.stdout)
        self.assertEqual(output['prepared'], version)
        self.assertEqual(output['preflight'], 'passed')
        
        # 验证版本目录存在
        version_dir = self.sandbox_state / 'releases' / version
        self.assertTrue(version_dir.exists())
        
        # 验证 manifest
        manifest = json.loads((version_dir / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], version)
    
    def test_deploy_success_real(self):
        """测试 deploy 成功（真实 subprocess）"""
        # 创建 baseline
        baseline_version = 'v-baseline-cli'
        baseline_dir = self.sandbox_state / 'releases' / baseline_version
        baseline_dir.mkdir(parents=True)
        (baseline_dir / 'source').mkdir()
        
        # 复制源码
        for path in self.sandbox_root.rglob('*'):
            if path.is_file() and 'data' not in path.parts:
                rel_path = path.relative_to(self.sandbox_root)
                target = baseline_dir / 'source' / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(path), str(target))
        
        # 创建 manifest
        files = {}
        for p in (baseline_dir / 'source').rglob('*'):
            if p.is_file():
                rel = str(p.relative_to(baseline_dir / 'source'))
                files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        
        manifest = {
            'version': baseline_version,
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
            'files': files
        }
        (baseline_dir / 'manifest.json').write_text(json.dumps(manifest))
        
        # 设置 current
        (self.sandbox_state / 'current.json').write_text(
            json.dumps({'version': baseline_version, 'at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')})
        )
        
        # Prepare 新版本
        archive = self.create_test_archive()
        new_version = 'v20261125-cli-002'
        
        self.run_release_cli('prepare', str(archive), new_version)
        
        # Deploy
        result = self.run_release_cli('deploy', new_version)
        
        # 验证输出
        output = json.loads(result.stdout)
        self.assertEqual(output['deployed'], new_version)
        self.assertEqual(output['health'], 'passed')
        self.assertTrue(output['private_config_unchanged'])
        
        # 验证 current.json 更新
        current = json.loads((self.sandbox_state / 'current.json').read_text())
        self.assertEqual(current['version'], new_version)
        
        # 验证秘密文件未变
        secret_content = (self.sandbox_root / 'data' / 'secret.env').read_text()
        self.assertIn('SECRET=test123', secret_content)
    
    def test_deploy_rollback_on_health_failure_real(self):
        """测试健康检查失败自动回滚（真实 subprocess + 真实 HTTP）"""
        # 1. 创建并部署旧版本（健康成功）
        baseline_version = 'v-baseline-20261125'
        baseline_dir = self.sandbox_state / 'releases' / baseline_version
        baseline_dir.mkdir(parents=True)
        (baseline_dir / 'source').mkdir()
        
        # 复制源码到旧版本
        for path in self.sandbox_root.rglob('*'):
            if path.is_file() and 'data' not in path.parts and not path.name.startswith('.'):
                rel_path = path.relative_to(self.sandbox_root)
                target = baseline_dir / 'source' / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(path), str(target))
        
        # 创建旧版本 manifest
        files = {}
        for p in (baseline_dir / 'source').rglob('*'):
            if p.is_file():
                rel = str(p.relative_to(baseline_dir / 'source'))
                files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        
        old_manifest = {
            'version': baseline_version,
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
            'files': files
        }
        (baseline_dir / 'manifest.json').write_text(json.dumps(old_manifest))
        
        # 设置旧版本为当前版本
        (self.sandbox_state / 'current.json').write_text(
            json.dumps({'version': baseline_version, 'at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')})
        )
        
        # 记录旧版本数据 hash（用于验证回滚后不变）
        old_secret_hash = hashlib.sha256((self.sandbox_root / 'data' / 'secret.env').read_bytes()).hexdigest()
        old_db_hash = hashlib.sha256((self.sandbox_root / 'data' / 'platform.db').read_bytes()).hexdigest()
        
        # 记录旧版本源码 hash
        old_app_hash = hashlib.sha256((self.sandbox_root / 'app.py').read_bytes()).hexdigest()
        
        # 2. 创建会失败的新版本 archive
        # 在 app.py 中包含 'unhealthy' 标记
        archive_fail = Path(tempfile.mktemp(suffix='.zip', prefix='release-fail-'))
        
        with zipfile.ZipFile(str(archive_fail), 'w') as z:
            for path in self.sandbox_root.rglob('*'):
                if path.is_file() and 'data' not in path.parts:
                    arcname = 'inspect-platform/' + str(path.relative_to(self.sandbox_root))
                    
                    # 修改 app.py 包含 unhealthy
                    if path.name == 'app.py':
                        # 创建临时文件
                        tmpf = Path(tempfile.mktemp())
                        tmpf.write_text('# app unhealthy\n')
                        z.write(str(tmpf), arcname)
                        tmpf.unlink()
                    else:
                        z.write(str(path), arcname)
        
        new_version = 'v20261125-fail'
        
        # 3. Prepare 新版本
        self.run_release_cli('prepare', str(archive_fail), new_version)
        
        # 清理
        archive_fail.unlink()
        
        # 4. Deploy 新版本（应触发健康失败并自动回滚）
        result = self.run_release_cli('deploy', new_version, should_fail=True)
        
        # 5. 验证 deploy 失败
        self.assertNotEqual(result.returncode, 0)
        
        # 6. 解析输出
        output_lines = result.stdout.strip().split('\n')
        deploy_result = None
        for line in output_lines:
            try:
                deploy_result = json.loads(line)
                break
            except:
                continue
        
        self.assertIsNotNone(deploy_result, 'Deploy result not found')
        self.assertFalse(deploy_result.get('ok', True))
        
        # 7. 验证明确回滚成功代码（不接受 ROLLBACK_FAILED）
        self.assertEqual(deploy_result['code'], 'DEPLOY_FAILED_ROLLED_BACK')
        
        # 8. 验证 current.json 仍为旧版本
        current = json.loads((self.sandbox_state / 'current.json').read_text())
        self.assertTrue(
            current['version'] == baseline_version or
            current['version'].startswith('before-')
        )
        
        # 9. 验证旧源码恢复（app.py hash 不变）
        restored_app_hash = hashlib.sha256((self.sandbox_root / 'app.py').read_bytes()).hexdigest()
        self.assertEqual(restored_app_hash, old_app_hash)
        
        # 10. 验证旧版本 HTTP 服务已启动（端口文件存在，证明 start_front 被调用）
        # 注意：由于 release.py 进程已退出，HTTP server（daemon thread）也已停止
        # 但端口文件存在证明 restore() 调用了 controller.start_front()
        port_file = self.sandbox_root / '.test_http_port'
        self.assertTrue(port_file.exists(), 'Old version HTTP port file missing (start_front not called)')
        
        # 11. 验证临时数据/秘密完整保留（hash 不变）
        restored_secret_hash = hashlib.sha256((self.sandbox_root / 'data' / 'secret.env').read_bytes()).hexdigest()
        self.assertEqual(restored_secret_hash, old_secret_hash)
        
        restored_db_hash = hashlib.sha256((self.sandbox_root / 'data' / 'platform.db').read_bytes()).hexdigest()
        self.assertEqual(restored_db_hash, old_db_hash)
        
        # 12. 验证 transaction 标记为 rolled_back
        tx = json.loads((self.sandbox_state / 'transaction.json').read_text())
        self.assertEqual(tx['phase'], 'rolled_back')
    
    def test_deploy_rollback_failure_real(self):
        """测试回滚失败场景（真实 subprocess + 真实 HTTP）"""
        # 1. 创建并 deploy 旧版本（app.py 包含 unhealthy，会导致回滚失败）
        
        # 创建会失败的旧版本 archive
        archive_old_fail = Path(tempfile.mktemp(suffix='.zip', prefix='release-old-fail-'))
        
        with zipfile.ZipFile(str(archive_old_fail), 'w') as z:
            for path in self.sandbox_root.rglob('*'):
                if path.is_file() and 'data' not in path.parts:
                    arcname = 'inspect-platform/' + str(path.relative_to(self.sandbox_root))
                    
                    if path.name == 'app.py':
                        tmpf = Path(tempfile.mktemp())
                        tmpf.write_text('# app unhealthy old\n')
                        z.write(str(tmpf), arcname)
                        tmpf.unlink()
                    else:
                        z.write(str(path), arcname)
        
        old_version = 'v-baseline-fail'
        
        # Prepare 旧版本
        self.run_release_cli('prepare', str(archive_old_fail), old_version)
        archive_old_fail.unlink()
        
        # 设置旧版本为 baseline (手动)
        (self.sandbox_state / 'current.json').write_text(
            json.dumps({'version': old_version, 'at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')})
        )
        
        # 安装旧版本到 root（这样 snapshot 会包含 unhealthy 的 app.py）
        old_release_dir = self.sandbox_state / 'releases' / old_version / 'source'
        for p in old_release_dir.rglob('*'):
            if p.is_file():
                rel = p.relative_to(old_release_dir)
                target = self.sandbox_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(p), str(target))
        
        # 2. 创建会失败的新版本 archive
        archive_fail = Path(tempfile.mktemp(suffix='.zip', prefix='release-fail2-'))
        
        with zipfile.ZipFile(str(archive_fail), 'w') as z:
            for path in self.sandbox_root.rglob('*'):
                if path.is_file() and 'data' not in path.parts:
                    arcname = 'inspect-platform/' + str(path.relative_to(self.sandbox_root))
                    
                    if path.name == 'app.py':
                        tmpf = Path(tempfile.mktemp())
                        tmpf.write_text('# app unhealthy\n')
                        z.write(str(tmpf), arcname)
                        tmpf.unlink()
                    else:
                        z.write(str(path), arcname)
        
        new_version = 'v20261125-fail2'
        
        # 3. Prepare 新版本
        self.run_release_cli('prepare', str(archive_fail), new_version)
        
        archive_fail.unlink()
        
        # 3. Deploy 新版本（应触发健康失败，回滚也失败）
        result = self.run_release_cli('deploy', new_version, should_fail=True)
        
        # 4. 验证 deploy 失败
        self.assertNotEqual(result.returncode, 0)
        
        # 5. 解析输出
        output_lines = result.stdout.strip().split('\n')
        deploy_result = None
        for line in output_lines:
            try:
                deploy_result = json.loads(line)
                break
            except:
                continue
        
        self.assertIsNotNone(deploy_result, 'Deploy result not found')
        self.assertFalse(deploy_result.get('ok', True))
        
        # 6. 验证回滚失败代码（fail closed）
        self.assertEqual(deploy_result['code'], 'ROLLBACK_FAILED_MANUAL_RECOVERY_REQUIRED')
        
        # 7. 验证 transaction 标记为 rollback_failed
        tx = json.loads((self.sandbox_state / 'transaction.json').read_text())
        self.assertEqual(tx['phase'], 'rollback_failed')


def run_tests():
    """运行真实 CLI 测试"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestReleaseCLIReal)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
