# 部署操作清单

**基于真实拓扑设计**（2026-11-25）

---

## 前提条件

- Jenkins 容器：`jenkins/jenkins:lts-jdk17`，root (uid=0)
- 挂载：Docker socket (RW), `/opt/inspect-platform` (RW)
- 宿主：Python 3.6.8, Node 20.20.2, systemd
- 资源：809MB 可用，测试容器 384MB 限额

---

## 操作步骤

### 步骤 1：准备固定脚本

#### 1.1 Jenkins 容器内脚本

```bash
# 在 Jenkins 容器内创建脚本目录
docker exec -u root jenkins bash -c 'mkdir -p /var/jenkins_home/scripts'

# 复制测试脚本到容器
docker cp jenkins/run-isolated-test.sh jenkins:/var/jenkins_home/scripts/
docker exec -u root jenkins chmod 755 /var/jenkins_home/scripts/run-isolated-test.sh

# 验证
docker exec jenkins ls -lh /var/jenkins_home/scripts/run-isolated-test.sh
# 应显示：-rwxr-xr-x 1 root root ... run-isolated-test.sh
```

#### 1.2 宿主机部署脚本

```bash
# 创建 ops 目录（如不存在）
sudo mkdir -p /opt/inspect-ops
sudo chmod 755 /opt/inspect-ops

# 复制部署脚本
sudo cp jenkins/deploy-wrapper.sh /opt/inspect-ops/
sudo chmod 755 /opt/inspect-ops/deploy-wrapper.sh

# 验证
ls -lh /opt/inspect-ops/deploy-wrapper.sh
# 应显示：-rwxr-xr-x 1 root root ... deploy-wrapper.sh
```

---

### 步骤 2：配置 sudoers（宿主机）

```bash
# 创建 sudoers 配置
# 注意：需要确定实际执行部署的用户（通过 docker exec 调用）
# 假设是宿主 root 用户（从 Jenkins 容器 docker exec）

sudo visudo -f /etc/sudoers.d/inspect-deploy

# 添加以下内容：
# 允许 root 执行 release.py（实际生产需更精细控制）
# root ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py prepare *
# root ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py deploy *
# root ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py status
# root ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py rollback *

# 或者，如通过特定用户执行：
# jenkins_host_user ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py *
```

**注意**：
- `/opt/inspect-platform/ops/release.py` 当前属于 1000:1000
- 需要评审 root 执行非 root 拥有的脚本的安全性
- 建议将 release.py 改为 root:root 且只读（644 或 755）

---

### 步骤 3：创建 Jenkins Job

**类型**：Freestyle project（不是 Pipeline）

**Job 配置**：

1. **General**
   - Project name: `inspect-platform-deploy`
   - Description: `巡检平台自动部署（仅 main 分支）`

2. **Source Code Management**
   - Git
   - Repository URL: `https://github.com/zhushuai0825/inspect-platform.git`
   - Credentials: 添加 GitHub 只读 token
   - Branches to build: `*/main`

3. **Build Triggers**
   - Poll SCM: `H/2 * * * *`（每 2 分钟）

4. **Build Environment**
   - ✓ Delete workspace before build starts
   - ✓ Abort the build if it's stuck
     - Timeout strategy: Absolute
     - Timeout minutes: 20

5. **Build Steps**

**Build Step 1**: Execute shell - 隔离测试
```bash
#!/bin/bash
set -euo pipefail

echo "=== 启动隔离测试 ==="
/var/jenkins_home/scripts/run-isolated-test.sh \
  --workspace "${WORKSPACE}" \
  --commit "${GIT_COMMIT}"
```

**Build Step 2**: Execute shell - 打包制品
```bash
#!/bin/bash
set -euo pipefail

echo "=== 打包制品 ==="
BUILD_DIR="${WORKSPACE}/build"
mkdir -p "${BUILD_DIR}"

ARTIFACT_NAME="inspect-platform-${GIT_COMMIT:0:8}.zip"

# 使用临时 Alpine 容器压缩（Jenkins 容器无 zip）
docker run --rm \
  -v "${WORKSPACE}:/src:ro" \
  -v "${BUILD_DIR}:/out:rw" \
  --network none \
  --memory 128m \
  alpine:3.19 \
  sh -c "
    apk add --no-cache zip > /dev/null 2>&1
    cd /src
    zip -r /out/${ARTIFACT_NAME} . \
      -x '.git/*' '*.pyc' '__pycache__/*' 'build/*' 'data/*' '*.db' '*.env'
  "

# 计算哈希
cd "${BUILD_DIR}"
sha256sum "${ARTIFACT_NAME}" > "${ARTIFACT_NAME}.sha256"
cat "${ARTIFACT_NAME}.sha256"
```

**Build Step 3**: Execute shell - 部署
```bash
#!/bin/bash
set -euo pipefail

echo "=== 部署到生产 ==="

# 注意：需要根据实际容器挂载情况调整路径
# Jenkins workspace 在容器内：/var/jenkins_home/workspace/inspect-platform-deploy
# 如宿主挂载了 jenkins_home，则宿主路径为：/var/lib/docker/volumes/jenkins_jenkins_home/_data/workspace/inspect-platform-deploy

ARTIFACT_PATH="${WORKSPACE}/build/inspect-platform-${GIT_COMMIT:0:8}.zip"

# 通过 Docker exec 到宿主执行部署
# 方式 1：直接在宿主执行（如果可以访问 artifact）
docker exec -i <宿主容器名或直接到宿主> \
  /opt/inspect-ops/deploy-wrapper.sh \
  --artifact "<宿主路径到 artifact>" \
  --commit "${GIT_COMMIT}" \
  --branch "${GIT_BRANCH}"

# 方式 2：或者复制 artifact 到宿主后执行
# docker cp "${ARTIFACT_PATH}" <宿主>:/tmp/
# docker exec -i <宿主> /opt/inspect-ops/deploy-wrapper.sh ...
```

**注意**：Build Step 3 需要根据实际容器拓扑调整：
- 确定宿主如何访问 Jenkins workspace 中的 artifact
- 可能需要先 docker cp 到宿主临时目录

6. **Post-build Actions**
   - Archive the artifacts: `build/*.zip, build/*.sha256`
   - 可选：失败时发送邮件通知

---

### 步骤 4：首次构建测试

1. Jenkins UI → `inspect-platform-deploy` → **Build Now**
2. 查看 Console Output

**预期输出**：
```
✓ 克隆 main 分支
✓ 启动隔离测试容器
  - Python 语法检查通过
  - JavaScript 语法检查通过
  - 50 应用测试通过
  - 17 运维测试通过
✓ 打包制品
  - inspect-platform-<commit>.zip
  - inspect-platform-<commit>.zip.sha256
✓ 部署到生产
  - 准备发布 v<date>-<commit>
  - 执行部署
  - 健康检查通过
✓ 构建成功
```

---

### 步骤 5：验证部署结果

```bash
# 查看部署状态
sudo python3 /opt/inspect-platform/ops/release.py status

# 查看审计日志
sudo cat /opt/inspect-ops/state/audit.jsonl | tail -5 | python3 -m json.tool

# 检查服务
sudo systemctl status inspect-platform inspect-admin inspect-worker inspect-mail

# 访问公开看板
curl http://8.146.206.64/qa/api/health
```

---

### 步骤 6：配置 GitHub Actions（PR 测试）

GitHub Actions workflow 已在 `.github/workflows/test.yml`

**无需额外配置**：
- PR 提交后自动触发
- 在 GitHub 托管 runner 运行（隔离）
- 通过后才允许合并

---

## 验收标准

- [ ] Jenkins 容器内脚本就位（`/var/jenkins_home/scripts/run-isolated-test.sh`）
- [ ] 宿主部署脚本就位（`/opt/inspect-ops/deploy-wrapper.sh`）
- [ ] sudoers 配置完成（允许执行 release.py）
- [ ] Jenkins Job `inspect-platform-deploy` 创建成功
- [ ] Git 轮询配置生效（2 分钟）
- [ ] 首次构建成功（测试 + 打包 + 部署）
- [ ] 服务健康（http://8.146.206.64/qa/）
- [ ] GitHub Actions PR 测试生效

---

## 安全边界

### ✅ 已实现

1. **控制器隔离**：不执行仓库代码（Freestyle Job 配置固定）
2. **测试隔离**：独立容器（无生产挂载、无 socket、384MB 限额）
3. **部署隔离**：固定宿主脚本（不受 repo 控制）
4. **PR 隔离**：GitHub Actions（完全独立环境）

### ❌ 已知限制

1. **无 PR 在 Jenkins 测试**（资源不足）
2. **串行部署**（单并发）
3. **短暂维护窗口**（20-40 秒）
4. **手动升级固定脚本**

---

## 故障排查

### 测试失败

```bash
# 查看容器日志
docker logs inspect-test-<commit>

# 本地复现
docker run --rm -v /path/to/repo:/src:ro python:3.6-slim \
  bash -c "cd /src && python3 -m unittest discover -s tests -v"
```

### 部署失败

```bash
# 查看状态
sudo python3 /opt/inspect-platform/ops/release.py status

# 查看审计日志
sudo cat /opt/inspect-ops/state/audit.jsonl | tail -10

# 手动回滚
sudo python3 /opt/inspect-platform/ops/release.py rollback <version>
```

---

## 后续维护

### 查看部署历史

```bash
# 当前版本
sudo python3 /opt/inspect-platform/ops/release.py status

# 可用版本
ls -lh /opt/inspect-ops/state/releases/
```

### 升级固定脚本

```bash
# Jenkins 容器内脚本
docker cp jenkins/run-isolated-test.sh jenkins:/var/jenkins_home/scripts/
docker exec jenkins chmod 755 /var/jenkins_home/scripts/run-isolated-test.sh

# 宿主部署脚本
sudo cp jenkins/deploy-wrapper.sh /opt/inspect-ops/
sudo chmod 755 /opt/inspect-ops/deploy-wrapper.sh
```

---

## 安全提醒

### ❌ 禁止

1. 在控制器执行仓库 Jenkinsfile
2. PR 代码访问生产挂载或 Docker socket
3. repo 脚本控制容器启动参数
4. 将生产凭证暴露给测试容器

### ✅ 建议

1. 定期审查固定脚本
2. 监控资源使用（测试容器 384MB 限额）
3. 定期测试回滚流程
4. 备份 `data/` 目录

---

## 完成标志

- [ ] 所有脚本就位并验证
- [ ] Jenkins Job 配置完成
- [ ] 首次构建成功
- [ ] 服务健康验证通过
- [ ] PR 测试方案验证（GitHub Actions）

**完成后回报**：
- Jenkins Job URL
- 首次部署版本号
- 任何配置调整或异常

---

## 文档日期

2026-11-25（基于真实拓扑设计）

## 参考

- 架构设计：`JENKINS-ARCHITECTURE.md`
- 仓库：https://github.com/zhushuai0825/inspect-platform
