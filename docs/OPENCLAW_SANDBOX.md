# OpenClaw Sandbox 稳定运行

OpenClaw sandbox 通常不适合依赖 systemd，也未必预装 tmux。AP 提供 `scripts/ap_openclaw.sh` 作为 sandbox 专用运行脚本，用于一键启动、停止、查看状态和恢复任务。

## 为什么不用 nohup 或 systemd

- `nohup command &` 只能降低 shell 退出对进程的影响，不能在服务崩溃后自动重启，也很难统一管理子进程和日志。
- `setsid` 可以让进程脱离当前 shell session，但它本身不是 watchdog；服务退出后不会自动恢复。
- systemd 适合长期 Linux VM，但 OpenClaw sandbox 经常不是 systemd init 环境，`systemctl` 不可用。

因此脚本采用：

- `setsid`：脱离当前 shell session。
- watchdog：只用于 `serve`，服务退出后等待 5 秒自动重启。
- PID 文件：写入 `run/`，用于防重复启动和停止进程组。
- 日志文件：写入 `logs/`，用于排查启动、重启和标注失败。

## 一键启动工作台

```bash
bash scripts/ap_openclaw.sh serve start
```

默认命令等价于：

```bash
python cli.py serve --host 0.0.0.0 --port 8800
```

查看状态：

```bash
bash scripts/ap_openclaw.sh serve status
```

重启：

```bash
bash scripts/ap_openclaw.sh serve restart
```

停止：

```bash
bash scripts/ap_openclaw.sh serve stop
```

访问地址通常是：

```text
http://127.0.0.1:8800
```

如果 OpenClaw 提供外部访问域名，请使用平台展示的主机名加端口 `8800`。

## 后台启动标注

```bash
bash scripts/ap_openclaw.sh label start
```

默认命令等价于：

```bash
python cli.py label --status pending,failed --strict
```

`label` 是一次性后台任务，不做无限自动重启。若模型服务、配置或 schema 有问题，先修复问题，再重新运行 `label start`，它会继续处理 `pending,failed`。

查看状态：

```bash
bash scripts/ap_openclaw.sh label status
```

停止：

```bash
bash scripts/ap_openclaw.sh label stop
```

## 环境变量

```bash
PORT=8810 bash scripts/ap_openclaw.sh serve start
TASK=intent_v1 bash scripts/ap_openclaw.sh label start
PYTHON=/path/to/python bash scripts/ap_openclaw.sh serve start
```

- `HOST`：默认 `0.0.0.0`。
- `PORT`：默认 `8800`。
- `TASK`：可覆盖 `config/config.yaml` 里的默认任务。
- `PYTHON`：可指定 Python；未指定时优先 `.venv/bin/python`，其次 `python3`。

## 日志和 PID

- serve 日志：`logs/openclaw_serve.log`
- label 日志：`logs/openclaw_label.log`
- PID 文件：`run/openclaw_*.pid`

查看最新日志：

```bash
tail -n 100 logs/openclaw_serve.log
tail -n 100 logs/openclaw_label.log
```

## 常见恢复步骤

端口被占用：

```bash
bash scripts/ap_openclaw.sh serve stop
PORT=8810 bash scripts/ap_openclaw.sh serve start
```

模型配置缺失：

- 检查 `OPENCLAW_ENDPOINT`
- 检查 `OPENCLAW_API_KEY`
- 再运行 `bash scripts/ap_openclaw.sh label start`

数据库或目录不可写：

- 确认 `data/`、`logs/`、`run/` 所在目录可写。
- 重新执行对应 start 命令。

标注失败后恢复：

```bash
bash scripts/ap_openclaw.sh label status
tail -n 100 logs/openclaw_label.log
bash scripts/ap_openclaw.sh label start
```

AP 的导入是按 `task_id` 幂等的，标注命令默认处理 `pending,failed`，适合修复问题后继续跑。
