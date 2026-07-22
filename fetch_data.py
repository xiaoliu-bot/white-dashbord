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
    """指数来源（优先级）：Tushare pro（best-effort，限频时跳过）→ 腾讯 qt.gtimg.cn（CI 稳定主用）→ AKShare（兜底）。
    腾讯接口在 CI 中稳定可用（恒生科技已验证），故 A 股三大指数也统一走腾讯一次请求拿全。
    """
    result = fetch_indices_tushare(os.environ.get('TUSHARE_TOKEN'))  # best-effort，限频时返回 {}
    covered = set(result.keys())

    # 腾讯 qt.gtimg.cn：上证/沪深300/创业板/恒生科技（一次请求，CI 稳定）
    tencent = {
        '000001': ('上证指数', 'sh000001'),
        '000300': ('沪深300',  'sh000300'),
        '399006': ('创业板指',  'sz399006'),
        'HSTECH': ('恒生科技', 'hkHSTECH'),
    }
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
            gcode = m.group(1)            # sh000001 / sh000300 / sz399006 / hkHSTECH
            p = m.group(2).split('~')
            if len(p) < 33:
                continue
            for key, (name, tcode) in tencent.items():
                if tcode == gcode:
                    if key in covered:
                        break  # Tushare 已给到，优先保留
                    try:
                        price = float(p[3]) if p[3] else 0
                        prev = float(p[4]) if p[4] else price
                        chg = float(p[32]) if p[32] else 0
                        pct = round(chg / prev * 100, 2) if prev else 0
                        result[key] = {'name': name, 'price': round(price, 2),
                                       'chg': round(chg, 2), 'pct': pct}
                        print(f"  ✅ 腾讯 {name}: {price} ({pct}%)")
                    except Exception as e:
                        print(f"  · 腾讯 {name} 解析失败: {e}")
                    break
    except Exception as e:
        print(f"  ❌ 腾讯指数接口失败: {e}")

    # AKShare 兜底（东财，加重试；CI 中偶发 Connection aborted）
    ak_map = {'000001': '上证指数', '000300': '沪深300', '399006': '创业板指'}
    for attempt in range(3):
        missing = [c for c in ak_map if c not in result]
        if not missing:
            break
        try:
            df = ak.stock_zh_index_spot_em(symbol='上证系列指数')
            for code in missing:
                if code in ('000001', '000300'):
                    row = df[df['代码'] == code]
                    if not row.empty:
                        rr = row.iloc[0]
                        result[code] = {'name': ak_map[code],
                                        'price': round(float(rr['最新价'] or 0), 2),
                                        'chg': round(float(rr['涨跌额'] or 0), 2),
                                        'pct': round(float(rr['涨跌幅'] or 0), 2)}
            if '399006' not in result:
                df2 = ak.stock_zh_index_spot_em(symbol='深证系列指数')
                cy = df2[df2['名称'] == '创业板指']
                if not cy.empty:
                    rr = cy.iloc[0]
                    result['399006'] = {'name': '创业板指',
                                        'price': round(float(rr['最新价'] or 0), 2),
                                        'chg': round(float(rr['涨跌额'] or 0), 2),
                                        'pct': round(float(rr['涨跌幅'] or 0), 2)}
            print(f"  ✅ AKShare 补充命中: {[c for c in ak_map if c in result]}")
            break
        except Exception as e:
            print(f"  · AKShare 补充尝试 {attempt+1} 失败: {e}")
            time.sleep(2)
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

def main():
    today = datetime.date.today().strftime('%Y-%m-%d')
    print(f"📅 抓取日期: {today}")
    print()

    print("[1/3] 抓取指数...")
    indices = fetch_indices()
    print()

    print("[2/3] 抓取黄金...")
    gold = fetch_gold()
    print()

    print("[3/3] 抓取板块资金流...")
    plate_data = fetch_plate_data()

    # 组装 plateFlows
    plateFlows = []
    for plate_name in ['芯片', '半导体', '细分化工', '科创创业AI', '机器人',
                       '新能源电池', '锂矿', 'CPO', 'PCB', '创新药']:
        d = plate_data.get(plate_name, {})
        net = d.get('net', 0)
        # 估算散户/大户/主力比例
        zhu = int(net * 0.55)
        da = int(net * 0.28)
        san = net - zhu - da
        print(f"  {plate_name}: {d.get('行业名','?')} | {d.get('pct',0):+.2f}% | 净额:{(net/1e8):+.2f}亿 [{d.get('source','未知')}]")
        plateFlows.append({
            'name': plate_name,
            'code': '',   # 暂不填东财代码
            'pct': d.get('pct', 0),
            '行业名': d.get('行业名', ''),
            '散户': san,
            '大户': da,
            '主力': zhu,
            'net': net,
            'source': d.get('source', '未知'),
        })
    print()

    data = {
        'updated': today,
        'indices': indices,
        'gold': gold,
        'plateFlows': plateFlows,
    }

    os.makedirs('api', exist_ok=True)
    with open('api/data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 已写入 api/data.json")

    hist_dir = 'api/history'
    os.makedirs(hist_dir, exist_ok=True)
    hist_file = os.path.join(hist_dir, f'{today}.json')
    with open(hist_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 已写入 {hist_file}")

    print(f"\n📊 摘要:")
    for k, v in indices.items():
        print(f"  {v['name']}: {v['price']} ({v['pct']}%)")
    print(f"  黄金: {gold['usd']} USD/oz | {gold['cny']} CNY/g")

if __name__ == '__main__':
    main()
