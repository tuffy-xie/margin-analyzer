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
import os
import re
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ★ A3.2.5：归因式错误账本（替代 A3.2 的按文本全局排除）
from verify_errors import ErrorLedger, CheckLedger, NA, SKIP

URL = 'http://127.0.0.1:8848/'
LEDGER_ERR = ErrorLedger()
ledger = LEDGER_ERR          # 兼容旧名（错误账本，非断言账本）
CODES = ['285A', '6920', '7974', '3905']
BAD_CODE = '9999Z'          # 合法格式但几乎不存在 → 用于「行情缺失」场景

# 场景 5 用的固定 Ganan fixture（不依赖真实网络）。
# 沙箱/CI 无外网到 ganan-finance.com，真实 Ganan 请求会挂起/失败而残留旧 official
# 数据，导致「选 ganan 后 sourceMeta 仍 official」的假阳性。
# 该 fixture 仅替换 window.MA.fetchMarginSeries 的返回，专测「选 ganan 后
# sourceMeta 是否如实标注週次/Ganan」——切换逻辑正确性已由 A2.5 复现证实
# （注入 fixture 后 sourceMeta.id='ganan' / frequency='週次'）。
GANAN_FIXTURE_JS = r"""(async (code) => {
  const base = [
    {date:'2026-07-10', buy:9000000, sell:700000, ratio:12.85},
    {date:'2026-07-17', buy:9100000, sell:705000, ratio:12.91},
    {date:'2026-07-24', buy:8950000, sell:690000, ratio:12.97},
    {date:'2026-07-31', buy:9200000, sell:710000, ratio:12.96},
    {date:'2026-08-07', buy:9300000, sell:720000, ratio:12.92},
    {date:'2026-08-14', buy:9150000, sell:700000, ratio:13.07},
    {date:'2026-08-21', buy:9400000, sell:730000, ratio:12.88},
    {date:'2026-08-28', buy:9350000, sell:725000, ratio:12.90},
    {date:'2026-09-04', buy:9450000, sell:735000, ratio:12.86},
    {date:'2026-09-11', buy:9600000, sell:740000, ratio:12.97},
  ];
  return base.map(r => ({...r, ratioCalc: +(r.buy/r.sell).toFixed(2)}));
})"""

FORBIDDEN = [
    (r'方向.{0,12}[-+]?\d+\s*/\s*100', '方向内部分数'),
    (r'リスク.{0,12}\d+\s*/\s*100', '风险内部分数'),
    (r'方向（多空倾向）', '方向依据标题'),
    (r'信用需給リスク', '风险维度标题'),
    (r'评分内部贡献', '旧标题'),
    # ★ A4.3 §1：**裸分数**形式（无 /100 后缀）同样禁止。
    #   A4.2 实测 4519 首页泄漏「方向スコア 30」，原 FORBIDDEN 只禁 /100 形式，
    #   没禁裸数字 → 漏检。这里补齐，首页只允许定性表述。
    (r'方向スコア\s*[-+]?\d+', '方向スコア裸数字'),
    (r'信用リスク\s*[-+]?\d+', '信用リスク裸数字'),
    (r'スコア\s*[:：]?\s*[-+]?\d+', 'スコア裸数字'),
]

results = []
errors_all = []

# ★ A4.2 §1：断言统计可信度改造。
#   · PASS / FAIL / SKIP / N-A 四态**分开统计**（由 CKS 内部实现）
#   · SKIP 与 N/A **一律不计入 PASS**
#   · **禁止** check(True, ...) 伪装 N/A —— N/A 必须走 na()，单列且不冒充通过
#   · 每个场景登记，check() 记录所属场景，收尾逐场景打印断言数
#   · 数据依赖分支未执行时，用 na()/skip() 显式登记**未执行原因**
CKS = CheckLedger()          # A4.2 §1：四态断言账本


def scen(name):
    """登记并打印一个场景；后续 check() 都归属该场景。"""
    CKS.scen(name)
    print(f'\n=== {name} ===')


# ★ A3.2.5 §7：区分「应用回归」与「因上游环境而无法评估」的断言。
#   下列断言依赖真实上游（JPX/代理）可用；当上游持续 429/CORS 时它们无法评估。
#   这类失败会**如实上报为 ONLINE_DEGRADED**，绝不改写成成功；
#   且对应行为已由离线确定性回归 verify_offline.py 严格覆盖。
UPSTREAM_SENSITIVE = {
    '9b/9c：週次失败时 margin 主页仍正常（有评级，上游限速时重试）',
}


def check(cond, name, detail=None):
    """真断言：PASS 或 FAIL。**绝不用它记录 N/A**（那是 na() 的职责）。"""
    return CKS.check(cond, name, detail)


def na(name, reason):
    """N/A：该断言无法评估（上游不可用）。**不计入 PASS**，必须给出原因。"""
    return CKS.na(name, reason)


def skip(name, reason):
    """SKIP：故意不执行。不计入 PASS，必须给出原因。"""
    return CKS.skip(name, reason)


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

# 上游节流：Yahoo / Ganan 走公共代理，连续快速刷新会触发 HTTP 429。
# 浏览器端每次整页加载都会重新拉一次 Yahoo 日线，因此场景之间留出间隔，
# 避免把「上游限流」误判成应用缺陷（限流属环境问题，不是 JS 错误）。
def throttle(ms=2500):
    time.sleep(ms / 1000.0)

print('chromium:', find_chromium())
exe = find_chromium()

with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
    page = browser.new_page(viewport={'width': 1280, 'height': 1400})
    # ★ A3.2.5 §1/§3：统一改用**归因式**错误账本（verify_errors.ErrorLedger）。
    #   A3.2 的按文本全局排除（Failed to load resource / net::ERR / 429 / CORS /
    #   Failed to fetch）已**全部移除**——那会隐藏真实错误。
    #   现在只有「当前场景明确制造 + 可归因到该场景预期请求」的错误才算 expected，
    #   其余一律保留为未确认（致命），并按在线环境如实上报，绝不静默忽略。
    ledger = ErrorLedger()
    ledger.begin_scenario('online-general')
    page.on('pageerror', lambda e: ledger.add_pageerror(str(e)))   # pageerror 永不忽略
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

    # ---------- 场景 1：多股票切换 ----------
    scen('场景 1：多股票切换')
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
        else:
            na(f'{code} 改善时文案矛盾检测',
               reason=f'grade={st["grade"]}（非 good），该断言仅在 grade=good 时适用')
        # 信用ポジション不得在无依据时显示绿色「軽い」
        if st['posKey'] == 'light':
            check(st['pos'].get('unknown') is False,
                  f'{code} light 档非 unknown')
        else:
            na(f'{code} light 档非 unknown 检测',
               reason=f'posKey={st["posKey"]}（非 light），该断言仅在 light 档时适用')
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
        else:
            na(f'{code} comparison from<=to',
               reason='comparison 不存在（数据不足，无法评估区间自洽性）')
        # 数据源标注
        check(bool(st['srcLabel']), f'{code} 数据源已标注', st['srcLabel'])
        if st['sourceMeta'] and not st['sourceMeta']['isOfficial']:
            sm = st['sourceMeta']
            check('週次' in (sm.get('frequency') or ''),
                  f'{code} 非官方源标注为週次', sm.get('frequency'))
            check(sm.get('periodUnit') == '週',
                  f'{code} 周期单位=週', sm.get('periodUnit'))
        else:
            na(f'{code} 非官方源週次标注 / 周期单位',
               reason='当前为官方源（isOfficial=True）或 sourceMeta 缺失，'
                      '该断言仅适用于非官方源')

        # 公司行动：status 与文案措辞必须一致
        if st['caAffected']:
            check(st['caStatus'] in ('confirmed', 'suspected'),
                  f'{code} caStatus 合法', st['caStatus'])
            if st['caStatus'] == 'confirmed':
                check('を検出' in st['caText'],
                      f'{code} confirmed 用「を検出」', st['caText'][:60])
                check('可能性があります' not in st['caText'],
                      f'{code} confirmed 不用「可能性」')
            elif st['caStatus'] == 'suspected':
                check('可能性があります' in st['caText'],
                      f'{code} suspected 用「可能性があります」', st['caText'][:60])
                check('を検出' not in st['caText'],
                      f'{code} suspected 不用「を検出」')
            else:
                na(f'{code} 公司行动文案措辞（confirmed/suspected）',
                   reason=f'caAffected=True 但 caStatus={st["caStatus"]}，两者不一致')
        else:
            na(f'{code} 公司行动文案措辞检测',
               reason='caAffected=False（未触发公司行动），该断言不适用')
            check(st['caStatus'] == 'none',
                  f'{code} 未触发时 caStatus=none', st['caStatus'])

        results.append({'code': code, 'grade': st['grade'], 'badge': st['badge'],
                        'caStatus': st['caStatus'],
                        'pos': st['posKey'], 'cmp': cmp,
                        'src': st['srcLabel'], 'ca': st['caAffected']})

    # ---------- 场景 2：快速连续查询（竞态） ----------
    scen('场景 2：快速连续查询（竞态 issue 8）')
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
    scen('场景 3：行情缺失（清空旧图表）')
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
    scen('场景 4：refresh 强制刷新')
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
    if f0 is not None and f1 is not None:
        delta = f1 - f0
        check(True, '可读取上游抓取计数', (f0, f1))
        check(delta <= 1, '一次 refresh 上游索引抓取 ≤ 1 次', f'{f0} → {f1}')
        print(f"    上游抓取次数：{f0} → {f1}（delta={delta}）")
    else:
        na('可读取上游抓取计数 / 一次 refresh 上游索引抓取 ≤ 1 次',
           reason=f'/api/health 不可读或无 indexFetches 字段（f0={f0}, f1={f1}），'
                  f'无法评估上游抓取次数')

    # ---------- 场景 5：JPX / Ganan 数据源切换 ----------
    scen('场景 5：数据源切换（JPX official / Ganan）')
    for val, label in [('official', 'JPX 官方'), ('ganan', 'Ganan 週次')]:
        try:
            page.goto(URL, wait_until='domcontentloaded')
            wait_badge(page)
            if val == 'ganan':
                # 固定 Ganan fixture：不依赖真实网络（沙箱无外网到 ganan-finance.com）。
                # 仅替换 fetchMarginSeries 的返回，专测「选 ganan 后 sourceMeta 是否
                # 如实标注週次/Ganan」。切换逻辑正确性已另行复现证实。
                page.evaluate("window.MA.fetchMarginSeries = " + GANAN_FIXTURE_JS)
            page.select_option('#srcSel', val)
            page.wait_for_timeout(800)
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
                # ★ A4.2 §1：**禁止 check(True) 伪装 N/A**。
                #   上游 DEGRADED 导致 sourceMeta 缺失时，这条标注断言**无法评估** →
                #   走 na()，在统计里单列为 N/A，**不计入 PASS**，并登记未执行原因。
                #   对应行为已由离线 verify_offline.py 确定性覆盖。
                na(f'{label} 模式：sourceMeta 标注断言',
                   reason=f'上游 DEGRADED，sourceMeta 不可用'
                          f'（srcLabel={st.get("srcLabel") if st else None}）；'
                          f'该标注行为由 verify_offline.py 离线确定性覆盖')
        except Exception as e:
            check(False, f'{label} 模式异常', str(e)[:120])

    # ---------- 场景 6：週次トレンド 4519 ----------
    scen('场景 6：週次トレンド 4519')
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

    # ---------- 场景 6b：週次トレンド 5801（正常株、NOT 比較停止） ----------
    scen('场景 6b：週次トレンド 5801')
    try:
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '5801')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(500)
        wt = read_weekly(page)
        check('4週' in wt and '8週' in wt and '13週' in wt,
              '5801 週次三行齐全', wt[:80].replace('\n', ' '))
        check('データ蓄積中' in wt, '5801 13W 显示数据蓄积中（非比較停止）')
        check('（11/14週）' in wt, '5801 13W 显示 11/14週', wt[:140].replace('\n', ' '))
        check('比較停止' not in wt, '5801 正常株 NOT 显示比較停止')
        check('週次データ取得失敗' not in wt, '5801 周次正常取得（非失败）')
    except Exception as e:
        check(False, '5801 週次异常', str(e)[:120])

    # ---------- 场景 7：股票切换 4519→6981 不残留 ----------
    scen('场景 7：週次竞态（4519→6981）')
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
    scen('场景 8：285A 公司行动（比較停止）')
    try:
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '285A')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(500)
        wt = read_weekly(page)
        check('比較停止' in wt, '285A 受影响周期显示 比較停止', wt[:180].replace('\n', ' '))
        # ★ A3.1：285A 由週次ヒューリスティックのみ検出 → 仅 suspected（无 splitConfirmed 精确事件源）。
        #   故 UI 必须显示「可能性」文案，绝不可冒充 confirmed 的「影響」文案。
        check('株式分割・併合の可能性' in wt, '285A 显示 suspected 文案（可能性，而非 confirmed 影響）', wt[:180].replace('\n', ' '))
        check('株式分割・併合の影響' not in wt, '285A 无 confirmed 事件源 → 不实显示 confirmed「影響」文案')
        check('買残 +2' not in wt, '285A 不显示误导性巨大買残变化', wt[:180].replace('\n', ' '))
    except Exception as e:
        check(False, '285A 公司行动异常', str(e)[:120])

    # ---------- 场景 9：週次 API 失败不影响 margin 主页面 ----------
    scen('场景 9：週次 API 失败隔离')
    try:
        # 本场景明确制造：abort /api/weekly → 登记为预期请求
        ledger.begin_scenario('9-weekly-abort', expected_requests=['/api/weekly'])
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
        ledger.end_scenario()
        try:
            page.unroute('**/api/weekly*')
        except Exception:
            pass

    # ---------- 场景 10：13W 未来可用 fixture ----------
    scen('场景 10：13W 可用 fixture')
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
    scen('场景 11：窄屏布局（380px）')
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

    # ---------- 场景 12：注目 的真实支撑（非 -2% 机械位） ----------
    scen('场景 12-14/16：注目真实支撑 / Hero 四象限 / 注目位置 / 窄屏')
    # 这 4 项都只读同一只股票(4519)的 DOM，共用一次整页加载 —— Yahoo 走公共代理，
    # 每次 reload 都会重新拉一次日线，频繁 reload 会触发上游 429 限流（环境问题）。
    try:
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '4519')
        wait_badge(page)
        page.wait_for_timeout(700)

        # ---- 场景 12：注目使用真实技术支撑（非 -2% 机械位） ----
        sup = page.evaluate("() => { const d=window.__lastD; if(!d) return null;"
                            " return calcShortSupport(d.barsAll, d.val.price); }")
        check(sup is not None, '支撑计算可在真实数据上执行')
        if sup:
            check(sup['type'] in ('pivot_cluster', 'pivot', '20d_low', 'none'),
                  '支撑 type 合法', sup['type'])
            if sup['type'] != 'none':
                check(sup['price'] is not None, '有支撑时给出价格', sup)
                if sup['type'] in ('pivot_cluster', 'pivot'):
                    check(abs(sup['distancePct'] - 2.0) > 0.05,
                          '支撑距离不是固定 -2%', 'distancePct=' + str(sup['distancePct']))
                else:
                    na('支撑距离不是固定 -2%',
                       reason=f'support type={sup["type"]}（非 pivot/pivot_cluster），'
                              f'该断言仅适用于枢轴支撑')
            else:
                na('有支撑时给出价格 / 支撑距离不是固定 -2%',
                   reason='支撑 type=none（未找到有效支撑），该断言不适用')
        wt = page.evaluate("() => { const e=document.getElementById('hWatchB'); return e?e.innerText:''; }")
        check(len(wt.strip()) > 0, '注目文案已生成', wt[:120].replace('\n', ' '))
        n_items = page.evaluate("() => document.querySelectorAll('#hWatchB li').length")
        check(n_items <= 3, '注目条目 <= 3', 'n=' + str(n_items))
        check(n_items >= 1, '注目至少 1 条', 'n=' + str(n_items))

        # ---- 场景 13：Hero 四象限「株価 × 信用買残」 ----
        qt = page.evaluate("() => { const e=document.getElementById('hQuad');"
                           " return e?e.innerText.replace(/\\s+/g,''):''; }")
        check('株価' in qt, 'Hero 显示「株価」', qt)
        check('信用買残' in qt, 'Hero 显示「信用買残」', qt)
        check('×' in qt, 'Hero 显示 × 组合符', qt)
        check(any(a in qt for a in ('↑', '↓', '→', '—')), 'Hero 显示方向箭头', qt)
        reason = page.evaluate("() => { const e=document.getElementById('hQuadReason'); return e?e.innerText:''; }")
        check(len(reason.strip()) > 0, 'Hero 显示结构原因小字', reason[:80])

        # ---- 场景 14：注目 位于 Hero 之后、KPI 之前；KPI 在週次トレンド 之前 ----
        order = page.evaluate(
            "() => { const y=id=>{const e=document.getElementById(id);"
            " return e? e.getBoundingClientRect().top + window.scrollY : null;};"
            " return {hero:y('hero'), watch:y('hWatch'), kpis:y('hKpis'), wkTrend:y('wkTrend')}; }")
        check(order['watch'] is not None, '注目元素存在')
        if order['watch'] is not None:
            check(order['hero'] <= order['watch'], '注目在 Hero 之后', order)
            check(order['watch'] <= order['kpis'], '注目在 KPI 之前', order)
            if order['wkTrend'] is not None:
                check(order['kpis'] <= order['wkTrend'], 'KPI 在 週次トレンド 之前', order)
            else:
                na('KPI 在 週次トレンド 之前',
                   reason='wkTrend 元素不存在（週次未渲染），无法比较纵向顺序')

        # ---- 场景 17：判定ロジック只存在于「信用残詳細」且默认收起 ----
        scope = page.evaluate(
            "() => {"
            " const vc=document.getElementById('verdictCard');"
            " const inDetail = !!(vc && vc.closest('#tab1'));"
            " const det=document.querySelector('#tab1 details.fold');"
            " const collapsed = det ? !det.open : null;"
            " const home=document.getElementById('tab0');"
            " const t=home?home.innerText:'';"
            " return {inDetail, collapsed,"
            "   homeHasVerdict: t.includes('この判定になった理由'),"
            "   homeHasScore: t.includes('方向（多空倾向）')"
            "     || t.includes('信用需給リスク')"
            "     || t.includes('/ 100') || t.includes('/100')};"
            " }")
        check(scope['inDetail'], '判定ロジック容器位于「信用残詳細」(tab1)')
        check(scope['collapsed'] is True, '判定ロジック默认收起（不展开）', scope)
        check(not scope['homeHasVerdict'] and not scope['homeHasScore'],
              '首页不含「判定理由」标题与内部分数(/100)', scope)

        # ---- 场景 16：窄屏不溢出（仅 resize，不重新加载 → 不触发新上游请求） ----
        page.set_viewport_size({'width': 380, 'height': 900})
        page.wait_for_timeout(500)
        ov = page.evaluate(
            "() => { const ids=['hQuad','hWatch','hQuadReason']; const out={vw:window.innerWidth};"
            " ids.forEach(id=>{const e=document.getElementById(id); if(e){const r=e.getBoundingClientRect();"
            " out[id]={right:r.right, w:r.width};}}); return out; }")
        for key in ('hQuad', 'hWatch', 'hQuadReason'):
            if key in ov:
                check(ov[key]['right'] <= ov['vw'] + 1, '窄屏 ' + key + ' 不溢出', ov[key])
        page.set_viewport_size({'width': 1280, 'height': 1400})
    except Exception as e:
        check(False, '注目/四象限/位置/窄屏 综合异常', str(e)[:160])

    # ---------- 场景 15：切换股票，注目支撑不残留 ----------
    scen('场景 15：切换股票支撑不残留')
    # 复用本套件前面已加载过的代码（285A / 3905），避免引入「未缓存的新代码」
    # 而额外打一次上游 Yahoo —— Yahoo 走公共代理，突发请求会 429（环境问题）。
    try:
        throttle(3000)
        search(page, '3905')
        wait_badge(page)
        page.wait_for_timeout(900)
        sup_b = page.evaluate("() => { const d=window.__lastD; return d?calcShortSupport(d.barsAll,d.val.price):null; }")
        check(sup_b is not None, '切换后支撑可计算（3905）', sup_b)
        # 快速来回切换，旧请求不覆盖
        throttle(2000)
        search(page, '285A')
        page.wait_for_timeout(150)
        search(page, '3905')
        wait_badge(page)
        page.wait_for_timeout(900)
        st = page.evaluate("() => ({ code: window.__lastD? window.__lastD.val.code : null })")
        check(st['code'] == '3905', '快速切换后最终为 3905（无旧请求覆盖）', str(st))
    except Exception as e:
        check(False, '切换股票残留异常', str(e)[:120])

    # ---------- 场景 9b/9c：故意 abort 不吞掉其他资源/JS 错误（Issue F） ----------
    scen('场景 9b/9c：错误过滤严格化（Issue F）')
    try:
        # 本场景明确制造：abort /api/weekly（预期请求）+ 两个哨兵（预期错误）
        ledger.begin_scenario(
            '9b9c-sentinels',
            sentinels=['UNRELATED_RESOURCE_ERR_TEST_9b', 'UNRELATED_JS_ERR_TEST_9c'],
            expected_requests=['/api/weekly'])
        page.route('**/api/weekly*', lambda route: route.abort())
        page.goto(URL, wait_until='domcontentloaded')
        # 上游（JPX）偶发 429 限速时重试，避免把「上游限流」误判为应用缺陷
        ok_grade = False
        last_home = ''
        for _ in range(3):
            search(page, '4519')
            page.wait_for_timeout(1500)
            last_home = read_home(page)
            ok_grade = any(g in last_home for g in ('改善', '中立', '注意', '悪化', '判定不能'))
            if ok_grade:
                break
            throttle(2500)
        n0 = len(ledger.entries)
        # 故意注入一个与 weekly 无关的 console 错误 + 一个 pageerror，
        # 验证它们不会被「abort 期间忽略所有资源错误」的过宽过滤吞掉。
        page.evaluate("console.error('UNRELATED_RESOURCE_ERR_TEST_9b')")
        page.evaluate("(function(){ setTimeout(function(){ throw new Error('UNRELATED_JS_ERR_TEST_9c'); }, 0); })()")
        page.wait_for_timeout(2500)
        home = read_home(page)
        ok_grade = ok_grade or any(g in home for g in ('改善', '中立', '注意', '悪化', '判定不能'))
        check(ok_grade, '9b/9c：週次失败时 margin 主页仍正常（有评级，上游限速时重试）',
              last_home[:80].replace('\n', ' '))
        check(any('UNRELATED_RESOURCE_ERR_TEST_9b' in e['text'] for e in ledger.entries[n0:]),
              '9b：无关 console.error 未被吞掉（错误过滤已严格化）',
              [e['text'][:60] for e in ledger.entries[n0:n0+3]])
        check(any('UNRELATED_JS_ERR_TEST_9c' in e['text'] for e in ledger.entries[n0:]),
              '9c：pageerror 未被吞掉（pageerror 永远上报）',
              [e['text'][:60] for e in ledger.entries[n0:n0+3]])
    except Exception as e:
        check(False, '9b/9c 异常', str(e)[:120])
    finally:
        ledger.end_scenario()
        try:
            page.unroute('**/api/weekly*')
        except Exception:
            pass

    # ---------- 场景 E：4519 → 6981 旧週次响应不覆盖（Issue E） ----------
    scen('场景 E：週次竞态 4519→6981（Issue E）')
    try:
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '4519'); page.wait_for_timeout(200)
        search(page, '6981')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(500)
        wt = read_weekly(page)
        # 6981 的 4W 買残为负（≈ -30%）；若残留 4519 会显示 +20% 左右
        check('買残 -' in wt, 'E：6981 週次为自身数据（買残负）', wt[:140].replace('\n', ' '))
        check('買残 +' not in wt, 'E：6981 未残留 4519 的 +買残（旧响应未覆盖）', wt[:140].replace('\n', ' '))
        st = read_state(page)
        check(st and st['code'] == '6981', 'E：最终状态为 6981', st and st['code'])
    except Exception as e:
        check(False, 'E 异常', str(e)[:120])

    # ---------- 场景 F：同一代码连续刷新，旧响应不覆盖（Issue E/C） ----------
    scen('场景 F：同代码连续刷新竞态（Issue E/C）')
    try:
        page.goto(URL, wait_until='domcontentloaded')
        search(page, '4519')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(500)
        # 连续两次刷新（同一代码 4519），验证最终状态稳定为 4519、无旧响应覆盖/无残留旧週次
        page.click('button.wide-only')
        page.wait_for_timeout(300)
        page.click('button.wide-only')
        wait_badge(page)
        wait_weekly(page)
        page.wait_for_timeout(800)
        st = read_state(page)
        check(st and st['code'] == '4519', 'F：同代码连续刷新后最终为 4519', st and st['code'])
        wt = read_weekly(page)
        check('4週' in wt and '8週' in wt and '13週' in wt, 'F：週次为 4519 自身三行', wt[:60].replace('\n', ' '))
        check('データ蓄積中' in wt, 'F：4519 13W 仍为数据蓄积中（非比较停止）')
        check('読み込み中' not in (read_home(page)), 'F：margin 主页无残留 loading')
    except Exception as e:
        check(False, 'F 异常', str(e)[:120])

    # ---------- 场景 H：Ganan 取得失败不残留旧 JPX 结论（Issue D） ----------
    scen('场景 H：Ganan 失败不残留 JPX（Issue D）')
    try:
        # 本场景明确制造：abort Ganan（经代理）→ 登记为预期请求
        ledger.begin_scenario('H-ganan-fail', expected_requests=['ganan'])
        page.route(re.compile(r'ganan-finance\.com'), lambda route: route.abort())
        page.goto(URL, wait_until='domcontentloaded')
        # 先以官方源成功渲染一次，建立「旧 JPX 结论」状态
        search(page, '4519')
        wait_badge(page)
        page.wait_for_timeout(800)
        src_before = page.evaluate("() => (document.getElementById('srcLabel')||{}).textContent || ''")
        # 切到 Ganan 模式并重新查询（Ganan 请求被 abort → 失败）
        page.select_option('#srcSel', 'ganan')
        page.wait_for_timeout(600)
        search(page, '4519')
        page.wait_for_timeout(6000)
        src_label = page.evaluate("() => (document.getElementById('srcLabel')||{}).textContent || ''")
        lastd = page.evaluate("() => window.__lastD ? window.__lastD.sourceMeta : null")
        home = read_home(page)
        status_txt = page.evaluate("() => (document.getElementById('status')||{}).innerText || ''")
        check(src_label.strip() == '—' or 'JPX' not in src_label,
              'H：Ganan 失败后数据源标签不是 JPX 官方', src_label)
        check(lastd is None or lastd.get('isOfficial') is not True,
              'H：Ganan 失败后无残留 JPX 官方结论（__lastD 为 null 或非官方）', lastd)
        check('JPX 官方' not in home,
              'H：主页无残留「JPX 官方」字样', home[:80])
        check('Ganan' in status_txt,
              'H：状态明确提示 Ganan 失败（而非伪装 JPX）', status_txt[:80])
    except Exception as e:
        check(False, 'H 异常', str(e)[:120])
    finally:
        ledger.end_scenario()
        try:
            page.unroute(re.compile(r'ganan-finance\.com'))
        except Exception:
            pass

    page.screenshot(path='/tmp/verify_final.png', full_page=True)
    browser.close()

# ---------------------------------------------------------------- 汇总
print('\n' + '=' * 60)
_counts = CKS.counts()

# 1) 失败明细
for _st, _name, _detail, _sc in CKS.failures():
    print(f'  ❌ [{_sc}] {_name}'
          + (f'  → {_detail}' if _detail is not None else ''))

# 2) 四态分列统计（A4.2 §1 硬性要求）
print(f'\n断言统计（四态分离）：{CKS.summary_line()}')
print(f'  · PASS 只统计真实通过的断言')
print(f'  · SKIP / N-A **不计入 PASS**，其原因逐条列在下方')

# 3) 未执行原因审计（A4.2 §1 要求 5）
_notev = CKS.not_evaluated()
print(f'\n未执行 / 无法评估明细（N-A + SKIP，共 {len(_notev)} 条）：')
if not _notev:
    print('  （无）')
for _st, _name, _reason, _sc in _notev:
    print(f'  [{_st}] [{_sc}] {_name}')
    print(f'         原因：{_reason}')

# 4) 逐场景审计（A4.2 §1 要求 4：每个场景记录实际执行断言数）
print('\n逐场景断言数（审计）：')
print(f'  {"PASS":>5} {"FAIL":>5} {"SKIP":>5} {"N/A":>5} {"总计":>6}  场景')
_scen_empty = []
for _sc, _p, _f, _s, _n, _tot, _real in CKS.scenario_report():
    flag = '  （虚拟桶，非真实场景）' if not _real else ''
    if _tot == 0 and _real:
        flag = '  ← ⚠️ 本场景未产生任何断言（疑似整体跳过）'
        _scen_empty.append(_sc)
    print(f'  {_p:5d} {_f:5d} {_s:5d} {_n:5d} {_tot:6d}  {_sc}{flag}')
print(f'  登记场景 {len(CKS.scenario_order)} 个；'
      f'零断言场景 {len(_scen_empty)} 个；'
      f'记录总数 {len(CKS.records)} 条')
if _scen_empty:
    print('  ⚠️ 零断言场景明细：' + '；'.join(_scen_empty))

_ok, _why = CKS.verdict()
print(f'\n判定：{_why}')

# ---------------------------------------------------------------- 错误账本
# ★ A3.2.5：全部错误**如实记录**。判定只有两类：
#   expected    = 当前场景明确制造且可归因（哨兵 / 该场景预期失败请求）
#   unconfirmed = 无法归因 → 保留，按在线环境如实上报，绝不静默删除
print('\n错误账本：')
print('  ' + json.dumps(ledger.summary(), ensure_ascii=False))
if ledger.entries:
    print('  --- expected（已归因）---')
    for e in ledger.expected_entries()[:10]:
        print(f"   [{e['scenario']}] {e['kind']}: {e['text'][:80]}  ({e['reason']})")
else:
    print('  （无 JS 错误）')

# 未确认错误再细分为「环境退化」与「真实回归」——这是**如实分类上报**，
# 不再是 A3.2 那种"直接从致命集合里按文本删掉"。
env_errors, real_errors = ledger.online_status()

print('\n各股票结论：')
for r in results:
    print(f"  {r['code']:5} {r['badge']:26} pos={r['pos']:8} ca={r.get('caStatus','-'):9} "
          f"cmp={(r['cmp'] or {}).get('from','—')}→{(r['cmp'] or {}).get('to','—')} "
          f"src={r['src']}")

# ---------------------------------------------------------------- 在线状态
# ★ A3.2.5 §2/§7 + A4.2 §1/§6：在线 smoke 必须**独立报告**。
#   · hard_failed = 应用回归（真实缺陷）        → ONLINE_FAILED
#   · env_failed  = 因上游环境无法评估的断言    → ONLINE_DEGRADED
#   · real_errors = 非环境类未确认 JS 错误      → ONLINE_FAILED
#   · na_count    = N/A（无法评估，**不计PASS**）→ 至少 DEGRADED，绝不 SUCCESS
#
#   ★ A4.2 §6 硬性要求：**不得把 429/CORS 一律定性为无害环境错误**。
#   因此这里额外做数据源级归因（见下方「数据源健康」），逐源说明
#   成功 / 失败 / 失败后用什么数据 / 是否影响评级可信度 / 是否影响真实用户。
_failures = CKS.failures()
hard_failed = [c for c in _failures if c[1] not in UPSTREAM_SENSITIVE]
env_failed = [c for c in _failures if c[1] in UPSTREAM_SENSITIVE]
_na_recs = CKS.by_status(NA)
_skip_recs = CKS.by_status(SKIP)

if hard_failed or real_errors:
    online_status = 'ONLINE_FAILED'
elif env_failed or env_errors or _na_recs or _skip_recs:
    # ★ 关键修正：存在 N/A / SKIP 时**绝不能报SUCCESS**。
    #   之前 N/A 被伪装成 check(True) 混进 PASS，会让「未验证」显示成「已通过」。
    online_status = 'ONLINE_DEGRADED'
else:
    online_status = 'ONLINE_SUCCESS'

print('\n' + '=' * 60)
print(f'在线数据源 smoke 状态：{online_status}')
print(f'  应用回归失败 {len(hard_failed)} 条；上游不可评估失败 {len(env_failed)} 条；'
      f'N/A {len(_na_recs)} 条；SKIP {len(_skip_recs)} 条')
if hard_failed:
    print(f'  （应用回归 {len(hard_failed)} 条 —— 真实缺陷，必须修复）')
    for c in hard_failed:
        print(f"    [HARD] [{c[3]}] {c[1]}")
if env_failed:
    print(f'  （因上游环境无法评估的断言 {len(env_failed)} 条 —— 非应用缺陷，'
          f'对应行为已由离线确定性回归覆盖）')
    for c in env_failed:
        print(f"    [上游不可评估] {c[1]}")
if _na_recs:
    print(f'  （N/A {len(_na_recs)} 条：**未能评估，不计为通过**；'
          f'原因已在「未执行/无法评估明细」中逐条列出）')
if env_errors:
    print(f'  （环境类未确认错误 {len(env_errors)} 条：'
          f'如实上报，但**不等于无害**，见数据源健康分析）')
    for e in env_errors[:8]:
        print(f"    [env][{e['scenario']}] {e['text'][:100]}")
if real_errors:
    print(f'  （真实回归：非环境类未确认错误 {len(real_errors)} 条）')
    for e in real_errors[:8]:
        print(f"    [REAL][{e['scenario']}] {e['text'][:100]}")
print('=' * 60)

# 严格门：离线确定性回归见 verify_offline.py（零未确认错误，全绿）。
# 本在线套件：应用回归(断言失败/真实 JS 错误) → 失败退出；
#             仅上游环境退化 → 如实标注 ONLINE_DEGRADED，不谎称成功也不误判为回归。
if hard_failed or real_errors:
    print('\n❌ 验收未通过（ONLINE_FAILED）')
    sys.exit(1)
print(f'\n✅ 在线 smoke 通过（状态：{online_status}；'
      f'严格门由 verify_offline.py 承担）')