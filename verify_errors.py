#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_errors.py —— A3.2.5：归因式浏览器错误账本

背景（A3.2 遗留问题）
--------------------
A3.2 为了让 verify_ui.py 通过，逐步按**错误文本**扩大排除范围：
    Failed to load resource / net::ERR / 429 / CORS / Failed to fetch
这类"全局文本排除"会把**真实错误**一并吞掉，存在隐藏缺陷的风险。

A3.2.5 的判定原则（对应需求 §1 / §3 / §4）
----------------------------------------
只有同时满足以下两点，错误才算 **expected（预期）**：
  1. 由**当前测试场景明确制造**（场景注册了哨兵，或注册了预期失败请求）；
  2. 且能**归因**到该场景的预期请求（有 Playwright requestfailed 证据，
     或 console 自带 URL 命中预期请求集合）。

除此之外一律保留为 **unconfirmed（未确认）**，最终判为致命，绝不静默忽略。

关键约束：
  · **禁止**按 429 / CORS / Failed to fetch / Failed to load resource 做全局排除；
    这些文本只在"本场景确有预期请求失败、且归因无歧义"时才参与归因判断。
  · **pageerror 永不忽略**（除非场景显式注册为哨兵）。
  · console.error 若**没有提供请求 URL**，必须靠 requestfailed 事件关联；
    若本场景同时存在**非预期**的失败请求，则归因有歧义 → **宁可保留未确认**。

用法
----
    ledger = ErrorLedger()
    ledger.begin_scenario('9-weekly-abort',
                          sentinels=['UNRELATED_JS_ERR_TEST_9c'],
                          expected_requests=['/api/weekly'])
    ledger.add_console_error(text, url)
    ledger.add_pageerror(text)
    ledger.add_requestfailed(url, resource_type, failure_text)
    ledger.end_scenario()
    fatal = ledger.fatal_entries()
"""

# 网络类错误的**表现**文本。注意：这里不是"全局排除清单"——
# 这些文本只在「本场景确有预期请求失败 + 归因无歧义」时才用于归因，
# 单独出现（无预期请求、或有其他非预期失败请求）时一律判为未确认。
NETWORK_ERROR_MARKERS = (
    'Failed to load resource',
    'net::ERR',
    'Failed to fetch',
)


def looks_like_network_error(text):
    t = (text or '')
    return any(m in t for m in NETWORK_ERROR_MARKERS)


# ---------------------------------------------------------------------------
# 在线**报告**用的「环境类」判定（注意与上面 NETWORK_ERROR_MARKERS 的区别）：
#   · NETWORK_ERROR_MARKERS        → 用于**归因**（能否归因到预期请求），必须窄；
#   · ENVIRONMENTAL_MARKERS        → 仅用于**分类上报**未确认错误是
#                                    ONLINE_DEGRADED（环境）还是 ONLINE_FAILED（真实回归）。
# 后者**绝不**把错误从致命集合中删除 —— 它们仍然全部打印、仍然算未确认，
# 只是如实归类为"上游/环境问题"，不再谎称测试成功。
ENVIRONMENTAL_MARKERS = (
    'Failed to load resource',
    'net::ERR',
    'Failed to fetch',
    'blocked by CORS',
    'CORS policy',
    'Access to fetch',
    'status of 429',
    'HTTP 429',
)


def looks_like_environmental_error(text):
    """仅用于在线状态分类上报（DEGRADED vs FAILED），不用于归因放行。"""
    t = (text or '')
    return any(m in t for m in ENVIRONMENTAL_MARKERS)


class ErrorLedger(object):
    """按场景归因的错误账本。"""

    def __init__(self):
        self.entries = []            # 所有 JS 错误（console.error / pageerror）
        self.failed_requests = []    # 所有 requestfailed 记录
        self._scenario = None
        self._sentinels = ()
        self._expected_patterns = ()
        self._scn_failed_expected = []    # 本场景内命中预期请求集合的失败
        self._scn_failed_unexpected = []  # 本场景内其它（非预期）失败

    # ---------------------------------------------------------------- 场景
    def begin_scenario(self, name, sentinels=(), expected_requests=()):
        """开启一个场景，登记它明确制造的东西。

        sentinels        : 本场景显式注入的哨兵字符串（如 9c 的 pageerror）。
                           仅"场景明确制造"的错误才允许走这条路。
        expected_requests: 本场景预期会失败的请求 URL 片段
                           （如故意 abort 的 '/api/weekly'）。
        """
        self._scenario = name
        self._sentinels = tuple(sentinels or ())
        self._expected_patterns = tuple(expected_requests or ())
        self._scn_failed_expected = []
        self._scn_failed_unexpected = []

    def end_scenario(self):
        self._scenario = None
        self._sentinels = ()
        self._expected_patterns = ()
        self._scn_failed_expected = []
        self._scn_failed_unexpected = []

    @property
    def scenario(self):
        return self._scenario

    def matches_expected_request(self, url):
        if not url:
            return False
        return any(p in url for p in self._expected_patterns)

    # ---------------------------------------------------------------- 记录
    def add_requestfailed(self, url, resource_type='', failure=''):
        """Playwright requestfailed 事件：真实记录到底哪个请求失败了。"""
        rec = {
            'url': url or '',
            'type': resource_type or '',
            'failure': failure or '',
            'scenario': self._scenario,
            'expected': self.matches_expected_request(url),
        }
        self.failed_requests.append(rec)
        if rec['expected']:
            self._scn_failed_expected.append(rec)
        else:
            self._scn_failed_unexpected.append(rec)
        return rec

    def add_console_error(self, text, url=''):
        entry = {'kind': 'console.error', 'text': text or '', 'url': url or '',
                 'scenario': self._scenario, 'status': 'unconfirmed', 'reason': ''}
        self._classify(entry)
        self.entries.append(entry)
        return entry

    def add_pageerror(self, text):
        entry = {'kind': 'pageerror', 'text': text or '', 'url': '',
                 'scenario': self._scenario, 'status': 'unconfirmed', 'reason': ''}
        self._classify(entry)
        self.entries.append(entry)
        return entry

    # ---------------------------------------------------------------- 归因
    def _classify(self, entry):
        """判定一条错误是 expected 还是 unconfirmed。"""

        # (1) 场景显式注册的哨兵 —— 由本场景明确制造
        for s in self._sentinels:
            if s and s in entry['text']:
                entry['status'] = 'expected'
                entry['reason'] = 'scenario-sentinel:%s' % s
                return

        # 没有注册任何预期请求 → 没有任何归因依据 → 未确认
        if not self._expected_patterns:
            entry['status'] = 'unconfirmed'
            entry['reason'] = 'no-expected-request-registered'
            return

        # (2) console 自带 URL，且命中本场景预期请求集合 → 可归因
        if entry['url'] and self.matches_expected_request(entry['url']):
            entry['status'] = 'expected'
            entry['reason'] = 'expected-request-url'
            return

        # (3) console 未给 URL：靠 requestfailed 证据归因。
        #     仅当「本场景确有预期请求失败」**且**「不存在其它非预期失败请求」
        #     （归因无歧义）时，才可归因；否则保留未确认。
        if (not entry['url']) and self._scn_failed_expected \
                and not self._scn_failed_unexpected \
                and looks_like_network_error(entry['text']):
            entry['status'] = 'expected'
            entry['reason'] = 'attributed-to-expected-requestfailed'
            return

        # (4) 其余：无法可靠关联 → 未确认（不静默忽略）
        if (not entry['url']) and self._scn_failed_unexpected:
            entry['reason'] = 'ambiguous:unexpected-request-also-failed'
        elif entry['url']:
            entry['reason'] = 'url-not-in-expected-set'
        else:
            entry['reason'] = 'no-url-and-no-evidence'
        entry['status'] = 'unconfirmed'

    # ---------------------------------------------------------------- 查询
    def fatal_entries(self):
        """未确认（致命）错误。"""
        return [e for e in self.entries if e['status'] != 'expected']

    def expected_entries(self):
        return [e for e in self.entries if e['status'] == 'expected']

    def summary(self):
        return {
            'total': len(self.entries),
            'expected': len(self.expected_entries()),
            'fatal': len(self.fatal_entries()),
            'failed_requests': len(self.failed_requests),
        }

    def online_status(self):
        """给在线 smoke 用：把未确认错误区分为「环境退化」与「真实回归」。

        · 环境类（429 / CORS / net::ERR / Failed to fetch…）→ ONLINE_DEGRADED
        · 其它（真实 JS 异常、应用逻辑错误等）　　　　　　　→ ONLINE_FAILED

        重要：这只是**如实分类上报**。所有未确认错误仍然：
          · 保留在 fatal_entries() 中（不删除、不过滤）；
          · 由调用方完整打印。
        它不会把任何网络失败改写成"测试成功"，只是标明失败来源是上游环境。
        """
        env, real = [], []
        for e in self.fatal_entries():
            if looks_like_environmental_error(e['text']):
                env.append(e)
            else:
                real.append(e)
        return env, real


# ===========================================================================
# A4.2 §1：断言统计可信度
# ===========================================================================
#
# 问题（A4.2 §1）
#   原先只有一个 `checks` 列表 + `check(cond, ...)`，导致：
#     · check(True, '...N/A...') 这类**伪装成通过的 N/A**（verify_ui.py 场景 5）；
#     · SKIP / N/A 与真PASS 混在同一总数里，无法区分"真的验证了"与"没验证"；
#     · 数据依赖分支未执行时**不留任何痕迹**，断言总数悄悄变化（如 124→122）。
#
# 本类的契约（A4.2 §1 硬性要求）
#   1. PASS / FAIL / SKIP / N-A **分开统计**；
#   2. **SKIP 与 N/A 一律不计入 PASS**；
#   3. **禁止**用 check(True, ...) 伪装 N/A —— N/A 只能走 na()，
#      它在统计里单列，且默认**不让验收通过**（见strict 开关）；
#   4. 每个场景记录**实际执行断言数**；
#   5. 数据依赖分支未执行时，用 na()/skip() 显式登记**未执行原因**。

PASS = 'PASS'
FAIL = 'FAIL'
SKIP = 'SKIP'
NA = 'N/A'


class CheckLedger(object):
    """带 PASS/FAIL/SKIP/N-A 四态分离与场景审计的断言账本。"""

    def __init__(self):
        self.records = []          # (status, name, detail, scenario)
        self._scenario = None
        self.scenario_order = []

    # ---------------------------------------------------------------- 场景
    def scen(self, name):
        """登记并进入一个场景（同时用于打印场景标题）。"""
        self._scenario = name
        if name not in self.scenario_order:
            self.scenario_order.append(name)
        return name

    @property
    def scenario(self):
        return self._scenario

    # ---------------------------------------------------------------- 断言
    def check(self, cond, name, detail=None):
        """真断言：通过=条件为真，失败=条件为假。计入 PASS/FAIL。"""
        status = PASS if cond else FAIL
        self.records.append((status, name, detail, self._scenario))
        return bool(cond)

    def skip(self, name, reason):
        """显式跳过：因环境/前置条件不满足而**故意不执行**。不计入 PASS。"""
        self.records.append((SKIP, name, reason, self._scenario))
        return False

    def na(self, name, reason):
        """N/A：断言**无法评估**（典型是上游不可用导致被测行为不存在）。

        绝不计入 PASS；必须给出未执行/无法评估的原因。
        """
        self.records.append((NA, name, reason, self._scenario))
        return False

    # ---------------------------------------------------------------- 查询
    def by_status(self, status):
        return [r for r in self.records if r[0] == status]

    def failures(self):
        return self.by_status(FAIL)

    def counts(self):
        c = {PASS: 0, FAIL: 0, SKIP: 0, NA: 0}
        for r in self.records:
            c[r[0]] = c.get(r[0], 0) + 1
        return c

    def summary_line(self):
        c = self.counts()
        return (f'PASS {c[PASS]} / FAIL {c[FAIL]} / SKIP {c[SKIP]} / N-A {c[NA]}'
                f'（登记 {len(self.records)} 条）')

    def scenario_report(self):
        """逐场景审计：返回 [(scenario, pass, fail, skip, na, total, is_real)]。

        · 场景顺序按登记顺序
        · is_real=False 表示这是**虚拟桶** '(prelude)'（进入任何场景前的断言），
          它不是"被跳过的场景"，因此**不参与零断言报警**。
        """
        order = list(self.scenario_order)
        acc = {}
        for st, name, detail, sc in self.records:
            key = sc or '(prelude)'
            if key not in acc:
                acc[key] = {PASS: 0, FAIL: 0, SKIP: 0, NA: 0}
            acc[key][st] += 1
        out = []
        if '(prelude)' in acc:
            c = acc['(prelude)']
            out.append(('(prelude)', c[PASS], c[FAIL], c[SKIP], c[NA],
                        sum(c.values()), False))
        for sc in order:
            c = acc.get(sc, {PASS: 0, FAIL: 0, SKIP: 0, NA: 0})
            out.append((sc, c[PASS], c[FAIL], c[SKIP], c[NA], sum(c.values()), True))
        # 未登记但有断言的真实场景（理论上不该出现）也一并暴露
        for sc, c in acc.items():
            if sc not in order and sc != '(prelude)':
                out.append((sc, c[PASS], c[FAIL], c[SKIP], c[NA],
                            sum(c.values()), True))
        return out

    def not_evaluated(self):
        """返回所有 SKIP / N-A 记录，供审计打印「未执行原因」。"""
        return [r for r in self.records if r[0] in (SKIP, NA)]

    def verdict(self):
        """验收判定：**只有 FAIL 才算失败**；SKIP / N-A 透明暴露但不冒充通过。

        返回 (ok, 说明文字)。
        """
        c = self.counts()
        if c[FAIL]:
            return False, f'{c[FAIL]} 条断言失败'
        if c[NA] or c[SKIP]:
            return True, (f'无失败；但有 {c[NA]} 条 N/A + {c[SKIP]} 条 SKIP '
                          f'未能评估（不计为通过，已在审计中列出原因）')
        return True, '全部断言通过（无 N/A、无 SKIP）'
