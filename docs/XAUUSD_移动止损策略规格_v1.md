# XAUUSD 移动止损策略工程规格 v1

状态：回测模块 v1 已实现并有自动化测试；规则按用户确认的默认口径执行。本文只覆盖历史回测和模拟盘，不包含实盘下单程序。

## 1. 需求复述与澄清结论

目标是把 XAUUSD 的 1H 区间过滤、1M/5M MACD 同向交叉、分批开仓、单向移动止损、周期限额和周末清仓规则，做成可复现、可审计、可作样本外评估的 Python 策略。

| 项目 | 当前规格 | 状态 |
|---|---|---|
| 数据区间 | 2020-01-01 起，至 2025-01-01 止（结束时间不包含） | 已确认 |
| 数据源 | MT5 导出的 XAUUSD M1、M5、H1 | 已确认；需审计实际覆盖范围 |
| 交易时区 | `America/Mexico_City`；周期、检查和周末规则按墨西哥城当地民用时间解释，账本时间统一保存 UTC | 已确认 |
| 区间 | 最近 5 根已收盘 H1 | 已确认 |
| 信号 | 每 5 分钟检查；最近 5 分钟内 M1 与 M5 均出现同方向 MACD 交叉；MACD 12/26/9 | 已确认 |
| 单次开仓 | 0.05 手 | 已确认；需核对券商最小手数和步长 |
| 每周期开仓数 | 多空合计不超过 10 次 | 已确认 |
| 总持仓 | 不设跨周期累计手数上限；即使旧仓仍在，也按新周期规则继续开仓 | 2026-09-24 最新确认，覆盖此前 0.50 手总仓上限 |
| 资金 | 2,000 USD | 按默认假设；账户币种仍需对照 MT5 账户规格 |
| 成本 | 佣金 0；固定完整点差 0.20 美元；单边滑点 0.10 美元 | 已确认；滑点单位按每盎司报价美元解释 |
| 出场 | 不设止盈；只用初始止损和移动止损；不看反向指标 | 已确认 |
| 移动止损 | 当地时间每小时 :00、:30 检查一次；只用上一根已收盘 M5 | 已确认 |
| 止损后暂停 | 每周期第 10 次净亏损止损后暂停下一个完整五小时周期；盈利止损退出不计入 | 2026-09-25 最新确认 |
| 周期锚点 | Demo 明早墨西哥城 09:00 开始一个新周期，之后每 5 小时刷新；已有仓位不阻止新周期开仓 | 2026-09-25 最新确认 |
| 隔夜/周末 | 可隔夜；周末前主动清仓 | 已确认 |
| 周末清仓时刻 | 周五 21:55 墨西哥城当地时间 | 已确认；成交报价必须在截止前且不陈旧 |

当前不生成投资建议。后续 AI 输出限于对规则状态、入场条件和已有持仓的解释或建议；下单执行边界另行授权。

## 2. 策略规格说明书

### 2.1 时间与周期

- 使用 IANA 时区键 `America/Mexico_City`，不把 UTC-6 写死。策略历史包含墨西哥城实行夏令时的年份；时区数据库应负责把历史本地时间正确映射为 UTC。
- 例如 2024 年墨西哥城周五 21:55（UTC-6）对应 UTC 周六 03:55；2020–2022 的部分夏令时日期会对应 UTC 周六 02:55。周五 21:55 当地时刻可能晚于 XAUUSD 周末休市开始时间，必须用目标 MT5 账户的实际交易日历确定可成交的主动清仓时刻。
- 原始 MT5 bar 时间转换成带时区的 UTC 时间后再排序、去重和计算。界面显示墨西哥城本地时间；所有数据库事件另存 UTC 时间戳。
- 每 5 分钟在墨西哥城本地分钟为 `00、05、10、…、55` 的边界检查一次，只读取该时刻之前已收盘的 bar。每 30 分钟止损更新安排在本地 `:00` 和 `:30`。
- 周期从配置锚点开始，每经过 5 小时刷新一次；本次 Demo 锚点为明早墨西哥城 09:00，后续边界为 14:00、19:00 等。历史回测锚点默认为 `2020-01-01 09:00` 本地时间。每个边界重置该周期的开仓额度；老仓跨周期继续独立移动止损，不阻止新周期按信号开仓。
- 只有止损平仓的实际净盈亏小于 0 才计入亏损止损次数。MT5 按成交历史的 profit、commission、swap、fee 合计；回测按扣除点差、滑点和佣金后的 `net_pnl`。盈利或不亏损的止损退出不计数。第 10 次净亏损止损后，暂停紧接着的一个完整五小时周期；暂停期间已有持仓继续管理止损。
- Demo 运行器不在明早 09:00 自动停止开仓或退出；策略会继续按周期运行，直到用户明确停止进程。停止进程不会主动平仓，已有服务器端止损仍保留在券商端，但进程停止后不会继续更新移动止损。
- 2020-01-01 为预热期起点。指标先使用区间开始前可取得的数据预热；若数据不包含预热数据，则正式信号从满足所有窗口后的首个时点开始，不能把缺失历史补造出来。

### 2.2 H1 区间

在检查时刻 `t`，选出收盘时间 `<= t` 的最近 5 根 H1 bar，记为 `H1…H5`、`L1…L5`：

```text
range_high(t) = max(H1, H2, H3, H4, H5)
range_low(t)  = min(L1, L2, L3, L4, L5)
avg_high(t)   = (H1 + H2 + H3 + H4 + H5) / 5
avg_low(t)    = (L1 + L2 + L3 + L4 + L5) / 5
midline(t)    = (avg_high(t) + avg_low(t)) / 2
```

实际做多做空条件只用 `midline`；`range_high` 和 `range_low` 为区间输出与审计字段，不作为额外入场或出场条件。少于 5 根已收盘 H1 bar 时不交易。

### 2.3 MACD 与入场信号

对每个周期分别用收盘价计算：

```text
macd_line = EMA(close, 12) - EMA(close, 26)
signal_line = EMA(macd_line, 9)
histogram = macd_line - signal_line
```

复现约定：EMA 使用递归系数 `alpha=2/(period+1)`；初值取首个完整周期的简单平均，MACD signal EMA 从首个有效 MACD 值开始累计并在 9 个有效值后输出。MT5 内置指标若采用不同初始化方式，应做逐 bar 对照并记录差异；策略计算不切换初始化口径。

交叉按已收盘 bar 定义：

```text
golden_cross(t) = macd_line[t-1] <= signal_line[t-1]
                  and macd_line[t] > signal_line[t]
dead_cross(t)   = macd_line[t-1] >= signal_line[t-1]
                  and macd_line[t] < signal_line[t]
```

每个检查时刻 `t`，在时间闭区间 `[t-5min, t]` 查找已确认交叉：

- 做多：最新可用价格 `P(t) > midline(t)`，M1 有金叉，M5 有金叉。
- 做空：`P(t) < midline(t)`，M1 有死叉，M5 有死叉。
- `P(t) == midline(t)` 时不产生新入场。
- 做多与做空可以分别持有；总手数按 `sum(abs(position.lots))` 计算。
- 默认工程假设：一个 MACD 交叉事件最多被使用一次；M1、M5 交叉各自保存时间戳，开仓幂等键由两个交叉时间、方向和策略版本组成。这样一个持续落在 5 分钟窗口内的旧交叉不会在后续检查重复开仓。
- 默认工程假设：只有实际成交的 0.05 手开仓才消耗一次周期开仓额度；因总手数上限、无效止损、数据缺失或风控拒绝的信号记为拒绝事件，不占已成交开仓次数。
- 入场方向和信号不得引用形成中的 M1/M5/H1 bar。MT5 bar 时间是 bar 开始时刻；M1 的 `time=t-1min` bar 在 `t` 时才完整收盘。

### 2.4 开仓与初始止损

每次开仓固定 0.05 手。开仓前同时检查：周期开仓计数小于 10；存在完整信号；初始止损在可接受的一侧；数据没有断档；当前不是周末清仓禁入窗口。持仓手数不作为拒绝新开仓的条件；未平仓头寸跨周期累积时，总暴露可超过此前的 0.50 手。

- 多单初始止损：开仓决策时刻前最近一根已收盘 M5 的最低价。
- 空单初始止损：开仓决策时刻前最近一根已收盘 M5 的最高价。
- 若多单参考低点不低于可成交 Bid，或空单参考高点不高于可成交 Ask，视作止损无效，拒绝开仓并记审计原因；不把止损移到更差的位置。
- 回测信号在已收盘 bar 上确认，成交使用下一条可用 M1 bar 的开盘报价；不得用刚用于生成信号的收盘价假设成交。

### 2.5 移动止损与平仓

每个持仓腿独立维护止损价 `S`。仅在墨西哥城每小时 `:00` 或 `:30` 的更新事件运行：

```text
candidate_long  = previous_closed_M5.low
new_long_stop   = max(old_stop, candidate_long)

candidate_short = previous_closed_M5.high
new_short_stop  = min(old_stop, candidate_short)
```

- 若候选止损在当前可成交报价的错误一侧，则不追价、不放宽原止损，记录 `TRAIL_UPDATE_SKIPPED_MARKET_SIDE`。
- 止损更新只在该时刻生效，不能回头使用更新前已经发生的 bar 高低价触发新止损。
- 止损触发后以市价模拟平仓，不设止盈。多单用 Bid 触发和卖出；空单用 Ask 触发和买入。
- 只有因该止损价触发而平仓的交易，`close_reason=STOP_LOSS`。若该腿实际净盈亏小于 0，增加当期亏损止损计数；净盈亏大于等于 0 时仍记为止损出场，但不计入暂停阈值。
- 周末主动清仓标记为 `FORCED_WEEKEND_CLOSE`；不计入亏损止损次数、不触发暂停，但记录为主动平仓及费用事件。
- 周期切换只重置当期已成交开仓数和当期亏损止损数；旧持仓及其止损状态延续。
- 亏损止损按实际平仓成交所在的墨西哥本地周期计数；旧持仓在新周期止损时，计入新周期。

### 2.6 成交成本与账户模型

默认输入为 Bid OHLC、固定完整点差 `0.20 USD/oz`、单边滑点 `0.10 USD/oz`、佣金 0。定义 `mid = bid + spread/2`：

```text
Ask = Bid + 0.20
买入成交价 = Ask + 0.10
卖出成交价 = Bid - 0.10
多单平仓价 = Bid - 0.10
空单平仓价 = Ask + 0.10
```

止损跨价跳空时，不假设按止损价成交：多单若 M1 开盘 Bid 已低于止损，按开盘 Bid 再减滑点；空单若 M1 开盘 Ask 已高于止损，按开盘 Ask 再加滑点。否则按止损触发价加减滑点。点差和滑点分开记录，不能重复扣减。

盈亏计算需要 `contract_size_oz_per_lot`。以 mid 价格变化计算毛盈亏，再单独扣一次点差与滑点，避免成交价已含成本后重复扣费：

```text
多单毛盈亏 = lots * contract_size_oz_per_lot * (exit_mid - entry_mid)
空单毛盈亏 = lots * contract_size_oz_per_lot * (entry_mid - exit_mid)
净盈亏 = 毛盈亏 - spread_cost - slippage_cost - 佣金 - swap - 其他已配置费用
```

实际 Bid/Ask 成交价仍用于止损触发和模拟撮合；`spread_cost` 与 `slippage_cost` 作为独立账本字段列示，不再从已含这些成本的成交价盈亏重复扣除。跳空造成的止损成交价格变化计入实际行情价格变动，额外执行滑点仍按不利方向计成本。

不硬编码一手等于多少盎司。回测启动时从 MT5 导出的合约规格或 `symbol_info` 导入合约大小、最小/最大手数、手数步长、tick size/value 和账户币种。没有合约大小时可以验证信号和订单时序，但不得发布以美元计的盈亏、收益率或风险指标。

初始净值暂设 2,000 USD。当前规则不设跨周期总手数上限；每周期最多开 10 次、每次 0.05 手，但未平仓头寸跨周期累积时，总暴露可继续增长。回测不模拟无限杠杆；保证金占用、强平和账户杠杆须另行纳入，不能据此假定账户可承受累积仓位。允许隔夜；隔夜 swap 尚未提供，不能把当前 swap 值伪装成 2020–2024 历史值。第一版分别报告“不含 swap 的基础结果”和 swap 成本敏感性；拿到有日期的历史 swap 序列后再给完整净值结果。

## 3. 数据字段定义

### 3.1 原始 bar 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `timestamp_utc` | datetime64[ns, UTC] | MT5 bar 开始时间；唯一索引 |
| `open`, `high`, `low`, `close` | float64 | XAUUSD 报价；需确认 MT5 导出 OHLC 是 Bid |
| `tick_volume` | int64 | MT5 tick volume；仅数据质量/诊断，不参与策略规则 |
| `spread_points` | int64/float | MT5 导出点差；固定点差假设回测时保留作对照，不与固定点差重复计费 |
| `real_volume` | int64 | 若有则保留；不作为 FX/CFD 真实成交量假设 |
| `source_time_zone` | string | 数据导出时区声明，建议 `UTC` |
| `source_file`, `source_row` | string/int | 可追溯到输入文件行 |

### 3.2 派生字段与事件字段

| 字段 | 说明 |
|---|---|
| `bar_close_time_utc` | `timestamp_utc + timeframe`；只有此时刻不晚于决策时刻才算已收盘 |
| `timestamp_mexico` | UTC 转换后的本地展示时间，保留 UTC offset 与 DST fold 信息 |
| `macd`, `macd_signal`, `macd_hist` | 1M/M5 两套指标，分别计算，不混用周期 |
| `cross_type`, `cross_time_utc`, `cross_id` | 金叉/死叉、确认时间、不可重复消费的信号 ID |
| `range_high`, `range_low`, `avg_high`, `avg_low`, `midline` | 最近 5 根已收盘 H1 的区间字段 |
| `decision_time_utc`, `decision_time_mexico` | 检查时间的双时区记录 |
| `signal_direction`, `signal_reason` | LONG/SHORT/NONE 及通过/拒绝原因 |
| `order_id`, `position_id`, `side`, `lots` | 模拟订单和独立持仓腿 |
| `entry_bid`, `entry_ask`, `entry_fill`, `initial_stop`, `active_stop` | 入场报价、成交价、初始和当前止损 |
| `exit_fill`, `close_reason` | 平仓成交价；`STOP_LOSS` 或 `FORCED_WEEKEND_CLOSE` |
| `gross_pnl`, `spread_cost`, `slippage_cost`, `commission`, `swap`, `net_pnl` | 逐腿成本与盈亏审计字段 |
| `cycle_id`, `entries_in_cycle`, `losing_stops_in_cycle`, `paused_until` | 本地周期、开仓和净亏损止损计数及暂停状态 |
| `strategy_version`, `config_hash`, `data_hash` | 复现策略配置和输入数据 |

### 3.3 数据质量门槛

- 三种周期按 UTC 排序，检查重复时间、OHLC 合法关系、负/零价格、缺失区间、异常时区偏移和时间戳单位。
- 由 M1 按 MT5 周期边界聚合 M5/H1，与独立导出的 M5/H1 对比 OHLC；差异生成报告。策略计算统一使用同一套边界，避免三份文件错位。
- 不把周末、节假日和日内休市的正常空档当成缺失 bar；另用交易时段日历区分正常休市与数据缺失。
- 缺失数据期间禁止新开仓。若止损可能触发但没有可执行报价，标记 `DATA_GAP_STOP_UNCERTAIN`，不得静默按有利价格填单；保守统计按最坏可验证报价或将该段列为不可评估。
- MT5 Python 的历史接口返回 bar 开始时间并按 UTC 保存；导出流程需写清数据时区，不能把无时区时间戳直接当本机时间。[MetaTrader 5 `copy_rates_range` 文档](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py)

## 4. 伪代码

```text
load config, symbol specification, timezone database version
load M1/M5/H1; normalize all timestamps to UTC
validate data; aggregate M5/H1 from M1 and compare with exported bars
warm up EMA/MACD using pre-test history
positions = []

for each UTC minute event t in test interval:
    local_t = t converted with America/Mexico_City

    if t is scheduled weekly flatten deadline:
        close all positions at the last executable quote at/before deadline
        label FORCED_WEEKEND_CLOSE; do not count as stop

    update/trigger existing stops using only prices available at t
    if local_t is :00 or :30:
        candidate = previous fully closed M5 low/high
        tighten each stop monotonically; never loosen
        apply new stop only from this event onward

    if local_t is a 5-minute check boundary:
        derive current local cycle_id and its quota/pause state
        if paused, at weekly flatten cutoff, or data is incomplete: record skip
        else:
            calculate midline from last 5 closed H1 bars
            find unused M1 and M5 crosses in [t-5min, t]
            if price > midline and both crosses are bullish:
                if entries < 10:
                    create 0.05-lot LONG at next available M1 open + ask/spread/slippage
                    initial stop = previous closed M5 low
                    reject if initial stop is not below executable market side
                    increment entry count only on successful simulated fill
            elif price < midline and both crosses are bearish:
                symmetric SHORT entry with previous closed M5 high

    on a STOP_LOSS fill:
        increment stops_in_cycle for the cycle containing the close event
        if stops_in_cycle == 10:
            pause the next complete local cycle; continue managing existing stops

at end of sample:
    mark remaining positions to market for open-position metrics
    do not silently force-close them at the sample endpoint
    emit trades, equity curve, costs, rejects, pause events, and data audit
```

若同一 M1 bar 的 OHLC 同时满足止损更新前后可能的不同路径，使用只会更不利于策略的成交假设，并将该次歧义单独计数。历史 M1 OHLC 不包含 bar 内 tick 顺序，不能声称还原了真实止损成交路径。

## 5. 回测方案

### 5.1 成交与市场成本

- 主回测以 M1 为事件时钟；M5/H1 用于信号、区间和止损更新。
- 使用 Bid OHLC 构造 Ask=Bid+0.20；逐次成交再应用每边 0.10 美元滑点，佣金设为 0。
- 止损触发以可执行一侧报价判断；有跳空时按下一可执行开盘价成交并加不利滑点。
- 周末清仓必须落在有效且足够接近截止时间的报价。实现默认只接受截止前 5 分钟内的最后一根 M1 收盘；若 21:55 已休市且最后报价更早，则不虚构成交，标记 `WEEKEND_CLOSE_UNVERIFIABLE`、保留未平仓并将回测标记为不可用于绩效审查。取得目标账户交易日历后，可按真实最后可成交时间配置，不把陈旧价格冒充成交。[Exness 工具交易时间](https://get.exness.help/hc/en-us/articles/4405235684498-Instrument-trading-hours)
- 可隔夜意味着跨日持仓会继续承受敞口；swap 历史缺失时要单独报告，不并入“完整成本”结果。

### 5.2 样本切分与过拟合控制

按 UTC 日期切分，区间结束日不包含：

| 数据集 | 日期 | 用途 |
|---|---|---|
| 开发/训练 | 2020-01-01 至 2023-01-01 | 验证实现、只在此范围做有限参数敏感性 |
| 验证 | 2023-01-01 至 2024-01-01 | 选择冻结配置，不继续反复试参 |
| 最终样本外 | 2024-01-01 至 2025-01-01 | 只运行一次冻结配置并报告，不用于调参 |

另外做滚动前推：训练窗口扩展到某个日期，下一段作为未见期；每个折叠分别记录结果。MACD 默认参数和用户规则先作为基准，不因 OOS 表现不好再改完重测并把新结果称为样本外。若比较大量候选参数，增加 PBO/CSCV 或 Deflated Sharpe 类多重试验偏差评估；同时报告尝试过的参数数目。

### 5.3 未来函数检查

- **已收盘约束：** 每个信号的所有来源 bar 的 `bar_close_time <= decision_time`。
- **前缀一致性：** 对任一历史时刻，只用截至该时刻的数据重算，过去的信号、区间和止损不得改变。
- **未来扰动测试：** 修改检查时刻之后的 OHLC，时刻之前的指标和订单必须完全不变。
- **执行延后一根：** 入场不得在产生交叉的同一根 bar 收盘价成交；成交落在下一可用 M1 开盘。
- **止损顺序：** 更新时只用上一根完整 M5；新止损不能在生效前被同根历史低/高价触发。
- **切分隔离：** 标准化、参数选择、缺失值规则和成本校准不得读取最终 OOS 数据。

### 5.4 指标口径

- 年化收益：从逐日净值曲线计算，明确按日历年化因子；未平仓按可成交方向盯市。
- 夏普：按日净收益计算，列明无风险利率假设（基准取 0）及年化因子。
- 最大回撤：净值相对历史峰值的最大百分比回撤。
- 胜率：已平仓持仓腿中净盈亏大于 0 的比例；同时列出未平仓数量。
- 盈亏比：平均盈利持仓腿净盈亏 / 平均亏损持仓腿净盈亏绝对值。
- 止损出场总数：统计全部 `STOP_LOSS`；暂停阈值另只统计净亏损的止损出场；`FORCED_WEEKEND_CLOSE` 单列。
- 换手率：报告成交总名义金额 / 时间加权平均净值，并同时给开仓次数和总手数，方便解释口径。
- 附加审计：总交易数、方向分布、成本拆分、最长持仓、隔夜次数、周期额度拒绝数、暂停周期数、数据缺口数、强制周末平仓数、期末未平仓数。
- 结果分训练、验证、OOS 和滚动前推各表展示，并附净值曲线、逐笔交易和配置/数据哈希。

## 6. Python 工程框架

实际模块已建于 `research/xauusd_trailing/`：

```text
research/xauusd_trailing/
├── README.md                 # 数据约定、限制和运行说明
├── config.example.yaml       # 策略、数据路径与输出；不含凭据
├── models.py                 # 回测配置
├── data.py                   # MT5 CSV 导入、UTC 归一化、数据审计
├── indicators.py             # SMA-seeded EMA/MACD 与 H1 区间
├── causality.py              # 未来数据扰动/前缀一致性检查
├── engine.py                 # M1 事件驱动撮合、持仓和周期风控
├── metrics.py                # 收益、Sharpe、回撤、胜率、盈亏比、换手
├── walk_forward.py           # 2020–2022 开发、2023 验证、2024 OOS
└── run.py                    # CSV → 回测 → 审计文件；不连接 MT5

自动化测试位于 `tests/test_xauusd_backtest.py`。运行命令和测试口径见模块 README。
```

配置文件包含 `timezone=America/Mexico_City`、`check_interval=5m`、`range_bars=5`、`macd=(12,26,9)`、`entry_lots=0.05`、`max_entries_per_cycle=10`、不设跨周期总持仓上限、`initial_equity=2000 USD`、`spread_usd=0.20`、`slippage_usd=0.10`、`commission=0`、样本日期和周末平仓政策。MT5 登录信息不参与回测配置。

## 7. 风控与暂停逻辑

1. 不设多空合计总手数上限；每个本地五小时周期仍最多开仓 10 次，每次固定 0.05 手。跨周期遗留仓位可能令总暴露继续累积。
2. 每笔独立持仓腿固定 0.05 手，独立保存信号来源、入场价、止损和出场原因。
3. 每个本地周期成功成交的开仓最多 10 次；平仓不会恢复本周期开仓额度。
4. 每个周期统计净亏损的 `STOP_LOSS` 平仓；盈利止损不计数。第 10 次亏损止损后将下个完整五小时周期加入暂停表。
5. 周期刷新后，旧仓位不阻止新仓；暂停时仅拒绝新开仓，继续管理已有仓位止损；暂停周期结束后按周期边界恢复。
6. 主动周末平仓不算止损；其它原因的主动平仓如果未来加入，也必须有独立原因枚举，不能写成止损。
7. 报价缺失、时区不明、MACD 不完整、合约规格缺失或订单步长不合法时拒绝新开仓，并写明拒绝原因。
8. 本策略不含止盈、反向 MACD 平仓、中线反向平仓、加仓以外的指标过滤或 AI 自行改规则。
9. 这一版最大手数限制不等于经纪商保证金风控；在确认杠杆/保证金规格前，不把回测结果描述为可承受真实账户回撤。

## 8. 模拟盘/实盘前检查清单

- [ ] 确认 CSV 的 OHLC 报价侧、时间戳单位、UTC 声明、符号名称与完整日期覆盖。
- [ ] 用同一批 MT5 数据验证 M1 聚合 M5/H1 与独立导出数据一致。
- [ ] 从目标 MT5 Demo 的 XAUUSD 规格导出合约大小、tick size/value、volume min/step/max、账户币种、保证金和交易日历。
- [ ] 确认 0.05 手符合最小手数和交易步长。
- [ ] 确认 0.20 美元固定点差及 0.10 美元单边滑点的单位和压力测试区间。
- [ ] 提供历史 swap 或明确接受第一版只做无 swap 基准加敏感性分析。
- [ ] 用经纪商交易日历确认墨西哥城周五 21:55 对应的 XAUUSD 是否可交易，以及休市前实际平仓时间。
- [ ] 验证 Mexico City 夏令时历史处理、重复/跳过本地时间及每 5 分钟/30 分钟事件。
- [x] 完成未来数据扰动、已收盘 H1、成本拆分、止损计数、暂停周期和周末主动平仓自动化测试。
- [ ] 用目标 MT5 交易日历复核周末最后可成交时间，并补齐数据缺口/跳空成交路径核验。
- [ ] 冻结配置后只运行一次 2024 OOS；保存数据哈希、代码版本、日志和指标报告。
- [ ] 先运行只读信号监控和本地 Paper；任何 Demo 自动执行都需单独明确授权与独立风控评审。

## 9. 风险与局限性

- M1 OHLC 不能还原一分钟内部 tick 顺序，止损成交需要保守近似；tick 数据能进一步改善，但仍不能复现全部延迟和流动性。
- 固定点差和固定滑点不代表历史真实交易成本；黄金在波动、换日、开收市时成本可能变化。
- 佣金 0 不代表无成本；隔夜 swap、融资和账户币种换算仍可能改变结果。
- `0.05` 手的美元盈亏依赖 XAUUSD 合约大小；不同经纪商/账户可能不同。
- 总手数不封顶且未模拟保证金、杠杆和强平时，回测不能反映累积暴露导致的保证金压力或强平风险；Demo 结果也不能证明实盘可承受。
- 周五 21:55 墨西哥城时间可能落在该券商 XAUUSD 已休市时段；必须用目标账户实际交易日历确定最后可成交时刻，不能用停市前陈旧价格填单。
- 2020–2022 墨西哥城夏令时规则与之后年份不同；必须使用 IANA 时区数据库，固定 UTC offset 会错置历史检查和周末时间。
- 五小时窗口从指定的本地锚点连续滚动；墨西哥城 09:00 是本次 Demo 新周期锚点，之后严格每 5 小时刷新。
- 2024 只有一个日历年 OOS，不足以证明跨市场制度和极端行情稳定；结果仅用于工程验证。
- 回测结果不保证未来表现，也不构成投资建议。

### 规范参考

- Python `zoneinfo` 使用 IANA 时区数据库，适合按墨西哥历史民用时间处理夏令时：[Python zoneinfo 文档](https://docs.python.org/3/library/zoneinfo.html)。IANA 2022f 记录墨西哥多数地区在 2022 年后不再实行夏令时：[IANA tzdb 2022f](https://www.iana.org/time-zones/releases/2022f)。
- MT5 Python 历史 bar 时间按 UTC 解释：[MetaQuotes `copy_rates_range`](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py)。
- MT5 bar 结构包含 OHLC、tick volume、spread 和 real volume：[MetaQuotes `MqlRates`](https://www.mql5.com/en/docs/constants/structures/mqlrates)。
- 手数、合约大小、tick 规格和 swap 是交易品种属性，需从目标 MT5 账户读取：[MetaQuotes symbol properties](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants)。
- Exness 工具交易时间表按品种、日期、时区、假期和夏令时变化；须在目标账户核对具体 XAUUSD 日历：[Exness instrument trading hours](https://get.exness.help/hc/en-us/articles/4405235684498-Instrument-trading-hours)。

### 运行前数据依赖

1. 导出 MT5 的 Bid OHLC M1/M5/H1 CSV，并确认时间戳来源时区和完整覆盖范围；三周期必须通过聚合一致性审计。
2. 从目标 MT5 账户提供 XAUUSD 合约大小和账户币种。示例配置的 `contract_size_oz` 留空时，只输出信号/时序结果，不输出美元绩效。
3. 历史 swap 尚未提供；有合约规格后可以先运行不含 swap 的金额基准和成本敏感性分析，报告明确标注“未计隔夜费”。
4. 默认周末报价容忍度为截止前 5 分钟。若经纪商在 21:55 前已休市，先从账户交易日历确认其最后可成交时段；超过报价容忍度会标为不可核验，不会伪造成交。
5. 2020–2024 历史交易日历/正常休市与数据断档尚未导入；出现持仓跨数据缺口时，该次回测自动标记为不可用于绩效审查。
