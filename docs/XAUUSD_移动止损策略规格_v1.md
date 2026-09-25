# XAUUSD 移动止损策略工程规格 v1

状态：本次修订已实现回测和 Demo 规则，并由共享规则函数约束；回测不发订单，Demo 入口要求显式确认且受 Demo-only 校验保护。本文件同步记录旧口径和本次新口径。所有金额/交易结果都是配置下的研究模拟，不构成投资建议。

## 变更记录：旧口径与当前口径

| 主题 | 旧口径（已废止） | 当前口径 |
|---|---|---|
| 休市 | 周五 21:55 `America/Mexico_City` 墙钟强平 | 不按墨西哥墙钟推断。回测由 M1 相邻 bar 间隔 >30 分钟识别休市；Demo 用交易时段或最近 20 个交易日最后 tick 推算，统一记 UTC/服务器时间 |
| 休市动作 | 仅周末前尝试平仓 | 默认不允许跨任何已识别交易时段休市持仓；回测在休市前最后可成交 M1 bar 记 `SESSION_CLOSE`，Demo 在预计休市前 15 分钟开始主动平仓 |
| 入场临近休市 | 原先只看周末截止 | 休市前 30 分钟禁止新开仓 |
| 仓位 | 固定 0.05 手 | 默认按权益风险 0.5% 和初始止损距离下单量；超 8 美元止损距离或不足最小手数则跳过。仅风险比例设为 null 才用固定手数回退 |
| 周期止损 | 达阈值后暂停下一个完整周期 | 默认 `current_cycle`：阈值在平仓所在周期触发后，本周期停止新开仓；也可选择 `next_cycle` 或 `both` |
| 开仓/止损计数 | 共用 10 次口径 | 每周期开仓上限 10 次与亏损止损上限 10 次独立；盈利止损默认不计入亏损阈值，但同时输出全部止损数 |
| 回测合约规格 | 允许合约大小空值并继续输出信号结果 | 回测必须提供 `contract_size_oz`，否则立即报错；不能遗漏止损计数或美元口径 |
| 账户保护 | 无账户级熔断、保证金强平及 swap 模型 | 默认日亏损 3%、峰值回撤 10%只平不开；增加简化保证金/强平及可配置隔夜 swap 模型 |
| MACD 时延 | 允许 M1 使用过去 5 分钟旧交叉 | 默认要求 M1 交叉为最近一根已收盘 M1 的交叉；M5 交叉仍须在最近 5 分钟窗口内 |
| Demo 恢复 | 锁定无法自动恢复、状态只在内存、已有持仓拒绝启动 | 增加轮询恢复和手动复位、原子持久化及安全接管；状态文件缺失/损坏且已有本策略仓位时仍拒绝启动 |

## 1. 需求复述与当前参数

市场为 XAUUSD，技术栈 Python。信号只使用已收盘 H1/M1/M5；不使用 K 线形态、止盈、反向 MACD、中线反向信号或其他额外指标。多空可以同时持有；默认无策略层总手数上限，但有风险仓位、周期、账户熔断和保证金限制。

| 配置 | 默认值 | 说明 |
|---|---:|---|
| 回测区间 | 2020-01-01 至 2025-01-01（结束不含） | UTC 数据；须验证覆盖 |
| 初始资金/币种 | 2000 USD | 回测配置 |
| 区间与信号 | 5 根已收盘 H1；MACD 12/26/9 | 每 5 分钟检查一次 |
| 风险仓位 | 0.005；最大止损距离 8.0 USD | 按初始止损报价距离和合约大小计算 |
| 周期额度 | 每 5 小时 10 次开仓、10 次亏损止损 | `stop_rule_scope=current_cycle`；盈利止损默认不算亏损止损 |
| 成本 | 点差 0.20 USD；单边滑点 0.10 USD；佣金 0 | 配置模型，不等同历史真实报价 |
| 休市控制 | 入场缓冲 30 分钟；Demo 强制平仓缓冲 15 分钟 | `allow_hold_through_session_break=false` |
| 账户保护 | 日亏损 3%；峰值回撤 10% | 熔断后只平不开 |
| 保证金 | 杠杆 100；margin call 100%；stop-out 50% | 简化模型，须用券商参数校准 |
| swap | 多空默认 0；周三三倍 | 费率按每手每夜 USD 配置，必须用券商历史数据替代默认值 |
| 最小止损距离 | 0 USD | 回测可配；Demo 还按券商 `trade_stops_level` 预检 |
| 新闻窗口 | 空列表 | 手工维护 UTC `HH:MM-HH:MM`，仅阻止开仓 |

## 2. 策略规格说明书

### 2.1 时钟、交易周期与休市

- 交易和事件账本使用 UTC/券商服务器时间；`America/Mexico_City` 只用于用户配置的五小时策略周期/展示，不再决定周末清仓时间。
- 周期仍从 `cycle_anchor_local` 起每 5 小时滚动；每个周期单独计成功开仓数、亏损止损数、全部止损数及暂停状态。跨周期持仓继续管理；止损归属按实际平仓所在周期。
- 回测从 M1 bar 开始时间序列推断休市：相邻 bar 间隔严格大于 30 分钟即为休市。最后可成交 bar 为缺口前最后一根 M1，结束时刻为该 bar 开始时间 +1 分钟。休市前 30 分钟不发新入场；默认最后一根可成交 bar 按该 bar 收盘价模型主动平掉全部仓位，原因为 `SESSION_CLOSE`，不计入止损数。`allow_hold_through_session_break=true` 时不主动清仓，但默认仍禁止休市前新入场。
- `session_close_buffer_minutes=15` 是 Demo 主动平仓的计划窗口；离线回测为了满足因果与数据可成交性，以实际缺口前最后一根 M1 执行强平，而不是在缓冲窗口起点虚构报价。
- Demo 优先使用券商/终端明确提供的交易时段扩展；其次使用最近 20 个交易日每日最后 tick，按星期估算下一次休市时刻。日志和心跳必须带 `SYMBOL_INFO_TRADE_SESSIONS`、`OBSERVED_LAST_TICK_20_TRADING_DAYS` 或 `UNAVAILABLE` 来源。不可用时禁止新开仓，不推测一个时间继续交易。
- 标准 MetaQuotes Python `symbol_info` 文档列出的是品种属性，并未承诺暴露每周交易时段；`session_open/session_close` 也可能是价格字段，不能当作时钟。实现只有在运行环境明确暴露交易时段接口时才使用，否则安全回退到最近 tick 观测。[MetaQuotes `symbol_info`](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py)、[MetaQuotes symbol properties](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants)

### 2.2 H1 中线

令 `H1...H5`、`L1...L5` 为决策时点之前最近 5 根完整 H1 的 high/low：

```text
range_high = max(H1, H2, H3, H4, H5)
range_low  = min(L1, L2, L3, L4, L5)
avg_high   = (H1 + H2 + H3 + H4 + H5) / 5
avg_low    = (L1 + L2 + L3 + L4 + L5) / 5
midline    = (avg_high + avg_low) / 2
```

区间上下界供审计；当前入场方向过滤只比较执行时可用价格与 `midline`。每根 H1 只有在 `bar_open + 1h <= decision_time` 后才可用。

### 2.3 MACD、检查时刻与入场

MACD 使用收盘价与参数 `(12,26,9)`，M1/M5 独立计算。定义金叉为 MACD 从不高于 signal 转为高于 signal；死叉相反。信号时间是确认交叉的已收盘 bar 时间。

```text
每 5 分钟检查一次，使用决策时刻前可用的报价与指标：
  LONG  当 price > midline，最新 M1 已收盘 bar 有金叉，且 M5 最近 5 分钟有金叉
  SHORT 当 price < midline，最新 M1 已收盘 bar 有死叉，且 M5 最近 5 分钟有死叉
```

默认 `require_latest_m1_cross=true`，因此 M1 交叉必须来自最近一根已收盘 M1 bar；此设置优先于 M1 的 5 分钟旧交叉窗口。M5 交叉时间必须在决策时间往前 5 分钟范围内。未来交叉不得用于当前决策。已消费的交叉不会重复使用；因风险手数不足而 `ENTRY_SKIPPED_RISK` 的信号不消费交叉。成功下单/回测成交后才消费对应 M1/M5 cross ID。

入场前按顺序检查：Demo/状态/下单锁定、账户熔断、UTC blackout、休市前禁入、周期亏损止损暂停、周期开仓额度、最小止损距离、止损所在方向、按风险计算手数、保证金/券商预检。拒绝/跳过事件保留数值和原因。

### 2.4 风险仓位和初始止损

多单初始止损为最近一根已收盘 M5 的 low；空单为该 bar 的 high。令 `D` 为入场可执行市场报价与初始止损的正向距离，`E` 为下单前账户权益，`C` 为每手合约盎司数，`r` 为风险比例，`v_min` 和 `v_step` 为券商最小手数和步长：

```text
risk_budget_usd = E * r
raw_lots = risk_budget_usd / (D * C)
lots = floor(raw_lots / v_step) * v_step
```

若 `risk_per_trade_pct=null` 才改用 `entry_lots` 固定回退。风险比例有效时，固定手数不覆盖风险算法。`lots < v_min`、`D > max_stop_distance_usd`、止损无效或缺少合约大小时不建仓。风险手数不足或止损超过最大距离记 `ENTRY_SKIPPED_RISK`，且不消费交叉；低于券商最小止损距离记 `ENTRY_REJECTED`。回测从配置读取 `volume_min_lots`、`volume_step_lots`；Demo 从 `symbol_info` 读取手数约束、合约大小和券商 stops level。

### 2.5 移动止损、出场与周期止损规则

固定 UTC 每小时 `:00`、`:30` 检查，使用上一根已收盘 M5。止损只收紧：

```text
多单：new_stop = max(old_stop, previous_closed_M5.low)
空单：new_stop = min(old_stop, previous_closed_M5.high)
```

不设止盈、不看反向信号。止损触发后按不利方向滑点的市价模型平仓。Demo 通过适配器坚持只收紧，不放松服务端止损；保留原有 Demo-only、对冲账户校验、下单前预检和不确定成交时锁定保护。

每周期成功入场上限 `max_entries_per_cycle=10` 与亏损止损阈值 `max_losing_stops_per_cycle=10` 是独立计数。每次止损事件都增加“全部止损数”；净 `net_pnl<0` 才默认增加“亏损止损数”。`count_profitable_stops=true` 时，全部止损数也参加暂停阈值，但仍分别展示两类数量。

`stop_rule_scope` 定义达到阈值时停止开仓的周期：

- `current_cycle`（默认）：平仓所在周期剩余时间不再开仓。
- `next_cycle`：暂停后一个完整周期，为旧版行为兼容选项。
- `both`：当前剩余周期和下一个完整周期都暂停。

阈值由止损实际平仓时刻所属周期决定。上一周期开仓的仓位若在当前周期止损，计入当前周期止损数并可能停止当前周期开仓。主动 `SESSION_CLOSE`、`MARGIN_STOP_OUT` 不记为止损。

### 2.6 账户级熔断、保证金和 swap

- 当日亏损以 UTC 服务器交易日为界，按该日开始权益比较当前已实现+浮动净值；达到 `max_daily_loss_pct` 即 `RISK_HALT`。回测下一个 UTC 日期解除；Demo 保持到下一服务器日，或操作者显式 `--reset-risk-halt`。
- 峰值回撤为 `(equity_peak - equity) / equity_peak`；达到 `max_drawdown_pct` 后同样只平不开。熔断期间持续管理现有仓位及止损，心跳暴露状态。
- 保证金占用按 `lots × contract_size_oz × mark_price / leverage` 简化估算；保证金水平为 `equity / used_margin × 100%`。达到 margin call 记录事件，低于/达到 stop-out 阈值时按最差浮动盈亏逐腿平仓，原因为 `MARGIN_STOP_OUT`。此模型不是券商逐 tick 强平的精确复制。
- 持仓跨越 UTC 服务器日时，按方向收取配置的每手每夜 swap；`triple_swap_weekday` 默认 2（Python weekday：周三）。回测不跨周末持仓的默认策略通常会减少周末计费，但实际日历及经纪商三倍计费日须核验。零费率是示例默认，不代表无融资成本。

### 2.7 成本、报价和 UTC blackout

回测默认 Bid OHLC，完整固定点差 `spread_usd=0.20`，每边滑点 `slippage_usd_per_side=0.10`，单边每手佣金 `commission_usd_per_lot_side=0`。买入按 Ask、卖出按 Bid 处理；成本拆分记录，不重复扣减。手续费、点差、滑点均可配置，另提供机械成本翻倍敏感性结果。

`blackout_windows` 格式为 UTC `HH:MM-HH:MM`，开始包含、结束不包含，支持跨午夜，仅禁新开仓；默认空列表，新闻时段由用户手工维护，不调用新闻源。

## 3. 数据字段定义

### 3.1 bar 输入

| 字段 | 类型 | 定义 |
|---|---|---|
| `timestamp_utc` | UTC datetime | bar 开始时间，唯一且递增 |
| `open/high/low/close` | float | 品种报价 OHLC，配置声明 Bid/Ask 侧 |
| `tick_volume` | int | tick 数/数据质量字段，不作为真实成交量 |
| `spread_points` | numeric | MT5 导出原始 spread，保留作审计，不与固定点差重复扣费 |
| `real_volume` | numeric | 若有则保留，不假定 CFD 成交量 |
| `source_file/source_row` | string/int | 可追溯源文件和行 |

### 3.2 策略/账本字段

至少保存 bar 收盘 UTC、1M/5M MACD 及交叉 ID、H1 range/midline、决策时间、方向/理由、持仓 ticket、手数、入场/止损/出场价和原因、周期 ID、周期入场/亏损止损/全部止损计数、已消费 cross、熔断/锁定、止损风险美元和 R 倍数、gross/spread/slippage/commission/swap/net PnL、MAE/MFE、配置/数据哈希。Demo `state.json` 还保存最后处理成交 ticket、已有本策略仓位标识和恢复所需状态。

### 3.3 数据完整性

- 检查时间排序、重复记录、OHLC 高低关系、正价格、UTC 标注及区间覆盖。
- 由完整 M1 窗口聚合 M5/H1 并与 MT5 独立导出数据审计；保留 spread 列。
- 大于 30 分钟的 bar 间隔作为交易休市用于会话边界；非休市数据缺口若影响持仓止损路径须标注不可核验，禁止伪造有利成交。
- MT5 `copy_rates_range` 返回 UTC 时间范围；导出时显式指定 UTC 并检查首尾覆盖。[MetaQuotes `copy_rates_range`](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py)

## 4. 伪代码

```text
load config; reject backtest if contract_size_oz is missing
load and audit UTC M1/M5/H1; assert aggregate and prefix-causal integrity
infer M1 session breaks where adjacent bar starts differ by >30 minutes

for each M1 event:
    update existing stop fills, swap, equity and margin state
    derive current five-hour cycle and server trading date
    if daily loss or peak drawdown threshold reached: set RISK_HALT (close-only)
    on UTC :00/:30: monotonically tighten stops from previous closed M5
    if this is last M1 before an inferred break:
        close all at this bar; label SESSION_CLOSE; do not count stop
    at each 5-minute entry check:
        if any shared entry gate blocks: record reason and continue
        require price vs H1 midline and unused same-direction M1/M5 crosses
        validate stop side and broker/configured minimum distance
        size from current equity, stop distance, contract size and lot step
        if risk sizing fails: record ENTRY_SKIPPED_RISK; leave crosses unused
        check available margin; otherwise record fill and consume crosses
    on STOP_LOSS: count total and net-losing stops in exit cycle
        apply current_cycle / next_cycle / both halt scope at configured threshold

emit trades, events, daily equity, open positions, audits and split reports
```

## 5. 回测、未来函数和过拟合方案

- 事件时钟为 M1；M5/H1 只用于已经收盘的数据。MACD/中线前缀因果断言及未来 OHLC 扰动测试必须通过。
- M1 入场以可用 bar 开盘价格模型成交，不用刚生成信号的未来收盘；止损更新只在事件时刻后生效。OHLC 不含 bar 内路径，无法证明真实 tick 先后顺序，需报告不确定性。
- 每笔交易单独保存入场初始风险 `initial_risk_usd`，`R = net_pnl / initial_risk_usd`；不同美元风险的交易不能只按金额比较。
- 样本区间默认按 UTC：2020–2022 开发、2023 验证、2024 最终 OOS（以 2025-01-01 截止）。参数先冻结再评估 OOS；报告滚动前推。若做大量参数搜索，还需多重试验/PBO 或 Deflated Sharpe 评估；不得把重复调参后的 OOS 当未见数据。
- 报告包括年化收益、日频 Sharpe（252 年化，零无风险率）、最大回撤、胜率、平均盈利/亏损比、止损数、换手率、R 分布、前 5 盈利交易利润集中度、多空拆分、分年和上涨/震荡/下跌状态、成本翻倍敏感性、MAE/MFE、每周期入场和止损数、swap、保证金强平与休市/风险事件。
- 成本翻倍是对已成交路径的机械敏感性，不会重新跑不同成本下的交易决策/止损路径；不可当作完整二次撮合回测。

## 6. Python 工程框架

```text
research/xauusd_trailing/
  models.py             # 配置模型与默认值
  data.py               # CSV 读取、UTC 归一化和 M1/M5/H1 审计
  indicators.py         # H1 区间、MACD 与周期时间
  rules.py              # 回测/Demo 共用的纯策略规则
  sessions.py           # M1 休市推断和 Demo 休市时刻来源
  engine.py             # M1 事件驱动回测、成本、保证金和 swap
  metrics.py            # 指标和诊断统计
  demo_runner.py        # Demo 状态恢复、心跳和策略调度
  walk_forward.py       # 开发/验证/OOS 分段
  run.py                # 离线回测入口
  config.example.yaml   # 无凭据策略配置
scripts/
  export_xauusd_mt5_history.py  # Demo 终端只读导出 M1/M5/H1
  run_xauusd_demo_strategy.py   # 显式确认后的 Demo 入口
  xauusd_watchdog.py            # 只检查心跳，不执行交易操作
tests/
  test_xauusd*.py       # 不依赖 MT5 的规则、引擎与假 adapter 测试
```

回测：`python -m research.xauusd_trailing.run --config research/xauusd_trailing/config.example.yaml`。Demo：`python scripts/run_xauusd_demo_strategy.py --confirm-demo-strategy`。数据导出需 `--confirm-demo-read`，且只读。README 提供数据准备、运行与安全细节。

## 7. 风控与暂停逻辑

1. 手数依止损距离风险定额；最大止损距离、最小手数/步长和合约大小缺失都会阻止开仓。
2. 每周期最多 10 次开仓，与每周期最多 10 次亏损止损分开统计；三种 stop scope 由共享函数处理。
3. 默认 `current_cycle` 的亏损止损计数归于平仓周期，因此老仓在新周期亏损止损也影响新周期入场资格。
4. 账户日亏损/峰值回撤任一熔断后只平不开；Demo 可等新 UTC 服务器日或手动复位，状态写入心跳和 state 文件。
5. 休市前 30 分钟停止新开仓；回测缺口前最后可成交 bar 清仓；Demo 使用券商时段或最近 20 个交易日 last tick 回退，时段未知时保守禁止新开仓。
6. 保证金不足时回测按配置模拟 margin call 和 stop-out；Demo 实际下单仍经过适配器下单预检，不能将回测简化公式当作券商保证金承诺。
7. `SESSION_CLOSE` 与 `MARGIN_STOP_OUT` 不算止损。盈利止损默认不增加亏损止损数；心跳同时给出两种止损总数。
8. Demo entry lockout 连续默认 60 次成功轮询后自动恢复；`--reset-lockout` 提供人工复位。命令复位不能代替排查订单状态。
9. 持久化状态损坏或无法读取时，不允许在已有本策略持仓状态不明的情况下继续自动交易；必须人工核对 MT5 持仓 ticket、方向、手数、止损与历史成交，再修复/恢复 state 文件。

## 8. 运行前检查清单

- [ ] MT5「图表最大柱数」设为不限；导出 M1/M5/H1 并核验 2020-01-01 至 2025-01-01 的首尾覆盖和 `coverage.json`。
- [ ] 确认目标券商符号名、合约大小、最小/步进/最大手数、tick size/value、账户币种、杠杆、保证金和 stops level。
- [ ] 确认 Demo 的服务器时钟/UTC 转换、交易时段来源与近 20 个交易日最后 tick 估算；在假期/夏令时/临时维护日人工复核。
- [ ] 确认点差、滑点、佣金、swap 费率与三倍计息日。配置中的零 swap/佣金只是默认假设。
- [ ] 用无 MT5 的测试跑完所有共享规则、休市、会话缓冲、风险尺寸、三种 stop scope、跨周期旧仓止损、熔断、保证金和恢复测试。
- [ ] 检查周末及日内休市没有跨时段持仓；`WEEKEND_CLOSE_UNVERIFIABLE` 应为 0；所有回测主动会话平仓定位在休市前最后可成交 bar。
- [ ] 对最终 OOS 冻结配置后只运行一次并保存代码版本、配置哈希、数据哈希和完整报告。
- [ ] Demo 运行时确认状态文件、心跳及 watchdog 告警路径有效；watchdog 只告警，不会自动平仓。
- [ ] 任何实盘部署或实盘下单须单独授权和另行评审；当前代码未提供实盘模式。

## 9. 风险与局限性

- M1 OHLC 无法恢复 bar 内 tick 顺序；止损跳空、报价侧、执行滑点都使用近似模型。
- 回测按固定点差、滑点和配置 swap 计算；成本可能在新闻、换日、开收市时变化。成本翻倍敏感性不是重新撮合。
- 休市由历史 bar 缺口推断，可能把严重数据中断误判成正常休市；回测应审查 session break 和数据审计，Demo 的 20 日 last tick 只是估算，不是券商承诺日历。
- 保证金和逐腿强平顺序是简化模型，不涵盖券商净额、动态保证金、负余额保护、滑点扩大或强平执行优先级。
- 风险仓位使用止损距离的理论预算；跳空和成本会令实际损失超过预算，连续持仓和相关性也会令账户波动高于单笔风险。
- Demo 进程断电/断网时无法继续更新移动止损；心跳和 watchdog 可报告进程失联，但不会恢复进程或自动管理仓位。
- 2024 OOS 单年不足以证明跨制度、极端行情或真实成交表现；回测无未来收益保证。

### 参考资料

- MT5 Python 历史 bar UTC 说明：[MetaQuotes `copy_rates_range`](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py)。
- MT5 交易品种属性：[MetaQuotes symbol properties](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants)。
- IANA 时区转换：[Python `zoneinfo`](https://docs.python.org/3/library/zoneinfo.html)。
