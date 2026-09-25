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

- 中线基于最近 5 根已收盘 H1：`((五根 high 的平均值) + (五根 low 的平均值)) / 2`。MACD 参数为 12/26/9。每 5 分钟检查；默认 M1 金叉/死叉必须在最近已收盘 M1 bar 发生，M5 同向交叉须落在配置的最近 5 分钟窗口内。信号只在成功开仓后消费。
- 多单要求价格高于中线且 M1/M5 同为金叉；空单要求低于中线且两者同为死叉。多空可同时持有；每周期最多成功开仓 `max_entries_per_cycle` 次，默认 10。默认没有跨周期总手数上限，保证金/熔断仍会阻止或清算超限账户风险。
- 默认每笔风险预算为当时权益的 0.5%。手数按 `floor((equity × risk_pct) / (止损距离 × 合约盎司数) / volume_step) × volume_step` 向下取整；低于最小手数、止损距离超过 8 美元时记 `ENTRY_SKIPPED_RISK`，交叉不消费。仅 `risk_per_trade_pct: null` 时，才回退至固定 `entry_lots`。
- 初始止损用上一根已收盘 M5 的低点/高点；每小时 UTC 的 `:00` 和 `:30` 只用上一根完整 M5 更新，且只朝盈利方向收紧。只靠止损出场，不设止盈、不用反向指标。少于 `min_stop_distance_usd` 的信号记 `ENTRY_REJECTED`。
- `stop_rule_scope` 可设 `current_cycle`（默认）、`next_cycle` 或 `both`。亏损止损阈值与每周期开仓上限分离；默认每周期最多 10 次亏损止损触发限制，盈利止损默认不计入阈值，但全部止损数始终单独报告。止损归属于平仓发生的周期，跨周期旧持仓在本周期止损时计入本周期。
- 每日亏损熔断默认 3%（当日已实现+浮动净值相对当日开始权益），最大回撤熔断默认 10%（相对权益峰值）。触发后只平不开，回测在下一个 UTC 服务器交易日解除；Demo 到下一个服务器日或手动 `--reset-risk-halt` 才恢复。熔断写入事件和心跳。
- 回测按相邻 M1 bar 开始时间差大于 30 分钟推断休市。在休市前 30 分钟内禁止新开仓，默认在休市前最后一根可成交 M1 bar 主动平掉全部仓位，原因 `SESSION_CLOSE`，不计止损。`allow_hold_through_session_break` 默认 false。回测主动平仓严格按数据中的最后可成交 bar，不再用周五或墨西哥墙钟时间推测。
- Demo 优先读显式券商交易时段扩展；若不可得，使用最近 20 个交易日每日最后 tick 估算下一休市时刻并记录来源。若时段无法判断则停止新开仓并在心跳暴露 `UNAVAILABLE`。Demo 在计划休市前 `session_close_buffer_minutes`（默认 15）进入主动平仓窗口；过期报价不会被伪造为可成交价。
- 默认 UTC blackout 窗口为空，格式 `HH:MM-HH:MM`，起始包含、结束不包含，可跨午夜。仅屏蔽新开仓。
- 默认成本：完整点差 0.20 美元、单边滑点 0.10 美元、佣金 0；这些是配置假设，不是历史实测。保证金按 `leverage` 简化计算，达到 margin call / stop-out 百分比时发出事件并逐腿清算。swap 按配置的多空每手每夜金额计费，默认周三三倍；应以券商真实历史费率替换默认值。

## Demo 运行、状态和监控

```powershell
python scripts/run_xauusd_demo_strategy.py --confirm-demo-strategy
```

运行器仍执行 Demo-only、对冲账户、订单前预检、止损只收紧不放松、订单结果不确定时锁定等既有保护。持仓/周期状态原子写入 `artifacts/xauusd_demo/state.json`；可通过 `--state-file` 改路径。启动时能读取状态则接管已有本策略仓位；已有策略仓位但状态无法读取时会拒绝启动并要求先人工核对 MT5 持仓/服务器止损及状态文件。连续成功轮询达到 `--lockout-recovery-polls`（默认 60）后自动解除入场锁定；可在确认账户/状态无异常后使用 `--reset-lockout`。

默认每 60 秒记录心跳，含当日已实现+浮动盈亏、当前回撤、熔断、入场锁定、周期亏损/全部止损计数、下一休市时间与其数据来源。独立看门狗只检查 JSONL 心跳是否过期，输出日志及退出码，不会平仓或重启：

```powershell
python scripts/xauusd_watchdog.py --log artifacts/xauusd_demo/xauusd_demo_<时间戳>.jsonl --max-age-seconds 180
```

Demo 进程停止时不会继续推移动止损；券商端已挂止损是否保留，取决于账户和订单实际状态。看门狗不能替代进程监控或券商端风险控制。

## 研究防护和报告

`causality.assert_prefix_causal` 检查未来扰动不改变过去特征；数据模块检查时间排序、重复、OHLC 关系，并核对 M1 聚合与独立 M5/H1。回测报告包括年化收益、日频 Sharpe（252 年化、无风险利率 0）、最大回撤、胜率、平均盈亏比、止损/亏损止损次数、换手率、R 倍数分布、前 5 盈利交易利润集中度、多空/年度/上涨震荡下跌状态分解、机械成本翻倍敏感性、MAE/MFE 分布、每周期开仓和止损分布、swap、拒绝及休市审计。

逐笔/权益结果是配置模型下的历史模拟，不构成投资建议或未来表现保证。M1 OHLC 不能恢复 bar 内 tick 顺序；交易时段、点差、滑点、佣金、swap、杠杆和保证金强平均需用目标券商数据核验。固定成本翻倍报告是机械敏感性分析，并不重跑不同成交路径。
