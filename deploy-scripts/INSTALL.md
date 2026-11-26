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

### 2.1 只运行官方脚本

目录由 `control-plane/INSTALL-CONTROL-PLANE.sh` 创建。不要另用 `install -d` 覆盖已经存在的生产目录。

```bash
sudo bash control-plane/INSTALL-CONTROL-PLANE.sh
```

不带 `INSTALL_ROOT` 时，脚本会创建 `inspect-deploy` 组和 `inspect-dispatcher` 用户。带 `INSTALL_ROOT` 的前缀安装只写该前缀下的副本，不调用 `useradd` / `groupadd`。`INSTALL_ROOT` 不能落在 `/opt`、`/var`、`/var/lib`、`/etc` 这类父路径上，也不能落在 `/opt/inspect-platform`、`/opt/inspect-ops`、`/opt/inspect-deploy`、`/var/lib/inspect-deploy` 或 `/etc/sudoers.d` 里面。

脚本会安装这些控制面路径：

- `/opt/inspect-deploy`（程序、配置、unit 副本、runner 定义）
- `/var/lib/inspect-deploy`（work、backups、runner-stage）
- `/var/lib/inspect-deploy/spool`（请求队列，属主 inspect-dispatcher）
- `/etc/sudoers.d/inspect-deploy`（只允许固定 helper；visudo 失败则删除）

脚本对已经存在的生产目录只做检查，不 `chown`、不 `chmod`、不 `install -d`：

| 路径 | 可以继续安装的前提 | 否则 |
| --- | --- | --- |
| `/opt/inspect-platform` | 真实目录，且不是世界可写。属主可以不是 root | 失败退出，目录保持原样 |
| `/opt/inspect-platform/data` | 真实目录，且不是世界可写。不要求改成 0711 | 失败退出，目录保持原样 |
| `/opt/inspect-ops` | 若存在，必须是 root:root 且 mode 0700 | 失败退出，不放宽、不收紧 |
| `/opt/inspect-ops/state` | 若存在，必须是 root:root 且 mode 0700 | 失败退出，不改权限 |

不存在的应用目录不会被创建。`/opt/inspect-platform/data/deploy-spool` 不会被创建；如果它已经存在，脚本失败退出，并且不删除、不改它。`/opt/inspect-ops` 若不存在，脚本不创建它。只有在 `/opt/inspect-ops` 已经是 root:root 0700 且 `state` 还不存在时，脚本才用 `mkdir` 创建 `state` 这一个目录，不用 `install -d` 改父目录。

脚本不读取 `secret.env`、`mail.env` 或数据库。

### 2.2 配置

脚本安装 `control-plane/config/deploy-config.json` 为 `/opt/inspect-deploy/config/deploy-config.json`，root:inspect-deploy 0640。不要改成 0600，也不要改成 0644。

- `repo_url`: 固定 GitHub repo（不可由请求更改）
- `branch`: 固定 main（不可由请求更改）
- `spool_dir`: `/var/lib/inspect-deploy/spool`
- `work_dir`: `/var/lib/inspect-deploy/work`
- `log_file`: `/var/lib/inspect-deploy/log/dispatcher.log`

### 2.3 不在本安装里做的事

下面这些都不是本安装脚本的步骤，也还没有验收。不要把它们当成必做：

- 不把 unit 拷到 `/etc/systemd`，不 `systemctl daemon-reload`，不 `systemctl enable`，不 `systemctl start`
- 不把 `notify-deploy.sh` 拷进 Jenkins 家目录
- 不把脚本拷进 `/opt/inspect-ops/scripts/`

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

`systemctl enable` 和 `systemctl start` 不是安装步骤，本轮没有验收，不要从本文档执行。安装脚本只把 unit 放在 `/opt/inspect-deploy/systemd/`。

日志文件配置为 `/var/lib/inspect-deploy/log/dispatcher.log`。该目录属主是 `inspect-dispatcher`，父目录 `/var/lib/inspect-deploy` 仍是 root 所有且组不可写。本文档不要求在生产机上跟踪 journal。

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

撤销没有随本安装脚本验收。不要为了撤销去改 `/opt/inspect-platform` 或删除应用 data 里的文件。队列若已按本脚本创建，位置是 `/var/lib/inspect-deploy/spool`，不是 `/opt/inspect-platform/data/deploy-spool`。

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
# work_dir 是否为 /var/lib/inspect-deploy/work
# spool_dir 是否为 /var/lib/inspect-deploy/spool，而不是应用 data
```

### 6.3 符号链接检查

```bash
# spool 在控制面，不在应用 data
sudo find /var/lib/inspect-deploy/spool -type l

# work 目录无符号链接
sudo find /var/lib/inspect-deploy/work -type l
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
# 检查 spool 目录（控制面，不是应用 data）
sudo ls -la /var/lib/inspect-deploy/spool/

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
bash control-plane/bin/run-candidate-tests $SOURCE $DATA
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

**文档版本**: 2026-11-26  
**状态**: 与 INSTALL-CONTROL-PLANE.sh 一致。生产接线未验收，不要按本文档上线。
