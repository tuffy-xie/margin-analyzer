/**
 * test_layer.mjs —— 信息层级 / 评分作用域 回归测试
 * ------------------------------------------------------------------
 * 验证本次调整的四条硬约束：
 *   A. PER / PBR / 配当 绝不进入信用需給评分（DIR_RULES / RISK_RULES）
 *   B. 首页展示对象只含等级与文字档位，不含 0~100 内部分数
 *   C. creditVerdict 等级只由信用指标决定；估值变化不改变等级
 *   D. creditPosition 三档（軽い/普通/重い）与阈值一致
 *
 * 运行：node test_layer.mjs
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
  else { fail++; console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); }
};

/* ---------- 构造一个可控的信用指标集 ---------- */
function mkInd(o = {}) {
  const dg = Object.assign(
    { days: 1.0, level: 'ok', label: '正常', period: 20, avgVolume: 1e6, fallback: false,
      days5: 0.9, avgVolume5: 1.1e6, samples: 20, totalValid: 485,
      winFrom: '2026-08-01', winTo: '2026-09-25', cutoff: '2026-09-25', note: null },
    o.digest || {});
  const bl = Object.assign(
    { buyListed: 1.0, sellListed: 0.01, shares: 1e8, sharesSource: 'Ganan 発行済株式数',
      buyListedJpx: 1.1, sellListedJpx: 0.01, denomStable: true, denomNote: null,
      impliedListedShares: 1e8, note: null,
      // ★ A1：默认 fixture 的 denomStable=true 且分母来自 Ganan 発行済株式数
      //   → 属**可信**分母。必须显式给出，否则 creditPosition 会按
      //   「分母可信度未知」fail-closed 把発行済比排除掉（契约要求如此）。
      denominator: { confidence: 'high', usableAsHardTrigger: true,
                     value: 1e8, type: '発行済株式数' } },
    o.borrowRate || {});
  const q = Object.assign(
    { key: 'strong', label: '🟢 強い / 健全', level: 'ok', color: '#22c55e',
      buyChg: -6.7, priceChg: 8.5, text: '' },
    o.quadrant || {});
  const long = Object.assign({ value: 1e6, chg1: -1, chgN: -6.7, chg13w: null,
    ma5: null, ma20: null, maN: null, aboveMa5: null, aboveMa20: null, aboveMaN: null,
    listedRatio: null }, o.long || {});
  const short = Object.assign({ value: 1e4, chg1: 0, chgN: -2, chg13w: null,
    ma5: null, ma20: null, maN: null, aboveMa5: null, aboveMa20: null, aboveMaN: null,
    listedRatio: null }, o.short || {});
  const ratio = Object.assign({ value: 13.44, min: 5, max: 20, mean: 12, pos: 60,
    rangeWindow: 5, chg1: 1, chgN: 3,
    absoluteLevel: MA2.absoluteRatioLevel(o.ratioValue ?? 13.44) },
    o.ratio || {});
  const ratioDecomp = Object.assign(
    { ratio: ratio.value, cause: 'longBuild', causeText: '', level: 'warn',
      buyChg: long.chgN, sellChg: short.chgN, buyHigh: 60, sellLow: 30,
      buyListed: bl.buyListed },
    o.ratioDecomp || {});
  return Object.assign({
    span: 4,
    dataRange: { n: 5, from: '2026-09-18', to: '2026-09-25', has13w: false },
    ratio, ratioDecomp, long, short, digest: dg, borrowRate: bl, quadrant: q,
    trend: { buySlope: 0, sellSlope: 0, ratioSlope: 0, netFlow: 0, span: 4 },
    creditToMcap: { price: 1000, mcap: 1e9, creditValue: 1e9, ratio: 1, note: null },
    per: o.per ?? { now: 35.18, result: 33, forecast: 35.18 },
    pbr: o.pbr ?? { now: 2.1, result: 2.1, forecast: 2.05 },
    dividend: o.dividend ?? { yield: 1.2, dps: 30 },
  }, o.extra || {});
}

console.log('\n=== A. 估值指标不得进入信用需給评分 ===');
{
  const dirIds = MA2.DIR_RULES.map(r => r.id);
  const riskIds = MA2.RISK_RULES.map(r => r.id);
  ok('DIR_RULES 不含 per-high', !dirIds.includes('per-high'), dirIds.join(','));
  ok('DIR_RULES 不含 div-good', !dirIds.includes('div-good'));
  ok('RISK_RULES 不含任何估值规则',
     !riskIds.some(id => /per|pbr|div|dividend|bps|eps/i.test(id)), riskIds.join(','));

  // 逐条扫描规则体，确认没有任何一条读取 i.per / i.pbr / i.dividend
  const all = MA2.DIR_RULES.concat(MA2.RISK_RULES);
  const leaky = all.filter(r => /\bi\.(per|pbr|dividend)\b/.test(r.test.toString()));
  ok('没有任何规则读取 i.per / i.pbr / i.dividend', leaky.length === 0,
     leaky.map(r => r.id).join(','));

  // PER 高低不应改变 dir / risk
  const loPer = MA2.runRules(mkInd({ per: { now: 8 } }), {});
  const hiPer = MA2.runRules(mkInd({ per: { now: 90 } }), {});
  ok('PER 8倍 与 PER 90倍 的 dir 相同', loPer.dir === hiPer.dir,
     `${loPer.dir} vs ${hiPer.dir}`);
  ok('PER 8倍 与 PER 90倍 的 risk 相同', loPer.risk === hiPer.risk,
     `${loPer.risk} vs ${hiPer.risk}`);

  // 股息同样不影响
  const noDiv = MA2.runRules(mkInd({ dividend: { yield: 0.1 } }), {});
  const hiDiv = MA2.runRules(mkInd({ dividend: { yield: 8 } }), {});
  ok('配当利回り 0.1% 与 8% 的 dir 相同', noDiv.dir === hiDiv.dir,
     `${noDiv.dir} vs ${hiDiv.dir}`);
}

console.log('\n=== B. 首页展示对象不含 0~100 内部分数 ===');
{
  const ind = mkInd();
  const vr = MA2.runRules(ind, {});
  const cv = MA2.creditVerdict(ind, vr);

  ok('creditVerdict 返回 grade 等级键', ['good','mid','warn','bad'].includes(cv.grade), cv.grade);
  ok('badge 只含等级文字，无 /100',
     !/\d+\s*\/\s*100/.test(cv.badge) && !/\d/.test(cv.badge.replace(/短期信用需給|改善|中立|注意|悪化|注意な急騰|（|）/g, '')),
     cv.badge);
  ok('grade 为 good 时 badge 含「改善」', cv.badge.includes('改善'), cv.badge);
  ok('creditVerdict 不返回 dir / risk 数字',
     cv.dir === undefined && cv.risk === undefined);
  ok('内部分数仍可通过 verdictAudit 审计（供详细页）',
     typeof vr.dir === 'number' && typeof vr.risk === 'number');

  const au = MA2.verdictAudit(ind, vr, {});
  ok('audit 含 dir/risk 数字', typeof au.dir === 'number' && typeof au.risk === 'number');
  ok('audit 列出被排除的估值信号', Array.isArray(au.valuationExcluded));
  ok('PER 35 倍被列入「除外」清单',
     au.valuationExcluded.some(s => s.id === 'per-high'),
     JSON.stringify(au.valuationExcluded.map(s=>s.id)));
}

console.log('\n=== C. 等级只由信用指标决定 ===');
{
  // 同一信用状态 + 估值从低到高，等级必须一致
  const grades = [5, 20, 35.18, 90, 200].map(p => {
    const ind = mkInd({ per: { now: p } });
    return MA2.creditVerdict(ind, MA2.runRules(ind, {})).grade;
  });
  ok('PER 5~200 倍区间内等级恒定', new Set(grades).size === 1, grades.join(','));

  // 信用指标变化则等级必须会变（证明等级确实由信用指标驱动）
  const good = MA2.creditVerdict(mkInd(), MA2.runRules(mkInd(), {})).grade;
  const bad = MA2.creditVerdict(
    mkInd({ digest: { days: 6.0, level: 'alert', label: '混雑' },
            quadrant: { key: 'accumulateDown', buyChg: 30, priceChg: -12 } }),
    MA2.runRules(mkInd({ digest: { days: 6.0 }, quadrant: { key:'accumulateDown', buyChg:30, priceChg:-12 } }), {})
  ).grade;
  ok('健康仓位 → good', good === 'good', good);
  ok('消化6日 + 下落中买残增 → bad', bad === 'bad', bad);
  ok('信用指标变化会改变等级', good !== bad);

  // PER 高但信用健康 → 不得判成 bad
  const hiPerHealthy = mkInd({ per: { now: 120 } });
  const g2 = MA2.creditVerdict(hiPerHealthy, MA2.runRules(hiPerHealthy, {})).grade;
  ok('PER 120倍 + 信用健康 → 仍非 bad（估值不得拉低信用评级）', g2 !== 'bad', g2);
}

console.log('\n=== D. creditPosition 三档 ===');
{
  const light = MA2.creditPosition(mkInd({ digest: { days: 0.4 }, borrowRate: { buyListed: 0.8 } }));
  const norm  = MA2.creditPosition(mkInd({ digest: { days: 2.0 }, borrowRate: { buyListed: 5 } }));
  const heavy = MA2.creditPosition(mkInd({ digest: { days: 7.0 }, borrowRate: { buyListed: 2 } }));
  const heavy2= MA2.creditPosition(mkInd({ digest: { days: 0.3 }, borrowRate: { buyListed: 25 } }));

  ok('0.4日 / 0.8% → 軽い', light.label === '軽い', light.label);
  ok('2.0日 / 5%   → 普通', norm.label === '普通', norm.label);
  ok('7.0日 / 2%   → 重い', heavy.label === '重い', heavy.label);
  ok('0.3日 / 25%  → 重い（比值触发）', heavy2.label === '重い', heavy2.label);

  /* ★ A1：同一个「0.3日 / 25%」，但分母不可信 →
     発行済比 只展示不判定，仓位由**可靠的**消化日数决定 → 軽い。
     （若连消化日数都没有，则必须是 判定不能，见 test_credit_validity_gate D 段）*/
  const heavyLow = MA2.creditPosition(mkInd({ digest: { days: 0.3 },
    borrowRate: { buyListed: 25,
      denominator: { confidence: 'low', usableAsHardTrigger: false } } }));
  ok('0.3日 / 25% 但分母不可信 → 軽い（由可靠消化日数决定）',
     heavyLow.label === '軽い', heavyLow.label);
  ok('同上 basis 标注比值未用于判定', /判定に未使用/.test(heavyLow.basis), heavyLow.basis);

  ok('position 只输出文字档位，不含数字分数',
     !/\d+\s*\/\s*100/.test(light.label + norm.label + heavy.label));
  ok('position 带可追溯依据',
     light.basis.includes('消化日数') && light.basis.includes('発行済比'));
}

console.log(`\n${fail === 0 ? '✅' : '❌'}  通过 ${pass} / 失败 ${fail}\n`);
process.exit(fail ? 1 : 0);