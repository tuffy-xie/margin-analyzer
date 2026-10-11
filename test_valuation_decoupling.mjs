/**
 * test_valuation_decoupling.mjs —— PER / PBR / EPS / BPS 不得影响信用需給评分
 * ----------------------------------------------------------------------------
 * 自动化回归（node test_valuation_decoupling.mjs）：
 *   构造「完全相同的信用数据」，仅改变估值输入，断言整条评分链路不变：
 *     · directionScore (dir) 完全相同
 *     · riskScore (risk) 完全相同
 *     · creditVerdict 等级 (grade) 完全相同
 *     · reasonCode / reasonFacts（信用需給结论文案）不因估值改变
 *
 *   Case A: PER=10, PBR=1
 *   Case B: PER=50, PBR=8
 *   再分别改变 EPS / BPS → 同样不得影响信用需給 verdict。
 *
 * 关键：走完整链路 MA2.computeIndicators(rows, val, ctx) → runRules → creditVerdict，
 * 而非只构造 ind，确保估值不会从 computeIndicators 这一层泄漏进信用指标。
 *
 * 运行：node test_valuation_decoupling.mjs
 */
import fs from 'fs';
import vm from 'vm';

const src = fs.readFileSync('./engine2.js', 'utf8');
const ctx = { window: {}, console };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const MA2 = ctx.window.MA2;

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra !== undefined ? '  → ' + JSON.stringify(extra) : '')); }
};

/* ---------- 相同的信用残数据（决定信用需給的唯一输入） ---------- */
const DAYS = ['2026-09-22','2026-09-23','2026-09-24','2026-09-25',
             '2026-09-28','2026-09-29','2026-09-30','2026-10-01'];
const CLOSE = [100, 98, 96, 95, 93, 91, 89, 88];          // 区间 -12%
const BUY   = [1000000,1010000,1030000,1050000,1070000,1090000,1110000,1120000]; // +12%
const rows = DAYS.map((d, i) => ({
  date: d, buy: BUY[i], sell: 60000,
  ratio: +(BUY[i] / 60000).toFixed(2), close: CLOSE[i],
}));
// 供 digestDays 计算「買残消化日数」用的完整行情（与股价同源）
const barsAll = CLOSE.map((c, i) => ({ date: DAYS[i], close: c, adjClose: c, vol: 1_000_000 }));

const SHARES = 1_679_057_667;   // 4519 级发行済株式数；固定不变，保证分母稳定
const CTX = { priceBarsAll: barsAll, lastMarginDate: '2026-10-01' };

/** 仅估值不同；信用残 rows / shares 完全一致 */
function val(extra) {
  return Object.assign({
    shares: SHARES, price: 88,
    perResult: null, pbrResult: null, divYield: null, dps: null,
  }, extra || {});
}

/** 走完整链路，返回一个可比较的评分快照 */
function snapshot(v) {
  const ind = MA2.computeIndicators(rows, v, CTX);
  const vr  = MA2.runRules(ind, {});
  const cv  = MA2.creditVerdict(ind, vr);
  return {
    dir: vr.dir, risk: vr.risk,
    grade: cv.grade, reasonCode: cv.reasonCode,
    reasonFacts: JSON.stringify(cv.reasonFacts),
  };
}

const base = { perResult: 10, pbrResult: 1 };          // Case A
const A = snapshot(val(base));
const B = snapshot(val({ perResult: 50, pbrResult: 8 }));   // Case B

console.log('\n=== 估值解耦：Case A (PER=10/PBR=1) vs Case B (PER=50/PBR=8) ===');
ok('directionScore 完全相同', A.dir === B.dir, { A: A.dir, B: B.dir });
ok('riskScore 完全相同', A.risk === B.risk, { A: A.risk, B: B.risk });
ok('creditVerdict 等级完全相同', A.grade === B.grade, { A: A.grade, B: B.grade });
ok('reasonCode 不因估值改变', A.reasonCode === B.reasonCode, { A: A.reasonCode, B: B.reasonCode });
ok('reasonFacts 不因估值改变', A.reasonFacts === B.reasonFacts,
   { A: A.reasonFacts, B: B.reasonFacts });
ok('评分确实由信用指标驱动（dir<0 且 risk>0）',
   typeof A.dir === 'number' && A.dir !== 0 && typeof A.risk === 'number' && A.risk > 0, A);

console.log('\n=== 极端估值也不影响信用需給 ===');
const extreme = snapshot(val({ perResult: 999, pbrResult: 999 }));
ok('PER/PBR=999 时 dir 仍与 Case A 相同', extreme.dir === A.dir, { extreme: extreme.dir, A: A.dir });
ok('PER/PBR=999 时 risk 仍与 Case A 相同', extreme.risk === A.risk, { extreme: extreme.risk, A: A.risk });
ok('PER/PBR=999 时 等级仍与 Case A 相同', extreme.grade === A.grade, { extreme: extreme.grade, A: A.grade });

console.log('\n=== 单独改变 EPS / BPS 不影响信用需給 ===');
const epsHi = snapshot(val({ ...base, eps: 999, bps: 50 }));
const epsLo = snapshot(val({ ...base, eps: 0.01, bps: 0.01 }));
ok('EPS 改变 → dir 不变', epsHi.dir === A.dir && epsLo.dir === A.dir, { epsHi: epsHi.dir, epsLo: epsLo.dir, A: A.dir });
ok('EPS 改变 → risk 不变', epsHi.risk === A.risk && epsLo.risk === A.risk, { epsHi: epsHi.risk, epsLo: epsLo.risk, A: A.risk });
ok('EPS 改变 → 等级不变', epsHi.grade === A.grade && epsLo.grade === A.grade, { epsHi: epsHi.grade, epsLo: epsLo.grade, A: A.grade });
ok('BPS 改变 → dir 不变', epsHi.dir === A.dir, { epsHi: epsHi.dir, A: A.dir });
ok('BPS 改变 → risk 不变', epsHi.risk === A.risk, { epsHi: epsHi.risk, A: A.risk });
ok('BPS 改变 → 等级不变', epsHi.grade === A.grade, { epsHi: epsHi.grade, A: A.grade });

console.log('\n=== valuationExcluded 仍如实列出 PER（证明「估值不参与评分」） ===');
{
  // PER>=25 才触发 per-high 信号；用高 PER 验证它会被列入「除外（不计入）」清单
  const hiPer = val({ perResult: 50, pbrResult: 8 });
  const ind = MA2.computeIndicators(rows, hiPer, CTX);
  const au = MA2.verdictAudit(ind, MA2.runRules(ind, {}), hiPer);
  ok('audit 含 valuationExcluded 清单', Array.isArray(au.valuationExcluded));
  ok('PER 信号被列入「除外（不计入）」清单',
     au.valuationExcluded.some(s => s.id === 'per-high'),
     au.valuationExcluded.map(s => s.id));
  // 同时确认：即便 PER 很高，上面 Case A 的 dir/risk/等级 校验已证明等级不受影响
}

console.log(`\n${fail === 0 ? '✅' : '❌'} 通过 ${pass} / 失败 ${fail}\n`);
process.exit(fail ? 1 : 0);
