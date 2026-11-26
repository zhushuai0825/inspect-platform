#!/bin/bash
# 控制面安装脚本
# 执行：sudo bash INSTALL-CONTROL-PLANE.sh
# 在 control-plane/ 目录中运行。测试可设置绝对路径 INSTALL_ROOT，只改该前缀下的副本。
# INSTALL_ROOT 不能是 /opt/inspect-platform、/opt/inspect-ops、/opt/inspect-deploy、
# /var/lib/inspect-deploy、/etc/sudoers.d 本身、它们的子路径，或它们的父路径（例如 /opt）。
# 带 INSTALL_ROOT 的安装不创建宿主账户。不带 INSTALL_ROOT 的正式安装仍会创建
# inspect-deploy 组和 inspect-dispatcher 用户。
#
# 不 systemctl enable/start，不创建 Jenkins Job，不读取 data/secret.env，
# 不把文件拷进 Jenkins 家目录或 /opt/inspect-ops/scripts。
# Docker 镜像只在 docker build 退出码为 0 时才算建成；本脚本不会把跳过说成成功。
#
# 已经存在的生产目录只检查，不 install -d、不 chown、不 chmod：
#   /opt/inspect-platform
#   /opt/inspect-platform/data
#   /opt/inspect-ops
#   /opt/inspect-ops/state（仅当它已经存在）
# 不符合下面的前提就失败退出，这些目录保持原样。
# spool 只建在 /var/lib/inspect-deploy/spool，不建在应用 data 下。

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Must run as root (use sudo)" >&2
    exit 1
fi

INSTALL_ROOT="${INSTALL_ROOT:-}"
if [ -n "$INSTALL_ROOT" ] && [ "$INSTALL_ROOT" != "/" ]; then
    case "$INSTALL_ROOT" in
        /*) ;;
        *)
            echo "ERROR: INSTALL_ROOT must be an absolute path" >&2
            exit 1
            ;;
    esac
    RESOLVED_ROOT=$(readlink -f "$INSTALL_ROOT")
    for protected in \
        /opt/inspect-platform \
        /opt/inspect-ops \
        /opt/inspect-deploy \
        /var/lib/inspect-deploy \
        /etc/sudoers.d
    do
        case "$RESOLVED_ROOT" in
            "$protected"|"$protected"/*)
                echo "ERROR: INSTALL_ROOT resolves inside ${protected}. Leaving it unchanged." >&2
                exit 1
                ;;
        esac
        case "$protected" in
            "$RESOLVED_ROOT"|"$RESOLVED_ROOT"/*)
                echo "ERROR: INSTALL_ROOT ${RESOLVED_ROOT} is a parent of ${protected}. Refusing this prefix. Leaving it unchanged." >&2
                exit 1
                ;;
        esac
    done
fi

p() {
    case "$1" in
        /*) ;;
        *)
            echo "ERROR: path must be absolute: $1" >&2
            exit 1
            ;;
    esac
    if [ -z "$INSTALL_ROOT" ] || [ "$INSTALL_ROOT" = "/" ]; then
        printf '%s' "$1"
    else
        printf '%s%s' "${INSTALL_ROOT%/}" "$1"
    fi
}

fail_closed() {
    echo "ERROR: $1 Leaving it unchanged." >&2
    exit 1
}

is_world_writable() {
    local mode last
    mode=$(stat -c '%a' "$1")
    last=${mode: -1}
    case "$last" in
        2|3|6|7) return 0 ;;
        *) return 1 ;;
    esac
}

META=$(mktemp -d)
trap 'rm -rf "$META"' EXIT

capture() {
    local name="$1"
    local path="$2"
    if [ -e "$path" ] || [ -L "$path" ]; then
        stat -c '%u %g %a' "$path" > "$META/$name"
    else
        echo absent > "$META/$name"
    fi
}

assert_same() {
    local name="$1"
    local path="$2"
    local now was
    if [ -e "$path" ] || [ -L "$path" ]; then
        now=$(stat -c '%u %g %a' "$path")
    else
        now=absent
    fi
    was=$(cat "$META/$name")
    if [ "$now" != "$was" ]; then
        echo "ERROR: $path metadata changed from [$was] to [$now]. Refusing to leave a modified production directory." >&2
        exit 1
    fi
}

PLATFORM=$(p /opt/inspect-platform)
DATA=$(p /opt/inspect-platform/data)
DATA_SPOOL=$(p /opt/inspect-platform/data/deploy-spool)
OPS=$(p /opt/inspect-ops)
STATE=$(p /opt/inspect-ops/state)

capture platform "$PLATFORM"
capture data "$DATA"
capture data_spool "$DATA_SPOOL"
capture ops "$OPS"
capture state "$STATE"

check_app_dir() {
    local logical="$1"
    local path="$2"
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        return 0
    fi
    if [ -L "$path" ]; then
        fail_closed "$logical is a symlink. Prerequisite: a real directory, not world-writable."
    fi
    if [ ! -d "$path" ]; then
        fail_closed "$logical is not a directory. Prerequisite: a real directory, not world-writable."
    fi
    if is_world_writable "$path"; then
        local owner mode
        owner=$(stat -c '%u:%g' "$path")
        mode=$(stat -c '%a' "$path")
        fail_closed "$logical is $owner mode $mode. Prerequisite: not world-writable. This installer does not chown or chmod it."
    fi
}

check_locked_dir() {
    local logical="$1"
    local path="$2"
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        return 0
    fi
    if [ -L "$path" ]; then
        fail_closed "$logical is a symlink. Prerequisite: root:root mode 0700 directory."
    fi
    if [ ! -d "$path" ]; then
        fail_closed "$logical is not a directory. Prerequisite: root:root mode 0700 directory."
    fi
    local owner mode
    owner=$(stat -c '%u:%g' "$path")
    mode=$(stat -c '%a' "$path")
    if [ "$owner" != "0:0" ] || [ "$mode" != "700" ]; then
        fail_closed "$logical is $owner mode $mode. Prerequisite: root:root mode 0700. This installer does not chown or chmod it."
    fi
}

check_app_dir /opt/inspect-platform "$PLATFORM"
check_app_dir /opt/inspect-platform/data "$DATA"

if [ -e "$DATA_SPOOL" ] || [ -L "$DATA_SPOOL" ]; then
    fail_closed "/opt/inspect-platform/data/deploy-spool exists. Spool is not created under application data, and this path is not removed or chmodded."
fi

check_locked_dir /opt/inspect-ops "$OPS"
check_locked_dir /opt/inspect-ops/state "$STATE"

# 只在运维目录已经符合 0700 且 state 还不存在时创建 state 本身。
# 不用 install -d，避免改到已经存在的 /opt/inspect-ops。
if [ -d "$OPS" ] && [ ! -L "$OPS" ] && [ ! -e "$STATE" ] && [ ! -L "$STATE" ]; then
    mkdir -m 0700 "$STATE"
    chown root:root "$STATE"
    chmod 0700 "$STATE"
fi

cd "$(dirname "$0")"

echo "=== 控制面安装 ==="

PREFIX_INSTALL=0
if [ -n "$INSTALL_ROOT" ] && [ "$INSTALL_ROOT" != "/" ]; then
    PREFIX_INSTALL=1
fi

# 前缀安装只写 INSTALL_ROOT 下面的副本，不改宿主账户库。
# 正式安装（没有 INSTALL_ROOT）仍然创建服务账户。
if [ "$PREFIX_INSTALL" -eq 0 ]; then
    if ! getent group inspect-deploy >/dev/null; then
        groupadd --system inspect-deploy
    fi
    if ! getent passwd inspect-dispatcher >/dev/null; then
        useradd --system --gid inspect-deploy --home-dir /var/lib/inspect-deploy \
            --shell /usr/sbin/nologin inspect-dispatcher
    fi
fi

if getent group inspect-deploy >/dev/null && getent passwd inspect-dispatcher >/dev/null; then
    RUN_USER=inspect-dispatcher
    RUN_GROUP=inspect-deploy
elif [ "$PREFIX_INSTALL" -eq 0 ]; then
    echo "ERROR: service accounts were not created" >&2
    exit 1
else
    echo "WARNING: prefix install did not create host accounts; prefixed files are root-owned" >&2
    RUN_USER=root
    RUN_GROUP=root
fi

DEPLOY=$(p /opt/inspect-deploy)
VARLIB=$(p /var/lib/inspect-deploy)

install -d -o root -g root -m 0755 "$DEPLOY/bin"
install -d -o root -g root -m 0755 "$DEPLOY/config"
install -d -o root -g root -m 0755 "$DEPLOY/runner"
install -d -o root -g root -m 0755 "$DEPLOY/systemd"
install -d -o root -g "$RUN_GROUP" -m 0750 "$VARLIB"
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0750 "$VARLIB/work"
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0750 "$VARLIB/log"
install -d -o root -g root -m 0700 "$VARLIB/backups"
install -d -o root -g root -m 0755 "$VARLIB/runner-stage"
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0750 "$VARLIB/spool"

install -o root -g root -m 0755 bin/run-candidate-tests "$DEPLOY/bin/run-candidate-tests"
install -o root -g root -m 0755 bin/inspect-release "$DEPLOY/bin/inspect-release"
install -o root -g root -m 0755 bin/stage_runner.py "$DEPLOY/bin/stage_runner.py"

if [ -f ../ops/release.py ]; then
    install -o root -g root -m 0755 ../ops/release.py "$DEPLOY/bin/release.py"
else
    echo "WARNING: ops/release.py not found, skipping" >&2
fi
if [ -f ../deploy-scripts/host-dispatcher/dispatcher.py ]; then
    install -o root -g root -m 0755 ../deploy-scripts/host-dispatcher/dispatcher.py "$DEPLOY/bin/dispatcher.py"
else
    echo "WARNING: dispatcher.py not found, skipping" >&2
fi

install -o root -g "$RUN_GROUP" -m 0640 config/deploy-config.json "$DEPLOY/config/deploy-config.json"
install -o root -g root -m 0644 ../deploy-scripts/host-dispatcher/inspect-deploy-dispatcher.service \
    "$DEPLOY/systemd/inspect-deploy-dispatcher.service"

cp runner/* "$DEPLOY/runner/"
chown -R root:root "$DEPLOY/runner"
chmod 755 "$DEPLOY/runner"
chmod 755 "$DEPLOY"/runner/*.sh
chmod 644 "$DEPLOY/runner/Dockerfile.runner"

SUDOERS_DST=$(p /etc/sudoers.d/inspect-deploy)
if [ ! -d "$(dirname "$SUDOERS_DST")" ]; then
    install -d -o root -g root -m 0755 "$(dirname "$SUDOERS_DST")"
fi
install -o root -g root -m 0440 \
    ../deploy-scripts/host-dispatcher/sudoers-inspect-deploy "$SUDOERS_DST"
if [ -z "$INSTALL_ROOT" ] || [ "$INSTALL_ROOT" = "/" ]; then
    if ! visudo -cf /etc/sudoers; then
        rm -f "$SUDOERS_DST"
        echo "ERROR: sudoers rejected" >&2
        exit 1
    fi
else
    if ! visudo -cf "$SUDOERS_DST"; then
        rm -f "$SUDOERS_DST"
        echo "ERROR: sudoers rejected" >&2
        exit 1
    fi
fi

echo "Unit 已放到 $DEPLOY/systemd/ （未复制到 /etc/systemd，未 enable，未启动）"

if [ -n "$INSTALL_ROOT" ] && [ "$INSTALL_ROOT" != "/" ]; then
    echo "WARNING: INSTALL_ROOT is set; docker image was NOT built" >&2
elif command -v docker >/dev/null 2>&1; then
    echo "Building inspect-platform-runner:v1..."
    if docker build -t inspect-platform-runner:v1 -f "$DEPLOY/runner/Dockerfile.runner" "$DEPLOY/runner"; then
        echo "Runner image build exited 0"
    else
        echo "ERROR: docker build failed; image is not built" >&2
        exit 1
    fi
else
    echo "WARNING: docker not found; runner image was NOT built" >&2
fi

assert_same platform "$PLATFORM"
assert_same data "$DATA"
assert_same data_spool "$DATA_SPOOL"
assert_same ops "$OPS"
if [ "$(cat "$META/state")" != "absent" ]; then
    assert_same state "$STATE"
fi

echo "=== 文件权限 ==="
ls -la "$DEPLOY/bin"
ls -la "$DEPLOY/config"
echo "Spool: $VARLIB/spool"
echo "未启动服务，未创建 Jenkins Job，未修改已检查过的生产目录。"
