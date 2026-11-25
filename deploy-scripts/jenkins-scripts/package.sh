#!/bin/bash
# 固定打包脚本 - 部署到 Jenkins 容器内
# 目标位置：/var/jenkins_home/jenkins-scripts/package.sh
# 权限：root:root, 755

set -euo pipefail

if [ $# -ne 2 ]; then
  echo "用法: $0 <WORKSPACE> <COMMIT>"
  exit 1
fi

WORKSPACE="$1"
COMMIT="$2"
BUILD_DIR="${WORKSPACE}/build"

echo "=== 打包制品 ==="
echo "工作空间: ${WORKSPACE}"
echo "提交: ${COMMIT}"

mkdir -p "${BUILD_DIR}"
ARTIFACT_NAME="inspect-platform-${COMMIT:0:8}.zip"

# 使用 Git archive 创建制品（Jenkins 容器有 Git）
cd "${WORKSPACE}"
git archive --format=zip --prefix=inspect-platform/ HEAD > "${BUILD_DIR}/${ARTIFACT_NAME}"

echo "制品已创建: ${BUILD_DIR}/${ARTIFACT_NAME}"
ls -lh "${BUILD_DIR}/${ARTIFACT_NAME}"

# 计算 SHA256（使用临时 Alpine 容器）
echo "计算 SHA256..."
docker run --rm \
  -v "${BUILD_DIR}:/data:rw" \
  --network none \
  --memory 128m \
  alpine:3.19 \
  sh -c "cd /data && sha256sum ${ARTIFACT_NAME} > ${ARTIFACT_NAME}.sha256 && cat ${ARTIFACT_NAME}.sha256"

echo "✓ 打包完成"
