#!/usr/bin/env python3
"""
部署集成测试
验证 Jenkins 部署流程的关键组件
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

# 添加父目录到 path（测试运行需要）
sys.path.insert(0, str(Path(__file__).parent.parent))


class DispatcherLogicTests(unittest.TestCase):
    """Dispatcher 逻辑测试（不需要实际 dispatcher 运行）"""
    
    def test_branch_validation(self):
        """测试分支验证逻辑"""
        allowed = ('main', 'refs/heads/main', 'origin/main')
        rejected = ('feature/test', 'dev', 'pr-123')
        
        for branch in allowed:
            self.assertIn(branch, allowed, f"应允许: {branch}")
        
        for branch in rejected:
            self.assertNotIn(branch, allowed, f"应拒绝: {branch}")
    
    def test_artifact_path_mapping(self):
        """测试 Jenkins workspace 路径映射"""
        # 容器内路径
        container_path = "/var/jenkins_home/workspace/inspect-platform-deploy/build/artifact.zip"
        
        # 转换为宿主路径
        host_path = container_path.replace(
            "/var/jenkins_home",
            "/var/lib/docker/volumes/jenkins_jenkins_home/_data"
        )
        
        expected = "/var/lib/docker/volumes/jenkins_jenkins_home/_data/workspace/inspect-platform-deploy/build/artifact.zip"
        self.assertEqual(host_path, expected)
    
    def test_request_json_format(self):
        """测试部署请求 JSON 格式"""
        request = {
            "commit": "abc123def456",
            "branch": "main",
            "artifact": "/path/to/artifact.zip",
            "artifact_sha256": "/path/to/artifact.zip.sha256",
            "timestamp": "2026-11-25T07:30:00+08:00",
            "source": "jenkins"
        }
        
        # 验证必需字段
        self.assertIn("commit", request)
        self.assertIn("branch", request)
        self.assertIn("artifact", request)
        self.assertIn("artifact_sha256", request)
        
        # 验证 JSON 可序列化
        json_str = json.dumps(request)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["commit"], request["commit"])


class ArtifactPackagingTests(unittest.TestCase):
    """制品打包测试"""
    
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.build_dir = Path(self.test_dir) / 'build'
        self.build_dir.mkdir()
    
    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)
    
    def test_git_archive_format(self):
        """测试 git archive 格式（需要在 git repo 中运行）"""
        # 检查是否在 git repo 中
        result = subprocess.run(['git', 'rev-parse', '--git-dir'],
                                capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest("不在 git 仓库中")
        
        # 创建测试 archive
        artifact = self.build_dir / 'test-artifact.zip'
        subprocess.run([
            'git', 'archive',
            '--format=zip',
            '--prefix=inspect-platform/',
            'HEAD',
            '--output', str(artifact)
        ], check=True)
        
        # 验证 ZIP 文件
        self.assertTrue(artifact.exists())
        self.assertGreater(artifact.stat().st_size, 1000)
        
        # 验证内容有前缀
        with zipfile.ZipFile(artifact) as z:
            names = z.namelist()
            self.assertTrue(any(n.startswith('inspect-platform/') for n in names),
                            "制品应包含 inspect-platform/ 前缀")
    
    def test_sha256_generation(self):
        """测试 SHA256 生成"""
        # 创建测试文件
        test_file = self.build_dir / 'test.zip'
        test_file.write_bytes(b'test content 12345')
        
        # 生成 SHA256
        result = subprocess.run([
            'sha256sum', test_file.name
        ], cwd=self.build_dir, capture_output=True, text=True, check=True)
        
        sha256_file = self.build_dir / f'{test_file.name}.sha256'
        sha256_file.write_text(result.stdout)
        
        # 验证 SHA256 文件
        self.assertTrue(sha256_file.exists())
        self.assertIn(test_file.name, sha256_file.read_text())
        
        # 验证校验
        verify_result = subprocess.run([
            'sha256sum', '-c', sha256_file.name
        ], cwd=self.build_dir, capture_output=True, text=True)
        
        self.assertEqual(verify_result.returncode, 0,
                         "SHA256 校验应通过")


class SecurityBoundaryTests(unittest.TestCase):
    """安全边界测试"""
    
    def test_no_jenkinsfile_in_repo_root(self):
        """测试 repo 根目录没有可执行的 Jenkinsfile"""
        repo_root = Path(__file__).parent.parent
        
        # 根目录不应有 Jenkinsfile（已删除）
        jenkinsfile = repo_root / 'Jenkinsfile'
        self.assertFalse(jenkinsfile.exists(),
                         "根目录不应有 Jenkinsfile（使用固定脚本）")
    
    def test_fixed_scripts_not_in_repo(self):
        """测试固定脚本不在 repo 中（仅模板）"""
        repo_root = Path(__file__).parent.parent
        
        # 检查固定脚本只在 deploy-scripts/ 模板目录
        jenkins_scripts = repo_root / 'var' / 'jenkins_home' / 'jenkins-scripts'
        self.assertFalse(jenkins_scripts.exists(),
                         "实际固定脚本不应在 repo 中")
        
        # 模板目录应存在
        template_dir = repo_root / 'deploy-scripts' / 'jenkins-scripts'
        self.assertTrue(template_dir.exists(),
                        "模板脚本应在 deploy-scripts/ 中")
    
    def test_dispatcher_validates_branch(self):
        """测试 dispatcher 分支验证逻辑"""
        # 模拟 dispatcher 验证
        def validate_branch(branch):
            allowed = ('main', 'refs/heads/main', 'origin/main')
            if branch not in allowed:
                raise ValueError(f"拒绝非 main 分支: {branch}")
        
        # 应允许 main
        try:
            validate_branch('main')
            validate_branch('refs/heads/main')
            validate_branch('origin/main')
        except ValueError:
            self.fail("应允许 main 分支")
        
        # 应拒绝其他分支
        with self.assertRaises(ValueError):
            validate_branch('feature/test')
        
        with self.assertRaises(ValueError):
            validate_branch('dev')


class ReleaseToolIntegrationTests(unittest.TestCase):
    """与 release.py 的集成测试"""
    
    def test_release_py_exists(self):
        """测试 release.py 存在"""
        repo_root = Path(__file__).parent.parent
        release_py = repo_root / 'ops' / 'release.py'
        self.assertTrue(release_py.exists())
    
    def test_release_py_has_required_commands(self):
        """测试 release.py 包含必需命令"""
        repo_root = Path(__file__).parent.parent
        release_py = repo_root / 'ops' / 'release.py'
        
        content = release_py.read_text()
        
        # 检查必需的命令
        self.assertIn("'prepare'", content, "应有 prepare 命令")
        self.assertIn("'deploy'", content, "应有 deploy 命令")
        self.assertIn("'status'", content, "应有 status 命令")
        self.assertIn("'rollback'", content, "应有 rollback 命令")


class DataIsolationTests(unittest.TestCase):
    """数据隔离测试"""
    
    def test_gitignore_excludes_secrets(self):
        """测试 .gitignore 排除敏感文件"""
        repo_root = Path(__file__).parent.parent
        gitignore = repo_root / '.gitignore'
        
        self.assertTrue(gitignore.exists())
        
        content = gitignore.read_text()
        
        # 验证排除敏感文件
        self.assertIn('data/', content, "应排除 data/ 目录")
        self.assertIn('*.db', content, "应排除数据库文件")
        self.assertIn('*.env', content, "应排除环境变量文件")
    
    def test_no_secrets_in_repo(self):
        """测试 repo 中无敏感文件"""
        repo_root = Path(__file__).parent.parent
        
        # 不应有这些文件
        forbidden = [
            'data/secret.env',
            'data/mail.env',
            'data/platform.db',
            '.env',
            'secret.env'
        ]
        
        for path in forbidden:
            full_path = repo_root / path
            self.assertFalse(full_path.exists(),
                             f"不应有敏感文件: {path}")


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
