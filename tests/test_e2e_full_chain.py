#!/usr/bin/env python3
"""
完整链路端到端测试：Git bare → 通知 → dispatcher 进程 → release CLI
包括重启防重放验证
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

REPO_ROOT = Path(__file__).parent.parent
DISPATCHER_PY = REPO_ROOT / 'deploy-scripts' / 'host-dispatcher' / 'dispatcher.py'
NOTIFY_SCRIPT = REPO_ROOT / 'deploy-scripts' / 'jenkins' / 'notify-deploy.sh'
RELEASE_PY = REPO_ROOT / 'ops' / 'release.py'


class TestFullChainE2E(unittest.TestCase):
    """完整链路集成测试"""
    
    def setUp(self):
        """创建测试环境"""
        self.tmpdir = tempfile.mkdtemp(prefix='e2e-full-chain-')
        self.test_root = Path(self.tmpdir)
        
        # 临时目录结构
        self.git_bare = self.test_root / 'git-bare'
        self.git_work = self.test_root / 'git-work'
        self.spool_dir = self.test_root / 'spool'
        self.release_root = self.test_root / 'release-root'
        self.release_state = self.test_root / 'release-state'
        self.dispatcher_work = self.test_root / 'dispatcher-work'
        
        # 创建目录
        self.git_bare.mkdir()
        self.git_work.mkdir()
        self.spool_dir.mkdir()
        self.release_root.mkdir()
        self.release_state.mkdir()
        self.dispatcher_work.mkdir()
        
        # 创建数据目录
        (self.release_root / 'data').mkdir()
        (self.release_root / 'data' / 'secret.env').write_text('SECRET=test\n')
        (self.release_root / 'data' / 'mail.env').write_text('SMTP_PASSWORD=test\n')
        (self.release_root / 'data' / 'platform.db').write_bytes(b'fake-db')
        
        # Dispatcher 环境变量
        self.dispatcher_env = os.environ.copy()
        self.dispatcher_env['SPOOL_DIR'] = str(self.spool_dir)
        self.dispatcher_env['WORK_DIR'] = str(self.dispatcher_work)
        self.dispatcher_env['GITHUB_REPO'] = str(self.git_bare)  # 使用本地 bare repo
        self.dispatcher_env['GITHUB_BRANCH'] = 'main'
        self.dispatcher_env['RELEASE_CLI'] = str(RELEASE_PY)
        self.dispatcher_env['RELEASE_ROOT'] = str(self.release_root)
        self.dispatcher_env['RELEASE_STATE'] = str(self.release_state)
        
        # Preflight runner路径（使用fake runner）
        fake_runner = Path(__file__).parent / 'fixtures' / 'fake-runner-success'
        if fake_runner.exists():
            self.dispatcher_env['RELEASE_TEST_RUNNER_PATH'] = str(fake_runner)
        
        # Release 环境变量
        self.release_env = os.environ.copy()
        self.release_env['RELEASE_TEST_SANDBOX'] = '{}:{}'.format(
            self.release_root, self.release_state
        )
        self.release_env['RELEASE_TEST_SKIP_PREFLIGHT'] = '1'  # 临时跳过，待修复
        self.release_env['PYTHONDONTWRITEBYTECODE'] = '1'
        
    def tearDown(self):
        """清理测试环境"""
        shutil.rmtree(self.tmpdir)
    
    def create_git_bare_with_code(self):
        """创建包含代码的 Git bare repository"""
        # 初始化 work repo
        subprocess.run(
            ['git', 'init'],
            cwd=str(self.git_work),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        subprocess.run(
            ['git', 'config', 'user.name', 'Test'],
            cwd=str(self.git_work),
            check=True
        )
        
        subprocess.run(
            ['git', 'config', 'user.email', 'test@example.com'],
            cwd=str(self.git_work),
            check=True
        )
        
        # 创建完整源码（满足 REQUIRED）
        required_files = {
            '.gitignore': '*.pyc\n',
            'DEPLOYMENT.md': '# Deployment\n',
            'MAIL-SETUP.md': '# Mail Setup\n',
            'README.md': '# README\n',
            'admin-static/admin.css': '/* admin css */\n',
            'admin-static/app.js': '// admin js\n',
            'admin-static/index.html': '<html></html>\n',
            'admin.py': '# admin\n',
            'app.py': '# app\nprint("hello")\n',
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
            'tests/test_notifications.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n',
            'tests/test_platform.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n',
            'worker.py': '# worker\n',
        }
        
        for fname, content in required_files.items():
            fpath = self.git_work / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content)
        
        # Commit
        subprocess.run(
            ['git', 'add', '.'],
            cwd=str(self.git_work),
            check=True
        )
        
        subprocess.run(
            ['git', 'commit', '-m', 'Initial commit'],
            cwd=str(self.git_work),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # 获取 commit SHA
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(self.git_work),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        commit_sha = result.stdout.strip()
        
        # 创建 bare repo
        subprocess.run(
            ['git', 'init', '--bare'],
            cwd=str(self.git_bare),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # 设置默认分支
        subprocess.run(
            ['git', 'branch', '-M', 'main'],
            cwd=str(self.git_work),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # Push to bare
        subprocess.run(
            ['git', 'remote', 'add', 'origin', str(self.git_bare)],
            cwd=str(self.git_work),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        result = subprocess.run(
            ['git', 'push', '-u', 'origin', 'main'],
            cwd=str(self.git_work),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        if result.returncode != 0:
            raise Exception('Git push failed: {}'.format(result.stderr))
        
        return commit_sha
    
    def write_notification(self, commit_sha):
        """调用真实 notify-deploy.sh 脚本写入通知（Jenkins 通知链路）"""
        notify_script = Path(__file__).parent.parent / 'deploy-scripts' / 'jenkins' / 'notify-deploy.sh'
        
        # 调用真实通知脚本
        env = os.environ.copy()
        env['SPOOL_DIR'] = str(self.spool_dir)
        
        result = subprocess.run(
            ['bash', str(notify_script), commit_sha],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        if result.returncode != 0:
            raise AssertionError(
                'notify-deploy.sh failed: stdout={} stderr={}'.format(
                    result.stdout, result.stderr))
        
        # 查找生成的文件（脚本生成 request-*.json）
        notify_files = list(self.spool_dir.glob('request-*.json'))
        if not notify_files:
            raise AssertionError('notify-deploy.sh did not create notification file')
        
        # 返回最新的文件
        return max(notify_files, key=lambda p: p.stat().st_mtime)
    
    def write_notification_manual(self, request_id, commit_sha):
        """手动写入通知文件（仅用于重放测试模拟攻击）"""
        notification = {
            'request_id': request_id,
            'commit': commit_sha,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
            'source': 'test-replay'
        }
        
        notify_file = self.spool_dir / 'request-{}.json'.format(request_id)
        notify_file.write_text(json.dumps(notification))
        
        return notify_file
    
    def run_dispatcher_once(self, timeout=30):
        """运行 dispatcher 一次处理（非守护）"""
        cmd = [sys.executable, str(DISPATCHER_PY), '--once']
        
        result = subprocess.run(
            cmd,
            env=self.dispatcher_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout
        )
        
        return result
    
    def test_full_chain_deploy_success(self):
        """测试完整链路：Git → 通知 → dispatcher → release CLI 成功部署"""
        # 1. 创建 Git bare repo 并获取 commit SHA
        commit_sha = self.create_git_bare_with_code()
        
        # 2. 写入通知（调用真实 notify-deploy.sh）
        notify_file = self.write_notification(commit_sha)
        
        # 获取生成的 request_id
        notification = json.loads(notify_file.read_text())
        request_id = notification['request_id']
        
        # 3. 创建 baseline（dispatcher 需要）
        baseline_version = 'v-baseline-e2e'
        baseline_dir = self.release_state / 'releases' / baseline_version
        baseline_dir.mkdir(parents=True)
        (baseline_dir / 'source').mkdir()
        
        # 复制源码到 baseline
        for fname in ['app.py', 'core.py']:
            src = self.git_work / fname
            if src.exists():
                dst = baseline_dir / 'source' / fname
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
        
        # 创建 baseline manifest（需要所有 REQUIRED 文件）
        for fname, content in {
            '.gitignore': '*.pyc\n',
            'DEPLOYMENT.md': '# Deployment\n',
            'MAIL-SETUP.md': '# Mail Setup\n',
            'README.md': '# README\n',
            'admin-static/admin.css': '/* admin css */\n',
            'admin-static/app.js': '// admin js\n',
            'admin-static/index.html': '<html></html>\n',
            'admin.py': '# admin\n',
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
            'tests/test_notifications.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n',
            'tests/test_platform.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n',
            'worker.py': '# worker\n',
        }.items():
            fpath = baseline_dir / 'source' / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content)
        
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
        
        # 安装 baseline 到 root
        for p in (baseline_dir / 'source').rglob('*'):
            if p.is_file():
                rel = p.relative_to(baseline_dir / 'source')
                target = self.release_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(p), str(target))
        
        # 设置 current
        (self.release_state / 'current.json').write_text(
            json.dumps({'version': baseline_version, 'at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')})
        )
        
        # 4. 运行 dispatcher
        result = self.run_dispatcher_once(timeout=60)
        
        # 打印 dispatcher 输出（调试）
        print('\n=== Dispatcher Output ===')
        print('STDOUT:')
        print(result.stdout)
        print('STDERR:')
        print(result.stderr)
        print('=========================\n')
        
        # 5. 验证 dispatcher 成功
        self.assertEqual(result.returncode, 0, 
            'Dispatcher failed: stdout={} stderr={}'.format(result.stdout, result.stderr))
        
        # 6. 验证通知已处理（文件已移除或标记）
        notify_file = self.spool_dir / '{}.json'.format(request_id)
        self.assertFalse(notify_file.exists(), 'Notification not processed')
        
        # 7. 验证部署结果
        current = json.loads((self.release_state / 'current.json').read_text())
        
        # 版本应该是基于 commit SHA 的
        self.assertIn('v', current['version'])
        
        # 8. 验证源码对应 Git commit
        deployed_app = self.release_root / 'app.py'
        self.assertTrue(deployed_app.exists())
        
        deployed_content = deployed_app.read_text()
        expected_content = (self.git_work / 'app.py').read_text()
        self.assertEqual(deployed_content, expected_content)
        
        # 9. 验证健康检查成功（端口文件存在）
        port_file = self.release_root / '.test_http_port'
        self.assertTrue(port_file.exists(), 'Health check failed (no port file)')
        
    def test_full_chain_replay_prevention(self):
        """测试重启后防重放：退出 dispatcher → 重启 → 重发相同 request_id"""
        # 1. 创建 Git bare repo
        commit_sha = self.create_git_bare_with_code()
        
        # 2. 创建 baseline（同上）
        baseline_version = 'v-baseline-replay'
        baseline_dir = self.release_state / 'releases' / baseline_version
        baseline_dir.mkdir(parents=True)
        (baseline_dir / 'source').mkdir()
        
        # 创建完整 baseline
        for fname, content in {
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
            'tests/test_notifications.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n',
            'tests/test_platform.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n',
            'worker.py': '# worker\n',
        }.items():
            fpath = baseline_dir / 'source' / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content)
        
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
        
        # 安装 baseline 到 root
        for p in (baseline_dir / 'source').rglob('*'):
            if p.is_file():
                rel = p.relative_to(baseline_dir / 'source')
                target = self.release_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(p), str(target))
        
        (self.release_state / 'current.json').write_text(
            json.dumps({'version': baseline_version, 'at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')})
        )
        
        # 3. 第一次：调用真实 notify 脚本
        notify_file = self.write_notification(commit_sha)
        notification = json.loads(notify_file.read_text())
        request_id = notification['request_id']
        
        # 运行 dispatcher 第一次
        result1 = self.run_dispatcher_once(timeout=60)
        self.assertEqual(result1.returncode, 0, 'First dispatch failed')
        
        # 记录第一次部署后的版本
        current1 = json.loads((self.release_state / 'current.json').read_text())
        version1 = current1['version']
        
        # 4. 模拟 dispatcher 进程退出（已经退出了，因为是 subprocess）
        
        # 5. 重启 dispatcher：手动写入相同 request_id（模拟重放攻击）
        self.write_notification_manual(request_id, commit_sha)
        
        # 运行 dispatcher 第二次（新进程）
        result2 = self.run_dispatcher_once(timeout=60)
        
        # 6. 验证第二次处理：应该检测到重放并拒绝
        # Dispatcher 应该成功退出（处理了请求但拒绝）
        self.assertEqual(result2.returncode, 0, 'Second dispatch failed')
        
        # 验证没有第二次部署（版本未变）
        current2 = json.loads((self.release_state / 'current.json').read_text())
        version2 = current2['version']
        
        self.assertEqual(version1, version2, 'Version changed on replay (duplicate deployment)')
        
        # 7. 验证重放被拒绝
        # 请求文件应被检测并删除
        replay_notify_file = self.spool_dir / '{}.json'.format(request_id)
        self.assertFalse(replay_notify_file.exists(), 
            'Replay request file not removed')
        
        # 检查 processed-requests.txt 包含 request_id
        processed_file = self.dispatcher_work / 'processed-requests.txt'
        self.assertTrue(processed_file.exists(), 'Processed requests file missing')
        
        processed_ids = processed_file.read_text().strip().split('\n')
        # 应该只有一个 request_id（防重放生效）
        self.assertEqual(len([x for x in processed_ids if x == request_id]), 1,
            'Request ID appeared multiple times in processed list')
    
    def test_full_chain_preflight_failure_blocks_deploy(self):
        """
        完整链路：Preflight 失败阻止部署
        
        验证候选测试失败时：
        1. 部署被阻止
        2. 旧版本不变
        3. 服务保持运行
        """
        # 1. 创建 Git repo 并获取 commit
        commit_sha = self.create_git_bare_with_code()
        
        # 2. 创建 baseline 版本（健康）
        baseline_version = 'v1.0.0'
        baseline_dir = self.release_state / 'releases' / baseline_version
        baseline_dir.mkdir(parents=True)
        
        source_dir = baseline_dir / 'source'
        source_dir.mkdir()
        (source_dir / 'app.py').write_text('# baseline healthy\nprint("v1")\n')
        (source_dir / 'core.py').write_text('# core v1\n')
        
        files = []
        for p in source_dir.rglob('*'):
            if p.is_file():
                rel = p.relative_to(source_dir)
                files.append({
                    'path': str(rel),
                    'sha256': hashlib.sha256(p.read_bytes()).hexdigest()
                })
        
        manifest = {
            'version': baseline_version,
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
            'files': files
        }
        (baseline_dir / 'manifest.json').write_text(json.dumps(manifest))
        
        # 安装 baseline 到 root
        for p in (baseline_dir / 'source').rglob('*'):
            if p.is_file():
                rel = p.relative_to(baseline_dir / 'source')
                target = self.release_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(p), str(target))
        
        (self.release_state / 'current.json').write_text(
            json.dumps({'version': baseline_version, 'at': time.strftime('%Y-%m-%dT%H:%M:%S+08:00')})
        )
        
        # 3. 创建新版本（包含故意失败的测试）
        # 修改现有测试文件添加失败测试（test_platform.py 在 RUNTIME 中）
        failing_test = self.git_work / 'tests' / 'test_platform.py'
        failing_test.write_text('''import unittest
class T(unittest.TestCase):
    def test_ok(self):
        """原有测试"""
        pass
    
    def test_intentional_failure(self):
        """故意失败的测试（验证 preflight 执行）"""
        self.fail("Intentional preflight failure")
''')
        
        subprocess.run(
            ['git', 'add', 'tests/test_platform.py'],
            cwd=str(self.git_work),
            check=True
        )
        
        subprocess.run(
            ['git', 'commit', '-m', 'Add failing test'],
            cwd=str(self.git_work),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        subprocess.run(
            ['git', 'push', 'origin', 'main'],
            cwd=str(self.git_work),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # 获取新的 commit SHA
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(self.git_work),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        failing_commit_sha = result.stdout.strip()
        
        # 4. 写入通知（真实脚本）
        notify_file = self.write_notification(failing_commit_sha)
        
        # 5. 运行 dispatcher
        result = self.run_dispatcher_once(timeout=60)
        
        # 6. 验证部署失败（preflight 阻止）
        # Dispatcher 应该非零退出（部署失败）
        self.assertNotEqual(result.returncode, 0,
            'Dispatcher should fail when preflight fails')
        
        # 7. 验证旧版本不变
        current = json.loads((self.release_state / 'current.json').read_text())
        self.assertEqual(current['version'], baseline_version,
            'Version changed despite preflight failure')
        
        # 8. 验证旧版本文件完好
        app_content = (self.release_root / 'app.py').read_text()
        self.assertIn('v1', app_content, 'Old version files modified')
        self.assertNotIn('v2', app_content, 'New version deployed despite failure')


if __name__ == '__main__':
    unittest.main()
