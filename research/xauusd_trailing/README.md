# XAUUSD 移动止损策略

本目录实现 XAUUSD 的事件驱动历史回测与 MT5 Demo 运行器。默认只回测或连接 Demo；没有实盘下单模式，不在配置中保存账号密码/API 密钥。回测与 Demo 共用 `rules.py` 中的手数、交叉有效性、止损计数、熔断和入场门控规则。

## 变更摘要（本次修订）

旧口径曾使用墨西哥城周五 21:55 主动平仓、固定 0.05 手、亏损止损达到 10 次后暂停“下一周期”，且允许回测合约大小为空。它们已被本次规则取代：交易时段改为 UTC/服务器时钟；仓位按止损距离缩放；默认在发生亏损止损的当前周期停止开新仓；回测必须有合约大小，并模拟账户级熔断、保证金/强平与隔夜 swap。变更细节见 [`docs/XAUUSD_移动止损策略规格_v1.md`](../../docs/XAUUSD_移动止损策略规格_v1.md)。

## 准备和导出数据

回测需要 MT5 导出的 XAUUSD M1、M5、H1 CSV。字段至少含 UTC bar 开始时间及 OHLC；支持 `timestamp_utc/open/high/low/close`、常见 `time/open/high/low/close` 和 MT5 分列格式。`spread_points` 会保留作审计；固定点差配置与原始 spread 不会重复扣费。无时区时间戳按 `source_timezone` 解释，MT5 导出应明确使用 UTC。

可从本机已登录的 MT5 Demo 终端只读导出并验证覆盖区间：

```powershell
python scripts/export_xauusd_mt5_history.py --confirm-demo-read `
  --start 2020-01-01T00:00:00Z --end 2025-01-01T00:00:00Z
```

导出器写入 M1/M5/H1 CSV 和 `coverage.json`，检查请求区间两端（默认允许 5 日容差），并对 M1 聚合与 M5/H1 做完整窗口审计。MT5 的“图表最大柱数”必须设为“不限/Unlimited”，否则无法保证取得 2020 年起的完整 M1 历史。导出脚本需要显式 `--confirm-demo-read`，只读历史、不发订单。

## 回测

从仓库根目录运行：

```powershell
python -m research.xauusd_trailing.run --config research/xauusd_trailing/config.example.yaml
```

`contract_size_oz` 必须填写目标券商 XAUUSD 合约大小，否则回测立即报错；不可用空值继续算信号或漏记亏损止损。输出默认写到 `artifacts/xauusd_backtest/`：逐笔成交、事件、日权益、期末持仓、配置/数据哈希、样本切分和诊断报告。Walk-forward 默认区间为 2020–2022 开发、2023 验证、2024 样本外；最终样本外不得用于调参。

### 当前策略和风控口径

- 中线基于最近 5 根已收盘 H1：`((五根 high 的平均值) + (五根 low 的平均值)) / 2`。MACD 参数为 12/26/9。每 1 分钟检查；默认 M1 金叉/死叉必须在最近已收盘 M1 bar 发生，M5 同向交叉须落在配置的最近 5 分钟窗口内。信号只在成功开仓后消费。
- 信号决策时间取实际 tick 时间向下对齐的 UTC 整分钟（最近已收盘 M1 边界）；执行和日志仍保留真实 tick 时间。2026-10-01 修复了 tick 秒/毫秒导致完整 5 根 M1 被误判为缺失、最新交叉比较失败的问题。修复前的 `NO_SIGNAL` 记录不能用于证明实际信号频率；修复后无信号日志同时记录 `decision_bar_close_utc`。
- 2026-10-01 按用户确认把默认 `check_minutes` 从 5 改为 1，保留 `require_latest_m1_cross=true`，避免跳过检查边界之间的 M1 交叉。Demo 在当前分钟首个可用 tick 上检查，不再要求 tick 必须落在 00–05 秒；完整检查完成后同一分钟不再重复评估。数据未齐则记 `ENTRY_CHECK_DATA_NOT_READY` 并在当前分钟重新读取/补检，不消费交叉；跨入下一分钟后只判断新的最近已收盘 M1，不追开过期信号。回测使用相同分钟调度和完整 M1 窗口规则。
- 多单要求价格高于中线且 M1/M5 同为金叉；空单要求低于中线且两者同为死叉。多空可同时持有；每周期最多成功开仓 `max_entries_per_cycle` 次，默认 10。默认没有跨周期总手数上限，保证金/熔断仍会阻止或清算超限账户风险。
- 默认每笔风险预算为当时权益的 0.5%。手数按 `floor((equity × risk_pct) / (止损距离 × 合约盎司数) / volume_step) × volume_step` 向下取整；低于最小手数、止损距离超过 8 美元时记 `ENTRY_SKIPPED_RISK`，交叉不消费。仅 `risk_per_trade_pct: null` 时，才回退至固定 `entry_lots`。
- 初始止损用上一根已收盘 M5 的低点/高点；每小时 UTC 的 `:00` 和 `:30` 只用上一根完整 M5 更新，且只朝盈利方向收紧。只靠止损出场，不设止盈、不用反向指标。少于 `min_stop_distance_usd` 的信号记 `ENTRY_REJECTED`。
- `stop_rule_scope` 可设 `current_cycle`（默认）、`next_cycle` 或 `both`。亏损止损阈值与每周期开仓上限分离；默认每周期最多 10 次亏损止损触发限制，盈利止损默认不计入阈值，但全部止损数始终单独报告。止损归属于平仓发生的周期，跨周期旧持仓在本周期止损时计入本周期。
- 每日亏损熔断默认 3%（当日已实现+浮动净值相对当日开始权益），最大回撤熔断默认 10%（相对权益峰值）。触发后只平不开，回测在下一个 UTC 服务器交易日解除；Demo 到下一个服务器日或手动 `--reset-risk-halt` 才恢复。熔断写入事件和心跳。
- 回测按相邻 M1 bar 开始时间差大于 30 分钟推断休市。在休市前 30 分钟内禁止新开仓，默认在休市前最后一根可成交 M1 bar 主动平掉全部仓位，原因 `SESSION_CLOSE`，不计止损。`allow_hold_through_session_break` 默认 false。回测主动平仓严格按数据中的最后可成交 bar，不再用周五或墨西哥墙钟时间推测。
- Demo 优先读显式券商交易时段扩展；若不可得，先从最近 40 天 M1 识别相邻 bar 间隔大于 30 分钟的休市，再读取最近 20 个交易日各缺口前最后可成交 M1 的最后 tick，按星期估算下一休市时刻并记录来源。不能把日内重开后的 UTC 当天最后 tick 当成休市时刻。若时段无法判断则停止新开仓并在心跳暴露 `UNAVAILABLE`。Demo 在计划休市前 `session_close_buffer_minutes`（默认 15）进入主动平仓窗口；过期报价不会被伪造为可成交价。
- 默认 UTC blackout 窗口为空，格式 `HH:MM-HH:MM`，起始包含、结束不包含，可跨午夜。仅屏蔽新开仓。
- 默认成本：完整点差 0.20 美元、单边滑点 0.10 美元、佣金 0；这些是配置假设，不是历史实测。保证金按 `leverage` 简化计算，达到 margin call / stop-out 百分比时发出事件并逐腿清算。swap 按配置的多空每手每夜金额计费，默认周三三倍；应以券商真实历史费率替换默认值。

## Demo 运行、状态和监控

```powershell
python scripts/run_xauusd_demo_strategy.py --confirm-demo-strategy
```

运行器仍执行 Demo-only、对冲账户、订单前预检、止损只收紧不放松、订单结果不确定时锁定等既有保护。持仓/周期状态原子写入 `artifacts/xauusd_demo/state.json`；可通过 `--state-file` 改路径。启动时能读取状态则接管已有本策略仓位；已有策略仓位但状态无法读取时会拒绝启动并要求先人工核对 MT5 持仓/服务器止损及状态文件。连续成功轮询达到 `--lockout-recovery-polls`（默认 60）后自动解除入场锁定；可在确认账户/状态无异常后使用 `--reset-lockout`。

默认每 60 秒记录心跳，含本次运行唯一 `run_id`、实际 Python 进程 PID、实际 `check_minutes`、`require_latest_m1_cross`、数据等待原因、当日已实现+浮动盈亏、当前回撤、熔断、入场锁定、周期亏损/全部止损计数、下一休市时间与其数据来源。看门狗独立核对进程是否存在及创建标识、同一运行的心跳是否过期（默认 180 秒）、终端断线/查询错误/入场锁定，输出日志及退出码，不会平仓或重启：

```powershell
python scripts/xauusd_watchdog.py --log-dir artifacts/xauusd_demo --max-age-seconds 180
```

### 停机审计与人工停止（2026-10-01 本次补充）

旧版只有交易心跳，进程结束时没有可靠的停机原因；退出码 0、旧心跳或终端仍在后台不能证明策略正常。新口径只有“明确确认的人工停止请求被本次运行器接收，保存策略状态并记录人工退出”属于正常停机。其它退出均默认异常，包括更新导致进程消失、未确认的中断、意外返回/退出码 0、截止时间到且空仓后的旧版自动返回。此修订只分类并告警，不改变截止时间、入场、移动止损或会话平仓等交易规则。

- `runtime/runtime.json` 保存当前 `run_id`、PID/创建标识、起止时间、阶段及退出原因；`runtime/lifecycle.jsonl` 追加生命周期事件。异常只记录异常类型和调用位置，不记录异常消息、密钥、局部变量或账户密码。
- `runtime/control.json` 保存明确人工停止意图和目标 `run_id`，运行器确认后的凭证另存入运行审计，解除停止标记不会篡改此前的正常停机事实。后补一个停止标记不能把已经消失的进程改成正常停机。
- `watchdog.jsonl` 保存每次检查和 `RUNTIME_ANOMALY_DETECTED`/`RUNTIME_ANOMALY_CLEARED` 事件；`runtime/watchdog_status.json` 保存最新检查。同一未改变的问题不重复创建“新事件”，但保留检查记录。退出码 1 表示异常；退出码 0 的 `WATCHDOG_STARTING`/人工停止等待表示过渡状态，不等于已确认正在监测。
- 每个策略 state 文件使用操作系统排他锁，避免两个新版运行器同时接管同一状态；强杀后操作系统释放锁。没有旧运行结束记录时，下一次显式启动必须补记异常，不能静默覆盖。锁和审计文件不是 MT5 交易命令。

人工停止入口（对正在运行的新版运行器生效，不连接 MT5）：

```powershell
python scripts/xauusd_runtime_control.py stop --confirm-manual-stop
python scripts/xauusd_runtime_control.py status
```

运行器下一次轮询接收停止请求，先保存周期计数、已消费交叉、锁定状态及持仓快照，再退出；不主动平仓、不移除已挂止损。应通过 `status`/看门狗确认 `MANUAL_STOPPED`，不能把“请求已写入”当作“停止已完成”。直接强杀或没有记录的 Ctrl+C 无法核验是否由用户发起，默认按异常处理。

人工停止意图持续保留，后续启动入口先检查该标记；未解除时不连接 MT5。以下命令仅表达恢复意图并解除标记，本身不会启动策略：

```powershell
python scripts/xauusd_runtime_control.py clear-stop --confirm-demo-resume-intent
```

如自定义运行器 `--runtime-dir`，控制脚本和看门狗也必须使用同一路径。看门狗本身仍是单次检查工具，不执行重启。2026-10-01 用户曾要求只记录告警；2026-10-02 明确改为“故障自动重启，主动停止不重启”，由下面的独立守护实现。远程推送通知仍未开通。既有“运行中连续成功轮询后解除 entry lockout”的交易规则保留，不通过重启清除锁定。

### 故障自动恢复与 Windows 独立任务（2026-10-02 用户确认）

`supervision.py` 与 `scripts/xauusd_supervisor.py` 只管理进程生命周期，不含 MT5 下单代码。部署后守护每 15 秒检查一次：

- 现有策略进程已退出且控制意图为 `RUNNING`：使用原配置、原 `.env` 路径和同一 `state.json` 拉起 Demo 运行器；不传入任何锁定/熔断复位参数，不清零周期计数、不重发历史订单。运行器仍重新验证 Demo、hedging、已有仓位服务端止损，只有通过所有既有规则才允许交易。
- `control.json` 明确 `STOPPED`：不启动新策略，不自动解除标记。人工停止是策略全局意图，在旧/新 `run_id` 交接中也有效；新进程若恰好与停止请求交错，启动门控或下一轮轮询必须接受停止。
- 已存活但心跳超时、MT5 断线或订单不确定锁定：持续告警，不强杀、不新建第二个实例。熔断/周期暂停/正常休市也不通过反复重启解除。
- 状态文件缺失/损坏、进程身份无法核验或找不到配置的 MT5 可见窗口：阻止恢复并留痕；不让 `initialize()` 隐式启动隐藏终端。MT5 关闭后需先打开原 Demo 终端；守护会继续等待，不更改账号或设置。
- 启动失败按 60、120、240……秒退避，上限 900 秒，退避状态持久化；确认新运行心跳健康后清除失败次数。正在启动的进程不重复拉起，启动超时仍存活则告警，不盲目强杀。

配置和安装都需要明确 Demo 恢复授权，凭据只从本地 `.env` 读取，不进入服务配置/任务参数：

```powershell
python scripts/xauusd_supervisor.py --configure --confirm-demo-auto-recovery `
  --python-executable '<现有虚拟环境>/Scripts/python.exe' `
  --env-path '<现有本地 .env>' --terminal-path '<已有 MT5>/terminal64.exe'

./scripts/install_xauusd_recovery_task.ps1 -ConfirmDemoAutoRecovery `
  -PythonwPath '<现有虚拟环境>/Scripts/pythonw.exe' `
  -ServiceConfig './artifacts/xauusd_demo/runtime/recovery_config.json'
```

Windows 任务 `XAUUSD-Demo-Recovery-f8bf` 在当前用户登录会话中以普通权限执行 `pythonw`，不存储系统密码、不弹终端；守护常驻，每分钟兜底触发且忽略重复实例，用户登录时也触发。任务不设置三天默认执行限时，允许电池供电时继续；并给守护本身设置失败重试。任务不依赖 Codex 进程，但电脑关机、睡眠、用户注销或 Windows 任务服务不可用时不能保证交易管理；重新登录且 MT5 窗口就绪后才可恢复。[Microsoft 任务设置说明](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset?view=windowsserver2025-ps)、[用户会话 principal](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal?view=windowsserver2025-ps)。

`runtime/recovery_config.json` 保存显式授权和本机路径；`supervisor_status.json` 保存最新检查、守护 PID/创建标识和退避/待启动状态；`supervisor.jsonl` 追加接管、故障恢复、阻止恢复等事件。`xauusd_runtime_control.py status` 同时显示 `auto_recovery_enabled` 和守护状态。独立运行器/看门狗的 `automatic_restart=false` 仍表示“它自身不会重启”；已部署的外部守护以 `auto_recovery_enabled=true` 和守护实况为准，不能只看孤立字段。

人工停止仍用 `stop --confirm-manual-stop`，守护继续运行但保持不启动策略。`clear-stop --confirm-demo-resume-intent` 本身不启动进程；启用守护后明确解除停止意图，会允许守护下一轮恢复 Demo。直接在任务管理器结束策略进程或无停止凭证的 Ctrl+C 会被当作故障并自动拉起，因此主动停止必须使用已记录的停止入口。不要只关一个 Python 窗口来表达长期停机。

无 MT5 的集成测试使用独立假运行器，实际执行“故障退出→重启→人工停止→不再重启”，核对 state 内容未改变；绝不通过强杀真实 Demo 运行器来演示恢复。

熔断、周期暂停、正常休市属于交易限制；进程应继续心跳，不能把它们当成正常进程退出。不可抗力需要日志/系统事件证据与人工确认，不能由程序仅凭“断线/没有心跳”自动豁免。断电、系统彻底停机时本机看门狗也无法立即记录或通知；恢复后需核验，若要求即时外部告警须另行部署独立监控。

Demo 进程停止时不会继续推移动止损；券商端已挂止损是否保留，取决于账户和订单实际状态。看门狗不能替代券商端风险控制。测试命令：`python -m unittest discover -s tests -p 'test_xauusd_*.py'`，包括退出分类、人工停止确认、强杀后补记、旧心跳/PID 复用、异常去重及无 MT5 的交易安全回归。

## 研究防护和报告

`causality.assert_prefix_causal` 检查未来扰动不改变过去特征；数据模块检查时间排序、重复、OHLC 关系，并核对 M1 聚合与独立 M5/H1。回测报告包括年化收益、日频 Sharpe（252 年化、无风险利率 0）、最大回撤、胜率、平均盈亏比、止损/亏损止损次数、换手率、R 倍数分布、前 5 盈利交易利润集中度、多空/年度/上涨震荡下跌状态分解、机械成本翻倍敏感性、MAE/MFE 分布、每周期开仓和止损分布、swap、拒绝及休市审计。

逐笔/权益结果是配置模型下的历史模拟，不构成投资建议或未来表现保证。M1 OHLC 不能恢复 bar 内 tick 顺序；交易时段、点差、滑点、佣金、swap、杠杆和保证金强平均需用目标券商数据核验。固定成本翻倍报告是机械敏感性分析，并不重跑不同成交路径。
