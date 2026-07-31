#!/usr/bin/env python3
"""
每日收盘数据抓取 → api/data.json + api/history/YYYY-MM-DD.json
数据源（所有对外 HTTP 请求统一走 _http_get：随机 UA 伪装 + 按 host 限频 + 指数退避 + 短缓存）：
数据源优先级（板块数据优先从 Tushare 获取）：
  【指数】1) Tushare pro（用户 token，best-effort，限频时跳过）→ 2) 腾讯 qt.gtimg.cn（CI 稳定主用）→ 3) AKShare 兜底
  【板块资金流】1) Tushare 优先：pro.plate_fund_flow（含涨跌幅+净额，需积分≥2000）→ pro.moneyflow_industry（主力净流入）→ pro.moneyflow_concept（概念主力净流入）
    命中即保留，best-effort，限频/积分不足时整体跳过；2) AKShare 东财兜底仅补充 Tushare 未命中的持仓板块
  【黄金】Gold-API.com（国际金价，无需 key）
  （板块资金流字段：net 主力/净额净流入，pct 涨跌幅，source 标记数据来源 Tushare / 行业 / 概念 / 板块涨跌）
Tushare token 通过环境变量 TUSHARE_TOKEN 注入（建议配置为仓库 Secrets，勿硬编码）。
"""
import json, time, os, re, datetime, socket, threading, random, math, urllib.request
import warnings
from urllib.parse import urlparse
import ssl
try:
    import akshare as ak
except Exception:
    ak = None
try:
    import pandas as pd
except Exception:
    pd = None
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

# === 统一伪装 + 限频 + 退避 HTTP 请求器 ===
# 目标：所有对外 HTTP 请求都走这里，按元宝对新浪财经的建议配置，
# 保障免费接口「稳定可用」——浏览器伪装、按 host 限速、指数退避、短缓存。
_RATE = {}            # host -> 上次请求时间戳
_RATE_LOCK = threading.Lock()
_CACHE = {}           # url -> (ts, text)
_CACHE_TTL = 60       # 同 URL 默认 60s 内复用（看板 5 分钟刷新，足够兜底）

# 浏览器 UA 池：随机轮转，避免单一 UA 被识别为脚本爬虫
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]
# 各 host 强制 Referer（新浪必须带 finance.sina.com.cn，否则 403/空）
_REFERERS = {
    "hq.sinajs.cn": "https://finance.sina.com.cn/",
    "qt.gtimg.cn": "https://finance.qq.com/",
    "www.cffex.com.cn": "http://www.cffex.com.cn/",
    "api.gold-api.com": "https://www.google.com/",
    "open.er-api.com": "https://www.google.com/",
    "push2.eastmoney.com": "https://data.eastmoney.com/bkzj/hy.html",
}
# 各 host 最小请求间隔（秒）：新浪按元宝建议 ≥1~2s（取 2s + 随机抖动）
_HOST_INTERVAL = {
    "hq.sinajs.cn": 2.0,
    "qt.gtimg.cn": 1.0,
    "www.cffex.com.cn": 1.0,
    "api.gold-api.com": 1.0,
    "open.er-api.com": 1.0,
    "push2.eastmoney.com": 1.5,
}

def _http_get(url, encoding='utf-8', timeout=15, retries=3, cache_ttl=None,
              verify_ssl=False, label=""):
    """统一的伪装 + 限频 + 指数退避 HTTP GET。
    - 伪装：随机浏览器 UA + 按 host 注入 Referer + 完整请求头；
    - 限频：按 host 最小间隔 + 随机抖动，杜绝突发并发触发 WAF/封禁；
    - 退避：403/429/空响应/超时按 2/4/8s 指数退避重试；
    - 缓存：cache_ttl 秒内同 URL 直接复用，减少重复拉取。
    返回 (text, ok)，失败返回 ("", False)。"""
    host = urlparse(url).netloc if '//' in url else url.split('/')[0]
    iv = _HOST_INTERVAL.get(host, 1.0)
    # —— 限频：确保与同 host 上次请求间隔 >= iv（含 0~1s 随机抖动）——
    with _RATE_LOCK:
        last = _RATE.get(host, 0.0)
        elapsed = time.time() - last
        wait = (iv + random.uniform(0, 1.0)) - elapsed
        if wait > 0:
            time.sleep(min(wait, 5.0))
        _RATE[host] = time.time()
    # —— 短缓存：避免同一次运行内重复拉取相同 URL ——
    ttl = cache_ttl if cache_ttl is not None else _CACHE_TTL
    if ttl and url in _CACHE:
        ts, txt = _CACHE[url]
        if time.time() - ts < ttl:
            return txt, True
    # —— 请求头伪装 ——
    headers = {
        "User-Agent": random.choice(_UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Referer": _REFERERS.get(host, "https://www.google.com/"),
    }
    ctx = None if verify_ssl else ssl._create_unverified_context()
    last_err = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                raw = r.read()
            try:
                txt = raw.decode(encoding)
            except Exception:
                txt = raw.decode('utf-8', errors='replace')
            if not txt or len(txt) < 4:
                last_err = "empty body"; raise ValueError("empty body")
            if ttl:
                _CACHE[url] = (time.time(), txt)
            return txt, True
        except Exception as e:
            last_err = str(e)
            if attempt < retries - 1:
                back = 2 ** (attempt + 1)
                print(f"  · {label} 请求失败({last_err[:48]}), {back}s 后退避重试 ({attempt+1}/{retries})")
                time.sleep(back)
    print(f"  · {label} 最终失败: {last_err[:60]}")
    return "", False

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
        import re
        codes = ','.join(v[1] for v in tencent.values())
        content, ok = _http_get(
            f"https://qt.gtimg.cn/q={codes}",
            encoding='gbk', timeout=10, retries=2, cache_ttl=60, label="腾讯指数")
        if not ok:
            return result
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

def fetch_indices_sina():
    """新浪财经 hq.sinajs.cn 兜底源（A股指数）。
    按元宝对新浪财经的建议配置：
      - Referer 强制带 https://finance.sina.com.cn/（否则 403/空）；
      - 随机浏览器 UA 伪装；
      - 按 host 最小间隔≥2s（含随机抖动），杜绝突发并发触发 WAF；
      - 批量一次拉全（list=sh000001,sh000300,sz399006），不拆单循环；
      - 403/429/空响应按指数退避重试。
    字段格式：var hq_str_xxx="名称,今开,昨收,当前,最高,最低,..." → 当前=parts[3], 昨收=parts[2]。
    （已用腾讯实时值交叉验证：sh000001 新浪 parts[3]=3867.0336 == 腾讯当前价 3867.03）"""
    sina = {
        '000001': ('上证指数', 'sh000001'),
        '000300': ('沪深300',  'sh000300'),
        '399006': ('创业板指',  'sz399006'),
    }
    codes = ','.join(v[1] for v in sina.values())
    url = f"https://hq.sinajs.cn/list={codes}"
    txt, ok = _http_get(url, encoding='gbk', timeout=15, retries=3,
                        cache_ttl=60, label="新浪指数")
    if not ok:
        return {}
    import re
    result = {}
    for line in txt.split(';'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        m = re.match(r'var hq_str_(\w+)="([^"]*)"', line)
        if not m:
            continue
        code = m.group(1)
        parts = m.group(2).split(',')
        if len(parts) < 4:
            continue
        for key, (name, scode) in sina.items():
            if scode != code:
                continue
            try:
                price = float(parts[3])
                prev = float(parts[2])
                pct = round((price - prev) / prev * 100, 2) if prev else 0
                result[key] = {'name': name, 'price': round(price, 2),
                               'chg': round(price - prev, 2), 'pct': pct}
                print(f"  ✅ 新浪 {name}: {price} ({pct}%)")
            except Exception as e:
                print(f"  · 新浪 {name} 解析失败: {e}")
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
def _usdcny_primary():
    txt, ok = _http_get("https://open.er-api.com/v6/latest/USD",
                        timeout=10, retries=3, cache_ttl=120, label="美元兑人民币")
    if not ok:
        return 0.0
    try:
        return float(json.loads(txt)['rates']['CNY'])
    except Exception:
        return 0.0

def _usdcny_backup():
    """备用源：jsDelivr 上的免费汇率 API（无需 key，CDN 稳定）。"""
    txt, ok = _http_get("https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
                        timeout=10, retries=3, cache_ttl=300, label="美元兑人民币(备用)")
    if not ok:
        return 0.0
    try:
        return float(json.loads(txt)['usd']['cny'])
    except Exception:
        return 0.0

def fetch_usdcny():
    """实时美元兑人民币：主源 open.er-api → 备用 jsDelivr 汇率 API → 兜底 7.2。"""
    for fn in (_usdcny_primary, _usdcny_backup):
        try:
            rate = fn()
            if 6.0 < rate < 8.0:   # 合理区间校验，避免脏数据
                return rate
        except Exception:
            pass
    return 7.2


def fetch_gold_tencent():
    """腾讯 hf_GC 兜底源（伦敦金/XAU 现货，USD/oz）。
    v_hf_GC 形如 "4111.35,-1.18,4111.10,4111.50,...伦敦金"，逗号分隔，[0]=现价。"""
    txt, ok = _http_get("https://qt.gtimg.cn/q=hf_GC",
                        encoding='gbk', timeout=10, retries=2, cache_ttl=120, label="腾讯黄金")
    if not ok:
        return 0.0
    import re
    m = re.search(r'v_hf_GC="([^"]+)"', txt)
    if not m:
        return 0.0
    parts = m.group(1).split(',')
    try:
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0

def fetch_gold(prev_gold=None):
    """国际金价(XAU/USD) + 实时汇率换算的国内金价；涨跌幅与上一次快照比较。
    主源 gold-api.com → 兜底 腾讯 hf_GC。"""
    price = 0.0
    txt, ok = _http_get("https://api.gold-api.com/price/XAU",
                        timeout=10, retries=3, cache_ttl=120, label="Gold-API")
    if ok:
        try:
            price = float(json.loads(txt)['price'])
        except Exception:
            price = 0.0
    if not price:
        price = fetch_gold_tencent()
        if price:
            print("  · 黄金改用腾讯兜底源")
    if not price:
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

# === 东财 push2 板块资金流（主源，免 token / 免积分）===
# 相比 Tushare(无权限恒空) 与 AKShare(依赖 pandas、接口易变)：
#   ① 纯 HTTP + JSON，无第三方库依赖，快且稳定；
#   ② 直接给出 主力/中单/小单 的真实分解，不必再按 55%/28% 估算。
# 字段：f12 代码 | f14 名称 | f3 涨跌幅% | f62 主力净额 | f66 超大单 | f72 大单 | f78 中单 | f84 小单（均为元）
_EM_UT = "b2884a393a59ad64002292a3e90d46a5"


def _em_fetch_boards(t, label):
    """拉东财板块资金流全量。t=2 行业板块，t=3 概念板块。返回 list[dict]，失败返回 []。"""
    rows, pn = [], 1
    while pn <= 4:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?"
               "fid=f62&po=1&pz=200&pn=%d&np=1&fltt=2&invt=2&ut=%s"
               "&fs=m%%3A90%%2Bt%%3A%d"
               "&fields=f12%%2Cf14%%2Cf3%%2Cf62%%2Cf66%%2Cf72%%2Cf78%%2Cf84"
               % (pn, _EM_UT, t))
        # 海外 runner 常被东财以 502 拒绝，单次尝试即可，不做昂贵重试
        txt, ok = _http_get(url, timeout=8, retries=1, cache_ttl=120,
                            label="%s第%d页" % (label, pn))
        if not ok:
            break
        try:
            data = (json.loads(txt) or {}).get('data') or {}
        except Exception as e:
            print("  · %s 解析失败: %s" % (label, str(e)[:50]))
            break
        diff = data.get('diff') or []
        if not diff:
            break
        rows.extend(diff)
        if len(rows) >= int(data.get('total') or 0):
            break
        pn += 1
    return rows


def _em_num(row, key):
    v = row.get(key)
    if v in (None, '', '-'):
        return 0
    try:
        return int(float(v))
    except Exception:
        return 0


def fetch_plate_data_eastmoney():
    """东财板块资金流（主源）。返回 {板块: {...}}。
    ⚠️ net 口径 = f62 主力净流入净额（元）。不用「超大+大+中+小」的总净额——
       该总和恒接近 0（买卖对等），做方向与气泡大小都没有参考价值。"""
    out = {}
    for t, src in ((2, '东财行业'), (3, '东财概念')):
        rows = _call_with_timeout(lambda _t=t, _s=src: _em_fetch_boards(_t, _s),
                                  45, [], src)
        if not rows:
            print("  · %s 无数据，跳过" % src)
            if t == 2:
                print("  · 东财 push2 不可达（GitHub 海外 runner 常被 502 拒绝），放弃东财、直接走兜底源")
                break
            continue
        for r in rows:
            name = str(r.get('f14') or '').strip()
            if not name:
                continue
            for plate, keywords in PLATE_KEYWORDS.items():
                if plate in out:
                    continue
                if not any(kw in name for kw in keywords):
                    continue
                zhu = _em_num(r, 'f62')      # 主力 = 超大单 + 大单
                try:
                    pct = round(float(r.get('f3') or 0), 2)
                except Exception:
                    pct = 0.0
                out[plate] = {
                    'name': plate, '行业名': name, 'code': str(r.get('f12') or ''),
                    'net': zhu,
                    'inflow': _em_num(r, 'f66'), 'outflow': _em_num(r, 'f72'),
                    '超大单': _em_num(r, 'f66'), '大单': _em_num(r, 'f72'),
                    '主力': zhu, '大户': _em_num(r, 'f78'), '散户': _em_num(r, 'f84'),
                    'pct': pct, 'source': src,
                }
                break
        print("  ✅ %s: %d 条，累计命中 %d/%d 个持仓板块"
              % (src, len(rows), len(out), len(PLATE_KEYWORDS)))
    return out


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
    """板块资金流，按优先级降级：
    1) 东财 push2（免 token，含主力/中单/小单真实分解）——主源；
    2) Tushare（需积分权限，无权限时秒级跳过）；
    3) AKShare 行业/概念资金流；
    4) 东财板块涨跌（仅补 pct / 估算 net）。
    东财已覆盖全部持仓板块时直接返回，跳过后面三次慢调用（每次 25s 超时），
    可显著缩短 CI 时长并降低整条流水线的失败概率。"""
    all_flows = fetch_plate_data_eastmoney()  # 主源：东财 push2
    if len(all_flows) >= len(PLATE_KEYWORDS):
        print("  · 东财已覆盖全部持仓板块，跳过 Tushare/AKShare 兜底")
        return all_flows

    _tus = fetch_plate_data_tushare(os.environ.get('TUSHARE_TOKEN'))
    for _k, _v in (_tus or {}).items():
        all_flows.setdefault(_k, _v)
    print(f"  · Tushare 补充后命中: {list(all_flows.keys())}")

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
    targets = {'IF': '沪深300', 'IC': '中证500', 'IH': '上证50', 'IM': '中证1000'}
    ym, dd = day[:6], day[6:]
    result = {}
    for sym, label in targets.items():
        url = f"http://www.cffex.com.cn/sj/ccpm/{ym}/{dd}/{sym}_1.csv"
        raw, ok = _http_get(url, encoding='gb18030', timeout=12, retries=3,
                            cache_ttl=300, label=f"中信期指{sym}")
        if not ok:
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


def fetch_citic_futures(prev_citic=None):
    """中信期货在股指期货(IF/IC/IH/IM)的多空单（前20会员持仓，跨合约汇总）。
    来源：中金所官网每日持仓排名 CSV（http，约 16:30 发布），best-effort。
    优先抓最新交易日；美国节点对中金所偶发超时，故抓不到时沿用上次快照(prev)，
    避免回退到更早日期。返回 {date, contracts:{IF:{label,long,short,net},...}, total:{...}}。"""
    try:
        from datetime import date as _d, timedelta as _td
        candidate_days = []
        for i in range(0, 3):
            d = _d.today() - _td(days=i)
            if d.weekday() < 5:
                candidate_days.append(d.strftime('%Y%m%d'))
        if not candidate_days:
            return prev_citic or {}
        # 1) 优先抓最新交易日（美国节点对中金所偶发超时，15s 即弃）
        for day in candidate_days[:1]:
            print(f"  · 中信期指：取中金所排名 {day} (HTTP CSV)")
            out = _call_with_timeout(lambda: _fetch_cffex_csv_day(day), 15, {}, f"中信期指 {day}")
            if out:
                total = {'long': 0, 'short': 0, 'net': 0}
                for s, v in out.items():
                    total['long'] += v['long']; total['short'] += v['short']; total['net'] += v['net']
                print(f"  ✅ 中信期指多空 {day}: {list(out.keys())} 总净仓 {total['net']} 手")
                return {'date': day, 'contracts': out, 'total': total}
        # 2) 抓不到 → 沿用上次快照（已含最近可用数据）
        if prev_citic and prev_citic.get('contracts'):
            print("  · 中信期指沿用上次快照")
            return prev_citic
        # 3) 无快照 → 试昨天
        for day in candidate_days[1:]:
            print(f"  · 中信期指：取中金所排名 {day} (HTTP CSV)")
            out = _call_with_timeout(lambda: _fetch_cffex_csv_day(day), 15, {}, f"中信期指 {day}")
            if out:
                total = {'long': 0, 'short': 0, 'net': 0}
                for s, v in out.items():
                    total['long'] += v['long']; total['short'] += v['short']; total['net'] += v['net']
                print(f"  ✅ 中信期指多空 {day}: {list(out.keys())} 总净仓 {total['net']} 手")
                return {'date': day, 'contracts': out, 'total': total}
        return {}
    except Exception as e:
        print(f"  ❌ 中信期指多空: {e}")
        return prev_citic or {}

# === 吸筹/出货 判断逻辑 ===
MAIN_COLS = ['今日主力净流入-净额', '主力净流入', '主力净流入-净额', '主力净买额', '主力净额']
RETAIL_COLS = ['今日小单净流入-净额', '小单净流入', '小单净流入-净额', '小单净买额', '小单净额']

def _try_cols(row, names, default=0):
    for n in names:
        v = row.get(n)
        if v not in (None, ''):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default

def load_history(days=5):
    """读取 api/history/ 下最近 days 个日快照，构建 板块名->[每日主力]（用于持续性加成）。

    ⚠️ 只认 YYYY-MM-DD.json。目录里还有 dates.json 这类索引文件，其 JSON 根是数组，
       误当日快照读会抛 "'list' object has no attribute 'get'"；
       且旧实现把 try 包在整个循环外，一个坏文件就会中断全部历史加载，
       导致 5 日持续性加成长期静默失效。现改为白名单 + 逐文件容错。"""
    hm = {}
    hist_dir = 'api/history'
    if not os.path.isdir(hist_dir):
        return hm
    try:
        cand = [f for f in os.listdir(hist_dir)
                if re.match(r'^\d{4}-\d{2}-\d{2}\.json$', f)]
        files = sorted(cand)[-days:]
    except Exception as e:
        print(f"  ⚠️ 历史目录读取失败: {e}")
        return hm
    for f in files:
        try:
            with open(os.path.join(hist_dir, f), encoding='utf-8') as fh:
                dat = json.load(fh)
            if not isinstance(dat, dict):
                continue
            for pf in (dat.get('plateFlows') or []):
                if not isinstance(pf, dict):
                    continue
                nm = pf.get('name')
                if nm:
                    hm.setdefault(nm, []).append({'主力': pf.get('主力', 0) or 0})
        except Exception as e:
            print(f"  ⚠️ 历史文件 {f} 跳过: {str(e)[:60]}")
    if hm:
        print(f"  · 历史累计: {len(files)} 个日快照 / {len(hm)} 个板块")
    return hm

def classify_plate(plate, history=None, flow_max=None):
    """
    板块吸筹/出货判断。打分（+吸筹 / -出货）：
      方向【严格看当日净额】：净流入→吸筹(红)，净流出→出货(绿)。history 只做强度加成，绝不反转方向。
      1) 主力方向（±70，金额 sqrt 动态归一）：当日净流入→+，净流出→-
      2) 主力/散户背离（±15）：主力进+散户退=典型吸筹；主力退+散户进=典型出货
      3) 价格上下文（±8）：流入未大涨=低位吸筹；已大涨仍流入=警惕追高；
                          上涨中主力流出=高位派发；下跌中主力流出=出货延续
      4) 持续性（±6）：近5日累计主力与当日同向→加强；反向→削弱（不反转方向）
    信号：当日净额>0→吸筹，<0→出货（所有板块非红即绿，无中性）。
    强度：按当批最大 |score| 的 0.6/0.3 动态分档（强/温和/弱），在 main() 里二次计算。
    """
    main = plate.get('主力', 0) or 0
    retail = plate.get('散户', 0) or 0
    pct = plate.get('pct', 0) or 0
    net = plate.get('net', 0) or 0             # 当日净额（净流入为正）—方向权威字段
    today_flow = net if net != 0 else main     # net 缺失时回退当日主力
    hist_flow = sum((h.get('主力', 0) or 0) for h in history) if history else 0

    score = 0.0
    reasons = []
    denom = flow_max if (flow_max and flow_max > 0) else 1.5e10
    mag = min(math.sqrt(abs(today_flow) / denom), 1.0) if denom > 0 else 0
    if today_flow > 0:
        score += 70 * mag
        reasons.append('当日净流入')
    elif today_flow < 0:
        score -= 70 * mag
        reasons.append('当日净流出')

    # 持续性：history 与当日同向加强、反向削弱（绝不反转方向）
    if hist_flow != 0 and today_flow != 0:
        if (hist_flow > 0) == (today_flow > 0):
            score += 6 * (1 if today_flow > 0 else -1)
            reasons.append('近5日持续%s' % ('吸筹' if today_flow > 0 else '出货'))
        else:
            score -= 4 * (1 if today_flow > 0 else -1)
            reasons.append('近5日与当日反向·警惕反转')

    if main > 0 and retail < 0:
        score += 15
        reasons.append('主力吸筹+散户割肉')
    elif main < 0 and retail > 0:
        score -= 15
        reasons.append('主力派发+散户接盘')
    elif main > 0 and retail > 0:
        score -= 4
        reasons.append('主力散户同进(偏追高)')
    elif main < 0 and retail < 0:
        score += 4
        reasons.append('主力散户同退(偏恐慌)')

    if main > 0 and pct <= 3:
        score += 8
        reasons.append('流入未大涨·低位吸筹')
    elif main > 0 and pct > 5:
        score -= 5
        reasons.append('已大涨仍在流入·警惕追高')
    elif main < 0 and pct > 3:
        score -= 8
        reasons.append('上涨中主力流出·高位派发')
    elif main < 0 and pct <= 0:
        score -= 4
        reasons.append('下跌中主力流出·出货延续')

    score = round(score, 1)
    # 方向严格看当日净额（不再用 history 累计翻方向）
    signal = '吸筹' if today_flow >= 0 else '出货'
    a = abs(score)
    strength = '强' if a >= 45 else ('温和' if a >= 20 else '弱')
    return {'signal': signal, 'strength': strength, 'score': score, 'reason': '；'.join(reasons)}


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
    indices = fetch_indices()                       # 主源：腾讯
    sina = fetch_indices_sina() or {}              # 兜底源：新浪（补缺）
    added = [k for k in sina if k not in indices]
    indices.update({k: v for k, v in sina.items() if k not in indices})
    if added:
        print(f"  · 新浪补全指数: {added}")
    if not indices:
        indices = prev.get('indices', {})
        if indices:
            idxStale = True
            print("  · 指数接口全部失败，沿用上次快照")
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
    history_map = load_history(5)   # 近5日累计主力，用于更稳的吸筹/出货判断
    PLATE_ORDER = ['芯片', '半导体', '细分化工', '科创创业AI', '机器人',
                   '新能源电池', '锂矿', 'CPO', 'PCB', '创新药']
    # 第一遍：收集各板块用于“方向”的 flow（近5日累计主力，无则用当日主力），求全局最大做动态归一
    specs = []
    for plate_name in PLATE_ORDER:
        d = plate_data.get(plate_name, {})
        net = d.get('net', 0)
        zhu = d.get('主力')
        if zhu is None: zhu = int(net * 0.55)
        da = d.get('大户')
        if da is None: da = int(net * 0.28)
        san = d.get('散户')
        if san is None: san = net - zhu - da
        hist = history_map.get(plate_name)
        flow = net if net != 0 else zhu   # 方向/归一都改用当日净额，不再用 history 累计
        specs.append({'name': plate_name, 'd': d, 'zhu': zhu, 'da': da,
                      'san': san, 'hist': hist, 'net': net, 'flow': flow})
    max_abs_flow = max((abs(s['flow']) for s in specs), default=0) or 1
    # 第二遍：先按动态归一打分（signal + score），强度稍后按当批最大 |score| 动态分档
    raw_cls = []
    for s in specs:
        cls = classify_plate({'主力': s['zhu'], '散户': s['san'], 'pct': s['d'].get('pct', 0), 'net': s['net']},
                             history=s['hist'], flow_max=max_abs_flow)
        raw_cls.append((s, cls))
    max_abs_score = max((abs(c['score']) for _, c in raw_cls), default=0) or 1
    for s, cls in raw_cls:
        a = abs(cls['score'])
        cls['strength'] = '强' if a >= 0.6 * max_abs_score else ('温和' if a >= 0.3 * max_abs_score else '弱')
        print(f"  {s['name']}: {s['d'].get('行业名','?')} | {s['d'].get('pct',0):+.2f}% | 净额:{(s['net']/1e8):+.2f}亿 | {cls['signal']}({cls['strength']}) [{s['d'].get('source','未知')}]")
        plateFlows.append({
            'name': s['name'],
            'code': '',
            'pct': s['d'].get('pct', 0),
            '行业名': s['d'].get('行业名', ''),
            '散户': s['san'], '大户': s['da'], '主力': s['zhu'], 'net': s['net'],
            'source': s['d'].get('source', '未知'),
            'signal': cls['signal'],
            'strength': cls['strength'],
            'score': cls['score'],
            'reason': cls['reason'],
        })
    plateStale = False
    if (not plateFlows or all(p.get('net', 0) == 0 for p in plateFlows)) and prev.get('plateFlows'):
        plateFlows = prev['plateFlows']
        plateStale = True
        print("  · 板块资金流抓取失败/为空，沿用上次快照")
    print()

    print("[4/4] 中信期指多空...")
    citicStale = False
    _prev_citic = prev.get('citic', {})
    citic = fetch_citic_futures(_prev_citic)
    if citic is _prev_citic and _prev_citic:
        citicStale = True
        print("  · 中信期指沿用上次快照")
    elif not citic:
        citic = _prev_citic

    # 兜底：若四大模块全部缺失，整体沿用上次快照，避免写出全空数据导致页面空白
    if not indices and not gold.get('usd') and not plateFlows and not citic:
        print("  ⚠️ 本次抓取全模块失败，整体沿用上次快照")
        return prev

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
