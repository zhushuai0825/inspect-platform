# 后续入库、测试与部署说明

## 源码包范围

包含应用源文件、前端、Jenkinsfile、systemd 模板、单元/集成测试。本包不含 data/、运行报告、数据库、凭证、服务器备份、SSH 信息、旧 bootstrap 登录脚本。入库前继续沿用 `.gitignore`，不要把服务器 data/ 提交到 Git。

## 本地验证

依赖：Python 3.6+ 标准库（含 sqlite3）与 Node（仅 JavaScript 语法检查）。

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py' -v
node --check static/app.js
node --check admin-static/app.js
bash -n inspect_run.sh
```

测试在临时目录与本机 HTTP fixture 中完成，不读生产凭证、不访问云元数据、不投递消息。DNS 与套接字固定测试使用模拟数据。

## 部署既有服务器

先对 `/opt/inspect-platform`、Jenkins 任务 XML 与服务文件做 root 私有备份。只更新源文件，不覆盖 data/，不修改个人主页与 nginx。建议暂存、完成 Python/Node 兼容性测试后再短暂停服务替换源码。

```sh
cd /opt/inspect-platform
python3 -m py_compile app.py core.py admin.py worker.py
node --check static/app.js
node --check admin-static/app.js
python3 -c 'import core; core.init()'
# 首次安装新增服务时：
sudo cp deploy/inspect-admin.service deploy/inspect-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inspect-admin inspect-worker
sudo systemctl restart inspect-platform inspect-admin inspect-worker
```

服务器已有 `data/secret.env` 时，`python3 deploy/update_job.py` 用该服务器现有 Jenkins API 凭证只更新 `site-http-smoke` 任务，脚本不会输出秘密。不要在其他机器上为运行此脚本而复制生产密钥。

验证公网匿名总览/报告正常、匿名配置写入拒绝、管理端仅 loopback 监听、Jenkins 认证保持、7 项回归通过。配置修改后验证新报告使用新快照，旧报告内容不变。

## 回滚

本次完整备份：`/root/backups/inspect-task-config-20261125-104650/`。

先停止三个 inspect 服务，再从备份恢复旧源代码/旧静态目录及 `jenkins-job.xml` 对应任务定义，重启旧平台。不要把备份的 data/ 覆盖到新的 data/，避免丢失新报告。旧版本不使用新数据库，可保留它供后续恢复；新增管理/worker 服务在旧版本回滚时应停止。Jenkins 任务应恢复旧定义后再触发，以免旧 shell 与新队列逻辑混用。

## 视觉验收

本次未运行浏览器，不能把语法/DOM 测试称作视觉验收。可检查公开 `/qa/#/tasks`、`/qa/#/events`、最新报告快照，以及通过 SSH 隧道访问的本机管理表单；需覆盖桌面和移动窄屏。


## 邮件适配器（本次追加）

仅安装 `deploy/inspect-mail.service` 并启动队列进程不会自动开通发信。专用 `data/mail.env` 必须保持 600，默认 `MAIL_ENABLED=false`；不从其他程序复制 SMTP 秘密。配置键、基线、不发测试信和重试语义见 `MAIL-SETUP.md`。入库时必须继续排除整个 data/。

本次备份：`/root/backups/inspect-mail-20261125-114001/`，含一致性数据库副本。回滚前停止 inspect-mail；旧 core.py 不认识新增 outbox，但可以保留数据库新表，避免覆盖新运行报告。不要用旧数据库覆盖后续数据。

## QQ 正式启用记录

2026-11-25 14:28:18（Asia/Shanghai）完成真实TLS/认证/QUIT后启用未来失败恢复通知；未发测试信。最新记录和本轮备份见 `MAIL-ACTIVATION.md`。源码包仍不包含任何生产配置值。
