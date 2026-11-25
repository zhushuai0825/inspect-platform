# Testing Guide

## 依赖要求

### 必需依赖
- **Python**: 3.6+ (推荐 3.11+)
- **Git**: 2.0+（完整链路测试必需）

### 可选依赖
- **Docker**: 用于容器化测试

---

## 运行测试

### 1. 本地测试（需要 Git）

```bash
# 完整测试套件
python3 -m unittest discover -s tests -p "test_*.py" -v

# 单独测试组
python3 -m unittest tests.test_e2e_local -v          # Mock E2E (3项)
python3 -m unittest tests.test_e2e_full_chain -v     # 真实链路 (3项)
python3 -m unittest tests.test_path_security -v      # 路径安全 (6项)

python3 -m unittest ops.test_release -v              # Ops Mock (17项)
python3 -m unittest ops.test_release_cli -v          # Mock CLI (5项, 1 skip)
python3 -m unittest ops.test_release_cli_real -v     # 真实 CLI (5项)
```

### 2. Docker 测试

```bash
# 构建镜像（240秒限制）
docker build -f Dockerfile.test -t inspect-platform-test:latest .

# 运行测试
docker run --rm inspect-platform-test:latest

# 或运行特定测试
docker run --rm inspect-platform-test:latest \
    python3 -m unittest tests.test_e2e_local -v
```

---

## 测试统计

### 预期结果（Python 3.6-3.12）

| 测试组 | 数量 | 状态 |
|--------|------|------|
| Platform Tests | 84 | passed |
| - Mock E2E | 3 | passed |
| - 真实链路 | 3 | passed (需要 Git) |
| - 路径安全 | 6 | passed |
| Ops Mock | 17 | passed |
| Mock CLI | 5 | passed (1 skip) |
| Real CLI | 5 | passed |
| **总计** | **111** | **passed (1 skip)** |

**Root 相关测试**（2项）：
- 非 root 环境自动跳过
- Root 环境实际执行并验证拒绝测试覆盖

---

## 依赖缺失检测

测试启动时自动检查：

```bash
# 检查 Git（完整链路测试需要）
if ! command -v git &> /dev/null; then
    echo "警告: Git 未安装，部分测试将跳过"
fi

# 检查 Python 版本
python3 --version
```

**缺失 Git 的影响**：
- Mock E2E (3项): ✅ 通过（不需要 Git）
- 真实链路 (3项): ❌ 失败（需要 Git）
- 其他测试: ✅ 通过

---

## Dockerfile 说明

### Dockerfile.test

```dockerfile
FROM python:3.11-slim

# 安装 Git
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# 验证依赖
RUN python3 --version && git --version

WORKDIR /workspace
COPY . .

# 启动检查
RUN python3 -c "import sys; print('Python', sys.version)" && \
    git --version && \
    echo "✓ Dependencies verified"

CMD ["python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"]
```

**构建时间**: ~30-60秒（取决于网络）  
**镜像大小**: ~200MB

---

## 故障排除

### Git 相关错误

```
subprocess.CalledProcessError: Command '['git', ...]' returned non-zero exit status
```

**原因**: Git 未安装  
**解决**: 安装 Git 或跳过真实链路测试

### 路径权限错误

```
PermissionError: [Errno 13] Permission denied: '/opt/inspect-platform'
```

**原因**: 测试尝试访问生产路径  
**解决**: 检查测试代码，确保使用临时目录

### Mock 注入失败

```
release.py 不存在: /opt/inspect-platform/ops/release.py
```

**原因**: Mock 未正确设置环境变量  
**解决**: 检查 setUp() 是否设置 RELEASE_ROOT, RELEASE_STATE, RELEASE_CLI

---

## 环境变量（测试专用）

| 变量 | 用途 | 示例 |
|------|------|------|
| `RELEASE_ROOT` | 测试 root 目录 | `/tmp/test-root` |
| `RELEASE_STATE` | 测试 state 目录 | `/tmp/test-state` |
| `RELEASE_CLI` | 测试 release.py 路径 | `/tmp/fake-release.py` |
| `SPOOL_DIR` | Spool 目录覆盖 | `/tmp/test-spool` |

**注意**: Root 进程永远拒绝这些环境变量。
