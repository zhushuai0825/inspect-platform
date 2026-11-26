# 部署指南

**Main-Only Jenkins 自动化**（2026-11-25）

---

## 前提

- Jenkins 容器：root (uid=0)，Docker socket，挂载 `/opt/inspect-platform`
- 宿主：Python 3.6.8, Node 20.20.2, systemd
- 资源：809MB 可用

## 架构

```
GitHub main → Jenkins 轮询 → 固定脚本测试/打包 → 写入 spool → 宿主 dispatcher → 部署
```

详见 `ARCHITECTURE.md`

---

## 安装

控制面只按 `control-plane/INSTALL-CONTROL-PLANE.sh` 安装。该脚本不修改已经存在且不符合前提的生产目录，不在应用 data 下创建 spool，不 `systemctl enable` 或 start，不创建 Jenkins Job，也不把文件拷进 Jenkins 家目录或 `/opt/inspect-ops/scripts/`。

sudoers 只用 `deploy-scripts/host-dispatcher/sudoers-inspect-deploy`：`inspect-dispatcher` 只能 sudo 固定 helper `/opt/inspect-deploy/bin/inspect-release`。不要再写通配 release.py 规则。

下面这些旧步骤没有验收，不要执行：把脚本拷进 Jenkins 家目录、`systemctl enable` / `systemctl start`、在 Jenkins 里新建 Job、对生产地址做发布。spool 是 `/var/lib/inspect-deploy/spool`。

---

## 安全边界

1. **控制器不执行 repo 代码**：固定脚本在 root 拥有的目录
2. **测试隔离**：独立容器，只读 repo
3. **部署验证**：Dispatcher 验证分支和制品
4. **PR 禁用**：Job 仅轮询 main

---

## 限制

- 无 PR 自动测试（本里程碑明确禁用）
- 串行部署
- 短暂维护窗口
- 固定脚本需手动维护

---

## 文档日期

2026-11-25
