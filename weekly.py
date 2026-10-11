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
  · 同周合并：**先比 snapshotDate，时间更晚的胜出**；snapshotDate 相同时
    JPX 优先于 Ganan（见 merge_weekly / _prefer_snapshot）
  · 随着 JPX 历史自然积累，未来自动减少对 Ganan 的依赖

★ 时间契约（A2，2026-10-09 起生效）
-------------------------------------------------------------------------
每个 weekly snapshot 同时携带两个**不同语义**的日期，绝不能混为一谈：

  weekKey       自然周锚点 = 该周（Mon 起算）的周五日期。
                **趋势比较只用 weekKey**。
  snapshotDate  这一周实际取到数据的日期。
                日本节假日当周没有周五交易时，weekKey 仍是周五，
                但 snapshotDate 是该周最后一个实际交易日（如周四）。

  例：weekKey = 2026-10-09，snapshotDate = 2026-10-08（周五休市，周四收盘）
      → 该自然周**存在**，不算 missing week。

趋势定义（严格自然周，禁止索引假设）：
  · 4W  = to.weekKey 与「正好往前 4 个自然周」的 weekKey 比较
  · 8W  = 正好往前 8 个自然周；13W = 正好往前 13 个自然周
  · 窗口内每一个 weekKey 都必须存在：
      - 缺任意一个 → available=False, reason='missing_week', missingWeeks=[...]
      - 目标周早于最早快照 → reason='insufficient_history'（历史还没攒够）
      - 区间内有公司行动 → reason='corporate_action'
  · 绝不：选更早一条代替 / 插值 / 用最早数据顶替 / 把 5W 伪装成 4W。

历史积累：
  · merged cache = 既有 cache（按 weekKey） ∪ 本轮新输入，旧 weekKey 不会
    因为本轮上游没返回而被删除（上游只给最近 8 周时，已积累的 14 周仍在）。
  · 同 weekKey 按 snapshotDate / source 规则覆盖更新。

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
# v1 → v2（A2）：rows 结构改为「weekKey + snapshotDate 双日期」严格自然周语义，
#   且 4W/8W/13W 由「索引假设」改为「weekKey 精确对齐 + 缺周检测」。
#   旧 v1 缓存不含 weekKey，无法通过迁移补齐（无法判断当时按哪个锚点聚合），
#   故按版本闸门直接作废，由当前输入重建（见 _load_weekly_cache）。
WEEKLY_PARSER_VERSION = 2

# 同 weekKey 且 snapshotDate 相同时的 source 优先级（数值大者优先）
SOURCE_PRIORITY = {"jpx": 2, "ganan": 1, "unknown": 0}

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


def _now_utc():
    """当前 UTC 时刻（独立出来的可注入点，便于测试固定时间）。"""
    return datetime.datetime.now(datetime.timezone.utc)


def _today_jst():
    """返回 Asia/Tokyo 当前日期（明确时区，不依赖服务器部署时区）。

    信用残「当前周是否结束」必须按 JST 判断：日本市场周五收盘后该周即结束。
    若误用 UTC / 服务器本地时区（可能与 JST 相差数小时乃至跨日），会在周五深夜
    / 周六凌晨把「已完成周」误判为 provisional（永久停在暫定态）。统一用 UTC+9
    计算，杜绝时区漂移。
    """
    return (_now_utc() + datetime.timedelta(hours=9)).date()


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
def normalize_snapshot(s, default_source="unknown"):
    """把任意来源的周次记录规范化为统一结构（weekKey + snapshotDate 双日期）。

    weekKey       = 该 snapshotDate 所在自然周的周五（比较锚点）
    snapshotDate  = 实际取到数据的日期（节假日周可能是周四/周三）
    weekEnding    = snapshotDate 的旧字段名，**仅为兼容既有 UI 图表保留**

    注意：weekEnding 与 snapshotDate 恒等，请不要再把它当成「周五」使用；
    需要周锚点时一律读 weekKey。
    """
    d = _date(s.get("snapshotDate") or s.get("weekEnding"))
    if d is None:
        return None
    snap = s.get("snapshotDate") or s.get("weekEnding")
    wk = s.get("weekKey") or week_end_friday(d).isoformat()
    return {
        "weekKey": wk,
        "snapshotDate": snap,
        "weekEnding": snap,          # 兼容：既有 UI 按 weekEnding 排序/画 X 轴
        "buy": s.get("buy"),
        "sell": s.get("sell"),
        "ratio": s.get("ratio"),
        "source": (s.get("source") or default_source),
        # ★ A3.1：保留「日次侧精确会社行為事件」标志，用于把週次ヒューリスティック的
        #   suspected 与 精确确认的 confirmed 区分开（不引入新框架，仅透传一个布尔）。
        "splitConfirmed": bool(s.get("splitConfirmed")),
    }


def daily_to_weekly(daily_rows):
    """把 JPX 日次记录按自然周（周五锚点）聚合成周次快照。

    每个 weekKey 取该周内 **date 最大**（最后一条有效记录）的那条日次作为
    该周快照 —— 自动满足「节假日周取最后实际数据」：某周只交易到周四，
    则周四即为该周 snapshotDate，但 weekKey 仍为该周周五。
    该周**仍然算存在**，不是 missing week。
    """
    if not daily_rows:
        return []
    buckets = {}  # weekKey -> normalized snapshot
    for r in daily_rows:
        ds = r.get("date")
        d = _date(ds)
        if d is None:
            continue
        wk = week_end_friday(d).isoformat()
        cand = normalize_snapshot({
            "snapshotDate": ds, "buy": r.get("buy"), "sell": r.get("sell"),
            "ratio": r.get("ratio"), "source": "jpx", "weekKey": wk,
            # ★ A3.1：日次侧若已带精确会社行為标志，必须透传，
            #   否则 confirmed 通路（corporate_action_confidence_map）收不到。
            "splitConfirmed": r.get("splitConfirmed"),
        })
        if cand is None:
            continue
        cur = buckets.get(wk)
        # 同周内取**日期更晚**的那条（最后实际交易日）
        if cur is None or cand["snapshotDate"] > cur["snapshotDate"]:
            buckets[wk] = cand
    return [buckets[k] for k in sorted(buckets)]


# ------------------------------------------------- 3. merge（同周：先比时间）
def _prefer_snapshot(new, old):
    """同 weekKey 内两条候选，判断 new 是否应胜出（覆盖 old）。

    ★ A2 修正的错误时间语义：
      旧实现是「JPX 无条件覆盖同周 Ganan」，于是同一周里
      JPX 只到周一（09-28）也会把 Ganan 周五（10-02）的数据盖掉 ——
      等于用更早的快照冒充整周。

    正确规则（严格按时间，再按来源）：
      1. snapshotDate 更晚者优先
      2. snapshotDate 相同 → JPX 优先于 Ganan
      3. 胜出者保留自己的 source
    """
    if old is None:
        return True
    nd = _date(new.get("snapshotDate"))
    od = _date(old.get("snapshotDate"))
    if nd is not None and od is not None and nd != od:
        return nd > od                       # 1) 时间更晚的优先
    if nd is None and od is not None:
        return False
    if nd is not None and od is None:
        return True
    # 2) 同一天（或都无日期）→ 比 source 优先级
    return (SOURCE_PRIORITY.get(new.get("source") or "unknown", 0)
            > SOURCE_PRIORITY.get(old.get("source") or "unknown", 0))


def merge_weekly(jpx_list, ganan_list, as_of=None):
    """合并 JPX 周次 与 Ganan 周次（仅处理**本轮输入**，不读历史 cache）。

    规则：
      · 以 weekKey（周五锚点）为连接键
      · 同 weekKey 用 _prefer_snapshot（先比 snapshotDate，再比 source）
      · 保留 sourceCandidates 仅供 audit（说明这一周有哪些候选、谁被淘汰）
      · 升序排序
      · provisional 由 mark_provisional() 统一标注
    """
    by_key = {}
    cands = {}

    def _feed(lst, default_source):
        for s in (lst or []):
            e = normalize_snapshot(s, default_source=default_source)
            if e is None:
                continue
            k = e["weekKey"]
            cands.setdefault(k, [])
            cands[k].append({"source": e["source"], "snapshotDate": e["snapshotDate"]})
            if _prefer_snapshot(e, by_key.get(k)):
                by_key[k] = e

    _feed(ganan_list, "ganan")
    _feed(jpx_list, "jpx")

    merged = [by_key[k] for k in sorted(by_key)]
    for s in merged:
        s["sourceCandidates"] = cands.get(s["weekKey"], [])
    return mark_provisional(merged, as_of)


def merge_series(existing_rows, new_rows, as_of=None):
    """★ A2：历史 cache 与本轮新输入按 weekKey 合并 —— 历史真正积累。

    规则：
      · 旧 weekKey **不会因为本轮输入没出现而被删除**
        （上游只返回最近 8 周时，已积累的 14 周必须仍在）
      · 同 weekKey 用同一套 _prefer_snapshot 规则更新（更晚 / 更可靠者覆盖）
      · 新 weekKey append
      · 按 weekKey 升序
    """
    by_key = {}
    cands = {}

    def _feed(lst, default_source):
        for s in (lst or []):
            e = normalize_snapshot(s, default_source=default_source)
            if e is None:
                continue
            k = e["weekKey"]
            for c in (e.get("sourceCandidates") or []):
                cands.setdefault(k, []).append(c)
            if _prefer_snapshot(e, by_key.get(k)):
                by_key[k] = e

    _feed(existing_rows, "unknown")
    _feed(new_rows, "unknown")

    merged = [by_key[k] for k in sorted(by_key)]
    for s in merged:
        sc = cands.get(s["weekKey"]) or s.get("sourceCandidates") or []
        # 去重并按时间排序，仅作 audit
        seen, uniq = set(), []
        for c in sorted(sc, key=lambda x: (x.get("snapshotDate") or "")):
            sig = (c.get("source"), c.get("snapshotDate"))
            if sig in seen:
                continue
            seen.add(sig)
            uniq.append(c)
        s["sourceCandidates"] = uniq
    return mark_provisional(merged, as_of)


def mark_provisional(series, as_of=None):
    """标注「当前自然周尚未结束」的快照。

    定义：weekKey（该周周五）**尚未过去** → provisional = True。
      as_of <= weekKey  → 该周还在进行 / 刚到周五（周五数据通常还没发布）
      as_of >  weekKey  → 该周已结束（哪怕因节假日只交易到周四，也算完成周）

    ★ as_of 默认取 **Asia/Tokyo 日期**（见 _today_jst），绝不依赖服务器部署时区。
      否则 UTC/本地时区与 JST 相差跨日时，会把「已完成周」误判为 provisional。

    ★ 节假日周（周五休市、周四收盘）**不是** provisional —— 周已结束，
      只是最后一个交易日是周四。这与「缺周」是两回事。
    """
    if as_of is None:
        as_of = _today_jst()
    for s in series:
        wk = _date(s.get("weekKey"))
        s["provisional"] = bool(wk is not None and as_of <= wk)
    return series


# ------------------------------------------------- 4. 公司行动检测
def _week_key_of(s):
    """取快照的 weekKey；缺失时由 snapshotDate 反推（兼容旧结构）。"""
    if s.get("weekKey"):
        return s["weekKey"]
    d = _date(s.get("snapshotDate") or s.get("weekEnding"))
    return week_end_friday(d).isoformat() if d else None


def detect_corporate_actions(series):
    """扫描相邻周次，返回发生拆并股的 **weekKey** 列表（週次ヒューリスティック =
    suspected —— 没有精确事件证据，只能「疑似」）。

    判定（比值守恒的大幅跳变）：
      buy / sell 同时同向跳变 ≥ 40%，且 ratio(买/卖) 的变化 < 10%
      → 两腿按相同比例缩放，倍率几乎不变，强烈暗示拆并股。
    不自动复权，仅标记，由上层比较逻辑暂停（停止解释，不制造交易信号）。

    ★ A2：返回 weekKey 而非 snapshotDate —— 趋势比较已全面改用 weekKey。

    ★ A3.1 保守性修正（先复现后修）：
      旧实现三处缺陷被 Codex Review 抓出 ——
        (1) 前一期売残=0、后一期>0 → 除零崩溃（守卫写错：把 `bb>0` 校验了两遍，
            漏掉了 `as_>0` / `bs>0`，于是 as_=0 仍能进入 `bs/as_`）。
        (2) 两期売残均为 0、買残扩大 3 倍 → 因两条腿都缺，完全无法验证同倍率，
            旧实现直接跳过 → 漏检。
        (3) 缺 ratio 时（ratio=None）→ `ratio_change=None` 被当成「比值守恒」通过
            → 買残增加/売残减少这类正常供需变化被误报为拆股。
      修正后的统一原则：**只要无法验证「同倍率缩放」的证据，就不判定会社行為**。
        · 任一脚（买或卖）为 None / 0 → 不能算 jump，跳过该相邻对；
        · ratio 任一为 None / 0 → 不能验证比值守恒，跳过（杜绝误报）；
        · 只有「四值齐备 > 0 且 ratio 守恒」时才标 suspected。
      这样既不崩溃、不产生 NaN/Inf，也不为追求检出率而扩大误报。
    """
    events = []
    for i in range(1, len(series)):
        a, b = series[i - 1], series[i]
        ab, bb = a.get("buy"), b.get("buy")
        as_, bs = a.get("sell"), b.get("sell")
        # 验证「同倍率缩放」所需 4 值必须全部 present 且 > 0；
        # 任一为 None/0 都无法验证，直接跳过（不崩溃、不误报）。
        if not (ab and bb and as_ and bs
                and ab > 0 and bb > 0 and as_ > 0 and bs > 0):
            continue
        buy_jump = abs(bb / ab - 1)
        sell_jump = abs(bs / as_ - 1)
        rf, rt = a.get("ratio"), b.get("ratio")
        # ratio 缺一 → 无法验证比值守恒 → 跳过（避免把供需变化误报为拆股）
        if rf is None or rt is None or rf == 0 or rt == 0:
            continue
        ratio_change = abs(rt / rf - 1)
        if min(buy_jump, sell_jump) > 0.4 and ratio_change < 0.10:
            k = _week_key_of(b)
            if k:
                events.append(k)
    return events


def corporate_action_confidence_map(events, series):
    """把检测到的会社行為 weekKey 映射到可信度。

    confirmed ：该 weekKey 的快照带有 `splitConfirmed` 标志（来自日次 Yahoo 精确事件）。
                —— 週次ヒューリスティック 自身は持ち得ない；日次侧が渡した場合のみ。
    suspected ：默认。週次残量变化のみからの推测，证据不够硬。
    none      ：无事件。

    返回 {weekKey: "confirmed" | "suspected"}。上层据此给 UI 选不同文案，
    绝不把 suspected 当成已确认。
    """
    by_key = {}
    for s in series:
        k = _week_key_of(s)
        if k:
            by_key[k] = s
    return {
        k: ("confirmed" if (by_key.get(k) or {}).get("splitConfirmed") else "suspected")
        for k in (events or [])
    }


# ------------------------------------------------- 5. 趋势计算
# ★ A2 / A2.5：不可用时 reason 的优先级。
#   先判断「窗口能否构成」：窗口起点早于最早已积累快照 → insufficient_history
#   （データ蓄積中，例如仅 11 周却要算 13W）；此时即便窗口内检测到公司行动，
#   也不能伪装成「已具备完整历史、只因拆股停止比较」。
#   窗口可构成时：corporate_action（比較停止）> missing_week（週次データ欠損）> normal。
REASON_CORPORATE_ACTION = "corporate_action"
REASON_MISSING_WEEK = "missing_week"
REASON_INSUFFICIENT_HISTORY = "insufficient_history"


def _unavailable(n, reason, weeks_present, missing, ca, provisional=False, ca_confidence=None):
    """构造「不可用」趋势 —— **绝不伪造百分比，也绝不给出假的起止日**。

    保留既有字段名（fromDate/toDate/buyPct/...）以便调用方不必分支判空，
    但数值一律为 None。

    ★ fromDate / toDate 也置 None：既然这次比较没有真正发生，
      就不该产出一对看起来「已经比较过」的日期 —— 那正是旧实现
      「缺周却照样报 08-28 → 10-02」的误导来源。

    新增 reason / missingWeeks / weeksNeeded / weeksPresent / caConfidence
    让 UI 能区分「没攒够」「中间缺周」「公司行动（确认/疑似）」这些情况。
    """
    if ca and ca_confidence is None:
        ca_confidence = "suspected"
    if not ca:
        ca_confidence = "none"
    return {
        "available": False,
        "reason": reason,
        "weeksNeeded": n + 1,
        "weeksPresent": weeks_present,
        "missingWeeks": missing or [],
        "fromDate": None,
        "toDate": None,
        "buyPct": None,
        "sellPct": None,
        "ratioFrom": None,
        "ratioTo": None,
        "corporateActionAffected": bool(ca),
        "caConfidence": ca_confidence,
        "provisional": bool(provisional),
    }


def compute_trend(series, n, events, as_of=None, allow_provisional=True, conf_map=None):
    """计算单条趋势（n = 4 / 8 / 13）—— 严格按 weekKey 对齐自然周。

    ★ A2 修正的错误语义：
      旧实现是 `frm = series[-1 - n]` —— 用**列表索引**假设「相邻快照 = 相邻自然周」。
      一旦中间缺周（例：08-28/09-04/09-11/09-25/10-02，缺 09-18），
      series[-5] 会静默跨过缺失周，把 35 天（5 个自然周）包装成「4W」。

    正确做法：
      1. 取 to = 最新可用快照（allow_provisional=False 时取最新**已完成**周）
      2. from_wk = to.weekKey - n 个自然周（精确日期运算，不是索引）
      3. 窗口内每个 weekKey 都必须存在；缺任一 → missing_week
      4. from_wk 早于最早快照 → insufficient_history（没攒够，不是缺周）
      5. 区间内有公司行动 → corporate_action（优先级最高）
    """
    if not series:
        return _unavailable(n, REASON_INSUFFICIENT_HISTORY, 0, [], False)

    # weekKey -> snapshot（同一个 weekKey 只可能有一条，merge 阶段已去重）
    idx = {}
    for s in series:
        k = _week_key_of(s)
        if k:
            idx[k] = s

    # 1) 选 to 端点
    cur = None
    for s in reversed(series):
        if allow_provisional or not s.get("provisional"):
            cur = s
            break
    if cur is None:
        return _unavailable(n, REASON_INSUFFICIENT_HISTORY, len(series), [], False)

    to_wk_d = _date(_week_key_of(cur))
    if to_wk_d is None:
        return _unavailable(n, REASON_INSUFFICIENT_HISTORY, len(series), [], False)

    # 2) 精确的自然周窗口（不是索引）
    from_wk_d = to_wk_d - datetime.timedelta(weeks=n)
    span = [(to_wk_d - datetime.timedelta(weeks=k)).isoformat() for k in range(n + 1)]
    span = list(reversed(span))            # 升序 [from_wk ... to_wk]
    from_wk, to_wk = span[0], span[-1]

    # 3) 缺周检测 —— 只统计「本应已积累到」的范围，
    #    早于最早快照的那些周属于「还没攒够」，不算 missing week。
    earliest_d = _date(_week_key_of(series[0]))
    floor_d = max(from_wk_d, earliest_d) if earliest_d else from_wk_d
    missing = [k for k in span if k not in idx and _date(k) >= floor_d]

    present = sum(1 for k in span if k in idx)
    provisional = bool(cur.get("provisional"))

    # 4) ★ A2.5：历史不足优先于公司行动（per-period 先判断「窗口能否构成」）。
    #    若窗口起点 weekKey 早于最早已积累快照（例如仅 11 周却要算 13W 的 14 周窗口），
    #    则根本无法构成比较窗口 —— 必须如实显示「データ蓄積中（N/14週）」，
    #    而不能因窗口内恰好检测到拆并股就伪装成「已具备完整历史、只因拆股停止比较」。
    #    公司行动只在「窗口确实能构成」（from_wk 已存在于历史）时才生效。
    if earliest_d and from_wk_d < earliest_d:
        return _unavailable(n, REASON_INSUFFICIENT_HISTORY, present, [], False, provisional)

    # 5) 公司行动（窗口已具备 → 停止比较）
    ca_keys = [ev for ev in (events or []) if from_wk < ev <= to_wk]
    if ca_keys:
        # 该窗口内的会社行為可信度：confirmed（日次精确事件）> suspected（週次推测）
        conf = "suspected"
        for ev in ca_keys:
            if (conf_map or {}).get(ev) == "confirmed":
                conf = "confirmed"
                break
        return _unavailable(n, REASON_CORPORATE_ACTION, present, missing, True, provisional,
                            ca_confidence=conf)
    if missing:
        return _unavailable(n, REASON_MISSING_WEEK, present, missing, False, provisional)
    if from_wk not in idx:
        return _unavailable(n, REASON_INSUFFICIENT_HISTORY, present, [], False, provisional)

    frm = idx[from_wk]
    return {
        "available": True,
        "reason": None,
        "weeksNeeded": n + 1,
        "weeksPresent": present,
        "missingWeeks": [],
        "fromDate": frm.get("snapshotDate") or from_wk,
        "toDate": cur.get("snapshotDate") or to_wk,
        "fromWeekKey": from_wk,
        "toWeekKey": to_wk,
        "buyPct": _pct(cur.get("buy"), frm.get("buy")),
        "sellPct": _pct(cur.get("sell"), frm.get("sell")),
        "ratioFrom": frm.get("ratio"),
        "ratioTo": cur.get("ratio"),
        "corporateActionAffected": False,
        "caConfidence": "none",
        "provisional": provisional,
    }


def compute_trends(series, events, as_of=None, allow_provisional=True, conf_map=None):
    return {
        "w4": compute_trend(series, 4, events, as_of, allow_provisional, conf_map),
        "w8": compute_trend(series, 8, events, as_of, allow_provisional, conf_map),
        "w13": compute_trend(series, 13, events, as_of, allow_provisional, conf_map),
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
        "note": ("Ganan 約10週履歴を bootstrap し、JPX 日次から生成した週次で更新。"
                 "同週は snapshotDate が新しい方を採用（同日なら JPX 優先）。"
                 "履歴は週次キャッシュに蓄積され、上流が短期間しか返さなくても削除しない。"
                 "4W/8W/13W は weekKey で自然週を厳密に揃え、欠週があれば比較しない。"),
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


def _load_weekly_cache(code, cache_dir, fp=None):
    """读取既有 merged 缓存 —— **只做版本闸门，不再用 fp 判脏**。

    ★ A2 修正：旧实现要求 `sourceFingerprint == fp` 才算命中，而 fp 只由
    **本轮输入**算出来的。于是上游某天只返回最近 8 周时，fp 变化 → 缓存失效
    → 用 8 周重算 → 已积累的 14 周历史被整段删除（coverageWeeks 14→8）。

    现在：版本一致就读出历史，与本轮输入按 weekKey 合并（见 merge_series），
    历史只增不减。fp 仍然记录，仅作为「本轮输入指纹」供审计，
    不再承担失效判定的职责。

    版本不一致（schema 变更）→ 返回 None → 由当前输入重建（A2 的 v1→v2 即如此）。
    """
    cpath = _weekly_cache_path(code, cache_dir)
    if not os.path.exists(cpath):
        return None
    try:
        with open(cpath, encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        return None
    if (cached.get("weeklyParserVersion") == WEEKLY_PARSER_VERSION
            and isinstance(cached.get("rows"), list)):
        return cached
    return None


def _series_signature(rows):
    """序列内容签名（用于判断是否需要重写缓存）。"""
    return json.dumps(
        [[r.get("weekKey"), r.get("snapshotDate"), r.get("buy"),
          r.get("sell"), r.get("ratio"), r.get("source"), r.get("provisional")]
         for r in (rows or [])],
        ensure_ascii=False, sort_keys=True)


def build_weekly_response(code, daily_rows, fresh=False,
                          cache_dir=CACHE_DIR_DEFAULT, as_of=None, upstream=None):
    """生成 /api/weekly 的完整响应 dict。

    入参 daily_rows 由调用方（server.py 的 gather）提供，本模块不重解析 JPX PDF，
    直接复用现有日次缓存。Ganan 周次由本模块抓取 + 缓存。

    upstream：server.gather 返回的 upstream_meta（含 stale / fetched / error）。
    ★ A3.2：其中的 stale 状态必须透传到响应，让 UI 能如实标注「週次数据来自
    降级缓存（上游刷新失败）」，不得把不同日期/降级数据伪装成最新。
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
        # 5) 本轮输入的周次序列（同周先比 snapshotDate，再比 source）
        incoming = merge_weekly(jpx, ganan, as_of=as_of)

        # 6) ★ A2：与既有历史按 weekKey 合并 —— 历史只增不减
        cached = _load_weekly_cache(code, cache_dir)
        prior = cached.get("rows") if (cached and isinstance(cached.get("rows"), list)) else None
        merged = merge_series(prior, incoming, as_of=as_of) if prior else incoming

        # 7) 公司行动 + 严格自然周趋势
        events = detect_corporate_actions(merged)
        conf_map = corporate_action_confidence_map(events, merged)
        trends = compute_trends(merged, events, as_of=as_of,
                               allow_provisional=True, conf_map=conf_map)
        sources = build_sources(ganan, jpx, merged)

        # ★ A3.1：把「会社行為影响」提升为响应级字段，供所有消费者（首页/详细页/
        #   数据来源卡/指标卡）统一判断，避免某一处仍把受影响区间解释为正常趋势。
        ca_flag = any((t or {}).get("corporateActionAffected") for t in trends.values())
        ca_conf = "none"
        for t in trends.values():
            c = (t or {}).get("caConfidence")
            if c == "confirmed":
                ca_conf = "confirmed"
                break
            if c == "suspected" and ca_conf != "confirmed":
                ca_conf = "suspected"

        latest = merged[-1] if merged else None
        resp = {
            "weeklyParserVersion": WEEKLY_PARSER_VERSION,
            "sourceFingerprint": fp,          # 本轮输入指纹（仅供审计，不再用于判脏）
            "cachedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "code": code,
            "rows": merged,
            "coverageWeeks": len(merged),
            # ★ A3.1：会社行為影响（确认/疑似）提升到响应级，UI 各消费者据此统一抑制解释
            "corporateActionAffected": ca_flag,
            "caConfidence": ca_conf,
            # ★ A2 新增：让调用方/UI 直接读到「当前周是否暫定」与时间序列边界
            "latestWeekKey": (latest or {}).get("weekKey"),
            "latestSnapshotDate": (latest or {}).get("snapshotDate"),
            "latestProvisional": bool((latest or {}).get("provisional")),
            "earliestWeekKey": (merged[0] or {}).get("weekKey") if merged else None,
            "trends": trends,
            "sources": sources,
        }

        # ★ A3.2：上游刷新状态透传（stale 贯穿 API 响应与 UI）。
        #   stale=True 表示本轮上游刷新失败、当前数据为降级缓存；除非有「成功的上游
        #   刷新」证据，否则不得伪装为最新。各字段含义：
        #     stale              —— 是否处于降级状态
        #     dataAsOf           —— 数据所属日期（最新周的实际数据日 / weekKey）
        #     lastSuccessfulUpdate —— 最近一次成功刷新时刻；stale 时为 null
        #     degraded           —— 是否降级使用缓存（stale 或无数据）
        #     freshServed        —— 本轮是否真的抓取了上游索引
        up = upstream or {}
        up_stale = bool(up.get("stale"))
        resp["stale"] = up_stale
        resp["dataAsOf"] = (latest or {}).get("snapshotDate") or (latest or {}).get("weekKey")
        resp["lastSuccessfulUpdate"] = (
            None if up_stale else datetime.datetime.now().isoformat(timespec="seconds"))
        resp["degraded"] = bool(up_stale or not merged)
        resp["freshServed"] = bool(up.get("fetched"))
        resp["upstream"] = {
            "gen": up.get("gen"),
            "fetched": up.get("fetched"),
            "reused": up.get("reused"),
            "stale": up_stale,
            "error": up.get("error"),
        }

        # 8) 仅在内容真变化时写盘（避免每次请求无谓重写）
        cpath = _weekly_cache_path(code, cache_dir)
        try:
            changed = True
            if cached is not None:
                changed = (_series_signature(cached.get("rows"))
                           != _series_signature(merged))
            if changed:
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
