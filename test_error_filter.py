#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_error_filter.py —— A3.2.5 §4：负向测试（证明错误检测真的有效）

目的：证明 verify_errors.ErrorLedger **确实能让验收失败**，
而不是靠"按文本全局排除"把错误抹平。

覆盖需求：
  A. 非预期 console.error        → 必须判为致命（验收失败）
  B. 非预期 pageerror            → 必须判为致命
  C. weekly abort 场景里同时制造**另一个无关 API 错误** → 不得被忽略
  D. 不存在对 429 / CORS / Failed to fetch 的全局忽略（E 的核心）
  E. 以上文本单独出现、无归因依据时 → 一律致命

同时给出正对照：
  · 场景注册预期请求('/api/weekly') + requestfailed 证据 → 该错误可被归因（非致命）
  · 场景注册哨兵 → 该哨兵可被归因（非致命）

运行：python test_error_filter.py      （失败 exit 1）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_errors import (ErrorLedger, CheckLedger,
                          looks_like_network_error, PASS, FAIL, SKIP, NA)

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


# ================================================================ A
print('\n===== A：非预期 console.error 必须致命 =====')
lg = ErrorLedger()
lg.begin_scenario('A-normal')          # 没有注册任何预期请求 / 哨兵
lg.add_console_error('UNEXPECTED_REAL_BUG_AAA', url='')
ok(len(lg.fatal_entries()) == 1, 'A：无预期注册时 console.error → 致命',
   lg.entries[0]['reason'])
ok(lg.entries[0]['status'] == 'unconfirmed', 'A：状态为 unconfirmed')
lg.end_scenario()

# ================================================================ B
print('\n===== B：非预期 pageerror 必须致命（pageerror 永不忽略） =====')
lg = ErrorLedger()
lg.begin_scenario('B-normal')
lg.add_pageerror('UNEXPECTED_REAL_PAGEERROR_BBB')
ok(len(lg.fatal_entries()) == 1, 'B：无预期注册时 pageerror → 致命')
lg.end_scenario()

# 即便在"有预期请求"的场景里，未注册的 pageerror 也不能被顺带吞掉
lg = ErrorLedger()
lg.begin_scenario('B-in-abort', expected_requests=['/api/weekly'])
lg.add_requestfailed('http://127.0.0.1:8848/api/weekly?code=4519', 'fetch', 'net::ERR_FAILED')
lg.add_pageerror('SOME_OTHER_REAL_EXCEPTION_BBB')
ok(len(lg.fatal_entries()) == 1,
   'B：即便在 weekly-abort 场景，未注册 pageerror 仍致命（pageerror 永不忽略）',
   lg.entries[0]['reason'])
lg.end_scenario()

# ================================================================ C
print('\n===== C：weekly abort 场景 + 无关 API 错误 → 不得被忽略 =====')
lg = ErrorLedger()
lg.begin_scenario('C-abort-plus-unrelated', expected_requests=['/api/weekly'])
# 预期：我们故意 abort 的 /api/weekly
lg.add_requestfailed('http://127.0.0.1:8848/api/weekly?code=4519', 'fetch', 'net::ERR_FAILED')
# 无关：另一个 API（/api/margin）也失败了 —— 这不是本场景预期制造的
lg.add_requestfailed('http://127.0.0.1:8848/api/margin?code=4519', 'fetch', 'net::ERR_FAILED')
# 无关 API 的 console 错误，带自己的 URL
lg.add_console_error('Failed to load resource: net::ERR_FAILED',
                     url='http://127.0.0.1:8848/api/margin?code=4519')
ok(len(lg.fatal_entries()) == 1, 'C：无关 API(/api/margin) 错误未被忽略 → 致命',
   [(e['status'], e['reason']) for e in lg.entries])
f = lg.fatal_entries()[0]
ok(f['reason'] == 'url-not-in-expected-set', 'C：因 URL 不在预期集合而被判未确认', f['reason'])
lg.end_scenario()

# C 变体：无关 API 的 console 错误**没有** URL，但场景里已存在非预期失败请求
#        → 归因有歧义，宁可保留未确认
lg = ErrorLedger()
lg.begin_scenario('C-abort-plus-unrelated-nourl', expected_requests=['/api/weekly'])
lg.add_requestfailed('http://127.0.0.1:8848/api/weekly?code=4519', 'fetch', 'net::ERR_FAILED')
lg.add_requestfailed('http://127.0.0.1:8848/api/margin?code=4519', 'fetch', 'net::ERR_FAILED')
lg.add_console_error('Failed to load resource: net::ERR_FAILED', url='')
ok(len(lg.fatal_entries()) == 1,
   'C：URL 缺失且存在非预期失败 → 归因歧义，保留未确认（不静默忽略）',
   [(e['status'], e['reason']) for e in lg.entries])
ok(lg.fatal_entries()[0]['reason'] == 'ambiguous:unexpected-request-also-failed',
   'C：判因 = 存在非预期失败请求导致歧义', lg.fatal_entries()[0]['reason'])
lg.end_scenario()

# ================================================================ D/E
print('\n===== E：不允许全局忽略 429 / CORS / Failed to fetch / Failed to load resource =====')
E_TEXTS = [
    ('Failed to load resource: the server responded with a status of 429 ()', '429'),
    ("Access to fetch at 'https://api.codetabs.com/...' has been blocked by CORS policy", 'CORS'),
    ('TypeError: Failed to fetch', 'Failed to fetch'),
    ('Failed to load resource: net::ERR_FAILED', 'Failed to load resource'),
]
for txt, label in E_TEXTS:
    lg = ErrorLedger()
    lg.begin_scenario('E-no-attribution')     # 无任何预期注册
    lg.add_console_error(txt, url='')
    ok(len(lg.fatal_entries()) == 1,
       f'E：「{label}」无归因依据时不得被全局忽略 → 致命',
       lg.entries[0]['reason'])
    lg.end_scenario()

# E 补充：即便文本是网络类，只要 URL 不属于预期集合，也不能归因
lg = ErrorLedger()
lg.begin_scenario('E-wrong-url', expected_requests=['/api/weekly'])
lg.add_requestfailed('http://127.0.0.1:8848/api/weekly?code=4519', 'fetch', 'net::ERR_FAILED')
lg.add_console_error('Failed to load resource: the server responded with a status of 429 ()',
                     url='https://api.codetabs.com/v1/proxy/?quest=yahoo')
ok(len(lg.fatal_entries()) == 1,
   'E：网络类错误但 URL 不在预期集合 → 不得归因（不能靠文本放过）',
   lg.entries[0]['reason'])
lg.end_scenario()

# ================================================================ 正对照
print('\n===== 正对照：预期 abort 的错误可被正常归因（不误报） =====')
lg = ErrorLedger()
lg.begin_scenario('POS-weekly-abort', expected_requests=['/api/weekly'])
lg.add_requestfailed('http://127.0.0.1:8848/api/weekly?code=4519', 'fetch', 'net::ERR_FAILED')
lg.add_console_error('Failed to load resource: net::ERR_FAILED', url='')
ok(len(lg.fatal_entries()) == 0,
   '正对照：唯一预期请求失败时，URL 缺失的同类错误可归因 → 非致命',
   [(e['status'], e['reason']) for e in lg.entries])
lg.end_scenario()

lg = ErrorLedger()
lg.begin_scenario('POS-url-match', expected_requests=['/api/weekly'])
lg.add_console_error('Failed to load resource: net::ERR_FAILED',
                     url='http://127.0.0.1:8848/api/weekly?code=4519')
ok(len(lg.fatal_entries()) == 0, '正对照：console URL 命中预期请求 → 归因成功')
lg.end_scenario()

lg = ErrorLedger()
lg.begin_scenario('POS-sentinel', sentinels=['UNRELATED_JS_ERR_TEST_9c'])
lg.add_pageerror('Error: UNRELATED_JS_ERR_TEST_9c')
ok(len(lg.fatal_entries()) == 0, '正对照：显式注册哨兵 → 归因成功（非致命）')
lg.end_scenario()

# ================================================================ F（A4.2 §1 四态账本）
print('\n===== F：A4.2 §1 —— PASS/FAIL/SKIP/N-A 四态分离 =====')
ck = CheckLedger()
ck.scen('F-demo')
ok(ck.check(True, '真断言通过') is True, 'F：check(True) → PASS')
ok(ck.check(False, '真断言失败') is False, 'F：check(False) → FAIL')
ok(ck.na('无法评估', reason='上游 DEGRADED') is False, 'F：na() 返回 False（不冒充通过）')
ok(ck.skip('故意跳过', reason='前置条件不满足') is False, 'F：skip() 返回 False')

_c = ck.counts()
ok(_c[PASS] == 1, 'F：PASS 只统计真实通过（na/skip 不计入）', _c)
ok(_c[FAIL] == 1, 'F：FAIL 独立统计', _c)
ok(_c[NA] == 1, 'F：N-A 单列统计', _c)
ok(_c[SKIP] == 1, 'F：SKIP 单列统计', _c)
ok(len(ck.records) == 4, 'F：四条记录全部留痕（可审计）', len(ck.records))
ok(len(ck.not_evaluated()) == 2, 'F：未评估项可完整列出（含原因）',
   [(r[0], r[1], r[2]) for r in ck.not_evaluated()])
# F1b：干净账本（无 FAIL）+ 只有 N/A → verdict 通过但**明确标注未评估**，
#      绝不能显示成「全部通过」。
ck1b = CheckLedger()
ck1b.scen('F1b')
ck1b.check(True, '真实通过的一条')
ck1b.na('无法评估的一条', reason='上游 DEGRADED')
_ok1b, _why1b = ck1b.verdict()
ok(_ok1b is True, 'F1b：无 FAIL 时 verdict 为通过（N/A 不误判为失败）', _why1b)
ok('N/A' in _why1b, 'F1b：verdict 说明中明确标注 N-A 未评估', _why1b)
ok('全部断言通过' not in _why1b,
   'F1b：存在 N/A 时**不得**显示「全部断言通过」（防伪装）', _why1b)
# 反证：ck 里故意放了 check(False)，verdict 必须判失败
_ok, _why = ck.verdict()
ok(_ok is False, 'F：ck 含故意 FAIL → verdict 必须失败', _why)

# F2：FAIL 必须导致 verdict 失败
ck2 = CheckLedger()
ck2.scen('F2-demo')
ck2.check(False, '真实回归')
_ok2, _why2 = ck2.verdict()
ok(_ok2 is False, 'F2：存在 FAIL 时 verdict 必须为失败（不可被 N/A 掩盖）', _why2)

# F3：N/A 不得让「零断言场景」被误认为已验证
ck3 = CheckLedger()
ck3.scen('S1')
ck3.check(True, 'a')
ck3.scen('S2')
ck3.na('b', reason='上游不可用')
ck3.scen('S3-empty')
_rep = ck3.scenario_report()
_map = {r[0]: r for r in _rep}
ok(_map['S3-empty'][5] == 0, 'F3：零断言场景在报告中总计为 0（可被审计发现）', _rep)
ok(_map['S2'][4] == 1, 'F3：N-A 计入该场景的 N/A 列而非 PASS', _map['S2'])
ok(_map['S2'][1] == 0, 'F3：N-A 场景的 PASS 数为 0（不算通过）', _map['S2'])

# F4：非预期错误必须导致失败（与 ErrorLedger 联动）
lg2 = ErrorLedger()
lg2.begin_scenario('F4-normal')            # 无任何预期注册
lg2.add_console_error('UNEXPECTED_FATAL_FFF', url='')
ck4 = CheckLedger()
ck4.scen('F4')
ck4.check(len(lg2.fatal_entries()) == 0, 'F4：非预期错误 → 非致命集合非空 → 断言失败', None)
_ok4, _why4 = ck4.verdict()
ok(_ok4 is False, 'F4：非预期错误导致 verdict 失败', _why4)

# ================================================================ 汇总
print('\n' + '=' * 60)
print(f'断言统计（四态分离）：PASS {pass_n} / FAIL {fail_n}')
print(f'（本文件所有断言均为真断言，无N/A / 无 SKIP）')
if fail_n:
    print('\n❌ 负向测试未通过（说明错误过滤仍会放过真实错误）')
    sys.exit(1)
print('\n✅ 负向测试通过：非预期错误确实会使验收失败')
