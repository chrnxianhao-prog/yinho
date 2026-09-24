# NautilusTrader 多市场真实数据纸面回测

本版本已经把原来的合成行情移除，链路改为：

`AKShare / MT5 -> CSV -> 清洗 -> ParquetDataCatalog -> BacktestNode -> 双 EMA 策略`

当前只做历史回测和纸面模拟，不连接真实下单接口，也不会发送真实订单。

## 一体化多市场终端（已经可用）

项目现在提供电脑和手机自适应的一体化终端，包含：

- A股、国内期货、黄金/外汇三套独立纸面账户和独立风控。
- EMA、MACD、RSI、ATR、成交量比率和可解释的 `0-100` 信号评分。
- `WATCH / ENTRY / STRONG_ENTRY / EXIT / STRONG_EXIT` 信号。
- K线、快速/慢速 EMA、自动强信号和人工备注标记。
- 在任意K线上输入强入场、强退出、止损、止盈、标签和文字备注。
- 每条备注自动绑定当时的指标快照；后续修改保留修订历史。
- 手工纸面买卖、自动强信号回放、持仓、资金、费用和盈亏。
- A股整手、禁止裸卖和纸面 T+1；期货合约乘数、保证金和多空持仓。
- 单笔数量、名义金额、最大持仓、只允许平仓和独立 Kill Switch。
- SQLite 本地审计账本：`data/runtime/quant_demo.db`。
- QMT/MiniQMT 和 CTP 接口就绪检查；未开户前保持 `RESERVED_ONLY`。
- MT5 只接受 Demo 账户，QMT/CTP 实盘执行仍然锁定。

最简单的启动方式是双击：

```text
启动量化终端.bat
```

也可以在 PowerShell 中运行：

```powershell
.\.venv\Scripts\python.exe scripts\run_web.py --open-browser
```

页面地址：`http://127.0.0.1:8787`

### 页面操作顺序

1. 顶部选择市场和标的。
2. 点击“刷新分析”查看真实历史K线和最新指标。
3. 点击某根K线，在右侧填写强入场/强退出备注并保存。
4. 在“指标与强度规则”中调整周期和阈值；保存后自动生成新版本。
5. “仅运行信号回放”只记录信号；“自动纸面回放”会重置当前市场纸面账户并按照强信号模拟交易。
6. 模拟买卖和自动回放都不会发送到真实券商或期货柜台。
7. 发生异常时开启该市场 Kill Switch；另两个市场不会被同时关闭。

指标、信号、备注、订单和风控的完整技术路线见
[`docs/CN_LIVE_TRADING_TECHNICAL_ROUTE.md`](docs/CN_LIVE_TRADING_TECHNICAL_ROUTE.md)。

### 自定义指标插件

复杂指标可以作为可信本地 Python 模块加载。模块需要提供：

```python
def register_indicators(registry):
    registry.register(MyIndicatorPlugin())
```

插件对象实现 `plugin_id`、`version` 和 `enrich(frame, params)`。在本机 `.env` 中配置：

```text
QUANT_PLUGIN_MODULES=my_indicators.custom_strength
```

插件只负责增加指标列，不能直接下单；需要在版本化信号规则中引用输出列，经过回放后才能参与纸面交易。

### A股和期货当前验证数据

下载或更新真实数据：

```powershell
.\.venv\Scripts\python.exe data_ingestion/fetch_ashare.py --symbol 600000 --start 20200101 --end 20260919 --adjust qfq
.\.venv\Scripts\python.exe data_ingestion/fetch_futures.py --symbol RB0 --start 20200101 --end 20260919
```

脚本为公开数据源提供自动回退。当前验证文件各包含 1629 根真实日线。

只构建并回测 A股和期货，不要求 MT5 数据已经准备好：

```powershell
.\.venv\Scripts\python.exe data_ingestion/build_catalog.py --reset --markets stock_cn futures_cn
.\.venv\Scripts\python.exe scripts/run_backtest.py --markets stock_cn futures_cn
```

运行核心测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 项目结构

```text
.
├─ configs/settings.yaml
├─ data/
│  ├─ ashare/                 # AKShare 股票 CSV
│  ├─ futures/                # AKShare 期货 CSV
│  ├─ mt5/                    # MT5 CSV
│  └─ catalog/                # Nautilus ParquetDataCatalog
├─ data_ingestion/
│  ├─ common.py               # OHLCV 清洗、Bar 构造、catalog 写入
│  ├─ fetch_ashare.py         # A 股日线/可选近期 1 分钟
│  ├─ fetch_futures.py        # RB0 主力连续日线
│  ├─ export_mt5_csv.py       # MT5 copy_rates_range 导出
│  └─ build_catalog.py        # CSV -> Parquet catalog
├─ docker/mt5/                # 可选 nautilus_mt5 Docker terminal
├─ scripts/run_backtest.py    # BacktestNode 真实数据回测
├─ scripts/test_mt5_demo_order.py # 一次性 Demo 开平仓测试
├─ scripts/run_web.py         # 电脑/手机响应式 Web 终端
├─ src/quant_demo/            # 配置、BarType、instrument、双 EMA 策略
│  ├─ web_app.py              # Demo 下单 API
│  ├─ web/index.html          # 响应式页面
│  └─ adapters/mt5_demo_adapter.py
├─ requirements.txt
└─ .env.example
```

## 环境和安装

推荐 Python 3.12（NautilusTrader 2.x 当前文档要求 Python 3.12+）。Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Linux/macOS 的虚拟环境激活命令改为 `source .venv/bin/activate`；MetaTrader5 Python 包只在 Windows 路径安装，MT5 导出也建议在 Windows 执行。

## 获取 A 股真实历史数据

默认配置使用长期日线：

```powershell
python data_ingestion/fetch_ashare.py `
  --symbol 600000 `
  --start 20180101 `
  --end 20261231 `
  --adjust qfq
```

输出 `data/ashare/600000_daily.csv`。脚本会统一列名、转 UTC、去重、排序并过滤无效 OHLC 行。

如果只研究最近可取得的 1 分钟窗口：

```powershell
python data_ingestion/fetch_ashare.py --symbol 600000 --minute --start 20260901 --end 20260917
```

AKShare 的 A 股 1 分钟公开接口不是长期历史源；若必须多年 1 分钟回测，需要可提供该粒度历史的行情源。

## 获取国内期货真实数据

`RB0` 表示螺纹钢主力连续序列，不是固定的 `rb2401` 合约：

```powershell
python data_ingestion/fetch_futures.py `
  --symbol RB0 `
  --start 20180101 `
  --end 20261231
```

输出 `data/futures/RB0_daily.csv`。当前使用 AKShare `futures_main_sina` 日线接口，包含 OHLC、成交量和持仓量字段。日线数据不包含夜盘逐分钟结构，也不执行真实换月撮合；严格夜盘、合约切换和展期收益需要逐合约数据和 roll 规则。

## 获取 MT5 EURUSD / XAUUSD 数据

### 推荐：Windows 本地 MT5 terminal

1. 安装 MetaTrader 5 终端并登录模拟账户。
2. 复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

3. 编辑 `.env`，只填写模拟账户值，不要提交 `.env`：

```text
MT5_LOGIN=你的模拟账号
MT5_PASSWORD=你的模拟密码
MT5_SERVER=你的模拟服务器
MT5_PATH=C:\Program Files\你的MT5终端\terminal64.exe
LIVE_TRADING_ENABLED=false
```

4. 确认 Market Watch 中有 `EURUSD`、`XAUUSD`，再导出：

```powershell
python data_ingestion/export_mt5_csv.py `
  --symbols EURUSD XAUUSD `
  --start 2024-01-01T00:00:00Z `
  --end 2026-01-01T00:00:00Z `
  --timeframe M1
```

脚本使用 `mt5.copy_rates_range()`，时间按 UTC 传入；优先使用 real volume，没有时回退到 tick volume。输出为 `data/mt5/EURUSD_M1.csv` 和 `data/mt5/XAUUSD_M1.csv`。

### MT5 Demo 一次性下单测试

项目新增了独立的 MT5 Demo smoke test。它通过 MetaTrader5 Python API 向已经登录的
MT5 Demo 账户发送一笔最小市价单，等待持仓出现后自动平仓。`account_info().trade_mode`
不是 Demo 时会拒绝执行；已有该 symbol 持仓时也会拒绝执行。MT5 官方的 `order_send`
使用交易请求结构，其中 volume 是经纪商手数。[官方 order_send 说明](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py)

先在 `.env` 中设置：

```text
MT5_DEMO_ENABLED=true
```

确认 MT5 终端当前确实是模拟账户，并且没有该 symbol 的已有持仓后，执行：

```powershell
python scripts/test_mt5_demo_order.py `
  --symbol EURUSD `
  --side BUY `
  --volume 0.01 `
  --hold-seconds 3 `
  --confirm-demo-order
```

缺少 `--confirm-demo-order` 时不会连接 MT5、更不会下单。XAUUSD 的最小手数取决于经纪商；
如果开仓成功但自动平仓失败，请立即在 MT5 终端人工检查并平仓。

## 6. 电脑端 / 手机端下单页面

项目提供一个响应式 Web 终端，同一页面可在电脑浏览器和手机浏览器使用。它只调用
MT5 Demo 适配器，不连接实盘账户；页面支持查看账户、报价、持仓、买入、卖出和平仓。

启动电脑本机访问：

```powershell
python scripts/run_web.py
```

打开：`http://127.0.0.1:8787`

如果要让同一局域网内的手机访问，先在 `.env` 设置一个随机访问令牌：

```text
WEB_API_TOKEN=请替换为足够长的随机字符串
WEB_MAX_VOLUME=0.01
WEB_SYMBOLS=EURUSD,XAUUSD
```

然后使用局域网监听：

```powershell
python scripts/run_web.py --host 0.0.0.0 --port 8787
```

手机访问运行电脑的局域网 IP，例如 `http://192.168.1.20:8787`，页面顶部输入
`WEB_API_TOKEN`。未设置令牌时，程序拒绝监听非本机地址。下单按钮和后端 API 都要求
二次确认；服务端还会限制品种白名单和最大手数。

不要把 8787 端口暴露到公网，也不要在网页、聊天或代码中填写 MT5 密码。当前 Web
终端是单用户本地/局域网 Demo 工具，不是生产级多用户交易系统。

### 可选：社区 nautilus_mt5 Docker terminal

本项目提供 `docker/mt5/docker-compose.yml`，但历史导出本身不依赖该适配器：

```powershell
New-Item -ItemType Directory -Force .external | Out-Null
git clone https://github.com/alifnurc/nautilus_mt5 .external/nautilus_mt5
Copy-Item docker/mt5/.env.example docker/mt5/.env
docker compose --env-file docker/mt5/.env -f docker/mt5/docker-compose.yml up --build -d mt5-wine
```

打开 `http://localhost:60832/vnc.html` 完成 MT5 模拟终端登录。不同版本的社区仓库可能调整 Dockerfile、端口或 Wine 参数；构建失败时以该仓库 README 为准。当前项目仍采用 CSV 导出再回测，不会启动实盘 Node。

## 构建 catalog 并运行回测

三个数据源的 CSV 都准备好后：

```powershell
python data_ingestion/build_catalog.py --reset
python scripts/run_backtest.py
```

`--reset` 只删除项目内 `data/catalog/` 并重建。回测使用三个独立模拟 venue/account：A 股 `SIM_STK / PAPER-STOCK-CNY`，期货 `SIM_FUT / PAPER-FUT-CNY`，外汇/黄金 `SIM_FX / PAPER-FX-USD`。每个 instrument 使用自己的 BarType 和指标状态；批量分析、纸面回放和 Nautilus 回测共用 `indicator_engine` 的指标计算与评分逻辑。指标参数以 `configs/signal_rules.yaml` 为默认来源，网页中保存的当前规则版本会被回测优先读取。自动回测沿用纸面规则，只在信号转入 `STRONG_ENTRY` / `STRONG_EXIT` 时调仓。日志输出评分、原因和订单，报告写入 `artifacts/`。

可用 `--output-dir` 把本次报告写入独立目录，保留以往回测结果，例如：`python scripts/run_backtest.py --output-dir artifacts/run_2026-09-23`。

当前 Nautilus 策略支持内置指标和 `custom_conditions`；外部指标插件尚未提供逐K线流式适配器，若生效规则引用 `plugins`，回测会明确报错而不会静默使用另一套信号。

## 配置和市场差异

`configs/settings.yaml` 已包含：

```yaml
data:
  mt5_data_path: data/mt5/
  ashare_data_path: data/ashare/
  futures_data_path: data/futures/
  catalog_path: data/catalog/
```

股票使用 `Equity`、CNY 现金账户、整手 100 股；涨跌停、T+1、停牌、印花税等 A 股规则尚未完整建模。期货使用 `FuturesContract`、乘数、保证金和最小变动价位；RB0 连续序列的有效期是回测代理窗口，不等同真实逐合约换月。EURUSD 使用 `CurrencyPair`，XAUUSD 使用 `Commodity` 的现货/CFD 风格定义，报价货币均为 USD。经纪商若使用 `XAUUSDm`、`EURUSD.a` 等名称，需要同步修改 YAML 和导出命令。

## 常见问题

- `No bars for ...`：检查 CSV 文件名是否与 YAML 的 `data_file` 完全一致，然后重新执行 `build_catalog.py --reset`。
- AKShare 超时或为空：检查网络、日期和代码，服务端限流时稍后重试；不要把空 CSV 写入 catalog。
- MT5 初始化失败：检查终端、账号、服务器、`MT5_PATH`，并确认两个 symbol 已启用。
- MT5 时间偏移：`copy_rates_range()` 使用 UTC，不要导出后再次加减时区。
- Docker 找不到 `mt5.dockerfile`：检查 `.external/nautilus_mt5` 或 `NAUTILUS_MT5_REPO_DIR`。
- 期货没有夜盘：这是日线主力连续数据的限制；夜盘研究必须换逐分钟/Tick 数据和交易所日历。
- 本基础版不需要 PostgreSQL/Redis：离线回测用 Parquet catalog 即可；服务化、实时 WebSocket、审计事件总线阶段再引入它们。

配置加载器强制要求 `app.mode: paper`、`live_trading_enabled: false`、`data.source: parquet_catalog`。所有账号字段只从环境变量或本地 `.env` 读取，仓库没有实盘下单代码。
