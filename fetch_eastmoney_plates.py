#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_eastmoney_plates.py
从东方财富 push2 接口抓取行业/概念板块资金流（真实 主力/中单/小单 拆分），
按「智瞰」看板的 10 个持仓板块匹配，输出 api/plate_em.json，并推送回 white-dashbord 仓库。

为什么需要它：
- GitHub Actions 海外 runner 访问东财被 502 拒绝（地缘封锁），CI 里东财自动跳过，
  板块主力/散户拆分退化为 AKShare 的 55%/28% 估算。
- 本机在中国大陆（北京 IP），可直连东财拿到真实主力/散户数据。
- 看板 index.html 会拉取 api/plate_em.json，用真实拆分覆盖 AKShare 估算值。

运行（用户本机，需 Python 3.8+，建议用 Windows 任务计划程序每 5 分钟跑一次）：
  python fetch_eastmoney_plates.py

PAT（仅推送用）：优先读环境变量 GH_PAT，否则读同目录 em_config.json 的 {"pat":"..."}。
"""
import os
import sys
import json
import time
import base64
import socket
import ssl
import datetime
import urllib.request
import urllib.error

# ---------- 配置 ----------
REPO = "xiaoliu-bot/white-dashbord"
EM_UT = "b2884a393a59ad64002292a3e90d46a5"
EM_REFERER = "https://data.eastmoney.com/bkzj/hy.html"
TIMEOUT = 12
RETRIES = 3

# 看板 10 个持仓板块 -> 东财板块名匹配别名（精确/包含皆可命中）
TARGETS = {
    "芯片":       ["芯片", "存储芯片", "集成电路"],
    "半导体":     ["半导体"],
    "细分化工":   ["化学制品", "化学原料", "农用化工", "细分化工"],
    "科创创业AI": ["AI眼镜", "人工智能", "科创AI"],
    "机器人":     ["机器人", "自动化设备", "人形机器人", "减速器"],
    "新能源电池": ["电池", "新能源电池", "蓄电池"],
    "锂矿":       ["锂矿", "锂电池概念", "盐湖提锂", "锂电"],
    "CPO":        ["共封装光学", "CPO", "光模块", "光通信"],
    "PCB":        ["PCB", "印制电路板", "电路板"],
    "创新药":     ["创新药", "化学制药", "医疗器械", "生物制品"],
}


def _ctx():
    # 用户本机走 TLS 拦截代理，关闭证书校验（与 git sslVerify=false 一致）
    return ssl._create_unverified_context()


def _num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _http_get(url, timeout=TIMEOUT, retries=RETRIES):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": EM_REFERER,
                "Accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5)
    raise last


def fetch_em_boards(t):
    """t=2 行业, t=3 概念。返回统一结构的板块列表。"""
    fs = "m%3A90%2Bt%3A%d" % t
    url = ("https://push2.eastmoney.com/api/qt/clist/get?fid=f62&po=1&pz=2000&pn=1"
           "&np=1&fltt=2&invt=2&ut=%s&fs=%s&fields=f12,f14,f3,f62,f66,f72,f78,f84"
           % (EM_UT, fs))
    txt = _http_get(url)
    data = json.loads(txt)
    diff = (data.get("data") or {}).get("diff") or []
    out = []
    for row in diff:
        out.append({
            "name":   row.get("f14"),
            "pct":    _num(row.get("f3")),
            "net":    _num(row.get("f62")),   # 主力净流入净额（超大单+大单）
            "xlarge": _num(row.get("f66")),   # 超大单
            "large":  _num(row.get("f72")),   # 大单
            "mid":    _num(row.get("f78")),   # 中单
            "small":  _num(row.get("f84")),   # 小单
        })
    return out


def build_plates():
    boards = fetch_em_boards(2) + fetch_em_boards(3)
    print("东财返回板块数: %d" % len(boards))
    used = set()
    plates = {}
    for target, aliases in TARGETS.items():
        hit = None
        for b in boards:
            if id(b) in used:
                continue
            n = b.get("name") or ""
            for a in aliases:
                if a and (a == n or a in n or n in a):
                    hit = b
                    break
            if hit:
                break
        if not hit:  # 兜底：直接用 target 名匹配
            for b in boards:
                if id(b) in used:
                    continue
                n = b.get("name") or ""
                if target == n or target in n or n in target:
                    hit = b
                    break
        if not hit:
            print("  x 未匹配: %s" % target)
            continue
        used.add(id(hit))
        # 总净流入 = 主力(f62) + 中单(f78) + 小单(f84)，与 data.json 的 主力+大户+散户=net 一致
        total = hit["net"] + hit["mid"] + hit["small"]
        plates[target] = {
            "name":  target,
            "net":   int(round(total)),
            "主力": int(round(hit["net"])),
            "大户": int(round(hit["mid"])),
            "散户": int(round(hit["small"])),
            "pct":  round(hit["pct"], 2),
            "emName": hit["name"],
        }
        print("  OK %s <- 东财[%s]  主力=%.2f亿 大户=%.2f亿 散户=%.2f亿"
              % (target, hit["name"], hit["net"] / 1e8, hit["mid"] / 1e8, hit["small"] / 1e8))
    return plates


def write_local(plates, updated):
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "api", "plate_em.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "plates": plates}, f, ensure_ascii=False, indent=2)
    print("已写出 %s (%d 个板块)" % (out, len(plates)))
    return out


def get_pat():
    pat = os.environ.get("GH_PAT")
    if pat:
        return pat
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = os.path.join(here, "em_config.json")
    if os.path.exists(cfg):
        try:
            with open(cfg, encoding="utf-8") as f:
                return json.load(f).get("pat")
        except Exception:  # noqa: BLE001
            pass
    return None


def push_github(path, pat):
    if not pat:
        print("警告: 未找到 PAT（环境变量 GH_PAT 或 em_config.json），跳过推送。")
        print("      可手动: git add api/plate_em.json && git commit -m '东财叠加' && git push")
        return False
    url = "https://api.github.com/repos/%s/contents/api/plate_em.json" % REPO
    headers = {
        "Authorization": "Bearer " + pat,
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
    }
    sha = None
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=_ctx()) as r:
            sha = json.loads(r.read().decode("utf-8")).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print("  取 sha 失败: %s" % e)
    except Exception as e:  # noqa: BLE001
        print("  取 sha 失败: %s" % e)
    with open(path, "rb") as f:
        content = base64.b64encode(f.read()).decode("ascii")
    body = {"message": "chore: 东财板块真实拆分叠加 (plate_em.json)", "content": content}
    if sha:
        body["sha"] = sha
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ctx()) as r:
            print("OK 已推送 api/plate_em.json 到 %s (HTTP %s)" % (REPO, r.status))
        return True
    except Exception as e:  # noqa: BLE001
        print("推送失败: %s" % e)
        print("      可手动: git add api/plate_em.json && git commit -m '东财叠加' && git push")
        return False


def main():
    socket.setdefaulttimeout(TIMEOUT + 5)
    try:
        plates = build_plates()
    except Exception as e:  # noqa: BLE001
        print("东财抓取失败: %s" % e)
        sys.exit(1)
    if not plates:
        print("未匹配到任何板块，退出")
        sys.exit(1)
    updated = datetime.datetime.now().astimezone().isoformat()
    path = write_local(plates, updated)
    push_github(path, get_pat())


if __name__ == "__main__":
    main()
