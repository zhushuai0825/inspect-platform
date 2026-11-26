#!/bin/bash
# 固定部署触发脚本 - 部署到 Jenkins 容器内
# 目标位置：/var/jenkins_home/jenkins-scripts/trigger-deploy.sh
# 权限：root:root, 755

set -euo pipefail

if [ $# -ne 3 ]; then
  echo "用法: $0 <COMMIT> <BRANCH> <ARTIFACT>"
  exit 1
fi

COMMIT="$1"
BRANCH="$2"
ARTIFACT="$3"  # 容器内路径

echo "=== 触发部署 ==="
echo "提交: ${COMMIT}"
echo "分支: ${BRANCH}"
echo "制品: ${ARTIFACT}"

# 转换路径：容器内 → 宿主
# /var/jenkins_home/... → /var/lib/docker/volumes/jenkins_jenkins_home/_data/...
HOST_ARTIFACT="${ARTIFACT/\/var\/jenkins_home/\/var\/lib\/docker\/volumes\/jenkins_jenkins_home\/_data}"

echo "宿主路径: ${HOST_ARTIFACT}"

# Spool 在控制面。不要在应用 data 下创建目录。
SPOOL="${SPOOL_DIR:-/var/lib/inspect-deploy/spool}"
case "${SPOOL}" in
    /opt/inspect-platform/data|/opt/inspect-platform/data/*)
        echo "ERROR: spool must not be under /opt/inspect-platform/data: ${SPOOL}" >&2
        exit 1
        ;;
esac
mkdir -p "${SPOOL}"

TIMESTAMP=$(date +%s)
REQUEST_FILE="${SPOOL}/request-${TIMESTAMP}-${COMMIT:0:8}.json"
RESULT_FILE="${SPOOL}/result-${TIMESTAMP}-${COMMIT:0:8}.json"

cat > "${REQUEST_FILE}" <<EOF
{
  "commit": "${COMMIT}",
  "branch": "${BRANCH}",
  "artifact": "${HOST_ARTIFACT}",
  "artifact_sha256": "${HOST_ARTIFACT}.sha256",
  "timestamp": "$(date -Iseconds)",
  "source": "jenkins",
  "result_file": "${RESULT_FILE}"
}
EOF

echo "部署请求已写入: ${REQUEST_FILE}"
echo "等待 dispatcher 处理..."

# 等待结果（最多 10 分钟）
for i in {1..120}; do
  if [ -f "${RESULT_FILE}" ]; then
    echo "收到响应:"
    cat "${RESULT_FILE}"
    
    if command -v jq >/dev/null 2>&1; then
      STATUS=$(jq -r '.status' "${RESULT_FILE}")
    else
      # 简单解析（如无 jq）
      if grep -q '"status"[[:space:]]*:[[:space:]]*"success"' "${RESULT_FILE}"; then
        STATUS="success"
      else
        STATUS="failed"
      fi
    fi
    
    if [ "$STATUS" = "success" ]; then
      echo "✓ 部署成功"
      rm -f "${RESULT_FILE}"
      exit 0
    else
      echo "✗ 部署失败"
      rm -f "${RESULT_FILE}"
      exit 1
    fi
  fi
  sleep 5
done

echo "✗ 超时：未收到 dispatcher 响应"
exit 1
