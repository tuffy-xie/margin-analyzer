"""test_weekly.py —— 周次信用残趋势（4W/8W/13W）回归测试

覆盖用户指定的 11 项：
  1. 5  快照 → 4W  available
  2. 9  快照 → 8W  available
  3. 14 快照 → 13W available
  4. 13 快照 → 13W null
  5. Ganan + JPX merge
  6. 同周 JPX wins
  7. 节假日周取最后实际数据
  8. 当前周 provisional
  9. split 影响对应周期
 10. 数据不足不外推
 11. cache / concurrency

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
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weekly

pass_n = 0
fail_n = 0


def ok(cond, name, extra=None):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print("  ✅ " + name)
    else:
        fail_n += 1
        print("  ❌ " + name + (f" → {extra}" if extra is not None else ""))


# ---------------------------------------------------------------- 合成序列工具
def _fridays(start_iso, n):
    d = datetime.date.fromisoformat(start_iso)
    return [(d + datetime.timedelta(days=7 * i)).isoformat() for i in range(n)]


def mk_series(n, start="2026-01-02", b0=100000, s0=10000):
    """生成 n 个连续周五的周次快照（线性增长，ratio 自洽）。"""
    ds = _fridays(start, n)
    return [{
        "weekEnding": ds[i],
        "buy": b0 + i * 1000,
        "sell": s0 + i * 100,
        "ratio": round((b0 + i * 1000) / (s0 + i * 100), 2),
    } for i in range(n)]


# ================================================================ 1-4. 趋势可用性
print("=== 趋势可用性（4W/8W/13W 阈值）===")
s5 = mk_series(5)
ok(weekly.compute_trend(s5, 4, []) is not None, "5 快照 → 4W available")
ok(weekly.compute_trend(s5, 8, []) is None, "5 快照 → 8W 不足(null)")

s9 = mk_series(9)
ok(weekly.compute_trend(s9, 8, []) is not None, "9 快照 → 8W available")
ok(weekly.compute_trend(s9, 13, []) is None, "9 快照 → 13W 不足(null)")

s13 = mk_series(13)
ok(weekly.compute_trend(s13, 13, []) is None, "13 快照 → 13W 不足(null)")
ok(weekly.compute_trend(s13, 8, []) is not None, "13 快照 → 8W available")

s14 = mk_series(14)
ok(weekly.compute_trend(s14, 13, []) is not None, "14 快照 → 13W available")

# ================================================================ 5. Ganan + JPX merge
print("\n=== Ganan + JPX merge ===")
ganan10 = mk_series(10, start="2026-07-31")          # 10 个周五（bootstrap）
# JPX 日次：只覆盖最近两周（与 Ganan 最近两周同周）+ 一个新周
jpx_daily = [
    {"date": "2026-09-28", "buy": 9000000, "sell": 700000, "ratio": 12.8},  # Mon→同周 10-02
    {"date": "2026-10-02", "buy": 9100000, "sell": 690000, "ratio": 13.2},  # Fri→同周 10-02
    {"date": "2026-10-05", "buy": 9200000, "sell": 680000, "ratio": 13.5},  # Mon→新周 10-09
]
jpx_weekly = weekly.daily_to_weekly(jpx_daily)
merged = weekly.merge_weekly(jpx_weekly, ganan10, as_of=datetime.date(2026, 10, 7))
ok(len(merged) == 11, "合并后应为 11 周（Ganan10 + JPX新周1）", len(merged))
ok(merged[-1]["weekEnding"] == "2026-10-05", "最新周来自 JPX 10-05", merged[-1]["weekEnding"])

# ================================================================ 6. 同周 JPX wins
print("\n=== 同周 JPX 优先覆盖 Ganan ===")
ganan_one = [{"weekEnding": "2026-10-02", "buy": 8000000, "sell": 600000, "ratio": 13.3}]
# JPX 该周最后一条是 09-28(Mon)→同一 week_key 10-02，应覆盖 Ganan
jpx_one = [{"weekEnding": "2026-09-28", "buy": 9500000, "sell": 720000, "ratio": 13.19}]
m1 = weekly.merge_weekly(jpx_one, ganan_one, as_of=datetime.date(2026, 10, 7))
ok(len(m1) == 1 and m1[0]["source"] == "jpx", "同周 JPX 覆盖 Ganan")
ok(m1[0]["buy"] == 9500000, "采用 JPX 的 buy 值", m1[0]["buy"])

# ================================================================ 7. 节假日周取最后实际数据
print("\n=== 节假日周取最后实际数据 ===")
# 某周只交易到周四（周五休市），日次只有周四一条
holiday_daily = [
    {"date": "2026-01-05", "buy": 100000, "sell": 10000, "ratio": 10.0},   # Mon
    {"date": "2026-01-08", "buy": 105000, "sell": 10200, "ratio": 10.3},   # Thu（该周最后实际数据）
    {"date": "2026-01-12", "buy": 110000, "sell": 11000, "ratio": 10.0},   # 下周一
]
hw = weekly.daily_to_weekly(holiday_daily)
wk1 = [w for w in hw if w["weekEnding"] >= "2026-01-05" and w["weekEnding"] <= "2026-01-11"]
ok(len(wk1) == 1 and wk1[0]["weekEnding"] == "2026-01-08",
   "休市周五的周取周四(01-08)为快照", wk1[0]["weekEnding"] if wk1 else None)
ok(hw[-1]["weekEnding"] == "2026-01-12", "下周周一独立成周", hw[-1]["weekEnding"])

# ================================================================ 8. 当前周 provisional
print("\n=== 当前周 provisional ===")
jpx_current = [{"weekEnding": "2026-10-07", "buy": 900000, "sell": 80000, "ratio": 11.0}]  # 周三
m_now = weekly.merge_weekly(jpx_current, [], as_of=datetime.date(2026, 10, 7))
ok(m_now[0]["provisional"] is True, "当前周(as_of 同周) → provisional=True")
m_past = weekly.merge_weekly(jpx_current, [], as_of=datetime.date(2026, 10, 14))
ok(m_past[0]["provisional"] is False, "两周后(as_of 不同周) → provisional=False")

# ================================================================ 9. split 影响对应周期
print("\n=== 拆并股：影响对应周期 ===")
# 12 周，第 7 周(index6)发生 1:4 拆股：买/卖同时 ×4，ratio 不变
base = mk_series(12, start="2026-01-02", b0=100000, s0=10000)
for k in ("buy", "sell"):
    base[6][k] = base[6][k] * 4
base[6]["ratio"] = round(base[6]["buy"] / base[6]["sell"], 2)   # 比值守恒
events = weekly.detect_corporate_actions(base)
ok("2026-02-13" in events, "检测到拆股事件(2026-02-13)", events)
trends = weekly.compute_trends(base, events)
# w8 区间覆盖 index 3..11，含 index6 → 受影响
ok(trends["w8"]["corporateActionAffected"] is True, "w8 区间含拆股 → affected=True")
# w4 区间 index 7..11，不含 index6 → 不受影响
ok(trends["w4"]["corporateActionAffected"] is False, "w4 区间不含拆股 → affected=False")

# 普通有机增长（仅买残涨，ratio 变）→ 不应误报
organic = mk_series(8, b0=100000, s0=10000)
organic[4]["buy"] = organic[4]["buy"] * 4     # 买残 4x 但卖残没动 → ratio 大变
organic[4]["ratio"] = round(organic[4]["buy"] / organic[4]["sell"], 2)
ev2 = weekly.detect_corporate_actions(organic)
ok(ev2 == [], "仅买残跳变(ratio 失恒)不误报拆股", ev2)

# ================================================================ 10. 数据不足不外推
print("\n=== 数据不足不外推（严禁用最早数据顶替 13W）===")
ten = mk_series(10)
t10 = weekly.compute_trends(ten, weekly.detect_corporate_actions(ten))
ok(t10["w4"] is not None, "10 周 → 4W available")
ok(t10["w8"] is not None, "10 周 → 8W available")
ok(t10["w13"] is None, "10 周 → 13W 严格 null（不外推）", t10["w13"])
# 13W 区间若被伪造，fromDate 应为最早两条之一；此处必须整体为 None
ok(t10["w13"] is None, "13W null 不返回伪造对象")

# ================================================================ 11. cache / concurrency
print("\n=== cache 与并发 ===")
SANDBOX = tempfile.mkdtemp(prefix="weekly_cache_test_")
# 用固定 Ganan 夹具 + 关闭真实网络（与真实页面一致：属性值内用 &quot; 转义）
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
    # 缓存命中：第二调用不应再抓网络
    ok(len(get_calls) == 1, "单飞：Ganan 只抓取一次", len(get_calls))
    # 磁盘缓存文件存在且有效
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
    # 并发下 Ganan 仍只抓一次（单飞）
    ok(len(get_calls) <= 1, "并发下 Ganan 仅抓取一次", len(get_calls))
    # merged 缓存文件有效且无 .part 残留
    leftovers = [f for f in os.listdir(SANDBOX) if ".part" in f]
    ok(not leftovers, "并发后无临时文件残留", leftovers)
    with open(os.path.join(SANDBOX, "7974_weekly.json"), encoding="utf-8") as f:
        ok(json.load(f).get("code") == "7974", "并发后缓存有效(JSON 可解析)")
finally:
    weekly.http_get = real_get
    shutil.rmtree(SANDBOX, ignore_errors=True)

# ================================================================ 真实 Ganan 解析（离线夹具）
print("\n=== 真实 Ganan HTML 解析（离线夹具）===")
FIX = "/tmp/ganan_7974_fresh.html"
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
    print("  ⚠ 跳过：离线夹具 /tmp/ganan_7974_fresh.html 不存在")

print(f"\n结果：{pass_n} 通过，{fail_n} 失败")
sys.exit(1 if fail_n else 0)
