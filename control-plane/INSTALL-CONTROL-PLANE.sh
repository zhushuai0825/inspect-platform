#!/bin/bash
# 控制面安装脚本
# 执行：sudo bash INSTALL-CONTROL-PLANE.sh
# 在 control-plane/ 目录中运行。
#
# 不 systemctl enable/start，不创建 Jenkins Job，不读取 data/secret.env。
# Docker 镜像只在 docker build 退出码为 0 时才算建成；本脚本不会把跳过说成成功。

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Must run as root (use sudo)" >&2
    exit 1
fi

cd "$(dirname "$0")"

echo "=== 控制面安装 ==="

if ! getent group inspect-deploy >/dev/null; then
    groupadd --system inspect-deploy
fi
if ! getent passwd inspect-dispatcher >/dev/null; then
    useradd --system --gid inspect-deploy --home-dir /var/lib/inspect-deploy \
        --shell /usr/sbin/nologin inspect-dispatcher
fi

install -d -o root -g root -m 0755 /opt/inspect-deploy/bin
install -d -o root -g root -m 0755 /opt/inspect-deploy/config
install -d -o root -g root -m 0755 /opt/inspect-deploy/runner
install -d -o root -g root -m 0755 /opt/inspect-deploy/systemd
install -d -o root -g inspect-deploy -m 0750 /var/lib/inspect-deploy
install -d -o inspect-dispatcher -g inspect-deploy -m 0750 /var/lib/inspect-deploy/work
install -d -o root -g root -m 0700 /var/lib/inspect-deploy/backups
install -d -o root -g root -m 0755 /var/lib/inspect-deploy/runner-stage
install -d -o root -g root -m 0755 /opt/inspect-platform
# 0711：dispatcher 能按路径进入 spool，不能列出 data 里的秘密文件名以外的需求；
# 秘密文件本身保持 root:root 0600，本脚本不读取、不放宽它们。
install -d -o root -g root -m 0711 /opt/inspect-platform/data
install -d -o inspect-dispatcher -g inspect-deploy -m 0750 /opt/inspect-platform/data/deploy-spool
install -d -o root -g root -m 0755 /opt/inspect-ops
install -d -o root -g root -m 0700 /opt/inspect-ops/state

install -o root -g root -m 0755 bin/run-candidate-tests /opt/inspect-deploy/bin/run-candidate-tests
install -o root -g root -m 0755 bin/inspect-release /opt/inspect-deploy/bin/inspect-release
install -o root -g root -m 0755 bin/stage_runner.py /opt/inspect-deploy/bin/stage_runner.py

if [ -f ../ops/release.py ]; then
    install -o root -g root -m 0755 ../ops/release.py /opt/inspect-deploy/bin/release.py
else
    echo "WARNING: ops/release.py not found, skipping" >&2
fi
if [ -f ../deploy-scripts/host-dispatcher/dispatcher.py ]; then
    install -o root -g root -m 0755 ../deploy-scripts/host-dispatcher/dispatcher.py /opt/inspect-deploy/bin/dispatcher.py
else
    echo "WARNING: dispatcher.py not found, skipping" >&2
fi

install -o root -g inspect-deploy -m 0640 config/deploy-config.json /opt/inspect-deploy/config/deploy-config.json
install -o root -g root -m 0644 ../deploy-scripts/host-dispatcher/inspect-deploy-dispatcher.service \
    /opt/inspect-deploy/systemd/inspect-deploy-dispatcher.service

cp runner/* /opt/inspect-deploy/runner/
chown -R root:root /opt/inspect-deploy/runner
chmod 755 /opt/inspect-deploy/runner
chmod 755 /opt/inspect-deploy/runner/*.sh
chmod 644 /opt/inspect-deploy/runner/Dockerfile.runner

# sudoers 只包含固定 helper。校验失败就删掉，不留半份规则。
install -o root -g root -m 0440 \
    ../deploy-scripts/host-dispatcher/sudoers-inspect-deploy /etc/sudoers.d/inspect-deploy
if ! visudo -cf /etc/sudoers; then
    rm -f /etc/sudoers.d/inspect-deploy
    echo "ERROR: sudoers rejected" >&2
    exit 1
fi

# 不复制到 /etc/systemd，不 daemon-reload，不 enable。操作者自行安装 unit。
echo "Unit 已放到 /opt/inspect-deploy/systemd/ （未启用）"

if command -v docker >/dev/null 2>&1; then
    echo "Building inspect-platform-runner:v1..."
    if docker build -t inspect-platform-runner:v1 -f /opt/inspect-deploy/runner/Dockerfile.runner /opt/inspect-deploy/runner; then
        echo "Runner image build exited 0"
    else
        echo "ERROR: docker build failed; image is not built" >&2
        exit 1
    fi
else
    echo "WARNING: docker not found; runner image was NOT built" >&2
fi

echo "=== 文件权限 ==="
ls -la /opt/inspect-deploy/bin
ls -la /opt/inspect-deploy/config
echo "未启动服务，未创建 Jenkins Job。"
