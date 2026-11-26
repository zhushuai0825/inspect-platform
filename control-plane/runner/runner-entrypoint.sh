#!/bin/bash
# Runner容器入口脚本
# 功能：执行候选测试（只运行白名单内的tests/）

set -euo pipefail

CANDIDATE_SRC="${1:?Missing candidate source}"

cd "$CANDIDATE_SRC"

# 检查tests目录存在
if [ ! -d "tests" ]; then
    echo "ERROR: tests/ directory not found in candidate" >&2
    exit 1
fi

# 执行unittest discover（只测试白名单内的tests/）
# 只运行 test_notifications.py 和 test_platform.py（白名单）
python3 -m unittest discover -s tests -p "test_notifications.py" -v
python3 -m unittest discover -s tests -p "test_platform.py" -v

# 返回码：0=通过，非0=失败
