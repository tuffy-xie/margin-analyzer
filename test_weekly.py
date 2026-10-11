"""test_weekly.py —— 周次信用残趋势（4W/8W/13W）回归测试 (A2 严格自然周契约)

覆盖用户指定的 A~M：
  A. 连续 5 快照 → 真正 4 个自然周 → w4 available
  B. 5 快照但中间缺一周 → w4 unavailable(missing_week)
  C. 目标起点存在但窗口内部缺周 → unavailable
  D. 周五节假日、周四 snapshot → 该自然周仍存在（不算 missing）
  E. 连续 9 周 → w8 available
  F. 连续 14 周 → w13 available
  G. 14 周历史写入 cache，下一次仅最近 8 周 → coverageWeeks 仍 >=14、w13 仍可用
  H. 同 weekKey：Ganan 周五(snap 更晚) > JPX 周一
  I. 同 weekKey：Ganan 周五 < JPX 周五（同日 JPX 优先）
  J. 同 weekKey 旧 snapshotDate 较早、后续较晚 → 覆盖更新
  K. current week provisional 正确（可参与但标记暫定）
  L. 跨 corporate action：拆股保护不回归
  M. 缺周时绝不：用更早 snapshot 顶替（fromDate/buyPct 必为 None）
  + 历史 cache / 单飞 / 原子写 / 并发
  + 真实 Ganan HTML 解析（离线夹具）

运行：python test_weekly.py
"""
import sys
import os
import json
import shutil
import tempfile
import threading
import datetime
import time
import html

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weekly

pass_n = 0
fail_n = 0
skip_n = 0          # ★ A4.3 §3：跳过必须**单独计数**，绝不计入 PASS


def ok(cond, name, extra=None):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print("  ✅ " + name)
    else:
        fail_n += 1
        print("  ❌ " + name + (f" → {extra}" if extra is not None else ""))


def skipped(name, reason):
    """登记一条因缺少外部夹具而未执行的断言。**不计入 PASS**。"""
    global skip_n
    skip_n += 1
    print(f"  ⏭️  SKIP：{name} —— {reason}")


# ---------------------------------------------------------------- 合成序列工具
def _fridays(start_iso, n):
    d = datetime.date.fromisoformat(start_iso)
    return [(d + datetime.timedelta(days=7 * i)).isoformat() for i in range(n)]


def snap(wk, sd, buy, sell, source="ganan", ratio=None, splitConfirmed=None):
    """构造一条标准 weekly snapshot（weekKey + snapshotDate 双日期）。"""
    if ratio is None:
        ratio = round(buy / sell, 2) if sell else None
    s = {"weekKey": wk, "snapshotDate": sd, "buy": buy, "sell": sell,
         "ratio": ratio, "source": source}
    if splitConfirmed is not None:
        s["splitConfirmed"] = bool(splitConfirmed)
    return s


def _snaps(week_keys, b0=100000, s0=10000, step=1000, sstep=100):
    """连续周五快照：weekKey == snapshotDate == 该周五。"""
    return [snap(wk, wk, b0 + i * step, s0 + i * sstep, source="ganan")
            for i, wk in enumerate(week_keys)]


def _build(week_keys, as_of, events=None, snaps=None):
    """用连续周五 weekKeys 跑 compute_trends（as_of 远未来 → 无 provisional）。"""
    s = snaps if snaps is not None else _snaps(week_keys)
    series = weekly.merge_weekly([], s, as_of=as_of)
    return weekly.compute_trends(series, events or [], as_of=as_of)


AS_OF_FUTURE = datetime.date(2026, 12, 31)

# ================================================================ A. 连续 5 周 → 4W
print("=== A. 连续 5 快照 → 真正 4 个自然周 → w4 available ===")
wks_a = _fridays("2026-09-11", 5)   # 09-11/09-18/09-25/10-02/10-09
t = _build(wks_a, AS_OF_FUTURE)
ok(t["w4"]["available"] is True, "w4 available")
ok(t["w4"]["fromWeekKey"] == wks_a[0] and t["w4"]["toWeekKey"] == wks_a[-1],
   "w4 端点 = 09-11 → 10-09（精确 4 周）", (t["w4"].get("fromWeekKey"), t["w4"].get("toWeekKey")))
ok(t["w4"]["buyPct"] is not None, "w4 百分比真实计算（非 None）")
ok(t["w8"]["available"] is False
   and t["w8"]["reason"] == weekly.REASON_INSUFFICIENT_HISTORY,
   "w8 数据不足 → insufficient_history")

# ================================================================ B. 中间缺一周 → missing_week
print("\n=== B. 5 快照但中间缺一周 → w4 unavailable(missing_week) ===")
# 08-28, 09-04, 09-11, 09-25, 10-02（缺 09-18）
bkeys = ["2026-08-28", "2026-09-04", "2026-09-11", "2026-09-25", "2026-10-02"]
t = _build(bkeys, AS_OF_FUTURE)
ok(t["w4"]["available"] is False, "w4 不可用")
ok(t["w4"]["reason"] == weekly.REASON_MISSING_WEEK, "reason = missing_week", t["w4"].get("reason"))
ok(t["w4"]["missingWeeks"] == ["2026-09-18"], "missingWeeks = [09-18]", t["w4"].get("missingWeeks"))
# 这就是原 Codex 报告的问题：旧实现会静默把 35 天当 4W

# ================================================================ C. 目标存在但窗口内部缺周
print("\n=== C. 目标起点存在但窗口内部缺周 → unavailable ===")
# 09-11, 09-25, 10-02, 10-09（缺 09-18）；4W 目标=09-11 存在，但内部缺 09-18
ckeys = ["2026-09-11", "2026-09-25", "2026-10-02", "2026-10-09"]
t = _build(ckeys, AS_OF_FUTURE)
ok(t["w4"]["available"] is False, "w4 不可用（尽管 09-11 与 10-09 都存在）")
ok(t["w4"]["reason"] == weekly.REASON_MISSING_WEEK, "reason = missing_week", t["w4"].get("reason"))
ok(t["w4"]["missingWeeks"] == ["2026-09-18"], "缺失的是内部 09-18", t["w4"].get("missingWeeks"))

# ================================================================ D. 节假日周四 snapshot 仍算存在
print("\n=== D. 周五节假日、周四 snapshot → 该自然周仍存在 ===")
d_snaps = [
    snap("2026-09-11", "2026-09-11", 100000, 10000),
    snap("2026-09-18", "2026-09-18", 101000, 10100),
    snap("2026-09-25", "2026-09-25", 102000, 10200),
    snap("2026-10-02", "2026-10-02", 103000, 10300),
    snap("2026-10-09", "2026-10-08", 104000, 10400),   # 节假日：weekKey 周五 / snapshot 周四
]
series = weekly.merge_weekly([], d_snaps, as_of=AS_OF_FUTURE)
t = weekly.compute_trends(series, [], as_of=AS_OF_FUTURE)
ok(t["w4"]["available"] is True, "w4 available（周四快照周仍算存在）")
ok(t["w4"]["toWeekKey"] == "2026-10-09", "toWeekKey 为周五锚点 10-09", t["w4"].get("toWeekKey"))
ok(t["w4"]["toDate"] == "2026-10-08", "toDate 反映真实数据日 10-08（非伪造周五）", t["w4"].get("toDate"))
ok("2026-10-09" in [s["weekKey"] for s in series], "weekKey 10-09 在序列中（未当 missing）")

# ================================================================ E. 连续 9 周 → w8
print("\n=== E. 连续 9 周 → w8 available ===")
eks = _fridays("2026-08-07", 9)
t = _build(eks, AS_OF_FUTURE)
ok(t["w8"]["available"] is True, "w8 available")
ok(t["w8"]["fromWeekKey"] == eks[0] and t["w8"]["toWeekKey"] == eks[-1],
   "w8 端点 = 首 → 尾（精确 8 周）")
ok(t["w13"]["available"] is False, "w13 仍不足")

# ================================================================ F. 连续 14 周 → w13
print("\n=== F. 连续 14 周 → w13 available ===")
fks = _fridays("2026-07-03", 14)
t = _build(fks, AS_OF_FUTURE)
ok(t["w13"]["available"] is True, "w13 available")
ok(t["w13"]["fromWeekKey"] == fks[0] and t["w13"]["toWeekKey"] == fks[-1],
   "w13 端点 = 首 → 尾（精确 13 周）")

# ================================================================ G. 历史 cache 真正积累
print("\n=== G. 历史 cache：14 周 → 仅 8 周输入，coverage 不缩水 ===")
SANDBOX_G = tempfile.mkdtemp(prefix="weekly_hist_test_")
real_get = weekly.http_get
weekly.http_get = lambda *a, **k: ""   # 关闭真实 Ganan 网络，仅测 JPX 历史积累
try:
    all14 = _fridays("2026-01-02", 14)
    daily14 = [{"date": wk, "buy": 1_000_000 + i * 10000, "sell": 100_000 + i * 1000,
                "ratio": round((1_000_000 + i * 10000) / (100_000 + i * 1000), 2)}
               for i, wk in enumerate(all14)]
    r1 = weekly.build_weekly_response("9999", daily14, cache_dir=SANDBOX_G,
                                      as_of=datetime.date(2026, 12, 31))
    ok(r1["coverageWeeks"] == 14, "首次 14 周输入 → coverageWeeks=14", r1["coverageWeeks"])
    ok(r1["trends"]["w13"]["available"] is True, "首次 w13 available")

    last8 = all14[6:]   # 仅最近 8 周
    daily8 = [{"date": wk, "buy": 1_000_000 + (6 + i) * 10000, "sell": 100_000 + (6 + i) * 1000,
               "ratio": round((1_000_000 + (6 + i) * 10000) / (100_000 + (6 + i) * 1000), 2)}
              for i, wk in enumerate(last8)]
    r2 = weekly.build_weekly_response("9999", daily8, cache_dir=SANDBOX_G,
                                      as_of=datetime.date(2026, 12, 31))
    ok(r2["coverageWeeks"] >= 14, "二次仅 8 周输入 → coverageWeeks 仍 >=14", r2["coverageWeeks"])
    ok(r2["trends"]["w13"]["available"] is True, "二次 w13 仍可用（历史未删）")
finally:
    weekly.http_get = real_get
    shutil.rmtree(SANDBOX_G, ignore_errors=True)

# ================================================================ H. 同 weekKey：Ganan 周五 > JPX 周一
print("\n=== H. 同 weekKey：Ganan 周五(snap 更晚) > JPX 周一 ===")
wk = "2026-10-09"
ganan_fri = [snap(wk, "2026-10-09", 8000000, 600000, source="ganan")]
jpx_mon = [snap(wk, "2026-09-28", 9500000, 720000, source="jpx")]  # 周一→同周 10-09
m = weekly.merge_weekly(jpx_mon, ganan_fri, as_of=AS_OF_FUTURE)
ok(len(m) == 1 and m[0]["source"] == "ganan", "Ganan 周五（snapshotDate 更晚）胜出", m[0]["source"] if m else None)
ok(m[0]["buy"] == 8000000, "采用 Ganan 的 buy", m[0]["buy"] if m else None)

# ================================================================ I. 同 weekKey：同日 JPX 优先
print("\n=== I. 同 weekKey：Ganan 周五 < JPX 周五（同日 JPX 优先）===")
ganan_fri = [snap(wk, "2026-10-09", 8000000, 600000, source="ganan")]
jpx_fri = [snap(wk, "2026-10-09", 9500000, 720000, source="jpx")]
m = weekly.merge_weekly(jpx_fri, ganan_fri, as_of=AS_OF_FUTURE)
ok(len(m) == 1 and m[0]["source"] == "jpx", "同日 → JPX 优先", m[0]["source"] if m else None)
ok(m[0]["buy"] == 9500000, "采用 JPX 的 buy", m[0]["buy"] if m else None)

# ================================================================ J. 同 weekKey 更晚 snapshot 覆盖旧值
print("\n=== J. 同 weekKey 旧 snapshotDate 较早 → 后续较晚覆盖 ===")
old = [snap("2026-10-02", "2026-10-02", 700000, 50000, source="ganan")]
new = [snap("2026-10-02", "2026-10-09", 710000, 51000, source="ganan")]  # 更晚
m = weekly.merge_series(old, new, as_of=AS_OF_FUTURE)
ok(len(m) == 1 and m[0]["snapshotDate"] == "2026-10-09", "更晚 snapshotDate 覆盖旧值", m[0]["snapshotDate"] if m else None)
ok(m[0]["buy"] == 710000, "buy 更新为更晚值", m[0]["buy"] if m else None)

# ================================================================ K. current week provisional
print("\n=== K. current week provisional ===")
sn = [snap("2026-10-09", "2026-10-09", 900000, 80000, source="jpx")]
s_now = weekly.merge_weekly([], sn, as_of=datetime.date(2026, 10, 7))   # 周三，末周未结束
ok(s_now[0]["provisional"] is True, "as_of 周三 → provisional=True")
t_now = weekly.compute_trends(s_now, [], as_of=datetime.date(2026, 10, 7))
ok(t_now["w4"]["provisional"] is True, "趋势带 provisional=True")

s_past = weekly.merge_weekly([], sn, as_of=datetime.date(2026, 10, 16))
ok(s_past[0]["provisional"] is False, "as_of 两周后 → provisional=False")

# 完整可用 + 当前周 provisional：可参与但标记暫定
wks5 = _fridays("2026-09-11", 5)
s5 = weekly.merge_weekly([], _snaps(wks5), as_of=datetime.date(2026, 10, 7))  # 周三，末周进行中
t5 = weekly.compute_trends(s5, [], as_of=datetime.date(2026, 10, 7))
ok(t5["w4"]["available"] is True, "5 周且当前周进行中 → w4 仍可比较")
ok(t5["w4"]["provisional"] is True, "w4 标记 provisional=True（暫定）")
ok(s5[-1]["provisional"] is True, "末周快照 provisional=True")

# ================================================================ L. 跨 corporate action：保护不回归
print("\n=== L. 跨 corporate action：拆股保护不回归 ===")
base = _snaps(_fridays("2026-01-02", 12))
for k in ("buy", "sell"):
    base[6][k] = base[6][k] * 4
base[6]["ratio"] = round(base[6]["buy"] / base[6]["sell"], 2)   # 比值守恒
series = weekly.merge_weekly([], base, as_of=AS_OF_FUTURE)
events = weekly.detect_corporate_actions(series)
ok("2026-02-13" in events, "检测到拆股事件(2026-02-13)", events)
t = weekly.compute_trends(series, events, as_of=AS_OF_FUTURE)
ok(t["w8"]["corporateActionAffected"] is True, "w8 区间含拆股 → affected=True")
ok(t["w8"]["available"] is False
   and t["w8"]["reason"] == weekly.REASON_CORPORATE_ACTION,
   "w8 因公司行动停止比较（reason=corporate_action）", t["w8"].get("reason"))
ok(t["w4"]["corporateActionAffected"] is False, "w4 区间不含拆股 → 不受影响")
# 普通有机增长（仅买残涨，ratio 变）→ 不应误报
organic = _snaps(_fridays("2026-03-06", 8))
organic[4]["buy"] = organic[4]["buy"] * 4
organic[4]["ratio"] = round(organic[4]["buy"] / organic[4]["sell"], 2)
ev2 = weekly.detect_corporate_actions(weekly.merge_weekly([], organic, as_of=AS_OF_FUTURE))
ok(ev2 == [], "仅买残跳变(ratio 失恒)不误报拆股", ev2)

# ================================================================ M. 缺周时绝不：用更早 snapshot 顶替
print("\n=== M. 缺周时绝不：用更早 snapshot 顶替 ===")
mkeys = ["2026-08-28", "2026-09-04", "2026-09-11", "2026-09-25", "2026-10-02"]
t = _build(mkeys, AS_OF_FUTURE)
ok(t["w4"]["available"] is False, "w4 不可用")
ok(t["w4"]["fromDate"] is None, "fromDate 为 None（未用更早 snapshot 顶替）", t["w4"].get("fromDate"))
ok(t["w4"]["toDate"] is None, "toDate 为 None（未伪造日期区间）", t["w4"].get("toDate"))
ok(t["w4"]["buyPct"] is None and t["w4"]["sellPct"] is None, "百分比为 None（不伪造）")
ok(t["w4"]["ratioFrom"] is None and t["w4"]["ratioTo"] is None, "ratio 为 None（不伪造）")

# ================================================================ 历史 cache / 单飞 / 原子写 / 并发
print("\n=== 历史 cache 与并发 ===")
SANDBOX = tempfile.mkdtemp(prefix="weekly_cache_test_")
_buy = [["日付", "買残", "信用倍率"],
        ["2026-10-02", 9324600, 13.4], ["2026-09-25", 8958000, 12.7]]
_sell = [["日付", "売残", "信用倍率"],
         ["2026-10-02", 693900, 13.4], ["2026-09-25", 705500, 12.7]]
GAN_FIXTURE = ('data-rows="' + html.escape(json.dumps(_buy), quote=True) + '" '
               'data-rows="' + html.escape(json.dumps(_sell), quote=True) + '"')
real_get = weekly.http_get
get_calls = []
get_lock = threading.Lock()


def slow_get(url, timeout=30, binary=False):
    with get_lock:
        get_calls.append(url)
    time.sleep(0.2)                       # 制造并发窗口
    return GAN_FIXTURE


weekly.http_get = slow_get
try:
    # 单飞：多次调用只应抓一次 Ganan
    r1 = weekly.build_weekly_response("4519", [], cache_dir=SANDBOX)
    r2 = weekly.build_weekly_response("4519", [], cache_dir=SANDBOX)
    ok(r1["coverageWeeks"] == 2, "Ganan 夹具合并后 coverageWeeks=2", r1["coverageWeeks"])
    ok(len(get_calls) == 1, "单飞：Ganan 只抓取一次", len(get_calls))
    cpath = os.path.join(SANDBOX, "4519_weekly.json")
    ok(os.path.exists(cpath), "merged 缓存文件已写入")
    with open(cpath, encoding="utf-8") as f:
        cached = json.load(f)
    ok(cached.get("weeklyParserVersion") == weekly.WEEKLY_PARSER_VERSION, "缓存含版本闸门")
    ok(cached.get("rows") is not None, "缓存含 rows")
    ok(cached["code"] == "4519", "缓存 code 正确")

    # 并发：N 线程同时请求同一 code
    get_calls.clear()
    results = {}
    errors = []

    def worker(tag):
        try:
            results[tag] = weekly.build_weekly_response("7974", [], cache_dir=SANDBOX)
        except Exception as e:
            errors.append(f"{tag}: {e}")

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    ok(not errors, "并发无异常逃逸", errors)
    ok(len(results) == 6, "全部线程拿到响应", list(results.keys()))
    ok(all(r.get("rows") for r in results.values()), "响应均含有效 rows")
    ok(len(get_calls) <= 1, "并发下 Ganan 仅抓取一次", len(get_calls))
    leftovers = [f for f in os.listdir(SANDBOX) if ".part" in f]
    ok(not leftovers, "并发后无临时文件残留", leftovers)
    with open(os.path.join(SANDBOX, "7974_weekly.json"), encoding="utf-8") as f:
        ok(json.load(f).get("code") == "7974", "并发后缓存有效(JSON 可解析)")
finally:
    weekly.http_get = real_get
    shutil.rmtree(SANDBOX, ignore_errors=True)

# ================================================================ 真实 Ganan HTML 解析（离线夹具）
print("\n=== 真实 Ganan HTML 解析（离线夹具）===")
FIX = os.environ.get("GANAN_FIXTURE") or "/tmp/ganan_7974_fresh.html"
if os.path.exists(FIX):
    with open(FIX, encoding="utf-8") as f:
        rows = weekly.parse_ganan_html(f.read())
    ok(len(rows) == 10, "Ganan 7974 解析出 10 周", len(rows))
    if rows:
        ok(rows[0]["weekEnding"] == "2026-07-31" and rows[-1]["weekEnding"] == "2026-10-02",
           "周次升序且首尾正确", (rows[0]["weekEnding"], rows[-1]["weekEnding"]))
        ok(rows[-1]["buy"] == 9324600 and rows[-1]["sell"] == 693900,
           "最新周买/卖残数值正确", (rows[-1]["buy"], rows[-1]["sell"]))
else:
    # ★ A4.3 §3：旧实现只 print 一句，3 条断言悄悄消失、总数仍显示"全部通过"。
    #   现在显式登记为 SKIP，并在汇总里单列，绝不冒充 PASS。
    #   复现方式：设置 GANAN_FIXTURE=<path> 指向任意一份 Ganan 週次 HTML。
    skipped("Ganan 7974 解析出 10 周", f"缺夹具 {FIX}")
    skipped("周次升序且首尾正确", f"缺夹具 {FIX}")
    skipped("最新周买/卖残数值正确", f"缺夹具 {FIX}")

# ================================================================ N. 历史不足优先于公司行动（285A 场景）
print("\n=== N. 历史不足优先于公司行动（13W 窗口未攒够 14 周）===")
# 仅 11 周快照，中间含一次拆股（比值守恒跳变）。
# 预期：w13 因窗口起点早于最早快照 → insufficient_history（データ蓄積中 11/14週），
#       而不是被窗口内的拆股顶替成 corporate_action。
nkeys = _fridays("2026-01-02", 11)
nsnaps = _snaps(nkeys)
nsnaps[5]["buy"] *= 4
nsnaps[5]["sell"] *= 4
nsnaps[5]["ratio"] = round(nsnaps[5]["buy"] / nsnaps[5]["sell"], 2)
nseries = weekly.merge_weekly([], nsnaps, as_of=AS_OF_FUTURE)
nevents = weekly.detect_corporate_actions(nseries)
ok(len(nevents) >= 1, "检测到拆股事件", nevents)
tN = weekly.compute_trends(nseries, nevents, as_of=AS_OF_FUTURE)
ok(tN["w13"]["available"] is False, "w13 不可用")
ok(tN["w13"]["reason"] == weekly.REASON_INSUFFICIENT_HISTORY,
   "w13 缺历史 → insufficient_history（而非 corporate_action）", tN["w13"].get("reason"))
ok(tN["w13"]["weeksPresent"] == 11 and tN["w13"]["weeksNeeded"] == 14,
   "w13 weeksPresent=11 / weeksNeeded=14", (tN["w13"].get("weeksPresent"), tN["w13"].get("weeksNeeded")))
ok(tN["w8"]["corporateActionAffected"] is True,
   "已具备窗口的短周期（w8）仍感知公司行动", tN["w8"].get("corporateActionAffected"))

# ================================================================ O. provisional：JST 语义 + 跨周重读缓存
print("\n=== O. provisional：JST 语义 + 节假日周四 + 跨周重读缓存 ===")
# O1: _today_jst 明确按 UTC+9，不依赖服务器部署时区
_fixed = datetime.datetime(2026, 10, 9, 15, 0, 0, tzinfo=datetime.timezone.utc)  # = JST 10-10 00:00
_real_now_utc = weekly._now_utc
weekly._now_utc = staticmethod(lambda: _fixed)
try:
    ok(weekly._today_jst() == datetime.date(2026, 10, 10),
       "_today_jst 按 UTC+9 计算（15:00Z → JST 次日 10-10）", weekly._today_jst())
finally:
    weekly._now_utc = _real_now_utc

# O2: 节假日周四快照：周五锚点，as_of=周六(JST) → 非 provisional（周已结束）
hol = [weekly.normalize_snapshot({"weekKey": "2026-10-09", "snapshotDate": "2026-10-08",
                                  "buy": 900000, "sell": 80000, "ratio": 11.25, "source": "jpx"})]
weekly.mark_provisional(hol, as_of=datetime.date(2026, 10, 10))   # 周六 JST
ok(hol[0]["provisional"] is False, "节假日周四快照（as_of=周六 JST）→ 非 provisional", hol[0]["provisional"])
weekly.mark_provisional(hol, as_of=datetime.date(2026, 10, 8))    # 周四当天
ok(hol[0]["provisional"] is True, "同周周四当天 → provisional=True（周未结束）", hol[0]["provisional"])

# O3: 跨周后重读缓存，provisional 不残留旧状态（4D）
SANDBOX_O = tempfile.mkdtemp(prefix="weekly_prov_test_")
_real_get = weekly.http_get
weekly.http_get = lambda *a, **k: ""   # 关闭真实 Ganan 网络
try:
    okeys = _fridays("2026-10-02", 4)   # 末周 10-23
    odaily = [{"date": k, "buy": 1_000_000 + i * 1000, "sell": 100_000, "ratio": 10.0}
              for i, k in enumerate(okeys)]
    r1o = weekly.build_weekly_response("8888", odaily, cache_dir=SANDBOX_O,
                                       as_of=datetime.date(2026, 10, 20))   # 周三，末周进行中
    ok(r1o["latestProvisional"] is True, "首次（as_of=10-20 周三）→ latest provisional=True",
       r1o["latestProvisional"])
    r2o = weekly.build_weekly_response("8888", odaily, cache_dir=SANDBOX_O,
                                       as_of=datetime.date(2026, 11, 1))    # 隔周重读
    ok(r2o["latestProvisional"] is False, "跨周后重读缓存（as_of=11-01）→ provisional 翻为 False（不残留）",
       r2o["latestProvisional"])
    ok(r2o["coverageWeeks"] == 4, "coverage 仍 4 周（历史保留）", r2o["coverageWeeks"])
finally:
    weekly.http_get = _real_get
    shutil.rmtree(SANDBOX_O, ignore_errors=True)

# ================================================================ A3.1 会社行為保护完整性
# （在 A~O 之后追加；A3.1 只改 weekly.py 检测/置信度 + index.html 消费者，不碰 A1/A2 逻辑）
print("\n############ A3.1 会社行為保护完整性（corporate action）############")


def _ca_trends(wk_keys, split_at=None, split_factor=4, snaps=None):
    s = snaps if snaps is not None else _snaps(wk_keys)
    if split_at is not None:
        s[split_at]["buy"] *= split_factor
        s[split_at]["sell"] *= split_factor
        s[split_at]["ratio"] = round(s[split_at]["buy"] / s[split_at]["sell"], 2)
    series = weekly.merge_weekly([], s, as_of=AS_OF_FUTURE)
    ev = weekly.detect_corporate_actions(series)
    cmap = weekly.corporate_action_confidence_map(ev, series)
    tr = weekly.compute_trends(series, ev, as_of=AS_OF_FUTURE, conf_map=cmap)
    return series, ev, cmap, tr


# --- A3.1-A. sellPrev=0 / sellCurrent>0 → 绝不崩溃（旧实现 ZeroDivisionError）---
print("\n=== A3.1-A. sellPrev=0 / sellCurrent>0 → 不崩溃、不复检 ===")
a1 = [snap("2026-10-02", "2026-10-02", 1000, 0, ratio=5.0),
      snap("2026-10-09", "2026-10-09", 2000, 200, ratio=10.0)]   # buy 跳 1.0、sell 0→200
try:
    ev = weekly.detect_corporate_actions(weekly.merge_weekly([], a1, as_of=AS_OF_FUTURE))
    ok(ev == [], "sellPrev=0/sellCurr>0 → 不抛 ZeroDivisionError 且不复检会社行為", ev)
except Exception as e:
    ok(False, "sellPrev=0 → 崩溃! " + type(e).__name__, str(e))

# --- A3.1-B. sellPrev=0 / sellCurrent=0（buy 3x）→ 不崩溃、保守不判定 ---
print("\n=== A3.1-B. sell 両方 0、buy 3x → 不崩溃、证据不足不判定 ===")
a2 = [snap("2026-10-02", "2026-10-02", 1000, 0, ratio=5.0),
      snap("2026-10-09", "2026-10-09", 3000, 0, ratio=15.0)]
try:
    ev = weekly.detect_corporate_actions(weekly.merge_weekly([], a2, as_of=AS_OF_FUTURE))
    ok(ev == [], "sell 両方 0 且 buy 3x → 不崩溃、不误报（无法验证同倍率→保守跳过）", ev)
except Exception as e:
    ok(False, "sell 両方 0 → 崩溃! " + type(e).__name__, str(e))

# --- A3.1-C. buyPrev=0 → 不产生 NaN/Infinity ---
print("\n=== A3.1-C. buyPrev=0 → 不产生 NaN / Infinity ===")
a3 = [snap("2026-10-02", "2026-10-02", 0, 100, ratio=0.0),
      snap("2026-10-09", "2026-10-09", 500, 100, ratio=5.0)]
try:
    ev = weekly.detect_corporate_actions(weekly.merge_weekly([], a3, as_of=AS_OF_FUTURE))
    tr = weekly.compute_trends(weekly.merge_weekly([], a3, as_of=AS_OF_FUTURE), ev, as_of=AS_OF_FUTURE)
    ok(ev == [], "buyPrev=0 → 不崩溃、不复检", ev)
    any_nan = any(isinstance(t.get("buyPct"), float) and t.get("buyPct") != t.get("buyPct")
                  for t in tr.values())
    ok(not any_nan, "趋势中不含 NaN/Infinity（buyPct 全部为 None 或有限值）",
       [t.get("buyPct") for t in tr.values()])
except Exception as e:
    ok(False, "buyPrev=0 → 崩溃! " + type(e).__name__, str(e))

# --- A3.1-D. 買残↑/売残↓ 无其他证据 → 不能确认拆股 ---
print("\n=== A3.1-D. 買残↑/売残↓ 无其他证据 → 不误报会社行為 ===")
# D1: 缺 ratio（None）→ 旧实现会误报；新实现因无法验证比值守恒而跳过
d1 = [snap("2026-10-02", "2026-10-02", 1000, 100, ratio=None),
      snap("2026-10-09", "2026-10-09", 1500, 50, ratio=None)]
ok(weekly.detect_corporate_actions(weekly.merge_weekly([], d1, as_of=AS_OF_FUTURE)) == [],
   "缺 ratio 的 買↑/売↓ → 不误报（无法验证比值守恒）")
# D2: ratio 存在但不守恒（売残减少使倍率剧变）→ 也不该判会社行為
d2 = [snap("2026-10-02", "2026-10-02", 1000, 100, ratio=10.0),
      snap("2026-10-09", "2026-10-09", 1500, 50, ratio=30.0)]
ok(weekly.detect_corporate_actions(weekly.merge_weekly([], d2, as_of=AS_OF_FUTURE)) == [],
   "ratio 不守恒的 買↑/売↓ → 不误报（倍率骤变，非同倍率缩放）")

# --- A3.1-E. 同倍率缩放 fixture → 仍可识别为 suspected ---
print("\n=== A3.1-E. 同倍率缩放（both ×3, ratio 守恒）→ 仍识别为 suspected ===")
e_keys = _fridays("2026-01-02", 12)
_, evE, cmapE, trE = _ca_trends(e_keys, split_at=6, split_factor=3)
ok("2026-02-13" in evE, "检测到会社行為事件（含拆股周 2026-02-13；单周跳变会被相邻两步都检出）", evE)
ok(all(cmapE[k] == "suspected" for k in evE), "週次ヒューリスティック = suspected（非 confirmed）", cmapE)
ok(trE["w8"]["caConfidence"] == "suspected", "受影响周期 w8.caConfidence='suspected'",
   trE["w8"].get("caConfidence"))

# --- A3.1-F. 285A 镜像（11 周、拆股靠后）→ w4/w8 保护、13W 历史不足 ---
print("\n=== A3.1-F. 285A 镜像（11 周、拆股靠后）→ w4/w8 保护 + 13W 不足 ===")
f_keys = _fridays("2026-01-02", 11)
_, evF, cmapF, trF = _ca_trends(f_keys, split_at=9, split_factor=4)   # 末周前 1 周拆股
ok("2026-03-06" in evF, "检测到拆股事件（含拆股周 2026-03-06；单周跳变会被相邻两步都检出）", evF)
ok(trF["w4"]["corporateActionAffected"] is True, "w4 区间含拆股 → affected=True", trF["w4"].get("corporateActionAffected"))
ok(trF["w4"]["caConfidence"] == "suspected", "w4.caConfidence='suspected'", trF["w4"].get("caConfidence"))
ok(trF["w8"]["corporateActionAffected"] is True, "w8 区间含拆股 → affected=True", trF["w8"].get("corporateActionAffected"))
ok(trF["w13"]["corporateActionAffected"] is False, "w13 因窗口不足 → 非会社行為", trF["w13"].get("corporateActionAffected"))
ok(trF["w13"]["reason"] == weekly.REASON_INSUFFICIENT_HISTORY, "w13 reason=insufficient_history（优先于会社行為）", trF["w13"].get("reason"))
ok(trF["w13"]["caConfidence"] == "none", "w13.caConfidence='none'", trF["w13"].get("caConfidence"))

# --- A3.1-G. 同受影响周期：详细页绝不得展示可解释的 +200% ---
print("\n=== A3.1-G. 受影响周期：available=False & buyPct=None（详情页无 +200%）===")
ok(trF["w4"]["available"] is False, "w4 available=False（比较停止）")
ok(trF["w4"]["buyPct"] is None and trF["w4"]["sellPct"] is None,
   "w4 buyPct/sellPct 为 None（绝不伪造/解释变化百分比）",
   (trF["w4"].get("buyPct"), trF["w4"].get("sellPct")))

# --- A3.1-H. 响应级会社行為标志（首页/详情页统一依据）---
print("\n=== A3.1-H. 响应级 corporateActionAffected / caConfidence ===")
rF = weekly.build_weekly_response.__doc__  # noqa: 仅占位，下方用真实调用
SANDBOX_H = tempfile.mkdtemp(prefix="weekly_ca_resp_")
_real_get_h = weekly.http_get
weekly.http_get = lambda *a, **k: ""
try:
    f_daily = [{"date": k, "buy": 1_000_000 + i * 1000, "sell": 100_000, "ratio": 10.0}
               for i, k in enumerate(f_keys)]
    # 用 splitConfirmed 在日次侧标精确事件 → 验证 confirmed 通路（不依赖网络）
    f_daily[9]["buy"] = (1_000_000 + 9 * 1000) * 4
    f_daily[9]["sell"] = 100_000 * 4
    f_daily[9]["ratio"] = round(f_daily[9]["buy"] / f_daily[9]["sell"], 2)
    f_daily[9]["splitConfirmed"] = True
    resp_h = weekly.build_weekly_response("7777", f_daily, cache_dir=SANDBOX_H,
                                          as_of=datetime.date(2026, 12, 31))
    ok(resp_h["corporateActionAffected"] is True, "响应级 corporateActionAffected=True", resp_h.get("corporateActionAffected"))
    ok(resp_h["caConfidence"] == "confirmed",
       "日次 splitConfirmed → 响应级 caConfidence='confirmed'（与 suspected 区分）", resp_h.get("caConfidence"))
    ok(resp_h["trends"]["w4"]["caConfidence"] == "confirmed", "w4.caConfidence='confirmed'",
       resp_h["trends"]["w4"].get("caConfidence"))
finally:
    weekly.http_get = _real_get_h
    shutil.rmtree(SANDBOX_H, ignore_errors=True)

# --- A3.1-I. 无会社行為的正常股票 → 周次趋势照常 ---
print("\n=== A3.1-I. 正常股票（无会社行為）→ 周次趋势照常 ===")
i_keys = _fridays("2026-01-02", 14)
_, evI, cmapI, trI = _ca_trends(i_keys)
ok(evI == [], "无会社行為 → 检测为空", evI)
ok(trI["w13"]["available"] is True, "w13 available")
ok(trI["w13"]["buyPct"] is not None, "w13 百分比真实计算（非 None）")
ok(trI["w13"]["caConfidence"] == "none", "w13.caConfidence='none'", trI["w13"].get("caConfidence"))

# --- A3.1-J. confirmed vs suspected 文案映射（数据契约）---
print("\n=== A3.1-J. confirmed / suspected 区分（_plumbing 验证）===")
ok(weekly.corporate_action_confidence_map([], []) == {}, "空事件 → 空映射")
raw = [snap("2026-02-13", "2026-02-13", 100, 10, splitConfirmed=True),
       snap("2026-02-06", "2026-02-06", 100, 10)]
ok(weekly.corporate_action_confidence_map(["2026-02-13"], raw)["2026-02-13"] == "confirmed",
   "splitConfirmed=True → confirmed")
ok(weekly.corporate_action_confidence_map(["2026-02-13"], [snap("2026-02-13", "2026-02-13", 100, 10)])["2026-02-13"] == "suspected",
   "无 splitConfirmed → suspected")

# --- A3.1-K. _unavailable 的 caConfidence 字段语义 ---
print("\n=== A3.1-K. _unavailable caConfidence 字段 ===")
ok(weekly._unavailable(4, weekly.REASON_CORPORATE_ACTION, 4, [], True)["caConfidence"] == "suspected",
   "会社行為不可用 → caConfidence='suspected'（缺省）")
ok(weekly._unavailable(4, weekly.REASON_CORPORATE_ACTION, 4, [], True, ca_confidence="confirmed")["caConfidence"] == "confirmed",
   "会社行為不可用（指定 confirmed）→ caConfidence='confirmed'")
ok(weekly._unavailable(4, weekly.REASON_MISSING_WEEK, 4, ["x"], False)["caConfidence"] == "none",
   "非会社行為不可用 → caConfidence='none'")

# --- A3.1-L. A2 自然周 / 历史积累契约不被破坏 ---
print("\n=== A3.1-L. A2 自然周 / 历史积累契约保持 ===")
SANDBOX_L = tempfile.mkdtemp(prefix="weekly_a2_keep_")
_real_get_l = weekly.http_get
weekly.http_get = lambda *a, **k: ""
try:
    all14 = _fridays("2026-01-02", 14)
    d14 = [{"date": wk, "buy": 1_000_000 + i * 10000, "sell": 100_000 + i * 1000,
            "ratio": round((1_000_000 + i * 10000) / (100_000 + i * 1000), 2)}
           for i, wk in enumerate(all14)]
    r1 = weekly.build_weekly_response("6666", d14, cache_dir=SANDBOX_L, as_of=datetime.date(2026, 12, 31))
    ok(r1["coverageWeeks"] == 14, "14 周输入 → coverage=14")
    ok(r1["trends"]["w13"]["available"] is True, "w13 available（自然周精确 13 周）")
    r2 = weekly.build_weekly_response("6666", d14[6:], cache_dir=SANDBOX_L, as_of=datetime.date(2026, 12, 31))
    ok(r2["coverageWeeks"] >= 14, "仅 8 周输入（上游缩窗）→ coverage 仍 >=14（历史不删）", r2["coverageWeeks"])
    ok(r2["trends"]["w13"]["available"] is True, "w13 仍可用（历史积累保留）")
finally:
    weekly.http_get = _real_get_l
    shutil.rmtree(SANDBOX_L, ignore_errors=True)

# --- A3.1-M. 响应级 corporateActionAffected 不得覆盖周期级 reason（phase A3.1.5 issue 3）---
print("\n=== A3.1-M. 响应级 flag 不覆盖周期级 reason（w4/w8=CA, w13=insufficient）===")
SANDBOX_M = tempfile.mkdtemp(prefix="weekly_a31_m_")
_real_get_m = weekly.http_get
weekly.http_get = lambda *a, **k: ""
try:
    m_keys = _fridays("2026-01-02", 11)
    m_daily = [{"date": wk, "buy": 1_000_000 + i * 1000, "sell": 100_000 + i * 100,
                "ratio": round((1_000_000 + i * 1000) / (100_000 + i * 100), 2)}
               for i, wk in enumerate(m_keys)]
    # 末周前 1 周（index 9）拆股：买/卖同 ×4，ratio 守恒 → 週次 heuristic 判 suspected
    m_daily[9]["buy"] *= 4
    m_daily[9]["sell"] *= 4
    m_daily[9]["ratio"] = round(m_daily[9]["buy"] / m_daily[9]["sell"], 2)
    resp_m = weekly.build_weekly_response("5555", m_daily, cache_dir=SANDBOX_M,
                                          as_of=datetime.date(2026, 12, 31))
    ok(resp_m["corporateActionAffected"] is True, "响应级 corporateActionAffected=True（存在会社行為风险）")
    ok(resp_m["trends"]["w4"]["corporateActionAffected"] is True, "w4.corporateActionAffected=True")
    ok(resp_m["trends"]["w4"]["reason"] == weekly.REASON_CORPORATE_ACTION, "w4.reason=corporate_action")
    ok(resp_m["trends"]["w8"]["corporateActionAffected"] is True, "w8.corporateActionAffected=True")
    # ★ 关键：响应级 flag=True 绝不能让 w13 也显示比較停止；w13 只看自己的状态
    ok(resp_m["trends"]["w13"]["corporateActionAffected"] is False,
       "w13.corporateActionAffected=False（不受响应级 flag 影响）")
    ok(resp_m["trends"]["w13"]["reason"] == weekly.REASON_INSUFFICIENT_HISTORY,
       "w13.reason=insufficient_history（11/14週，蓄積中，而非比較停止）")
    ok(resp_m["trends"]["w13"]["buyPct"] is None, "w13 无伪造百分比")
finally:
    weekly.http_get = _real_get_m
    shutil.rmtree(SANDBOX_M, ignore_errors=True)

# --- A3.1-N. 売残=0 且买残巨大变化 → 不崩溃、不伪造会社行為事件（phase A3.1.5 issue 4）---
print("\n=== A3.1-N. 売残=0 + 买残巨変 → 不崩溃、不误报/不伪造 confirmed ===")
n_keys = _fridays("2026-05-01", 10)
n_rows = [snap(n_keys[i], n_keys[i],
               100_000 + (i * 300_000 if i == 5 else 0),  # 第5周买残暴增 3 倍
               0, ratio=None)                              # 売残恒为0（无融券余额，合法）
          for i in range(len(n_keys))]
try:
    evN = weekly.detect_corporate_actions(weekly.merge_weekly([], n_rows, as_of=AS_OF_FUTURE))
    trN = weekly.compute_trends(weekly.merge_weekly([], n_rows, as_of=AS_OF_FUTURE), evN, as_of=AS_OF_FUTURE)
    ok(evN == [], "売残=0 且买残暴增3倍 → 不复检、不伪造会社行為事件（ratio 缺失无法验证同倍率）", evN)
    ok(all(not (t or {}).get("corporateActionAffected") for t in trN.values()),
       "无周期被标 corporate_action（不伪造 confirmed/suspected）")
    def _finite(v):
        return v is None or (isinstance(v, float) and v == v and abs(v) != float("inf"))
    ok(all(_finite((t or {}).get("buyPct")) and _finite((t or {}).get("sellPct"))
           for t in trN.values()),
       "趋势百分比为 None 或有限值（无 NaN/Inf）")
except Exception as e:
    ok(False, "売残=0 + 买残巨変 → 崩溃! " + type(e).__name__, str(e))

# --- A3.1-O. confirmed / suspected 在响应级必须可区分（UI 文案依据）---
print("\n=== A3.1-O. 响应级 caConfidence：confirmed vs suspected 可区分 ===")
def _resp_with_splitconfirmed(flag):
    SANDBOX = tempfile.mkdtemp(prefix="weekly_a31_o_")
    _g = weekly.http_get
    weekly.http_get = lambda *a, **k: ""
    try:
        keys = _fridays("2026-01-02", 11)
        daily = [{"date": wk, "buy": 1_000_000 + i * 1000, "sell": 100_000 + i * 100,
                  "ratio": round((1_000_000 + i * 1000) / (100_000 + i * 100), 2)}
                 for i, wk in enumerate(keys)]
        daily[9]["buy"] *= 4
        daily[9]["sell"] *= 4
        daily[9]["ratio"] = round(daily[9]["buy"] / daily[9]["sell"], 2)
        if flag:
            daily[9]["splitConfirmed"] = True
        return weekly.build_weekly_response("4444", daily, cache_dir=SANDBOX,
                                            as_of=datetime.date(2026, 12, 31))
    finally:
        weekly.http_get = _g
        shutil.rmtree(SANDBOX, ignore_errors=True)
r_sus = _resp_with_splitconfirmed(False)
r_con = _resp_with_splitconfirmed(True)
ok(r_sus["caConfidence"] == "suspected", "无 splitConfirmed → 响应级 caConfidence='suspected'", r_sus["caConfidence"])
ok(r_con["caConfidence"] == "confirmed", "有 splitConfirmed → 响应级 caConfidence='confirmed'", r_con["caConfidence"])
ok(r_sus["caConfidence"] != r_con["caConfidence"], "两者必须不同（供 UI 选不同文案）")

print(f"\n结果（含 A3.1）：{pass_n} 通过，{fail_n} 失败，{skip_n} 跳过")
if skip_n:
    print(f"  ⏭️  注意：{skip_n} 条因缺少外部夹具未执行 —— **不得计为通过**")
    print("      补齐夹具：GANAN_FIXTURE=<path> python3 test_weekly.py")

# ★ A4.3 退出码约定：
#   0 = PASS（全部断言真实执行）  1 = FAIL  77 = SKIP（缺夹具，断言未执行）
#   有 SKIP 时必须返回 77 —— 绝不能返回 0，否则 CI 会把「缺 3 条没跑」误判成完整通过。
if fail_n:
    sys.exit(1)
if skip_n:
    sys.exit(77)
sys.exit(0)
