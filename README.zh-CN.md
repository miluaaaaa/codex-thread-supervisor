# Codex 对话监督器

[English](README.md) · [MIT 开源协议](LICENSE)

这是从实际部署的监督器中提取、独立适配的开源版本：定期回到**原来的 Codex 对话和 tmux pane**，提醒它核查并推进指定任务。

## 它做什么

- 每个对话独立设置监督周期，最低5分钟，最高7天。
- 首次等待完整周期；对话还在执行时不插入、不堆积监督消息。
- 绑定明确的对话UUID、Codex PID、进程启动标识和tmux pane；绑定变化或进程退出就停止。
- 每次续接带唯一nonce，必须在原对话记录中确认，不能把按下回车当成成功。
- 连续故障必须诊断；四次相同非容量故障或两次缺失状态报告会停止。
- 等GPU时采用基础周期的三倍，不会反复倍增；恢复执行后回到基础周期。
- **每完成5轮监督，生成一条通知；完成任务再生成结束通知。** 重复事件不重复计数。

## 公开版边界

支持Linux、Python 3.10+、tmux、systemd用户服务，只依赖Python标准库。运行中的本地Codex CLI必须持有原始rollout文件，且记录格式包含任务开始、结束及用户消息turn ID；不支持的格式会拒绝发送。

没有打包私人TLS服务、Cursor桥接、飞书凭据、历史对话、实验结果和GPU调度器。公开版不会自动迁移已有非tmux会话。通知先进入本地持久化outbox，飞书等聊天渠道需要接入自己的发送适配器；文件入队不等于手机已收到。

## 安装

```sh
git clone https://github.com/miluaaaaa/codex-thread-supervisor.git
cd codex-thread-supervisor
./install.sh
export PATH="$HOME/.local/bin:$PATH"
```

安装器创建命令链接，保留已有监督器skill，不会启动监督器或改变现有会话。请保留仓库目录。

## 绑定与使用

在操作终端查清同一个原对话的pane、PID和UUID：

```sh
tmux list-panes -a -F '#{pane_id} #{pane_pid} #{pane_tty} #{pane_current_command}'
ps -t pts/REPLACE -o pid,comm,args
codex-supervisor-bind bind --thread UUID --pane %PANE --pid CODEX_PID
codex-supervisor start --thread UUID --interval 15min --task "完成指定任务并核验"
codex-supervisor status --thread UUID
codex-supervisor stop --thread UUID
codex-supervisor list
```

以上大写值和`pts/REPLACE`是占位符，需要替换。不可从最新文件猜对话UUID；当前Codex工具环境中的`CODEX_THREAD_ID`只标识当前对话。绑定检查实际打开的rollout、用户归属、TTY及进程祖先，拒绝错绑及一pane多对话。

`start` 可加 `--report-instructions "每轮展示完整进度表和具体阻塞"`，为该对话保存汇报要求，放入提醒的协议注释，不改任务名。再次 `start` 默认保留；显式传入新内容替换，传空字符串清除。上限1000字符，不允许HTML注释或终端转义字符。

监督回复以`TLS_SUPERVISION_CONTINUE`开头，并以状态JSON结尾：

```text
TLS_SUPERVISION_STATE: {"status":"running","fault":"","evidence":"实际核查到的进展"}
```

完成验收后先停止监督，再回复`TLS_SUPERVISION_COMPLETE`。监督机制不替代实验科学验收，GPU分配不等于实际推理。

## 五轮通知与活动回调

通知保存在`~/.local/state/codex-supervisor/outbox/*.json`，包含事件ID、对话、任务和答案。目录权限0700，文件0600。发送适配器按事件ID去重，发送成功才归档，失败保留重试。

前端集成可以调用以下入口，提前关闭发送窗口、在回复完成时重置静默周期并收集通知：

```sh
codex-supervisor activity --thread UUID --event prompt
codex-supervisor activity --thread UUID --event started --turn-id TURN_ID
codex-supervisor activity --thread UUID --event completed --turn-id TURN_ID
```

这些是本项目的适配入口，不声称所有Codex版本都支持某种相同原生hook配置。没有活动回调时，运行中的监督器在下一次timer检查时收集回复；如果最终回复之前已显式停止，需要completed回调才能收集结束通知。

完成回调使用绝对截止时间重新设置静默周期，避免timer重载或提前唤醒后多等一整个周期。投递失败会停止监督，并在状态目录的 `alerts/UUID.json` 保存私有故障记录；这不等于已发送聊天通知，也不能据此判断底层任务是否运行。

仅当停止原因是nonce确认超时，且迟到的完成回调匹配原绑定（包括tmux socket）、原nonce、精确turn、继续标记和有效状态报告时，才恢复同一监督器并重新计时。手动停止、窗口变化、无关回复及任务完成均不会通过此路径重启监督。没有完成回调则保持停止；恢复不会重发原提醒，通知仍按原有规则计数。

tmux发送会清空输入框再粘贴，因此监督期间不要在原pane保留未提交草稿。最后一次记录检查和粘贴之间仍有很短的输入竞争窗口；活动回调能够提前阻止发送，但不是事务式锁定用户键盘。

## 测试

```sh
TZ=Asia/Shanghai PYTHONPATH=scripts python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
```

测试包括状态机、nonce确认、故障停止、绑定检查、五轮通知、失败重试，以及使用`cat`的真实隔离tmux传输检查。不会向你的运行对话发送测试内容，不运行模型。公开版仍需在使用者安装的Codex版本上验证记录格式兼容性。

详细配置和可选Zen/Agent Mail元数据接口见[英文说明](README.md)。

## 按任务调整 GPU 监督周期

```sh
codex-supervisor tune --thread UUID --mode adaptive
codex-supervisor observe --thread UUID --metrics-file task-gpu.json
codex-supervisor tune --thread UUID --mode base
```

只调整已有、仍活动的监督器，保留原窗口绑定、实例、nonce 和运行中对话保护。自适应模式明确覆盖固定周期和旧的缺卡三倍退避，普通定时器仍接受 5 分钟至 7 天。

自适应周期为 60–900 秒：有已预检任务但未达到目标卡数时 60 秒；确认无可用容量时 180 秒；稳定运行时采用任务耗时的 10%，并在预计完成前进一步缩短。这个公式把平均轮询等待控制在任务耗时的约 5%，只是启发式，不保证实际空闲比例或最高 GPU 利用率。不会打断正在运行的 Codex 回合。

任务负责人在正常进度 JSON 中增加 `gpu`，或在派发、完成、失败事件后立即调用 `observe`：

```json
{"observed_at": 0, "target_gpus": 8, "verified_gpus": 6, "ready_lanes": 2,
 "capacity": "available", "evidence": "模型 PID、显存和计算证据的引用",
 "job_runtime_seconds": 180, "next_completion_seconds": 60}
```

将时间戳和示例数值替换为实际观测。超过 600 秒、未来时间戳、乱序记录和非有限数值会被拒绝，重复观测不增加计数。执行证据是负责人提供的引用，本模块不能独立证明模型执行，也不批准 GPU 准入或结果验收。

每个任务保存 `gpu_task_stats`：根据相邻观测累计目标、已核验运行、缺口卡秒；间隔超过 600 秒计为未知时间。这些采样计数不能当作连续 SM 利用率或吞吐量。

任务调度器必须主动调用事件入口，本模块不会自动订阅 Slurm。有效任务完成后应立即通过 Zen 补位，不等待下一次聊天监督。缩短监督周期无法修复没有有效待跑任务、适配失败、CPU/I/O 瓶颈或窗口掉线。公开 tmux 后端与私有 TLS 后端独立，不迁移已有对话。

无固定 GPU 目标时将 `target_gpus` 显式设为 `null`。优化器以已核验运行卡加预检待跑 lane 作为本轮任务需求，调整周期并累计需求/缺口卡秒；此值不是资源上限，可以随每次观测改变。
