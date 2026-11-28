#!/bin/bash
# 固定测试脚本 - 部署到 Jenkins 容器内
# 目标位置：/var/jenkins_home/jenkins-scripts/run-test.sh
# 权限：root:root, 755
# 维护：Jenkins 管理员（不受 repo 控制）

set -euo pipefail

if [ $# -ne 2 ]; then
  echo "用法: $0 <WORKSPACE> <COMMIT>"
  exit 1
fi

WORKSPACE="$1"
COMMIT="$2"

echo "=== 隔离测试容器 ==="
echo "工作空间: ${WORKSPACE}"
echo "提交: ${COMMIT}"

# 创建隔离测试网络（如不存在）
if ! docker network inspect inspect-test-net >/dev/null 2>&1; then
  echo "创建隔离测试网络..."
  docker network create --internal --subnet 172.30.0.0/16 inspect-test-net
fi

# 清理旧测试容器
docker rm -f "inspect-test-${COMMIT:0:8}" 2>/dev/null || true

# 启动隔离测试容器
echo "启动测试容器: inspect-test-${COMMIT:0:8}"

docker run --rm \
  --name "inspect-test-${COMMIT:0:8}" \
  --network inspect-test-net \
  --memory 384m \
  --memory-swap 512m \
  --cpus 0.5 \
  --pids-limit 100 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /root:rw,noexec,nosuid,size=16m \
  --security-opt no-new-privileges:true \
  -v "${WORKSPACE}:/src:ro" \
  -w /src \
  --user root \
  python:3.6-slim \
  bash -euxc '
    echo "=== 安装依赖 ==="
    apt-get update -qq
    apt-get install -y --no-install-recommends nodejs npm > /dev/null 2>&1
    
    echo "=== Python 语法检查 ==="
    python3 -m py_compile app.py core.py admin.py worker.py notifications.py publish_board.py
    python3 -m py_compile deploy/update_job.py ops/release.py ops/test_release.py
    
    echo "=== JavaScript 语法检查 ==="
    node --check static/app.js admin-static/app.js
    
    echo "=== Bash 语法检查 ==="
    bash -n inspect_run.sh
    
    echo "=== 应用测试 (50 项) ==="
    export PYTHONPATH=/src
    export PYTHONDONTWRITEBYTECODE=1
    python3 -m unittest discover -s tests -p "test_*.py" -v
    
    echo "=== 运维测试 (17 项) ==="
    cd ops
    export PYTHONPATH=/src
    python3 -m unittest test_release -v
  '

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  echo "✓ 所有测试通过"
else
  echo "✗ 测试失败 (exit $EXIT_CODE)"
fi

exit $EXIT_CODE
