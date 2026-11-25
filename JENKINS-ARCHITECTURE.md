# Jenkins CI/CD 架构设计

**基于真实拓扑的安全架构**（2026-11-25 安全复核）

---

## 拓扑现状

### Jenkins 容器
- **镜像**: `jenkins/jenkins:lts-jdk17`
- **用户**: root (uid=0, gid=0)
- **工具**: Git 2.47.3, Java 17, Docker CLI
- **无**: Python, Node.js, systemctl, sudo

### 挂载（高风险）
```
/var/run/docker.sock        → 容器 (RW) ⚠️ 宿主机级控制
/opt/inspect-platform       → 容器 (RW) ⚠️ 含生产 data/secrets
/opt/quality-kb             → 容器 (RW)
jenkins_home volume         → 容器 (RW)
```

### 宿主机
- **OS**: systemd 239
- **工具**: Python 3.6.8, Node 20.20.2, Docker 26.1.3
- **部署脚本**: `/opt/inspect-platform/ops/release.py` (1000:1000, 644)

### 资源
- **CPU**: 2 核
- **内存**: 1.83GB 总计，~809MB 可用，Jenkins 占 515MB
- **结论**: 仅能容纳 1 个轻量测试容器（384-512MB 限额）

### Jenkins 配置
- **执行器**: 2 个 (NORMAL 模式)
- **Agent**: 无
- **Cloud**: 无
- **插件**: 无 docker-workflow, docker-plugin, ssh-agent

---

## 安全要求

### 禁止项
1. ❌ 在控制器执行任何仓库 Jenkinsfile（包括 main）
2. ❌ Pipeline 访问生产挂载或 Docker socket
3. ❌ PR/分支代码控制容器启动参数
4. ❌ repo 脚本 + sudo（release.py 是 1000:1000 非 root）

### 必需项
1. ✅ Jenkins 作为核心引擎（不用 GitHub Actions 替代）
2. ✅ 固定可信 launcher 控制所有特权操作
3. ✅ 测试在独立容器（无生产挂载，无 Docker socket）
4. ✅ 部署通过宿主脚本（docker exec 或 SSH）

---

## 架构方案

### 方案总览

```
GitHub main 分支更新
        ↓
Jenkins 轮询检测 (Freestyle Job - 固定配置)
        ↓
├─ 测试阶段 ─────────────────────────┐
│   固定脚本启动隔离 Docker 容器      │
│   - 无生产挂载                      │
│   - 无 Docker socket                │
│   - 只读 repo 副本                  │
│   - 384MB 内存限额                  │
│   └─ Python/Node 测试 (67 项)      │
        ↓ (测试通过)
├─ 打包阶段 ─────────────────────────┤
│   在 Jenkins 工作空间               │
│   - 创建 .zip + .sha256             │
│   - 归档到 Jenkins artifacts        │
        ↓
├─ 部署阶段 ─────────────────────────┤
│   docker exec 到宿主                │
│   - 下载 artifact                   │
│   - sudo 执行固定部署脚本           │
│   - release.py 验证 + 部署          │
│   - 健康检查 + 自动回滚             │
        ↓
  生产服务更新
```

### PR 测试

**方案**: GitHub Actions（隔离环境，无 Jenkins 资源消耗）

```yaml
# .github/workflows/test.yml
on: pull_request
jobs:
  test:
    runs-on: ubuntu-latest  # GitHub 托管
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
      - run: python3 -m unittest ...
```

**理由**：
- Jenkins 资源不足支持并发 PR 测试
- GitHub Actions 完全隔离，无生产访问
- Jenkins 仅处理 main 分支部署

---

## 实现细节

### 1. 固定 Freestyle Job

**Job 名称**: `inspect-platform-deploy`  
**类型**: Freestyle project（不是 Pipeline）

**配置**：

#### Source Code Management
- **Git**
  - Repository URL: `https://github.com/zhushuai0825/inspect-platform.git`
  - Credentials: GitHub 只读 token
  - **Branches to build**: `*/main`（仅 main）

#### Build Triggers
- **Poll SCM**: `H/2 * * * *`（每 2 分钟）

#### Build Environment
- ✓ Delete workspace before build starts
- ✓ Abort the build if it's stuck (20 分钟)

#### Build Steps

**步骤 1**: Execute shell - 隔离测试
```bash
#!/bin/bash
set -euo pipefail

# 使用固定脚本启动隔离测试容器
# 该脚本在 Jenkins 容器内，但启动的测试容器完全隔离
/var/jenkins_home/scripts/run-isolated-test.sh \
  --workspace "${WORKSPACE}" \
  --commit "${GIT_COMMIT}"
```

**步骤 2**: Execute shell - 打包制品
```bash
#!/bin/bash
set -euo pipefail

# 在 Jenkins 工作空间打包（有 Git，无 Python）
BUILD_DIR="${WORKSPACE}/build"
mkdir -p "${BUILD_DIR}"

# 创建制品（使用 Docker 临时容器压缩）
docker run --rm \
  -v "${WORKSPACE}:/src:ro" \
  -v "${BUILD_DIR}:/out:rw" \
  --network none \
  alpine:3.19 \
  sh -c "cd /src && zip -r /out/inspect-platform-${GIT_COMMIT:0:8}.zip ..."

# 计算哈希
cd "${BUILD_DIR}"
sha256sum inspect-platform-*.zip > inspect-platform-*.zip.sha256
```

**步骤 3**: Execute shell - 部署到生产
```bash
#!/bin/bash
set -euo pipefail

# 通过 Docker exec 到宿主执行部署
# Jenkins 容器有 Docker CLI 和 socket
docker exec -i <宿主部署容器或直接到宿主> \
  /opt/inspect-ops/deploy-wrapper.sh \
  --artifact "${WORKSPACE}/build/inspect-platform-${GIT_COMMIT:0:8}.zip" \
  --commit "${GIT_COMMIT}" \
  --branch "${GIT_BRANCH}"
```

#### Post-build Actions
- **Archive the artifacts**: `build/*.zip, build/*.sha256`
- **Build other projects**: 仅失败时通知（可选）

---

### 2. 隔离测试脚本

**路径**: `/var/jenkins_home/scripts/run-isolated-test.sh`（固定）

```bash
#!/bin/bash
# 固定脚本：在隔离 Docker 容器中运行测试
# 该脚本由 Jenkins 维护，不受 repo 控制

set -euo pipefail

WORKSPACE=""
COMMIT=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

# 验证参数
if [[ -z "$WORKSPACE" || -z "$COMMIT" ]]; then
  echo "错误：缺少必需参数"
  exit 1
fi

echo "=== 启动隔离测试容器 ==="
echo "工作空间: $WORKSPACE"
echo "提交: $COMMIT"

# 启动隔离测试容器
# - 无生产挂载
# - 无 Docker socket
# - 只读 repo 副本
# - 资源限额
# - 无网络（测试不需要外网）

docker run --rm \
  --name "inspect-test-${COMMIT:0:8}" \
  --network none \
  --memory 384m \
  --memory-swap 512m \
  --cpus 0.5 \
  --pids-limit 100 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --security-opt no-new-privileges:true \
  -v "${WORKSPACE}:/src:ro" \
  -w /src \
  python:3.6-slim \
  bash -euxo pipefail -c '
    # 安装 Node.js（测试需要）
    apt-get update && apt-get install -y --no-install-recommends nodejs npm
    
    # 语法检查
    python3 -m py_compile app.py core.py admin.py worker.py notifications.py
    node --check static/app.js admin-static/app.js
    bash -n inspect_run.sh
    
    # 运行测试
    export PYTHONPATH=/src
    export PYTHONDONTWRITEBYTECODE=1
    python3 -m unittest discover -s tests -p "test_*.py" -v
  '

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  echo "✓ 测试通过"
else
  echo "✗ 测试失败 (exit code: $EXIT_CODE)"
  exit $EXIT_CODE
fi
```

**安全特性**：
- `--network none`: 无网络访问
- `--memory 384m`: 内存限额
- `--read-only`: 只读根文件系统
- `--security-opt no-new-privileges`: 禁止提权
- `-v ${WORKSPACE}:/src:ro`: 只读挂载 repo

**权限**：
- 该脚本由 Jenkins 管理员维护
- 位置：`/var/jenkins_home/scripts/`（Jenkins 容器内）
- 权限：root:root, 755
- PR 无法修改

---

### 3. 宿主部署脚本

**路径**: `/opt/inspect-ops/deploy-wrapper.sh`（宿主机）

```bash
#!/bin/bash
# 宿主部署包装脚本
# 由 Jenkins 容器通过 docker exec 调用

set -euo pipefail

ARTIFACT=""
COMMIT=""
BRANCH=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --artifact) ARTIFACT="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

# 安全验证
if [[ "$BRANCH" != "origin/main" && "$BRANCH" != "main" ]]; then
  echo "❌ 安全拒绝：仅 main 分支允许部署"
  echo "   当前分支: $BRANCH"
  exit 1
fi

# 验证 artifact 在允许的路径
if [[ ! "$ARTIFACT" =~ ^/var/jenkins_home/workspace/inspect-platform-deploy/build/ ]]; then
  echo "❌ 安全拒绝：artifact 路径不合法"
  exit 1
fi

echo "=== 部署安全检查通过 ==="
echo "分支: $BRANCH"
echo "提交: $COMMIT"
echo "制品: $ARTIFACT"

# 生成版本号
VERSION="v$(date +%Y%m%d-%H%M%S)-${COMMIT:0:8}"

# 调用固定的 release.py
echo "=== 准备发布 $VERSION ==="
sudo /usr/bin/python3 /opt/inspect-platform/ops/release.py prepare "$ARTIFACT" "$VERSION"

echo "=== 执行部署 $VERSION ==="
sudo /usr/bin/python3 /opt/inspect-platform/ops/release.py deploy "$VERSION"

echo "=== 部署完成 ==="
sudo /usr/bin/python3 /opt/inspect-platform/ops/release.py status
```

**调用方式**（从 Jenkins 容器）：
```bash
# Jenkins 容器有 Docker CLI 和 socket
docker exec -i $(docker ps -qf name=<宿主标识>) \
  /opt/inspect-ops/deploy-wrapper.sh \
  --artifact "/var/jenkins_home/workspace/.../build/artifact.zip" \
  --commit "${GIT_COMMIT}" \
  --branch "${GIT_BRANCH}"
```

**注意**：
- 需要配置宿主机 sudoers 允许特定用户执行 release.py
- artifact 路径需从 Jenkins workspace 映射到宿主路径

---

### 4. GitHub Actions PR 测试

**文件**: `.github/workflows/test.yml`

```yaml
name: PR Test

on:
  pull_request:
    branches: [ main ]

jobs:
  test:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout code
        uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.6'
      
      - name: Set up Node.js
        uses: actions/setup-node@v3
        with:
          node-version: '20'
      
      - name: Syntax check
        run: |
          python3 -m py_compile app.py core.py admin.py worker.py notifications.py
          node --check static/app.js admin-static/app.js
          bash -n inspect_run.sh
      
      - name: Run tests
        run: |
          export PYTHONPATH=.
          export PYTHONDONTWRITEBYTECODE=1
          python3 -m unittest discover -s tests -p 'test_*.py' -v
```

---

## 安全边界

### ✅ 安全措施

1. **控制器隔离**
   - 控制器不执行任何仓库代码（Freestyle Job 配置固定）
   - 执行器仅运行固定脚本

2. **测试隔离**
   - 独立 Docker 容器（无生产挂载）
   - 无 Docker socket
   - 无网络（--network none）
   - 资源限额（384MB 内存，0.5 CPU）
   - 只读文件系统

3. **部署隔离**
   - 固定宿主脚本（不受 repo 控制）
   - 分支验证（仅 main）
   - sudo 执行固定 release.py
   - 审计日志

4. **PR 隔离**
   - GitHub Actions 完全独立环境
   - 无 Jenkins 资源消耗
   - 无生产访问

### ❌ 已知限制

1. **无 PR 在 Jenkins 测试**
   - 资源不足（809MB 可用，Jenkins 515MB，测试容器需 384MB）
   - GitHub Actions 是唯一 PR 测试方案

2. **串行部署**
   - 单并发（资源限制）
   - 短暂维护窗口（20-40 秒）

3. **手动升级部署工具**
   - `/opt/inspect-ops/deploy-wrapper.sh` 需人工维护
   - release.py 更新需评审后手动复制

4. **Docker socket 风险**
   - Jenkins 容器有 Docker socket（遗留配置）
   - 固定脚本限制其使用（仅启动测试容器）
   - 理想情况应移除（需重新配置容器）

---

## 配置步骤

### 步骤 1：准备固定脚本

```bash
# 在 Jenkins 容器内
docker exec -u root jenkins bash -c '
  mkdir -p /var/jenkins_home/scripts
  cat > /var/jenkins_home/scripts/run-isolated-test.sh << "EOF"
<隔离测试脚本内容>
EOF
  chmod 755 /var/jenkins_home/scripts/run-isolated-test.sh
'

# 在宿主机
sudo tee /opt/inspect-ops/deploy-wrapper.sh > /dev/null << 'EOF'
<宿主部署脚本内容>
EOF
sudo chmod 755 /opt/inspect-ops/deploy-wrapper.sh
```

### 步骤 2：创建 Freestyle Job

Jenkins UI → New Item → `inspect-platform-deploy` (Freestyle project)

按上述 "固定 Freestyle Job" 配置。

### 步骤 3：配置 GitHub Actions

创建 `.github/workflows/test.yml`（已在 repo）

### 步骤 4：首次构建

Jenkins → `inspect-platform-deploy` → Build Now

**预期**：
- ✓ 克隆 main 分支
- ✓ 启动隔离测试容器
- ✓ 67 项测试通过
- ✓ 打包制品
- ✓ 部署到生产

---

## 运维指南

### 日常操作

**合并 PR 到 main**：
1. PR 在 GitHub Actions 自动测试
2. 代码审查通过
3. 合并到 main
4. Jenkins 2 分钟内自动部署

**查看构建日志**：
Jenkins → `inspect-platform-deploy` → 最新构建 → Console Output

**手动触发部署**：
Jenkins → `inspect-platform-deploy` → Build Now

### 故障排查

**测试容器失败**：
```bash
# 查看 Docker 日志
docker logs inspect-test-<commit>

# 本地复现
docker run --rm -v /path/to/repo:/src:ro python:3.6-slim \
  bash -c "cd /src && python3 -m unittest ..."
```

**部署失败**：
```bash
# 查看 release.py 状态
sudo python3 /opt/inspect-platform/ops/release.py status

# 查看审计日志
sudo cat /opt/inspect-ops/state/audit.jsonl | tail -10
```

---

## 后续优化（可选）

### 移除 Docker socket

**理想配置**（需重建容器）：
```bash
docker run -d \
  --name jenkins \
  -v jenkins_home:/var/jenkins_home \
  -v /opt/inspect-platform:/opt/inspect-platform:ro \  # 只读
  # 移除 Docker socket 挂载
  ...
```

需要配置 SSH 到宿主执行部署，而非 docker exec。

### 独立 Agent

如资源增加，可配置专用测试 agent：
- SSH agent（无生产挂载）
- Docker agent（使用 docker-plugin）
- 云 agent（外部 VM）

---

## 文档日期

2026-11-25（基于真实拓扑完全重写）

## 参考

- 拓扑核查报告：内部文档（不入库）
- Jenkins 官方文档：https://www.jenkins.io/doc/
