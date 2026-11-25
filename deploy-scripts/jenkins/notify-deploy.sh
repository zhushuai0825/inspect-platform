#!/bin/bash
# Jenkins 固定部署通知脚本
# 位置（生产）：/var/jenkins_home/jenkins-scripts/notify-deploy.sh
# 权限：root:root, 755
# 由 Jenkins Freestyle Job 调用（在容器内）
#
# 功能：只发送通知（request_id + commit）
# 安全：不传递 artifact/path/branch

set -euo pipefail

# 参数
COMMIT="${1:-}"

# 验证参数
if [ -z "$COMMIT" ]; then
    echo "用法: $0 <commit-sha>" >&2
    exit 1
fi

# 验证 commit 格式（40 hex）
if ! echo "$COMMIT" | grep -qE '^[0-9a-f]{40}$'; then
    echo "错误: commit 必须是 40 位十六进制" >&2
    exit 1
fi

# 生成请求 ID
REQUEST_ID="jenkins-$(date +%s)-${COMMIT:0:8}"

# Spool 目录（通过挂载的 /opt/inspect-platform）
# 测试可通过 SPOOL_DIR 环境变量覆盖
SPOOL="${SPOOL_DIR:-/opt/inspect-platform/data/deploy-spool}"

# 确保目录存在
mkdir -p "${SPOOL}"

# 写入通知文件
REQUEST_FILE="${SPOOL}/request-${REQUEST_ID}.json"

cat > "${REQUEST_FILE}" <<EOF
{
  "request_id": "${REQUEST_ID}",
  "commit": "${COMMIT}",
  "timestamp": "$(date -Iseconds)",
  "source": "jenkins"
}
EOF

echo "✓ 部署通知已写入: ${REQUEST_FILE}"
echo "  Request ID: ${REQUEST_ID}"
echo "  Commit: ${COMMIT:0:8}...${COMMIT:32:8}"
echo ""
echo "Dispatcher 将验证并部署"

exit 0
