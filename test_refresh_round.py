"""test_refresh_round.py —— refresh 上游请求次数回归（issue 12）

背景：一次用户主动 refresh（run(true)）在前端最多触发 3 次 /api/margin
      （a=0 → a=1 → a=2 的 fallback/重试链）。旧实现每次 fresh=1 都重新抓
      JPX 索引页并重解析全部 PDF，于是「点一次刷新」= 3 × 昂贵刷新
      （实测单次 ~117s，最坏 350s+）。

本文件覆盖用户指定的 A~D：
  A. 模拟 run(true)：a=0 无数据 → fallback 到 a=1
     ⇒ 上游 index fetch 只执行 1 次
  B. fallback 到 a=2（前两次都无数据）
     ⇒ 仍然只 fresh / 抓上游 1 次
  C. 普通 run(false)：行为不变（数据一致、不额外打上游、命中进程缓存零开销）
  D. fresh 第一次失败：必须明确报错 / 降级，不能把旧数据静默伪装成 fresh

运行：python test_refresh_round.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

pass_n = 0
fail_n = 0


def ok(cond, name, extra=None):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print('  ✅ ' + name)
    else:
        fail_n += 1
        print('  ❌ ' + name + (f' → {extra}' if extra is not None else ''))


# ---------------------------------------------------------------- 隔离沙箱
REAL_CACHE = server.CACHE_DIR
SANDBOX = tempfile.mkdtemp(prefix='ma_refresh_test_')
server.CACHE_DIR = SANDBOX


def restore():
    server.CACHE_DIR = REAL_CACHE


def fresh_sandbox(days):
    """清空进程缓存 / 上游状态 / 磁盘缓存，并建立 days 个假 PDF。"""
    server.MEM.clear()
    server.MEM_META.clear(); server.MEM_STALE.clear()
    server.reset_upstream()
    for f in os.listdir(SANDBOX):
        os.unlink(os.path.join(SANDBOX, f))
    for d in days:
        p = os.path.join(SANDBOX, f'{d}_mtall.pdf')
        with open(p, 'wb') as f:
            f.write(b'%PDF-1.4\n' + b'z' * 6000 + b'\n%%EOF\n')
    return [(d, f'http://unused/{d}.pdf') for d in days]


def fake_parse(records):
    """records: {day: rec|None} —— 控制每天解析成功与否。"""
    def _p(code, path):
        day = os.path.basename(path).split('_')[0]
        rec = records.get(day)
        if not rec:
            return None, '该 PDF 中未找到此代码'
        out = dict(rec)
        out['code'] = code
        out['name'] = 'テスト'
        return out, None
    return _p


def rec(day, buy):
    iso = f'{day[:4]}-{day[4:6]}-{day[6:]}'
    return {'sell': 1000, 'buy': buy, 'ratio': round(buy / 1000, 2),
            'buyListed': 1.0, 'sellListed': 0.1, 'date': iso}


DAYS = ['20261005', '20261002', '20261001']      # 索引里的 3 个公表日（倒序）

# ================================================================ A
print('=== A. run(true)：a=0 无数据 → fallback a=1，上游只抓 1 次 ===')
if True:
    avail = fresh_sandbox(DAYS)
    # a=0：服务端「PDF 解析中」→ 全部返回空（模拟首次访问还没解析完）
    server.list_available = lambda limit=15: list(avail)
    state = {'phase': 0}
    records = {
        DAYS[0]: rec(DAYS[0], 100000),
        DAYS[1]: rec(DAYS[1], 90000),
        DAYS[2]: rec(DAYS[2], 80000),
    }
    real_parse = server.parse_pdf

    def parse_a(code, path):
        if state['phase'] == 0:
            return None, '解析中'          # a=0 拿不到数据 → 触发 fallback
        return fake_parse(records)(code, path)
    server.parse_pdf = parse_a
    try:
        # --- a=0：fresh=1，无数据 ---
        rows0, up0 = server.gather('7974', 15, fresh=True)
        ok(rows0 == [], 'A a=0 无数据（触发 fallback）', rows0)
        ok(up0 and up0['fetched'] is True, 'A a=0 真的抓了上游', up0)
        fetches_after_a0 = server.UPSTREAM_STATS['indexFetches']

        # --- a=1：fresh=1（旧前端行为，服务端必须自己兜住） ---
        state['phase'] = 1
        rows1, up1 = server.gather('7974', 15, fresh=True)
        ok(len(rows1) == 3, 'A a=1 拿到数据', len(rows1))
        ok(up1 and up1['fetched'] is False, 'A a=1 复用 a=0 的索引（未再抓）', up1)
        ok(server.UPSTREAM_STATS['indexFetches'] == fetches_after_a0 == 1,
           'A 整轮上游抓取次数 = 1', server.UPSTREAM_STATS['indexFetches'])

        # --- 修正后的前端：a=1 / a=2 用 fresh=0，同样只能抓 1 次 ---
        server.MEM.clear(); server.MEM_META.clear(); server.MEM_STALE.clear()
        rows1b, up1b = server.gather('7974', 15, fresh=False)
        ok(len(rows1b) == 3, 'A 修正前端 a=1（fresh=0）也拿到数据', len(rows1b))
        rows2b, up2b = server.gather('7974', 15, fresh=False)
        ok(len(rows2b) == 3, 'A 修正前端 a=2（fresh=0）也拿到数据', len(rows2b))
        ok(server.UPSTREAM_STATS['indexFetches'] == 1,
           'A 修正前端整轮上游抓取次数 = 1', server.UPSTREAM_STATS['indexFetches'])

        # --- 并发：两个线程同时 fresh=1，也只能抓 1 次 ---
        server.reset_upstream()
        server.MEM.clear(); server.MEM_META.clear(); server.MEM_STALE.clear()
        state['phase'] = 1
        res = {}

        def w(tag):
            res[tag] = server.gather('7974', 15, fresh=True)
        ts = [threading.Thread(target=w, args=(f't{i}',)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        ok(len(res) == 4 and all(len(r[0]) == 3 for r in res.values()),
           'A 并发 4 × fresh=1 全部拿到数据', {k: len(v[0]) for k, v in res.items()})
        ok(server.UPSTREAM_STATS['indexFetches'] == 1,
           'A 并发 4 × fresh=1 上游只抓 1 次（single-flight）',
           server.UPSTREAM_STATS['indexFetches'])
    finally:
        server.parse_pdf = real_parse

# ================================================================ B
print('\n=== B. fallback 到 a=2：仍然只抓 1 次上游 ===')
if True:
    avail = fresh_sandbox(DAYS)
    server.list_available = lambda limit=15: list(avail)
    records = {d: rec(d, 100000 - i * 1000) for i, d in enumerate(DAYS)}
    real_parse = server.parse_pdf
    state = {'n': 0}          # 前两次 gather 返回空，第三次才有数据

    def parse_b(code, path):
        if state['n'] < 2:
            return None, '解析中'
        return fake_parse(records)(code, path)
    server.parse_pdf = parse_b
    try:
        got = []
        ups = []
        for a in range(3):                     # a=0 / a=1 / a=2
            r, u = server.gather('7974', 15, fresh=True)   # 旧前端：3 次都 fresh=1
            got.append(len(r))
            ups.append(u)
            state['n'] += 1
            if r:
                break
        ok(got[0] == 0 and got[1] == 0, 'B 前两次无数据（fallback 链生效）', got)
        ok(got[-1] == 3, 'B 第三次拿到数据', got)
        ok(sum(1 for u in ups if u and u['fetched']) == 1,
           'B 三次请求里只有 1 次真的抓了上游',
           [u and u['fetched'] for u in ups])
        ok(server.UPSTREAM_STATS['indexFetches'] == 1,
           'B 上游抓取总数 = 1', server.UPSTREAM_STATS['indexFetches'])
        # 修正前端（a=0 fresh / a=1,2 非 fresh）同样只允许 1 次
        server.reset_upstream(); server.MEM.clear(); server.MEM_META.clear(); server.MEM_STALE.clear()
        state['n'] = 0
        got2 = []
        for a in range(3):
            r, u = server.gather('7974', 15, fresh=(a == 0))
            got2.append(len(r))
            state['n'] += 1
            if r:
                break
        ok(sum(got2) > 0, 'B 修正前端仍能 fallback 成功', got2)
        ok(server.UPSTREAM_STATS['indexFetches'] == 1,
           'B 修正前端上游抓取总数 = 1', server.UPSTREAM_STATS['indexFetches'])
    finally:
        server.parse_pdf = real_parse

# ================================================================ C
print('\n=== C. run(false)：行为不变 ===')
if True:
    avail = fresh_sandbox(DAYS)
    server.list_available = lambda limit=15: list(avail)
    records = {d: rec(d, 100000 - i * 1000) for i, d in enumerate(DAYS)}
    real_parse = server.parse_pdf
    server.parse_pdf = fake_parse(records)
    try:
        r1, u1 = server.gather('7974', 15, fresh=False)
        ok(len(r1) == 3, 'C run(false) 首次拿到数据', len(r1))
        f_after_first = server.UPSTREAM_STATS['indexFetches']
        ok(f_after_first == 1, 'C 首次（无索引缓存）抓 1 次上游', f_after_first)

        # 连续两次普通查询：不再打上游（10 分钟窗口内复用）
        r2, u2 = server.gather('7974', 15, fresh=False)
        r3, u3 = server.gather('7974', 15, fresh=False)
        ok(r2 == r1 and r3 == r1, 'C 后续查询结果与首次一致（行为不变）')
        # ★ A3.2（issue B）：缓存命中不再返回 None，而是返回一个
        #   upstream_meta（fetched=False / reused=True），以便 stale 状态
        #   能随缓存一起透传、不偷偷变 False。本断言的目的是验证
        #   「缓存命中完全不触碰上游」—— 现在用 fetched=False + reused=True 表达。
        ok(u2 is not None and u2.get('fetched') is False and u2.get('reused') is True
           and u3 is not None and u3.get('fetched') is False and u3.get('reused') is True,
           'C 后续查询命中进程缓存 → 零上游开销（fetched=False, reused=True，stale 沿用）', (u2, u3))
        ok(server.UPSTREAM_STATS['indexFetches'] == f_after_first,
           'C 后续查询未新增上游抓取', server.UPSTREAM_STATS['indexFetches'])

        # 换一只股票：仍走同一份索引，不额外抓上游
        r4, u4 = server.gather('6920', 15, fresh=False)
        ok(len(r4) == 3, 'C 换股票也拿到数据', len(r4))
        ok(server.UPSTREAM_STATS['indexFetches'] == f_after_first,
           'C 换股票不额外抓上游', server.UPSTREAM_STATS['indexFetches'])

        # 日期 fallback 语义不变：最新日缺失时仍用前一公表日
        server.MEM.clear(); server.MEM_META.clear(); server.MEM_STALE.clear()
        for f in os.listdir(SANDBOX):
            if f.startswith('7974_'):
                os.unlink(os.path.join(SANDBOX, f))   # 去掉 JSON 缓存，强制真解析
        server.parse_pdf = fake_parse({DAYS[1]: rec(DAYS[1], 90000),
                                       DAYS[2]: rec(DAYS[2], 80000)})
        r5, _ = server.gather('7974', 15, fresh=False)
        ok(len(r5) == 2 and r5[0]['date'] == '2026-10-01',
           'C 最新日缺失 → 仍回退到前一公表日', [x['date'] for x in r5])
        server.parse_pdf = fake_parse(records)

        # warm cache 的 fresh=1：不得重解析没变的 PDF（这是 117s 的根因）
        server.parse_pdf = fake_parse(records)
        server.MEM.clear(); server.MEM_META.clear(); server.MEM_STALE.clear()
        server.gather('7974', 15, fresh=False)   # 把 3 天的 JSON 缓存都补齐
        server.MEM.clear(); server.MEM_META.clear(); server.MEM_STALE.clear()
        server.reset_upstream()
        p_before = server.UPSTREAM_STATS['parses']
        h_before = server.UPSTREAM_STATS['cacheHits']
        r6, u6 = server.gather('7974', 15, fresh=True)
        ok(len(r6) == 3, 'C warm-cache fresh=1 仍拿到数据', len(r6))
        ok(server.UPSTREAM_STATS['parses'] == p_before,
           'C warm-cache fresh=1 未重新解析 PDF（指纹一致 → 复用）',
           (p_before, server.UPSTREAM_STATS['parses']))
        ok(server.UPSTREAM_STATS['cacheHits'] - h_before == 3,
           'C warm-cache fresh=1 命中 3 次 JSON 缓存',
           server.UPSTREAM_STATS['cacheHits'] - h_before)

        # 反例：PDF 真的变了（指纹不同）→ 必须重新解析
        with open(os.path.join(SANDBOX, f'{DAYS[0]}_mtall.pdf'), 'wb') as f:
            f.write(b'%PDF-1.4\n' + b'y' * 9000 + b'\n%%EOF\n')
        server.MEM.clear(); server.MEM_META.clear(); server.MEM_STALE.clear()
        p_before2 = server.UPSTREAM_STATS['parses']
        server.gather('7974', 15, fresh=True)
        ok(server.UPSTREAM_STATS['parses'] - p_before2 == 1,
           'C PDF 变化 → 只重解析变化的那一日',
           server.UPSTREAM_STATS['parses'] - p_before2)
    finally:
        server.parse_pdf = real_parse

# ================================================================ D
print('\n=== D. fresh 首次失败：必须明确报错 / 降级，不得伪装成 fresh ===')
if True:
    # D1：无任何缓存 + 上游失败 → 逻辑层必须给出 stale / error，且不静默成功
    fresh_sandbox(DAYS)
    real_list = server.list_available
    server.list_available = lambda limit=15: (_ for _ in ()).throw(
        OSError('JPX 索引页连接超时'))
    try:
        rows, up = server.gather('7974', 15, fresh=True)
        ok(rows == [], 'D1 上游失败 → 无数据', rows)
        ok(up and up['error'], 'D1 upstream.error 有值', up)
        ok(up and up['stale'] is True, 'D1 标记为 stale（降级）', up)
        ok(up and up['fetched'] is True, 'D1 确实尝试过抓取', up)
        ok(server.UPSTREAM_STATS['indexErrors'] == 1, 'D1 计入 indexErrors',
           server.UPSTREAM_STATS['indexErrors'])
        # 负缓存：紧接着的失败重试不得把延迟放大
        f0 = server.UPSTREAM_STATS['indexFetches']
        server.gather('7974', 15, fresh=True)
        ok(server.UPSTREAM_STATS['indexFetches'] == f0,
           'D1 失败负缓存内不重复抓上游', server.UPSTREAM_STATS['indexFetches'])
    finally:
        server.list_available = real_list

    # D2：HTTP 层端到端 —— fresh=1 且上游失败且无数据 → 502 + 明确 err
    fresh_sandbox(DAYS)
    server.list_available = lambda limit=15: (_ for _ in ()).throw(
        OSError('JPX 索引页 503'))
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        try:
            with urlopen(f'http://127.0.0.1:{port}/api/margin?code=7974&days=15&fresh=1',
                         timeout=30) as r:
                body = json.loads(r.read().decode())
            ok(False, 'D2 上游失败时应返回错误状态码', body)
        except HTTPError as e:
            body = json.loads(e.read().decode())
            ok(e.code == 502, 'D2 返回 502', e.code)
            ok(body.get('ok') is False, 'D2 ok=false', body.get('ok'))
            ok('JPX' in (body.get('err') or ''), 'D2 err 明确说明上游失败',
               body.get('err'))
            ok(body.get('freshServed') is False,
               'D2 freshServed=false（不伪装成 fresh）', body.get('freshServed'))
            ok(body.get('stale') is True, 'D2 stale=true', body.get('stale'))
            ok((body.get('upstream') or {}).get('error'),
               'D2 回传 upstream.error', (body.get('upstream') or {}).get('error'))
    finally:
        httpd.shutdown()
        httpd.server_close()

    # D3：有旧索引 + 旧数据 → 允许降级返回，但必须标注 stale，不得声称 fresh
    avail3 = fresh_sandbox(DAYS)
    server.list_available = lambda limit=15: list(avail3)      # 先恢复上游
    records = {d: rec(d, 100000 - i * 1000) for i, d in enumerate(DAYS)}
    real_parse = server.parse_pdf
    server.parse_pdf = fake_parse(records)
    try:
        warm_rows, _ = server.gather('7974', 15, fresh=False)   # 先把索引/缓存建好
        ok(len(warm_rows) == 3, 'D3 预热成功', len(warm_rows))
        # 现在让上游开始失败，并把 TTL 逼过窗口（模拟「过了一段时间再刷新」）
        server._upstream['ts'] -= 9999
        server.list_available = lambda limit=15: (_ for _ in ()).throw(
            OSError('JPX 不可达'))
        server.MEM.clear(); server.MEM_META.clear(); server.MEM_STALE.clear()
        rows, up = server.gather('7974', 15, fresh=True)
        ok(len(rows) == 3, 'D3 降级后仍有数据可用', len(rows))
        ok(up and up['stale'] is True, 'D3 明确标记 stale', up)
        ok(up and up['error'], 'D3 携带 error 说明', up)

        # HTTP 层：降级数据必须 stale=true / freshServed=false
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.H)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urlopen(f'http://127.0.0.1:{port}/api/margin?code=7974&days=15&fresh=1',
                         timeout=30) as r:
                body = json.loads(r.read().decode())
            ok(body.get('ok') is True, 'D3 降级数据仍可返回', body.get('ok'))
            ok(body.get('count') == 3, 'D3 降级数据条数正确', body.get('count'))
            ok(body.get('stale') is True, 'D3 响应 stale=true', body.get('stale'))
            ok(body.get('freshServed') is False,
               'D3 freshServed=false（关键：不伪装成 fresh）',
               body.get('freshServed'))
            ok((body.get('upstream') or {}).get('error'),
               'D3 响应回传 upstream.error')
        finally:
            httpd.shutdown()
            httpd.server_close()

        # D4：上游恢复 → 必须重新变回 fresh（不永久卡在 stale）
        server._upstream['ts'] -= 9999
        server.list_available = lambda limit=15: list(
            [(d, f'http://unused/{d}.pdf') for d in DAYS])
        server.MEM.clear(); server.MEM_META.clear(); server.MEM_STALE.clear()
        rows, up = server.gather('7974', 15, fresh=True)
        ok(up and up['stale'] is False and up['fetched'] is True,
           'D4 上游恢复 → 重新成功刷新', up)
        ok(up and up['error'] is None, 'D4 error 已清除', up)
    finally:
        server.parse_pdf = real_parse

# ---------------------------------------------------------------- 清理
restore()
shutil.rmtree(SANDBOX, ignore_errors=True)

print(f'\n结果：{pass_n} 通过，{fail_n} 失败')
sys.exit(1 if fail_n else 0)
