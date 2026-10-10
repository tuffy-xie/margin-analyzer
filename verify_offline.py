#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_offline.py —— A3.2.5 §2/§3/§4：离线确定性浏览器回归

与 verify_ui.py（在线 smoke）分离：本套件**完全不依赖公共代理/外网**。
所有数据源都用固定 fixture + Playwright route mock：
  · /api/margin  → JPX fixture（确定性、不访问 JPX）
  · /api/weekly  → 週次 fixture（含 fresh 与 stale 两个变体）
  · 外部代理      → Yahoo chart fixture（r.jina.ai / api.codetabs.com 全 mock）
  · Ganan        → 成功用函数替换 fixture；失败用 route abort

错误判定统一走 verify_errors.ErrorLedger（按场景归因，非文本全局排除）。

用法：
  python verify_offline.py                      正常离线回归（必须 0 致命）
  python verify_offline.py --inject-console     负向：注入非预期 console.error
  python verify_offline.py --inject-pageerror   负向：注入非预期 pageerror
  python verify_offline.py --inject-unrelated   负向：weekly abort 中同时制造无关 API 失败
负向模式下：本脚本**必须**把该错误判为致命；判出来 → exit 0（检测有效），
          被放过 → exit 1（说明过滤仍在吞错误）。
"""

import datetime
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, parse_qs, urlparse

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_errors import ErrorLedger, CheckLedger, NA, SKIP

URL = 'http://127.0.0.1:8848/'

# ---------------------------------------------------------------- 固定 fixture
# 全部为**确定性**数据，不依赖当天行情 / 公共代理是否可用。

MARGIN_LATEST = '2026-10-08'

# ★ A4.3 §1：首页禁止出现的内部评分泄漏模式（真实 DOM 层守死）。
#   A4.2 实测 4519 首页泄漏「方向スコア 30」。首页只允许定性表述，
#   评分数值一律保留在详细页「判定ロジック」折叠区。
HOME_SCORE_LEAK_PATTERNS = [
    (r'方向スコア\s*[-+]?\d+', '方向スコア裸数字'),
    (r'信用リスク\s*[-+]?\d+', '信用リスク裸数字'),
    (r'スコア\s*[:：]?\s*[-+]?\d+', 'スコア裸数字'),
    (r'[-+]?\d+\s*/\s*100', '/100 内部分数'),
]


def _weekdays_back(last_date, n):
    d = datetime.date.fromisoformat(last_date)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= datetime.timedelta(days=1)
    return list(reversed(out))


def build_margin_fixture(code='4519', days=20):
    """JPX 日次 fixture：确定性生成，buy/sell 缓慢上升（模拟温和増加）。"""
    dates = _weekdays_back(MARGIN_LATEST, days)
    rows = []
    for i, dt in enumerate(dates):
        buy = 2_150_000 + i * 4_000
        sell = 41_000 + i * 120
        d = datetime.date.fromisoformat(dt)
        rows.append({
            'code': code, 'sell': sell, 'buy': buy,
            'ratio': round(buy / sell, 2),
            'sellChg': 100, 'buyChg': 3_000,
            'sellListed': 0.1, 'buyListed': 3.4,
            'negSell': 23_000, 'stdSell': 18_000,
            'negBuy': 750_000, 'stdBuy': 1_400_000,
            'name': 'オフライン銘柄', 'date': dt,
            'shortMD': f'{d.month:02d}/{d.day:02d}',
        })
    return {
        'ok': True, 'code': code, 'name': rows[-1]['name'], 'rows': rows,
        'count': len(rows), 'latest': rows[-1]['date'], 'fresh': False,
        'freshServed': False, 'stale': False,
        'upstream': {'gen': 1, 'fetched': True, 'reused': False,
                     'age': 0.0, 'error': None, 'stale': False},
        'frequency': '日次（每営業日公表）', 'parserVersion': 4,
        'dataAsOf': rows[-1]['date'],
        'lastSuccessfulUpdate': '2026-10-08T16:05:00',
        'source': 'JPX 官方「銘柄別信用取引残高」（每交易日 16:00 公表）',
    }


def build_weekly_fixture(code='4519', stale=False):
    """週次 fixture。stale=True 表示上游刷新失败、返回的是降级旧缓存。"""
    to_date = MARGIN_LATEST
    return {
        'ok': True, 'code': code, 'coverageWeeks': 14,
        'rows': [], 'trends': {
            'w4': {'available': True, 'reason': None, 'weeksNeeded': 5, 'weeksPresent': 5,
                   'missingWeeks': [], 'fromDate': '2026-09-11', 'toDate': to_date,
                   'buyPct': 3.2, 'sellPct': -1.4, 'ratioFrom': 52.1, 'ratioTo': 53.9,
                   'corporateActionAffected': False},
            'w8': {'available': True, 'reason': None, 'weeksNeeded': 9, 'weeksPresent': 9,
                   'missingWeeks': [], 'fromDate': '2026-08-14', 'toDate': to_date,
                   'buyPct': 5.1, 'sellPct': -2.0, 'ratioFrom': 51.3, 'ratioTo': 53.9,
                   'corporateActionAffected': False},
            'w13': {'available': True, 'reason': None, 'weeksNeeded': 14, 'weeksPresent': 14,
                    'missingWeeks': [], 'fromDate': '2026-07-10', 'toDate': to_date,
                    'buyPct': 8.4, 'sellPct': -3.1, 'ratioFrom': 49.7, 'ratioTo': 53.9,
                    'corporateActionAffected': False},
        },
        'sources': {'ganan': {'weeks': 14, 'from': '2026-07-10', 'to': to_date},
                    'jpx': {'weeks': 14, 'from': '2026-07-10', 'to': to_date},
                    'mergedWeeks': 14, 'jpxWinsWeeks': 14, 'note': 'offline-fixture'},
        # ★ A3.2.5 §6：週次与日次的日期**可以不同**，但状态字段必须如实
        'dataAsOf': to_date,
        'lastSuccessfulUpdate': (None if stale else '2026-10-08T16:05:00'),
        'stale': stale,
        'degraded': stale,
        'freshServed': False,
        'caConfidence': 'none',
    }


def build_yahoo_fixture(days=400):
    """Yahoo chart fixture（engine.js fetchYahooBars 解析的形状）。"""
    end = datetime.date.fromisoformat(MARGIN_LATEST)
    ts, closes, vols, adj = [], [], [], []
    price = 4_800.0
    d = end - datetime.timedelta(days=int(days * 1.5))
    while len(ts) < days:
        if d.weekday() < 5:
            ts.append(int(datetime.datetime(d.year, d.month, d.day).timestamp()))
            price = round(price * 1.0007, 2)
            closes.append(price)
            adj.append(price)
            vols.append(1_200_000)
        d += datetime.timedelta(days=1)
    return json.dumps({
        'chart': {'result': [{
            'timestamp': ts,
            'indicators': {'quote': [{'close': closes, 'volume': vols}],
                           'adjclose': [{'adjclose': adj}]},
            'events': {},
        }], 'error': None}
    })


YAHOO_FIXTURE = build_yahoo_fixture()

# ★ A4.2 §1：PASS / FAIL / SKIP / N-A 四态分离，SKIP 与 N/A 不计入 PASS。
CKS = CheckLedger()


def scen(name):
    CKS.scen(name)
    print(f'\n=== {name} ===')


def check(cond, name, detail=None):
    """真断言：PASS 或 FAIL。**绝不用它记录 N/A**（那是 na() 的职责）。"""
    return CKS.check(cond, name, detail)


def na(name, reason):
    """N/A：无法评估。不计入 PASS，必须给出原因。"""
    return CKS.na(name, reason)


def skip(name, reason):
    """SKIP：故意不执行。不计入 PASS，必须给出原因。"""
    return CKS.skip(name, reason)


def find_chromium():
    cache = Path.home() / 'Library/Caches/ms-playwright'
    cands = []
    if cache.is_dir():
        for pat in ('chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell',
                    'chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium'):
            cands += sorted(cache.glob(pat), reverse=True)
    for c in cands:
        if c.exists():
            return str(c)
    return None


def read_home(page):
    return page.evaluate("() => { const p=document.getElementById('tab0');"
                         " return p ? p.innerText : ''; }")


def read_weekly(page):
    return page.evaluate(
        "() => { const e=document.getElementById('wkTrend');"
        " const f=document.getElementById('wkFoot');"
        " return (e?e.innerText:'') + '\\n' + (f?f.innerText:''); }")


def wait_badge(page, timeout=60000):
    page.wait_for_function(
        "() => { const b=document.getElementById('hBadge');"
        " return b && !b.textContent.includes('読み込み中'); }", timeout=timeout)


def wait_weekly(page, timeout=60000):
    page.wait_for_function(
        "() => { const e=document.getElementById('wkTrend'); if(!e) return false;"
        " const t=e.innerText;"
        " return t.includes('週次データ取得失敗') || t.includes('データ蓄積中')"
        " || t.includes('買残') || t.includes('比較停止'); }", timeout=timeout)


def search(page, code):
    page.fill('#inp', code)
    page.click('button.primary')


# ---------------------------------------------------------------- 主流程
def run(inject_mode=None):
    exe = find_chromium()
    ledger = ErrorLedger()
    captured = {'margin': [], 'weekly': []}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 1400})

        # ---- 事件接线：统一进账本（归因判定） ----
        page.on('pageerror', lambda e: ledger.add_pageerror(str(e)))
        page.on('requestfailed', lambda r: ledger.add_requestfailed(
            r.url, r.resource_type,
            (r.failure or '') if getattr(r, 'failure', None) else ''))

        def _on_console(msg):
            if msg.type != 'error':
                return
            try:
                loc = msg.location() or {}
                url = loc.get('url', '') or ''
            except Exception:
                url = ''
            ledger.add_console_error(msg.text, url)

        page.on('console', _on_console)

        # ---- 固定 fixture 路由（离线确定性核心） ----
        def _serve_margin(route):
            q = parse_qs(urlparse(route.request.url).query)
            code = (q.get('code', ['4519'])[0] or '4519').upper()
            captured['margin'].append(route.request.url)
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps(build_margin_fixture(code)))

        def _serve_weekly(route):
            q = parse_qs(urlparse(route.request.url).query)
            code = (q.get('code', ['4519'])[0] or '4519').upper()
            captured['weekly'].append(route.request.url)
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps(build_weekly_fixture(code)))

        def _serve_proxy(route):
            """外部代理统一 mock：按被代理的目标 URL 分流。"""
            u = route.request.url
            target = u
            if 'quest=' in u:
                try:
                    target = unquote(parse_qs(urlparse(u).query).get('quest', [''])[0])
                except Exception:
                    target = u
            if 'finance.yahoo' in target:
                route.fulfill(status=200, content_type='text/plain', body=YAHOO_FIXTURE)
            elif 'ganan-finance' in target:
                route.fulfill(status=200, content_type='text/plain',
                              body='<html><body>offline ganan fixture</body></html>')
            else:
                route.fulfill(status=200, content_type='text/plain', body='offline')

        page.route('**/api/margin*', _serve_margin)
        page.route('**/api/weekly*', _serve_weekly)
        page.route('**r.jina.ai/**', _serve_proxy)
        page.route('**api.codetabs.com/**', _serve_proxy)

        # ================= O0：基线渲染（零外网） =================
        scen('O0：离线基线渲染')
        # ★ page.goto 放在 begin_scenario **之前**：导航会中止上一页的在途请求，
        #   那些中止不属于本场景制造，不应污染本场景的归因判断。
        page.goto(URL, wait_until='domcontentloaded')
        ledger.begin_scenario('O0-baseline')
        search(page, '4519')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(800)
        home = read_home(page)
        wt = read_weekly(page)
        check(any(g in home for g in ('改善', '中立', '注意', '悪化', '判定不能')),
              'O0：离线 fixture 下 margin 正常给出评级', home[:80].replace('\n', ' '))
        check('4週' in wt and '8週' in wt and '13週' in wt,
              'O0：离线 fixture 下週次三行齐全', wt[:80].replace('\n', ' '))
        check('週次データ取得失敗' not in wt, 'O0：週次未失败')
        check(captured['margin'] and captured['weekly'],
              'O0：margin / weekly 均走了 mock（未打真实上游）',
              (len(captured['margin']), len(captured['weekly'])))
        ledger.end_scenario()

        # ================= O1：週次 abort 隔离（§3） =================
        scen('O1：週次 /api/weekly abort 隔离')
        # 预期请求：本场景明确 abort 的 /api/weekly
        page.goto(URL, wait_until='domcontentloaded')
        ledger.begin_scenario('O1-weekly-abort', expected_requests=['/api/weekly'])
        page.route('**/api/weekly*', lambda route: route.abort())
        search(page, '4519')
        wait_badge(page)
        page.wait_for_timeout(2500)
        wt = read_weekly(page)
        home = read_home(page)
        # A. weekly 区域正确显示失败
        check('週次データ取得失敗' in wt, 'O1-A：週次区域显示「週次データ取得失敗」',
              wt[:80].replace('\n', ' '))
        # B. margin 主页不受影响
        check(any(g in home for g in ('改善', '中立', '注意', '悪化', '判定不能')),
              'O1-B：週次失败时 margin 主页仍有评级（不受影响）',
              home[:80].replace('\n', ' '))
        check('読み込み中' not in home, 'O1-B：margin 无残留 loading')
        # C. 只有这次预期 abort 的错误被判为 expected
        exp = [e for e in ledger.expected_entries() if e['scenario'] == 'O1-weekly-abort']
        fatal_o1 = [e for e in ledger.fatal_entries() if e['scenario'] == 'O1-weekly-abort']
        check(len(fatal_o1) == 0,
              'O1-C：weekly abort 的错误被正确归因，无未确认错误',
              [(e['text'][:60], e['reason']) for e in fatal_o1])
        check(len(exp) >= 1 or len(ledger.failed_requests) >= 1,
              'O1-C：存在可归因的预期请求失败证据',
              [(e['status'], e['reason']) for e in exp])
        ledger.end_scenario()
        try:
            page.unroute('**/api/weekly*')
        except Exception:
            pass

        # ================= O2：哨兵（正对照，证明不过度抑制） =================
        scen('O2：abort 中注入无关错误必须被捕获')
        page.goto(URL, wait_until='domcontentloaded')
        ledger.begin_scenario('O2-sentinels',
                              sentinels=['NEG_SENTINEL_CONSOLE', 'NEG_SENTINEL_PAGEERROR'],
                              expected_requests=['/api/weekly'])
        page.route('**/api/weekly*', lambda route: route.abort())
        search(page, '4519')
        wait_badge(page)
        page.wait_for_timeout(1500)
        n0 = len(ledger.entries)
        page.evaluate("console.error('NEG_SENTINEL_CONSOLE')")
        page.evaluate("(function(){ setTimeout(function(){ throw new Error('NEG_SENTINEL_PAGEERROR'); }, 0); })()")
        page.wait_for_timeout(1500)
        new_entries = ledger.entries[n0:]
        check(any('NEG_SENTINEL_CONSOLE' in e['text'] for e in new_entries),
              'O2：无关 console.error 未被吞掉（已捕获）',
              [e['text'][:50] for e in new_entries])
        check(any('NEG_SENTINEL_PAGEERROR' in e['text'] for e in new_entries),
              'O2：pageerror 未被吞掉（已捕获）',
              [e['text'][:50] for e in new_entries])
        # 哨兵已注册 → 判为 expected（非致命），但**必须被记录**
        check(any(e['status'] == 'expected' and 'NEG_SENTINEL' in e['text'] for e in new_entries),
              'O2：哨兵被记录且因场景注册而归因为 expected')
        ledger.end_scenario()
        try:
            page.unroute('**/api/weekly*')
        except Exception:
            pass

        # ================= O3：Ganan 失败（§4D） =================
        scen('O3：Ganan 取得失败不残留 JPX')
        page.goto(URL, wait_until='domcontentloaded')
        # 本场景**明确制造** Ganan 失败：应用如实打印的失败日志属预期，
        # 注册哨兵使其可归因（这是"场景明确制造"，不是按文本全局排除）。
        ledger.begin_scenario('O3-ganan-fail', sentinels=['offline ganan down'])
        search(page, '4519')
        wait_badge(page)
        page.wait_for_timeout(600)
        # 让 Ganan 抓取失败（替换成抛错，确定性、不依赖外网）
        # 注意：Playwright 的 evaluate 若表达式求值结果是「函数」会自动调用它，
        # 因此必须用 IIFE 包一层并返回非函数值，避免刚赋值就被立刻执行。
        page.evaluate(
            "(function(){ window.MA.fetchMarginSeries = async function(){"
            " throw new Error('offline ganan down'); }; return true; })()")
        page.select_option('#srcSel', 'ganan')
        page.wait_for_timeout(500)
        search(page, '4519')
        page.wait_for_timeout(3000)
        src_label = page.evaluate("() => (document.getElementById('srcLabel')||{}).textContent || ''")
        status_txt = page.evaluate("() => (document.getElementById('status')||{}).innerText || ''")
        lastd = page.evaluate("() => window.__lastD ? window.__lastD.sourceMeta : null")
        check(src_label.strip() == '—' or 'JPX' not in src_label,
              'O3：Ganan 失败后数据源标签不是 JPX 官方', src_label)
        check(lastd is None or lastd.get('isOfficial') is not True,
              'O3：Ganan 失败后无残留 JPX 官方结论', lastd)
        check('Ganan' in status_txt,
              'O3：状态明确提示 Ganan 失败（而非伪装 JPX）', status_txt[:80])
        ledger.end_scenario()

        # ================= O4：竞态（快速切换 / 连续刷新） =================
        scen('O4：竞态（快速切换 + 连续刷新）')
        page.goto(URL, wait_until='domcontentloaded')
        # 竞态场景**故意**让在途请求被后续请求取代/中止，
        # 这类中止由本场景明确制造 → 登记为预期请求，避免误判。
        ledger.begin_scenario('O4-race',
                              expected_requests=['/api/margin', '/api/weekly'])
        search(page, '4519')
        page.wait_for_timeout(200)
        search(page, '6981')                 # 竞态触发：4519 在途请求被 6981 取代
        wait_badge(page)
        # ★ A4.3 冷启动确定性：竞态正确性由下方 code_now=='6981' 断言保证；
        #   週次区块在此处只做「最终稳定态」读取。冷启动时首个 /api/weekly 的
        #   route.fulfill 会略慢，4519 週次仍可能在 200ms 竞态窗口内未完成，
        #   导致 wait_weekly 等待一个已作废的 seq 而超时（测试时序问题，非应用缺陷）。
        #   故做一次稳定重搜：最后一次请求必然胜出（__runSeq 守卫），週次确定渲染。
        search(page, '6981')
        wait_weekly(page)
        page.wait_for_timeout(600)
        code_now = page.evaluate("() => window.__lastD ? window.__lastD.code : null")
        check(code_now == '6981', 'O4：快速切换后最终为最后请求的代码 6981', code_now)
        # 连续刷新（同一代码）
        page.click('button.wide-only')
        page.wait_for_timeout(300)
        page.click('button.wide-only')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(800)
        code_now2 = page.evaluate("() => window.__lastD ? window.__lastD.code : null")
        check(code_now2 == '6981', 'O4：连续刷新后最终稳定为 6981', code_now2)
        check('読み込み中' not in read_home(page), 'O4：无残留 loading')
        ledger.end_scenario()

        # ================= O5：数据状态（§6 stale 不伪装最新） =================
        scen('O5：stale 週次不得伪装成最新')
        page.goto(URL, wait_until='domcontentloaded')
        ledger.begin_scenario('O5-stale')
        # 直接以 stale fixture 渲染，验证 UI 如实展示降级
        stale_fx = build_weekly_fixture('4519', stale=True)
        page.evaluate("(function(){ renderWeeklyTrend(%s); })()" % json.dumps(stale_fx))
        page.wait_for_timeout(600)
        wt = read_weekly(page)
        check('降级缓存' in wt or '非最新' in wt,
              'O5：stale 週次显示降级提示（不伪装最新）', wt[:160].replace('\n', ' '))
        check(stale_fx['lastSuccessfulUpdate'] is None,
              'O5：stale fixture 的 lastSuccessfulUpdate 为 null（契约正确）')
        check(stale_fx['dataAsOf'] == MARGIN_LATEST,
              'O5：dataAsOf 仍如实给出数据所属日期')
        ledger.end_scenario()

        # ================= 负向注入（§4 A/B/C 端到端） =================
        injected_entry = None
        if inject_mode:
            print(f'\n=== 负向注入：{inject_mode} ===')
            page.goto(URL, wait_until='domcontentloaded')
            if inject_mode == 'unrelated':
                # §4C：模拟"週次 abort 场景"，/api/weekly 是本场景预期的；
                # 但同时制造一个**无关**请求失败 —— 它不得被一起忽略。
                ledger.begin_scenario('NEG-unrelated', expected_requests=['/api/weekly'])
            else:
                # A/B：不注册任何预期请求/哨兵 —— 注入的错误必须是"未确认"
                ledger.begin_scenario('NEG-' + inject_mode)
            search(page, '4519')
            wait_badge(page)
            page.wait_for_timeout(1000)
            n0 = len(ledger.entries)
            if inject_mode == 'console':
                page.evaluate("console.error('NEG_UNEXPECTED_CONSOLE_ZZZ')")
            elif inject_mode == 'pageerror':
                page.evaluate("(function(){ setTimeout(function(){ throw new Error('NEG_UNEXPECTED_PAGEERROR_ZZZ'); }, 0); })()")
            elif inject_mode == 'unrelated':
                page.route('**/api/weekly*', lambda route: route.abort())
                # 无关请求：用 route abort 造成真正的网络失败（404 不会触发）
                page.route('**/api/probe-unrelated*', lambda route: route.abort())
                page.evaluate("fetch('/api/probe-unrelated').catch(function(){});")
                search(page, '4519')
            page.wait_for_timeout(2500)
            new_entries = ledger.entries[n0:]
            if new_entries:
                injected_entry = new_entries[0]
            ledger.end_scenario()

        # ================= O6：A4.1 比較期間 / 期間株価騰落率 标签（DOM 级） =================
        scen('O6：A4.1 信用残比較期間 标签与引擎窗口一致')
        page.goto(URL, wait_until='domcontentloaded')
        ledger.begin_scenario('O6-cmp-label')
        search(page, '4519')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(800)
        home = read_home(page)
        # ① 两个新标签必须出现
        check('信用残比較期間' in home, 'O6：首页出现「信用残比較期間」标签', home[:160].replace('\n', ' '))
        check('期間株価騰落率' in home, 'O6：首页出现「期間株価騰落率」标签', home[:160].replace('\n', ' '))
        # ② UI 显示的 from/to 必须 == 引擎实际使用的 comparison.from/to
        ui_cmp = page.evaluate(
            "() => { const d = window.__lastD; if(!d) return null;"
            " const c = d.ind && d.ind.comparison; const q = d.ind && d.ind.quadrant;"
            " return c ? { from:c.from, to:c.to, periods:c.periods, priceChg:(q?q.priceChg:null), key:(q?q.key:null) } : null; }")
        check(ui_cmp is not None, 'O6：引擎 comparison 可读（__lastD.ind.comparison）', ui_cmp)
        if ui_cmp:
            from_md = f"{ui_cmp['from'][5:7]}/{ui_cmp['from'][8:10]}"
            to_md = f"{ui_cmp['to'][5:7]}/{ui_cmp['to'][8:10]}"
            check(from_md in home and to_md in home,
                  'O6：UI 显示的 from/to 与引擎 comparison 完全一致', (from_md, to_md, home[:160].replace('\n', ' ')))
            # ③ 当日涨跌绝不混入：UI 的利率符号应与引擎期間 priceChg 符号一致；
            #    若 priceChg 为 null（方向不可判定），UI 不得显示伪造百分比
            if ui_cmp['priceChg'] is not None:
                arrow = '↗' if ui_cmp['priceChg'] > 0.5 else ('↘' if ui_cmp['priceChg'] < -0.5 else '→')
                check(arrow in home, 'O6：UI 箭头方向与引擎期間 priceChg 一致', (arrow, ui_cmp['priceChg']))
            else:
                check('—' in home, 'O6：priceChg=null 时 UI 显示「—」而非伪造百分比', home[:160].replace('\n', ' '))
        check('データ不足' not in home, 'O6：正常 fixture 下不出现「データ不足」（未编造）', home[:160].replace('\n', ' '))
        ledger.end_scenario()

        # ================= O7：A4.1.1 股价日期溯源（真实 DOM 全链路） =================
        # 全链路：真实 pickPrice(对齐) → rows 组装 → comparisonWindow → renderHero → DOM
        # 通过覆盖 fetchMarginSeries / fetchYahooBars 注入受控 fixture（零外网）。
        scen('O7：A4.1.1 株価参照期間（真实 DOM 全链路）')
        MARGIN_JS = r"""(async (code) => ([
          {date:'2026-10-02', buy:1000000, sell:700000, ratio:1.43},
          {date:'2026-10-03', buy:990000,  sell:700000, ratio:1.41},
          {date:'2026-10-04', buy:980000,  sell:700000, ratio:1.40},
          {date:'2026-10-05', buy:970000,  sell:700000, ratio:1.39},
          {date:'2026-10-06', buy:960000,  sell:700000, ratio:1.37},
          {date:'2026-10-08', buy:950000,  sell:700000, ratio:1.36},
        ]))"""
        BARS_A = r"""(async (code, span) => ([
          {date:'2026-10-01', close:100, adjClose:100, vol:1000000},
          {date:'2026-10-07', close:108, adjClose:108, vol:1000000},
        ]))"""
        BARS_B = r"""(async (code, span) => ([
          {date:'2026-10-02', close:100, adjClose:100, vol:1000000},
          {date:'2026-10-03', close:101, adjClose:101, vol:1000000},
          {date:'2026-10-04', close:102, adjClose:102, vol:1000000},
          {date:'2026-10-05', close:103, adjClose:103, vol:1000000},
          {date:'2026-10-06', close:104, adjClose:104, vol:1000000},
          {date:'2026-10-08', close:108, adjClose:108, vol:1000000},
        ]))"""
        BARS_C = r"""(async (code, span) => ([
          {date:'2026-10-07', close:108, adjClose:108, vol:1000000},
        ]))"""

        def _o7(margin_js, bars_js, label, asserts_fn):
            ledger.begin_scenario('O7-' + label)
            try:
                page.goto(URL, wait_until='domcontentloaded')
                # 覆盖两只取数函数（在切源之前就位，避免 select 触发的首次 run 打到真网）
                page.evaluate("window.MA.fetchMarginSeries = " + margin_js)
                page.evaluate("window.MA.fetchYahooBars = " + bars_js)
                # 切到 ganan 源：run() 才会走 MA.fetchMarginSeries（official 源走的是
                # MA_OFFICIAL.fetchOfficialDaily，覆盖 fetchMarginSeries 对它无效）。
                page.select_option('#srcSel', 'ganan')
                search(page, '9999')
                wait_badge(page)
                page.wait_for_timeout(800)
                home = read_home(page)
                asserts_fn(home)
            except Exception as e:
                check(False, f'O7-{label} 异常', str(e)[:160])
            finally:
                ledger.end_scenario()

        # O7a：§2 固定反例（两端均回退）→ 显示真实参照期間 10/01～10/07 与 +8.0%
        def _assert_a(home):
            check('信用残比較期間' in home, 'O7a：出现「信用残比較期間」', home[:200].replace('\n', ' '))
            check('10/02' in home and '10/08' in home, 'O7a：信用残期間 = 10/02～10/08', home[:200].replace('\n', ' '))
            check('株価参照期間' in home, 'O7a：出现「株価参照期間」', home[:200].replace('\n', ' '))
            check('10/01' in home and '10/07' in home, 'O7a：株価参照期間 = 10/01～10/07（真实回退，非信用残日期）', home[:200].replace('\n', ' '))
            check('8%' in home, 'O7a：期間株価騰落率 = +8%', home[:200].replace('\n', ' '))
            check('データ不足' not in home, 'O7a：正常回退下不出现「データ不足」', home[:200].replace('\n', ' '))
        _o7(MARGIN_JS, BARS_A, 'a-fallback', _assert_a)

        # O7b：价格日 == 信用残日（无回退）→ 不显示额外提示
        def _assert_b(home):
            check('信用残比較期間' in home, 'O7b：出现「信用残比較期間」', home[:200].replace('\n', ' '))
            check('株価参照期間' not in home, 'O7b：价格日无回退 → 不显示「株価参照期間」额外提示', home[:200].replace('\n', ' '))
            check('8%' in home, 'O7b：期間株価騰落率仍显示 +8%', home[:200].replace('\n', ' '))
        _o7(MARGIN_JS, BARS_B, 'b-same', _assert_b)

        # O7c：起点价格日缺失 → 显示「データ不足」（绝不用信用残日期冒充）
        def _assert_c(home):
            check('信用残比較期間' in home, 'O7c：信用残期間仍显示', home[:200].replace('\n', ' '))
            check('株価参照期間' in home, 'O7c：出现「株価参照期間」行', home[:200].replace('\n', ' '))
            check('データ不足' in home, 'O7c：起点价格日缺失 → 株価参照期間显示「データ不足」', home[:200].replace('\n', ' '))
        _o7(MARGIN_JS, BARS_C, 'c-missing', _assert_c)

        # ================= O8：A4.3 首页不得泄漏内部评分数字 ==============
        # A4.2 实测 4519（grade=good）首页出现「方向スコア 30」。
        # 这里在**真实 DOM** 层守死：首页只允许定性表述，任何评分裸数字即失败。
        scen('O8：A4.3 首页无内部评分泄漏（真实 DOM）')
        ledger.begin_scenario('O8-score-leak')
        try:
            page.goto(URL, wait_until='domcontentloaded')
            search(page, '4519')
            wait_badge(page)
            page.wait_for_timeout(1200)
            home_txt = read_home(page)
            leaked = [(lbl, m.group(0))
                      for (pat, lbl) in HOME_SCORE_LEAK_PATTERNS
                      for m in [re.search(pat, home_txt)] if m]
            check(not leaked,
                  'O8：首页不出现任何内部评分裸数字（方向スコア / 信用リスク / /100）',
                  leaked[:3])
            check('方向スコア' not in home_txt,
                  'O8：首页不再出现「方向スコア」标签',
                  (re.search(r'.{0,30}方向スコア.{0,30}', home_txt) or ['(无)'])[0]
                  if '方向スコア' in home_txt else '(无)')
        except Exception as e:
            check(False, 'O8 异常', str(e)[:160])
        finally:
            ledger.end_scenario()

        page.screenshot(path='/tmp/verify_offline.png', full_page=True)
        browser.close()

    return ledger, injected_entry


def main():
    inject_mode = None
    for m in ('console', 'pageerror', 'unrelated'):
        if ('--inject-' + m) in sys.argv:
            inject_mode = m

    ledger, injected_entry = run(inject_mode)

    print('\n' + '=' * 60)
    for _st, _name, _detail, _sc in CKS.failures():
        print(f'  ❌ [{_sc}] {_name}'
              + (f'  → {_detail}' if _detail is not None else ''))
    # ★ A4.2 §1：四态分离；SKIP / N-A 不计入 PASS
    print(f'\n断言统计（四态分离）：{CKS.summary_line()}')
    print('  · PASS 只统计真实通过的断言；SKIP / N-A **不计入 PASS**')
    _notev = CKS.not_evaluated()
    print(f'\n未执行 / 无法评估明细（N-A + SKIP，共 {len(_notev)} 条）：')
    if not _notev:
        print('  （无）')
    for _st, _name, _reason, _sc in _notev:
        print(f'  [{_st}] [{_sc}] {_name}')
        print(f'         原因：{_reason}')
    print('\n逐场景断言数（审计）：')
    print(f'  {"PASS":>5} {"FAIL":>5} {"SKIP":>5} {"N/A":>5} {"总计":>6}  场景')
    _empty = []
    for _sc, _p, _f, _sk, _n, _tot, _real in CKS.scenario_report():
        _flag = '  （虚拟桶，非真实场景）' if not _real else ''
        if _tot == 0 and _real:
            _flag = '  ← ⚠️ 本场景未产生任何断言（疑似整体跳过）'
            _empty.append(_sc)
        print(f'  {_p:5d} {_f:5d} {_sk:5d} {_n:5d} {_tot:6d}  {_sc}{_flag}')
    print(f'  登记场景 {len(CKS.scenario_order)} 个；零断言场景 {len(_empty)} 个；'
          f'记录总数 {len(CKS.records)} 条')
    if _empty:
        print('  ⚠️ 零断言场景明细：' + '；'.join(_empty))
    _ok, _why = CKS.verdict()
    print(f'\n判定：{_why}')
    failed = CKS.failures()

    print('\n错误账本：')
    print('  ' + json.dumps(ledger.summary(), ensure_ascii=False))
    for e in ledger.fatal_entries()[:10]:
        print(f"   未确认[{e['scenario']}] {e['kind']}: {e['text'][:90]}  ({e['reason']})")

    # ---------------------------------------------------------- 判定
    if inject_mode:
        # 负向模式：本脚本**必须**把注入的错误判为致命（未确认）
        detected = injected_entry is not None and injected_entry['status'] != 'expected'
        if injected_entry is None:
            print('\n❌ 负向测试失败：注入的错误根本没被收集到')
            sys.exit(1)
        if not detected:
            print(f"\n❌ 负向测试失败：注入的错误被当成 expected 放过 "
                  f"（reason={injected_entry['reason']}）—— 过滤仍在吞错误")
            sys.exit(1)
        print(f"\n✅ 负向测试通过：注入的「{inject_mode}」错误被正确判为致命 "
              f"（reason={injected_entry['reason']}），未被静默忽略")
        sys.exit(0)

    # 正常模式：断言全过 + 零未确认错误
    fatal = ledger.fatal_entries()
    if failed or fatal:
        print('\n❌ 离线回归未通过')
    elif CKS.by_status(NA) or CKS.by_status(SKIP):
        # 离线是严格门：任何 N/A / SKIP 都必须暴露，不允许"带N/A 通过"
        print('\n❌ 离线回归存在 N/A 或 SKIP（严格门不接受未评估项）')
        sys.exit(1)
    print('\n✅ 离线回归通过（零未确认错误，完全不依赖外网）')


if __name__ == '__main__':
    main()
