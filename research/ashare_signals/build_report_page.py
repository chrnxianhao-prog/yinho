"""把 results/ 里的分析结果生成一页带图表的 HTML 报告（results/report_page.html）。

每次重跑 analyze.py 之后运行一次即可更新。页面是自包含的：数据内嵌，图表用原生 JS 画 SVG，
只从 Google Fonts 加载数字字体（加载不到时回退系统等宽字体）。
可选：results/findings.yaml 里写 `findings: [..]`，会作为“主要发现”显示在页首。

用法：..\\..\\.venv\\Scripts\\python.exe build_report_page.py
"""
from __future__ import annotations

import json
import math

import pandas as pd
import yaml

from common import RESULT_DIR, load_config
from indicators import EVENT_BY_NAME, FACTORS, SHORT_LABELS

FACTOR_SHORT = {
    "ret5": "5 日涨跌幅", "ret20": "20 日涨跌幅", "ret60": "60 日涨跌幅", "bias20": "偏离 MA20",
    "rsi14": "RSI14", "kdj_j": "KDJ J 值", "boll_pctb": "布林 %b", "macd_norm": "MACD 柱/股价",
    "dist_hh60": "距 60 日高点", "vol_ratio": "量比", "turnover_rel": "相对换手率", "turnover_rate": "换手率",
    "vol20": "20 日波动率", "atr_pct": "ATR/股价", "log_amount": "成交额（规模）", "upper_shadow": "上影线占比",
    "lower_shadow": "下影线占比", "gap": "开盘跳空",
}


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def records(frame: pd.DataFrame, columns: list[str]) -> list[dict]:
    return [{column: clean(row[column]) for column in columns} for _, row in frame[columns].iterrows()]


def build_data() -> dict:
    config = load_config()
    stats = pd.read_csv(RESULT_DIR / "event_stats.csv")
    ic = pd.read_csv(RESULT_DIR / "factor_ic.csv")
    yearly = pd.read_csv(RESULT_DIR / "event_yearly.csv")
    strong = yaml.safe_load((RESULT_DIR / "strong_signals.yaml").read_text(encoding="utf-8"))
    meta = yaml.safe_load((RESULT_DIR / "meta.yaml").read_text(encoding="utf-8"))
    findings_path = RESULT_DIR / "findings.yaml"
    findings = []
    if findings_path.is_file():
        findings = (yaml.safe_load(findings_path.read_text(encoding="utf-8")) or {}).get("findings", [])

    stats["short"] = stats["event"].map(SHORT_LABELS)
    ic["short"] = ic["factor"].map(FACTOR_SHORT)
    ic["label"] = ic["factor"].map(FACTORS)
    horizon = int(config["selection"]["horizon"])
    main = stats[(stats["horizon"] == horizon) & (stats["regime"] == "any")]
    focus = main.reindex(main["t_full"].abs().sort_values(ascending=False).index)["event"].head(6).tolist()
    yearly_groups = []
    for name in focus:
        row = main[main["event"] == name].iloc[0]
        series = yearly[yearly["event"] == name].sort_values("year")
        yearly_groups.append({
            "event": name, "short": SHORT_LABELS[name], "label": EVENT_BY_NAME[name].label,
            "t_full": clean(float(row["t_full"])), "mean_excess": clean(float(row["mean_excess"])),
            "year_consistency": clean(float(row["year_consistency"])),
            "series": [{"year": int(r["year"]), "mean": clean(float(r["mean"])), "count": int(r["count"])} for _, r in series.iterrows()],
        })
    costs = stats[stats["regime"] == "any"].groupby("horizon")["mean_cost"].median()
    strong_keys = {(item["event"], item["regime"]) for side in ("buy", "sell") for item in strong.get(side, [])}
    return {
        "meta": {
            **meta["panel"],
            "lookahead": meta["lookahead"],
            "horizon": horizon,
            "horizons": [int(h) for h in config["research"]["horizons"]],
            "in_sample_end": config["research"]["in_sample_end"],
            "oos_start_year": int(config["research"]["in_sample_end"][:4]) + 1,
            "cost": {str(int(h)): float(c) for h, c in costs.items()},
            "min_amount_wan": config["research"]["min_amount_20d"] / 1e4,
            "commission_bp": config["costs"]["commission_rate"] * 1e4,
            "slippage_bp": config["costs"]["slippage_per_side"] * 1e4,
            "selection": config["selection"],
            "n_events_defined": int(stats["event"].nunique()),
            "n_tests": int(len(stats)),
        },
        "events": records(stats, ["event", "short", "label", "hint", "regime", "horizon", "n_events", "n_dates", "mean_excess",
                                  "net_excess", "win_excess", "win_net", "mean_cost", "t_full", "is_mean", "oos_mean", "t_oos",
                                  "year_consistency"]),
        "ic": records(ic, ["factor", "short", "label", "horizon", "mean_ic", "icir", "t_nw", "pct_positive", "is_ic", "oos_ic",
                           "q1", "q5", "q5_minus_q1"]),
        "yearly": yearly_groups,
        "strong": {"buy": strong.get("buy", []), "sell": strong.get("sell", [])},
        "strong_keys": [list(key) for key in strong_keys],
        "findings": findings,
    }


TEMPLATE = r"""<title>A股指标信号实测</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&display=swap">
<style>
:root {
  color-scheme: light;
  --page: #f4f4f0;
  --surface: #fcfcfb;
  --ink: #15161a;
  --ink-2: #4c5058;
  --muted: #7f8187;
  --hair: #e3e3dc;
  --axis: #c3c2b9;
  --up: #d23f3f;
  --down: #138a6e;
  --neutral: #bdbcb3;
  --band: rgba(21, 22, 26, 0.04);
  --chip: #e9e9e3;
  --chip-on: #15161a;
  --chip-on-ink: #fcfcfb;
  --focus: #2a5bd7;
  --sans: -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", Consolas, "Microsoft YaHei", monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #111112; --surface: #1a1a19; --ink: #f1f1ed; --ink-2: #c3c2b7; --muted: #8e8d86;
    --hair: #2c2c2a; --axis: #45453f; --up: #e8615d; --down: #18a080; --neutral: #55554f;
    --band: rgba(255, 255, 255, 0.05); --chip: #262624; --chip-on: #f1f1ed; --chip-on-ink: #151514; --focus: #86a8ff;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #111112; --surface: #1a1a19; --ink: #f1f1ed; --ink-2: #c3c2b7; --muted: #8e8d86;
  --hair: #2c2c2a; --axis: #45453f; --up: #e8615d; --down: #18a080; --neutral: #55554f;
  --band: rgba(255, 255, 255, 0.05); --chip: #262624; --chip-on: #f1f1ed; --chip-on-ink: #151514; --focus: #86a8ff;
}
body { background: var(--page); color: var(--ink); font-family: var(--sans); font-size: 15px; line-height: 1.7; }
.page { max-width: 1000px; margin: 0 auto; padding-inline: 20px; padding-block: 40px 72px; display: grid; gap: 56px; }
header { display: grid; gap: 12px; }
.eyebrow { font-size: 12px; letter-spacing: 0.08em; color: var(--muted); }
h1 { font-size: clamp(30px, 5vw, 44px); line-height: 1.15; margin: 0; letter-spacing: -0.01em; text-wrap: balance; }
h2 { font-size: 22px; line-height: 1.3; margin: 0; text-wrap: balance; }
h3 { font-size: 15px; margin: 0; }
p { margin: 0; max-width: 68ch; }
.lede { font-size: 17px; color: var(--ink-2); }
section { display: grid; gap: 18px; }
.section-head { display: grid; gap: 6px; }
.note { color: var(--ink-2); font-size: 14px; }
.mono, .num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.tile { background: var(--surface); border: 1px solid var(--hair); border-radius: 10px; padding: 16px 18px; display: grid; gap: 4px; }
.tile .label { font-size: 13px; color: var(--ink-2); }
.tile .value { font-size: 26px; font-weight: 650; line-height: 1.2; }
.tile .sub { font-size: 12px; color: var(--muted); }
.verdict { background: var(--surface); border: 1px solid var(--hair); border-radius: 12px; padding: 22px 24px; display: grid; gap: 12px; }
.verdict ul { margin: 0; padding-left: 1.2em; display: grid; gap: 8px; max-width: 72ch; }
.verdict li::marker { color: var(--muted); }
.pill { display: inline-block; font-size: 12px; line-height: 1; padding: 5px 8px; border-radius: 999px; background: var(--chip); color: var(--ink-2); vertical-align: 2px; }
.card { background: var(--surface); border: 1px solid var(--hair); border-radius: 12px; padding: 18px 18px 12px; }
.filters { display: flex; flex-wrap: wrap; gap: 10px 22px; align-items: center; }
.seg { display: inline-flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.seg .seg-label { font-size: 13px; color: var(--ink-2); margin-right: 6px; }
.seg button { font: inherit; font-size: 13px; border: 0; border-radius: 999px; padding: 5px 12px; background: var(--chip); color: var(--ink); cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--chip-on); color: var(--chip-on-ink); }
.seg button:focus-visible, [tabindex]:focus-visible, th button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; color: var(--ink-2); }
.legend i { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
.legend i.line { width: 14px; height: 2px; border-radius: 1px; vertical-align: 4px; }
.legend i.dot { width: 9px; height: 9px; border-radius: 50%; box-shadow: 0 0 0 2px var(--surface); }
.chart { width: 100%; }
.chart svg { display: block; overflow: visible; }
.multiples { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 12px; }
.mini { background: var(--surface); border: 1px solid var(--hair); border-radius: 10px; padding: 14px 14px 8px; display: grid; gap: 2px; }
.mini .meta { font-size: 12px; color: var(--muted); }
.table-wrap { overflow-x: auto; background: var(--surface); border: 1px solid var(--hair); border-radius: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; min-width: 860px; }
th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--hair); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; position: sticky; left: 0; background: var(--surface); }
th { font-weight: 600; color: var(--ink-2); font-size: 12px; }
th button { font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; }
th button[aria-sort="descending"]::after { content: " ↓"; }
th button[aria-sort="ascending"]::after { content: " ↑"; }
td.num { font-size: 12.5px; }
tbody tr:last-child td { border-bottom: 0; }
.tag { font-size: 11px; padding: 2px 6px; border-radius: 4px; background: var(--chip); color: var(--ink); margin-left: 6px; }
.sig-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 7px; vertical-align: 1px; background: var(--neutral); }
.method { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
.method div { display: grid; gap: 8px; align-content: start; }
.method ul { margin: 0; padding-left: 1.1em; display: grid; gap: 6px; color: var(--ink-2); font-size: 14px; }
code { font-family: var(--mono); font-size: 0.92em; background: var(--chip); padding: 1px 5px; border-radius: 4px; }
pre { font-family: var(--mono); font-size: 13px; background: var(--surface); border: 1px solid var(--hair); border-radius: 10px; padding: 14px 16px; overflow-x: auto; margin: 0; line-height: 1.6; }
#tip { position: fixed; z-index: 10; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--hair); border-radius: 8px; padding: 8px 10px; font-size: 12.5px; box-shadow: 0 6px 24px rgba(0, 0, 0, 0.14); display: grid; gap: 2px; max-width: 280px; }
#tip b { font-family: var(--mono); font-weight: 600; margin-right: 6px; }
#tip span { color: var(--ink-2); }
#tip .tip-title { font-weight: 600; margin-bottom: 2px; }
footer { color: var(--muted); font-size: 12px; }
@media (max-width: 560px) {
  .page { padding-inline: 16px; gap: 44px; }
  .verdict { padding: 18px; }
}
</style>

<main class="page">
  <header>
    <div class="eyebrow" id="eyebrow"></div>
    <h1>A股指标信号实测</h1>
    <p class="lede" id="lede"></p>
  </header>

  <section aria-labelledby="h-verdict">
    <div class="tiles" id="tiles"></div>
    <div class="verdict">
      <h2 id="h-verdict">结论</h2>
      <ul id="findings"></ul>
    </div>
  </section>

  <section aria-labelledby="h-rank">
    <div class="section-head">
      <h2 id="h-rank">每个信号出现后，平均跑赢市场多少</h2>
      <p class="note">横条是信号出现后、按计划持有期间相对股票池平均收益的超额（未扣成本）。只有 |t| ≥ 3 的信号上色：红色为跑赢，绿色为跑输；灰色表示统计上和运气分不开。竖线是买卖一个来回的平均成本：买入信号要在竖线右边才可能赚到钱。</p>
    </div>
    <div class="filters" role="group" aria-label="筛选">
      <div class="seg" id="seg-h"><span class="seg-label">持有期</span></div>
      <div class="seg" id="seg-regime"><span class="seg-label">市场环境</span></div>
    </div>
    <div class="card">
      <div class="legend" id="rank-legend"></div>
      <div class="chart" id="rank-chart"></div>
    </div>
    <div class="table-wrap">
      <table id="rank-table" aria-label="全部信号统计表"><thead></thead><tbody></tbody></table>
    </div>
  </section>

  <section aria-labelledby="h-years">
    <div class="section-head">
      <h2 id="h-years">逐年看：是稳定的规律，还是几年运气</h2>
      <p class="note" id="years-note"></p>
    </div>
    <div class="multiples" id="multiples"></div>
  </section>

  <section aria-labelledby="h-ic">
    <div class="section-head">
      <h2 id="h-ic">连续指标：数值高低能不能排序未来收益</h2>
      <p class="note">每天把股票池按指标值排序，计算它与未来超额收益排序的相关系数（Rank IC），再对所有交易日取平均。正值表示指标越高、之后表现越好；负值相反。横条为全样本平均，圆点为样本外平均；只有 |t| ≥ 3 的上色。</p>
    </div>
    <div class="filters"><div class="seg" id="seg-ic"><span class="seg-label">预测期</span></div></div>
    <div class="card">
      <div class="legend" id="ic-legend"></div>
      <div class="chart" id="ic-chart"></div>
    </div>
  </section>

  <section aria-labelledby="h-method">
    <div class="section-head"><h2 id="h-method">口径和局限</h2></div>
    <div class="method" id="method"></div>
  </section>

  <section aria-labelledby="h-next">
    <div class="section-head">
      <h2 id="h-next">每天怎么收到信号</h2>
      <p class="note">研究和每日扫描共用同一份指标代码。只有达到强信号门槛的规则才会进入扫描；北京时间 15:05 之前、或数据没更新到最新时，扫描会拒绝出信号。</p>
    </div>
    <pre>cd research\ashare_signals
..\..\.venv\Scripts\python.exe fetch_prices.py --current-only --refresh --end 今天日期
..\..\.venv\Scripts\python.exe scan.py</pre>
  </section>

  <footer id="footer"></footer>
</main>
<div id="tip" hidden></div>

<script>
(() => {
const D = __DATA__;
const SVGNS = "http://www.w3.org/2000/svg";
const $ = (s) => document.querySelector(s);
const state = { h: D.meta.horizon, regime: "any", icH: 5, sortKey: "mean_excess", sortDir: -1 };
const REGIMES = [["any", "不限"], ["bull", "沪深300 在 60 日线上"], ["bear", "在 60 日线下"]];
const strongKeys = new Set(D.strong_keys.map(([e, r]) => e + "|" + r));

function svg(tag, attrs, parent) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}
function text(parent, x, y, value, opts = {}) {
  const t = svg("text", { x, y, "text-anchor": opts.anchor || "start", "font-size": opts.size || 12 }, parent);
  t.style.fill = opts.color || "var(--ink-2)";
  if (opts.mono) t.style.fontFamily = "var(--mono)";
  if (opts.weight) t.style.fontWeight = opts.weight;
  t.textContent = value;
  return t;
}
const finite = (v) => v !== null && v !== undefined && Number.isFinite(v);
function pct(v, d = 2) { if (!finite(v)) return "—"; const s = (Math.abs(v) * 100).toFixed(d); return (v > 0 ? "+" : v < 0 ? "−" : "") + s + "%"; }
function signed(v, d = 3) { if (!finite(v)) return "—"; return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d); }
function num(v, d = 1) { if (!finite(v)) return "—"; return (v < 0 ? "−" : "") + Math.abs(v).toFixed(d); }
function share(v) { return finite(v) ? Math.round(v * 100) + "%" : "—"; }
function thousands(v) { return Number(v).toLocaleString("en-US"); }
function colorFor(value, t) { if (!finite(t) || Math.abs(t) < 3) return "var(--neutral)"; return value > 0 ? "var(--up)" : "var(--down)"; }

function barPath(x0, x1, y, h, r) {
  const w = Math.abs(x1 - x0); if (w < 0.5) return "";
  r = Math.min(r, w, h / 2);
  return x1 >= x0
    ? `M${x0},${y}H${x1 - r}Q${x1},${y} ${x1},${y + r}V${y + h - r}Q${x1},${y + h} ${x1 - r},${y + h}H${x0}Z`
    : `M${x0},${y}H${x1 + r}Q${x1},${y} ${x1},${y + r}V${y + h - r}Q${x1},${y + h} ${x1 + r},${y + h}H${x0}Z`;
}
function colPath(x, w, y0, y1, r) {
  const h = Math.abs(y1 - y0); if (h < 0.5) return "";
  r = Math.min(r, h, w / 2);
  return y1 <= y0
    ? `M${x},${y0}V${y1 + r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 + r}V${y0}Z`
    : `M${x},${y0}V${y1 - r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 - r}V${y0}Z`;
}
function niceTicks(lo, hi, target = 5) {
  const span = hi - lo, raw = span / target, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= target + 1);
  const ticks = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-12; v += step) ticks.push(Math.abs(v) < 1e-12 ? 0 : v);
  return { ticks, step };
}

const tip = $("#tip");
function showTip(evt, title, rows) {
  tip.replaceChildren();
  const head = document.createElement("div"); head.className = "tip-title"; head.textContent = title; tip.appendChild(head);
  for (const [value, label] of rows) {
    const line = document.createElement("div");
    const b = document.createElement("b"); b.textContent = value; line.appendChild(b);
    const s = document.createElement("span"); s.textContent = label; line.appendChild(s);
    tip.appendChild(line);
  }
  tip.hidden = false;
  let x, y;
  if (evt.type === "focus") { const r = evt.target.getBoundingClientRect(); x = r.left + r.width * 0.6; y = r.top; }
  else { x = evt.clientX; y = evt.clientY; }
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, x + 14)) + "px";
  tip.style.top = Math.max(8, Math.min(window.innerHeight - h - 8, y - h - 12 < 8 ? y + 18 : y - h - 12)) + "px";
}
function hideTip() { tip.hidden = true; }
function bindTip(node, title, rows, onHover) {
  node.setAttribute("tabindex", "0");
  node.setAttribute("aria-label", title + "：" + rows.map((r) => r[1] + " " + r[0]).join("，"));
  const show = (e) => { showTip(e, title, rows); if (onHover) onHover(true); };
  const hide = () => { hideTip(); if (onHover) onHover(false); };
  node.addEventListener("pointermove", show); node.addEventListener("focus", show);
  node.addEventListener("pointerleave", hide); node.addEventListener("blur", hide);
}

/* ---------- header, tiles, findings ---------- */
function renderHeader() {
  const m = D.meta;
  $("#eyebrow").textContent = `研究报告 · 沪深300 + 中证500 历史成分股 · 日线 · 数据截至 ${m.last_date}`;
  $("#lede").textContent = `${m.n_events_defined} 个常见技术信号，在 ${thousands(m.stocks)} 只股票、${m.first_date.slice(0, 4)}–${m.last_date.slice(0, 4)} 年的历史上逐一检验。前提是次日开盘才能买、成本全部扣除、并且和同一天的全市场平均收益比较：哪些信号还站得住？`;
  const nb = D.strong.buy.length, ns = D.strong.sell.length;
  const tiles = [
    ["股票池", `${thousands(m.stocks)} 只`, "当时在沪深300/中证500 的股票"],
    ["样本", `${(m.rows / 1e6).toFixed(2)}M`, `股票·交易日，共 ${thousands(m.dates)} 个交易日`],
    ["检验组合", thousands(m.n_tests), `${m.n_events_defined} 个信号 × 3 种市场环境 × ${m.horizons.length} 个持有期`],
    ["达标强信号", `${nb} 买 · ${ns} 卖`, `持有 ${m.horizon} 日、t≥${m.selection.min_t_full}、样本外 t≥${m.selection.min_t_oos}`],
  ];
  const box = $("#tiles");
  for (const [label, value, sub] of tiles) {
    const tile = document.createElement("div"); tile.className = "tile";
    for (const [cls, content] of [["label", label], ["value", value], ["sub", sub]]) {
      const d = document.createElement("div"); d.className = cls; d.textContent = content; tile.appendChild(d);
    }
    box.appendChild(tile);
  }
  const list = $("#findings");
  const items = [...D.findings];
  const describe = (item, side) => {
    const s = item.stats;
    return `${side}：${EVENT_LABEL[item.event] || item.label}（${item.regime_label}）— 持有 ${item.horizon_days} 日平均超额 ${pct(s.mean_excess)}，` +
      (side === "强买入" ? `扣成本后 ${pct(s.net_excess)}，` : "") + `t=${num(s.t_full)}，样本外 t=${num(s.t_oos)}，事件 ${thousands(s.n_events)} 次`;
  };
  for (const item of D.strong.buy) items.push(describe(item, "强买入"));
  for (const item of D.strong.sell) items.push(describe(item, "强卖出/回避"));
  if (!D.strong.buy.length) items.push(`没有任何买入信号达到强信号门槛（持有 ${m.horizon} 日）。`);
  if (!D.strong.sell.length) items.push("没有任何卖出信号达到强信号门槛。");
  for (const line of items) { const li = document.createElement("li"); li.textContent = line; list.appendChild(li); }
}
const EVENT_LABEL = Object.fromEntries(D.events.map((e) => [e.event, e.label]));

/* ---------- filters ---------- */
function segmented(container, options, current, onChange) {
  const box = $(container);
  for (const [value, label] of options) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    b.setAttribute("aria-pressed", String(value === current));
    b.addEventListener("click", () => {
      box.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false"));
      b.setAttribute("aria-pressed", "true"); onChange(value);
    });
    box.appendChild(b);
  }
}

/* ---------- ranking chart ---------- */
function currentRows() {
  return D.events.filter((r) => r.horizon === state.h && r.regime === state.regime);
}
function renderLegend(target, items) {
  const box = $(target); box.replaceChildren();
  for (const [kind, color, label] of items) {
    const span = document.createElement("span");
    const i = document.createElement("i"); if (kind !== "box") i.className = kind; i.style.background = color;
    span.appendChild(i); span.appendChild(document.createTextNode(label)); box.appendChild(span);
  }
}
function renderRanking() {
  const rows = currentRows().filter((r) => finite(r.mean_excess)).sort((a, b) => b.mean_excess - a.mean_excess);
  const cost = D.meta.cost[String(state.h)];
  renderLegend("#rank-legend", [
    ["box", "var(--up)", "显著跑赢（t ≥ 3）"], ["box", "var(--down)", "显著跑输（t ≤ −3）"],
    ["box", "var(--neutral)", "不显著"], ["line", "var(--ink-2)", `买卖一次的平均成本 ${pct(cost)}`],
  ]);
  const box = $("#rank-chart"); box.replaceChildren();
  if (!rows.length) { box.textContent = "这个组合下样本太少。"; return; }
  const W = box.clientWidth, narrow = W < 560;
  const labelW = narrow ? 112 : 170, valueW = narrow ? 58 : 118, rowH = 26, top = 14, axisH = 28, barH = 12;
  const plotL = labelW, plotR = W - valueW - 6;
  let lo = Math.min(0, ...rows.map((r) => r.mean_excess)), hi = Math.max(0, cost, ...rows.map((r) => r.mean_excess));
  const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
  const x = (v) => plotL + ((v - lo) / (hi - lo)) * (plotR - plotL);
  const H = top + rows.length * rowH + axisH;
  const root = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "各信号平均超额收益横条图" }, box);
  const { ticks } = niceTicks(lo, hi, narrow ? 3 : 6);
  for (const t of ticks) {
    const gx = x(t);
    const line = svg("line", { x1: gx, x2: gx, y1: top - 4, y2: top + rows.length * rowH }, root);
    line.style.stroke = t === 0 ? "var(--axis)" : "var(--hair)"; line.style.strokeWidth = 1;
    text(root, gx, H - 8, pct(t, Math.abs(t) < 0.01 && t !== 0 ? 2 : 1), { anchor: "middle", size: 11, color: "var(--muted)", mono: true });
  }
  const cx = x(cost);
  const costLine = svg("line", { x1: cx, x2: cx, y1: top - 8, y2: top + rows.length * rowH }, root);
  costLine.style.stroke = "var(--ink-2)"; costLine.style.strokeWidth = 1.5;
  rows.forEach((r, i) => {
    const y = top + i * rowH, mid = y + rowH / 2;
    const significant = finite(r.t_full) && Math.abs(r.t_full) >= 3;
    const g = svg("g", {}, root);
    const hit = svg("rect", { x: 0, y, width: W, height: rowH, rx: 4 }, g);
    hit.style.fill = "transparent";
    text(g, labelW - 10, mid + 4, r.short, { anchor: "end", size: narrow ? 11.5 : 12.5, color: significant ? "var(--ink)" : "var(--ink-2)", weight: significant ? 600 : 400 });
    const bar = svg("path", { d: barPath(x(0), x(r.mean_excess), y + (rowH - barH) / 2, barH, 4) }, g);
    bar.style.fill = colorFor(r.mean_excess, r.t_full);
    const value = narrow ? pct(r.mean_excess) : `${pct(r.mean_excess)}  t ${num(r.t_full)}`;
    text(g, W - 2, mid + 4, value, { anchor: "end", size: 11.5, color: significant ? "var(--ink)" : "var(--muted)", mono: true });
    bindTip(hit, r.label, [
      [pct(r.mean_excess), `平均超额（持有 ${r.horizon} 日）`], [pct(r.net_excess), "买入扣成本后"],
      [num(r.t_full), "t 值"], [pct(r.oos_mean), `样本外（${D.meta.oos_start_year} 年起）`],
      [share(r.win_excess), "跑赢市场的比例"], [thousands(r.n_events), "事件数"],
    ], (on) => { hit.style.fill = on ? "var(--band)" : "transparent"; });
  });
}

/* ---------- table ---------- */
const COLUMNS = [
  ["short", "信号", (r) => r.short, "text"],
  ["hint", "传统解读", (r) => (r.hint === "buy" ? "买" : "卖"), "text"],
  ["n_events", "事件数", (r) => thousands(r.n_events)],
  ["mean_excess", "平均超额", (r) => pct(r.mean_excess)],
  ["net_excess", "买入扣费后", (r) => pct(r.net_excess)],
  ["win_excess", "跑赢占比", (r) => share(r.win_excess)],
  ["t_full", "t 值", (r) => num(r.t_full)],
  ["is_mean", "样本内", (r) => pct(r.is_mean)],
  ["oos_mean", "样本外", (r) => pct(r.oos_mean)],
  ["t_oos", "样本外 t", (r) => num(r.t_oos)],
  ["year_consistency", "年份一致", (r) => share(r.year_consistency)],
];
function renderTable() {
  const rows = currentRows().slice();
  const key = state.sortKey, dir = state.sortDir;
  rows.sort((a, b) => {
    const va = a[key], vb = b[key];
    if (typeof va === "string") return dir * va.localeCompare(vb, "zh");
    return dir * ((finite(va) ? va : -Infinity) - (finite(vb) ? vb : -Infinity));
  });
  const thead = $("#rank-table thead"), tbody = $("#rank-table tbody");
  thead.replaceChildren(); tbody.replaceChildren();
  const tr = document.createElement("tr");
  for (const [k, label] of COLUMNS) {
    const th = document.createElement("th"); th.scope = "col";
    const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    if (k === key) b.setAttribute("aria-sort", dir < 0 ? "descending" : "ascending");
    b.addEventListener("click", () => { state.sortDir = state.sortKey === k ? -state.sortDir : -1; state.sortKey = k; renderTable(); });
    th.appendChild(b); tr.appendChild(th);
  }
  thead.appendChild(tr);
  for (const r of rows) {
    const row = document.createElement("tr");
    COLUMNS.forEach(([k, , fmt, type], idx) => {
      const td = document.createElement("td");
      if (idx === 0) {
        const dot = document.createElement("span"); dot.className = "sig-dot"; dot.style.background = colorFor(r.mean_excess, r.t_full);
        td.appendChild(dot); td.appendChild(document.createTextNode(fmt(r)));
        if (strongKeys.has(r.event + "|" + r.regime) && r.horizon === D.meta.horizon) {
          const tag = document.createElement("span"); tag.className = "tag"; tag.textContent = "达标"; td.appendChild(tag);
        }
        td.title = r.label;
      } else {
        td.textContent = fmt(r); if (type !== "text") td.className = "num";
      }
      row.appendChild(td);
    });
    tbody.appendChild(row);
  }
}

/* ---------- yearly small multiples ---------- */
function renderMultiples() {
  const box = $("#multiples"); box.replaceChildren();
  const groups = D.yearly;
  const years = [...new Set(groups.flatMap((g) => g.series.map((s) => s.year)))].sort();
  $("#years-note").textContent = `t 值绝对值最大的 ${groups.length} 个信号（持有 ${D.meta.horizon} 日），每年的平均超额收益。所有小图共用同一纵轴；浅色底为样本外年份（${D.meta.oos_start_year} 年起）。方向年年一致的信号才可能是规律。`;
  const maxAbs = Math.max(0.002, ...groups.flatMap((g) => g.series.map((s) => Math.abs(s.mean || 0))));
  const W = 300, H = 128, left = 34, right = 6, top = 10, bottom = 20;
  const band = (W - left - right) / years.length, colW = Math.min(14, band - 4);
  const y = (v) => top + ((maxAbs - v) / (2 * maxAbs)) * (H - top - bottom);
  for (const g of groups) {
    const card = document.createElement("div"); card.className = "mini";
    const h3 = document.createElement("h3"); h3.textContent = g.short; card.appendChild(h3);
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = `全样本 ${pct(g.mean_excess)} · t ${num(g.t_full)} · 年份一致 ${share(g.year_consistency)}`;
    card.appendChild(meta);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": g.label + " 逐年平均超额" }, card);
    const oosIndex = years.findIndex((yr) => yr >= D.meta.oos_start_year);
    if (oosIndex >= 0) {
      const rect = svg("rect", { x: left + oosIndex * band, y: top - 4, width: W - right - (left + oosIndex * band), height: H - top - bottom + 8, rx: 4 }, root);
      rect.style.fill = "var(--band)";
    }
    for (const t of [maxAbs, 0, -maxAbs]) {
      const line = svg("line", { x1: left, x2: W - right, y1: y(t), y2: y(t) }, root);
      line.style.stroke = t === 0 ? "var(--axis)" : "var(--hair)"; line.style.strokeWidth = 1;
      text(root, left - 4, y(t) + 3.5, t === 0 ? "0" : pct(t, 1), { anchor: "end", size: 9.5, color: "var(--muted)", mono: true });
    }
    const byYear = Object.fromEntries(g.series.map((s) => [s.year, s]));
    years.forEach((yr, i) => {
      const s = byYear[yr], x0 = left + i * band;
      if (s && finite(s.mean)) {
        const path = svg("path", { d: colPath(x0 + (band - colW) / 2, colW, y(0), y(s.mean), 3) }, root);
        path.style.fill = s.mean >= 0 ? "var(--up)" : "var(--down)";
        const hit = svg("rect", { x: x0, y: top - 4, width: band, height: H - top - bottom + 8 }, root);
        hit.style.fill = "transparent";
        bindTip(hit, `${g.short} · ${yr} 年`, [[pct(s.mean), "平均超额"], [thousands(s.count), "事件数"]],
          (on) => { hit.style.fill = on ? "var(--band)" : "transparent"; });
      }
      if (i === 0 || i === years.length - 1 || yr === D.meta.oos_start_year) {
        text(root, x0 + band / 2, H - 5, String(yr), { anchor: "middle", size: 9.5, color: "var(--muted)", mono: true });
      }
    });
    box.appendChild(card);
  }
}

/* ---------- factor IC ---------- */
function renderIC() {
  const rows = D.ic.filter((r) => r.horizon === state.icH && finite(r.mean_ic)).sort((a, b) => b.mean_ic - a.mean_ic);
  renderLegend("#ic-legend", [
    ["box", "var(--up)", "显著为正"], ["box", "var(--down)", "显著为负"], ["box", "var(--neutral)", "不显著"],
    ["dot", "var(--ink)", "样本外平均 IC"],
  ]);
  const box = $("#ic-chart"); box.replaceChildren();
  const W = box.clientWidth, narrow = W < 560;
  const labelW = narrow ? 104 : 150, valueW = narrow ? 58 : 110, rowH = 26, top = 10, axisH = 28, barH = 12;
  const plotL = labelW, plotR = W - valueW - 6;
  const values = rows.flatMap((r) => [r.mean_ic, r.oos_ic].filter(finite));
  let lo = Math.min(0, ...values), hi = Math.max(0, ...values); const pad = (hi - lo) * 0.08 || 0.01; lo -= pad; hi += pad;
  const x = (v) => plotL + ((v - lo) / (hi - lo)) * (plotR - plotL);
  const H = top + rows.length * rowH + axisH;
  const root = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "因子 Rank IC 横条图" }, box);
  const { ticks } = niceTicks(lo, hi, narrow ? 3 : 6);
  for (const t of ticks) {
    const gx = x(t);
    const line = svg("line", { x1: gx, x2: gx, y1: top - 4, y2: top + rows.length * rowH }, root);
    line.style.stroke = t === 0 ? "var(--axis)" : "var(--hair)"; line.style.strokeWidth = 1;
    text(root, gx, H - 8, signed(t, 2), { anchor: "middle", size: 11, color: "var(--muted)", mono: true });
  }
  rows.forEach((r, i) => {
    const y = top + i * rowH, mid = y + rowH / 2;
    const significant = finite(r.t_nw) && Math.abs(r.t_nw) >= 3;
    const g = svg("g", {}, root);
    const hit = svg("rect", { x: 0, y, width: W, height: rowH, rx: 4 }, g); hit.style.fill = "transparent";
    text(g, labelW - 10, mid + 4, r.short, { anchor: "end", size: narrow ? 11.5 : 12.5, color: significant ? "var(--ink)" : "var(--ink-2)", weight: significant ? 600 : 400 });
    const bar = svg("path", { d: barPath(x(0), x(r.mean_ic), y + (rowH - barH) / 2, barH, 4) }, g);
    bar.style.fill = colorFor(r.mean_ic, r.t_nw);
    if (finite(r.oos_ic)) {
      const dot = svg("circle", { cx: x(r.oos_ic), cy: mid, r: 4.5 }, g);
      dot.style.fill = "var(--ink)"; dot.style.stroke = "var(--surface)"; dot.style.strokeWidth = 2;
    }
    text(g, W - 2, mid + 4, narrow ? signed(r.mean_ic) : `${signed(r.mean_ic)}  t ${num(r.t_nw)}`, { anchor: "end", size: 11.5, color: significant ? "var(--ink)" : "var(--muted)", mono: true });
    bindTip(hit, r.label, [
      [signed(r.mean_ic), "全样本平均 IC"], [signed(r.oos_ic), "样本外平均 IC"], [num(r.t_nw), "t 值"],
      [share(r.pct_positive), "IC 为正的天数占比"], [pct(r.q1), "指标最低 20% 的平均超额"], [pct(r.q5), "指标最高 20% 的平均超额"],
    ], (on) => { hit.style.fill = on ? "var(--band)" : "transparent"; });
  });
}

/* ---------- method ---------- */
function renderMethod() {
  const m = D.meta, s = m.selection;
  const blocks = [
    ["执行口径", [
      "信号日收盘后出信号，次日开盘买入，持有 N 个交易日后开盘卖出。",
      "次日停牌、或开盘即涨停/跌停（买不进、卖不出）的事件不计入。",
      `只统计当时在沪深300/中证500 里的股票（半年一次成分股快照），且 20 日平均成交额 ≥ ${thousands(m.min_amount_wan)} 万元。`,
    ]],
    ["成本和收益", [
      `佣金 ${num(m.commission_bp, 1)}‱、滑点 ${num(m.slippage_bp, 0)}bp，买卖各收一次。`,
      "印花税按卖出日期：2023-08-28 前 0.1%，之后 0.05%。过户费：2022-04-29 前 0.002%，之后 0.001%。",
      "超额收益 = 个股收益 − 同一天股票池等权平均收益，用来剔除大盘涨跌。",
    ]],
    ["怎么算“强信号”", [
      `持有 ${m.horizon} 日；事件 ≥ ${s.min_events} 次，分布在 ≥ ${s.min_dates} 个交易日。`,
      `全样本 t ≥ ${s.min_t_full}，样本外 t ≥ ${s.min_t_oos}，样本内外方向一致；t 值按入场日聚类后做 Newey-West 调整。`,
      `买入信号扣成本后超额 ≥ ${pct(s.min_net_excess, 1)}；至少 ${share(s.min_positive_year_ratio)} 的年份方向一致。`,
    ]],
    ["局限", [
      `防未来函数：随机截断数据重算 ${thousands(m.lookahead.checked)} 个指标和信号值，不一致 ${m.lookahead.mismatched} 个。`,
      "成分股快照半年一次；新浪和腾讯都取不到数据的退市股缺失，仍有少量幸存者偏差。",
      "按开盘价成交是理想情况：集合竞价的实际成交价和挂单方式有关，滑点要用真实成交校准。",
      `一共检验了 ${thousands(m.n_tests)} 个组合，所以门槛定得高，用来降低“碰巧显著”的概率。`,
    ]],
  ];
  const box = $("#method");
  for (const [title, lines] of blocks) {
    const div = document.createElement("div");
    const h3 = document.createElement("h3"); h3.textContent = title; div.appendChild(h3);
    const ul = document.createElement("ul");
    for (const line of lines) { const li = document.createElement("li"); li.textContent = line; ul.appendChild(li); }
    div.appendChild(ul); box.appendChild(div);
  }
  $("#footer").textContent = `由 research/ashare_signals/build_report_page.py 生成 · 数据 ${m.first_date} 至 ${m.last_date} · 研究信号不构成投资建议`;
}

renderHeader();
segmented("#seg-h", D.meta.horizons.map((h) => [h, `${h} 日`]), state.h, (v) => { state.h = v; renderRanking(); renderTable(); });
segmented("#seg-regime", REGIMES, state.regime, (v) => { state.regime = v; renderRanking(); renderTable(); });
segmented("#seg-ic", [[5, "5 日"], [20, "20 日"]], state.icH, (v) => { state.icH = v; renderIC(); });
renderRanking(); renderTable(); renderMultiples(); renderIC(); renderMethod();
let resizeTimer = null, lastWidth = window.innerWidth;
window.addEventListener("resize", () => {
  if (window.innerWidth === lastWidth) return; lastWidth = window.innerWidth;
  clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { renderRanking(); renderIC(); }, 150);
});
})();
</script>
"""


def main() -> None:
    data = build_data()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    output = RESULT_DIR / "report_page.html"
    output.write_text(TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
    print(f"已生成 {output}（{output.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
