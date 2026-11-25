# 剩余接线缺项（精确范围）

## 已完成 ✅

### Preflight隔离runner（PR2已实现）
- ✅ ops/release.py preflight调用固定可信隔离runner
- ✅ Root生产模式固定runner路径 `/opt/inspect-deploy/bin/run-candidate-tests`
- ✅ 验证runner owner=root（root模式）
- ✅ 验证runner mode不可组写/他人写
- ✅ Fail-closed（缺失/不可信/失败均阻止）
- ✅ 真实subprocess调用
- ✅ 8项回归测试（5 passed, 3 skipped需root）
- ✅ 保留所有已有测试（117 tests: 112 passed, 5 skipped）

---

## 待完成（需后续PR）

### 1. ⚠️ Dispatcher固定调用控制面release

**当前状态**（`deploy-scripts/host-dispatcher/dispatcher.py:380-382`）:
```python
# 固定的 release.py 路径（测试环境可覆盖）
release_py = CONFIG.get('release_cli', '/opt/inspect-platform/ops/release.py')
```

**问题**:
- 默认路径指向应用树 `/opt/inspect-platform/ops/release.py`
- 应用树文件 (1000:1000 0644) 可被源码发布覆盖
- 不可信控制面

**需要修改** (`execute_deploy()` 函数):
```python
def execute_deploy(artifact_path, commit):
    """执行部署（安全：固定控制面release）"""
    
    # 确定release路径
    if os.geteuid() == 0 or CONFIG.get('is_test_mode') == False:
        # 生产：固定控制面，不可覆盖
        release_py = Path('/opt/inspect-deploy/bin/release.py')
    else:
        # 测试：允许env注入
        release_py = Path(CONFIG.get(
            'release_cli',
            '/opt/inspect-deploy/bin/release.py'
        ))
    
    # 验证release存在
    if not release_py.exists():
        raise Exception('Control plane release missing: {}'.format(release_py))
    
    # 验证release安全性（包括父目录）
    # 1. 检查文件本身
    stat_info = release_py.stat()
    if os.geteuid() == 0:
        if stat_info.st_uid != 0:
            raise Exception('Control plane release not root-owned')
    if stat_info.st_mode & 0o022:
        raise Exception('Control plane release world-writable')
    
    # 2. 检查父目录链（防止symlink逃逸）
    current = release_py.resolve()
    if current != release_py:
        # 路径包含symlink
        raise Exception('Control plane release contains symlink')
    
    # 3. 检查父目录owner和权限
    parent = release_py.parent
    parent_stat = parent.stat()
    if os.geteuid() == 0:
        if parent_stat.st_uid != 0:
            raise Exception('Control plane directory not root-owned')
    if parent_stat.st_mode & 0o022:
        raise Exception('Control plane directory world-writable')
    
    # 调用固定控制面release
    # （不可被应用源码覆盖）
    ...
```

**测试**（需添加 `tests/test_dispatcher_control_plane.py`）:
1. test_dispatcher_calls_control_plane_release
2. test_dispatcher_rejects_app_tree_release
3. test_dispatcher_verifies_release_owner
4. test_dispatcher_verifies_release_mode
5. test_dispatcher_rejects_symlink_release
6. test_dispatcher_verifies_parent_directory

---

### 2. ⚠️ Dispatcher打包白名单匹配

**当前状态** (`deploy-scripts/host-dispatcher/dispatcher.py:333-342`):
```python
def create_artifact(source_dir, commit):
    # 使用 git archive 全量打包
    cmd = [
        'git', 'archive',
        '--format=zip',
        '--prefix=inspect-platform/',
        'HEAD',
        '--output', str(artifact_path)
    ]
```

**问题**:
- 全量打包，包含19个超白名单文件
- 控制面文件（deploy-scripts/）进入包
- 测试文件（test_*.py）进入包
- 文档文件（*.md）进入包

**19个超白名单文件**:
1. ARCHITECTURE.md
2. DEPLOY-GUIDE.md
3. PRODUCTION-WIRING.md
4. REMAINING-WIRING-GAPS.md (本文件)
5. Dockerfile.test
6. TESTING.md
7. deploy-scripts/host-dispatcher/*
8. deploy-scripts/isolated-test/*
9. deploy-scripts/jenkins/*
10. deploy-scripts/jenkins-scripts/*
11. deploy-scripts/INSTALL.md
12. control-plane/*
13. tests/test_dispatcher_security.py
14. tests/test_e2e_full_chain.py
15. tests/test_e2e_local.py
16. tests/test_path_security.py
17. tests/test_preflight_runner.py
18. tests/fixtures/*
19. ops/test_release_cli.py, ops/test_release_cli_real.py

**release.py白名单**（`ops/release.py:7-8`）:
```python
RUNTIME=set(""".gitignore DEPLOYMENT.md MAIL-ACTIVATION.md MAIL-SETUP.md README.md admin-static/admin.css admin-static/app.js admin-static/index.html admin.py app.py core.py deploy/inspect-admin.service deploy/inspect-mail.service deploy/inspect-platform.service deploy/inspect-worker.service deploy/update_job.py inspect_run.sh jenkins/Jenkinsfile notifications.py static/app.js static/index.html static/styles.css tests/test_notifications.py tests/test_platform.py worker.py""".split())
OPS={'ops/RELEASE.md','ops/release.py','ops/test_release.py'}
```

**需要修改** (`create_artifact()` 函数):
```python
def create_artifact(source_dir, commit):
    """创建部署制品（只包含白名单文件）"""
    
    # 白名单（与release.py RUNTIME|OPS保持一致）
    RUNTIME_WHITELIST = {
        '.gitignore', 'DEPLOYMENT.md', 'MAIL-ACTIVATION.md', 
        'MAIL-SETUP.md', 'README.md',
        'admin-static/admin.css', 'admin-static/app.js', 
        'admin-static/index.html', 'admin.py', 'app.py', 'core.py',
        'deploy/inspect-admin.service', 'deploy/inspect-mail.service',
        'deploy/inspect-platform.service', 'deploy/inspect-worker.service',
        'deploy/update_job.py', 'inspect_run.sh', 'jenkins/Jenkinsfile',
        'notifications.py', 'static/app.js', 'static/index.html',
        'static/styles.css', 'tests/test_notifications.py', 
        'tests/test_platform.py', 'worker.py'
    }
    
    OPS_WHITELIST = {
        'ops/RELEASE.md', 'ops/release.py', 'ops/test_release.py'
    }
    
    WHITELIST = RUNTIME_WHITELIST | OPS_WHITELIST
    
    # 创建zip（只包含白名单文件）
    artifact_path = work_dir / 'inspect-platform-{}.zip'.format(commit[:8])
    
    with zipfile.ZipFile(str(artifact_path), 'w', zipfile.ZIP_DEFLATED) as z:
        for file_path in WHITELIST:
            src_file = source_dir / file_path
            if src_file.exists() and src_file.is_file():
                z.write(str(src_file), 'inspect-platform/' + file_path)
    
    return artifact_path
```

**测试**（需添加 `tests/test_dispatcher_whitelist.py`）:
1. test_dispatcher_packs_whitelist_only
2. test_dispatcher_excludes_control_plane
3. test_dispatcher_excludes_secrets
4. test_dispatcher_excludes_git
5. test_dispatcher_matches_release_whitelist
6. test_real_git_pack_prepare_success

---

### 3. ⚠️ Dispatcher Systemd权限

**当前状态**: 没有dispatcher service文件

**需要创建** (`deploy-scripts/host-dispatcher/inspect-deploy-dispatcher.service`):
```ini
[Unit]
Description=Inspect Deploy Dispatcher
After=network.target

[Service]
Type=simple
User=inspect-dispatcher
Group=inspect-dispatcher
WorkingDirectory=/var/lib/inspect-deploy

# 控制面访问（只读）
ReadOnlyPaths=/opt/inspect-deploy

# 应用访问（读写）
# Source: 部署时写入
# Data/deploy-spool: dispatcher读写
# State: release写入
ProtectSystem=strict
ReadWritePaths=/opt/inspect-platform/source
ReadWritePaths=/opt/inspect-platform/data/deploy-spool
ReadWritePaths=/opt/inspect-platform/state
ReadWritePaths=/var/lib/inspect-deploy

# 安全加固
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictRealtime=yes

# 能力限制（需要调用sudo release）
# 通过 /etc/sudoers.d/inspect-deploy 控制

ExecStart=/usr/bin/python3 /opt/inspect-deploy/bin/dispatcher.py

[Install]
WantedBy=multi-user.target
```

**测试**: 需要实际systemd环境，提供验证命令：
```bash
# 验证语法
sudo systemd-analyze verify /opt/inspect-deploy/systemd/inspect-deploy-dispatcher.service

# 检查权限
systemctl cat inspect-deploy-dispatcher.service | grep ReadWritePaths

# 测试启动（dry-run）
sudo systemctl daemon-reload
sudo systemctl status inspect-deploy-dispatcher.service
```

---

## 安装脚本更新

**control-plane/INSTALL-CONTROL-PLANE.sh** 需要添加:
```bash
# 安装dispatcher.py到控制面
cp ../deploy-scripts/host-dispatcher/dispatcher.py /opt/inspect-deploy/bin/
chown root:root /opt/inspect-deploy/bin/dispatcher.py
chmod 755 /opt/inspect-deploy/bin/dispatcher.py

# 安装systemd service
cp systemd/inspect-deploy-dispatcher.service /etc/systemd/system/
chown root:root /etc/systemd/system/inspect-deploy-dispatcher.service
chmod 644 /etc/systemd/system/inspect-deploy-dispatcher.service

# 验证安装
systemd-analyze verify /etc/systemd/system/inspect-deploy-dispatcher.service || echo "Warning: systemd-analyze failed"
```

---

## 测试统计（当前准确）

**PR2当前状态** (commit 7794e7e):
```
Platform Tests: 90 tests (86 passed, 4 skipped)
Ops Tests: 27 tests (27 passed)
Total: 117 tests (112 passed, 5 skipped, 0 failed)
```

**详细分组**:
- tests/: 90 tests
  - test_preflight_runner.py: 8 (5 passed, 3 skipped)
  - test_e2e_full_chain.py: 3 (3 passed)
  - test_e2e_local.py: 3 (3 passed)
  - test_path_security.py: 6 (5 passed, 1 skipped)
  - test_dispatcher_security.py: ~30 (30 passed)
  - test_notifications.py: ~20 (20 passed)
  - test_platform.py: ~20 (20 passed)
- ops/test_release.py: 17 (17 passed)
- ops/test_release_cli.py: 5 (4 passed, 1 skipped)
- ops/test_release_cli_real.py: 5 (5 passed)

**待添加测试**:
- test_dispatcher_control_plane.py: ~6 tests
- test_dispatcher_whitelist.py: ~6 tests

---

## 实际验证vs待验证

### ✅ 可在Cloud环境验证
1. ✅ Preflight调用runner（已验证，真实subprocess）
2. ✅ Runner fail-closed（已验证）
3. ✅ 完整链路Git→notify→dispatcher→release（已验证）
4. ⚠️ Dispatcher调用控制面release（代码未实现）
5. ⚠️ Dispatcher打包白名单（代码未实现）

### ⚠️ 需实际部署环境验证
1. ⚠️ Docker runner镜像构建
2. ⚠️ Docker runner实际执行
3. ⚠️ Root环境owner/mode检查
4. ⚠️ Systemd service权限
5. ⚠️ 生产部署流程
6. ⚠️ Sudo配置
7. ⚠️ 控制面安装

---

## 明确范围

### 本PR已完成
- ✅ 关键安全缺口#1: Preflight隔离runner（代码+测试）

### 本PR待完成（需后续commit）
- ⚠️ 关键安全缺口#2: Dispatcher控制面（代码+测试）
- ⚠️ 关键安全缺口#3: 白名单匹配（代码+测试）
- ⚠️ 关键安全缺口#4: Systemd权限（配置+验证）

### 需独立运维验证
- ⚠️ Docker runner环境
- ⚠️ 实际部署流程
- ⚠️ Root环境测试
- ⚠️ 生产权限配置

---

**策略**: 增量修复，每个缺口独立验证，不编造验收
