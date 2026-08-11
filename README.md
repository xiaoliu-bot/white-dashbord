# 智瞰 · 实时财经看板 — 技术文档

> 版本：v2（「板块讨论度」模块已移除）｜更新：2026-08-11
> 仓库：https://github.com/xiaoliu-bot/white-dashbord
> 线上地址：https://xiaoliu-bot.github.io/white-dashbord/

---

## 一、项目概述

**智瞰** 是一个面向 A 股 / 美股 / 港股投资者的**实时财经监控看板**：纯前端单页（深色金融终端风格），数据由 GitHub Actions 每 5 分钟抓取并写入 `api/data.json`，页面自动读取、无需后端服务器。

### 核心模块

| 模块 | 内容 | 主要数据源 |
|:---|:---|:---|
| **M1 大盘指数 · 中信期指多空** | 上证 / 沪深300 / 创业板 / 纳指 / 恒生科技 + 中信期货股指期货多空净单 | 腾讯 `qt.gtimg.cn` + CFFEX（中金所）|
| **M2 板块异动看板** | 10 个持仓板块涨跌幅 + 吸筹/出货信号与强度 | 东财 push2 + AKShare |
| **M3 黄金实时金价** | 国际金价(USD/oz) + 国内金价(CNY/g) + 涨跌幅 | Gold-API + 腾讯 |
| **M4 板块资金流 · 气泡图** | 10 板块资金净流入/流出气泡（红涨/绿跌，簇拥布局）| 东财 push2 + AKShare |

> 另含：指数分时走势**历史回溯**（日期下拉 + 时间轴拖动 + 「● 实时」返回）、开屏赞赏海报 + 右下角常驻赞赏按钮。

---

## 二、技术架构

### 2.1 数据流

```
        ┌──────────────── GitHub Actions (daily-fetch.yml) ────────────────┐
        │  fetch_data.py 每 5 分钟运行                                      │
        │   ├─ 指数    腾讯 qt.gtimg.cn（Tushare best-effort；AKShare/Sina 兜底）│
        │   ├─ 黄金    Gold-API XAU → 腾讯 hf_GC 兜底；USD/CNY 汇率换算国内价 │
        │   ├─ 板块资金流  东财 push2 行业/概念五档拆分（AKShare 兜底）      │
        │   ├─ 中信期指  CFFEX 中金所 CSV（IF/IC/IH/IM 中信会员多空）       │
        │   └─ 吸筹/出货打分  classify_plate（板块内相对打分）              │
        │         │  写入                                                    │
        │         ▼                                                         │
        │   api/data.json       当前快照：indices/gold/plateFlows/citic/stale│
        │   api/plate_em.json   东财主力/散户拆分叠加层                    │
        │   api/history/<日期>/series.json + dates.json  时序（供回溯）    │
        └────────────────────────────┬────────────────────────────────────┘
                                      │ git push（secrets.GH_PAT）
                                      ▼
              浏览器 index.html 每 5 分钟 fetch api/data.json
                ├─ 板块/期指/黄金：直接读快照
                ├─ 指数：叠加「浏览器直连腾讯 qt.gtimg.cn」实时刷新（每 20s，独立于 CI）
                ├─ 东财拆分：applyEastmoneyOverlay 叠加 api/plate_em.json
                └─ 「⟳ 重新抓取」按钮：用用户自己的 GitHub Token 触发 workflow 317447721
```

### 2.2 仓库文件结构

```
white-dashbord/
├── index.html              # 看板全部代码（HTML+CSS+JS），深色终端风格
├── fetch_data.py           # 数据抓取脚本（CI 内运行，写入 api/）
├── donate.html             # 开屏赞赏海报
├── assets/                 # 赞赏收款码等静态资源
├── api/
│   ├── data.json           # 当前数据快照（看板主数据源）
│   ├── plate_em.json       # 东财主力/散户拆分叠加层
│   └── history/<日期>/     # series.json（时序）+ dates.json
└── .github/workflows/
    └── daily-fetch.yml     # Daily Market Data Fetch（每 5 分钟，周一~周五 09:00–16:55 北京时间）
```

> 本地工作区 `wd_repo/` 为部署源镜像（`index.html` / `fetch_data.py` 在此编辑，再经 GitHub Contents API 推送至仓库）。

### 2.3 数据源明细

| 数据 | 主源 | 兜底 | 说明 |
|:---|:---|:---|:---|
| 指数（上证/沪深300/创业板/纳指/恒生科技）| 腾讯 `qt.gtimg.cn` | Tushare pro（best-effort，限频跳过）、AKShare、Sina | 浏览器每 20s 直连腾讯实时刷新；CI 每 5min 存快照 |
| 黄金（XAU USD/CNY）| `api.gold-api.com/price/XAU` | 腾讯 `hf_GC`（国际金）、USD/CNY 汇率 | 国际/国内双价 |
| 板块资金流（10 板块，主力/大户/散户五档）| 东财 `push2.eastmoney.com` 板块排行 | AKShare `stock_fund_flow_industry/concept` | 真实五档拆分；GitHub 海外 runner 偶被东财 502 拒绝时自动降级 AKShare |
| 中信期指多空（IF/IC/IH/IM）| 中金所 `cffex.com.cn` CSV（经 AKShare / 直连）| — | 过滤「中信」会员，净仓 = 多 − 空 |
| 吸筹/出货信号 | `classify_plate()` 本仓内打分 | — | 见 §3.2 |

### 2.4 刷新机制

```
页面加载 → loadData()  全量读取 api/data.json + 叠加东财拆分 + 渲染
setInterval(loadData, 300000)            // 每 5 分钟重读快照（板块/期指/黄金）
setInterval(refreshLiveIndices, 20000)   // 指数每 20 秒直连腾讯实时刷新（独立于 CI）
「⟳ 重新抓取」按钮 → 触发 workflow 317447721（需用户 GitHub Token，仅存本机 localStorage）
```

---

## 三、模块详解

### 3.1 M1 大盘指数 · 中信期指多空
- **指数卡片**：名称、代码、现价、涨跌幅（**红涨绿跌**，A 股惯例）。
- **中信期指净持仓条形图**（全宽卡片，红=净空单 / 绿=净多单）：IF/IC/IH/IM 四行横向条，条长 ∝ |净持仓|；底部展示「全市场合计净空/净多 + 今日减空/加空」（合计红、日变化绿）。
- **指数分时走势图**（canvas）：上证 / 纳指 / 恒生科技 归一化涨跌幅叠加，支持**历史回溯**（日期下拉 + 时间轴滑块 + 「● 实时」返回）。

### 3.2 M2 板块异动看板 + 吸筹/出货打分
- 10 个持仓板块：**芯片 / 半导体 / 细分化工 / 科创创业AI / 机器人 / 新能源电池 / 锂矿 / CPO / PCB / 创新药**，按 |涨跌幅| 降序排列。
- 每张卡片显示涨跌幅 + **信号标签**：`吸筹 · 强/温和/弱` 或 `出货 · 强/温和/弱`。
- 打分器 `classify_plate()`（在 `fetch_data.py`）：
  - **方向严格由当日资金净额 `net` 决定**：净流入 → 吸筹（红），净流出 → 出货（绿）。
  - 强度按**全局动态阈值**分级（按当日最大 |score| 的 0.6 / 0.3 比例切分 强/温和/弱），保证三档有梯度。
  - `reason` 文本说明依据（如「当日净流入；近5日持续吸筹；主力吸筹+散户割肉」），气泡 hover 可见。
  - 成分：主力方向(±) + 主力/散户背离(±) + 价格上下文(±) + 近5日持续性(±，仅加成不反转方向)。

### 3.3 M3 黄金实时金价
- 国际金价 `g.usd`（USD/oz）+ 涨跌幅 `usd_pct`（金 / 红涨绿跌）。
- 国内金价 `g.cny`（CNY/g，由 USD/CNY 汇率换算）+ `cny_pct`。
- 涨跌幅 SVG 条形图。

### 3.4 M4 板块资金流 · 气泡图
- 球径 ∝ |资金净额|；**红=净流入(吸筹) / 绿=净流出(出货)**。
- 布局：每半区最大球为锚点居中贴近分隔线，其余球以黄金角环绕 + 力导向簇拥（小球簇拥大球、互不重叠），分界线之上=吸筹、之下=出货。
- hover 显示：强度 + 原因 + 净额。

---

## 四、数据格式（api/data.json）

```json
{
  "updated": "2026-08-06", "time": "15:00",
  "indices": { "000001": {"name":"上证指数","price":...,"pct":...}, "...": {...} },
  "gold":    { "usd":4259.1, "usd_pct":0.3, "cny":925.97, "cny_pct":0.2, "fx":7.18 },
  "plateFlows": [
    {"name":"芯片","pct":3.95,"散户":-1.6e9,"大户":-1.4e9,"主力":3.0e9,"net":3.0e9,
     "source":"东财行业","signal":"吸筹","strength":"强","score":53.0,
     "reason":"当日净流入；近5日持续吸筹；主力吸筹+散户割肉"}
  ],
  "citic": { "date":"2026-08-11",
             "contracts": {"IF":{"label":"沪深300","net":-17407}, "IC":{...}, "IH":{...}, "IM":{...}},
             "total": {"long":156863,"short":228665,"net":-71802,"prev_net":-72559,"change_net":757} },
  "stale": { "indices":false, "gold":false, "plateFlows":false, "citic":false }
}
```
- `stale`：某项本次抓取失败则标 `true`，前端显示「未更新」标签，并沿用上一快照（避免空白）。
- `signal` / `strength` 由 `classify_plate` 生成；`citic.total.net < 0` = 中信整体净空。
- `citic.total.prev_net` = 上一交易日总净仓；`change_net` = 今日 `net` − 昨日 `net`（>0 = 减空/加多，<0 = 加空/减多）。

---

## 五、部署

### 5.1 GitHub Pages（当前方式）
1. `main` 分支根目录含 `index.html`；仓库 Settings → Pages → Source: Deploy from a branch → `main` / (root)。
2. 数据由 CI 写入并提交，页面静态托管，**无后端**。

### 5.2 数据流水线（daily-fetch.yml）
- `schedule: '*/5 1-8 * * 1-5'`（北京时间约 09:00–16:55，周一至周五，每 5 分钟）。
- 支持 `workflow_dispatch` 与 `repository_dispatch`（type: `fetch-data`）手动触发。
- 步骤：checkout → setup-python 3.11 → `pip install akshare tushare jqdatasdk` → `python fetch_data.py` → 用 `secrets.GH_PAT` 推送 `api/` 变更。

### 5.3 手动触发抓取（网页端）
看板右上「⟳ 重新抓取」按钮：弹出输入 **用户自己的 GitHub Personal Access Token（repo 权限）**，仅存本机 `localStorage`，调 GitHub API 触发 workflow 317447721，轮询完成后自动 `loadData()`。Token 不上传任何服务器。

---

## 六、配色规范（深色金融终端）

| 含义 | 颜色 | 用途 |
|:---|:---|:---|
| 涨 / 净流入 / 吸筹 | `#F43F5E` 红 | A 股惯例：红涨绿跌 |
| 跌 / 净流出 / 出货 | `#10B981` 绿 | |
| 黄金 | `#F5B301` 金 | 金价 |
| 中信期指净持仓 | `#F43F5E` 红 / `#10B981` 绿 | 净空单=红，净多单=绿（沿用涨/跌色惯例，不再用蓝框卡片）|
| 背景 / 卡片 / 边框 | `#0A0A0B` / `#141417` / `#27272A` | 深色主题 |

---

## 七、密钥与 Secrets

| Secret | 用途 |
|:---|:---|
| `GH_PAT` | CI 推送 `api/` 数据（repo 权限）；**如过期则数据停更** |
| `TUSHARE_TOKEN` | Tushare pro 指数兜底（积分不足时自动跳过）|
| `JQ_USER` / `JQ_PASSWORD` | 聚宽 JQData 指数兜底（可选）|

> ⚠️ 密钥仅存 GitHub Secrets，不写入代码。

---

## 八、更新日志

| 日期 | 变更 |
|:---|:---|
| 2026-08-06 | 移除「板块讨论度」模块（整行删除，打分不含此项）；重写本技术文档 |
| 2026-08-11 | 中信期指多空模块从蓝框卡片改为横向条形图（条长 ∝ |净持仓|，红=净空单/绿=净多单）；data.json 的 citic.total 新增 prev_net / change_net 日环比字段 |
| 2026-07-31 ~ 08-06 | 板块真实主力/散户拆分改由 CI 东财 push2 直接抓取（去本机方案）；新增中信期指、分时回溯、吸筹/出货打分、开屏赞赏 |
| 2026-07-19 | 初版 4 模块看板（原 market-monitor 仓库）|

---

> ⚠️ 风险提示：本看板数据来自公开行情接口，仅供参考，**不构成任何投资建议**。中信期指多空等数据为客观呈现，入市有风险，决策需独立判断。
