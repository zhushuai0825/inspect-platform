# 服务器部署操作清单

**⚠️ 安全更新**：本清单已针对 Docker 容器拓扑和安全隔离要求完全重写。

**目标**：在服务器完成 Jenkins CI/CD 配置，实现 main 分支自动测试与部署。

**重要**：PR 不在生产 Jenkins 构建（安全要求），需配置独立测试方案。

---

## 操作清单

### ✅ 步骤 1：准备宿主部署脚本（必需）

**这是安全要求的核心**：不能执行 repo 中的代码（PR 可修改），必须使用宿主固定脚本。

```bash
# 1. 创建 ops 目录
sudo mkdir -p /opt/inspect-ops/state
sudo chmod 700 /opt/inspect-ops/state

# 2. 从当前生产代码复制固定的 release.py
# 注意：这是固定版本，未来需要升级时人工评审后手动复制
sudo cp /opt/inspect-platform/ops/release.py /opt/inspect-ops/release.py
sudo chmod 755 /opt/inspect-ops/release.py

# 3. 创建部署脚本
sudo cp /opt/inspect-platform/deploy/deploy-from-jenkins.sh /opt/inspect-ops/deploy-from-jenkins.sh
sudo chmod 755 /opt/inspect-ops/deploy-from-jenkins.sh

# 4. 验证
ls -lh /opt/inspect-ops/deploy-from-jenkins.sh
ls -lh /opt/inspect-ops/release.py

# 预期输出：
# -rwxr-xr-x 1 root root ... /opt/inspect-ops/deploy-from-jenkins.sh
# -rwxr-xr-x 1 root root ... /opt/inspect-ops/release.py
```

**重要说明**：
- `/opt/inspect-ops/release.py` 是固定版本，不会被 PR 修改
- Jenkins 容器不能修改 `/opt/inspect-ops/`（只读挂载或无挂载）
- 未来需要升级部署工具时，需人工评审后手动复制

---

### ✅ 步骤 2：配置 Jenkins 访问宿主脚本

**方案 A：Docker 挂载（推荐）**

如果可以修改 Jenkins 容器启动命令：

```bash
# 1. 停止 Jenkins 容器
docker stop <jenkins-container-name>

# 2. 修改启动命令，添加只读挂载
docker run -d \
  --name <jenkins-container-name> \
  -v /opt/inspect-platform:/opt/inspect-platform:rw \
  -v /opt/inspect-ops:/opt/inspect-ops:ro \
  ... \
  jenkins/jenkins:lts

# 3. 验证挂载
docker exec <jenkins-container-name> ls -lh /opt/inspect-ops/
# 应显示 deploy-from-jenkins.sh 和 release.py
```

**方案 B：docker exec（如无法修改挂载）**

如果 Jenkins 容器无法添加挂载，需修改 Jenkinsfile 部署阶段。

**注意**：此方案需要手动修改 Jenkinsfile，本文档不自动完成。

---

### ✅ 步骤 3：创建 Git 凭证

在 Jenkins 中创建 GitHub 只读凭证：

1. 生成 GitHub Personal Access Token (PAT)
   - 访问：https://github.com/settings/tokens
   - Scopes: 勾选 `repo`（或仅 `public_repo` 若仓库公开）
   - 复制生成的 token

2. 在 Jenkins 添加凭证
   - Jenkins → Manage Jenkins → Credentials → System → Global credentials
   - 类型：**Username with password**
   - Username: `zhushuai0825`
   - Password: `<上面生成的 PAT>`
   - ID: `github-inspect-platform`
   - Description: `GitHub inspect-platform 只读`

---

### ✅ 步骤 4：创建 Jenkins Pipeline 任务

**重要**：仅配置 main 分支，不扫描 PR。

#### 推荐：单分支 Pipeline

1. Jenkins 首页 → **New Item**
2. 名称：`inspect-platform-deploy`
3. 类型：**Pipeline**
4. 配置：

**Pipeline Definition**:
- **Definition**: Pipeline script from SCM
- **SCM**: Git
  - **Repository URL**: `https://github.com/zhushuai0825/inspect-platform.git`
  - **Credentials**: 选择 `github-inspect-platform`
  - **Branch Specifier**: `*/main`（**仅 main 分支**）
- **Script Path**: `Jenkinsfile`

**Build Triggers**:
- ✓ **Poll SCM**
  - **Schedule**: `H/2 * * * *`（每 2 分钟轮询）

5. 点击 **Save**

---

### ✅ 步骤 5：首次构建测试

1. 进入 Jenkins → `inspect-platform-deploy`
2. 点击 **Build Now**
3. 点击构建号（如 #1）→ **Console Output**

**预期输出（成功）**：
```
✓ 安全检查通过：main 分支构建
✓ 准备
✓ 语法检查
✓ 自动化测试 - Ran 50 tests ... OK
✓ 自动化测试 - Ran 17 tests ... OK
✓ 打包制品
✓ 制品验证
✓ 部署到生产
✓ 部署成功: v20261125-HHMMSS-<commit>
```

**如果失败**：

**错误 1**：`部署脚本未就绪`
- 返回步骤 1，检查 `/opt/inspect-ops/deploy-from-jenkins.sh` 是否存在

**错误 2**：`No such file or directory: /opt/inspect-ops/deploy-from-jenkins.sh`
- 返回步骤 2，检查 Jenkins 容器是否可访问宿主脚本

**错误 3**：`健康检查失败`
- 查看服务日志：`sudo journalctl -u inspect-platform -n 50`
- 自动回滚应已恢复服务

---

### ✅ 步骤 6：验证部署结果

```bash
# 1. 查看部署状态
python3 /opt/inspect-ops/release.py status

# 应显示类似：
# {
#   "current": {
#     "version": "v20261125-150833-07450bb",
#     "deployed_at": "2026-11-25T15:08:35+08:00"
#   },
#   "releases": ["v20261125-150833-07450bb", ...],
#   "transaction": null
# }

# 2. 查看审计日志
sudo cat /opt/inspect-ops/state/audit.jsonl | tail -5 | python3 -m json.tool

# 3. 检查服务状态
sudo systemctl status inspect-platform inspect-admin inspect-worker inspect-mail
# 应全部为 active (running)

# 4. 测试健康端点
curl http://127.0.0.1:8765/qa/api/health
# 应返回 {"ok":true,"service":"inspect-platform",...}

# 5. 访问公开看板
curl -I http://8.146.206.64/qa/
# 应返回 200 OK
```

---

### ✅ 步骤 7：配置 PR 测试方案（三选一）

**重要**：PR 不能在生产 Jenkins 构建（安全要求），需选择以下方案之一。

#### 方案 A：本地测试（推荐，零成本）

**开发者工作流**：

```bash
# 1. 创建 PR 分支
git checkout -b feature/new-check

# 2. 修改代码
vim core.py

# 3. 本地运行测试
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py' -v

# 4. 语法检查
python3 -m py_compile *.py
node --check static/app.js admin-static/app.js
bash -n inspect_run.sh

# 5. 测试通过后提交 PR
git push -u origin feature/new-check
```

**代码审查者**：在批准 PR 前，要求提供测试通过的截图。

#### 方案 B：GitHub Actions（需配置）

创建 `.github/workflows/test.yml`（本轮未提供，需手动创建）：

```yaml
name: Test

on:
  pull_request:
    branches: [ main ]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.6'
      - name: Run tests
        run: PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py' -v
```

#### 方案 C：Jenkins 独立 Agent（需额外资源）

见 `JENKINS-SETUP.md` "附录：独立 CI Agent 配置"。

---

### ✅ 步骤 8：测试安全隔离（可选）

**验证 PR 被拒绝**：

```bash
# 1. 创建测试分支
git checkout -b test/security-check
echo "# Test" >> README.md
git add README.md
git commit -m "test: 安全检查测试"
git push -u origin test/security-check

# 2. 在 GitHub 创建 PR：test/security-check → main

# 3. 如果 Jenkins 配置了扫描 PR（Multibranch），应看到：
#    - 构建触发
#    - 安全检查阶段失败
#    - 错误消息："🚨 安全拒绝：非 main 分支不允许在生产 Jenkins 构建"

# 4. 这是预期行为（安全保护）

# 5. 关闭测试 PR
```

---

## 验收标准

### ✅ 部署自动化

- [ ] Jenkins 任务 `inspect-platform-deploy` 创建成功
- [ ] Git 轮询配置生效（2 分钟周期）
- [ ] main 分支推送 2 分钟内触发构建
- [ ] 语法检查通过
- [ ] 67 项测试通过
- [ ] 制品打包和验证成功
- [ ] 部署阶段调用宿主脚本
- [ ] 服务健康检查通过
- [ ] 公开看板 http://8.146.206.64/qa/ 正常访问

### ✅ 安全隔离

- [ ] 非 main 分支构建被拒绝（如有）
- [ ] PR 构建被拒绝（如配置扫描 PR）
- [ ] 部署使用 `/opt/inspect-ops/deploy-from-jenkins.sh`（固定脚本）
- [ ] 不执行 repo 中的 `ops/release.py`

### ✅ PR 测试方案

- [ ] 已选择并配置 PR 测试方案（A/B/C）
- [ ] 开发者知道如何测试 PR

### ✅ 版本追溯

- [ ] 每次部署生成唯一版本号（v<日期>-<时间>-<commit>）
- [ ] 审计日志记录在 `/opt/inspect-ops/state/audit.jsonl`
- [ ] 制品归档在 Jenkins

---

## 故障排查

### 问题 1：部署脚本不存在

**症状**：
```
部署脚本未就绪：/opt/inspect-ops/deploy-from-jenkins.sh 不存在
```

**原因**：未完成步骤 1

**解决**：
```bash
sudo cp /opt/inspect-platform/deploy/deploy-from-jenkins.sh /opt/inspect-ops/
sudo cp /opt/inspect-platform/ops/release.py /opt/inspect-ops/
sudo chmod 755 /opt/inspect-ops/*.sh /opt/inspect-ops/release.py
```

---

### 问题 2：Jenkins 容器无法访问宿主脚本

**症状**：
```
/opt/inspect-ops/deploy-from-jenkins.sh: No such file or directory
```

**原因**：容器未挂载 `/opt/inspect-ops`

**解决**：

**检查挂载**：
```bash
docker inspect <jenkins-container-name> | grep -A 10 Mounts
```

**添加挂载**（需重启容器）：
```bash
docker stop <jenkins-container-name>
# 修改启动命令添加：-v /opt/inspect-ops:/opt/inspect-ops:ro
```

**或使用 docker exec**（如无法修改挂载）：
需修改 Jenkinsfile 部署阶段，联系父代理配置。

---

### 问题 3：非 main 分支被拒绝

**症状**：
```
🚨 安全拒绝：非 main 分支不允许在生产 Jenkins 构建
```

**原因**：这是预期行为（安全保护）

**解决**：
- PR：在本地运行测试，或配置 GitHub Actions
- 其他分支：合并到 main 后自动部署

---

### 问题 4：健康检查失败

**症状**：
```
部署失败 - 应已自动回滚到前一版本
```

**检查**：
```bash
# 1. 查看服务状态
sudo systemctl status inspect-platform

# 2. 查看日志
sudo journalctl -u inspect-platform -n 50

# 3. 手动测试健康端点
curl http://127.0.0.1:8765/qa/api/health
```

**常见原因**：
- Python 语法错误或 import 失败
- 端口被占用
- data/ 目录权限问题

**恢复**：
- 自动回滚应已恢复服务
- 修复代码后重新推送 main 分支

---

### 问题 5：测试失败

**症状**：
```
Ran 50 tests ... FAILED
```

**检查**：
- 查看 Jenkins Console Output 中的失败测试
- 本地重现：`PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py' -v`
- 修复代码后重新推送

---

## 日常维护

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

# 验证
sudo systemctl status inspect-platform
curl http://127.0.0.1:8765/qa/api/health
```

### 升级部署工具

**⚠️ 谨慎操作**：需人工评审新版 `ops/release.py`

```bash
# 1. 审查变更
cd /opt/inspect-platform
git diff <old-commit> <new-commit> ops/release.py

# 2. 更新固定副本
sudo cp ops/release.py /opt/inspect-ops/release.py
sudo chmod 755 /opt/inspect-ops/release.py

# 3. 验证
python3 /opt/inspect-ops/release.py status
```

---

## 安全提醒

### ❌ 禁止操作

1. **不要**将 `data/secret.env`、`data/mail.env` 提交到仓库
2. **不要**让 Jenkins 容器以 writable 方式挂载 `/opt/inspect-ops`
3. **不要**在 Jenkinsfile 中执行 `repo` 中的 `ops/release.py`
4. **不要**配置 Jenkins 扫描 PR（生产 Jenkins 必须拒绝 PR）
5. **不要**删除审计日志 `/opt/inspect-ops/state/audit.jsonl`

### ✅ 建议操作

1. **定期备份** `data/` 目录到安全存储
2. **审查 PR** 后再合并到 main（代码审查 + 测试截图）
3. **维护窗口部署**：建议在低峰期合并重大变更
4. **监控审计日志**：定期检查 `/opt/inspect-ops/state/audit.jsonl`
5. **测试回滚流程**：每季度执行一次回滚演练

---

## 完成标志

当以下所有项打勾时，Jenkins CI/CD 集成完成：

- [ ] 宿主部署脚本准备完成（步骤 1）
- [ ] Jenkins 容器可访问宿主脚本（步骤 2）
- [ ] GitHub 只读凭证配置完成（步骤 3）
- [ ] Jenkins 任务 `inspect-platform-deploy` 创建成功（步骤 4）
- [ ] Git 轮询配置生效（2 分钟周期）
- [ ] main 分支首次构建成功（步骤 5）
- [ ] 部署阶段执行成功
- [ ] 服务健康检查通过
- [ ] 公开看板访问正常（步骤 6）
- [ ] PR 测试方案已选择并配置（步骤 7）
- [ ] 安全隔离验证通过（步骤 8，可选）

**完成后回报**：
- Jenkins 任务 URL
- 首次部署版本号
- 选择的 PR 测试方案（A/B/C）
- 任何异常或注意事项

---

## 联系与支持

- **完整配置指南**：`JENKINS-SETUP.md`
- **仓库**：https://github.com/zhushuai0825/inspect-platform
- **服务器**：http://8.146.206.64/qa/
- **问题反馈**：提交 GitHub Issue

**配置完成日期**：____________________

**配置人员签名**：____________________
