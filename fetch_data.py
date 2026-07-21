#!/usr/bin/env python3
"""
每日收盘数据抓取 → api/data.json + api/history/YYYY-MM-DD.json
数据源：
  - AKShare（东财数据，内部绕过封禁）
  - Gold-API.com（国际金价，无需 key）
  - AKShare stock_sector_spot（板块涨跌）
  - AKShare stock_fund_flow_industry（行业资金流）
  - AKShare stock_fund_flow_concept（概念资金流）
"""
import json, time, os, datetime
import akshare as ak
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

# === 指数 ===
def fetch_indices():
    """上证/沪深300/创业板 + 恒生科技"""
    result = {}
    try:
        # 上证、沪深300（东财上证系列指数）
        df = ak.stock_zh_index_spot_em(symbol='上证系列指数')
        index_map = {
            '000001': ('上证指数',),
            '000300': ('沪深300',),
        }
        for code, (name,) in index_map.items():
            row = df[df['代码'] == code]
            if not row.empty:
                r = row.iloc[0]
                result[code] = {
                    'name': name,
                    'price': round(float(r['最新价'] or 0), 2),
                    'chg': round(float(r['涨跌额'] or 0), 2),
                    'pct': round(float(r['涨跌幅'] or 0), 2),
                }

        # 创业板（深证系列指数）
        df2 = ak.stock_zh_index_spot_em(symbol='深证系列指数')
        cy = df2[df2['名称'] == '创业板指']
        if not cy.empty:
            r = cy.iloc[0]
            result['399006'] = {
                'name': '创业板指',
                'price': round(float(r['最新价'] or 0), 2),
                'chg': round(float(r['涨跌额'] or 0), 2),
                'pct': round(float(r['涨跌幅'] or 0), 2),
            }
        print(f"  ✅ 指数: {list(result.keys())}")
    except Exception as e:
        print(f"  ❌ 指数失败: {e}")

    # 恒生科技（腾讯接口）
    try:
        import urllib.request, re
        req = urllib.request.Request(
            "https://qt.gtimg.cn/q=hkHSTECH",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"})
        with urllib.request.urlopen(req, timeout=8) as r:
            content = r.read().decode('gbk', errors='replace')
        m = re.search(r'"([^"]+)"', content)
        if m:
            p = m.group(1).split('~')
            price = float(p[3]) if p[3] else 0
            prev = float(p[4]) if p[4] else price
            chg = float(p[32]) if p[32] else 0
            pct = round(chg / prev * 100, 2) if prev else 0
            result['HSTECH'] = {'name': '恒生科技', 'price': round(price, 2), 'chg': round(chg, 2), 'pct': pct}
            print(f"  ✅ 恒生科技: {price} ({pct}%)")
    except Exception as e:
        print(f"  ❌ 恒生科技: {e}")
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

def fetch_plate_data():
    """从AKShare获取行业资金流和概念资金流"""
    all_flows = {}  # name -> {净额, 流入, 流出, 涨跌幅, 来源}

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

    # 用板块涨跌做补充
    try:
        spots = ak.stock_sector_spot()
        for _, row in spots.iterrows():
            name = str(row.get('板块名称', '')).strip()
            pct_spot = float(row.get('涨跌幅', 0) or 0)
            for plate, keywords in PLATE_KEYWORDS.items():
                if plate in all_flows:
                    continue
                for kw in keywords:
                    if kw in name:
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
