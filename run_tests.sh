#!/usr/bin/env bash
# =============================================================================
# run_tests.sh —— 逐文件执行全部测试，逐个检查退出码
# =============================================================================
#
# 为什么要这个脚本（A4.3 暴露的两个真问题）
# ---------------------------------------------------------------------------
# 1) `node test_*.mjs` 这类 glob 一次执行，**拿不到每个文件各自的退出码**，
#    某个文件失败也会被后续文件的输出淹没。
# 2) `python3 test_weekly.py test_cache_concurrency.py` 是**真 bug**：
#    Python 只把第一个文件当脚本执行，第二个及以后被当成 sys.argv 参数丢弃
#    —— 看起来"跑了"，实际后面的测试**根本没执行**。
#
# 本脚本改为：对每个测试文件**单独**调用解释器，单独记录退出码，
# 并按「退出码约定」区分 PASS / FAIL / SKIP。
#
# 退出码约定
# ---------------------------------------------------------------------------
#   0  = PASS（全部断言真实执行并通过）
#   1  = FAIL（存在失败断言 / 未确认错误）
#   77 = SKIP（缺少外部夹具，断言未执行）—— **不得计为通过**
#
# 用法
# ---------------------------------------------------------------------------
#   ./run_tests.sh                # 常规模式：SKIP 不阻断（退出 0）
#   ./run_tests.sh --strict       # 严格模式：任何 SKIP 也判失败（退出 1）
#   ./run_tests.sh --no-browser   # 跳过需要本地服务的浏览器测试
#
# 环境变量
# ---------------------------------------------------------------------------
#   PYTHON        Python 解释器（默认 python3；需 pdfplumber 时请指向对应 venv）
#   NODE          Node 可执行文件（默认 node）
#   JPX_PDF       test_parse.py 用的 JPX 官方 PDF 路径（缺失 → 退出 77）
#   GANAN_FIXTURE test_weekly.py 用的 Ganan HTML 夹具路径（缺失 → 退出 77）
# =============================================================================

set -u

STRICT=0
NO_BROWSER=0
for arg in "$@"; do
  case "$arg" in
    --strict)    STRICT=1 ;;
    --no-browser) NO_BROWSER=1 ;;
    -h|--help)   sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg（支持 --strict / --no-browser）" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")" || exit 2

PYTHON="${PYTHON:-python3}"
NODE="${NODE:-node}"

pass_n=0; fail_n=0; skip_n=0
FAILED_FILES=""
SKIPPED_FILES=""

# --- 单个测试执行器 --------------------------------------------------------
# $1 = 显示名, $2.. = 命令
run_one() {
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?

  case "$rc" in
    0)  pass_n=$((pass_n+1))
        printf '  %-8s %s\n' "PASS" "$name" ;;
    77) skip_n=$((skip_n+1))
        SKIPPED_FILES="${SKIPPED_FILES}  ${name} (exit 77)\n"
        printf '  %-8s %s  ← 缺夹具，断言未执行\n' "SKIP" "$name"
        # 把 SKIP 的原因也打出来，避免"静默跳过"
        echo "$out" | grep -iE 'SKIP|跳过|夹具' | head -3 | sed 's/^/          /' ;;
    *)  fail_n=$((fail_n+1))
        FAILED_FILES="${FAILED_FILES}  ${name} (exit ${rc})\n"
        printf '  %-8s %s\n' "FAIL" "$name"
        echo "$out" | grep -E '❌|✗|Error|Traceback' | head -5 | sed 's/^/          /' ;;
  esac
  return 0
}

echo "============================================================"
echo "逐文件测试（解释器: PYTHON=${PYTHON} / NODE=${NODE}）"
echo "============================================================"

echo ""
echo "--- Node 测试 ---"
for f in test_*.mjs; do
  [ -e "$f" ] || continue
  run_one "$f" "$NODE" "$f"
done

echo ""
echo "--- Python 测试 ---"
for f in test_*.py; do
  [ -e "$f" ] || continue
  run_one "$f" "$PYTHON" "$f"
done

if [ "$NO_BROWSER" -eq 0 ]; then
  echo ""
  echo "--- 浏览器回归 ---"
  # 需要本地服务在 8848 上运行；未运行时该脚本自身会失败并计入 FAIL（不静默）
  run_one "verify_offline.py" "$PYTHON" verify_offline.py
fi

# --- 汇总 ------------------------------------------------------------------
echo ""
echo "============================================================"
echo "汇总：PASS ${pass_n} / FAIL ${fail_n} / SKIP ${skip_n}"
echo "============================================================"
if [ -n "$FAILED_FILES" ]; then
  echo "失败文件："
  printf "$FAILED_FILES"
fi
if [ -n "$SKIPPED_FILES" ]; then
  echo "跳过文件（缺外部夹具，断言未执行 —— 不计为通过）："
  printf "$SKIPPED_FILES"
fi
echo ""
echo "退出码约定：0=PASS  1=FAIL  77=SKIP（不计为通过）"

# --- 最终判定 --------------------------------------------------------------
if [ "$fail_n" -gt 0 ]; then
  echo ""
  echo "❌ 存在失败测试"
  exit 1
fi
if [ "$STRICT" -eq 1 ] && [ "$skip_n" -gt 0 ]; then
  echo ""
  echo "❌ 严格模式：存在 ${skip_n} 个 SKIP（缺夹具），不得宣称完整通过"
  echo "   补齐夹具后重跑，或用 JPX_PDF=/ GANAN_FIXTURE= 指定外部文件："
  echo "     JPX_PDF=/path/to/x.pdf GANAN_FIXTURE=/path/to/y.html ./run_tests.sh --strict"
  exit 1
fi
if [ "$skip_n" -gt 0 ]; then
  echo ""
  echo "⚠️  有 ${skip_n} 个 SKIP（非严格模式放行）—— 通过项不含未执行的断言"
  exit 0
fi
echo ""
echo "✅ 全部测试通过（无失败、无跳过）"
exit 0