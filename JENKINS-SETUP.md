# Jenkins CI/CD 配置指南

**⚠️ 重要安全说明**：本文档已针对真实 Docker 容器拓扑和安全隔离要求完全重写。

---

## 安全架构说明

### 现实约束

1. **Jenkins 在 Docker 容器内运行**
   - 容器挂载宿主 `/opt/inspect-platform`（生产代码和数据）
   - 容器内可能接触 `data/secret.env`、`data/mail.env` 等机密文件
   - 容器内 `sudo`/`systemctl` 与宿主不是同一回事

2. **公开仓库的 PR 可修改 Jenkinsfile**
   - PR 可执行任意代码（包括读取挂载的生产凭证）
   - `when` 条件不构成安全边界（可被 PR 删除或绕过）
   - **因此：PR 不能在生产 Jenkins 上构建**

3. **资源限制**
   - 服务器内存仅 1.8GB
   - 无法同时运行独立的隔离 CI 环境和生产 Jenkins

### 安全决策

**本方案采用的安全策略**：

✅ **仅 main 分支在生产 Jenkins 构建**
- Jenkinsfile 第一阶段明确拒绝所有非 main 分支
- 拒绝所有 Pull Request（无论来自 origin 还是 fork）

✅ **部署使用宿主固定脚本**
- 不执行 repo 中的 `ops/release.py`（PR 可修改）
- 调用宿主 `/opt/inspect-ops/deploy-from-jenkins.sh`（固定，不受 repo 影响）
- 该脚本验证来源后，使用宿主固定的 `/opt/inspect-ops/release.py`

✅ **PR 测试方案**（三选一）：

**方案 A**：手动本地测试（推荐，零成本）
```bash
# 开发者在本地运行测试
git checkout <pr-branch>
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py' -v
```

**方案 B**：GitHub Actions（免费，但需配置）
- 使用 GitHub Actions 在隔离环境测试 PR
- 无法访问生产服务器或凭证
- 配置文件：`.github/workflows/test.yml`（本轮未提供）

**方案 C**：Jenkins 独立 Agent（需额外配置）
- 配置无生产挂载的独立 Jenkins agent
- 仅运行测试，不执行部署
- 需要父代理配置（见"附录：独立 CI Agent 配置"）

---

## 核心流程

### main 分支更新（自动部署）

```
1. 推送到 main 分支（或合并 PR）
   ↓
2. Jenkins Git 轮询检测（2 分钟内）
   ↓
3. 安全检查：验证分支 == main && 非 PR
   ↓
4. 语法检查 + 自动化测试（67 项）
   ↓
5. 打包制品（.zip + .sha256）
   ↓
6. 制品验证（哈希、大小）
   ↓
7. 调用宿主部署脚本
   ↓ (固定脚本执行)
8. 验证来源 → 下载制品 → 调用固定 release.py
   ↓
9. 部署（停服、备份、安装、健康检查）
   ↓
10. 成功 ✓ 或 失败自动回滚 ↻
```

### PR 提交（禁止在生产 Jenkins）

```
1. 提交 PR
   ↓
2. Jenkins 轮询检测（如配置扫描 PR）
   ↓
3. 安全检查：分支 != main 或是 PR
   ↓
4. ❌ 立即失败，显示安全拒绝消息
   ↓
5. PR 作者需在本地运行测试
   或配置 GitHub Actions（方案 B）
```

---

## 配置步骤

### 步骤 1：准备宿主部署脚本

**⚠️ 必须先完成此步骤，否则部署阶段会失败**

```bash
# 1. 创建 ops 目录（如不存在）
sudo mkdir -p /opt/inspect-ops/state
sudo chmod 700 /opt/inspect-ops/state

# 2. 从当前生产代码复制固定的 release.py
sudo cp /opt/inspect-platform/ops/release.py /opt/inspect-ops/release.py
sudo chmod 755 /opt/inspect-ops/release.py

# 3. 创建部署脚本（从 repo 模板）
sudo cp /opt/inspect-platform/deploy/deploy-from-jenkins.sh /opt/inspect-ops/deploy-from-jenkins.sh
sudo chmod 755 /opt/inspect-ops/deploy-from-jenkins.sh

# 4. 验证脚本可执行
ls -lh /opt/inspect-ops/deploy-from-jenkins.sh
ls -lh /opt/inspect-ops/release.py
```

**重要**：
- `/opt/inspect-ops/release.py` 是固定版本，不会被 PR 修改
- 未来需要升级部署工具时，需人工评审后手动复制
- 不能让 Jenkins 自动替换这些固定脚本

### 步骤 2：配置 Jenkins 访问宿主脚本

**方案 A**：Docker 挂载（推荐）

如果 Jenkins 容器启动命令可修改：

```bash
# 在 docker run 添加挂载
docker run ... \
  -v /opt/inspect-platform:/opt/inspect-platform:rw \
  -v /opt/inspect-ops:/opt/inspect-ops:ro \  # 只读挂载
  ...
```

重启 Jenkins 容器后，容器内可访问 `/opt/inspect-ops/deploy-from-jenkins.sh`

**方案 B**：docker exec（备选）

如果无法修改容器挂载，修改 Jenkinsfile 部署阶段：

```groovy
sh '''
  # 从宿主执行部署脚本
  docker exec -i <jenkins-container-name> \
    /opt/inspect-ops/deploy-from-jenkins.sh \
    --version "${VERSION}" \
    --commit "${GIT_COMMIT}" \
    --branch "${BRANCH_NAME}" \
    --artifact "${WORKSPACE}/${BUILD_DIR}/${ARTIFACT_NAME}" \
    --artifact-sha256 "${WORKSPACE}/${BUILD_DIR}/${ARTIFACT_NAME}.sha256"
'''
```

需要将 `<jenkins-container-name>` 替换为实际容器名。

### 步骤 3：创建 Jenkins 任务

**重要**：仅配置 main 分支，不扫描 PR。

#### 方案 A：单分支 Pipeline（推荐，简单）

1. Jenkins 首页 → **New Item**
2. 名称：`inspect-platform-deploy`
3. 类型：**Pipeline**
4. 配置：

**Pipeline Definition**:
- **Definition**: Pipeline script from SCM
- **SCM**: Git
  - **Repository URL**: `https://github.com/zhushuai0825/inspect-platform.git`
  - **Credentials**: 添加 GitHub 只读凭证（PAT 或 SSH Key）
  - **Branch Specifier**: `*/main`（**仅 main 分支**）
- **Script Path**: `Jenkinsfile`

**Build Triggers**:
- ✓ **Poll SCM**
  - **Schedule**: `H/2 * * * *`（每 2 分钟轮询）

**保存**

#### 方案 B：Multibranch Pipeline（不推荐，会扫描 PR）

如使用 Multibranch，必须配置过滤：

**Branch Sources** → **Behaviors**:
- ✓ **Filter by name (with regular expression)**
  - **Regular expression**: `^main$`（仅匹配 main）
- ✗ **不要**勾选 "Discover pull requests"

这样 Jenkins 只扫描 main 分支，忽略所有 PR。

### 步骤 4：首次构建测试

1. 进入 Jenkins → `inspect-platform-deploy`
2. 点击 **Build Now**（如是 Multibranch，点击 **Scan Repository Now**）
3. 查看 **Console Output**

**预期阶段**：
```
✓ 安全检查 - 验证 main 分支
✓ 准备 - 清理工作空间
✓ 语法检查 - Python/JavaScript/Bash
✓ 自动化测试 - 67 项测试
✓ 打包制品 - 生成 .zip 和 .sha256
✓ 制品验证 - 哈希校验
✓ 部署到生产 - 调用宿主脚本
```

**如部署阶段失败**：
- 检查 `/opt/inspect-ops/deploy-from-jenkins.sh` 是否存在
- 检查 Jenkins 容器是否可访问该脚本（挂载或 docker exec）
- 检查 `/opt/inspect-ops/release.py` 是否存在

### 步骤 5：验证部署结果

```bash
# 查看部署状态
python3 /opt/inspect-ops/release.py status

# 查看审计日志
sudo cat /opt/inspect-ops/state/audit.jsonl | tail -5 | python3 -m json.tool

# 检查服务健康
sudo systemctl status inspect-platform inspect-admin inspect-worker inspect-mail
curl http://127.0.0.1:8765/qa/api/health

# 访问公开看板
curl -I http://8.146.206.64/qa/
```

---

## PR 测试方案详解

### 方案 A：本地测试（推荐）

**优势**：零成本，不需要额外配置

**开发者工作流**：

```bash
# 1. 创建 PR 分支
git checkout -b feature/new-check

# 2. 修改代码
vim core.py

# 3. 本地运行测试
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py' -v

# 4. 语法检查
python3 -m py_compile app.py core.py admin.py worker.py notifications.py
node --check static/app.js admin-static/app.js
bash -n inspect_run.sh

# 5. 测试通过后提交 PR
git push -u origin feature/new-check
# 在 GitHub 创建 PR → main
```

**代码审查者**：在批准 PR 前，要求提供测试通过的截图或日志。

### 方案 B：GitHub Actions（需配置）

**优势**：自动化，PR 自动触发测试

**配置步骤**：

1. 在 repo 创建 `.github/workflows/test.yml`：

```yaml
name: Test

on:
  pull_request:
    branches: [ main ]
  push:
    branches: [ main ]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.6'
      
      - name: Set up Node.js
        uses: actions/setup-node@v3
        with:
          node-version: '16'
      
      - name: Syntax check
        run: |
          python3 -m py_compile app.py core.py admin.py worker.py notifications.py
          node --check static/app.js admin-static/app.js
          bash -n inspect_run.sh
      
      - name: Run tests
        run: |
          PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py' -v
```

2. 推送到 main 分支
3. 此后所有 PR 自动触发测试

**注意**：GitHub Actions 在隔离环境运行，无法访问生产服务器。

### 方案 C：Jenkins 独立 Agent（高级）

**仅当需要在 Jenkins 测试 PR 时使用**

见"附录：独立 CI Agent 配置"。

---

## 安全边界确认

### ✅ 安全措施

1. **main 分支构建**
   - Jenkinsfile 第一阶段拒绝所有非 main 分支
   - 错误消息明确说明原因和替代方案

2. **固定部署脚本**
   - 宿主 `/opt/inspect-ops/deploy-from-jenkins.sh` 不受 repo 影响
   - 宿主 `/opt/inspect-ops/release.py` 不受 PR 修改

3. **来源验证**
   - 部署脚本验证 `--branch main`
   - 验证制品完整性（SHA256）

4. **审计日志**
   - 所有部署记录在 `/opt/inspect-ops/state/audit.jsonl`
   - 包含版本、提交、时间、结果

### ❌ 已知限制

1. **PR 无自动测试**
   - 生产 Jenkins 拒绝 PR 构建（安全要求）
   - 需要本地测试或配置 GitHub Actions

2. **短暂维护窗口**
   - 部署时停服 20-40 秒（非零停机）
   - 不支持蓝绿部署（资源限制）

3. **手动升级部署工具**
   - `/opt/inspect-ops/release.py` 需人工更新
   - 不能自动从 repo 同步（安全考虑）

---

## 维护操作

### 查看部署历史

```bash
# 当前版本
python3 /opt/inspect-ops/release.py status

# 审计日志（最近 10 条）
sudo cat /opt/inspect-ops/state/audit.jsonl | tail -10 | python3 -m json.tool

# 可用版本
ls -lh /opt/inspect-ops/state/releases/
```

### 手动回滚

```bash
# 查看可回滚的版本
python3 /opt/inspect-ops/release.py status

# 回滚到指定版本
python3 /opt/inspect-ops/release.py rollback v20261125-150833-07450bb
```

### 升级部署工具

**⚠️ 谨慎操作**：需人工评审新版 `ops/release.py`

```bash
# 1. 审查新版本变更
git diff <old-commit> <new-commit> ops/release.py

# 2. 在测试环境验证（如有）
cd /tmp
cp /opt/inspect-platform/ops/release.py ./release-new.py
python3 release-new.py --help

# 3. 更新固定副本
sudo cp /opt/inspect-platform/ops/release.py /opt/inspect-ops/release.py
sudo chmod 755 /opt/inspect-ops/release.py

# 4. 验证
python3 /opt/inspect-ops/release.py status
```

### 紧急部署（绕过 Jenkins）

**仅紧急情况使用**：

```bash
# 1. 从 main 分支创建制品
cd /tmp
git clone --depth 1 --branch main https://github.com/zhushuai0825/inspect-platform.git
cd inspect-platform
zip -r ../inspect-platform-emergency.zip .

# 2. 手动部署
python3 /opt/inspect-ops/release.py prepare ../inspect-platform-emergency.zip vEmergency
python3 /opt/inspect-ops/release.py deploy vEmergency
```

---

## 故障排查

### 问题 1：安全检查失败（非 main 分支）

**症状**：
```
🚨 安全拒绝：非 main 分支不允许在生产 Jenkins 构建
```

**原因**：这是预期行为，PR 不能在生产 Jenkins 构建。

**解决**：
- PR：在本地运行测试，或配置 GitHub Actions
- 其他分支：合并到 main 后自动部署

### 问题 2：部署脚本不存在

**症状**：
```
部署脚本未就绪：/opt/inspect-ops/deploy-from-jenkins.sh 不存在
```

**原因**：未完成步骤 1 的准备工作。

**解决**：
```bash
sudo mkdir -p /opt/inspect-ops
sudo cp /opt/inspect-platform/deploy/deploy-from-jenkins.sh /opt/inspect-ops/
sudo cp /opt/inspect-platform/ops/release.py /opt/inspect-ops/
sudo chmod 755 /opt/inspect-ops/*.sh /opt/inspect-ops/release.py
```

### 问题 3：Jenkins 容器无法访问宿主脚本

**症状**：
```
/opt/inspect-ops/deploy-from-jenkins.sh: No such file or directory
```

**原因**：容器未挂载 `/opt/inspect-ops`

**解决**：

**方案 A**：修改容器挂载（需重启容器）
```bash
docker stop <jenkins-container>
docker run ... -v /opt/inspect-ops:/opt/inspect-ops:ro ...
```

**方案 B**：使用 docker exec
修改 Jenkinsfile 部署阶段为：
```groovy
sh '''
  docker exec <jenkins-container-name> \
    /opt/inspect-ops/deploy-from-jenkins.sh ...
'''
```

### 问题 4：健康检查失败

**症状**：
```
部署失败 - 应已自动回滚到前一版本
```

**检查**：
```bash
# 查看服务状态
sudo systemctl status inspect-platform inspect-admin inspect-worker inspect-mail

# 查看日志
sudo journalctl -u inspect-platform -n 50

# 手动测试健康端点
curl http://127.0.0.1:8765/qa/api/health
```

**常见原因**：
- 代码语法错误（Python import 失败）
- 端口被占用
- 权限问题（data/ 目录）

**恢复**：
- 自动回滚应已恢复服务
- 修复代码后重新推送 main 分支

---

## 附录：独立 CI Agent 配置

**仅当需要在 Jenkins 测试 PR 时使用（可选）**

### 要求

- 独立机器或容器（无生产挂载）
- 无法访问生产服务器或凭证
- 仅运行测试，不执行部署

### 配置步骤

1. **准备 Agent 机器**

```bash
# 在独立机器上安装 Java
sudo apt-get update
sudo apt-get install -y openjdk-11-jre python3 nodejs
```

2. **在 Jenkins Master 添加 Node**

Jenkins → Manage Jenkins → Nodes → New Node
- **Name**: `ci-test-agent`
- **Type**: Permanent Agent
- **Labels**: `ci-test`
- **Remote root directory**: `/home/jenkins/agent`
- **Launch method**: Launch agent via SSH

3. **创建独立 CI Pipeline**

新建任务 `inspect-platform-test-pr`：

**Pipeline Script**:
```groovy
pipeline {
  agent { label 'ci-test' }  // 使用隔离 agent
  
  stages {
    stage('测试') {
      steps {
        checkout scm
        sh 'PYTHONPATH=. python3 -m unittest discover -s tests -p "test_*.py" -v'
      }
    }
  }
}
```

**Branch Sources** → **Behaviors**:
- ✓ Discover pull requests from origin
- ✓ Filter by name (exclude main)

4. **配置 Git 轮询**

**Scan Repository Triggers**:
- ✓ Periodically if not otherwise run: `5 minutes`

---

## 检查清单

### 初始配置

- [ ] 创建 `/opt/inspect-ops/deploy-from-jenkins.sh`
- [ ] 创建 `/opt/inspect-ops/release.py`（从当前生产复制）
- [ ] 配置 Jenkins 访问宿主脚本（挂载或 docker exec）
- [ ] 创建 Jenkins 任务（仅 main 分支）
- [ ] 配置 Git 轮询（2 分钟）
- [ ] 首次构建验证

### PR 测试方案（三选一）

- [ ] 方案 A：开发者本地测试（零成本）
- [ ] 方案 B：配置 GitHub Actions（需创建 workflow）
- [ ] 方案 C：配置 Jenkins 独立 Agent（需额外资源）

### 安全验证

- [ ] 非 main 分支构建被拒绝
- [ ] PR 构建被拒绝
- [ ] 部署使用宿主固定脚本（不执行 repo 代码）
- [ ] 审计日志记录部署事件

---

## 文档更新日期

2026-11-25（安全复核后完全重写）

## 联系与支持

- **仓库**: https://github.com/zhushuai0825/inspect-platform
- **服务器**: http://8.146.206.64/qa/
- **问题反馈**: 提交 GitHub Issue
