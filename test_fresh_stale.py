#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_fresh_stale.py —— A3.2：fresh / stale / source / 请求代次一致性

覆盖用户指定的 A~D：
  A. 刷新意图必须同时到达 /api/margin 与 /api/weekly（fresh=1 真实传到 gather）
  B. stale=True 经过内存缓存（再次命中缓存）仍为 True（不得偷偷变 False）
  C. /api/weekly 响应真实携带 stale / dataAsOf / lastSuccessfulUpdate / degraded
  D. 一次成功的上游刷新后 stale 正确恢复为 False

运行：python test_fresh_stale.py
"""
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

pass_n = 0
fail_n = 0


def ok(c, name, extra=None):
    global pass_n, fail_n
    if c:
        pass_n += 1
        print('  ✅ ' + name)
    else:
        fail_n += 1
        print('  ❌ ' + name + (f' → {extra}' if extra is not None else ''))


# ---------------------------------------------------------------- 隔离沙箱
SANDBOX = tempfile.mkdtemp(prefix='ma_fresh_stale_')
server.CACHE_DIR = SANDBOX


def clear_all():
    server.MEM.clear()
    server.MEM_META.clear()
    server.MEM_STALE.clear()
    server.reset_upstream()


clear_all()

FAKE_AVAIL = [("20261002", "http://x/20261002_mtall.pdf"),
              ("20260925", "http://x/20260925_mtall.pdf")]
# 注意：avail 的 day 是 ymd（与真实 server 一致）；fetch_one_day 返回 row 的
# date 字段这里也用 ymd，确保 gather 的 `day == r["date"]` 匹配逻辑可对照。
FAKE_ROWS = [
    {"code": "4519", "date": "20261002", "buy": 100, "sell": 10, "ratio": 10.0, "name": "X"},
    {"code": "4519", "date": "20260925", "buy": 90, "sell": 9, "ratio": 10.0, "name": "X"},
]


def fake_fetch(code, day, url, fresh=False):
    for r in FAKE_ROWS:
        if r["date"] == day:
            return dict(r)
    return None


def idx_ok(fresh=False, limit=15):
    # 上游成功：返回可用索引，无错误
    return list(FAKE_AVAIL), {"gen": 1, "fetched": True, "reused": False,
                              "age": 0.0, "error": None, "stale": False}


def idx_fail(fresh=False, limit=15):
    # 上游失败：保留旧 avail（server 真实行为：失败时 _upstream["avail"] 不变），
    # 但 err 置位 → stale=True。这样失败刷新会返回旧缓存 + stale=True。
    return list(FAKE_AVAIL), {"gen": 0, "fetched": True, "reused": False,
                              "age": 0.0, "error": "boom", "stale": True}


# ================================================================ A
print('\n===== A：fresh 同时到达 margin / weekly =====')
clear_all()
captured = {"calls": []}
orig_gather = server.gather
orig_bwr = server.weekly.build_weekly_response


def spy_gather(code, days=20, fresh=False):
    captured["calls"].append((code, days, fresh))
    return ([], {"gen": 1, "fetched": True, "reused": False,
                "age": 0.0, "error": None, "stale": False})


server.gather = spy_gather
server.weekly.build_weekly_response = (
    lambda code, rows, fresh=False, cache_dir=None, as_of=None, upstream=None: {
        "ok": True, "code": code, "rows": rows,
        "stale": bool(upstream and upstream.get("stale")), "upstream": upstream})

try:
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{port}/'

    def get(path):
        with urlopen(base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    m1 = get('api/margin?code=4519&fresh=1')
    w1 = get('api/weekly?code=4519&fresh=1')
    m0 = get('api/margin?code=4519')
    w0 = get('api/weekly?code=4519')
    calls = captured["calls"]
    margin_fresh = [c for c in calls if c[1] in (15, 20) and c[2] is True]
    weekly_fresh = [c for c in calls if c[1] == 40 and c[2] is True]
    margin_plain = [c for c in calls if c[1] in (15, 20) and c[2] is False]
    weekly_plain = [c for c in calls if c[1] == 40 and c[2] is False]

    ok(len(margin_fresh) >= 1, 'margin 路由以 fresh=True 调用 gather', margin_fresh)
    ok(len(weekly_fresh) >= 1, 'weekly 路由以 fresh=True 调用 gather（刷新意图到达週次）', weekly_fresh)
    ok(len(margin_plain) >= 1, 'margin 普通请求以 fresh=False 调用 gather', margin_plain)
    ok(len(weekly_plain) >= 1, 'weekly 普通请求以 fresh=False 调用 gather', weekly_plain)
    ok(m1.get('ok') is True and w1.get('ok') is True,
       'fresh=1 下 margin 与 weekly 均成功响应')
finally:
    try:
        httpd.shutdown()
    except Exception:
        pass
    server.gather = orig_gather
    server.weekly.build_weekly_response = orig_bwr

# ================================================================ B / D
print('\n===== B：stale=True 经过内存缓存仍为 True（D 续：成功刷新后恢复） =====')
clear_all()
server.upstream_index = idx_ok
server.fetch_one_day = fake_fetch

# 1) 成功刷新一次：建立缓存，stale=False
rows1, up1 = server.gather("4519", 20, fresh=True)
ok(up1["stale"] is False, '首次成功刷新 stale=False', up1)
ok(server.MEM_STALE.get("4519:20") is False, 'MEM_STALE 记录为 False')
ok(rows1 and len(rows1) == 2, '成功刷新返回数据', len(rows1) if rows1 else 0)

# 2) 上游失败刷新：返回旧缓存 + stale=True，并写回 MEM_STALE=True
server.upstream_index = idx_fail
rows2, up2 = server.gather("4519", 20, fresh=True)
ok(up2["stale"] is True, '上游失败刷新 stale=True（降级缓存）', up2)
ok(server.MEM_STALE.get("4519:20") is True, 'MEM_STALE 翻转为 True')

# 3) 普通请求命中同一缓存：不得偷偷变回 stale=False
rows3, up3 = server.gather("4519", 20, fresh=False)
ok(up3["stale"] is True, '缓存命中后 stale 仍为 True（不伪装最新）', up3)
ok(rows3[0] == rows1[0], '缓存命中返回原缓存数据')

# 4) D：成功刷新恢复 stale
server.upstream_index = idx_ok
rows4, up4 = server.gather("4519", 20, fresh=True)
ok(up4["stale"] is False, '成功刷新后 stale 恢复为 False（D）', up4)
ok(server.MEM_STALE.get("4519:20") is False, 'MEM_STALE 复位为 False（D）')
rows5, up5 = server.gather("4519", 20, fresh=False)
ok(up5["stale"] is False, '恢复后缓存命中 stale=False（D）', up5)

# ================================================================ C
print('\n===== C：/api/weekly 响应携带 stale/dataAsOf/lastSuccessfulUpdate/degraded =====')
clear_all()
orig_fgw = server.weekly.fetch_ganan_weekly
server.weekly.fetch_ganan_weekly = lambda code, cache_dir, fresh=False: ([], {})

stale_up = {"gen": 2, "fetched": True, "reused": False, "age": 0.0,
            "error": "boom", "stale": True}
resp_s = server.weekly.build_weekly_response("4519", [], upstream=stale_up)
ok(resp_s.get("stale") is True, 'stale=True 透传到响应', resp_s.get('stale'))
ok(resp_s.get("degraded") is True, 'degraded=True（降级）', resp_s.get('degraded'))
ok(resp_s.get("lastSuccessfulUpdate") is None,
   'stale 时 lastSuccessfulUpdate=None（未更新）', resp_s.get('lastSuccessfulUpdate'))
ok(resp_s.get("freshServed") is True, 'freshServed 反映本轮抓取', resp_s.get('freshServed'))
ok("upstream" in resp_s and resp_s["upstream"]["stale"] is True,
   'upstream 子字段含 stale')
ok("dataAsOf" in resp_s, '响应含 dataAsOf 字段（数据所属日期）')

ok_up = {"gen": 3, "fetched": True, "reused": False, "age": 0.0,
         "error": None, "stale": False}
resp_o = server.weekly.build_weekly_response("4519", [], upstream=ok_up)
ok(resp_o.get("stale") is False, 'stale=False 透传', resp_o.get('stale'))
ok(resp_o.get("degraded") is False, '非降级 degraded=False', resp_o.get('degraded'))
ok(resp_o.get("lastSuccessfulUpdate") is not None,
   '非 stale 时 lastSuccessfulUpdate 有值', resp_o.get('lastSuccessfulUpdate'))

server.weekly.fetch_ganan_weekly = orig_fgw

# ================================================================ D（A3.2.5 §5）
print('\n===== D：MEM / MEM_STALE 一致性（并发 / 淘汰 / 隔离） =====')

# ---- A. 缓存命中时 stale=true 必须保持 ----
clear_all()
server.upstream_index = idx_ok
server.fetch_one_day = fake_fetch
server.gather('4519', 20, fresh=True)                  # 成功 → False
server.upstream_index = idx_fail
server.gather('4519', 20, fresh=True)                  # 上游失败 → True
ok(server.MEM_STALE.get('4519:20') is True, 'D-A1：上游失败后 MEM_STALE=True')
_r, up_hit = server.gather('4519', 20, fresh=False)    # 命中缓存
ok(up_hit['stale'] is True, 'D-A2：缓存命中时 stale=True 保持（不伪装最新）', up_hit)
ok(server.MEM_STALE.get('4519:20') is True, 'D-A3：命中后 MEM_STALE 仍为 True')

# ---- B. 有效更新后 stale=false ----
server.upstream_index = idx_ok
_r, up_ok = server.gather('4519', 20, fresh=True)
ok(up_ok['stale'] is False, 'D-B1：成功刷新后 stale=False', up_ok)
ok(server.MEM_STALE.get('4519:20') is False, 'D-B2：MEM_STALE 复位为 False')
_r, up_hit2 = server.gather('4519', 20, fresh=False)
ok(up_hit2['stale'] is False, 'D-B3：缓存命中 stale=False')

# ---- C. 旧请求不能覆盖新请求的状态（generation 守卫 + stale 原子写入） ----
clear_all()
key = '4519:20'
ok(server.mem_set(key, [{'date': '20261002'}], generation=10, stale=False) is True,
   'D-C1：新代写入成功（数据 + stale=False 原子写入）')
ok(server.MEM_STALE.get(key) is False, 'D-C2：新代写入后 MEM_STALE=False')
rejected = server.mem_set(key, [{'date': '20260925'}], generation=5, stale=True)
ok(rejected is False, 'D-C3：旧代写入被拒绝', rejected)
ok(server.MEM_STALE.get(key) is False,
   'D-C4：旧请求未能把 stale 覆盖为 True（状态未被旧请求污染）',
   server.MEM_STALE.get(key))
ok(server.MEM[key] == [{'date': '20261002'}], 'D-C5：旧请求未能覆盖数据')

# ---- D. 不同股票之间 stale 不串线 ----
clear_all()
server.upstream_index = idx_ok
server.gather('6920', 20, fresh=True)                  # 6920 成功
ok(server.MEM_STALE.get('6920:20') is False, 'D-D1：6920 成功 → stale=False')
server.upstream_index = idx_fail
server.gather('4519', 20, fresh=True)                  # 4519 失败
ok(server.MEM_STALE.get('4519:20') is True, 'D-D2：4519 失败 → stale=True')
ok(server.MEM_STALE.get('6920:20') is False,
   'D-D3：6920 不受 4519 失败影响（stale 不串线）',
   (server.MEM_STALE.get('6920:20'), server.MEM_STALE.get('4519:20')))
_r, up6920 = server.gather('6920', 20, fresh=False)
ok(up6920['stale'] is False, 'D-D4：6920 缓存命中仍为 False')

# ---- E. 缓存清空 / 淘汰后，MEM_STALE 不留下错误状态 ----
clear_all()
server.upstream_index = idx_ok
# 人为制造「数据已淘汰、但 stale 残留」的不同步状态
server.MEM_STALE['4519:20'] = True
server.MEM.pop('4519:20', None)
ok('4519:20' not in server.MEM, 'D-E1：模拟缓存淘汰（MEM 无数据）')
_r, up_miss = server.gather('4519', 20, fresh=False)
ok(up_miss['stale'] is False,
   'D-E2：缓存未命中时不得沿用残留 stale=True（重新走上游并如实返回）', up_miss)
ok(server.MEM_STALE.get('4519:20') is False, 'D-E3：重新成功后 MEM_STALE 被正确覆写')
clear_all()
ok(len(server.MEM_STALE) == 0, 'D-E4：缓存清空后 MEM_STALE 一并清空，无残留')

# ---------------------------------------------------------------- 汇总
print('\n' + '=' * 60)
print(f'断言：{pass_n} 通过 / {fail_n} 失败')
if fail_n:
    print('\n❌ 未通过')
    sys.exit(1)
print('\n✅ 全部通过')
