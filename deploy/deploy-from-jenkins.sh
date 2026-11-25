#!/usr/bin/env bash
# 宿主机部署脚本 - 由 Jenkins 容器调用，但在宿主机执行
# 位置：/opt/inspect-ops/deploy-from-jenkins.sh（宿主机，不在 repo）
# 权限：root 拥有，755，不可被 Jenkins 容器修改
#
# 安全要求：
# 1. 本脚本固定在宿主机，不受 repo PR 影响
# 2. 仅接受 main 分支的制品
# 3. 验证制品完整性（SHA256）
# 4. 使用宿主机固定的 /opt/inspect-ops/release.py（不执行 repo 代码）
# 5. 拒绝执行任何来自 repo 的脚本

set -euo pipefail

# === 参数解析 ===
VERSION=""
COMMIT=""
BRANCH=""
ARTIFACT=""
ARTIFACT_SHA256=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --version) VERSION="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --artifact) ARTIFACT="$2"; shift 2 ;;
    --artifact-sha256) ARTIFACT_SHA256="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

# === 安全验证 ===
echo "=== 部署安全检查 ==="

# 1. 验证分支（仅 main）
if [[ "$BRANCH" != "main" ]]; then
  echo "❌ 安全拒绝：仅 main 分支允许部署"
  echo "   当前分支: $BRANCH"
  exit 1
fi

# 2. 验证必需参数
if [[ -z "$VERSION" || -z "$COMMIT" || -z "$ARTIFACT" || -z "$ARTIFACT_SHA256" ]]; then
  echo "❌ 缺少必需参数"
  echo "   VERSION: $VERSION"
  echo "   COMMIT: $COMMIT"
  echo "   ARTIFACT: $ARTIFACT"
  echo "   ARTIFACT_SHA256: $ARTIFACT_SHA256"
  exit 1
fi

# 3. 验证制品文件存在
if [[ ! -f "$ARTIFACT" ]]; then
  echo "❌ 制品文件不存在: $ARTIFACT"
  exit 1
fi

if [[ ! -f "$ARTIFACT_SHA256" ]]; then
  echo "❌ 制品哈希文件不存在: $ARTIFACT_SHA256"
  exit 1
fi

# 4. 验证制品完整性
echo "验证制品完整性..."
cd "$(dirname "$ARTIFACT")"
if ! sha256sum -c "$(basename "$ARTIFACT_SHA256")"; then
  echo "❌ 制品完整性校验失败"
  exit 1
fi

echo "✓ 安全检查通过"
echo "  分支: $BRANCH"
echo "  提交: $COMMIT"
echo "  版本: $VERSION"
echo "  制品: $ARTIFACT"

# === 部署执行 ===
echo ""
echo "=== 执行部署 ==="

# 检查固定的 release.py 是否存在
FIXED_RELEASE_PY="/opt/inspect-ops/release.py"
if [[ ! -f "$FIXED_RELEASE_PY" ]]; then
  echo "❌ 固定部署工具不存在: $FIXED_RELEASE_PY"
  echo "   请从最新 main 分支复制 ops/release.py 到 /opt/inspect-ops/"
  echo "   命令: sudo cp /opt/inspect-platform/ops/release.py /opt/inspect-ops/"
  exit 1
fi

# 准备发布（校验、临时测试、预检）
echo "准备发布 $VERSION..."
python3 "$FIXED_RELEASE_PY" prepare "$ARTIFACT" "$VERSION"

# 执行部署（停服、备份、安装、健康检查、自动回滚）
echo "部署 $VERSION..."
python3 "$FIXED_RELEASE_PY" deploy "$VERSION"

# 显示部署状态
echo ""
echo "=== 部署完成 ==="
python3 "$FIXED_RELEASE_PY" status

# 记录部署信息到审计日志
AUDIT_LOG="/opt/inspect-ops/state/audit.jsonl"
if [[ -f "$AUDIT_LOG" ]]; then
  echo "最近部署记录:"
  tail -5 "$AUDIT_LOG" | python3 -m json.tool || tail -5 "$AUDIT_LOG"
fi

echo ""
echo "✓ 部署成功: $VERSION"
echo "  提交: $COMMIT"
echo "  时间: $(date -Iseconds)"
