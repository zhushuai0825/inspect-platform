#!/bin/bash
# 隔离候选测试脚本
# 位置（生产）：/opt/inspect-ops/scripts/run-candidate-tests.sh
# 权限：root:root, 755
# 由 release.py preflight() 调用
#
# 功能：在隔离容器内运行候选源码测试
# 安全：
# - 无生产挂载
# - 降权运行（非 root）
# - cap-drop ALL
# - no-new-privileges
# - 资源限制（384MB memory, 0.5 CPU, 100 pids）
# - 只读源码
# - 临时数据目录
# - 固定镜像（python:3.6-slim）

set -euo pipefail

# 参数
SOURCE_DIR="${1:-}"
DATA_DIR="${2:-}"

# 验证参数
if [ -z "$SOURCE_DIR" ] || [ -z "$DATA_DIR" ]; then
    echo "用法: $0 <source-dir> <data-dir>" >&2
    exit 1
fi

# 验证路径存在
if [ ! -d "$SOURCE_DIR" ]; then
    echo "错误: 源码目录不存在: $SOURCE_DIR" >&2
    exit 1
fi

if [ ! -d "$DATA_DIR" ]; then
    echo "错误: 数据目录不存在: $DATA_DIR" >&2
    exit 1
fi

# 容器名（唯一）
CONTAINER_NAME="inspect-preflight-test-$RANDOM-$$"

# 清理函数
cleanup() {
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
}
trap cleanup EXIT

# 运行隔离测试容器
echo "启动隔离测试容器: $CONTAINER_NAME"

# Docker 安全选项：
# --read-only: 容器文件系统只读
# --tmpfs /tmp: 临时文件系统（noexec, nosuid）
# --network none: 无网络访问
# --cap-drop ALL: 移除所有 capabilities
# --security-opt no-new-privileges: 禁止提权
# --memory 384m: 内存限制
# --cpus 0.5: CPU 限制
# --pids-limit 100: 进程数限制
# --user 1000:1000: 非 root 用户

docker run --rm \
  --name "$CONTAINER_NAME" \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --network none \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --memory 384m \
  --cpus 0.5 \
  --pids-limit 100 \
  --user 1000:1000 \
  -v "${SOURCE_DIR}:/src:ro" \
  -v "${DATA_DIR}:/data:rw" \
  -w /src \
  python:3.6-slim \
  python3 -m unittest discover -s tests -p "test_*.py" -v

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ 候选测试通过"
else
    echo "✗ 候选测试失败: exit $EXIT_CODE" >&2
fi

exit $EXIT_CODE
