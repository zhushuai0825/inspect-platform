#!/bin/bash
# 宿主部署包装脚本
# 部署位置：/opt/inspect-ops/deploy-wrapper.sh (宿主机)
# 权限：root:root, 755
# 调用方式：从 Jenkins 容器通过 docker exec 到宿主
#
# 安全要求：
# 1. 仅接受 main 分支制品
# 2. 验证 artifact 路径合法
# 3. 使用固定的 release.py（不执行 repo 代码）
# 4. sudo 执行部署（需配置 sudoers）

set -euo pipefail

# === 参数解析 ===
ARTIFACT=""
COMMIT=""
BRANCH=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --artifact) ARTIFACT="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

# === 验证参数 ===
if [[ -z "$ARTIFACT" || -z "$COMMIT" || -z "$BRANCH" ]]; then
  echo "❌ 错误：缺少必需参数"
  echo "用法: $0 --artifact <path> --commit <sha> --branch <name>"
  exit 1
fi

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  部署安全检查                                                ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# === 安全检查 1：分支验证 ===
if [[ "$BRANCH" != "origin/main" && "$BRANCH" != "main" && "$BRANCH" != "refs/heads/main" ]]; then
  echo "❌ 安全拒绝：仅 main 分支允许部署"
  echo "   当前分支: $BRANCH"
  exit 1
fi
echo "✓ 分支验证通过: $BRANCH"

# === 安全检查 2：制品路径验证 ===
# Jenkins workspace 在容器内：/var/jenkins_home/workspace/inspect-platform-deploy
# 映射到宿主：<需根据实际挂载配置>
# 此处需要根据实际容器挂载情况调整路径验证

if [[ ! -f "$ARTIFACT" ]]; then
  echo "❌ 安全拒绝：制品文件不存在"
  echo "   路径: $ARTIFACT"
  exit 1
fi
echo "✓ 制品存在: $ARTIFACT"

# === 安全检查 3：制品完整性 ===
if [[ ! -f "${ARTIFACT}.sha256" ]]; then
  echo "❌ 安全拒绝：制品哈希文件不存在"
  echo "   路径: ${ARTIFACT}.sha256"
  exit 1
fi

echo "验证制品完整性..."
cd "$(dirname "$ARTIFACT")"
if ! sha256sum -c "$(basename "${ARTIFACT}.sha256")"; then
  echo "❌ 制品完整性校验失败"
  exit 1
fi
echo "✓ 制品完整性校验通过"

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  安全检查通过，开始部署                                      ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "分支: $BRANCH"
echo "提交: $COMMIT"
echo "制品: $ARTIFACT"
echo ""

# === 生成版本号 ===
VERSION="v$(date +%Y%m%d-%H%M%S)-${COMMIT:0:8}"
echo "部署版本: $VERSION"
echo ""

# === 准备发布 ===
echo "=== 准备发布 $VERSION ==="
# 注意：需要配置 sudoers 允许执行以下命令
# 示例：jenkins_user ALL=(root) NOPASSWD: /usr/bin/python3 /opt/inspect-platform/ops/release.py prepare *
sudo /usr/bin/python3 /opt/inspect-platform/ops/release.py prepare "$ARTIFACT" "$VERSION"

echo ""
echo "=== 执行部署 $VERSION ==="
# 这会：
# - 停止服务
# - 备份数据到 /root/backups/inspect-release-data-<version>/
# - 安装新代码
# - 启动服务
# - 健康检查
# - 失败则自动回滚
sudo /usr/bin/python3 /opt/inspect-platform/ops/release.py deploy "$VERSION"

echo ""
echo "=== 部署状态 ==="
sudo /usr/bin/python3 /opt/inspect-platform/ops/release.py status

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  ✓ 部署成功                                                   ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "版本: $VERSION"
echo "提交: $COMMIT"
echo "时间: $(date -Iseconds)"
echo ""
echo "审计日志: /opt/inspect-ops/state/audit.jsonl"
echo "数据备份: /root/backups/inspect-release-data-${VERSION}/"
