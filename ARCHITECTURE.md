# 巡检平台 Jenkins 自动化架构

**Main-Only 方案**（基于真实拓扑，2026-11-25）

---

## 核心约束

### 拓扑现状
- Jenkins 容器：root (uid=0)，可写挂载 Docker socket + `/opt/inspect-platform`
- 容器工具：Git, Java, Docker CLI（无 Python/Node）
- 宿主工具：Python 3.6.8, Node 20.20.2
- 控制器：2 执行器，无 agent/cloud
- 资源：809MB 可用

### 安全要求
1. ❌ 控制器不执行任何仓库代码
2. ❌ PR 执行明确禁用（本里程碑不支持）
3. ✅ Main-only: 自动测试 + 部署
4. ✅ 固定脚本控制所有特权操作

---

## 架构方案

### 总览

```
GitHub main 分支更新
        ↓
Jenkins 轮询（Freestyle Job）
        ↓
├─ 测试阶段 ──────────────────────────────┐
│   固定脚本（Jenkins 容器内）             │
│   docker run 隔离测试容器：              │
│   - 独立 bridge 网络                     │
│   - 384MB 内存限额                       │
│   - 只读 repo (通过 volume)              │
│   └─ 67 项测试                           │
        ↓
├─ 打包阶段 ──────────────────────────────┤
│   固定脚本创建制品：                     │
│   - inspect-platform-<commit>.zip        │
│   - SHA256 哈希                          │
        ↓
├─ 触发部署 ──────────────────────────────┤
│   写入部署请求到宿主 spool：             │
│   - /opt/inspect-platform/data/deploy-spool/
│   - 请求包含：commit, branch, artifact  │
        ↓
宿主 Dispatcher 服务
        ↓
├─ 验证请求 ──────────────────────────────┤
│   - 仅接受 main 分支                     │
│   - 验证 artifact 路径                   │
│   - 验证 SHA256                          │
        ↓
├─ 执行部署 ──────────────────────────────┤
│   sudo 执行固定 release.py：             │
│   - prepare <artifact> <version>         │
│   - deploy <version>                     │
│   - 健康检查 + 自动回滚                  │
        ↓
  生产服务更新
```

---

## 关键设计

### 1. 路径映射

**Jenkins workspace**（容器内）：
```
/var/jenkins_home/workspace/inspect-platform-deploy/
```

**映射到宿主**（volume）：
```
/var/lib/docker/volumes/jenkins_jenkins_home/_data/workspace/inspect-platform-deploy/
```

**固定脚本位置**：
- Jenkins 容器内：`/var/jenkins_home/jenkins-scripts/`（固定，不受 repo 控制）
- 宿主机：`/opt/inspect-deploy/`（固定 dispatcher）

### 2. 测试容器网络

**使用隔离 bridge 网络**（非 none）：
```bash
docker network create --internal inspect-test-net
docker run --rm --network inspect-test-net ...
```

**原因**：测试需要回环网络（127.0.0.1 HTTP 服务器）

**隔离**：`--internal` 禁止外网访问

### 3. Spool 触发机制

**类似现有 `inspect_run.sh` 机制**：

Jenkins 写入（通过挂载）：
```bash
/opt/inspect-platform/data/deploy-spool/request-<timestamp>.json
```

格式：
```json
{
  "commit": "abc123...",
  "branch": "main",
  "artifact": "/var/lib/docker/volumes/.../build/artifact.zip",
  "artifact_sha256": "...",
  "timestamp": "2026-11-25T07:30:00+08:00"
}
```

Dispatcher 监听（systemd service）：
- inotify 或定时扫描
- 验证请求
- 执行部署
- 写入结果

### 4. 固定脚本归属

**宿主机**：
```bash
/opt/inspect-deploy/           # root:root, 755
├── dispatcher.py              # root:root, 755
├── process-request.sh         # root:root, 755
└── config.conf                # root:root, 644
```

**Jenkins 容器内**：
```bash
/var/jenkins_home/jenkins-scripts/  # root:root, 755
├── run-test.sh                     # root:root, 755
└── package.sh                      # root:root, 755
```

**关键**：
- 父目录 root 拥有
- 脚本 root 拥有，非 repo 内容
- Repo 修改不能影响这些脚本

---

## 实现

### 固定脚本 1：测试（Jenkins 容器内）

**位置**：`/var/jenkins_home/jenkins-scripts/run-test.sh`

```bash
#!/bin/bash
# 固定测试脚本
# 由 Jenkins Freestyle Job 调用
# 不受 repo 控制

set -euo pipefail

WORKSPACE="$1"
COMMIT="$2"

# 创建隔离网络（如不存在）
if ! docker network inspect inspect-test-net >/dev/null 2>&1; then
  docker network create --internal --subnet 172.30.0.0/16 inspect-test-net
fi

# 清理旧测试容器
docker rm -f inspect-test-${COMMIT:0:8} 2>/dev/null || true

# 启动隔离测试容器
docker run --rm \
  --name "inspect-test-${COMMIT:0:8}" \
  --network inspect-test-net \
  --memory 384m \
  --cpus 0.5 \
  --pids-limit 100 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --security-opt no-new-privileges:true \
  -v "${WORKSPACE}:/src:ro" \
  -w /src \
  python:3.6-slim \
  bash -euxc '
    apt-get update -qq
    apt-get install -y --no-install-recommends nodejs npm
    python3 -m py_compile app.py core.py admin.py worker.py notifications.py
    node --check static/app.js admin-static/app.js
    bash -n inspect_run.sh
    export PYTHONPATH=/src PYTHONDONTWRITEBYTECODE=1
    python3 -m unittest discover -s tests -p "test_*.py" -v
  '
```

### 固定脚本 2：打包（Jenkins 容器内）

**位置**：`/var/jenkins_home/jenkins-scripts/package.sh`

```bash
#!/bin/bash
# 固定打包脚本
set -euo pipefail

WORKSPACE="$1"
COMMIT="$2"
BUILD_DIR="${WORKSPACE}/build"

mkdir -p "${BUILD_DIR}"
ARTIFACT_NAME="inspect-platform-${COMMIT:0:8}.zip"

# 使用 Jenkins 容器的 Git 创建 archive
cd "${WORKSPACE}"
git archive --format=zip --prefix=inspect-platform/ HEAD > "${BUILD_DIR}/${ARTIFACT_NAME}"

# 计算 SHA256（使用临时容器）
docker run --rm \
  -v "${BUILD_DIR}:/data:rw" \
  --network none \
  alpine:3.19 \
  sh -c "cd /data && sha256sum ${ARTIFACT_NAME} > ${ARTIFACT_NAME}.sha256"
```

### 固定脚本 3：触发部署（Jenkins 容器内）

**位置**：`/var/jenkins_home/jenkins-scripts/trigger-deploy.sh`

```bash
#!/bin/bash
# 触发宿主 dispatcher
set -euo pipefail

COMMIT="$1"
BRANCH="$2"
ARTIFACT="$3"  # 容器内路径

# 转换为宿主路径
# /var/jenkins_home/workspace/... → /var/lib/docker/volumes/jenkins_jenkins_home/_data/workspace/...
HOST_ARTIFACT="${ARTIFACT/\/var\/jenkins_home/\/var\/lib\/docker\/volumes\/jenkins_jenkins_home\/_data}"

# 写入部署请求（通过挂载的 /opt/inspect-platform）
SPOOL="/opt/inspect-platform/data/deploy-spool"
mkdir -p "${SPOOL}"

REQUEST_FILE="${SPOOL}/request-$(date +%s)-${COMMIT:0:8}.json"

cat > "${REQUEST_FILE}" <<EOF
{
  "commit": "${COMMIT}",
  "branch": "${BRANCH}",
  "artifact": "${HOST_ARTIFACT}",
  "artifact_sha256": "${HOST_ARTIFACT}.sha256",
  "timestamp": "$(date -Iseconds)",
  "source": "jenkins"
}
EOF

echo "部署请求已写入: ${REQUEST_FILE}"
echo "等待 dispatcher 处理..."

# 等待结果（最多 5 分钟）
RESULT_FILE="${SPOOL}/result-$(date +%s)-${COMMIT:0:8}.json"
for i in {1..60}; do
  if [ -f "${RESULT_FILE}" ]; then
    cat "${RESULT_FILE}"
    STATUS=$(jq -r '.status' "${RESULT_FILE}")
    [ "$STATUS" = "success" ] && exit 0 || exit 1
  fi
  sleep 5
done

echo "超时：未收到 dispatcher 响应"
exit 1
```

### 宿主 Dispatcher

**位置**：`/opt/inspect-deploy/dispatcher.py`

```python
#!/usr/bin/env python3
"""
宿主部署 dispatcher
监听 /opt/inspect-platform/data/deploy-spool/
验证请求，执行部署
"""
import json
import os
import subprocess
import time
from pathlib import Path

SPOOL = Path('/opt/inspect-platform/data/deploy-spool')
SPOOL.mkdir(parents=True, exist_ok=True, mode=0o755)

def process_request(req_file):
    """处理部署请求"""
    try:
        with open(req_file) as f:
            req = json.load(f)
        
        # 安全验证
        if req['branch'] not in ('main', 'refs/heads/main', 'origin/main'):
            raise ValueError(f"拒绝非 main 分支: {req['branch']}")
        
        artifact = Path(req['artifact'])
        if not artifact.exists():
            raise FileNotFoundError(f"制品不存在: {artifact}")
        
        artifact_sha256 = Path(req['artifact_sha256'])
        if not artifact_sha256.exists():
            raise FileNotFoundError(f"哈希文件不存在: {artifact_sha256}")
        
        # 验证 SHA256
        result = subprocess.run(
            ['sha256sum', '-c', str(artifact_sha256)],
            cwd=artifact.parent,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            raise ValueError("制品完整性校验失败")
        
        # 生成版本号
        version = f"v{time.strftime('%Y%m%d-%H%M%S')}-{req['commit'][:8]}"
        
        # 执行部署
        subprocess.run(
            ['sudo', '/usr/bin/python3', '/opt/inspect-platform/ops/release.py', 'prepare', str(artifact), version],
            check=True
        )
        subprocess.run(
            ['sudo', '/usr/bin/python3', '/opt/inspect-platform/ops/release.py', 'deploy', version],
            check=True
        )
        
        # 写入成功结果
        result_file = req_file.parent / req_file.name.replace('request-', 'result-')
        with open(result_file, 'w') as f:
            json.dump({'status': 'success', 'version': version}, f)
        
        # 删除请求文件
        req_file.unlink()
        
    except Exception as e:
        # 写入失败结果
        result_file = req_file.parent / req_file.name.replace('request-', 'result-')
        with open(result_file, 'w') as f:
            json.dump({'status': 'failed', 'error': str(e)}, f)
        req_file.unlink()

def main():
    """主循环"""
    print("Dispatcher 启动，监听:", SPOOL)
    while True:
        for req_file in sorted(SPOOL.glob('request-*.json')):
            print(f"处理请求: {req_file.name}")
            process_request(req_file)
        time.sleep(2)

if __name__ == '__main__':
    main()
```

**Systemd Service**：`/etc/systemd/system/inspect-deploy-dispatcher.service`

```ini
[Unit]
Description=Inspect Platform Deploy Dispatcher
After=network.target docker.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/inspect-deploy/dispatcher.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Jenkins Job 配置

**类型**：Freestyle project  
**名称**：`inspect-platform-deploy`

**Build Steps**：

1. Execute shell - 测试
```bash
/var/jenkins_home/jenkins-scripts/run-test.sh "${WORKSPACE}" "${GIT_COMMIT}"
```

2. Execute shell - 打包
```bash
/var/jenkins_home/jenkins-scripts/package.sh "${WORKSPACE}" "${GIT_COMMIT}"
```

3. Execute shell - 触发部署
```bash
/var/jenkins_home/jenkins-scripts/trigger-deploy.sh "${GIT_COMMIT}" "${GIT_BRANCH}" "${WORKSPACE}/build/inspect-platform-${GIT_COMMIT:0:8}.zip"
```

---

## 安全验证

### PR 无法执行
- Jenkins Job 仅轮询 `*/main`
- Repo 中的 Jenkinsfile 不被使用
- 固定脚本不在 repo 中

### Repo 修改无法提权
- 固定脚本在 root 拥有的目录
- Dispatcher 验证分支和制品
- release.py 被 sudo 执行，但不直接执行 repo 代码

### 路径隔离
- 测试容器只读挂载 repo
- 生产 data/ 不在测试容器中
- Dispatcher 在宿主运行，独立于 Jenkins

---

## 配置步骤

见 `DEPLOYMENT-GUIDE.md`

---

## 已知限制

1. **无 PR 自动测试**（本里程碑明确禁用）
2. **串行部署**（资源限制）
3. **短暂维护窗口**（具体时长未测量）
4. **手动维护固定脚本**

---

## 文档日期

2026-11-25（基于真实拓扑，Main-only 方案）
