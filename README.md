# Market Monitor — 市场监控面板 技术文档

> 版本：v1.0 | 更新：2026-07-19
> 仓库：https://github.com/xiaoliu-bot/market-monitor
> 线上地址：https://xiaoliu-bot.github.io/market-monitor/

---

## 一、项目概述

Market Monitor 是一个面向 A 股投资者的**实时市场监控看板**，纯前端单文件 HTML，无需后端，可直接在 GitHub Pages 免费托管并自动更新。

### 核心功能

| 模块 | 功能 | 数据源 |
|:---|:---|:---|
| 模块一 · 大盘指数 | 上证 / 纳斯达克 / 恒生科技 实时行情 | 东方财富 + Yahoo Finance |
| 模块二 · 板块异动 | 持仓板块涨跌 + 吸筹/流出状态 | 东方财富 |
| 模块三 · 黄金 | 国际金价(USD) + 国内金价(CNY) + 两周预测 | Yahoo Finance |
| 模块四 · 资金流 | 11个持仓板块资金流，**每30秒自动刷新** | 东方财富 |
| 警报栏 | 主力/机构大量流入流出时自动预警 | — |

---

## 二、技术架构

### 2.1 文件结构

```
market-monitor/
├── index.html          # 全部代码（HTML + CSS + JS），约 950 行
├── api/
│   └── data.json       # 节假日/收盘后兜底数据（可选）
└── .github/
    └── workflows/      # GitHub Actions（可选，用于自动抓取 data.json）
```

### 2.2 数据源

| 接口 | 用途 | 域名 |
|:---|:---|:---|
| `push2.eastmoney.com/api/qt/stock/get` | A股指数、大盘指数 | eastmoney.com |
| `push2.eastmoney.com/api/qt/clist/get` | 板块资金流 | eastmoney.com |
| `query1.finance.yahoo.com` | 纳斯达克、黄金 | yahoo.com |
| `/api/data.json` | 节假日兜底数据（本地） | 自己的 Pages |

### 2.3 定时刷新机制

```
页面加载
    └── loadAllData()  首次全量加载
            ├── loadIndices()      指数
            ├── loadGold()         黄金
            ├── loadFundFlows()   资金流（骨架+数据）
            └── loadPlates()       板块异动

setInterval(loadAllData, 5 * 60 * 1000)   // 每 5 分钟全量刷新
setInterval(loadFundFlows, 30 * 1000)    // 每 30 秒刷新资金流（独立）
setInterval(updateNextAutoRefresh, 30000) // 每 30 秒更新倒计时
```

---

## 三、模块详解

### 3.1 持仓板块配置

配置集中在一个 JS 对象中，修改板块只需改这里：

```javascript
const CONFIG = {
  plates: [
    { code: '886052', name: '芯片' },
    { code: '886035', name: '半导体' },
    { code: '886059', name: '细分化工' },
    { code: '886031', name: '科创创业AI' },
    { code: '886541', name: '机器人' },
    { code: '886542', name: '新能源电池' },
    { code: 'HSTECH',    name: '恒生科技' },
    { code: '886080', name: '创新药' },
    { code: '886054', name: '锂矿' },
    { code: '886083', name: 'CPO' },
    { code: '886041', name: 'PCB' },
  ],
};
```

> 东财板块代码格式：`886XXX`（A股板块），`HSTECH`（恒生科技，走独立接口）

### 3.2 模块四 · 资金流渲染逻辑

参考「小鹿看数据」可视化风格，每张卡片结构：

```
┌─────────────────────────────────────┐
│  板块名              +0.82亿        │  ← 净流入（红涨绿跌）
├─────────────────────────────────────┤
│  🌱 散户  ████████░░░░  +0.12亿     │
│  🏠 大户  ██████████░░  +0.30亿     │
│  ⚡ 主力  ████████████  +0.40亿     │
├─────────────────────────────────────┤
│  ▲ 吸筹中                            │  ← 状态标签
└─────────────────────────────────────┘
```

核心渲染函数：`loadFundFlows()`（约 90 行 JS）

数据来源 API：
```
GET https://push2.eastmoney.com/api/qt/clist/get
  ?fid=f62                          # 按主力净流入排序
  &fltt=2                           # 涨跌幅精确到小数
  &invt=2                           # 指数化
  &fs=b:+{板块代码}                  # 板块成分股过滤
  &fields=f12,f14,f62,f184,f2,f3   # 股票代码/名/主力净流入/机构净流入/现价/涨跌幅
```

### 3.3 刷新频率控制

使用 localStorage 记录每日/每小时刷新次数，防止 API 超限：

```javascript
localStorage: {
  mm_hourly: 0,    // 本小时已刷新次数
  mm_daily:  0,    // 今日已刷新次数
  mm_hr: timestamp, // 上次小时重置时间
  mm_dr: timestamp, // 上次日重置时间
}
// 上限：每小时 50 次，每天 200 次
```

---

## 四、部署

### 4.1 GitHub Pages（当前方式）

1. 将 `index.html` 推送到 `xiaoliu-bot/market-monitor` 仓库的 `main` 分支
2. Settings → Pages → Source: Deploy from a branch → main / (root)
3. 等待 1-2 分钟，页面自动上线

### 4.2 添加 data.json 兜底数据

在 `api/data.json` 放置节假日/收盘后数据：

```json
{
  "updated": "2026-07-18 15:00",
  "indices": {
    "000001": { "price": 2980.32, "pct": -0.45 }
  },
  "gold": {
    "usd": 2420.5, "usd_pct": 0.32,
    "cny": 578.2,  "cny_pct": 0.28
  },
  "plateFlows": [
    { "name": "芯片", "散户": 12000000, "大户": 30000000, "主力": 40000000 }
  ]
}
```

---

## 五、配色规范

| 含义 | 颜色 | CSS class |
|:---|:---|:---|
| 涨 / 流入 / 吸筹 | `#ff4444` 红 | `.up` `.tag-in` |
| 跌 / 流出 | `#44ff88` 绿 | `.down` `.tag-out` |
| 黄金专项 | `#ffd700` 金 | `.gold-value` |
| 背景 | `#000` 黑 | `body` |
| 卡片背景 | `#111` 深灰 | `.card` `.flow-card` |
| 边框 | `#222` 深灰 | `border` |

---

## 六、GitHub Token 管理

> ⚠️ Token 保密，只存后缀便于识别，不存完整值

| Token | 状态 | 权限 |
|:---|:---|:---|
| `ghp_1C…JSGF` | ✅ 当前有效 | repo（读+写）|
| `ghp_Jn…D09G` | ❌ 过期 | — |
| `GvUord…D09G` | ❌ 过期 | — |

---

## 七、更新日志

| 日期 | 变更 |
|:---|:---|
| 2026-07-19 | 完成4模块重构；模块四改为小鹿看数据风格；加入每30秒自动刷新+动态闪烁效果 |
| 2026-07-18 | 初始化项目，部署至 GitHub Pages |
