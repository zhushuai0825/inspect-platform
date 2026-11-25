# Jenkins CI/CD 配置指南

本文档说明如何在服务器 Jenkins 上配置仓库的自动化测试与部署流程。

## 概览

本平台使用 **Jenkins** 作为核心 CI/CD 执行引擎（非 GitHub Actions），实现：

1. **自动化测试（CI）**：所有分支/PR 提交自动触发测试
2. **自动化部署（CD）**：仅受信 `main` 分支通过测试后允许部署
3. **安全隔离**：PR 和 fork 只能运行测试，无法接触部署密钥或生产环境
4. **自动回滚**：部署失败时健康检查自动回滚到前一版本

## 两个独立任务

### 1. `inspect-platform-ci` - 仓库 CI/CD 流水线（新建）

- **用途**：自动化测试所有提交，自动部署 main 分支
- **Jenkinsfile**: 仓库根目录 `Jenkinsfile`
- **触发方式**：Git 轮询（建议 2 分钟）
- **分支策略**：
  - 所有分支和 PR：运行语法检查、自动化测试、打包制品
  - **仅** `main` 分支（非 PR）：额外执行制品验证和生产部署
- **权限要求**：需要 sudo 权限执行 `/opt/inspect-platform/ops/release.py`

### 2. `site-http-smoke` - 日常巡检任务（保持现有）

- **用途**：每日 09:00 执行 HTTP 冒烟巡检
- **Jenkinsfile**: `jenkins/Jenkinsfile`
- **触发方式**：cron 定时 + 手动触发
- **与 CI/CD 无关**：这是业务监控任务，不是代码发布流水线

---

## 配置步骤

### 步骤 1：准备 Jenkins 插件

确保安装以下插件（服务器 Jenkins 通常已有）：

- **Git Plugin** - 仓库克隆与轮询
- **Pipeline Plugin** - Jenkinsfile 支持
- **Credentials Plugin** - 凭证管理

### 步骤 2：创建 Git 凭证（如未配置）

Jenkins 需要 GitHub 仓库的只读访问权限：

1. 进入 Jenkins → Manage Jenkins → Credentials
2. 添加凭证（类型：Username with password 或 SSH Key）
   - **用户名/密码**：GitHub 用户名 + Personal Access Token (PAT)
     - Token 权限：`repo`（私有仓库）或 `public_repo`（公开仓库）
   - **SSH 密钥**：或使用 SSH deploy key
3. 记录凭证 ID（例如：`github-inspect-platform`）

**重要**：此凭证仅用于 Git 克隆，不涉及部署密钥。

### 步骤 3：创建 CI/CD 流水线任务

#### 3.1 创建新任务

1. Jenkins 首页 → New Item
2. 名称：`inspect-platform-ci`
3. 类型：**Pipeline** 或 **Multibranch Pipeline**（推荐）
4. 点击 OK

#### 3.2 推荐配置：Multibranch Pipeline

**优势**：自动发现所有分支和 PR，每个独立构建，安全隔离。

配置项：

- **Branch Sources** → Add source → Git
  - **Project Repository**: `https://github.com/zhushuai0825/inspect-platform.git`
  - **Credentials**: 选择步骤 2 创建的凭证
  - **Behaviors**:
    - ✓ Discover branches
    - ✓ Discover pull requests from origin
    - ✓ Discover pull requests from forks (Strategy: Merging the pull request with the current target branch revision)

- **Build Configuration**
  - **Mode**: by Jenkinsfile
  - **Script Path**: `Jenkinsfile`（仓库根目录）

- **Scan Repository Triggers**
  - ✓ **Periodically if not otherwise run**
    - **Interval**: `2 minutes`（或按需调整，建议 2-5 分钟）
  
  > **为什么用轮询而非 Webhook？**
  > - 避免 HTTP webhook 明文传输 secret
  > - 无需公网接口接收 GitHub POST 请求
  > - 轮询周期可明确（例如 2 分钟），而非依赖外部通知
  > - 如后续需要实时触发，可改用 GitHub App with JWT 或配置防火墙保护的 webhook endpoint

- **Orphaned Item Strategy**
  - Days to keep old items: `7`
  - Max # of old items to keep: `20`

#### 3.3 备选配置：单 Pipeline

如不需要自动发现分支/PR，可用简单 Pipeline：

- **Definition**: Pipeline script from SCM
- **SCM**: Git
  - **Repository URL**: `https://github.com/zhushuai0825/inspect-platform.git`
  - **Credentials**: 选择步骤 2 凭证
  - **Branch Specifier**: `*/main`（仅主分支）或 `*/*`（所有分支）
- **Script Path**: `Jenkinsfile`
- **Poll SCM**: `H/2 * * * *`（每 2 分钟轮询）

### 步骤 4：配置 sudo 权限（部署阶段必需）

CI/CD 流水线的**部署阶段**需要执行 `sudo python3 /opt/inspect-platform/ops/release.py`。

#### 4.1 确定 Jenkins 运行用户

```bash
ps aux | grep jenkins
# 或检查 Docker 容器运行用户
docker inspect <jenkins-container> | grep User
```

通常为 `jenkins` 用户（如运行在 Docker 中可能是 `root` 或自定义用户）。

#### 4.2 授予最小 sudo 权限

假设 Jenkins 用户为 `jenkins`，编辑 sudoers：

```bash
sudo visudo -f /etc/sudoers.d/jenkins-deploy
```

添加以下内容（**仅授权必需的命令**）：

```sudoers
# Allow Jenkins to run release.py for inspect-platform deployment
jenkins ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py status
jenkins ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py prepare *
jenkins ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py deploy *
jenkins ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py rollback *
```

保存并验证：

```bash
# 以 jenkins 用户测试（无密码提示）
sudo -u jenkins sudo python3 /opt/inspect-platform/ops/release.py status
```

**安全说明**：
- 仅授权 `/opt/inspect-platform/ops/release.py`，不允许任意命令
- 参数使用 `*` 通配符（版本号动态生成）
- `NOPASSWD` 避免交互式密码提示（流水线无法输入）

### 步骤 5：首次运行与验证

#### 5.1 触发首次扫描

- Multibranch Pipeline: 点击 "Scan Repository Now"
- 单 Pipeline: 点击 "Build Now"

#### 5.2 检查构建结果

**测试阶段（所有分支/PR）**：
- ✓ 语法检查通过
- ✓ 自动化测试通过（67 项单元/集成测试）
- ✓ 制品打包成功（生成 `.zip` 和 `.sha256`）

**部署阶段（仅 main 分支）**：
- ✓ 制品验证通过
- ✓ 调用 `ops/release.py prepare` 成功（校验、临时测试）
- ✓ 调用 `ops/release.py deploy` 成功（停服、备份、安装、健康检查）
- ✓ 若健康检查失败，自动回滚到前一版本

#### 5.3 查看部署日志

```bash
# 服务器上查看发布状态
sudo python3 /opt/inspect-platform/ops/release.py status

# 查看审计日志
sudo cat /opt/inspect-ops/state/audit.jsonl | tail -20

# 查看最新版本
ls -lh /opt/inspect-ops/state/releases/
```

---

## 分支与 PR 策略

### 允许部署的条件

**必须同时满足**：
1. 分支名为 `main`
2. 不是 Pull Request（`CHANGE_ID` 为空）

### PR 和其他分支的行为

- ✓ 自动触发构建
- ✓ 运行完整测试
- ✓ 打包制品（归档为 Jenkins artifact）
- ✗ **不执行部署阶段**（`when` 条件阻止）
- ✗ **无法访问生产凭证**（`data/secret.env` 不在工作空间）

### 合并到 main 的流程

1. 开发者提交 PR
2. Jenkins 自动测试 PR（提供测试反馈）
3. 代码审查通过后合并到 `main`
4. Jenkins 检测到 `main` 更新（2 分钟内）
5. 重新运行测试
6. **测试通过后自动部署到生产**

---

## 制品与版本追溯

### 制品内容

每次构建生成：
- `inspect-platform-<commit>.zip` - 部署包（包含 `inspect-platform/` 前缀）
- `inspect-platform-<commit>.zip.sha256` - 完整性校验

### 版本号规则

自动生成格式：`v<日期>-<时间>-<commit>`

示例：`v20261125-143022-a1b2c3d4`

### 完整性验证

部署前自动校验：
1. SHA256 哈希匹配
2. 制品大小合理（10KB - 4MB）
3. 文件白名单检查（`ops/release.py` 的 `RUNTIME` 列表）
4. Python 语法预检
5. 自动化测试（临时环境，无生产凭证）

### 回滚到历史版本

#### 方法 1：通过 Jenkins 重新部署旧 commit

1. Jenkins → inspect-platform-ci → main 分支
2. 找到要恢复的历史构建（包含对应 commit）
3. 点击 "Replay" 或 "Rebuild"

#### 方法 2：服务器手动回滚

```bash
# 查看可用版本
sudo python3 /opt/inspect-platform/ops/release.py status

# 回滚到已知版本
sudo python3 /opt/inspect-platform/ops/release.py rollback v20261125-143022-a1b2c3d4
```

**限制**：只能回滚到 `prepare` 过的版本（已校验和测试）。

---

## 维护窗口与并发控制

### 维护窗口

部署过程会**短暂停止服务**（非零停机）：

1. 检查活动巡检和待发邮件（有则中止）
2. 停止 `inspect-platform`、`inspect-admin`、`inspect-worker`、`inspect-mail` 服务
3. 备份运行数据到 `/root/backups/inspect-release-data-<version>/`（私有）
4. 原子替换源代码文件
5. 启动服务并运行健康检查
6. 失败则自动回滚并重启旧版本

**典型停机时长**：20-40 秒

**注意**：
- 破坏性数据库迁移不自动回滚（需人工评审）
- 不覆盖 `data/` 目录（持久数据与源码隔离）

### 并发控制

**Pipeline 级别**：
- `disableConcurrentBuilds()` - 同一任务串行执行

**部署级别**：
- `ops/release.py` 使用 flock 文件锁
- 多个部署请求自动排队（后者等待或失败）

### 冲突处理

如遇 `RELEASE_BUSY` 错误：
```bash
# 查看锁状态
sudo python3 /opt/inspect-platform/ops/release.py status

# 确认无部署进程后（若异常中断）
sudo python3 /opt/inspect-platform/ops/release.py recover
```

---

## 安全与权限设计

### 凭证隔离

| 凭证类型 | 位置 | 访问权限 | 用途 |
|---------|-----|---------|------|
| GitHub 只读凭证 | Jenkins Credentials | Jenkins 进程 | 克隆仓库 |
| Jenkins API Token | `data/secret.env` | root 只读（600） | 更新 `site-http-smoke` 任务定义 |
| SMTP 密码 | `data/mail.env` | root 只读（600） | 邮件通知 |
| 部署权限 | sudoers | Jenkins 用户（限定命令） | 执行 release.py |

### PR 安全保障

**PR 构建的限制**：
1. 工作空间隔离（临时目录，无生产数据）
2. 测试环境变量 `QA_DATA_DIR` 指向临时目录
3. 不继承生产环境变量（`data/secret.env` 不可见）
4. `when` 条件阻止部署阶段执行
5. 无 sudo 权限（即使尝试也被拒绝）

**Fork PR 的额外保护**：
- Multibranch Pipeline 的 "Discover pull requests from forks" 策略使用 "Merging" 模式
- Fork 的 Jenkinsfile 修改**不会**被信任执行（使用目标分支的 Jenkinsfile）
- 建议启用 Jenkins "Suppress automatic SCM triggering" for untrusted authors

### 生产凭证管理

**绝不**将以下内容提交到仓库：
- `data/secret.env` - Jenkins API Token
- `data/mail.env` - SMTP 密码
- `data/platform.db` - 数据库文件
- `/root/backups/*` - 私有备份

`.gitignore` 已正确配置排除这些文件。

---

## 监控与日志

### Jenkins 构建日志

- **位置**：Jenkins UI → 任务 → 构建历史 → Console Output
- **内容**：测试输出、部署步骤、回滚记录

### 服务器部署日志

```bash
# 审计日志（所有发布事件）
sudo cat /opt/inspect-ops/state/audit.jsonl | jq .

# 当前状态
sudo python3 /opt/inspect-platform/ops/release.py status

# 最近部署
sudo tail -f /var/log/syslog | grep inspect

# 应用服务日志
sudo journalctl -u inspect-platform -u inspect-admin -u inspect-worker -u inspect-mail -f
```

### 健康检查端点

部署后自动验证：
- http://127.0.0.1:8765/qa/api/health
- http://127.0.0.1:8766/api/health（管理端，需 loopback）
- 个人主页、简历、运维入口、Jenkins 认证边界

失败则自动回滚。

---

## 故障排查

### 问题 1：轮询未触发构建

**检查**：
```bash
# Jenkins → 任务 → Git Polling Log
# 查看最近轮询记录和 Git 仓库访问日志
```

**常见原因**：
- 凭证过期或无效
- 网络连接问题（防火墙）
- 轮询周期配置错误

**解决**：
- 重新生成 GitHub PAT 并更新 Jenkins 凭证
- 测试网络连通性：`curl https://github.com/zhushuai0825/inspect-platform.git`

### 问题 2：部署阶段权限拒绝

**错误**：`sudo: a password is required`

**原因**：sudoers 未正确配置

**解决**：参考步骤 4 配置 `NOPASSWD`

### 问题 3：健康检查失败，但服务正常

**原因**：健康检查超时或端点变更

**解决**：
1. 检查 `ops/release.py` 的 `health_check()` 实现
2. 调整超时或增加重试
3. 手动验证端点：`curl http://127.0.0.1:8765/qa/api/health`

### 问题 4：回滚失败

**错误**：`rollback_failed` 记录在 `audit.jsonl`

**应对**：
1. 不要继续自动部署
2. 查看 `/root/backups/inspect-release-data-<version>/` 备份
3. 手动恢复源代码：
   ```bash
   sudo cp -r /opt/inspect-ops/state/releases/<known-version>/source/* /opt/inspect-platform/
   sudo systemctl restart inspect-platform inspect-admin inspect-worker inspect-mail
   ```

### 问题 5：RELEASE_BUSY 锁无法释放

**原因**：发布进程异常中断

**解决**：
```bash
# 确认无 release.py 进程运行
ps aux | grep release.py

# 恢复事务状态
sudo python3 /opt/inspect-platform/ops/release.py recover
```

---

## 后续改进建议

### 可选增强（本次未实现）

1. **实时 Webhook 触发**
   - 配置 GitHub Webhook → Jenkins Generic Webhook Trigger
   - 需要防火墙规则保护 Jenkins endpoint
   - 或使用 GitHub App with JWT 避免明文 secret

2. **多环境部署**
   - 增加 staging 环境测试
   - main → staging 自动部署 → 手动批准 → production

3. **蓝绿部署**
   - 零停机切换（需双实例和负载均衡）
   - 当前为单实例，有短暂维护窗口

4. **破坏性迁移支持**
   - 数据库 schema 版本管理
   - 迁移脚本与回滚脚本成对

5. **通知集成**
   - Jenkins Email Extension 插件
   - 部署成功/失败通知到邮件/Slack/企业微信

---

## 检查清单

部署前确认：

- [ ] Jenkins 安装 Git、Pipeline 插件
- [ ] 创建 GitHub 只读凭证（PAT 或 SSH Key）
- [ ] 创建 `inspect-platform-ci` Multibranch Pipeline 任务
- [ ] 配置 Git 轮询（2 分钟周期）
- [ ] 配置 sudoers 授权 `jenkins` 用户执行 `release.py`
- [ ] 首次扫描仓库，验证 main 分支构建成功
- [ ] 检查部署日志，确认服务健康
- [ ] 测试 PR 构建（仅测试，不部署）
- [ ] 验证回滚功能（可选，建议在 staging 测试）

部署后验证：

- [ ] 访问 http://8.146.206.64/qa/ 看板正常
- [ ] 新报告使用新代码生成
- [ ] 管理端 SSH 隧道访问正常（http://127.0.0.1:18766/）
- [ ] Jenkins 认证边界保持
- [ ] 旧报告内容未变（配置快照隔离）
- [ ] 查看 `audit.jsonl` 确认部署记录
- [ ] 保留原 `site-http-smoke` 任务定义和调度

---

## 联系与支持

- **文档更新日期**：2026-11-25
- **仓库**：https://github.com/zhushuai0825/inspect-platform
- **服务器路径**：`/opt/inspect-platform`
- **运维工具路径**：`/opt/inspect-ops`（如已安装）
- **问题反馈**：提交 GitHub Issue 或联系维护人员

---

**重要提醒**：

1. **不要**在非服务器环境运行 `ops/release.py deploy`（会失败且无意义）
2. **不要**手动修改 `/opt/inspect-ops/state/releases/` 下的版本目录（破坏校验）
3. **不要**将 `data/secret.env`、`data/mail.env` 或私有备份提交到仓库
4. **不要**在生产时段触发部署（建议维护窗口：凌晨或低峰期）
5. **始终**在合并到 main 前通过 PR 完成测试和代码审查
