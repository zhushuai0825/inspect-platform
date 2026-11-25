#!/bin/bash
# 隔离测试脚本 - 由固定 Freestyle Job 调用
# 部署位置：/var/jenkins_home/scripts/run-isolated-test.sh (Jenkins 容器内)
# 权限：root:root, 755
# 维护：Jenkins 管理员（不受 repo 控制）
#
# 安全：在隔离 Docker 容器中运行测试
# - 无生产挂载
# - 无 Docker socket
# - 无网络
# - 资源限额 (384MB 内存, 0.5 CPU)
# - 只读根文件系统

set -euo pipefail

# === 参数解析 ===
WORKSPACE=""
COMMIT=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

# === 验证参数 ===
if [[ -z "$WORKSPACE" || -z "$COMMIT" ]]; then
  echo "❌ 错误：缺少必需参数"
  echo "用法: $0 --workspace <path> --commit <sha>"
  exit 1
fi

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  隔离测试容器启动                                            ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "工作空间: $WORKSPACE"
echo "提交: $COMMIT"
echo "容器名: inspect-test-${COMMIT:0:8}"
echo ""

# === 清理旧容器（如存在）===
if docker ps -a --format '{{.Names}}' | grep -q "^inspect-test-${COMMIT:0:8}$"; then
  echo "清理旧容器..."
  docker rm -f "inspect-test-${COMMIT:0:8}" || true
fi

# === 启动隔离测试容器 ===
echo "启动测试容器..."

docker run --rm \
  --name "inspect-test-${COMMIT:0:8}" \
  --network none \
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
  bash -euxo pipefail -c '
    echo "=== 测试容器环境 ==="
    echo "Python: $(python3 --version)"
    echo "工作目录: $(pwd)"
    echo "文件数: $(find . -type f | wc -l)"
    echo ""
    
    # 安装 Node.js（语法检查需要）
    echo "=== 安装 Node.js ==="
    apt-get update -qq
    apt-get install -y --no-install-recommends nodejs npm > /dev/null 2>&1
    echo "Node: $(node --version)"
    echo ""
    
    # Python 语法检查
    echo "=== Python 语法检查 ==="
    python3 -m py_compile app.py
    python3 -m py_compile core.py
    python3 -m py_compile admin.py
    python3 -m py_compile worker.py
    python3 -m py_compile notifications.py
    python3 -m py_compile deploy/update_job.py
    python3 -m py_compile ops/release.py
    python3 -m py_compile ops/test_release.py
    echo "✓ Python 语法检查通过"
    echo ""
    
    # JavaScript 语法检查
    echo "=== JavaScript 语法检查 ==="
    node --check static/app.js
    node --check admin-static/app.js
    echo "✓ JavaScript 语法检查通过"
    echo ""
    
    # Bash 语法检查
    echo "=== Bash 语法检查 ==="
    bash -n inspect_run.sh
    echo "✓ Bash 语法检查通过"
    echo ""
    
    # 应用测试
    echo "=== 运行应用测试 (50 项) ==="
    export PYTHONPATH=/src
    export PYTHONDONTWRITEBYTECODE=1
    python3 -m unittest discover -s tests -p "test_*.py" -v
    echo ""
    
    # 运维测试
    echo "=== 运行运维测试 (17 项) ==="
    cd ops
    export PYTHONPATH=/src
    python3 -m unittest test_release -v
    cd /src
    echo ""
    
    echo "╔════════════════════════════════════════════════════════════════╗"
    echo "║  ✓ 所有测试通过                                              ║"
    echo "╚════════════════════════════════════════════════════════════════╝"
  '

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  echo ""
  echo "✓ 测试容器完成，退出码: $EXIT_CODE"
  exit 0
else
  echo ""
  echo "✗ 测试失败，退出码: $EXIT_CODE"
  exit $EXIT_CODE
fi
