#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周次信用残趋势（4W / 8W / 13W）—— 后端计算层
=====================================================================
产品定位（用户 2026-10-07 调整）：
  几周波段交易，最长持有约 3 个月。仅做 **4W / 8W / 13W** 三种信用残趋势，
  不做 26W、不做百分位、不做新评分、不接 J-Quants、不用 SQLite、不引入
  复杂 provider framework。

数据构成：
  · bootstrap：Ganan 现有约 10 周 weekly 数据（公开网页，免费）
  · 近期及未来：项目已有 JPX 日次信用残，按 JST 自然周取该周最后一条
    有效记录生成 weekly snapshot
  · 同一 week：JPX 优先覆盖 Ganan
  · 随着 JPX 历史自然积累，未来自动减少对 Ganan 的依赖

周次定义（关键，避免 span 混乱）：
  · 一个 "week" 以「周五」为锚点（JPX 週末残高即周五收盘）。
    week_key(date) = 该日期所在自然周的周五（Mon 起算）。
    这样 Ganan 的周五周次 与 JPX 日次里同一周的最后一条记录 落在同一 week_key。
  · 26W 不在本功能范围；本功能 13W = 13 个 interval，即需要 ≥ 14 个有效快照。

趋势定义（严格，禁止用最早数据替代）：
  · 4W  = 当前 vs 4 interval 前，需 ≥ 5 个快照
  · 8W  = 当前 vs 8 interval 前，需 ≥ 9 个快照
  · 13W = 当前 vs 13 interval 前，需 ≥ 14 个快照
  · 不足 → 该 trend 返回 None（绝不外推 / 绝不拿最早数据顶替）。

公司行动（拆并股）：
  · 复用 engine2.js 的「fail-safe」精神：检测到比较区间内有拆并股 →
    corporateActionAffected = True，调用方停止比较（不自动复权信用残）。
  · 后端 weekly 序列没有价格，故用「比值守恒的大幅跳变」判定：
    相邻周次 buy 与 sell 同时同向跳变 ≥ 40%，且 ratio（买/卖）变化 < 10%
    → 强烈暗示拆并股（两腿按相同比例缩放，倍率不变）。

缓存（沿用现有架构，不引入 SQLite）：
  · Ganan weekly cache   : {code}_ganan_weekly.json
  · JPX 日次缓存         : 沿用 server.py 现有 {code}_{day}.json（本模块不重解析 PDF）
  · merged weekly series : {code}_weekly.json
  三者均保持 atomic_write + 版本闸门 + 单飞锁（concurrent safe）。
"""

import os
import re
import json
import html
import time
import hashlib
import datetime
import threading
import urllib.request
import tempfile

# ---------------------------------------------------------------- 版本与常量
# 独立的周次解析版本：本模块逻辑变更时 +1，旧 JSON 缓存自动作废。
WEEKLY_PARSER_VERSION = 1

GAN_BASE = "https://ganan-finance.com/{code}/short_positions"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CACHE_DIR_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE_DIR_DEFAULT, exist_ok=True)


# ---------------------------------------------------------------- 原子写
def atomic_write(path, data, binary=False):
    """先写唯一 .tmp，再 os.replace 原子替换（与 server.py 同语义）。

    绝不会出现「文件已创建但内容没写完」的中间态：os.replace 在同文件系统
    上是原子的，读者要么看到旧完整文件，要么看到新完整文件。
    """
    d = os.path.dirname(path) or "."
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8"}
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".part")
    os.close(fd)
    try:
        with open(tmp, mode, **kwargs) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 并发锁
# 单飞锁：同一 code 只允许一个线程在抓 Ganan / 写 merged 缓存，
# 其余线程等它完成再复用结果（避免半写 / 重复下载 / 重复计算）。
_ganan_locks = {}
_ganan_locks_guard = threading.Lock()

_weekly_locks = {}
_weekly_locks_guard = threading.Lock()


def _lock(table, key):
    guard = _ganan_locks_guard if table == "ganan" else _weekly_locks_guard
    store = _ganan_locks if table == "ganan" else _weekly_locks
    with guard:
        lk = store.get(key)
        if lk is None:
            lk = threading.Lock()
            store[key] = lk
        return lk


# ---------------------------------------------------------------- 工具
def http_get(url, timeout=30, binary=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "ja,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "ignore")


def week_end_friday(d):
    """返回 d 所在自然周（Mon 起算）的周五日期。

    Ganan 的週末残高即周五收盘，故用它作为 week 锚点，
    使 Ganan 周五 与 JPX 日次同一周的最后一条记录落在同一 week_key。
    """
    return d + datetime.timedelta(days=(4 - d.weekday()))


def _date(s):
    try:
        return datetime.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _pct(a, b):
    """(a - b) / |b| * 100，保留 1 位；缺值或分母非正返回 None。"""
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / abs(b) * 100.0, 1)


# ------------------------------------------------- 1. Ganan weekly parser
def parse_ganan_html(page_html):
    """从 Ganan short_positions 页面抽取周次信用残。

    页面含两组 data-rows（HTML 实体转义过的 JSON）：
      - 表头含「買残」→ [["日付","買残","信用倍率"],[date, buy, ratio], ...]
      - 表头含「売残」→ [["日付","売残","信用倍率"],[date, sell, ratio], ...]
    两组按日期对齐后合并为 {weekEnding, buy, sell, ratio}。

    返回按 weekEnding 升序的 list[dict]。解析失败 / 无数据 → 返回 []。
    """
    if not page_html:
        return []

    buy_map = {}
    sell_map = {}

    for m in re.finditer(r'data-rows="(.*?)"', page_html, re.S):
        raw = html.unescape(m.group(1))
        try:
            arr = json.loads(raw)
        except Exception:
            continue
        if not isinstance(arr, list) or not arr:
            continue
        header = arr[0]
        if not isinstance(header, list):
            continue
        hstr = " ".join(str(h) for h in header)
        if "買残" in hstr:
            key = "buy"
        elif "売残" in hstr:
            key = "sell"
        else:
            continue
        for row in arr[1:]:
            if not isinstance(row, list) or len(row) < 2:
                continue
            d = row[0]
            try:
                v = float(row[1])
            except (TypeError, ValueError):
                continue
            v = int(v) if float(v).is_integer() else v
            if key == "buy":
                buy_map[d] = v
            else:
                sell_map[d] = v

    out = []
    for d in sorted(set(buy_map) | set(sell_map)):
        b = buy_map.get(d)
        s = sell_map.get(d)
        ratio = round(b / s, 2) if (b is not None and s not in (None, 0)) else None
        out.append({"weekEnding": d, "buy": b, "sell": s, "ratio": ratio})
    out.sort(key=lambda x: x["weekEnding"])
    return out


def _ganan_cache_path(code, cache_dir):
    return os.path.join(cache_dir, f"{code}_ganan_weekly.json")


def fetch_ganan_weekly(code, cache_dir=CACHE_DIR_DEFAULT, fresh=False):
    """取某代码的 Ganan 周次序列（带缓存）。

    返回 (rows, meta)。meta["cached"]=True 表示直接复用磁盘缓存。
    rows 与 parse_ganan_html 的输出同构（升序 list[dict]）。
    """
    cpath = _ganan_cache_path(code, cache_dir)

    # 1) 缓存命中（版本一致即可，内容指纹用于 merged 层判脏）
    if not fresh and os.path.exists(cpath):
        try:
            with open(cpath, encoding="utf-8") as f:
                cached = json.load(f)
            if (cached.get("weeklyParserVersion") == WEEKLY_PARSER_VERSION
                    and isinstance(cached.get("rows"), list)):
                return cached["rows"], {"cached": True,
                                        "fp": cached.get("sourceFingerprint")}
        except Exception:
            pass

    # 2) 真正抓取（单飞：同一 code 同时只抓一次）
    lk = _lock("ganan", code)
    with lk:
        # 等锁期间可能已被别的线程写好
        if not fresh and os.path.exists(cpath):
            try:
                with open(cpath, encoding="utf-8") as f:
                    cached = json.load(f)
                if (cached.get("weeklyParserVersion") == WEEKLY_PARSER_VERSION
                        and isinstance(cached.get("rows"), list)):
                    return cached["rows"], {"cached": True,
                                            "fp": cached.get("sourceFingerprint")}
            except Exception:
                pass

        try:
            page = http_get(GAN_BASE.format(code=code), timeout=30)
            rows = parse_ganan_html(page)
            fp = hashlib.md5(page.encode("utf-8")).hexdigest()[:16]
            ok = True
            err = None
        except Exception as e:
            # 抓取失败：若有旧缓存则降级复用（避免直接丢光 bootstrap），
            # 否则返回空序列（不写脏缓存）。
            ok = False
            err = f"{type(e).__name__}: {e}"
            rows = None
            fp = None
            if os.path.exists(cpath):
                try:
                    with open(cpath, encoding="utf-8") as f:
                        cached = json.load(f)
                    if isinstance(cached.get("rows"), list):
                        rows = cached["rows"]
                        fp = cached.get("sourceFingerprint")
                        ok = True
                except Exception:
                    pass

        if ok and rows is not None:
            payload = {
                "weeklyParserVersion": WEEKLY_PARSER_VERSION,
                "sourceFingerprint": fp,
                "fetchedAt": datetime.datetime.now().isoformat(timespec="seconds"),
                "code": code,
                "rows": rows,
            }
            try:
                atomic_write(cpath, json.dumps(payload, ensure_ascii=False))
            except Exception:
                pass
            return rows, {"cached": False, "fp": fp, "err": err}
        return rows or [], {"cached": False, "fp": fp, "err": err}


# ------------------------------------------------- 2. JPX 日次 → 周次
def daily_to_weekly(daily_rows):
    """把 JPX 日次记录按自然周（周五锚点）聚合成周次快照。

    每个 week_key 取该周内 **date 最大**（最后一条有效记录）的那条日次作为
    该周快照 —— 自动满足「节假日周取最后实际数据」（某周只交易到周四，
    则周四即为该周快照）。
    """
    if not daily_rows:
        return []
    buckets = {}  # week_key(str) -> {"weekEnding","buy","sell","ratio"}
    for r in daily_rows:
        ds = r.get("date")
        d = _date(ds)
        if d is None:
            continue
        wk = week_end_friday(d).isoformat()
        cand = {
            "weekEnding": ds,
            "buy": r.get("buy"),
            "sell": r.get("sell"),
            "ratio": r.get("ratio"),
            "source": "jpx",
        }
        if wk not in buckets or ds > buckets[wk]["weekEnding"]:
            buckets[wk] = cand
    return [buckets[k] for k in sorted(buckets)]


# ------------------------------------------------- 3. merge（JPX 优先）
def merge_weekly(jpx_list, ganan_list, as_of=None):
    """合并 JPX 周次 与 Ganan 周次。

    规则：
      · 以 week_key（周五）为连接键；同一 week：JPX 优先覆盖 Ganan。
      · 升序排序；附加 source 字段（"jpx" / "ganan"）。
      · provisional：仅「最新一周」且「其 week_key == as_of 所在周」时为真
        （即当前周仍在累积，尚未定稿）。
    """
    if as_of is None:
        as_of = datetime.date.today()
    as_of_fri = week_end_friday(as_of).isoformat()

    by_key = {}
    for s in (ganan_list or []):
        d = _date(s.get("weekEnding"))
        if d is None:
            continue
        k = week_end_friday(d).isoformat()
        e = {"weekEnding": s["weekEnding"], "buy": s.get("buy"),
             "sell": s.get("sell"), "ratio": s.get("ratio"),
             "source": "ganan"}
        by_key[k] = e
    for s in (jpx_list or []):
        d = _date(s.get("weekEnding"))
        if d is None:
            continue
        k = week_end_friday(d).isoformat()
        e = {"weekEnding": s["weekEnding"], "buy": s.get("buy"),
             "sell": s.get("sell"), "ratio": s.get("ratio"),
             "source": "jpx"}
        by_key[k] = e  # JPX 覆盖同周 Ganan

    merged = [by_key[k] for k in sorted(by_key)]
    n = len(merged)
    for i, s in enumerate(merged):
        s["provisional"] = (i == n - 1) and (s.get("weekEnding") and
                       week_end_friday(_date(s["weekEnding"])).isoformat() == as_of_fri)
    return merged


# ------------------------------------------------- 4. 公司行动检测
def detect_corporate_actions(series):
    """扫描相邻周次，返回发生拆并股的 weekEnding 日期列表。

    判定（比值守恒的大幅跳变）：
      buy / sell 同时同向跳变 ≥ 40%，且 ratio(买/卖) 变化 < 10%
      → 两腿按相同比例缩放，倍率几乎不变，强烈暗示拆并股。
    不自动复权，仅标记，由上层比较逻辑暂停。
    """
    events = []
    for i in range(1, len(series)):
        a, b = series[i - 1], series[i]
        ab, bb = a.get("buy"), b.get("buy")
        as_, bs = a.get("sell"), b.get("sell")
        if not (ab and bb and ab > 0 and bs and bb > 0):
            continue
        buy_jump = abs(bb / ab - 1)
        sell_jump = abs(bs / as_ - 1)
        rf, rt = a.get("ratio"), b.get("ratio")
        ratio_change = abs(rt / rf - 1) if (rf and rt) else None
        if min(buy_jump, sell_jump) > 0.4 and (ratio_change is None or ratio_change < 0.10):
            events.append(b["weekEnding"])
    return events


# ------------------------------------------------- 5. 趋势计算
def compute_trend(series, n, events):
    """计算单条趋势（n = 4 / 8 / 13）。

    需要 len(series) >= n + 1 个快照；不足返回 None（不外推）。
    比较区间 [from, to] 内若含公司行动事件 → corporateActionAffected=True。
    """
    if len(series) < n + 1:
        return None
    to = series[-1]
    frm = series[-1 - n]
    from_d = frm["weekEnding"]
    to_d = to["weekEnding"]
    ca = any(from_d < ev <= to_d for ev in (events or []))
    return {
        "available": True,
        "fromDate": from_d,
        "toDate": to_d,
        "buyPct": _pct(to.get("buy"), frm.get("buy")),
        "sellPct": _pct(to.get("sell"), frm.get("sell")),
        "ratioFrom": frm.get("ratio"),
        "ratioTo": to.get("ratio"),
        "corporateActionAffected": ca,
    }


def compute_trends(series, events):
    return {
        "w4": compute_trend(series, 4, events),
        "w8": compute_trend(series, 8, events),
        "w13": compute_trend(series, 13, events),
    }


# ------------------------------------------------- 6. sources 元信息
def build_sources(ganan_list, jpx_list, merged):
    g = ganan_list or []
    j = jpx_list or []
    jpx_weeks = sum(1 for m in merged if m.get("source") == "jpx")
    return {
        "ganan": {
            "weeks": len(g),
            "from": g[0]["weekEnding"] if g else None,
            "to": g[-1]["weekEnding"] if g else None,
        },
        "jpx": {
            "weeks": len(j),
            "from": j[0]["weekEnding"] if j else None,
            "to": j[-1]["weekEnding"] if j else None,
        },
        "mergedWeeks": len(merged),
        "jpxWinsWeeks": jpx_weeks,
        "note": ("Ganan 约10週履歴を bootstrap、JPX 日次から直近週を上書き。"
                 "13W は週数不足の場合 null。"),
    }


# ------------------------------------------------- 7. 编排 + merged 缓存
def _source_fingerprint(ganan_rows, daily_rows):
    h = hashlib.md5()
    h.update(json.dumps(ganan_rows or [], ensure_ascii=False).encode("utf-8"))
    h.update(b"|")
    h.update(json.dumps(
        [{"d": r.get("date"), "b": r.get("buy"), "s": r.get("sell")}
         for r in (daily_rows or [])], ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:16]


def _weekly_cache_path(code, cache_dir):
    return os.path.join(cache_dir, f"{code}_weekly.json")


def _load_weekly_cache(code, cache_dir, fp):
    cpath = _weekly_cache_path(code, cache_dir)
    if not os.path.exists(cpath):
        return None
    try:
        with open(cpath, encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        return None
    if (cached.get("weeklyParserVersion") == WEEKLY_PARSER_VERSION
            and cached.get("sourceFingerprint") == fp
            and isinstance(cached.get("rows"), list)):
        return cached
    return None


def build_weekly_response(code, daily_rows, fresh=False,
                          cache_dir=CACHE_DIR_DEFAULT, as_of=None):
    """生成 /api/weekly 的完整响应 dict。

    入参 daily_rows 由调用方（server.py 的 gather）提供，本模块不重解析 JPX PDF，
    直接复用现有日次缓存。Ganan 周次由本模块抓取 + 缓存。
    """
    # 1) Ganan bootstrap（带缓存）
    ganan, _gmeta = fetch_ganan_weekly(code, cache_dir, fresh=fresh)

    # 2) JPX 日次 → 周次
    jpx = daily_to_weekly(daily_rows)

    # 3) 判脏指纹：Ganan 内容 + JPX 日次内容
    fp = _source_fingerprint(ganan, daily_rows)

    # 4) merged 缓存（单飞：同一 code 同时只算/写一次）
    lk = _lock("weekly", code)
    with lk:
        cached = _load_weekly_cache(code, cache_dir, fp)
        if cached is not None:
            return cached

        # 5) 合并 + 公司行动 + 趋势
        merged = merge_weekly(jpx, ganan, as_of=as_of)
        events = detect_corporate_actions(merged)
        trends = compute_trends(merged, events)
        sources = build_sources(ganan, jpx, merged)

        resp = {
            "weeklyParserVersion": WEEKLY_PARSER_VERSION,
            "sourceFingerprint": fp,
            "cachedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "code": code,
            "rows": merged,
            "coverageWeeks": len(merged),
            "trends": trends,
            "sources": sources,
        }

        cpath = _weekly_cache_path(code, cache_dir)
        try:
            atomic_write(cpath, json.dumps(resp, ensure_ascii=False))
        except Exception:
            pass
        return resp


if __name__ == "__main__":
    # 简单自测（无网络时仅验证解析逻辑）
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        buy = [["日付", "買残", "信用倍率"],
               ["2026-10-02", 9324600, 13.4], ["2026-09-25", 8958000, 12.7]]
        sell = [["日付", "売残", "信用倍率"],
                ["2026-10-02", 693900, 13.4], ["2026-09-25", 705500, 12.7]]
        sample = ('data-rows="' + html.escape(json.dumps(buy), quote=True)
                  + '" data-rows="' + html.escape(json.dumps(sell), quote=True) + '"')
        print(json.dumps(parse_ganan_html(sample), ensure_ascii=False, indent=2))
