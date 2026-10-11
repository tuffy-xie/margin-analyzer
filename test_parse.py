"""JPX PDF parser 回归测试：卖残=0 解析 + 正常代码无回归 + 不串标"""
import sys, os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

# 固定夹具日期；JPX PDF 属于本地缓存（17MB+ / 单份 1.7MB），**不随仓库分发**。
#
# ★ A4.3 §3：不得把「缺夹具造成的 SKIP」算作 PASS。
#   旧实现在此处直接 sys.exit(0)，CI 会把"根本没跑"当成"通过"。
#   现在改为：打印机器可读的 SKIP 标记，并用**专用退出码 77** 与
#   PASS(0) / FAIL(1) 明确区分。
#
# 最小可复现方案（任选其一）：
#   1) 环境变量指向任意一份 JPX 官方 PDF：
#        JPX_PDF=/path/to/YYYYMMDD_mtall.pdf python test_parse.py
#   2) 让本地服务抓过一次后复用缓存：
#        python server.py 8848   然后浏览器查任意日次代码 → .cache/ 下即生成 PDF
PDF = Path(os.environ.get('JPX_PDF')
           or (Path(__file__).resolve().parent / '.cache' / '20261002_mtall.pdf'))
if not PDF.is_file():
    print('RESULT: SKIPPED (缺夹具 JPX PDF)')
    print('SKIP: 解析测试需要一份 JPX 官方 PDF；')
    print('      设置 JPX_PDF=<path> 或让本地服务抓取一次生成 .cache/20261002_mtall.pdf。')
    print('      注意：SKIP **不计为通过**（退出码 77，区别于 PASS 的 0）。')
    sys.exit(77)
PDF = str(PDF)
day = '20261002'
print(f'测试日: {day}, 文件: {os.path.basename(PDF)}')

pass_n = 0; fail_n = 0
def ok(cond, name, extra=None):
    global pass_n, fail_n
    if cond: pass_n += 1; print('  ✅ ' + name)
    else: fail_n += 1; print('  ❌ ' + name + (f' → {extra}' if extra is not None else ''))

# 正常代码回归（数值必须与已知基准一致）
expected = {
    '7974': ('任天堂', 693900, 9324600, 13.44),
    '3905': ('データセクション', 1848100, 10110900, 5.47),
    '6920': ('レーザーテック', 680800, 1175100, 1.73),
    '4519': ('中外製薬', 41800, 2253800, 53.92),
    '285A': ('キオクシアホールディングス', 2575900, 41467700, 16.10),
}
print('\n=== 正常代码无回归 ===')
for code, (name, sell, buy, ratio) in expected.items():
    r, e = server.parse_pdf(code, PDF)
    if not r:
        ok(False, f'{code} 无数据: {e}'); continue
    ok(r['sell'] == sell and r['buy'] == buy, f'{code} 売{r["sell"]:,} 買{r["buy"]:,}', (r['sell'], r['buy']))
    ok(abs(r['ratio'] - ratio) < 0.01, f'{code} 倍率 {r["ratio"]}', r['ratio'])
    ok(r['name'] == name, f'{code} 名称 {r["name"]}', r['name'])

print('\n=== 卖残=0 正确解析 ===')
r, e = server.parse_pdf('130A', PDF)
if r:
    ok(r['sell'] == 0, '130A 卖残=0', r['sell'])
    ok(r['buy'] == 276400, '130A 买残=276,400', r['buy'])
    ok(r['ratio'] is None, '130A 倍率=None（不除零）', r['ratio'])
    ok(r['buyListed'] == 4.3, '130A 上場比=4.3%', r['buyListed'])
else:
    ok(False, f'130A 无数据: {e}')

# ETF（上場比 *）
r, e = server.parse_pdf('3190', PDF)
if r:
    ok(r['sell'] == 0, '3190 卖残=0', r['sell'])
    ok(r['buy'] == 6000, '3190 买残=6,000', r['buy'])
    ok(r['ratio'] is None, '3190 倍率=None', r['ratio'])
    ok(r['buyListed'] is None, '3190 上場比=* → None', r['buyListed'])
else:
    ok(False, f'3190 无数据: {e}')

print('\n=== 不串标（名称嵌数字的 ETF）===')
# 13990 是名称乱码的 ETF，代码列被 PDF 提取打乱，应返回"未找到"而非串标到别的股
r, e = server.parse_pdf('13990', PDF)
if r:
    # 如果返回了，name 不能是无关股票（如ホットマン）
    ok(r['name'] != 'ホットマン', '13990 未串标到ホットマン', r.get('name'))
else:
    ok('未找到' in str(e) or 'PDF' in str(e), '13990 安全返回"未找到"（不串标）', e)

print(f'\n结果：{pass_n} 通过，{fail_n} 失败，0 跳过')
sys.exit(1 if fail_n else 0)
