#!/bin/bash
# 控制面安装脚本
# 执行：sudo bash INSTALL-CONTROL-PLANE.sh
#
# 功能：
# 1. 创建 /opt/inspect-deploy/ 目录结构
# 2. 安装控制面文件（release.py, dispatcher.py, runner）
# 3. 设置权限（root:root, 不可覆盖）
# 4. 构建runner镜像
# 5. 验证安装

set -euo pipefail

# 检查root权限
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Must run as root (use sudo)" >&2
    exit 1
fi

echo "=== 控制面安装 ==="
echo ""

# 1. 创建目录结构
echo "[1/6] 创建 /opt/inspect-deploy/ 目录..."
mkdir -p /opt/inspect-deploy/{bin,config,runner,systemd}
chown -R root:root /opt/inspect-deploy
chmod 755 /opt/inspect-deploy

# 2. 安装bin文件
echo "[2/6] 安装控制面二进制文件..."
cp bin/run-candidate-tests /opt/inspect-deploy/bin/
chown root:root /opt/inspect-deploy/bin/run-candidate-tests
chmod 755 /opt/inspect-deploy/bin/run-candidate-tests

# 注意：release.py和dispatcher.py需要从main repo复制
if [ ! -f "../ops/release.py" ]; then
    echo "WARNING: ops/release.py not found, skipping" >&2
else
    cp ../ops/release.py /opt/inspect-deploy/bin/release.py
    chown root:root /opt/inspect-deploy/bin/release.py
    chmod 755 /opt/inspect-deploy/bin/release.py
fi

if [ ! -f "../deploy-scripts/host-dispatcher/dispatcher.py" ]; then
    echo "WARNING: dispatcher.py not found, skipping" >&2
else
    cp ../deploy-scripts/host-dispatcher/dispatcher.py /opt/inspect-deploy/bin/dispatcher.py
    chown root:root /opt/inspect-deploy/bin/dispatcher.py
    chmod 755 /opt/inspect-deploy/bin/dispatcher.py
fi

# 3. 安装配置
echo "[3/6] 安装固定配置..."
cp config/deploy-config.json /opt/inspect-deploy/config/
chown root:root /opt/inspect-deploy/config/deploy-config.json
chmod 644 /opt/inspect-deploy/config/deploy-config.json

# 4. 安装runner文件
echo "[4/6] 安装runner文件..."
cp runner/* /opt/inspect-deploy/runner/
chown -R root:root /opt/inspect-deploy/runner
chmod 755 /opt/inspect-deploy/runner/*.sh

# 5. 构建runner镜像
echo "[5/6] 构建runner镜像..."
cd /opt/inspect-deploy/runner/

if command -v docker &> /dev/null; then
    echo "Building inspect-platform-runner:v1..."
    docker build -t inspect-platform-runner:v1 -f Dockerfile.runner .
    echo "✓ Runner镜像构建成功"
else
    echo "WARNING: Docker not found, skipping runner build" >&2
    echo "Please manually build: docker build -t inspect-platform-runner:v1 -f /opt/inspect-deploy/runner/Dockerfile.runner /opt/inspect-deploy/runner/" >&2
fi

cd -

# 6. 验证
echo "[6/6] 验证安装..."
echo ""
echo "控制面文件："
ls -la /opt/inspect-deploy/bin/ | grep -E "release|dispatcher|run-candidate"
echo ""
echo "配置文件："
ls -la /opt/inspect-deploy/config/
echo ""

if command -v docker &> /dev/null; then
    echo "Runner镜像："
    docker image inspect inspect-platform-runner:v1 --format '{{.RepoTags}}' || echo "WARNING: Runner镜像未找到"
fi

echo ""
echo "=== 安装完成 ==="
echo ""
echo "下一步："
echo "1. 验证权限：ls -la /opt/inspect-deploy/bin/"
echo "2. 验证runner：docker image inspect inspect-platform-runner:v1"
echo "3. 测试runner：sudo /opt/inspect-deploy/bin/run-candidate-tests /path/to/candidate"
echo "4. 配置sudo：/etc/sudoers.d/inspect-deploy"
echo "5. 更新systemd service"
