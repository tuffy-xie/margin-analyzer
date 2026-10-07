#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日股信用残本地服务（路线 A）
=====================================================================
作用：抓取 JPX 官方「銘柄別信用取引残高」PDF（每交易日 16:00 更新，
     全銘柄・每日），解析成 JSON 供前端调用，从而绕过浏览器 CORS 限制。

数据源（实测 2026-10 有效）：
  索引页https://www.jpx.co.jp/markets/statistics-equities/margin/01.html
  文件  https://www.jpx.co.jp/markets/statistics-equities/margin/
         tvdivq0000001rnl-att/YYYYMMDD_mtall.pdf
         （YYYYMMDD = 申込日；9/25 改版后为每日公表）

启动：python3 server.py [端口]      默认 8848
接口：GET /api/margin?code=7974&days=20
      GET /api/health
"""

import sys
import re
import json
import io
import glob
import os
import gzip
import time
import hashlib
import datetime
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# 周次信用残趋势（4W/8W/13W）计算层：本模块只做编排，
# 实际计算见 weekly.py（独立模块，不引入循环依赖）。
import weekly

BASE = "https://www.jpx.co.jp"
INDEX = BASE + "/markets/statistics-equities/margin/01.html"
PDF_TMPL = BASE + "/markets/statistics-equities/margin/tvdivq0000001rnl-att/{ymd}_mtall.pdf"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ---------------------------------------------------------------- 版本与并发
# parserVersion：解析逻辑变更时必须 +1，否则旧的 JSON 缓存会被继续沿用。
PARSER_VERSION = 4
SCHEMA_VERSION = 1

# 每个日期一把锁：同一日期的 PDF 只允许一个线程下载/解析，
# 其余线程等待后直接复用结果（issue 7：半写文件被并发读取）。
_day_locks = {}
_day_locks_guard = threading.Lock()


def day_lock(day):
    """取得某日期的锁（进程内全局唯一）。"""
    with _day_locks_guard:
        lk = _day_locks.get(day)
        if lk is None:
            lk = threading.Lock()
            _day_locks[day] = lk
        return lk


# 进程内缓存： (code:days) -> 数据；另有 generation 防止旧请求覆盖新缓存
MEM = {}
MEM_GUARD = threading.Lock()


def mem_get(key):
    with MEM_GUARD:
        return MEM.get(key)


def mem_set(key, rows, generation=None):
    """写入进程缓存。

    ★ issue 7：refresh 与普通请求并行时，**旧请求不能覆盖新缓存**。
    做法：为每次写入附带 generation（单调递增的时间戳），
    只有 generation >= 已记录的 generation 才允许写入。
    """
    with MEM_GUARD:
        cur = MEM_META.get(key)
        if generation is not None and cur is not None and generation < cur:
            return False        # 旧请求，放弃写入
        MEM[key] = rows
        if generation is not None:
            MEM_META[key] = generation
        return True


MEM_META = {}   # key -> generation
MEM_GEN = [0]   # 单调递增计数器


def next_gen():
    with MEM_GUARD:
        MEM_GEN[0] += 1
        return MEM_GEN[0]


# ---------------------------------------------------------------- 上游索引
# ★ refresh 性能（issue 12）
#   一次用户主动 refresh（run(true) → 前端 a=0/1/2 最多 3 次 /api/margin）里，
#   「抓 JPX 索引页」这种昂贵动作只允许发生 **一次**。旧实现每个请求看到
#   fresh=1 就重新抓一次索引 + 重新解析全部 PDF，于是「点一次刷新」
#   = 3 × 昂贵刷新（实测单次 ~117s，最坏 350s+）。
#
#   现在的语义：
#     · 上游索引结果带 generation + 时间窗，同轮 refresh 的后继请求直接复用；
#     · 真正抓取时用 single-flight 锁，同一时刻只有一个线程在抓，
#       其余线程等它抓完再复用（并发安全语义不变：仍不会出现半写/重复下载）；
#     · 抓取失败 → 记负缓存 + 明确 stale，绝不把旧索引伪装成 fresh。
UPSTREAM_TTL_FRESH = 120.0   # fresh 请求：同一轮 refresh（2 分钟内）只抓一次
UPSTREAM_TTL_WARM = 600.0    # 普通请求：10 分钟内复用，避免每次查询都打 JPX
UPSTREAM_ERR_TTL = 30.0      # 抓取失败后的负缓存，避免连续重试把延迟放大
UPSTREAM_LIMIT = 40          # 索引最多保留的公表日数（API 上限 days=40）

_upstream = {"gen": 0, "ts": 0.0, "avail": [], "err": None}
_upstream_lock = threading.Lock()          # 保护 _upstream 字典
_upstream_fetch_lock = threading.Lock()    # single-flight：同时只有一个线程抓

# 观测计数：用于回答「一次 refresh 到底抓了几次上游」（测试与 /api/health 排查）
UPSTREAM_STATS = {"indexFetches": 0, "indexReuses": 0, "indexErrors": 0,
                  "parses": 0, "cacheHits": 0}


def reset_upstream():
    """清空上游索引状态（供测试隔离使用）。"""
    with _upstream_lock:
        _upstream.update({"gen": 0, "ts": 0.0, "avail": [], "err": None})
    for k in UPSTREAM_STATS:
        UPSTREAM_STATS[k] = 0


def _upstream_meta(st, age, fetched):
    """把上游状态包装成可回传给前端的元信息。"""
    return {
        "gen": st["gen"],
        "fetched": bool(fetched),
        "reused": not fetched,
        "age": round(age, 1),
        "error": st["err"],
        # stale：本次用的是「抓取失败后留下的旧索引」，属于降级，不是 fresh
        "stale": st["err"] is not None,
    }


def _upstream_reuse_ok(st, now, fresh):
    """当前状态能否直接复用（不抓上游）。"""
    if st["ts"] <= 0:
        return False, 0.0
    age = now - st["ts"]
    if st["err"] is not None:
        ttl = UPSTREAM_ERR_TTL
    elif fresh:
        ttl = UPSTREAM_TTL_FRESH
    else:
        ttl = UPSTREAM_TTL_WARM
    return age < ttl, age


def upstream_index(fresh=False, limit=15):
    """取可用 PDF 列表（日期倒序）+ 元信息。

    返回 (avail, meta)。meta["fetched"]=True 表示**这次真的抓了 JPX**；
    False 表示复用了已有 generation（同轮 refresh 的后继请求走这条）。
    """
    while True:
        # ---- 1) 能复用就直接返回（不触碰上游） ----
        with _upstream_lock:
            st = dict(_upstream)
            now = time.time()
            ok, age = _upstream_reuse_ok(st, now, fresh)
            if ok:
                UPSTREAM_STATS["indexReuses"] += 1
                return list(st["avail"])[:limit], _upstream_meta(st, age, False)

        # ---- 2) 需要真正抓一次：同一时刻只允许一个线程抓 ----
        if not _upstream_fetch_lock.acquire(blocking=False):
            # 已有线程在抓 → 等它结束再重新判断，绝不自己也抓一次
            _upstream_fetch_lock.acquire()
            _upstream_fetch_lock.release()
            continue

        try:
            # 拿到抓锁后再次确认（等待期间可能已被刷新）
            with _upstream_lock:
                st2 = dict(_upstream)
                now2 = time.time()
            ok2, age2 = _upstream_reuse_ok(st2, now2, fresh)
            if ok2:
                with _upstream_lock:
                    UPSTREAM_STATS["indexReuses"] += 1
                return list(st2["avail"])[:limit], _upstream_meta(st2, age2, False)

            try:
                avail = list_available(limit=max(limit, UPSTREAM_LIMIT))
                err = None
            except Exception as e:
                avail, err = [], f"{type(e).__name__}: {e}"

            with _upstream_lock:
                UPSTREAM_STATS["indexFetches"] += 1
                _upstream["ts"] = time.time()
                if err is not None:
                    # 失败：保留旧索引作为降级数据，但打上 err（→ stale），
                    # 绝不把它当成一次成功的 fresh。
                    UPSTREAM_STATS["indexErrors"] += 1
                    _upstream["err"] = err
                else:
                    _upstream["gen"] += 1
                    _upstream["avail"] = avail
                    _upstream["err"] = None
                snap = dict(_upstream)
            return list(snap["avail"])[:limit], _upstream_meta(snap, 0.0, True)
        finally:
            _upstream_fetch_lock.release()


# ---------------------------------------------------------------- 原子写
def atomic_write(path, data, binary=False):
    """先写唯一 .tmp，再 os.replace 原子替换。

    ★ issue 7：绝不能出现「文件已创建但内容没写完」的中间态。
    os.replace 在同一文件系统上是原子操作，读者要么看到旧完整文件，
    要么看到新完整文件，不存在看到半个文件的可能。
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
            os.fsync(f.fileno())      # 落盘后再 rename
        os.replace(tmp, path)          # 原子替换
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pdf_is_complete(path):
    """基本完整性校验：PDF 必须以 %PDF 开头、含 EOF，且大小合理。

    ★ issue 7：JPX 偶尔返回 HTML 错误页或被截断的 PDF。
    仅用 os.path.exists 判断存在性会把这种半成品当成有效缓存。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, "文件不存在"
    if size < 5000:
        return False, f"文件过小（{size} bytes）"
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
            f.seek(max(0, size - 2048))
            tail = f.read()
    except OSError as e:
        return False, f"读取失败：{e}"
    if not head.startswith(b"%PDF"):
        return False, "缺少 %PDF 文件头（可能是 HTML 错误页）"
    if b"%%EOF" not in tail:
        return False, "缺少 %%EOF 结尾（PDF 可能被截断）"
    return True, "ok"


# ---------------------------------------------------------------- 工具
def http_get(url, timeout=45, binary=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "ja,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "ignore")


def ymd(d):
    return d.strftime("%Y%m%d")


# ------------------------------------------------- 步骤1：发现可用 PDF
def list_available(limit=15):
    """从索引页解析出所有可下载的日次 PDF（申込日倒序）。"""
    html = http_get(INDEX)
    found = re.findall(
        r'href="([^"]*?/((?:\d{8})_mtall)\.pdf)"', html)
    #去重，按日期倒序
    seen, out = set(), []
    for href, fname in found:
        m = re.match(r"(\d{8})", fname)
        if not m:
            continue
        day = m.group(1)
        if day in seen:
            continue
        seen.add(day)
        out.append((day, BASE + href if href.startswith("/") else href))
    out.sort(reverse=True)
    return out[:limit]


# ------------------------------------------------- 步骤2：解析单个 PDF
def parse_pdf(code, path):
    """
    从 PDF 中抽出指定代码的一行，返回买卖残、倍率与扩展指标。

    解析要点（每条对应一个真实踩过的坑）：
      1. 代码必须**精确匹配**代码列，不能用子串 —— `if "3905" in line`
         会命中 ISIN（JP3539050009）与邻近代码，导致串标的。
      2. 「株数 Shs.」是锚点，其后是固定 14 列数据字段，
         用**固定列索引**定位（[0]=売残 [3]=買残 [2]/[5]=上場比），
         不再依赖「百分比往前第 3 个」—— 那样卖残=0 或上場比=* 时会错位。
      3. 字段正则要能匹配「0」和「*」，且「株数 Shs.」之前的内容（名称/ISIN）
         全部被截断，故名称里嵌数字（如 ETF「受益証1券」）不会串入。
      4. ▲ 表示减少（前日比），字段解析时转负号。
    """
    try:
        import pdfplumber
    except ImportError:
        return None, "缺少 pdfplumber，请先 pip install pdfplumber"

    try:
        with pdfplumber.open(path) as pdf:
            want = str(code)
            code_re = re.compile(rf"(?<!\d){re.escape(want)}\d?[A-Z]?(?!\d)")
            for page in pdf.pages:
                try:
                    text = page.extract_text() or ""
                except Exception:
                    continue
                for line in text.split("\n"):
                    if "株数" not in line:
                        continue
                    # 英文名行会重复代码，优先日文行（含假名/汉字）
                    if not re.search(r"[ぁ-んァ-ン一-鿿]", line):
                        continue
                    if not code_re.search(line):
                        continue

                    # ---- 锚点「株数 Shs.」之后 = 固定 14 列数据 ----
                    m = re.search(r"株数\s*Shs\.\s*(.*)$", line)
                    if not m:
                        continue
                    fields = re.findall(r"\*|▲\s*[\d,]+|[\d,]+(?:\.\d+)?%?", m.group(1))
                    if len(fields) < 6:
                        continue

                    def pf(s):
                        """解析单个字段：数字/▲数字/百分比/* → 数值或 None"""
                        if s is None:
                            return None
                        s = s.strip()
                        if s == "*":
                            return None
                        neg = s.startswith("▲")
                        s = s.replace("▲", "").replace("%", "").replace(",", "").strip()
                        if not s:
                            return None
                        try:
                            v = float(s) if "." in s else int(s)
                            return -v if neg else v
                        except ValueError:
                            return None

                    short = pf(fields[0]) or 0
                    long_ = pf(fields[3])
                    if long_ is None or long_ <= 0:
                        continue

                    rec = {
                        "code": code,
                        "sell": short,
                        "buy": long_,
                        "ratio": round(long_ / short, 2) if short > 0 else None,
                        "sellChg": pf(fields[1]),
                        "buyChg": pf(fields[4]),
                        "sellListed": pf(fields[2]),
                        "buyListed": pf(fields[5]),
                        # 扩展指标（列 6~13）：一般/制度信用的卖/买残
                        "negSell": pf(fields[6]) if len(fields) > 6 else None,
                        "stdSell": pf(fields[8]) if len(fields) > 8 else None,
                        "negBuy": pf(fields[10]) if len(fields) > 10 else None,
                        "stdBuy": pf(fields[12]) if len(fields) > 12 else None,
                    }

                    err = validate(rec)
                    if err:
                        print(f"  [skip] {code}: {err}", file=sys.stderr)
                        continue

                    # 证券種別写法不统一：
                    #   7974 →「普通株式 プライム」；285A →「普通株プライム」（省略「式」）
                    m2 = re.search(
                        r"\s([^\s\d][^\d\s]*?)\s+(?:普通株式?|優先株式?)", line)
                    rec["name"] = m2.group(1) if m2 else None
                    return rec, None
    except Exception as e:  # pragma: no cover
        return None, f"PDF 解析失败: {e}"
    return None, "该 PDF 中未找到此代码"


def validate(rec):
    """
    合理性校验。返回错误描述则判定为解析失败（多半是串标的）。
    原则：只拦「几乎不可能」的组合，宁可漏一条也不给错数据。

    注意：買残与売残的上場比本就可能差很多倍（買残通常是売残的数倍到
    数十倍），两者互相比较没有意义。真正的判据是「残量与上場比是否自洽」。
    """
    sell, buy = rec["sell"], rec["buy"]
    # 卖残=0 合法（无融券余额的 ETF/新股）；只要求买残 > 0
    if buy <= 0:
        return "买残非正"

    # 1) 買残不应低于売残的 1/100。低于此值在正常市场极罕见
    if buy * 100 < sell:
        return (f"買/売={buy/sell:.4f} 异常小，疑似串标的"
                f"（売{sell:,} 買{buy:,}）")

    # 2) 上場比必须 <= 100%
    for k in ("sellListed", "buyListed"):
        v = rec.get(k)
        if v and (v <= 0 or v > 100):
            return f"{k}={v} 超出 0~100%，疑似串标的"

    # 3) 残量与上場比自洽性：两者需指向同一「已上市股份数」。
    #    即 sell/sellListed 与 buy/buyListed 应在同一量级（允许 3 倍误差）。
    ls, lb = rec.get("sellListed") or 0, rec.get("buyListed") or 0
    if ls > 0 and lb > 0:
        implied_sell = sell / ls      # 卖残反推的上市股数
        implied_buy = buy / lb        # 买残反推的上市股数
        if max(implied_sell, implied_buy) / max(min(implied_sell, implied_buy), 1e-9) > 3:
            return (f"残量与上場比不自洽（反推上市股数 "
                    f"{implied_sell:,.0f} vs {implied_buy:,.0f}），疑似串标的")

    return None


def ensure_pdf(day, url):
    """确保某日期的 PDF 完整落盘。返回 (path, ok)。

    ★ issue 7 的核心修复：
      - per-date lock：同一日期只允许一个线程下载，其余等待后复用
      - 写入唯一 .tmp + os.replace 原子替换：读者永远看不到半写文件
      - 完整性校验：坏文件不落正式名，下一次会重新下载
    """
    ppath = os.path.join(CACHE_DIR, f"{day}_mtall.pdf")
    ok, _ = pdf_is_complete(ppath)
    if ok:
        return ppath, True

    with day_lock(day):
        # 双重检查：等锁期间可能已被别的线程下载好
        ok, _ = pdf_is_complete(ppath)
        if ok:
            return ppath, True

        # 旧的残缺文件先移除，避免 os.replace 前被误读
        if os.path.exists(ppath):
            try:
                os.unlink(ppath)
            except OSError:
                pass

        data = http_get(url, timeout=90, binary=True)
        # 先校验再落盘：坏内容不写正式文件
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix=".tmp_pdf_", suffix=".part")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            good, why = pdf_is_complete(tmp)
            if not good:
                raise ValueError(f"下载内容不完整（{why}）")
            os.replace(tmp, ppath)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return ppath, True


def cache_meta_ok(meta):
    """JSON 缓存的版本闸门（issue 10）。

    parserVersion / schemaVersion 不一致 → 视为无效，必须重新解析原始 PDF。
    """
    if not isinstance(meta, dict):
        return False, "无版本信息"
    if meta.get("schemaVersion") != SCHEMA_VERSION:
        return False, f"schemaVersion {meta.get('schemaVersion')} ≠ {SCHEMA_VERSION}"
    if meta.get("parserVersion") != PARSER_VERSION:
        return False, f"parserVersion {meta.get('parserVersion')} ≠ {PARSER_VERSION}"
    if not meta.get("sourceFingerprint"):
        return False, "无 sourceFingerprint"
    return True, "ok"


def fingerprint(code, day, ppath):
    """来源指纹：代码 + 日期 + 原始 PDF 的大小与 mtime。

    用于确认「这份 JSON 是由这份 PDF 解析出来的」。
    """
    try:
        st = os.stat(ppath)
        return f"{code}:{day}:{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return f"{code}:{day}:0:0"


def fetch_one_day(code, day, url, fresh=False):
    """下载（带缓存）并解析某一天。

    ★ issue 7 / issue 10：
      - 失败、不完整、解析异常的结果**绝不写入** JSON 缓存，也绝不进 MEM
      - JSON 带 schemaVersion / parserVersion / sourceFingerprint / parsedAt
      - parserVersion 变化 → 自动重新解析原始 PDF（fresh 不再是无条件吃旧 JSON）
    """
    cpath = os.path.join(CACHE_DIR, f"{code}_{day}.json")
    ppath = os.path.join(CACHE_DIR, f"{day}_mtall.pdf")

    # ---- 1) 读缓存（必须同时满足：版本一致 + 来源指纹一致） ----
    # ★ refresh 性能（issue 12）
    #   旧实现是 `if os.path.exists(cpath) and not fresh` —— 只要带 fresh=1
    #   就无条件重新解析 PDF。实测单个 mtall.pdf 解析约 50s，一次 refresh
    #   会把全部公表日重解析一遍 → 客户端 120s 超时 → 再重试 →「点一次刷新」
    #   变成 3 × 昂贵刷新。
    #
    #   fresh 的正确语义是「重新扫描上游有没有新的公表日」，而不是
    #   「把没变的 PDF 再解析一遍」。只要缓存版本与来源指纹
    #   （= PDF 大小 + mtime）仍然匹配，解析结果必然相同 → 安全复用。
    #   PDF 真的换了 / 换了版本 → 指纹或 parserVersion 不同 → 重新解析。
    fp = fingerprint(code, day, ppath)
    if os.path.exists(cpath):
        try:
            with open(cpath, "r", encoding="utf-8") as f:
                cached = json.load(f)
            good, why = cache_meta_ok(cached)
            if good and cached.get("sourceFingerprint") == fp:
                rec = cached.get("record")
                if isinstance(rec, dict) and rec.get("buy", 0) > 0:
                    UPSTREAM_STATS["cacheHits"] += 1
                    return rec
                print(f"  [stale] {day} {code}: 缓存内容不完整，重新解析", file=sys.stderr)
            else:
                print(f"  [reparse] {day} {code}: {why}", file=sys.stderr)
        except Exception as e:
            print(f"  [stale] {day} {code}: 缓存读取失败 {e}", file=sys.stderr)

    # ---- 2) 确保 PDF 完整存在（并发安全） ----
    try:
        ppath, _ = ensure_pdf(day, url)
    except Exception as e:
        print(f"  [warn] {day} {code}: PDF 下载失败 {e}", file=sys.stderr)
        return None            # ← 失败不缓存，不进 MEM

    fp = fingerprint(code, day, ppath)

    # ---- 3) 解析（同一日期串行，避免重复解析与半写竞争） ----
    with day_lock(day):
        # 等锁期间可能已被同 code 的另一请求写好
        if os.path.exists(cpath):
            try:
                with open(cpath, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                good, _ = cache_meta_ok(cached)
                if good and cached.get("sourceFingerprint") == fp:
                    rec = cached.get("record")
                    if isinstance(rec, dict) and rec.get("buy", 0) > 0:
                        return rec
            except Exception:
                pass

        rec, err = parse_pdf(code, ppath)
        UPSTREAM_STATS["parses"] += 1   # 真正的昂贵动作：解析 PDF
        if not rec:
            # 解析失败 / 该 PDF 里没有此代码 → 不写缓存（下次可重试）
            print(f"  [warn] {day} {code}: {err}", file=sys.stderr)
            return None

        try:
            d = datetime.datetime.strptime(day, "%Y%m%d").date()
            rec["date"] = d.isoformat()
            rec["shortMD"] = f"{d.month:02d}/{d.day:02d}"
        except ValueError:
            return None

        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "parserVersion": PARSER_VERSION,
            "sourceFingerprint": fp,
            "parsedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "code": code,
            "day": day,
            "record": rec,
        }
        try:
            atomic_write(cpath, json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            # 写缓存失败不影响本次返回（解析结果本身有效）
            print(f"  [warn] {day} {code}: 缓存写入失败 {e}", file=sys.stderr)
        return rec


def gather(code, days=20, fresh=False):
    """
    抓最近 days 个公表日的信用残，按日期升序返回。返回 (rows, upstream_meta)。

    fresh=True 时跳过进程缓存并重新扫描 JPX 索引页。
    重要：JPX 每日 16:00 才更新，若在 16:00 之后需要新数据，必须
    传 fresh=True（或重启服务），否则会一直拿到旧缓存。

    ★ issue 7：refresh 与普通请求并行时，旧请求不得覆盖新缓存
      —— 用 generation 单调标记，mem_set 会拒绝更旧的写入。
    ★ issue 7：空结果 / 失败结果**不写入** MEM，避免把失败当成功缓存。
    ★ issue 12：上游索引一轮 refresh 只抓一次（见 upstream_index）；
      命中进程缓存时完全不触碰上游，upstream_meta 为 None。
    """
    key = f"{code}:{days}"
    gen = next_gen()

    if not fresh:
        hit = mem_get(key)
        if hit is not None:
            return hit, None          # 进程缓存命中：零上游开销

    avail, up = upstream_index(fresh=fresh, limit=max(days, 12))
    rows = []
    for day, url in avail:
        if len(rows) >= days:
            break
        r = fetch_one_day(code, day, url, fresh=fresh)
        if r and r.get("buy", 0) > 0:
            rows.append(r)

    rows.sort(key=lambda x: x["date"])

    # 只有拿到数据才写进程缓存；空结果不入 MEM（下次会重试）
    if rows:
        mem_set(key, rows, generation=gen)
    return rows, up


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[api] " + (fmt % args) + "\n")

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开（切股票 / 刷新 / 超时）属正常情况。
            # 旧实现让它抛到 socketserver，日志里刷出整屏 traceback，
            # 反而把真正的错误淹掉。这里明确吞掉，不影响已完成的服务端工作。
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/api/health", "/health"):
            # 纯存活检查：**绝不做网络请求**，也不宣称验证了 JPX 链路。
            # 旧实现在这里调用 list_available()（会去抓 JPX 索引页），
            # 于是「健康检查」本身依赖外网往返；JPX 慢或正在解析 PDF 时
            # 前端 pingLocal() 就会超时 → 误判服务不可用 → 整页降级到
            # 无数据的 Ganan 源（表现为「信用残数据为空」，如 285A 加载失败）。
            #
            # 语义（issue 11c）：本接口只回答「本地服务是否在跑、缓存里有什么」，
            # **不代表** JPX 网络链路已验证。要验证链路请显式请求一个
            # /api/margin?code=... 或看 /api/health?probe=1。
            probe = (q.get("probe", ["0"])[0] or "0").lower() in ("1", "true", "yes")
            payload = {
                "ok": True,
                "service": "margin-analyzer local server",
                "parserVersion": PARSER_VERSION,
                "schemaVersion": SCHEMA_VERSION,
                "cachedPdfs": len(glob.glob(os.path.join(CACHE_DIR, "*.pdf"))),
                "cachedRecords": len(glob.glob(os.path.join(CACHE_DIR, "*_*.json"))),
                "jpxProbed": False,
                # 上游抓取次数（纯本地计数，不发网络请求）：
                # 用于排查「一次 refresh 到底抓了几次 JPX 索引页」。
                "upstream": dict(UPSTREAM_STATS),
                "note": "本接口仅检查本地服务与缓存，未验证 JPX 网络链路",
            }
            if probe:
                # 显式要求时才真正去探一次 JPX（可能较慢）
                try:
                    payload["availablePdfs"] = len(list_available(limit=5))
                    payload["jpxProbed"] = True
                except Exception as e:
                    payload["ok"] = False
                    payload["jpxError"] = str(e)
            self._send(payload)
            return

        if u.path in ("/api/weekly", "/weekly"):
            code = (q.get("code", [""])[0] or "").strip().upper()
            if not re.fullmatch(r"\d{3,4}[A-Z]?", code):
                self._send({"ok": False,
                            "err": "请提供 4 位股票代码（可带字母后缀，如 285A）"}, 400)
                return
            fresh = (q.get("fresh", ["0"])[0] or "0").lower() in ("1", "true", "yes")
            try:
                # 复用现有 JPX 日次抓取/缓存链，得到日次记录
                daily_rows, _up = gather(code, UPSTREAM_LIMIT, fresh=fresh)
            except Exception as e:
                self._send({"ok": False, "err": f"JPX 日次抓取失败: {e}"}, 500)
                return
            try:
                resp = weekly.build_weekly_response(code, daily_rows, fresh=fresh)
            except Exception as e:
                self._send({"ok": False, "err": f"周次计算失败: {e}"}, 500)
                return
            resp["ok"] = True
            resp["source"] = ("JPX 日次信用残（每交易日 16:00）+ Ganan 周次 bootstrap（免费）"
                              if daily_rows else
                              "Ganan 周次 bootstrap（JPX 日次暂不可用，仅 Ganan 约10週）")
            self._send(resp)
            return

        if u.path in ("/api/margin", "/margin"):
            code = (q.get("code", [""])[0] or "").strip().upper()
            # JPX 代码段：
            #   旧代码 = 4 位纯数字（7974）
            #   2024-01 起新增「数字+字母」（285A 铠侠、130A、43A 等）
            # 注意是 3~4 位数字 + 可选字母 —— 写成 \d{4}[A-Z]? 会要求
            # 「4 位数字后再跟字母」，而 285A 只有 3 位数字，匹配必然失败。
            if not re.fullmatch(r"\d{3,4}[A-Z]?", code):
                self._send({"ok": False, "err": "请提供 4 位股票代码（可带字母后缀，如 285A）"}, 400)
                return
            try:
                days = int(q.get("days", ["20"])[0])
                days = max(1, min(days, 40))
            except ValueError:
                days = 20
            fresh = (q.get("fresh", ["0"])[0] or "0").lower() in ("1", "true", "yes")
            try:
                rows, up = gather(code, days, fresh=fresh)
            except Exception as e:
                self._send({"ok": False, "err": f"抓取失败: {e}"}, 500)
                return

            up = up or {}
            # ★ issue 12-D：fresh 请求若上游刷新失败，必须**明确报错/降级**，
            #   绝不能把旧索引 / 旧数据静默伪装成 fresh。
            stale = bool(up.get("stale"))
            if fresh and stale and not rows:
                self._send({"ok": False, "upstream": up, "stale": True,
                            "freshServed": False,
                            "err": f"JPX 索引页抓取失败，无法刷新：{up.get('error')}"}, 502)
                return

            self._send({
                "ok": True,
                "code": code,
                "name": rows[-1]["name"] if rows else None,
                "rows": rows,
                "count": len(rows),
                "latest": rows[-1]["date"] if rows else None,
                "fresh": fresh,
                # freshServed：这份数据是否真的来自一次成功的上游刷新。
                # fresh=1 但上游失败 → False（前端必须提示降级，不能显示「已刷新」）。
                "freshServed": bool(fresh) and not stale,
                "stale": stale,
                "upstream": up,
                "frequency": "日次（每営業日公表）",
                "parserVersion": PARSER_VERSION,
                "source": "JPX 官方「銘柄別信用取引残高」（每交易日 16:00 公表）",
            })
            return

        if u.path in ("/", "/index.html"):
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "日股信用残分析.html")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    body = f.read()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

        self._send({"ok": False, "err": "not found"}, 404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8848
    print("=" * 60)
    print("日股信用残本地服务已启动")
    print(f"  接口  http://127.0.0.1:{port}/api/margin?code=7974&days=20")
    print(f"  健康  http://127.0.0.1:{port}/api/health")
    print("=" * 60)
    # 必须用 ThreadingHTTPServer：单线程 HTTPServer 会被 PDF 解析阻塞整个连接，
    # 导致前端 pingLocal() 在解析期间超时 → 误判「服务不可用」→ 整页降级到
    # 无数据的 Ganan 源（表现为「信用残数据为空」，如 285A 加载失败）。
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()