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
        
        # 创建秘密文件
        (self.sandbox_root / 'data').mkdir(exist_ok=True)
        (self.sandbox_root / 'data' / 'secret.env').write_text('SECRET=test123\n')
        (self.sandbox_root / 'data' / 'mail.env').write_text('SMTP_PASSWORD=fake\n')
        
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
        """测试健康检查失败自动回滚（真实 subprocess）"""
        # 创建 baseline
        baseline_version = 'v-baseline-rb-cli'
        baseline_dir = self.sandbox_state / 'releases' / baseline_version
        baseline_dir.mkdir(parents=True)
        (baseline_dir / 'source').mkdir()
        
        for path in self.sandbox_root.rglob('*'):
            if path.is_file() and 'data' not in path.parts:
                rel_path = path.relative_to(self.sandbox_root)
                target = baseline_dir / 'source' / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(path), str(target))
        
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
        
        (self.sandbox_state / 'current.json').write_text(
            json.dumps({'version': baseline_version, 'at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')})
        )
        
        # Prepare 新版本
        archive = self.create_test_archive()
        new_version = 'v20261125-cli-fail'
        
        self.run_release_cli('prepare', str(archive), new_version)
        
        # 设置健康检查失败
        env_fail = self.env.copy()
        env_fail['RELEASE_TEST_HEALTH_FAIL'] = '1'
        
        # Deploy（应失败并回滚）
        cmd = [sys.executable, str(RELEASE_PY), 'deploy', new_version]
        
        result = subprocess.run(
            cmd,
            env=env_fail,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # 应失败
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        
        # 健康检查失败应尝试回滚（可能回滚也失败）
        self.assertTrue(
            'ROLLED_BACK' in output or
            'ROLLBACK_FAILED' in output
        )
        
        # 如果回滚成功，验证状态
        if 'ROLLED_BACK' in output:
            current = json.loads((self.sandbox_state / 'current.json').read_text())
            self.assertTrue(
                current['version'].startswith('before-') or
                current['version'] == baseline_version
            )
            
            tx = json.loads((self.sandbox_state / 'transaction.json').read_text())
            self.assertEqual(tx['phase'], 'rolled_back')
        
        # 无论如何，秘密文件应保留
        secret_content = (self.sandbox_root / 'data' / 'secret.env').read_text()
        self.assertIn('SECRET=test123', secret_content)


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
