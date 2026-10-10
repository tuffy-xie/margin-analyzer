"""test_cache_concurrency.py —— server.py 缓存并发与版本回归（issue 7 / 10）

覆盖用户指定的 G 项：
  G. 两个线程同时请求同一 PDF
     - 不得解析半写文件
     - 不得把失败 / 空结果永久写入 MEM

另含issue 10（parserVersion / schemaVersion / sourceFingerprint）验证。

运行：python test_cache_concurrency.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

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


# ---------------------------------------------------------------- 隔离沙箱
REAL_CACHE = server.CACHE_DIR
SANDBOX = tempfile.mkdtemp(prefix='ma_cache_test_')
server.CACHE_DIR = SANDBOX
server.MEM.clear()
server.MEM_META.clear(); server.MEM_STALE.clear()


def restore():
    server.CACHE_DIR = REAL_CACHE


# ---------------------------------------------------------------- 假数据
def make_fake_pdf(path, payload=b'x' * 6000):
    """构造一个通过完整性校验的最小 PDF。"""
    body = b'%PDF-1.4\n' + payload + b'\n%%EOF\n'
    with open(path, 'wb') as f:
        f.write(body)
    return len(body)


print('=== 原子写与完整性校验 ===')
if True:
    p = os.path.join(SANDBOX, 'atomic.json')
    server.atomic_write(p, json.dumps({'a': 1}, ensure_ascii=False))
    ok(os.path.exists(p), 'atomic_write 后文件存在')
    with open(p, encoding='utf-8') as f:
        ok(json.load(f) == {'a': 1}, 'atomic_write 内容正确')
    # 不应留下 .tmp 残留
    leftovers = [f for f in os.listdir(SANDBOX) if '.part' in f]
    ok(not leftovers, '无 .tmp 残留文件', leftovers)

    # 不完整 PDF 必须被拒绝
    bad = os.path.join(SANDBOX, 'bad_mtall.pdf')
    with open(bad, 'wb') as f:
        f.write(b'<html>404 Not Found</html>' * 100)
    good, why = server.pdf_is_complete(bad)
    ok(not good, 'HTML 错误页被判为不完整', why)

    trunc = os.path.join(SANDBOX, 'trunc_mtall.pdf')
    with open(trunc, 'wb') as f:
        f.write(b'%PDF-1.4\n' + b'y' * 6000)   # 没有 %%EOF
    good2, why2 = server.pdf_is_complete(trunc)
    ok(not good2, '截断 PDF（无 %%EOF）被判为不完整', why2)

    tiny = os.path.join(SANDBOX, 'tiny_mtall.pdf')
    with open(tiny, 'wb') as f:
        f.write(b'%PDF-1.4\n%%EOF\n')
    good3, why3 = server.pdf_is_complete(tiny)
    ok(not good3, '过小文件被判为不完整', why3)

    okp = os.path.join(SANDBOX, 'ok_mtall.pdf')
    make_fake_pdf(okp)
    good4, why4 = server.pdf_is_complete(okp)
    ok(good4, '合法 PDF 通过校验', why4)

print('\n=== issue 10：缓存版本闸门 ===')
if True:
    # 版本不一致 → 必须重新解析
    good, why = server.cache_meta_ok({'schemaVersion': server.SCHEMA_VERSION,
                                      'parserVersion': server.PARSER_VERSION,
                                      'sourceFingerprint': 'x'})
    ok(good, '版本一致 → 缓存有效', why)

    good2, why2 = server.cache_meta_ok({'schemaVersion': server.SCHEMA_VERSION,
                                        'parserVersion': server.PARSER_VERSION - 1,
                                        'sourceFingerprint': 'x'})
    ok(not good2, 'parserVersion 落后 → 缓存作废（必须重解析）', why2)

    good3, why3 = server.cache_meta_ok({'schemaVersion': server.SCHEMA_VERSION - 1,
                                        'parserVersion': server.PARSER_VERSION,
                                        'sourceFingerprint': 'x'})
    ok(not good3, 'schemaVersion 不一致 → 缓存作废', why3)

    good4, why4 = server.cache_meta_ok({'parserVersion': server.PARSER_VERSION,
                                        'sourceFingerprint': 'x'})
    ok(not good4, '无 schemaVersion（旧格式）→ 缓存作废', why4)

    good5, why5 = server.cache_meta_ok({'schemaVersion': server.SCHEMA_VERSION,
                                        'parserVersion': server.PARSER_VERSION})
    ok(not good5, '无 sourceFingerprint → 缓存作废', why5)

print('\n=== issue 7：并发不得解析半写文件 ===')
if True:
    day = '20260101'
    pdf = os.path.join(SANDBOX, f'{day}_mtall.pdf')
    make_fake_pdf(pdf)

    # 让 parse_pdf 变成「慢速」：读取时先睡一下，模拟真实解析耗时
    real_parse = server.parse_pdf
    parse_calls = []
    parse_lock = threading.Lock()

    def slow_parse(code, path):
        with parse_lock:
            parse_calls.append(path)
        time.sleep(0.25)                    # 留出窗口给并发线程
        ok_p, _ = server.pdf_is_complete(path)
        if not ok_p:
            return None, 'PDF 不完整'
        return {'code': code, 'sell': 1000, 'buy': 500000, 'ratio': 500.0,
                'name': 'テスト', 'buyListed': 1.0, 'sellListed': 0.1}, None

    server.parse_pdf = slow_parse
    try:
        results = {}
        errors = []

        def worker(tag, code):
            try:
                results[tag] = server.fetch_one_day(code, day, 'http://unused')
            except Exception as e:            # 不允许异常逃逸
                errors.append(f'{tag}: {e}')

        # 5 个线程同时请求同一日期的不同代码
        threads = [threading.Thread(target=worker, args=(f't{i}', f'797{i}'))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        ok(not errors, '并发调用无异常逃逸', errors)
        ok(len(results) == 5, '全部线程拿到结果', list(results.keys()))
        ok(all(r is not None for r in results.values()), '无空结果')

        # 每个线程在解析时看到的 PDF 都必须是完整的
        ok(len(parse_calls) >= 1, 'parse 至少被调用一次', len(parse_calls))

        # 校验：解析期间读到的文件都通过了完整性检查（即没有半写文件）
        # （slow_parse 内部已校验，若读到半写会返回 None 并导致上面断言失败）

        # 每个 code 都应生成独立 JSON，且带完整版本信息
        for i in range(5):
            code = f'797{i}'
            cpath = os.path.join(SANDBOX, f'{code}_{day}.json')
            ok(os.path.exists(cpath), f'{code} 生成 JSON 缓存')
            if os.path.exists(cpath):
                with open(cpath, encoding='utf-8') as f:
                    meta = json.load(f)
                has_all = all(k in meta for k in
                              ('schemaVersion', 'parserVersion', 'sourceFingerprint', 'parsedAt'))
                ok(has_all, f'{code} JSON 含全部版本字段', list(meta.keys()))
                good, why = server.cache_meta_ok(meta)
                ok(good, f'{code} JSON 通过版本闸门', why)

        # 无 .tmp/.part 残留
        leftovers = [f for f in os.listdir(SANDBOX) if '.part' in f]
        ok(not leftovers, '并发结束后无临时文件残留', leftovers)
    finally:
        server.parse_pdf = real_parse

print('\n=== issue 7：失败 / 空结果不得写入 MEM ===')
if True:
    server.MEM.clear()
    server.MEM_META.clear(); server.MEM_STALE.clear()
    day2 = '20260102'
    pdf2 = os.path.join(SANDBOX, f'{day2}_mtall.pdf')
    make_fake_pdf(pdf2)

    real_parse = server.parse_pdf
    # 解析失败
    server.parse_pdf = lambda code, path: (None, '该 PDF 中未找到此代码')
    try:
        r = server.fetch_one_day('7974', day2, 'http://unused')
        ok(r is None, '解析失败返回 None', r)
        cpath = os.path.join(SANDBOX, f'7974_{day2}.json')
        ok(not os.path.exists(cpath), '解析失败不写 JSON 缓存')

        # gather 得到空结果 → 不进 MEM
        orig_list = server.list_available
        server.list_available = lambda limit=15: [(day2, 'http://unused')]
        try:
            rows, _up = server.gather('7974', 5, fresh=True)
            ok(rows == [], 'gather 返回空列表', rows)
            key = '7974:5'
            ok(server.MEM.get(key) is None, '空结果未写入 MEM')
        finally:
            server.list_available = orig_list
    finally:
        server.parse_pdf = real_parse

print('\n=== issue 7：旧请求不得覆盖新缓存 ===')
if True:
    server.MEM.clear()
    server.MEM_META.clear(); server.MEM_STALE.clear()
    key = '7974:5'
    new_rows = [{'date': '2026-01-02', 'buy': 200000, 'sell': 1000}]
    old_rows = [{'date': '2026-01-01', 'buy': 100000, 'sell': 900}]

    gen_new = server.next_gen()
    gen_old = gen_new - 1

    server.mem_set(key, new_rows, generation=gen_new)
    ok(server.MEM.get(key) is new_rows, '新请求写入成功')

    written = server.mem_set(key, old_rows, generation=gen_old)
    ok(not written, '旧请求写入被拒绝', written)
    ok(server.MEM.get(key) is new_rows, 'MEM 仍保持新数据')

    # 同代或更新代允许写入
    written2 = server.mem_set(key, old_rows, generation=gen_new)
    ok(written2, '同代写入允许')
    written3 = server.mem_set(key, new_rows, generation=gen_new + 1)
    ok(written3, '更新代写入允许')

print('\n=== issue 7：PDF 完整性校验会促使重下 ===')
if True:
    # 正式位置存在但内容损坏 → ensure_pdf 应尝试重新下载并替换
    day3 = '20260103'
    p3 = os.path.join(SANDBOX, f'{day3}_mtall.pdf')
    with open(p3, 'wb') as f:
        f.write(b'<html>error</html>' * 200)     # 坏文件

    downloads = []

    def fake_get(url, timeout=45, binary=False):
        downloads.append(url)
        time.sleep(0.05)
        return b'%PDF-1.4\n' + b'z' * 6000 + b'\n%%EOF\n'

    real_get = server.http_get
    server.http_get = fake_get
    try:
        path, good = server.ensure_pdf(day3, 'http://example/x.pdf')
        ok(good, 'ensure_pdf 成功')
        ok(len(downloads) == 1, '损坏文件触发了重新下载', downloads)
        g, why = server.pdf_is_complete(p3)
        ok(g, '重下后文件完整', why)
        # 第二次调用不应再下载
        downloads.clear()
        server.ensure_pdf(day3, 'http://example/x.pdf')
        ok(len(downloads) == 0, '完整文件不再重复下载', downloads)
    finally:
        server.http_get = real_get

# ---------------------------------------------------------------- 清理
restore()
shutil.rmtree(SANDBOX, ignore_errors=True)

print(f'\n结果：{pass_n} 通过，{fail_n} 失败')
sys.exit(1 if fail_n else 0)