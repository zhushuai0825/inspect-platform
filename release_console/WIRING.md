# 发布记录接线说明

公开看板 `/qa/#/releases` 是使用发布记录的地方：可以查看记录，也可以发布、回滚、刷新结果和标记未确认。查看不需要登录。这些写操作要在请求里带上发布口令，服务器把它和 `RELEASE_PUBLISH_PASSWORD` 比较。口令不写进页面、脚本或仓库。本机管理入口 `127.0.0.1:8766` 仍可操作同一份记录，它继续靠 SSH 隧道保护，不使用这道口令。

这里没有 GitHub 推送、拉取请求或冲突面板，也不要求先创建前端或后端仓库。

当前不是生产就绪状态。下面的 Jenkins 任务和受信任发布脚本都还没有安装。

## 进程

- 公开巡检仍由系统 Python 3.6 运行 `app.py`，本改动不要求替换它。
- 公开看板上的发布、回滚和记录读取走 `publish_board.py`。这个文件保持 Python 3.6 语法，`app.py` 不导入 `release_console`。模块加载失败时，看板其余页面继续可用；写操作返回错误且不写记录，读取返回空列表。
- `release_console` 仍用 Python 3.11 语法，只给本机管理进程用。管理进程是 3.6 时，管理页打得开，发布接口返回 503，并且不会写入发布记录。
- 以后若要让本机管理入口也启用发布，单独用 3.11 启动管理进程，例如 `python3.11 admin.py`，仍只监听 `127.0.0.1:8766`。不要为了这件事改公开服务的解释器。本文件不代为改 systemd，也不部署。

## 记录什么

操作者在公开看板或本机管理页提交服务（前端或后端）、操作者、版本或提交 SHA、可选制品标识。公开看板上的写操作还要带发布口令。记录在提交被接受时写入，状态从 `deploying` 开始。刷新结果后变为 `succeeded`、`failed` 或 `rolled_back`。

没有配置好 Jenkins 地址、用户、令牌和对应任务名时，接口直接拒绝，数据库不增加行，也不会显示成成功。发布口令未配置或口令不对时同样拒绝，不写行，也不调用 Jenkins。页面在没有记录时是空状态，不填示例数据。

同一服务同时只能有一条 `deploying`。前端和后端互不阻塞。回滚把该服务上一次不同内容的成功发布再提交一次，Jenkins 成功后新记录为 `rolled_back`，被替换的那次成功发布也标成 `rolled_back`。没有更早的成功发布时，回滚会被拒绝且不写新行。

制品标识只是交给以后受信任脚本的名字。这个管理端不上传文件，也不接收仓库 URL。

## 环境变量

写在服务器的 `data/secret.env`，或进程环境。示例见 `deploy/release-console.env.example`。不要把真实令牌提交进仓库。

| 变量 | 作用 |
| --- | --- |
| `JENKINS_URL` | 已有 Jenkins 根地址，仅用于调用已存在的任务 |
| `JENKINS_USER` | Jenkins API 用户 |
| `JENKINS_API_TOKEN` | Jenkins API 令牌 |
| `RELEASE_JENKINS_FRONTEND_DEPLOY_JOB` | 前端发布任务名。空则未接线 |
| `RELEASE_JENKINS_BACKEND_DEPLOY_JOB` | 后端发布任务名。空则未接线 |
| `RELEASE_PUBLISH_HOST` | 记在记录上的主机名，默认 `8.146.206.64`。程序不会自己连这个地址去发布 |
| `RELEASE_INSPECT_AFTER_PUBLISH` | `1`（默认）在终态后跑现有巡检；`0` 则不跑 |
| `RELEASE_DB_PATH` | 可选，发布库路径。默认 `data/release_console.db` |
| `RELEASE_SECRET_FILE` | 可选，秘密文件路径。默认 `data/secret.env` |
| `RELEASE_PUBLISH_PASSWORD` | 公开看板发布、回滚、刷新结果、标记未确认，以及新建、编辑、启停检查的口令。只放在 `data/secret.env` 或进程环境。未设置时这些操作失败且不改数据。不要写进示例文件、页面或测试 |

管理端不检查这道口令。公开接口只从 JSON 正文读取口令，不接受查询字符串，也不会把口令写进响应、页面或请求日志。

任务名只允许字母、数字、点、下划线和连字符。带路径的名字会被当成未配置。

## Jenkins 模板

`jenkins/templates/frontend-publish.Jenkinsfile` 和 `backend-publish.Jenkinsfile` 是给以后手工建任务用的模板。

- 本仓库的 `deploy/update_job.py` 只更新现有的 `site-http-smoke`，不会创建这两个任务。
- 不要把模板登记成「从业务仓库 Jenkinsfile 执行」。控制器不应执行不受信任的业务代码。
- 流水线顶层是 `agent none`，shell 只在标签 `inspect-isolated` 的阶段里跑。这个标签当前没有接到现网控制器上。
- 管理端触发参数只有 `ACTION`、`COMMIT`、`VERSION`、`ARTIFACT`、`SERVICE`、`TICKET`、`ACTOR`。没有仓库 URL。
- 模板先校验参数。受信任脚本 `/opt/inspect-deploy/bin/publish-frontend.sh` 或 `publish-backend.sh` 不在本仓库里，模板发现脚本不存在就以未完成退出，不会假装发布成功。
- 脚本以后要由管理员装到隔离代理上，并且只根据这些已经校验过的参数工作。它不能克隆构建参数里的地址，也不能执行被发布项目自带的 Jenkinsfile。

管理端触发后只保存 Jenkins 队列和构建链接（必须落在已配置的 `JENKINS_URL` 上）。构建还在跑时，刷新保持 `deploying`，锁不放开。Jenkins 明确 4xx 拒绝时，记录记为 `failed`，并写明服务器代码未切换。连接失败或 5xx 且没有队列地址时，记录保持 `deploying`，避免把一次可能已经开始的发布标成结束。这种未确认记录可以在核对 Jenkins 之后，由操作者点「标记未确认」；已经有队列或构建链接的记录不能这样标。没有自动超时把未知结果改成成功或失败。

## 发布之后的巡检

终态（成功、失败、已回滚）且 `RELEASE_INSPECT_AFTER_PUBLISH=1` 时，管理进程调用现有的 `core.execute(trigger="jenkins")`，运行编号形如 `rel-pubxxxxxxxxxxxx`。这就是现在 worker 使用的巡检入口。

它执行数据库里已经启用的检查，包含现有 7 个个人站冒烟项。本改动不替换这些内置目标，也不新建检查。若要让巡检指向这次发布的前端或后端，以后在管理页另加自定义检查；那个钩子还没有做。巡检失败只记在发布记录的巡检字段上，不改写发布本身的成功或失败。Jenkins 没接受发布、或操作者把未确认记录标掉时，不启动巡检。

## 测试

用 Python 3.11 运行，测试里的 Jenkins 和巡检都是假的，不会访问现网：

```sh
python3.11 -m unittest tests.test_release_console -v
```

Python 3.6 可以导入这个测试文件并跳过，不应因此编译 `release_console` 包。
