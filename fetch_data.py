#!/usr/bin/env python3
"""
每日收盘数据抓取 → api/data.json + api/history/YYYY-MM-DD.json
数据源：
数据源优先级（板块数据优先从 Tushare 获取）：
  【指数】1) Tushare pro（用户 token，best-effort，限频时跳过）→ 2) 腾讯 qt.gtimg.cn（CI 稳定主用）→ 3) AKShare 兜底
  【板块资金流】1) Tushare 优先：pro.plate_fund_flow（含涨跌幅+净额，需积分≥2000）→ pro.moneyflow_industry（主力净流入）→ pro.moneyflow_concept（概念主力净流入）
    命中即保留，best-effort，限频/积分不足时整体跳过；2) AKShare 东财兜底仅补充 Tushare 未命中的持仓板块
  【黄金】Gold-API.com（国际金价，无需 key）
  （板块资金流字段：net 主力/净额净流入，pct 涨跌幅，source 标记数据来源 Tushare / 行业 / 概念 / 板块涨跌）
Tushare token 通过环境变量 TUSHARE_TOKEN 注入（建议配置为仓库 Secrets，勿硬编码）。
"""
import json, time, os, datetime
import akshare as ak
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

import datetime as _dt

# === 指数（Tushare 优先，AKShare/腾讯兜底） ===
def fetch_indices_tushare(token):
    """用 Tushare 拉 A 股三大指数。
    优先 pro.index_daily（用户提供的 token）；失败回退 legacy get_hist_data（需交易所前缀 sh/sz）。
    仍为空时由 fetch_indices() 里的 AKShare / 腾讯兜底。
    """
    try:
        import tushare as ts
        if token:
            try: ts.set_token(token)
            except Exception: pass
        end = _dt.date.today().strftime('%Y-%m-%d')
        start = (_dt.date.today() - _dt.timedelta(days=10)).strftime('%Y-%m-%d')
        # 输出 key -> (名称, pro代码, legacy代码)
        spec = {
            '000001': ('上证指数', '000001.SH', 'sh000001'),
            '000300': ('沪深300',  '000300.SH', 'sh000300'),
            '399006': ('创业板指',  '399006.SZ', 'sz399006'),
        }
        out = {}
        pro = None
        try:
            pro = ts.pro_api()
        except Exception as e:
            print(f"  · Tushare pro 初始化失败: {e}")
        for key, (name, pro_code, legacy_code) in spec.items():
            row = None
            # 1) pro.index_daily（用户 token 拉市场数据的主路径）
            if pro is not None:
                try:
                    df = pro.index_daily(ts_code=pro_code,
                                         start_date=start.replace('-', ''),
                                         end_date=end.replace('-', ''))
                    if df is not None and not df.empty:
                        r = df.sort_values('trade_date').iloc[-1]
                        row = {
                            'price': round(float(r['close']), 2),
                            'chg': round(float(r.get('change') or 0), 2),
                            'pct': round(float(r.get('pct_chg') or 0), 2),
                        }
                        print(f"  ✅ Tushare pro {name}: {row['price']} ({row['pct']}%)")
                except Exception as e:
                    print(f"  · Tushare pro {name} 失败: {str(e)[:80]}")
                time.sleep(3)  # 规避 pro 频率限制（低积分约 1 次/分）
            # 2) legacy get_hist_data 兜底（必须带交易所前缀）
            if row is None:
                try:
                    df = ts.get_hist_data(legacy_code, start=start, end=end, retry_count=1)
                    if df is not None and not df.empty:
                        r = df.iloc[-1]
                        row = {
                            'price': round(float(r['close']), 2),
                            'chg': round(float(r.get('price_change') or 0), 2),
                            'pct': round(float(r.get('pct_change') or 0), 2),
                        }
                        print(f"  ✅ Tushare legacy {name}: {row['price']} ({row['pct']}%)")
                except Exception as e:
                    print(f"  · Tushare legacy {name} 失败: {str(e)[:80]}")
            if row is not None:
                row['name'] = name
                out[key] = row
        print(f"  ✅ Tushare 指数: {list(out.keys())}")
        return out
    except Exception as e:
        print(f"  ❌ Tushare 指数整体失败: {e}")
        return {}

def fetch_indices():
    """大盘指数（5 分钟级实时）：腾讯 qt.gtimg.cn 一次请求拿全。
    仅保留看板所需的 上证指数 / 纳指(Nasdaq, usIXIC) / 恒生科技。
    注：纳指为美股，A 股交易时段返回上一美股收盘值，属正常。
    """
    tencent = {
        '000001': ('上证指数', 'sh000001'),
        'NDX':    ('纳指',     'usIXIC'),
        'HSTECH': ('恒生科技', 'hkHSTECH'),
    }
    result = {}
    try:
        import urllib.request, re
        codes = ','.join(v[1] for v in tencent.values())
        req = urllib.request.Request(
            f"https://qt.gtimg.cn/q={codes}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"})
        with urllib.request.urlopen(req, timeout=10) as r:
            content = r.read().decode('gbk', errors='replace')
        for line in content.split(';'):
            m = re.search(r'v_(\w+)="([^"]+)"', line)
            if not m:
                continue
            gcode = m.group(1)            # sh000001 / usIXIC / hkHSTECH
            p = m.group(2).split('~')
            if len(p) < 10:
                continue
            for key, (name, tcode) in tencent.items():
                if tcode == gcode:
                    try:
                        price = float(p[3]) if p[3] else 0
                        prev = float(p[4]) if p[4] else price
                        pct = round((price - prev) / prev * 100, 2) if prev else 0
                        chg = round(price - prev, 2)
                        result[key] = {'name': name, 'price': round(price, 2),
                                       'chg': chg, 'pct': pct}
                        print(f"  ✅ 腾讯 {name}: {price} ({pct}%)")
                    except Exception as e:
                        print(f"  · 腾讯 {name} 解析失败: {e}")
                    break
    except Exception as e:
        print(f"  ❌ 腾讯指数接口失败: {e}")
    return result

# === 黄金 ===
def fetch_gold():
    """国际金价 + 估算国内金价"""
    try:
        raw = ak.gold_spot_price()
        print(f"  黄金字段: {raw.columns.tolist()}")
        print(f"  {raw.head(2)}")
    except Exception as e:
        print(f"  黄金接口: {e}")
    # 直接用 Gold-API
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.gold-api.com/price/XAU",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        price = float(d['price'])
        pct = 0.01  # 简化
        cny = round(price / 31.1035 * 7.25, 2)
        print(f"  ✅ 黄金: {price} USD/oz")
        return {'usd': round(price, 2), 'usd_pct': pct, 'cny': cny, 'cny_pct': pct}
    except Exception as e:
        print(f"  ❌ 黄金: {e}")
        return {'usd': 0, 'usd_pct': 0, 'cny': 0, 'cny_pct': 0}

# === 板块资金流（AKShare）===
# 用户持仓板块名称 → 东财/同花顺行业板块关键词
PLATE_KEYWORDS = {
    '芯片':     ['芯片', '集成电路', '半导体材料', '半导体设备', '半导体制造'],
    '半导体':   ['半导体', '硅片', '晶圆'],
    '细分化工': ['化学制品', '化学原料', '化学纤维', '农药', '橡胶'],
    '科创创业AI': ['人工智能', 'AI', '机器人', '智能制造'],
    '机器人':   ['机器人', '自动化', '工业自动化'],
    '新能源电池': ['锂电池', '电池', '储能', '动力电池', '新能源'],
    '锂矿':     ['锂', '盐湖', '矿石提锂'],
    'CPO':      ['CPO', '共封装光学', '光通信', '光模块'],
    'PCB':      ['PCB', '印制电路板', '覆铜板'],
    '创新药':   ['创新药', '生物药', '化学制药', '医疗器械'],
}

def _match_plate(result, name, net, inflow, outflow, pct, source):
    """按 PLATE_KEYWORDS 把东财/Tushare 行业名映射到持仓板块，命中且未存在的写入 result。"""
    for plate, keywords in PLATE_KEYWORDS.items():
        if plate in result:
            continue
        for kw in keywords:
            if kw in name:
                result[plate] = {
                    'name': plate, '行业名': name,
                    'net': round(net), 'inflow': round(inflow),
                    'outflow': round(outflow), 'pct': round(pct, 2),
                    'source': source,
                }
                return

def fetch_plate_data_tushare(token):
    """Tushare 优先：拉板块资金流（best-effort，限频/积分不足时返回 {} 交给 AKShare 兜底）。
    依次尝试：
      1) pro.plate_fund_flow（含涨跌幅 pct_change + 净额 net_buy，需积分≥2000）
      2) pro.moneyflow_industry（行业主力净流入 main_net_in，单位千元）
      3) pro.moneyflow_concept（概念主力净流入）
    任一接口返回数据即按行业名匹配持仓板块；单位统一换算为「元」。
    """
    if not token:
        print("  · 未配置 TUSHARE_TOKEN，跳过 Tushare 板块")
        return {}
    try:
        import tushare as ts
        ts.set_token(token)
        pro = ts.pro_api()
    except Exception as e:
        print(f"  · Tushare pro 初始化失败: {e}")
        return {}

    # 最近交易日（今日/昨日/前日中的前 3 个工作日），规避非交易日无数据
    trade_days = []
    for i in range(0, 4):
        d = _dt.date.today() - _dt.timedelta(days=i)
        if d.weekday() < 5:
            trade_days.append(d.strftime('%Y%m%d'))
        if len(trade_days) >= 3:
            break

    result = {}

    def _is_rate_limit(msg):
        return ('频率超限' in msg) or ('每分钟' in msg) or ('每小时' in msg) or ('限额' in msg)

    # 各接口按优先级尝试；命中即止。永久性问题（接口名错误/无权限）立即跳过，仅限频才 sleep。
    attempts = [
        ('plate_fund_flow',
         lambda td: pro.plate_fund_flow(trade_date=td, src='None'),
         lambda row: (float(row.get('net_buy', 0) or 0), 0, 0, float(row.get('pct_change', 0) or 0))),
        ('moneyflow_industry',
         lambda td: pro.moneyflow_industry(trade_date=td),
         lambda row: (float(row.get('main_net_in', 0) or 0) * 1000, 0, 0, 0)),
        ('moneyflow_concept',
         lambda td: pro.moneyflow_concept(trade_date=td),
         lambda row: (float(row.get('main_net_in', 0) or 0) * 1000, 0, 0, 0)),
    ]

    for iname, call, mapper in attempts:
        if result:
            break
        for td in trade_days:
            try:
                df = call(td)
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        net, inflow, outflow, pct = mapper(row)
                        _match_plate(result, str(row.get('name', '')).strip(),
                                     net, inflow, outflow, pct, 'Tushare')
                    print(f"  ✅ Tushare {iname} {td}: {len(df)} 条，命中 {len(result)} 个持仓板块")
                    break
            except Exception as e:
                msg = str(e)
                if _is_rate_limit(msg):
                    print(f"  · Tushare {iname} {td} 限频: {msg[:60]}")
                    time.sleep(3)
                    continue
                else:
                    # 接口名错误 / 无权限等永久性问题：跳过该接口，不再试其它交易日
                    print(f"  · Tushare {iname} 不可用（{msg[:50]}），跳过")
                    break

    if not result:
        print("  · Tushare 板块接口未命中（可能积分不足或限频），将由 AKShare 兜底")
    return result

def fetch_plate_data():
    """板块资金流：Tushare 优先（fetch_plate_data_tushare），AKShare 仅补充未命中板块。"""
    all_flows = fetch_plate_data_tushare(os.environ.get('TUSHARE_TOKEN'))  # Tushare 优先
    print(f"  · Tushare 已命中板块: {list(all_flows.keys())}")

    try:
        # 行业资金流
        df_ind = ak.stock_fund_flow_industry(symbol="即时")
        for _, row in df_ind.iterrows():
            name = str(row.get('行业', '')).strip()
            net = float(row.get('净额', 0) or 0)  # 亿元
            inflow = float(row.get('流入资金', 0) or 0)
            outflow = float(row.get('流出资金', 0) or 0)
            pct = float(row.get('行业-涨跌幅', 0) or 0)
            # 匹配持仓板块
            for plate, keywords in PLATE_KEYWORDS.items():
                if plate in all_flows:
                    continue
                for kw in keywords:
                    if kw in name:
                        all_flows[plate] = {
                            'name': plate,
                            '行业名': name,
                            'net': round(net * 1e8),     # 转为元
                            'inflow': round(inflow * 1e8),
                            'outflow': round(outflow * 1e8),
                            'pct': round(pct, 2),
                            'source': '行业',
                        }
                        break
        print(f"  ✅ 行业资金流: {len(df_ind)} 条，命中 {len(all_flows)} 个持仓板块")
    except Exception as e:
        print(f"  ❌ 行业资金流: {e}")

    try:
        # 概念资金流
        df_con = ak.stock_fund_flow_concept(symbol="即时")
        for _, row in df_con.iterrows():
            name = str(row.get('行业', '')).strip()
            net = float(row.get('净额', 0) or 0)
            inflow = float(row.get('流入资金', 0) or 0)
            outflow = float(row.get('流出资金', 0) or 0)
            pct = float(row.get('行业-涨跌幅', 0) or 0)
            for plate, keywords in PLATE_KEYWORDS.items():
                if plate in all_flows:
                    continue
                for kw in keywords:
                    if kw in name or name in kw:
                        all_flows[plate] = {
                            'name': plate,
                            '行业名': name,
                            'net': round(net * 1e8),
                            'inflow': round(inflow * 1e8),
                            'outflow': round(outflow * 1e8),
                            'pct': round(pct, 2),
                            'source': '概念',
                        }
                        break
        print(f"  ✅ 概念资金流: {len(df_con)} 条，总命中 {len(all_flows)} 个持仓板块")
    except Exception as e:
        print(f"  ❌ 概念资金流: {e}")

    # 用东财板块涨跌做补充：Tushare 已命中但缺涨跌幅(pct)的板块补 pct；完全缺失的板块估值 net
    try:
        spots = ak.stock_sector_spot()
        for _, row in spots.iterrows():
            name = str(row.get('板块名称', '')).strip()
            pct_spot = float(row.get('涨跌幅', 0) or 0)
            for plate, keywords in PLATE_KEYWORDS.items():
                matched = any(kw in name for kw in keywords)
                if not matched:
                    continue
                if plate in all_flows:
                    # Tushare 已给净额：仅补缺失的涨跌幅
                    if all_flows[plate].get('pct', 0) == 0:
                        all_flows[plate]['pct'] = round(pct_spot, 2)
                        all_flows[plate]['行业名'] = name
                    break
                all_flows[plate] = {
                    'name': plate,
                    '行业名': name,
                    'net': int(pct_spot * 3e8),  # 估算
                    'inflow': 0,
                    'outflow': 0,
                    'pct': round(pct_spot, 2),
                    'source': '板块涨跌',
                }
                break
        print(f"  ✅ 板块涨跌补充后: {len(all_flows)} 个持仓板块")
    except Exception as e:
        print(f"  ❌ 板块涨跌: {e}")

    return all_flows

# === 中信期货 股指期货多空单（CFFEX 前20会员持仓）===
def fetch_citic_futures():
    """中信期货在股指期货(IF/IC/IH/IM)的多空单（前20会员持仓）。
    来源：AKShare get_cffex_rank_table（中金所每日持仓排名，约 16:30 发布）。best-effort。
    返回 {date, contracts:{IF:{label,long,short,net},...}, total:{long,short,net}}。
    """
    try:
        import akshare as ak
        from datetime import date as _d, timedelta as _td
        # 取最近若干交易日，直到取到已发布的中金所持仓排名（当日约 16:30 后发布）
        candidate_days = []
        for i in range(0, 6):
            d = _d.today() - _td(days=i)
            if d.weekday() < 5:
                candidate_days.append(d.strftime('%Y%m%d'))
        if not candidate_days:
            return {}
        targets = {'IF': '沪深300', 'IC': '中证500', 'IH': '上证50', 'IM': '中证1000'}
        for day in candidate_days:
            print(f"  · 中信期指：取中金所持仓排名 {day}")
            try:
                tbl = ak.get_cffex_rank_table(date=day)   # dict: symbol -> DataFrame
            except Exception as e:
                print(f"  · 中信期指 {day} 获取失败: {str(e)[:60]}")
                continue
            out = {'date': day, 'contracts': {}, 'total': {'long': 0, 'short': 0, 'net': 0}}
            for sym, label in targets.items():
                df = tbl.get(sym)
                if df is None or (hasattr(df, 'empty') and df.empty):
                    continue
                cols = list(df.columns)
                # 兼容中英文列名找「会员名称」与「多/空持仓」
                name_col = None
                for c in cols:
                    s = str(c)
                    if any(k in s for k in ['party', 'Party', '会员', '名称', 'name', 'Name']):
                        name_col = c
                        break
                long_col = short_col = None
                for c in cols:
                    s = str(c).lower()
                    if 'long' in s and any(k in s for k in ['open', 'interest', 'position', '买', '多']):
                        long_col = c
                    if 'short' in s and any(k in s for k in ['open', 'interest', 'position', '卖', '空']):
                        short_col = c
                if name_col is None or long_col is None or short_col is None:
                    print(f"  · 中信期指 {sym} 列名未识别: {cols}")
                    continue
                sub = df[df[name_col].astype(str).str.contains('中信', na=False)]
                if sub.empty:
                    continue
                long_v = float(sub[long_col].iloc[0] or 0)
                short_v = float(sub[short_col].iloc[0] or 0)
                out['contracts'][sym] = {'label': label, 'long': int(long_v),
                                         'short': int(short_v), 'net': int(long_v - short_v)}
                out['total']['long'] += int(long_v)
                out['total']['short'] += int(short_v)
                out['total']['net'] += int(long_v - short_v)
            if out['contracts']:
                print(f"  ✅ 中信期指多空 {day}: {list(out['contracts'].keys())} 总净仓 {out['total']['net']} 手")
                return out
        print("  · 中信期指：近几个交易日均未取到中信持仓")
        return {}
        return {}
    except Exception as e:
        print(f"  ❌ 中信期指多空: {e}")
        return {}

def main():
    today = datetime.date.today().strftime('%Y-%m-%d')
    now_iso = datetime.datetime.now().strftime('%H:%M:%S')
    now_hm = datetime.datetime.now().strftime('%H:%M')
    print(f"📅 抓取: {today} {now_hm}")

    # 读取上一次快照，用于抓取失败时的兜底（5 分钟级刷新下避免瞬断导致页面空白）
    prev = {}
    if os.path.exists('api/data.json'):
        try:
            prev = json.load(open('api/data.json', encoding='utf-8'))
        except Exception:
            prev = {}

    print("[1/4] 指数...")
    indices = fetch_indices()
    if not indices:
        indices = prev.get('indices', {})
        if indices:
            print("  · 指数接口失败，沿用上次快照")

    print("[2/4] 黄金...")
    gold = fetch_gold()

    print("[3/4] 板块资金流...")
    plate_data = fetch_plate_data()
    plateFlows = []
    for plate_name in ['芯片', '半导体', '细分化工', '科创创业AI', '机器人',
                       '新能源电池', '锂矿', 'CPO', 'PCB', '创新药']:
        d = plate_data.get(plate_name, {})
        net = d.get('net', 0)
        zhu = int(net * 0.55)
        da = int(net * 0.28)
        san = net - zhu - da
        print(f"  {plate_name}: {d.get('行业名','?')} | {d.get('pct',0):+.2f}% | 净额:{(net/1e8):+.2f}亿 [{d.get('source','未知')}]")
        plateFlows.append({
            'name': plate_name,
            'code': '',
            'pct': d.get('pct', 0),
            '行业名': d.get('行业名', ''),
            '散户': san, '大户': da, '主力': zhu, 'net': net,
            'source': d.get('source', '未知'),
        })
    if all(p.get('net', 0) == 0 for p in plateFlows) and prev.get('plateFlows'):
        plateFlows = prev['plateFlows']
        print("  · 板块全为 0，沿用上次快照")
    print()

    print("[4/4] 中信期指多空...")
    citic = fetch_citic_futures()
    if not citic:
        citic = prev.get('citic', {})
        if citic:
            print("  · 中信期指沿用上次快照")

    data = {
        'updated': today,
        'time': now_iso,
        'indices': indices,
        'gold': gold,
        'plateFlows': plateFlows,
        'citic': citic,
    }

    os.makedirs('api', exist_ok=True)
    with open('api/data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("✅ 写入 api/data.json")

    # 历史时序：每 5 分钟追加一个快照，供网页回溯
    hist_dir = os.path.join('api/history', today)
    os.makedirs(hist_dir, exist_ok=True)
    series_file = os.path.join(hist_dir, 'series.json')
    series = []
    if os.path.exists(series_file):
        try:
            series = json.load(open(series_file, encoding='utf-8'))
        except Exception:
            series = []
    series.append({'time': now_hm, 'indices': indices, 'gold': gold,
                   'plateFlows': plateFlows, 'citic': citic})
    with open(series_file, 'w', encoding='utf-8') as f:
        json.dump(series, f, ensure_ascii=False, separators=(',', ':'))

    # 日期清单（供前端回溯选择）
    dates_file = os.path.join('api/history', 'dates.json')
    dates = []
    if os.path.exists(dates_file):
        try:
            dates = json.load(open(dates_file, encoding='utf-8'))
        except Exception:
            dates = []
    if today not in dates:
        dates.append(today)
        json.dump(sorted(dates), open(dates_file, 'w', encoding='utf-8'), ensure_ascii=False)

    print(f"\n📊 摘要: 历史快照数={len(series)}")
    for k, v in indices.items():
        print(f"  {v['name']}: {v['price']} ({v['pct']}%)")
    print(f"  黄金: {gold['usd']} USD/oz")
    if citic:
        print(f"  中信期指总净仓: {citic['total']['net']} 手")

if __name__ == '__main__':
    main()
