# 部署指南

**Main-Only Jenkins 自动化**（2026-11-25）

---

## 前提

- Jenkins 容器：root (uid=0)，Docker socket，挂载 `/opt/inspect-platform`
- 宿主：Python 3.6.8, Node 20.20.2, systemd
- 资源：809MB 可用

## 架构

```
GitHub main → Jenkins 轮询 → 固定脚本测试/打包 → 写入 spool → 宿主 dispatcher → 部署
```

详见 `ARCHITECTURE.md`

---

## 步骤 1：安装固定脚本

### 1.1 Jenkins 容器内

```bash
# 创建脚本目录
docker exec -u root jenkins mkdir -p /var/jenkins_home/jenkins-scripts

# 复制脚本
docker cp deploy-scripts/jenkins-scripts/run-test.sh jenkins:/var/jenkins_home/jenkins-scripts/
docker cp deploy-scripts/jenkins-scripts/package.sh jenkins:/var/jenkins_home/jenkins-scripts/
docker cp deploy-scripts/jenkins-scripts/trigger-deploy.sh jenkins:/var/jenkins_home/jenkins-scripts/

# 设置权限
docker exec -u root jenkins chmod 755 /var/jenkins_home/jenkins-scripts/*.sh

# 验证
docker exec jenkins ls -lh /var/jenkins_home/jenkins-scripts/
```

### 1.2 宿主机

```bash
# 创建 dispatcher 目录
sudo mkdir -p /opt/inspect-deploy
sudo chmod 755 /opt/inspect-deploy

# 复制 dispatcher
sudo cp deploy-scripts/host-dispatcher/dispatcher.py /opt/inspect-deploy/
sudo chmod 755 /opt/inspect-deploy/dispatcher.py

# 安装 systemd service
sudo cp deploy-scripts/host-dispatcher/inspect-deploy-dispatcher.service /etc/systemd/system/
sudo systemctl daemon-reload

# 验证
ls -lh /opt/inspect-deploy/
```

---

## 步骤 2：配置 Sudoers

```bash
sudo visudo -f /etc/sudoers.d/inspect-deploy
```

添加：
```sudoers
root ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py prepare *, \
                          /usr/bin/python3 /opt/inspect-platform/ops/release.py deploy *, \
                          /usr/bin/python3 /opt/inspect-platform/ops/release.py status, \
                          /usr/bin/python3 /opt/inspect-platform/ops/release.py rollback *
```

---

## 步骤 3：创建 Jenkins Job

**类型**：Freestyle project  
**名称**：`inspect-platform-deploy`

**配置**：

1. **SCM**: Git, `https://github.com/zhushuai0825/inspect-platform.git`, `*/main`
2. **Poll SCM**: `H/2 * * * *`
3. **Build Steps**:

```bash
# Step 1: 测试
/var/jenkins_home/jenkins-scripts/run-test.sh "${WORKSPACE}" "${GIT_COMMIT}"

# Step 2: 打包
/var/jenkins_home/jenkins-scripts/package.sh "${WORKSPACE}" "${GIT_COMMIT}"

# Step 3: 触发部署
/var/jenkins_home/jenkins-scripts/trigger-deploy.sh "${GIT_COMMIT}" "${GIT_BRANCH}" "${WORKSPACE}/build/inspect-platform-${GIT_COMMIT:0:8}.zip"
```

4. **Post-build**: Archive `build/*.zip, build/*.sha256`

---

## 步骤 4：启动 Dispatcher

```bash
sudo systemctl enable inspect-deploy-dispatcher
sudo systemctl start inspect-deploy-dispatcher
sudo systemctl status inspect-deploy-dispatcher

# 查看日志
sudo journalctl -u inspect-deploy-dispatcher -f
```

---

## 步骤 5：首次构建

Jenkins UI → `inspect-platform-deploy` → **Build Now**

**预期**：
- ✓ 测试 (67 项)
- ✓ 打包
- ✓ 触发部署
- ✓ Dispatcher 处理
- ✓ 部署成功

---

## 验证

```bash
# 查看部署状态
sudo python3 /opt/inspect-platform/ops/release.py status

# 查看 dispatcher 日志
sudo tail -50 /opt/inspect-deploy/dispatcher.log

# 访问服务
curl http://8.146.206.64/qa/api/health
```

---

## 安全边界

1. **控制器不执行 repo 代码**：固定脚本在 root 拥有的目录
2. **测试隔离**：独立容器，只读 repo
3. **部署验证**：Dispatcher 验证分支和制品
4. **PR 禁用**：Job 仅轮询 main

---

## 限制

- 无 PR 自动测试（本里程碑明确禁用）
- 串行部署
- 短暂维护窗口
- 固定脚本需手动维护

---

## 文档日期

2026-11-25
