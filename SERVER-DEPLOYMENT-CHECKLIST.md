# 服务器部署操作清单

**目标**：在服务器完成 Jenkins CI/CD 配置，实现仓库提交自动测试与 main 分支自动部署。

**前置条件**：本 PR 已合并到 `main` 分支。

---

## 操作清单

### ✅ 步骤 1：准备 Git 凭证

在 Jenkins 中创建 GitHub 只读凭证：

```bash
# 1. 生成 GitHub Personal Access Token (PAT)
# 访问：https://github.com/settings/tokens
# Scopes: 勾选 "repo"（或仅 "public_repo" 若仓库公开）
# 复制生成的 token（只显示一次）

# 2. 在 Jenkins 添加凭证
# Jenkins → Manage Jenkins → Credentials → System → Global credentials
# 类型：Username with password
# Username: zhushuai0825（GitHub 用户名）
# Password: <上面生成的 PAT>
# ID: github-inspect-platform（凭证 ID，后续引用）
# Description: GitHub inspect-platform 只读
```

**备选方案**：使用 SSH Key（如已配置 Deploy Key）

---

### ✅ 步骤 2：创建 Jenkins CI/CD 任务

#### 推荐：Multibranch Pipeline（自动发现所有分支和 PR）

1. Jenkins 首页 → **New Item**
2. 名称：`inspect-platform-ci`
3. 类型：**Multibranch Pipeline**
4. 点击 **OK**

**配置页面**：

##### Branch Sources
- 点击 **Add source** → **Git**
- **Project Repository**: `https://github.com/zhushuai0825/inspect-platform.git`
- **Credentials**: 选择 `github-inspect-platform`（步骤 1 创建的）
- **Behaviors**:
  - ✓ Discover branches
  - ✓ Discover pull requests from origin
  - ✓ Discover pull requests from forks（Strategy: Merging）

##### Build Configuration
- **Mode**: by Jenkinsfile
- **Script Path**: `Jenkinsfile`

##### Scan Repository Triggers
- ✓ **Periodically if not otherwise run**
- **Interval**: `2 minutes`（或 `5 minutes`，根据需求）

##### Orphaned Item Strategy
- **Days to keep old items**: `7`
- **Max # of old items to keep**: `20`

5. 点击 **Save**

---

### ✅ 步骤 3：配置 sudo 权限

Jenkins 用户需要执行 `ops/release.py` 进行部署。

#### 3.1 确定 Jenkins 用户

```bash
# 查看 Jenkins 进程用户
ps aux | grep jenkins | grep -v grep

# 或检查 Docker 容器用户
docker inspect <jenkins-container-name> | grep -i user
```

通常为 `jenkins` 用户。若 Jenkins 运行在 Docker 中且挂载了 `/opt/inspect-platform`，确认容器内外用户映射。

#### 3.2 授予最小权限

```bash
# 以 root 身份执行
sudo visudo -f /etc/sudoers.d/jenkins-deploy
```

**添加以下内容**（假设 Jenkins 用户为 `jenkins`）：

```sudoers
# Allow Jenkins to run release.py for inspect-platform deployment
jenkins ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py status
jenkins ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py prepare *
jenkins ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py deploy *
jenkins ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py rollback *
```

**保存并验证**：

```bash
# 切换到 jenkins 用户测试
sudo -u jenkins sudo python3 /opt/inspect-platform/ops/release.py status

# 应无密码提示，直接返回状态 JSON
```

**Docker 用户注意**：
- 如 Jenkins 运行在 Docker 中，确保容器用户可访问 `/opt/inspect-platform`
- 可能需要在宿主机配置 sudoers，并映射用户 UID/GID

---

### ✅ 步骤 4：首次运行测试

1. 进入 Jenkins → `inspect-platform-ci`
2. 点击 **Scan Repository Now**
3. 等待扫描完成（约 10-30 秒）

**预期结果**：
- 自动发现 `main` 分支和任何 PR
- 触发 `main` 分支的首次构建

#### 检查构建日志

点击 `main` 分支 → 最新构建 → **Console Output**

**预期阶段**：
1. ✅ **准备** - 克隆仓库
2. ✅ **语法检查** - Python/JavaScript/Bash
3. ✅ **自动化测试** - 50 应用测试 + 17 运维测试
4. ✅ **打包制品** - 生成 `.zip` 和 `.sha256`
5. ✅ **制品验证** - 哈希校验、大小检查
6. ✅ **部署到生产** - 调用 `ops/release.py`

**若部署阶段出现 `sudo: a password is required`**：
- 返回步骤 3，检查 sudoers 配置
- 确认 Jenkins 用户名正确

---

### ✅ 步骤 5：验证部署结果

#### 5.1 检查服务器状态

```bash
# 查看发布状态
sudo python3 /opt/inspect-platform/ops/release.py status

# 应显示：
# - current: 最新部署的版本（格式：v20261125-HHMMSS-<commit>）
# - releases: 已准备的版本列表
# - transaction: null（无未完成事务）
```

#### 5.2 检查应用健康

```bash
# 检查服务状态
sudo systemctl status inspect-platform inspect-admin inspect-worker inspect-mail

# 应全部为 active (running)
```

#### 5.3 访问公开看板

浏览器打开：http://8.146.206.64/qa/

**验证项**：
- [ ] 页面正常加载
- [ ] 显示最新报告
- [ ] 任务列表可见
- [ ] 事件列表可见
- [ ] 无 JavaScript 错误（F12 Console）

#### 5.4 访问管理端（通过 SSH 隧道）

```bash
# 本地电脑执行
ssh -N -L 18766:127.0.0.1:8766 root@8.146.206.64
```

浏览器打开：http://127.0.0.1:18766/

**验证项**：
- [ ] 管理界面正常加载
- [ ] 任务列表可编辑
- [ ] 保存配置成功

---

### ✅ 步骤 6：测试 PR 自动化（可选）

创建一个测试 PR 验证安全隔离：

```bash
# 在开发机器（非服务器）
git clone https://github.com/zhushuai0825/inspect-platform.git
cd inspect-platform
git checkout -b test/ci-validation

# 修改 README.md 添加测试注释
echo "<!-- CI Test -->" >> README.md

git add README.md
git commit -m "test: CI 自动化验证"
git push -u origin test/ci-validation

# 在 GitHub 创建 PR：test/ci-validation → main
```

**在 Jenkins 验证**：
1. 2 分钟内自动发现新 PR
2. 触发构建
3. 运行测试阶段（✓ 语法检查、✓ 测试、✓ 打包）
4. **跳过部署阶段**（`when` 条件阻止）
5. 构建日志显示："受信部署: 否 - 仅测试"

**验证完成后关闭测试 PR**，无需合并。

---

### ✅ 步骤 7：测试回滚功能（可选）

```bash
# 查看可用版本
sudo python3 /opt/inspect-platform/ops/release.py status

# 假设有版本：v20261125-140522-a1b2c3d4 和 v20261125-150833-e5f6g7h8
# 回滚到前一版本
sudo python3 /opt/inspect-platform/ops/release.py rollback v20261125-140522-a1b2c3d4

# 验证服务健康
curl http://127.0.0.1:8765/qa/api/health
sudo systemctl status inspect-platform

# 检查版本
sudo python3 /opt/inspect-platform/ops/release.py status
# current 应显示回滚后的版本
```

---

## 验收标准

### ✅ CI 自动化
- [ ] 所有分支提交触发构建（2 分钟内）
- [ ] 测试阶段通过（语法、单元测试、打包）
- [ ] PR 构建成功但不执行部署

### ✅ CD 自动化
- [ ] `main` 分支更新触发构建
- [ ] 测试通过后自动执行部署阶段
- [ ] 调用 `ops/release.py prepare` 和 `deploy`
- [ ] 服务健康检查通过
- [ ] 公开看板正常访问

### ✅ 安全隔离
- [ ] PR 和 fork 只运行测试
- [ ] 部署阶段仅 `main` 分支执行
- [ ] 生产凭证（`data/secret.env`）不泄露

### ✅ 版本追溯
- [ ] 每次部署生成唯一版本号
- [ ] 制品归档在 Jenkins
- [ ] 审计日志记录在 `/opt/inspect-ops/state/audit.jsonl`

### ✅ 回滚机制
- [ ] 健康检查失败自动回滚
- [ ] 手动回滚到历史版本成功
- [ ] 回滚后服务正常

---

## 故障排查

### 问题 1：Git 轮询未触发构建

**症状**：推送代码后 2 分钟内无构建

**检查**：
```bash
# Jenkins → inspect-platform-ci → 左侧菜单 → Git Polling Log
# 查看最近轮询记录
```

**常见原因**：
- 凭证过期或无效
- 仓库 URL 错误
- 轮询周期配置错误

**解决**：
- 重新生成 GitHub PAT 并更新凭证
- 测试网络连通：`curl https://github.com/zhushuai0825/inspect-platform.git`
- 检查防火墙规则

---

### 问题 2：部署阶段权限拒绝

**症状**：
```
sudo: a password is required
```

**原因**：sudoers 未配置或用户名不匹配

**解决**：
1. 确认 Jenkins 实际运行用户：`ps aux | grep jenkins`
2. 更新 `/etc/sudoers.d/jenkins-deploy` 中的用户名
3. 确保 `NOPASSWD:` 配置正确
4. 测试：`sudo -u jenkins sudo python3 /opt/inspect-platform/ops/release.py status`

---

### 问题 3：健康检查失败

**症状**：
```
部署失败 - 应已自动回滚到前一版本
```

**检查**：
```bash
# 查看服务状态
sudo systemctl status inspect-platform inspect-admin inspect-worker inspect-mail

# 查看应用日志
sudo journalctl -u inspect-platform -n 50

# 手动测试健康端点
curl http://127.0.0.1:8765/qa/api/health
curl http://127.0.0.1:8766/api/health
```

**常见原因**：
- 服务启动超时（代码语法错误、依赖缺失）
- 健康端点路径变更
- 端口被占用

**解决**：
- 修复代码错误后重新推送
- 检查 `ops/release.py` 的 `health_check()` 实现
- 手动回滚：`sudo python3 /opt/inspect-platform/ops/release.py rollback <version>`

---

### 问题 4：RELEASE_BUSY 锁无法释放

**症状**：
```
ReleaseError: RELEASE_BUSY
```

**原因**：发布进程异常中断，锁未释放

**解决**：
```bash
# 1. 确认无 release.py 进程运行
ps aux | grep release.py

# 2. 如无进程，恢复事务
sudo python3 /opt/inspect-platform/ops/release.py recover

# 3. 检查状态
sudo python3 /opt/inspect-platform/ops/release.py status
```

---

### 问题 5：部署后旧报告消失

**症状**：部署后历史报告丢失

**原因**：误操作覆盖了 `data/` 目录（**不应发生**）

**预防**：
- `ops/release.py` 明确隔离 `data/` 目录，不应被覆盖
- 检查是否有手动操作干预

**恢复**：
```bash
# 查看私有备份
ls -lh /root/backups/inspect-release-data-*/

# 恢复数据（谨慎操作，仅恢复 data/ 目录）
sudo tar -xzf /root/backups/inspect-release-data-<version>/data.tar.gz -C /opt/inspect-platform/
```

---

## 后续维护

### 日常操作

#### 合并 PR 到 main
1. 开发者提交 PR
2. Jenkins 自动测试 PR（提供反馈）
3. 代码审查通过
4. 合并到 `main`
5. **Jenkins 自动部署**（无需人工干预）

#### 查看部署日志
```bash
# Jenkins 构建日志
# Jenkins → inspect-platform-ci → main → 最新构建 → Console Output

# 服务器审计日志
sudo cat /opt/inspect-ops/state/audit.jsonl | tail -10 | jq .

# 应用服务日志
sudo journalctl -u inspect-platform -f
```

#### 手动触发部署
```bash
# 方法 1：在 Jenkins 触发
# Jenkins → inspect-platform-ci → main → Build Now

# 方法 2：服务器本地部署（紧急情况）
# 先从 Jenkins 下载制品或准备 ZIP
sudo python3 /opt/inspect-platform/ops/release.py prepare /path/to/artifact.zip vEmergency
sudo python3 /opt/inspect-platform/ops/release.py deploy vEmergency
```

#### 监控自动化流程
- Jenkins 构建历史：`inspect-platform-ci` 页面
- 部署频率：每次 `main` 更新（合并 PR 或直接推送）
- 失败通知：Jenkins 构建失败会显示红色，可配置邮件通知

---

## 安全提醒

### ❌ 禁止操作
1. **不要**将 `data/secret.env`、`data/mail.env` 提交到仓库
2. **不要**将 `/root/backups/` 下的私有备份公开
3. **不要**在非服务器环境运行 `ops/release.py deploy`
4. **不要**手动修改 `/opt/inspect-ops/state/releases/` 下的版本目录
5. **不要**删除已登记的版本目录（破坏校验）

### ✅ 建议操作
1. **定期备份** `data/` 目录到安全存储
2. **审查 PR** 后再合并到 `main`（部署前人工检查）
3. **维护窗口部署**：建议在低峰期合并重大变更
4. **监控审计日志**：定期检查 `/opt/inspect-ops/state/audit.jsonl`
5. **测试回滚流程**：每季度执行一次回滚演练

---

## 完成标志

当以下所有项打勾时，Jenkins CI/CD 集成完成：

- [ ] Jenkins 任务 `inspect-platform-ci` 创建成功
- [ ] Git 轮询配置生效（2 分钟周期）
- [ ] sudo 权限配置验证通过
- [ ] `main` 分支首次构建成功
- [ ] 部署阶段执行成功
- [ ] 服务健康检查通过
- [ ] 公开看板 http://8.146.206.64/qa/ 访问正常
- [ ] 管理端 SSH 隧道访问正常
- [ ] PR 构建测试通过但不部署
- [ ] 审计日志记录部署事件

**完成后回报**：
- Jenkins 任务 URL
- 首次部署版本号
- 部署日志摘要
- 任何异常或注意事项

---

## 联系与支持

- **文档**：`JENKINS-SETUP.md`（详细配置指南）
- **仓库**：https://github.com/zhushuai0825/inspect-platform
- **服务器路径**：`/opt/inspect-platform`
- **审计日志**：`/opt/inspect-ops/state/audit.jsonl`
- **问题反馈**：提交 GitHub Issue

**配置完成日期**：____________________

**配置人员签名**：____________________
