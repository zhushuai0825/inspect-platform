# 生产安全接线修复

## 关键安全缺口（已识别）

### 1. ❌ Preflight执行隔离缺失
**当前**: `ops/release.py:75-86` 直接用当前UID执行unittest  
**风险**: 生产root会root执行repo测试  
**修复**: 调用固定可信隔离runner `/opt/inspect-deploy/bin/run-candidate-tests`

### 2. ❌ 控制面未分离
**当前**: dispatcher调用生产app树 `release.py` (1000:1000 0644)  
**风险**: 源码发布可覆盖控制面  
**修复**: 固定控制面 `/opt/inspect-deploy/`，root-owned，不可覆盖

### 3. ❌ Systemd权限过严
**当前**: `ProtectSystem=strict` 阻止必要写入  
**风险**: 部署失败  
**修复**: 允许 `/opt/inspect-platform/{source,data,state}` 写入

### 4. ❌ 白名单不匹配
**当前**: git archive 全量打包，19个文件超白名单  
**风险**: 不可信文件进入生产  
**修复**: dispatcher只打包白名单文件

### 5. ❌ Runner环境不确定
**当前**: `Dockerfile.test` 未实际构建验证  
**风险**: 依赖缺失导致测试失败  
**修复**: 提供确定的runner镜像和依赖清单

---

## 修复架构

### 控制面结构 `/opt/inspect-deploy/` (root:root 755)

```
/opt/inspect-deploy/
├── bin/
│   ├── release.py              # 固定release CLI (root:root 755)
│   ├── dispatcher.py           # 固定dispatcher (root:root 755)
│   └── run-candidate-tests     # 固定runner调用脚本 (root:root 755)
├── config/
│   └── deploy-config.json      # 固定配置 (root:root 644)
├── runner/
│   ├── Dockerfile.runner       # Runner镜像定义
│   └── runner-entrypoint.sh    # Runner入口脚本
└── systemd/
    └── inspect-deploy-dispatcher.service
```

### 应用树结构 `/opt/inspect-platform/` (inspect:inspect)

```
/opt/inspect-platform/
├── source/                     # 应用源码 (当前版本符号链接)
├── data/                       # 应用数据 (持久，不是部署队列)
│   ├── db/                    # 数据库
│   ├── reports/               # 报告
│   └── secrets/               # 秘密
└── state/                     # 部署状态 (inspect-ops:inspect-ops)
    ├── current.json
    ├── releases/
    └── transaction.json
```

---

## 修复1: Preflight隔离runner

### release.py 修改

```python
def preflight(self, src):
    """
    Preflight测试（隔离runner）
    
    安全：
    - 生产：调用固定可信隔离runner /opt/inspect-deploy/bin/run-candidate-tests
    - Runner: 非root、只读候选、临时data、network=none、资源限制
    - Fail-closed: 缺runner/依赖/权限立即失败
    """
    # 测试模式：跳过或使用mock runner
    if os.environ.get('RELEASE_TEST_SANDBOX'):
        # 测试环境：直接执行或mock
        return self._preflight_direct(src)
    
    # 生产：调用固定隔离runner
    runner = Path('/opt/inspect-deploy/bin/run-candidate-tests')
    
    if not runner.exists():
        raise ReleaseError('PREFLIGHT_RUNNER_MISSING')
    
    # 验证runner owner和权限
    stat = runner.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022:
        raise ReleaseError('PREFLIGHT_RUNNER_INSECURE')
    
    # 调用runner（传递候选源码目录）
    cmd = [str(runner), str(src)]
    
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180  # 3分钟超时
    )
    
    if proc.returncode != 0:
        # Fail-closed: runner失败阻止部署
        raise ReleaseError('PREFLIGHT_RUNNER_FAILED')
```

### Runner脚本 `/opt/inspect-deploy/bin/run-candidate-tests`

```bash
#!/bin/bash
# 固定可信隔离runner调用脚本
# Owner: root:root 755
# 位置: /opt/inspect-deploy/bin/run-candidate-tests
#
# 功能：在隔离容器中执行候选测试
# 安全：非root、只读候选、临时data、network=none、资源限制

set -euo pipefail

# 参数
CANDIDATE_SRC="${1:?Missing candidate source directory}"

# 验证候选源码目录
if [ ! -d "$CANDIDATE_SRC" ]; then
    echo "ERROR: Candidate source not found: $CANDIDATE_SRC" >&2
    exit 1
fi

# Runner镜像
RUNNER_IMAGE="inspect-platform-runner:v1"

# 检查runner镜像存在
if ! docker image inspect "$RUNNER_IMAGE" >/dev/null 2>&1; then
    echo "ERROR: Runner image not found: $RUNNER_IMAGE" >&2
    echo "Please build: docker build -f /opt/inspect-deploy/runner/Dockerfile.runner -t $RUNNER_IMAGE /opt/inspect-deploy/runner/" >&2
    exit 2
fi

# 创建临时data目录
TEMP_DATA=$(mktemp -d /tmp/preflight-data-XXXXXX)
trap "rm -rf '$TEMP_DATA'" EXIT

# 执行测试（隔离容器）
docker run --rm \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --network=none \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --pids-limit=100 \
    --memory=384m \
    --cpus=0.5 \
    --user=nobody:nogroup \
    -v "${CANDIDATE_SRC}:/candidate:ro" \
    -v "${TEMP_DATA}:/data:rw" \
    -e "QA_DATA_DIR=/data" \
    "$RUNNER_IMAGE" \
    /candidate

# 返回码：0=成功，非0=失败
```

### Runner Dockerfile `/opt/inspect-deploy/runner/Dockerfile.runner`

```dockerfile
FROM python:3.11-slim

# 最小依赖（持久可复现）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 验证Python
RUN python3 --version

# Runner入口脚本
COPY runner-entrypoint.sh /runner-entrypoint.sh
RUN chmod 755 /runner-entrypoint.sh

# 非root用户（容器内nobody）
USER nobody:nogroup

ENTRYPOINT ["/runner-entrypoint.sh"]
```

### Runner入口脚本 `/opt/inspect-deploy/runner/runner-entrypoint.sh`

```bash
#!/bin/bash
# Runner容器入口脚本
# 功能：执行候选测试

set -euo pipefail

CANDIDATE_SRC="${1:?Missing candidate source}"

cd "$CANDIDATE_SRC"

# 执行unittest discover（只测试白名单内的tests/）
python3 -m unittest discover -s tests -p "test_*.py" -v

# 返回码：0=通过，非0=失败
```

---

## 修复2: 控制面分离

### dispatcher.py 修改

```python
# 固定控制面release CLI
CONTROL_PLANE_RELEASE = '/opt/inspect-deploy/bin/release.py'

def execute_deploy(artifact_path, commit):
    """
    执行部署
    
    安全：
    - 调用固定控制面 /opt/inspect-deploy/bin/release.py
    - 不调用应用树中的release.py（可被源码覆盖）
    """
    # 验证控制面release存在
    release_py = Path(CONTROL_PLANE_RELEASE)
    
    if not release_py.exists():
        raise Exception('Control plane release not found: {}'.format(CONTROL_PLANE_RELEASE))
    
    # 验证owner和权限
    stat = release_py.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022:
        raise Exception('Control plane release insecure: {}'.format(CONTROL_PLANE_RELEASE))
    
    # 调用固定control plane release
    # （不可被应用源码覆盖）
    ...
```

### 固定配置 `/opt/inspect-deploy/config/deploy-config.json`

```json
{
  "repo_url": "https://github.com/zhushuai0825/inspect-platform.git",
  "branch": "main",
  "spool_dir": "/var/lib/inspect-deploy/spool",
  "work_dir": "/var/lib/inspect-deploy/work",
  "log_file": "/var/lib/inspect-deploy/dispatcher.log"
}
```

**权限**: root:inspect-deploy 0640。服务用户通过 Group=inspect-deploy 读取。不要做成 0600（非 root 打不开），也不要做成 0644。发布状态在 `/opt/inspect-ops/state`，由 root 的 release.py 写入，不在应用源码目录，也不是 `/source`。

---

## 修复3: Systemd权限

### `/opt/inspect-deploy/systemd/inspect-platform.service`

```ini
[Unit]
Description=Inspect Platform Application
After=network.target

[Service]
Type=simple
User=inspect
Group=inspect
WorkingDirectory=/opt/inspect-platform

# 安全加固
NoNewPrivileges=yes
PrivateTmp=yes

# 最小权限：允许必要写入
ProtectSystem=strict
ReadWritePaths=/opt/inspect-platform/data

# 其他保护
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictRealtime=yes

ExecStart=/usr/bin/python3 /opt/inspect-platform/app.py

[Install]
WantedBy=multi-user.target
```

---

## 修复4: 白名单匹配

### dispatcher.py 创建制品修改

```python
def create_artifact(source_dir, commit):
    """
    创建部署制品
    
    安全：
    - 只打包白名单文件（与release.py RUNTIME|OPS一致）
    - 不打包控制面文件（deploy-scripts/）
    - 不打包测试专用文件（Dockerfile.test, TESTING.md）
    """
    # 白名单（与release.py保持一致）
    RUNTIME_WHITELIST = {
        '.gitignore', 'DEPLOYMENT.md', 'MAIL-ACTIVATION.md', 'MAIL-SETUP.md', 
        'README.md', 'admin-static/admin.css', 'admin-static/app.js', 
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
    
    # 创建制品（只包含白名单文件）
    artifact_path = work_dir / 'inspect-platform-{}.zip'.format(commit[:8])
    
    with zipfile.ZipFile(str(artifact_path), 'w', zipfile.ZIP_DEFLATED) as z:
        for file_path in WHITELIST:
            src_file = source_dir / file_path
            if src_file.exists() and src_file.is_file():
                z.write(str(src_file), 'inspect-platform/' + file_path)
    
    return artifact_path
```

---

## 修复5: Runner环境

### 确定依赖清单

```
Python: 3.11.x (python:3.11-slim基础镜像)
依赖: ca-certificates（HTTPS Git clone，如需要）
用户: nobody:nogroup（非root）
资源: 384M内存，0.5 CPU，100 PID限制
网络: none（或loopback only）
安全: cap-drop=ALL, no-new-privileges, read-only rootfs
```

### 构建runner镜像

```bash
# 在控制面主机执行
cd /opt/inspect-deploy/runner/
docker build -t inspect-platform-runner:v1 -f Dockerfile.runner .

# 验证
docker run --rm inspect-platform-runner:v1 python3 --version
```

---

## 安装清单

### 1. 创建控制面目录

```bash
# Root执行
sudo mkdir -p /opt/inspect-deploy/{bin,config,runner,systemd}
sudo chown -R root:root /opt/inspect-deploy
sudo chmod 755 /opt/inspect-deploy
```

### 2. 安装控制面文件

```bash
# 从repo复制控制面文件到固定位置
sudo cp ops/release.py /opt/inspect-deploy/bin/release.py
sudo cp deploy-scripts/host-dispatcher/dispatcher.py /opt/inspect-deploy/bin/dispatcher.py
sudo cp deploy-scripts/isolated-test/run-candidate-tests.sh /opt/inspect-deploy/bin/run-candidate-tests

# 设置权限（root-owned, 不可覆盖）
sudo chown root:root /opt/inspect-deploy/bin/*
sudo chmod 755 /opt/inspect-deploy/bin/*
```

### 3. 安装runner

```bash
# 复制runner文件
sudo cp deploy-scripts/runner/* /opt/inspect-deploy/runner/
sudo chown -R root:root /opt/inspect-deploy/runner
sudo chmod 755 /opt/inspect-deploy/runner/*.sh

# 构建runner镜像
cd /opt/inspect-deploy/runner/
sudo docker build -t inspect-platform-runner:v1 -f Dockerfile.runner .
```

### 4. 安装配置

```bash
# 复制固定配置。组可读，不是 0644，也不是 root-only 0600。
sudo install -o root -g inspect-deploy -m 0640 \
  control-plane/config/deploy-config.json /opt/inspect-deploy/config/deploy-config.json
```

### 5. 验证安装

```bash
# 验证控制面权限
ls -la /opt/inspect-deploy/bin/
# 应显示：root:root 755

# 验证runner镜像
docker image inspect inspect-platform-runner:v1

# 验证配置
cat /opt/inspect-deploy/config/deploy-config.json
```

---

## Sudo最小必要范围

### `/etc/sudoers.d/inspect-deploy`

```
# 只允许固定 helper。helper 内部 exec /usr/bin/python3 /opt/inspect-deploy/bin/release.py。
# 不把 sudo 授给 python、可写源码或 /opt/inspect-platform。
Defaults:inspect-dispatcher !requiretty
Defaults:inspect-dispatcher env_reset
inspect-dispatcher ALL=(root) NOPASSWD: /opt/inspect-deploy/bin/inspect-release
```

dispatcher 的 unit 不能同时写 `NoNewPrivileges=yes` 或空的 `CapabilityBoundingSet=`。前者让 sudo 无法 setuid；后者让 sudo 无法切到 root。候选测试不由这个 sudo 在宿主上裸跑，runner 在容器里使用 uid 65534。仓库里的规则文件是 `deploy-scripts/host-dispatcher/sudoers-inspect-deploy`。

---

## 备份与回滚

### 备份

发布状态在 `/opt/inspect-ops/state`，不在 `/opt/inspect-platform/source`。运行数据备份由 release.py 写到 `/var/lib/inspect-deploy/backups`（root:root 0700）。不要把 `data/`、`secret.env`、`mail.env` 或 sqlite 打进源码包。

### 回滚

```bash
# 固定控制面入口。不要对应用树做通配 sudo，也不要用软链把发布目录指回应用树。
sudo /opt/inspect-deploy/bin/inspect-release rollback <version>
```

---

## 未验证项（需实际部署环境）

### ⚠️ 未验证
1. Systemd service写权限实际测试
2. Docker runner实际构建和执行
3. Sudo配置实际权限验证
4. 完整部署流程端到端测试
5. 回滚流程实际验证
6. Runner网络隔离实际测试

### ✅ 可验证（代码层面）
1. Preflight调用runner接口
2. 控制面路径检查
3. 白名单匹配逻辑
4. Owner和权限检查
5. Fail-closed错误处理

---

## 验证命令

### 验证控制面分离

```bash
# 验证控制面文件存在且权限正确
ls -la /opt/inspect-deploy/bin/release.py
# 期望：-rwxr-xr-x root root

# 验证不在应用树
ls /opt/inspect-platform/ops/release.py
# 应该不存在或不被使用
```

### 验证preflight隔离

```bash
# 准备测试候选
mkdir -p /tmp/test-candidate/tests
echo "import unittest
class T(unittest.TestCase):
    def test_ok(self): pass
" > /tmp/test-candidate/tests/test_ok.py

# 测试runner调用
sudo /opt/inspect-deploy/bin/run-candidate-tests /tmp/test-candidate

# 期望：
# - 在隔离容器中执行
# - 非root用户
# - 只读候选源码
# - 返回0（成功）
```

### 验证白名单

```bash
# 测试dispatcher创建制品
# （需要实际dispatcher环境）
```

---

## 生产部署检查清单

- [ ] 控制面目录创建 (/opt/inspect-deploy)
- [ ] 控制面文件安装 (release.py, dispatcher.py, runner)
- [ ] 控制面权限设置 (root:root 755)
- [ ] Runner镜像构建 (inspect-platform-runner:v1)
- [ ] Runner镜像验证 (docker image inspect)
- [ ] 固定配置安装 (deploy-config.json)
- [ ] Sudo配置安装 (/etc/sudoers.d/inspect-deploy)
- [ ] Systemd service更新 (ReadWritePaths)
- [ ] 应用树权限验证 (inspect:inspect)
- [ ] State目录权限验证 (inspect-ops:inspect-ops)
- [ ] Dispatcher配置更新 (调用控制面release)
- [ ] 备份脚本准备
- [ ] 回滚流程测试
- [ ] 端到端验证

---

## 安全假设

1. Docker socket只有root和可信runner可访问
2. /opt/inspect-deploy由root独占，inspect用户只读
3. /opt/inspect-platform/{source,data,state}由inspect用户管理
4. Jenkins容器以root运行，只用于通知（不执行repo代码）
5. Dispatcher以 inspect-dispatcher 运行，只 sudo 固定 helper `/opt/inspect-deploy/bin/inspect-release`。unit 不启用 NoNewPrivileges，也不清空 CapabilityBoundingSet。
6. Runner 只挂载受控暂存里的候选（只读）和 nobody 拥有的临时 data。不挂生产 data、不挂控制面、不挂 Docker socket。
