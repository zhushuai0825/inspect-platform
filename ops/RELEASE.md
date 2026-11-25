# 私有发布与回滚运维手册

## 当前里程碑与边界

本工具服务于服务器现存平台的部署运维依赖；还没有 GitHub 仓库，未配置提交触发或仓库流水线。本轮只安装工具、登记当前版本、准备候选制品和隔离演练，**不切换生产代码、不重启生产服务、不在生产故意制造故障**。不改 nginx、个人主页、Jenkins 配置、QQ 发信配置、任务或调度。

- 生产应用仍在 `/opt/inspect-platform`，原 systemd 路径不变。
- 私有状态、只含源码的版本快照与发布日志位于 `/opt/inspect-ops/state`，目录700。
- `/opt/inspect-platform/data` 始终是持久数据，不进入源码制品，代码发布/回滚从不覆盖它。
- CLI 仅限 root SSH 操作，无新增网络监听、HTTP 发布接口、密码或网页登录。

## 制品和发布过程

接受根目录为 `inspect-platform/` 的 ZIP，按固定文件白名单校验。拒绝 data/、.env、未知文件、路径穿越、符号链接、重复项、超大条目和不完整制品；总解压量最多4MiB。源码 SHA256 清单校验后预检 Python 语法，并在临时 QA_DATA_DIR 下运行应用测试，子进程环境不继承生产秘密。

每次发布串行执行，生成不可覆盖的版本目录、当前源码快照和持久事务日志。检查巡检运行锁；有活动巡检或待发/发送中邮件则保守中止。暂停相关 systemd 服务后，将运行数据备份到 root 私有目录，再按白名单原子替换源码文件。先启动前台、验证主页/简历/巡检 API/Jenkins 认证边界，再启动 worker 和邮件服务。密钥文件摘要仅在进程内比较，日志只记录是否保持不变。

任一安装/健康步骤失败自动恢复发布前源码，重新启动并健康验证；**从不自动还原数据库、报告、邮件配置或运行目录**，防止丢失新数据。事务中断会拒绝后续发布，必须显式 recover。回滚自身失败明确记录 rollback_failed，需要人工排查，不谎报恢复成功。只读 status 不触发服务动作。

## 私有命令

以下必须在服务器 SSH 会话执行，不包含口令。版本号只允许字母数字及 ._-，最长64位。

```sh
sudo python3 /opt/inspect-platform/ops/release.py status
# 当前基线仅第一次登记，不能重复覆盖：
sudo python3 /opt/inspect-platform/ops/release.py baseline v20261125-mail-enabled
# 校验、临时测试和准备制品，不切换生产、不重启：
sudo python3 /opt/inspect-platform/ops/release.py prepare /path/to/inspect-platform-source.zip vNEXT
# 以下会造成短暂维护窗口，须在获准窗口内操作；本轮未在生产执行：
sudo python3 /opt/inspect-platform/ops/release.py deploy vNEXT
# 只允许回滚到已准备/已登记的版本；不得借此覆盖运行数据：
sudo python3 /opt/inspect-platform/ops/release.py rollback vKNOWN
# 仅用于中断事务，先查看 status 和私有日志，确认后执行：
sudo python3 /opt/inspect-platform/ops/release.py recover
```

不要在运行中手改已登记版本目录；修改会导致 SHA256 校验失败。发布锁在进程退出后自动释放，不代表中断事务自动成功；应以事务状态为准。不得删除版本以绕过保护。

## 隔离验证

```sh
cd /opt/inspect-platform/ops
python3 -m unittest test_release -v
```

17项运维测试全部使用临时应用/状态/数据和动态回环HTTP端口，不调用生产 systemctl、不访问生产目标、无SMTP连接。覆盖正常发布、HTTP503失败自动回滚、显式已知版本回退、崩溃事务恢复、回滚失败保护、并行互斥、忙碌中止、校验篡改、预检失败、穿越/私有文件/符号链接/重复条目/体积限制与数据保持。另运行现有50项应用测试。

## 数据、配置与升级限制

- 私有备份可能含真实凭证，仅保存在 `/root/backups` 700目录，归档600；它们不是源码包，不能上传GitHub或发到公开页面。
- 运行数据备份在真正发布暂停服务后生成，位于 `inspect-release-data-*/data.tar.gz`；仅供人工灾难恢复，不自动恢复。
- 目前只支持兼容旧版本的代码/附加式schema更新；破坏性数据库迁移不属于本工具能力，须另做迁移与恢复设计。
- `deploy/*.service` 只是模板，工具不改 `/etc/systemd/system`；不部署 nginx、Jenkins Job、宿主机软件或依赖。ops 工具自身也不随应用发布自动替换，升级需单独评审安装。
- 这是有短暂维护窗口的进程级发布，不是零停机蓝绿部署。异常断电由事务日志辅助人工 recover，不宣称自动跨断电恢复。

## 后续仓库接入

需要父代理提供用户新建的专用私有仓库URL、默认分支和允许部署的分支/环境。后续 CloudAgent 导入脱敏源码与ops文档，增加仓库CI、制品打包/哈希、受限部署身份或本机拉取方案、并发控制和审核门禁；凭证只能放受保护凭证存储，不进入仓库。当前未创建/修改任何仓库，未添加公网webhook或任意命令接口。
