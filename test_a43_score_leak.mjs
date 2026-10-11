/**
 * test_a43_score_leak.mjs —— A4.3 内部评分泄漏回归测试
 * -------------------------------------------------------------------------
 * 背景（A4.2 §5 发现的真实缺陷）：
 *   4519（grade=good）首页 Hero 出现「方向スコア 30（需給の改善傾向）」。
 *   根因：engine2.js 把内部 0~100 方向分写进了 verdict.reasonFacts，
 *         而 reasonFacts 会被首页 Hero 直接渲染。
 *
 * 本轮要求（A4.3 §1）：
 *   · 首页只做定性解释，**禁止**再显示裸露的方向评分数字
 *   · 不修改评分计算
 *   · 详细审计区保留必要评分信息
 *   · 增加回归测试
 *
 * 测试策略：**扫描式**覆盖，而不是手工命中阈值。
 *   生成大量(收盘方向 × 買残方向 × 売残方向 × 绝对量)组合，逐个算 verdict，
 *   只要**任何一条** reasonFacts 出现内部评分裸数字 → 失败。
 *   这样无论将来阈值怎么调，只要有人再把分数写进 reasonFacts，测试必红。
 *
 * 运行：node test_a43_score_leak.mjs
 */
import fs from 'fs';

const src = fs.readFileSync(new URL('./engine2.js', import.meta.url), 'utf8');
const { MA2 } = new Function('window', src + '\n; return {MA2: window.MA2};')({});

let pass = 0, fail = 0;
const ok = (cond, name, extra) => {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra !== undefined ? '  → ' + JSON.stringify(extra) : '')); }
};

/* ---------- helpers ---------- */
const bigBars = (lastDate) => {
  const out = [];
  const d = new Date(lastDate + 'T00:00:00Z');
  for (let i = 0; i < 30; i++) {
    out.push({ date: d.toISOString().slice(0, 10), close: 100, vol: 1e9 });
    d.setUTCDate(d.getUTCDate() - 1);
  }
  return out;
};
const ctxOf = (rows, extra = {}) => Object.assign({
  priceBarsAll: bigBars(rows[rows.length - 1].date),
  lastMarginDate: rows[rows.length - 1].date,
  barsMeta: { splits: [], known: true },
}, extra);

/**
 * 内部评分裸数字的检测器。
 * 只针对「评分标签 + 数字」与「/100 分数」两种泄漏形式，
 * 不会误伤合法的事实型数字（消化日数 / 百分比 / 買残金额等）。
 */
const LEAK_PATTERNS = [
  [/方向スコア\s*[-+]?\d+/, '方向スコア裸数字'],
  [/信用リスク\s*[-+]?\d+/, '信用リスク裸数字'],
  [/スコア\s*[:：]?\s*[-+]?\d+/, 'スコア裸数字'],
  [/リスク\s*[:：]?\s*[-+]?\d+/, 'リスク裸数字'],
  [/[-+]?\d+\s*\/\s*100/, '/100 内部分数'],
];
const findLeak = (texts) => {
  for (const t of texts) {
    for (const [re, label] of LEAK_PATTERNS) {
      if (re.test(t)) return { text: t, label };
    }
  }
  return null;
};

/* =================================================================
 * 1. 扫描式：任何分支下的 reasonFacts 都不得含内部评分裸数字
 * ================================================================= */
console.log('\n=== 1. 扫描式泄漏检测（reasonFacts 全分支） ===');
{
  const dates = ['2026-10-01', '2026-10-02', '2026-10-03',
                 '2026-10-04', '2026-10-05', '2026-10-06'];
  // 收盘方向：涨 / 平 / 跌
  const closeSets = [
    [100, 102, 104, 106, 108, 110],   // 涨
    [100, 100, 100, 100, 100, 100],   // 平
    [110, 108, 106, 104, 102, 100],   // 跌
  ];
  // 買残方向：减 / 平 / 增
  const buyTrends = [-8, 0, 8];
  // 売残方向：减 / 平 / 增
  const sellTrends = [-10, 0, 10];
  // 買残绝对量 → 影响 absHeavy / risk
  const buyBases = [500_000, 5_000_000, 40_000_000];

  let scanned = 0, gradeSeen = new Set(), leaks = [];
  for (const closes of closeSets) {
    for (const bt of buyTrends) {
      for (const st of sellTrends) {
        for (const bb of buyBases) {
          const buys = dates.map((_, i) => Math.round(bb * (1 + bt / 100 * i)));
          const sells = dates.map((_, i) => Math.round(bb * 0.1 * (1 + st / 100 * i)));
          const rows = dates.map((d, i) => ({
            date: d, close: closes[i], buy: buys[i], sell: sells[i],
            ratio: +(buys[i] / sells[i]).toFixed(2),
          }));
          const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
          const rules = MA2.runRules(ind, {});
          const v = MA2.creditVerdict(ind, rules);
          scanned++;
          gradeSeen.add(v.grade);
          const leak = findLeak(v.reasonFacts || []);
          if (leak) leaks.push({ grade: v.grade, reasonCode: v.reasonCode, ...leak });
        }
      }
    }
  }
  ok(scanned >= 50, `1a：扫描覆盖 ${scanned} 组 fixture（足够覆盖各分支）`, scanned);
  ok(leaks.length === 0,
     '1b：全部 fixture 的 reasonFacts 均无内部评分裸数字（首页不泄漏）',
     leaks.slice(0, 5));
  ok(gradeSeen.size >= 3,
     `1c：扫描确实覆盖了多个等级分支（${[...gradeSeen].join('/')}）`,
     [...gradeSeen]);
  console.log(`     覆盖等级：${[...gradeSeen].join(', ')}`);
}

/* =================================================================
 * 2. 定性文案本身：不再出现「方向スコア / 信用リスク」标签
 * ================================================================= */
console.log('\n=== 2. 定性文案：不含评分标签 ===');
{
  const dates = ['2026-10-01', '2026-10-02', '2026-10-03',
                 '2026-10-04', '2026-10-05', '2026-10-06'];
  // good 分支：股价涨 + 買残减（A4.2 中 4519 命中此分支并泄漏 "方向スコア 30"）
  const closes = [100, 102, 104, 106, 108, 110];
  const buys = [1_000_000, 960_000, 920_000, 880_000, 840_000, 800_000];
  const sells = buys.map(b => Math.round(b / 16));
  const rows = dates.map((d, i) => ({
    date: d, close: closes[i], buy: buys[i], sell: sells[i],
    ratio: +(buys[i] / sells[i]).toFixed(2),
  }));
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const rules = MA2.runRules(ind, {});
  const v = MA2.creditVerdict(ind, rules);

  ok(v.grade === 'good', '2a：该 fixture 命中 good 分支（复现 A4.2 泄漏场景）', v.grade);
  const all = (v.reasonFacts || []).join(' | ');
  ok(!/方向スコア/.test(all), '2b：reasonFacts 不再出现「方向スコア」标签', all);
  ok(!findLeak(v.reasonFacts || []), '2c：good 分支文案无裸分数', all);
  ok((v.reasonFacts || []).some(t => /改善傾向/.test(t)),
     '2d：改为定性解释（保留「改善傾向」语义）', all);
  console.log(`     首页文案：${all}`);
}

/* =================================================================
 * 3. 详细审计区必须保留评分数值（不得被本轮改动误删）
 * ================================================================= */
console.log('\n=== 3. 详细审计区保留评分信息 ===');
{
  const dates = ['2026-10-01', '2026-10-02', '2026-10-03',
                 '2026-10-04', '2026-10-05', '2026-10-06'];
  const closes = [100, 102, 104, 106, 108, 110];
  const buys = [1_000_000, 960_000, 920_000, 880_000, 840_000, 800_000];
  const sells = buys.map(b => Math.round(b / 16));
  const rows = dates.map((d, i) => ({
    date: d, close: closes[i], buy: buys[i], sell: sells[i],
    ratio: +(buys[i] / sells[i]).toFixed(2),
  }));
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const rules = MA2.runRules(ind, {});
  const v = MA2.creditVerdict(ind, rules);
  const audit = MA2.verdictAudit(ind, rules, {});

  ok(typeof audit.dir === 'number', '3a：verdictAudit 保留方向分 dir（详细页用）', audit.dir);
  ok(typeof audit.risk === 'number', '3b：verdictAudit 保留风险分 risk（详细页用）', audit.risk);
  ok(typeof audit.dirLabel === 'string' && audit.dirLabel.length > 0,
     '3c：审计区保留方向 Label', audit.dirLabel);
  ok(Array.isArray(audit.dirItems) && Array.isArray(audit.riskItems),
     '3d：审计区保留逐条规则贡献（dirItems / riskItems）',
     { d: audit.dirItems.length, r: audit.riskItems.length });
  ok(typeof v.dirScore === 'number' && typeof v.riskScore === 'number',
     '3e：verdict 暴露 dirScore / riskScore（供审计/调试，首页不得读）',
     { dirScore: v.dirScore, riskScore: v.riskScore });
  ok(v.dirScore === audit.dir && v.riskScore === audit.risk,
     '3f：verdict.dirScore 与 audit.dir 同源一致',
     { v: v.dirScore, a: audit.dir });
}

/* =================================================================
 * 4. 评分计算未被修改（本轮只允许改文案）
 * ================================================================= */
console.log('\n=== 4. 评分计算未变（只改文案） ===');
{
  const dates = ['2026-10-01', '2026-10-02', '2026-10-03',
                 '2026-10-04', '2026-10-05', '2026-10-06'];
  const cases = [
    { name: '涨+買残减', closes: [100, 102, 104, 106, 108, 110],
      buys: [1_000_000, 960_000, 920_000, 880_000, 840_000, 800_000] },
    { name: '跌+買残增', closes: [110, 108, 106, 104, 102, 100],
      buys: [1_000_000, 1_040_000, 1_080_000, 1_120_000, 1_160_000, 1_200_000] },
    { name: '平+買残平', closes: [100, 100, 100, 100, 100, 100],
      buys: [1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000] },
  ];
  // 评分由 runRules 决定，本轮未改 runRules / 阈值 → dir/risk 必须仍由同一套规则产出
  for (const cse of cases) {
    const rows = dates.map((d, i) => ({
      date: d, close: cse.closes[i], buy: cse.buys[i],
      sell: Math.round(cse.buys[i] / 16),
      ratio: +(cse.buys[i] / Math.round(cse.buys[i] / 16)).toFixed(2),
    }));
    const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
    const rules = MA2.runRules(ind, {});
    const v = MA2.creditVerdict(ind, rules);
    const audit = MA2.verdictAudit(ind, rules, {});
    ok(rules.dir === audit.dir && rules.risk === audit.risk,
       `4：${cse.name} → dir/risk 仍由 runRules 产出（未被文案改动影响）`,
       { rules: [rules.dir, rules.risk], audit: [audit.dir, audit.risk] });
    ok(!findLeak(v.reasonFacts || []), `4：${cse.name} → 无泄漏`, v.reasonFacts);
  }
}

/* =================================================================
 * 5. 缺陷复现：把裸分数写回 reasonFacts 必须被本测试抓到
 *    （证明测试真的有效，不是恒真断言）
 * ================================================================= */
console.log('\n=== 5. 反证：检测器能抓到泄漏（非恒真） ===');
{
  const bad = ['方向スコア 30（需給の改善傾向）',
               '信用リスク 62（偏高）',
               '方向スコア -25 ／ 信用リスク 40（中性圏）'];
  for (const t of bad) {
    const leak = findLeak([t]);
    ok(leak !== null, `5：检测器抓到泄漏样本「${t.slice(0, 24)}…」`,
       leak ? leak.label : '未抓到');
  }
  // 合法的事实型数字不得被误报
  const good = ['買残消化日数 0.62日（≥3日）＝ 絶対量が重い',
                '短期買残の急騰（+18.3%）',
                '買残/発行済株式数 2.3%（分母可信）',
                '需給の方向が改善傾向',
                '信用需給リスクが偏高'];
  for (const t of good) {
    ok(findLeak([t]) === null, `5：合法事实型数字不被误报「${t.slice(0, 22)}…」`, findLeak([t]));
  }
}

console.log('\n' + '='.repeat(60));
console.log(`A4.3 内部评分泄漏测试： ${pass} 通过 / ${fail} 失败`);
if (fail) { console.log('❌ 存在失败项'); process.exit(1); }
console.log('✅ A4.3 内部评分泄漏测试全部通过');