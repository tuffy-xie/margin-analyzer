"""verify_ui.py —— 浏览器验收：真实 Chromium + 严格 assertion + 非 0 退出码

覆盖：
  · 3 个以上股票切换（285A / 6920 / 7974 / 3905）
  · 快速连续查询（竞态，issue 8）
  · 行情缺失（非法代码 → 必须清空旧图表，不能残留上一只股票）
  · refresh（强制刷新）
  · JPX / Ganan fallback 的数据源标注（issue 11）
  · 首页无内部评分数字、无矛盾文案（issue 9）
  · look-ahead：priceDate 不得晚于 marginDate（issue 2）

运行：python verify_ui.py        （失败时 exit 1）
"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

URL = 'http://127.0.0.1:8848/'
CODES = ['285A', '6920', '7974', '3905']
BAD_CODE = '9999Z'          # 合法格式但几乎不存在 → 用于「行情缺失」场景

FORBIDDEN = [
    (r'方向.{0,12}[-+]?\d+\s*/\s*100', '方向内部分数'),
    (r'リスク.{0,12}\d+\s*/\s*100', '风险内部分数'),
    (r'方向（多空倾向）', '方向依据标题'),
    (r'信用需給リスク', '风险维度标题'),
    (r'评分内部贡献', '旧标题'),
]

results = []
errors_all = []
checks = []          # (bool, name, detail)


def check(cond, name, detail=None):
    checks.append((bool(cond), name, detail))
    return bool(cond)


def find_chromium():
    """复用本机已缓存的 Chromium，不重复下载。"""
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


def wait_badge(page, timeout=90000):
    page.wait_for_function(
        "() => { const b=document.getElementById('hBadge');"
        " return b && !b.textContent.includes('読み込み中'); }",
        timeout=timeout)


def read_home(page):
    return page.evaluate(
        "() => { const p=document.getElementById('tab0');"
        " return p ? p.innerText : ''; }")


def read_state(page):
    return page.evaluate("""() => window.__lastD ? {
      code: window.__lastD.code,
      grade: window.__lastD.verdict.grade,
      badge: window.__lastD.verdict.badge,
      reasonCode: window.__lastD.verdict.reasonCode,
      reasonFacts: window.__lastD.verdict.reasonFacts,
      pos: window.__lastD.verdict.position,
      posKey: window.__lastD.verdict.position.key,
      comparison: window.__lastD.verdict.comparison || null,
      caAffected: window.__lastD.verdict.corporateActionAffected,
      caStatus: window.__lastD.verdict.corporateActionStatus || 'none',
      caText: (document.getElementById('hCA')||{}).innerText || '',
      denomConf: window.__lastD.verdict.denominatorConfidence,
      why: (document.getElementById('hWhy')||{}).innerText || '',
      srcLabel: (document.getElementById('srcLabel')||{}).textContent || '',
      srcFreq: (document.getElementById('srcFreq')||{}).textContent || '',
      sourceMeta: window.__lastD.sourceMeta,
      quadrant: window.__lastD.ind.quadrant,
      priceDates: (window.__lastD.rows||[]).map(r => ({
        margin: r.marginDate, price: r.priceDate, same: r.isSameDate })),
      chartIds: (function(){
        const p=document.getElementById('tab0');
        return p ? Array.from(p.querySelectorAll('div')).filter(e=>/^c\\d/.test(e.id)).map(e=>e.id) : [];
      })(),
      kpiN: document.querySelectorAll('#hKpis .kpi').length,
      caWarnVisible: (function(){
        const e=document.getElementById('hCA');
        return !!(e && e.style.display !== 'none' && e.textContent.trim());
      })(),
    } : null""")


def read_weekly(page):
    return page.evaluate(
        "() => { const e=document.getElementById('wkTrend');"
        " const f=document.getElementById('wkFoot');"
        " return (e?e.innerText:'') + '\\n' + (f?f.innerText:''); }")

def wait_weekly(page, timeout=90000):
    page.wait_for_function(
        "() => { const e=document.getElementById('wkTrend'); if(!e) return false;"
        " const t=e.innerText;"
        " return t.includes('週次データ取得失敗') || t.includes('データ蓄積中')"
        " || t.includes('買残') || t.includes('比較停止'); }",
        timeout=timeout)

def search(page, code):
    page.fill('#inp', code)
    page.click('button.primary')

print('chromium:', find_chromium())
exe = find_chromium()

with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
    page = browser.new_page(viewport={'width': 1280, 'height': 1400})
    js_errors = []
    # 仅当「场景 9 故意 abort /api/weekly」进行中时，该 abort 引起的资源加载错误
    # （net::ERR_FAILED / Failed to load resource）才被忽略。其余任何 console.error /
    # pageerror / 资源加载异常都必须照常导致验收失败。
    abort_active = {'v': False}
    page.on('pageerror', lambda e: js_errors.append(str(e)))

    def _on_console(msg):
        if msg.type != 'error':
            return
        t = msg.text
        if abort_active['v'] and ('Failed to load resource' in t or t.startswith('net::ERR')):
            return
        js_errors.append('console.error: ' + t)
    page.on('console', _on_console)

    # ---------- 场景 1：多股票切换 ----------
    print('\n=== 场景 1：多股票切换 ===')
    for code in CODES:
        page.goto(URL, wait_until='domcontentloaded')
        page.fill('#inp', code)
        page.click('button.primary')
        try:
            wait_badge(page)
        except Exception as e:
            check(False, f'{code} 加载超时', str(e)[:120])
            continue
        page.wait_for_timeout(1000)

        st = read_state(page)
        home = read_home(page)
        if not st:
            check(False, f'{code} 无 __lastD')
            continue

        check(st['code'] == code, f'{code} 状态码一致', st.get('code'))
        # 无内部评分
        viol = [lbl for pat, lbl in FORBIDDEN if re.search(pat, home)]
        check(not viol, f'{code} 首页无内部评分', viol)
        check(not re.findall(r'[-+]?\d+\s*/\s*100', home),
              f'{code} 首页无 /100 分数')
        # 单图 + 3 KPI
        check(st['chartIds'] == ['c4'], f'{code} 首页只有 1 张图', st['chartIds'])
        check(st['kpiN'] == 3, f'{code} 恰好 3 个 KPI', st['kpiN'])
        # verdict 自洽
        POS = {'good': '改善', 'mid': '中立', 'warn': '注意',
               'bad': '悪化', 'unknown': '判定不能'}
        check(POS.get(st['grade']) in st['badge'],
              f'{code} badge 与 grade 一致', f"{st['grade']} / {st['badge']}")
        check(bool(st['reasonFacts']),
              f'{code} reasonFacts 非空')
        # 矛盾检测：改善时不得说悪化
        if st['grade'] == 'good':
            txt = ' '.join(st['reasonFacts'])
            check('悪化' not in txt and '弱気' not in txt,
                  f'{code} 改善时文案不矛盾', txt)
        # 信用ポジション不得在无依据时显示绿色「軽い」
        if st['posKey'] == 'light':
            check(st['pos'].get('unknown') is False,
                  f'{code} light 档非 unknown')
        check(st['posKey'] in ('light', 'normal', 'heavy', 'unknown'),
              f'{code} posKey 合法', st['posKey'])
        # look-ahead：priceDate 不得 > marginDate
        bad_dates = [p for p in st['priceDates']
                     if p['price'] and p['margin'] and p['price'] > p['margin']]
        check(not bad_dates, f'{code} 无未来价格（priceDate<=marginDate）', bad_dates[:3])
        # comparison 必须自洽（from<=to）
        cmp = st.get('comparison')
        if cmp:
            check(cmp['from'] <= cmp['to'],
                  f'{code} comparison from<=to', cmp)
        # 数据源标注
        check(bool(st['srcLabel']), f'{code} 数据源已标注', st['srcLabel'])
        if st['sourceMeta']:
            sm = st['sourceMeta']
            if not sm['isOfficial']:
                check('週次' in (sm.get('frequency') or ''),
                      f'{code} 非官方源标注为週次', sm.get('frequency'))
                check(sm.get('periodUnit') == '週',
                      f'{code} 周期单位=週', sm.get('periodUnit'))

        # 公司行动：status 与文案措辞必须一致
        if st['caAffected']:
            check(st['caStatus'] in ('confirmed', 'suspected'),
                  f'{code} caStatus 合法', st['caStatus'])
            if st['caStatus'] == 'confirmed':
                check('を検出' in st['caText'],
                      f'{code} confirmed 用「を検出」', st['caText'][:60])
                check('可能性があります' not in st['caText'],
                      f'{code} confirmed 不用「可能性」')
            else:
                check('可能性があります' in st['caText'],
                      f'{code} suspected 用「可能性があります」', st['caText'][:60])
                check('を検出' not in st['caText'],
                      f'{code} suspected 不用「を検出」')
        else:
            check(st['caStatus'] == 'none',
                  f'{code} 未触发时 caStatus=none', st['caStatus'])

        results.append({'code': code, 'grade': st['grade'], 'badge': st['badge'],
                        'caStatus': st['caStatus'],
                        'pos': st['posKey'], 'cmp': cmp,
                        'src': st['srcLabel'], 'ca': st['caAffected']})

    # ---------- 场景 2：快速连续查询（竞态） ----------
    print('\n=== 场景 2：快速连续查询（竞态 issue 8） ===')
    page.goto(URL, wait_until='domcontentloaded')
    wait_badge(page)
    page.wait_for_timeout(500)
    # 不等待结果，直接连续切换
    for code in ['7974', '6920', '285A', '6920']:
        page.fill('#inp', code)
        page.click('button.primary')
        page.wait_for_timeout(120)
    try:
        wait_badge(page)
        page.wait_for_timeout(2500)
        st = read_state(page)
        # 最后一次请求是 6920
        check(st and st['code'] == '6920',
              '竞态：最终状态 = 最后请求的股票', st and st['code'])
        shown_code = page.evaluate(
            "() => (document.getElementById('codeLbl')||{}).textContent || ''")
        check(shown_code.strip() == '6920',
              '竞态：页面显示的代码 = 6920', shown_code)
    except Exception as e:
        check(False, '竞态：等待最终状态超时', str(e)[:120])

    # ---------- 场景 3：行情缺失必须清空旧图表 ----------
    print('\n=== 场景 3：行情缺失（清空旧图表） ===')
    page.goto(URL, wait_until='domcontentloaded')
    wait_badge(page)
    page.wait_for_timeout(800)
    page.fill('#inp', BAD_CODE)
    page.click('button.primary')
    try:
        wait_badge(page, timeout=60000)
        page.wait_for_timeout(1500)
    except Exception:
        pass
    name_after = page.evaluate(
        "() => (document.getElementById('name')||{}).textContent || ''")
    code_after = page.evaluate(
        "() => (document.getElementById('codeLbl')||{}).textContent || ''")
    # 关键：不得残留上一只股票（7974 任天堂）
    check('任天堂' not in name_after,
          '缺失场景：未残留上一只股票名称', name_after)
    check('7974' not in code_after,
          '缺失场景：未残留上一只股票代码', code_after)

    # ---------- 场景 4：refresh ----------
    print('\n=== 场景 4：refresh 强制刷新 ===')
    page.goto(URL, wait_until='domcontentloaded')
    wait_badge(page)
    page.wait_for_timeout(600)

    def upstream_fetches():
        """读 /api/health 里的上游抓取计数（纯本地计数，不发网络请求）。"""
        try:
            with urlopen(URL + 'api/health', timeout=10) as r:
                return (json.loads(r.read().decode()).get('upstream') or {}).get('indexFetches')
        except Exception:
            return None

    f0 = upstream_fetches()
    t0 = time.time()
    page.click('button.wide-only')          # 刷新按钮
    # ★ issue 12：一次用户主动 refresh 只允许刷新一次上游索引。
    #   旧实现前端 a=0/1/2 三次请求都带 fresh=1 → 3 次昂贵刷新（实测 ~117s/次），
    #   只能靠把 timeout 拉到 420s 让测试"通过"。现在恢复到合理预算（120s），
    #   并直接断言上游抓取次数 ≤ 1。
    try:
        wait_badge(page, timeout=120000)
        page.wait_for_timeout(1500)
        elapsed = time.time() - t0
        st = read_state(page)
        if st:
            cmp = st.get('cmp')
            check(cmp is None or cmp.get('from') <= cmp.get('to'),
                  'refresh 后 comparison 仍自洽', cmp)
            check(st['posKey'] in ('light', 'normal', 'heavy', 'unknown'),
                  'refresh 后仓位档位合法', st['posKey'])
            check(elapsed < 120, 'refresh 在 120s 预算内完成', round(elapsed, 1))
            print(f"    refresh 完成：{st['badge']}（{elapsed:.1f}s）")
        else:
            check(False, 'refresh 后无数据（可能仍在解析）')
    except Exception as e:
        check(False, 'refresh 未在 120s 内完成', str(e)[:120])

    f1 = upstream_fetches()
    check(f0 is not None and f1 is not None, '可读取上游抓取计数', (f0, f1))
    if f0 is not None and f1 is not None:
        delta = f1 - f0
        check(delta <= 1, '一次 refresh 上游索引抓取 ≤ 1 次', f'{f0} → {f1}')
        print(f"    上游抓取次数：{f0} → {f1}（delta={delta}）")

    # ---------- 场景 5：JPX / Ganan 数据源切换 ----------
    print('\n=== 场景 5：数据源切换（JPX official / Ganan） ===')
    for val, label in [('official', 'JPX 官方'), ('ganan', 'Ganan 週次')]:
        try:
            page.goto(URL, wait_until='domcontentloaded')
            wait_badge(page)
            page.select_option('#srcSel', val)
            page.wait_for_timeout(500)
            try:
                wait_badge(page)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            st = read_state(page)
            if st and st['sourceMeta']:
                sm = st['sourceMeta']
                if val == 'official':
                    check(sm['isOfficial'] is True,
                          'official 模式标注为官方', sm)
                    check('JPX' in (sm.get('label') or ''),
                          'official 模式 label 含 JPX', sm.get('label'))
                else:
                    check('週次' in (sm.get('frequency') or '') or
                          'Ganan' in (sm.get('label') or ''),
                          'ganan 模式如实标注为週次/Ganan', sm)
            else:
                print(f'    ({label}: 无数据可比对，跳过标注断言)')
        except Exception as e:
            check(False, f'{label} 模式异常', str(e)[:120])

    # ---------- 场景 6：週次トレンド 4519 ----------
    print('\n=== 场景 6：週次トレンド 4519 ===')
    try:
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '4519')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(500)
        wt = read_weekly(page)
        check('4週' in wt and '8週' in wt and '13週' in wt,
              '4519 週次三行齐全', wt[:80].replace('\n', ' '))
        check('データ蓄積中' in wt, '4519 13W 显示数据蓄积中')
        check('（11/14週）' in wt, '4519 13W 显示 11/14週', wt[:140].replace('\n', ' '))
        check('履歴' in wt and '11週' in wt, '4519 履歴 11週', wt[:140].replace('\n', ' '))
        check('週次データ取得失敗' not in wt, '4519 周次正常取得（非失败）')
    except Exception as e:
        check(False, '4519 週次异常', str(e)[:120])

    # ---------- 场景 7：股票切换 4519→6981 不残留 ----------
    print('\n=== 场景 7：週次竞态（4519→6981） ===')
    try:
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '4519'); page.wait_for_timeout(200)
        search(page, '6981')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(500)
        wt = read_weekly(page)
        # 6981 的 4W 買残为负（≈ -30%）；若残留 4519 会显示 +20% 左右
        check('買残 -' in wt, '6981 週次为自身数据（買残负）', wt[:140].replace('\n', ' '))
        check('買残 +' not in wt, '6981 未残留 4519 的 +買残', wt[:140].replace('\n', ' '))
    except Exception as e:
        check(False, '週次竞态异常', str(e)[:120])

    # ---------- 场景 8：285A 公司行动 → 比較停止 ----------
    print('\n=== 场景 8：285A 公司行动（比較停止） ===')
    try:
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '285A')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(500)
        wt = read_weekly(page)
        check('比較停止' in wt, '285A 受影响周期显示 比較停止', wt[:180].replace('\n', ' '))
        check('株式分割・併合の影響' in wt, '285A 显示 株式分割・併合の影響')
        check('買残 +2' not in wt, '285A 不显示误导性巨大買残变化', wt[:180].replace('\n', ' '))
    except Exception as e:
        check(False, '285A 公司行动异常', str(e)[:120])

    # ---------- 场景 9：週次 API 失败不影响 margin 主页面 ----------
    print('\n=== 场景 9：週次 API 失败隔离 ===')
    try:
        abort_active['v'] = True
        page.route('**/api/weekly*', lambda route: route.abort())
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '4519')
        wait_badge(page)
        page.wait_for_timeout(2500)
        home = read_home(page)
        wt = read_weekly(page)
        ok_grade = any(g in home for g in ('改善', '中立', '注意', '悪化', '判定不能'))
        check(ok_grade, '週次失败时 margin 主页仍正常（有评级）')
        check('週次データ取得失敗' in wt, '週次失败时本区块单独报错', wt[:80].replace('\n', ' '))
        check('読み込み中' not in home, 'margin 主页无残留 loading')
    except Exception as e:
        check(False, '週次隔离异常', str(e)[:120])
    finally:
        abort_active['v'] = False
        try:
            page.unroute('**/api/weekly*')
        except Exception:
            pass

    # ---------- 场景 10：13W 未来可用 fixture ----------
    print('\n=== 场景 10：13W 可用 fixture ===')
    try:
        fixture = {
            "ok": True, "code": "9999", "coverageWeeks": 14,
            "rows": [{"weekEnding": "2026-07-10", "buy": 1000000, "sell": 500000, "ratio": 2.0},
                     {"weekEnding": "2026-10-09", "buy": 950000, "sell": 480000, "ratio": 1.98}],
            "trends": {
                "w4":  {"available": True, "fromDate": "2026-10-02", "toDate": "2026-10-09", "buyPct": -5.0, "sellPct": -4.0, "ratioFrom": 2.0, "ratioTo": 1.98, "corporateActionAffected": False},
                "w8":  {"available": True, "fromDate": "2026-09-18", "toDate": "2026-10-09", "buyPct": -3.0, "sellPct": -2.0, "ratioFrom": 2.1, "ratioTo": 1.98, "corporateActionAffected": False},
                "w13": {"available": True, "fromDate": "2026-07-10", "toDate": "2026-10-09", "buyPct": -7.7, "sellPct": -4.0, "ratioFrom": 2.0, "ratioTo": 1.98, "corporateActionAffected": False},
            },
            "sources": {"ganan": {"weeks": 14, "from": "2026-07-10", "to": "2026-10-09"}, "jpx": {"weeks": 0, "from": None, "to": None}, "mergedWeeks": 14, "jpxWinsWeeks": 0, "note": "fixture"},
        }
        page.goto(URL, wait_until='domcontentloaded')
        page.evaluate("(function(){ renderWeeklyTrend(%s); })()" % json.dumps(fixture))
        page.wait_for_timeout(300)
        wt = read_weekly(page)
        check('13週' in wt, '13W 可用时显示 13週 行')
        check('買残 -7.7%' in wt, '13W 可用时显示数字变化（非数据蓄积中）', wt[:200].replace('\n', ' '))
        check('データ蓄積中' not in wt, '13W 可用时不显示数据蓄积中')
    except Exception as e:
        check(False, '13W fixture 异常', str(e)[:120])

    # ---------- 场景 11：窄屏不溢出 ----------
    print('\n=== 场景 11：窄屏布局（380px） ===')
    try:
        page.set_viewport_size({'width': 380, 'height': 800})
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '4519')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(400)
        box = page.evaluate(
            "() => { const e=document.getElementById('wkTrend'); const r=e.getBoundingClientRect();"
            " return {right:r.right, w:r.width, vw:window.innerWidth}; }")
        check(box['right'] <= box['vw'] + 1, '窄屏 wkTrend 不溢出视口', box)
        page.set_viewport_size({'width': 1280, 'height': 1400})
    except Exception as e:
        check(False, '窄屏布局异常', str(e)[:120])

    page.screenshot(path='/tmp/verify_final.png', full_page=True)
    browser.close()

# ---------------------------------------------------------------- 汇总
print('\n' + '=' * 60)
failed = [c for c in checks if not c[0]]
for good, name, detail in checks:
    if not good:
        print(f'  ❌ {name}' + (f'  → {detail}' if detail is not None else ''))

print(f'\n断言：{len(checks) - len(failed)} 通过 / {len(failed)} 失败')
if js_errors:
    print('JS 错误：')
    for e in js_errors[:10]:
        print('   ', e[:160])
else:
    print('JS 错误：无')

print('\n各股票结论：')
for r in results:
    print(f"  {r['code']:5} {r['badge']:26} pos={r['pos']:8} ca={r.get('caStatus','-'):9} "
          f"cmp={(r['cmp'] or {}).get('from','—')}→{(r['cmp'] or {}).get('to','—')} "
          f"src={r['src']}")

if failed or js_errors:
    print('\n❌ 验收未通过')
    sys.exit(1)
print('\n✅ 验收通过')