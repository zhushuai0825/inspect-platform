# Dispatcher 安装与验收指南

**状态**: 未完成生产验收  
**环境**: 测试/临时环境  
**用途**: 供运维在隔离目录测试

---

## ⚠️ 重要说明

本文档用于**测试环境**安装和验收。

**未完成验收项**：
- Python 3.6 实际环境测试（当前测试环境 Python 3.12）
- Docker 容器隔离测试（当前环境无 Docker）
- 真实 release.py 集成测试
- 生产环境端到端验证

**已完成**：
- 代码层面 Python 3.6 兼容（使用 universal_newlines）
- 安全测试（20项）+ 端到端测试（3项）
- 配置文件和 service 准备
- 隔离测试脚本设计

---

## 1. 文件清单

### Dispatcher 核心
- `dispatcher.py` - 主程序（450行，20项安全测试）
- `config.json.example` - 配置模板
- `inspect-deploy-dispatcher.service` - Systemd service

### Jenkins 脚本
- `notify-deploy.sh` - 部署通知脚本（简化）

### 隔离测试（供 release.py 调用）
- `run-candidate-tests.sh` - 容器隔离测试脚本

### 测试
- `test_dispatcher_security.py` - 20项安全测试
- `test_e2e_local.py` - 3项端到端测试

---

## 2. 安装步骤（测试环境）

### 2.1 创建目录结构

```bash
# 以 root 身份操作。控制面与应用树分开，应用目录不是 /source。
# 完整步骤见 control-plane/INSTALL-CONTROL-PLANE.sh。这里只列与 dispatcher 读配置有关的权限。
sudo install -d -o root -g root -m 0755 /opt/inspect-deploy/bin /opt/inspect-deploy/config
sudo install -d -o root -g inspect-deploy -m 0750 /var/lib/inspect-deploy
sudo install -d -o inspect-dispatcher -g inspect-deploy -m 0750 /var/lib/inspect-deploy/work
sudo install -d -o root -g root -m 0700 /var/lib/inspect-deploy/backups
sudo install -d -o root -g root -m 0755 /var/lib/inspect-deploy/runner-stage
sudo install -d -o root -g root -m 0711 /opt/inspect-platform/data
sudo install -d -o inspect-dispatcher -g inspect-deploy -m 0750 /opt/inspect-platform/data/deploy-spool
sudo install -d -o root -g root -m 0700 /opt/inspect-ops/state
```

### 2.2 安装 Dispatcher

```bash
# 复制主程序到控制面。dispatcher 用户不能写这个目录。
sudo install -o root -g root -m 0755 dispatcher.py /opt/inspect-deploy/bin/dispatcher.py
sudo install -o root -g root -m 0755 ../../control-plane/bin/inspect-release /opt/inspect-deploy/bin/inspect-release

# 配置给 inspect-deploy 组读，不使用 root-only 0600（服务用户会在 import 时 PermissionError）。
# 也不使用 0644。文件里没有 secret.env / mail.env。
sudo install -o root -g inspect-deploy -m 0640 config.json.example /opt/inspect-deploy/config/deploy-config.json

# 编辑配置（如需要）
sudo vi /opt/inspect-deploy/config/deploy-config.json
```

**配置说明**：
- `repo_url`: 固定 GitHub repo（不可由请求更改）
- `branch`: 固定 main（不可由请求更改）
- `spool_dir`: 请求队列目录
- `work_dir`: 工作目录（克隆/制品）

### 2.3 安装 Systemd Service

```bash
# 复制 service 文件
sudo cp inspect-deploy-dispatcher.service /etc/systemd/system/
sudo chmod 644 /etc/systemd/system/inspect-deploy-dispatcher.service

# 重载 systemd
sudo systemctl daemon-reload

# 启用（但不立即启动）
sudo systemctl enable inspect-deploy-dispatcher
```

### 2.4 安装 Jenkins 脚本

```bash
# Jenkins 容器内
sudo cp notify-deploy.sh /var/jenkins_home/jenkins-scripts/
sudo chmod 755 /var/jenkins_home/jenkins-scripts/notify-deploy.sh
```

### 2.5 安装隔离测试脚本

```bash
# 复制到运维脚本目录
sudo cp run-candidate-tests.sh /opt/inspect-ops/scripts/
sudo chmod 755 /opt/inspect-ops/scripts/run-candidate-tests.sh
sudo chown root:root /opt/inspect-ops/scripts/run-candidate-tests.sh
```

---

## 3. 验收测试

### 3.1 单元测试

```bash
# 安全测试（20项）
cd <source-dir>
python3 tests/test_dispatcher_security.py

# 预期输出：
# Ran 20 tests in 1.0s
# OK
```

### 3.2 端到端测试（Mock）

```bash
# 端到端测试（3项）
cd <source-dir>
python3 tests/test_e2e_local.py

# 预期输出：
# Ran 3 tests in 0.0s
# OK
```

### 3.3 配置验证

```bash
# 验证配置文件
sudo python3 -c "
import json
with open('/opt/inspect-deploy/config/deploy-config.json') as f:
    config = json.load(f)
print('Repo:', config['repo_url'])
print('Branch:', config['branch'])
"
```

### 3.4 Dispatcher 语法检查

```bash
# Python 3.6 兼容检查（如环境有 python3.6）
python3.6 -m py_compile /opt/inspect-deploy/dispatcher.py

# 或 Python 3.12
python3 -m py_compile /opt/inspect-deploy/dispatcher.py
```

### 3.5 手动通知测试（临时 spool）

```bash
# 创建临时 spool
mkdir -p /tmp/test-spool

# 创建测试请求
cat > /tmp/test-spool/request-manual-test.json <<EOF
{
  "request_id": "manual-test-001",
  "commit": "$(git -C <source-dir> rev-parse main)",
  "timestamp": "$(date -Iseconds)",
  "source": "manual"
}
EOF

# 手动运行 dispatcher（一次）
# 注意：需要修改 deploy-config.json 指向 /tmp/test-spool
# 或环境变量覆盖
```

---

## 4. 启动与监控

### 4.1 启动 Dispatcher

```bash
# 启动服务
sudo systemctl start inspect-deploy-dispatcher

# 检查状态
sudo systemctl status inspect-deploy-dispatcher

# 查看日志
sudo journalctl -u inspect-deploy-dispatcher -f
```

### 4.2 停止 Dispatcher

```bash
sudo systemctl stop inspect-deploy-dispatcher
```

### 4.3 查看日志

```bash
# Systemd journal
sudo journalctl -u inspect-deploy-dispatcher --since "1 hour ago"

# Dispatcher 日志文件
sudo tail -f /opt/inspect-deploy/dispatcher.log
```

---

## 5. 备份与撤销

### 5.1 备份配置

```bash
# 备份配置
sudo cp /opt/inspect-deploy/config/deploy-config.json /opt/inspect-deploy/config/deploy-config.json.backup-$(date +%Y%m%d-%H%M%S)

# 备份程序
sudo cp /opt/inspect-deploy/dispatcher.py /opt/inspect-deploy/dispatcher.py.backup-$(date +%Y%m%d-%H%M%S)
```

### 5.2 撤销安装

```bash
# 停止服务
sudo systemctl stop inspect-deploy-dispatcher
sudo systemctl disable inspect-deploy-dispatcher

# 删除 service
sudo rm /etc/systemd/system/inspect-deploy-dispatcher.service
sudo systemctl daemon-reload

# 删除程序（可选）
sudo rm -rf /opt/inspect-deploy

# 保留 spool（如需要）
# sudo rm -rf /opt/inspect-platform/data/deploy-spool
```

---

## 6. 安全检查清单

### 6.1 文件权限

```bash
# Dispatcher 程序（控制面，root 拥有，目录不可被 dispatcher 用户写入）
sudo ls -l /opt/inspect-deploy/bin/dispatcher.py /opt/inspect-deploy/bin/inspect-release
# 应为：-rwxr-xr-x root root

# 配置文件（组 inspect-deploy 可读，与 service 的 Group= 一致）
sudo ls -l /opt/inspect-deploy/config/deploy-config.json
# 应为：-rw-r----- root inspect-deploy

# 工作目录在控制面外面
sudo ls -ld /var/lib/inspect-deploy/work
# 应为：drwxr-x--- inspect-dispatcher inspect-deploy
```

### 6.2 配置检查

```bash
# repo_url 是否为固定 GitHub URL
# branch 是否为 main
# spool_dir 是否正确
# work_dir 是否在 /opt/inspect-deploy 下
```

### 6.3 符号链接检查

```bash
# spool 及父目录无符号链接
sudo find /opt/inspect-platform/data/deploy-spool -type l

# work 目录无符号链接
sudo find /opt/inspect-deploy/work -type l
```

---

## 7. 故障排查

### 7.1 Dispatcher 无法启动

```bash
# 检查日志
sudo journalctl -u inspect-deploy-dispatcher -n 50

# 检查 Python 版本
python3 --version

# 检查配置文件
sudo python3 -c "import json; print(json.load(open('/opt/inspect-deploy/config/deploy-config.json')))"
```

### 7.2 请求未处理

```bash
# 检查 spool 目录
sudo ls -la /opt/inspect-platform/data/deploy-spool/

# 检查 dispatcher 是否运行
sudo systemctl is-active inspect-deploy-dispatcher

# 检查防重放记录
sudo cat /opt/inspect-deploy/work/processed-requests.txt | tail -20
```

### 7.3 Git 克隆失败

```bash
# 检查网络
curl -I https://github.com/zhushuai0825/inspect-platform.git

# 检查 Git 配置
git config --global --list

# 手动测试 git ls-remote
git ls-remote https://github.com/zhushuai0825/inspect-platform.git refs/heads/main
```

---

## 8. 未完成项

### 8.1 Python 3.6 实际环境测试

**状态**: 代码兼容，未在 3.6 运行

**原因**: 测试环境 Python 3.12，无 python3.6 命令

**验证方法**（如环境有 Python 3.6）:
```bash
python3.6 tests/test_dispatcher_security.py
python3.6 tests/test_e2e_local.py
```

### 8.2 Docker 容器隔离测试

**状态**: 脚本准备，未实际运行

**原因**: 测试环境无 Docker

**验证方法**（如环境有 Docker）:
```bash
# 测试隔离脚本
SOURCE=/tmp/test-source
DATA=/tmp/test-data
mkdir -p $SOURCE $DATA
echo '# test' > $SOURCE/app.py
bash /opt/inspect-ops/scripts/run-candidate-tests.sh $SOURCE $DATA
```

### 8.3 release.py 集成

**状态**: 设计完成，未端到端验证

**原因**: 需要完整生产环境（数据库/systemd/secrets）

**验证方法**: 见后续运维测试文档

---

## 9. 下一步

1. 在 Python 3.6 环境运行所有测试
2. 在有 Docker 环境测试容器隔离
3. 完整 release.py 集成测试
4. 生产环境端到端验证
5. 监控和告警配置

---

**文档版本**: 2026-11-25  
**状态**: 测试环境验收  
**下一里程碑**: 生产环境验收
