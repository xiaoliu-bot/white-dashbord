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
import json, time, os, datetime, socket, threading, urllib.request
import akshare as ak
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

import datetime as _dt

def _call_with_timeout(fn, timeout, default=None, label=""):
    """在子线程执行 fn，超时未返回则按失败处理，避免 AKShare/网络调用卡死整个流水线。"""
    box = {'v': default}
    def _t():
        try:
            box['v'] = fn()
        except Exception as e:
            print(f"  · {label} 异常: {str(e)[:80]}")
    th = threading.Thread(target=_t, daemon=True)
    th.start(); th.join(timeout)
    if th.is_alive():
        print(f"  · {label} 超时({timeout}s)，跳过")
        return default
    return box['v']

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

# === 聚宽 JQData（优先源，凭据走环境变量，绝不硬编码） ===
def jq_auth():
    """登录聚宽 JQData，返回 jq 模块；未配置/失败返回 None。"""
    user = os.environ.get('JQ_USER'); pwd = os.environ.get('JQ_PASSWORD')
    if not (user and pwd):
        print("  · 未配置 JQ_USER/JQ_PASSWORD，跳过聚宽")
        return None
    try:
        try:
            import jqdatasdk as jq
        except ImportError:
            # 兜底：运行时自装，避免 workflow 未 pre-install 时直接跳过
            import subprocess, sys
            print("  · 运行时安装 jqdatasdk ...")
            subprocess.run([sys.executable, "-m", "pip", "install", "jqdatasdk", "-q"], check=False)
            import jqdatasdk as jq
        jq.auth(user, pwd)
        print("  ✅ 聚宽登录成功")
        return jq
    except Exception as e:
        print(f"  · 聚宽登录失败: {str(e)[:80]}")
        return None

def fetch_indices_jq(jq):
    """聚宽优先提供的 A 股指数实时行情（get_price 1m 最新一根）。
    纳指/恒生科技不在聚宽，由腾讯兜底；这里只取上证（看板展示项）。"""
    spec = {'000001': ('上证指数', '000001.XSHG')}
    out = {}
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for key, (name, code) in spec.items():
        try:
            df = _call_with_timeout(
                lambda c=code: jq.get_price(c, end_date=now_str, count=1,
                                            frequency='1m', fields=['close', 'pre_close']),
                20, None, f"聚宽{name}")
            if df is not None and not getattr(df, 'empty', True) and len(df):
                row = df.iloc[-1]
                price = float(row['close']); prev = float(row['pre_close'])
                pct = round((price - prev) / prev * 100, 2) if prev else 0
                out[key] = {'name': name, 'price': round(price, 2),
                            'chg': round(price - prev, 2), 'pct': pct}
                print(f"  ✅ 聚宽 {name}: {price} ({pct}%)")
        except Exception as e:
            print(f"  · 聚宽 {name} 解析失败: {str(e)[:80]}")
    return out

# === 黄金 ===
def fetch_usdcny():
    """实时美元兑人民币（免费接口，带超时与兜底）。"""
    def _go():
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        rate = float(d['rates']['CNY'])
        if 6.0 < rate < 8.0:   # 合理区间校验，避免脏数据
            return rate
        raise ValueError(f"汇率异常 {rate}")
    return _call_with_timeout(_go, 12, 7.2, "美元兑人民币")


def fetch_gold(prev_gold=None):
    """国际金价(XAU/USD) + 实时汇率换算的国内金价；涨跌幅与上一次快照比较。"""
    def _go():
        req = urllib.request.Request(
            "https://api.gold-api.com/price/XAU",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    d = _call_with_timeout(_go, 12, None, "Gold-API")
    if not d:
        return {'usd': 0, 'usd_pct': 0, 'cny': 0, 'cny_pct': 0, 'fx': 0}
    try:
        price = float(d['price'])
    except (KeyError, TypeError, ValueError):
        return {'usd': 0, 'usd_pct': 0, 'cny': 0, 'cny_pct': 0, 'fx': 0}
    fx = fetch_usdcny()
    cny = round(price / 31.1035 * fx, 2)
    usd_pct = 0.0
    if prev_gold and prev_gold.get('usd'):
        usd_pct = round((price - prev_gold['usd']) / prev_gold['usd'] * 100, 2)
    print(f"  ✅ 黄金: {price} USD/oz | 汇率 {fx} | {cny} 元/克 | 涨跌 {usd_pct}%")
    return {'usd': round(price, 2), 'usd_pct': usd_pct,
            'cny': cny, 'cny_pct': round(usd_pct, 2), 'fx': round(fx, 4)}

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

    df_ind = _call_with_timeout(lambda: ak.stock_fund_flow_industry(symbol="即时"), 25, None, "行业资金流")
    if df_ind is not None:
        try:
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
            print(f"  ❌ 行业资金流解析: {e}")

    df_con = _call_with_timeout(lambda: ak.stock_fund_flow_concept(symbol="即时"), 25, None, "概念资金流")
    if df_con is not None:
        try:
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
            print(f"  ❌ 概念资金流解析: {e}")

    # 用东财板块涨跌做补充：Tushare 已命中但缺涨跌幅(pct)的板块补 pct；完全缺失的板块估值 net
    spots = _call_with_timeout(lambda: ak.stock_sector_spot(), 25, None, "板块涨跌")
    if spots is not None:
        try:
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
            print(f"  ❌ 板块涨跌解析: {e}")

    return all_flows

# === 中信期货 股指期货多空单（CFFEX 前20会员持仓）===
def _fetch_cffex_csv_day(day):
    """从中金所官网直抓某交易日持仓排名 CSV（HTTP，免费权威源）。
    返回 {IF:{label,long,short,net}, ...}；单个 {SYM}_1.csv 内含 成交量/持买/持卖 三榜，
    按会员简称匹配「中信期货」并跨合约(IF2608/2609/2612...)汇总。"""
    import csv, io
    import urllib.request
    targets = {'IF': '沪深300', 'IC': '中证500', 'IH': '上证50', 'IM': '中证1000'}
    ym, dd = day[:6], day[6:]
    result = {}
    for sym, label in targets.items():
        url = f"http://www.cffex.com.cn/sj/ccpm/{ym}/{dd}/{sym}_1.csv"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as resp:
                _data = resp.read()
                try:
                    raw = _data.decode('gb18030')
                except Exception:
                    raw = _data.decode('utf-8-sig', errors='replace')
        except Exception as e:
            print(f"  · 中信期指 {sym} {day} 抓取失败: {e}")
            continue
        rows = list(csv.reader(io.StringIO(raw)))
        if len(rows) < 3:
            continue
        # 列序：0交易日 1合约 2排名 | 3成交量会员 4成交量 5增减 | 6持买会员 7持买量 8增减 | 9持卖会员 10持卖量 11增减
        long_v = short_v = 0
        for r in rows[2:]:
            if len(r) <= 11:
                continue
            if '中信期货' in (r[6] or ''):
                try: long_v += int(float(r[7] or 0))
                except: pass
            if '中信期货' in (r[9] or ''):
                try: short_v += int(float(r[10] or 0))
                except: pass
        if long_v or short_v:
            result[sym] = {'label': label, 'long': long_v, 'short': short_v, 'net': long_v - short_v}
    return result


def fetch_citic_futures():
    """中信期货在股指期货(IF/IC/IH/IM)的多空单（前20会员持仓，跨合约汇总）。
    来源：中金所官网每日持仓排名 CSV（http，约 16:30 发布），best-effort。
    返回 {date, contracts:{IF:{label,long,short,net},...}, total:{long,short,net}}。"""
    try:
        from datetime import date as _d, timedelta as _td
        # 取最近若干交易日，直到取到已发布的中金所持仓排名（当日约 16:30 后发布）
        candidate_days = []
        for i in range(0, 4):
            d = _d.today() - _td(days=i)
            if d.weekday() < 5:
                candidate_days.append(d.strftime('%Y%m%d'))
        if not candidate_days:
            return {}
        for day in candidate_days:
            print(f"  · 中信期指：取中金所排名 {day} (HTTP CSV)")
            out = _call_with_timeout(lambda: _fetch_cffex_csv_day(day), 25, {}, f"中信期指 {day}")
            if out:
                total = {'long': 0, 'short': 0, 'net': 0}
                for s, v in out.items():
                    total['long'] += v['long']; total['short'] += v['short']; total['net'] += v['net']
                print(f"  ✅ 中信期指多空 {day}: {list(out.keys())} 总净仓 {total['net']} 手")
                return {'date': day, 'contracts': out, 'total': total}
        print("  · 中信期指：近几个交易日均未取到中信持仓")
        return {}
    except Exception as e:
        print(f"  ❌ 中信期指多空: {e}")
        return {}

def main():
    socket.setdefaulttimeout(30)   # 兜底：任何网络调用卡死都在 30s 内失败，避免整条流水线挂起
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
    idxStale = False
    indices = fetch_indices()
    if not indices:
        indices = prev.get('indices', {})
        if indices:
            idxStale = True
            print("  · 指数接口失败，沿用上次快照")
    # 聚宽优先覆盖 A 股指数（纳指/恒生科技聚宽无，走腾讯兜底；不新增沪深300/创业板）
    try:
        _jq = _call_with_timeout(jq_auth, 25, None, "聚宽登录")
        if _jq:
            _jq_idx = _call_with_timeout(lambda: fetch_indices_jq(_jq), 30, {}, "聚宽指数")
            for _k, _v in (_jq_idx or {}).items():
                if _k in indices and _v:
                    indices[_k] = _v
            _cov = [k for k in (_jq_idx or {}) if k in indices]
            if _cov:
                print(f"  · 聚宽覆盖指数: {_cov}")
    except Exception as e:
        print(f"  · 聚宽索引异常: {e}")

    print("[2/4] 黄金...")
    goldStale = False
    gold = fetch_gold(prev.get('gold'))
    if not gold.get('usd') and prev.get('gold', {}).get('usd'):
        gold = prev['gold']; goldStale = True
        print("  · 黄金接口失败，沿用上次快照")

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
    plateStale = False
    if all(p.get('net', 0) == 0 for p in plateFlows) and prev.get('plateFlows'):
        plateFlows = prev['plateFlows']
        plateStale = True
        print("  · 板块全为 0，沿用上次快照")
    print()

    print("[4/4] 中信期指多空...")
    citicStale = False
    citic = fetch_citic_futures()
    if not citic:
        citic = prev.get('citic', {})
        if citic:
            citicStale = True
            print("  · 中信期指沿用上次快照")

    data = {
        'updated': today,
        'time': now_iso,
        'indices': indices,
        'gold': gold,
        'plateFlows': plateFlows,
        'citic': citic,
        'stale': {'indices': idxStale, 'gold': goldStale,
                  'plateFlows': plateStale, 'citic': citicStale},
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
