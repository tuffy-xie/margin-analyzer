/**
 * test_credit_validity_gate.mjs —— 信用需給结论的「数据有效性门槛」回归测试
 * ============================================================================
 * 背景（Codex/Astra review，feature/weekly-margin-trend@f26b6a9）：
 *   1. 缺关键数据（株価 / 出来高 / 発行済株式数）时仍可能输出 🟢改善
 *   2. 「売残极少」被当成 short-squeeze 正向方向加分
 *   3. creditPosition 没有服从 denominator confidence
 *   4. grade / position / reason 在数据不足时语义不一致
 *
 * 本文件覆盖需求 A~H：
 *   A. 缺 price + volume + denominator        → 不得输出 改善
 *   B. quadrant unknown                       → 不得仅靠 shortTiny/short-squeeze 推成改善
 *   C. 売残极少                                → 只能解释高倍率，不得单独增加 directionScore
 *   D. denominator confidence = low           → 不得让 creditPosition 变成 重い/軽い
 *                                              （除非还有其他可靠指标足够判断）
 *   E. denominator confidence = high          → 原有 ratio 参与仓位判定的行为保持
 *   F. grade / position / reason              → 数据不足时语义一致（不得互相矛盾）
 *   G. 正常完整 fixture                        → 原有正常判定不发生意外大范围变化
 *   H. PER/PBR/EPS/BPS 仍不影响信用需給        → 由 test_valuation_decoupling.mjs 覆盖
 *
 * 运行：node test_credit_validity_gate.mjs
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

const DAYS = ['2026-09-22','2026-09-23','2026-09-24','2026-09-25',
              '2026-09-28','2026-09-29','2026-09-30','2026-10-01'];
const LAST = '2026-10-01';

/** 造 rows；close 传 null 表示「缺价格」 */
function mkRows({ buy, sell, close = null, buyListed = null, sellListed = null }) {
  return DAYS.map((d, i) => {
    const r = { date: d, buy: buy[i], sell: sell[i],
                ratio: +(buy[i] / sell[i]).toFixed(2) };
    if (close != null) r.close = close[i];
    if (buyListed != null) r.buyListed = buyListed;
    if (sellListed != null) r.sellListed = sellListed;
    return r;
  });
}

/** 造完整日线（供 買残消化日数 使用）；vol=0 / n=0 表示「缺成交量」 */
function mkBars(vol, n = 30) {
  if (!vol) return [];
  const out = [];
  for (let i = 0; i < n; i++) {
    const dt = new Date(Date.UTC(2026, 7, 1 + i));   // 2026-08-01 起
    out.push({ date: dt.toISOString().slice(0, 10), close: 1000, adjClose: 1000, vol });
  }
  return out;
}

/** 走完整链路：computeIndicators → runRules → creditVerdict */
function run(rows, val, barsAll) {
  const ind = MA2.computeIndicators(rows, val, { priceBarsAll: barsAll, lastMarginDate: LAST });
  const vr  = MA2.runRules(ind, val);
  const cv  = MA2.creditVerdict(ind, vr);
  return { ind, vr, cv };
}
const flat = (v, n = 8) => Array(n).fill(v);

/* =========================================================================
 * 案例 A：缺 price + volume + denominator
 *   買残持平 1,000,000；売残 100,000 → 10,000
 *   price / volume / shares 全部缺失
 * ======================================================================= */
console.log('\n=== A. 缺 price + volume + denominator：不得输出 改善 ===');
{
  const rows = mkRows({ buy: flat(1_000_000),
                        sell: [100000, 90000, 70000, 50000, 40000, 30000, 20000, 10000] });
  const { ind, vr, cv } = run(rows, { shares: null, price: null }, []);   // 无 bars → 无成交量

  ok('A1 四象限确为 判定不能', ind.quadrant.key === 'unknown', ind.quadrant.key);
  ok('A2 消化日数为 null（缺成交量）', ind.digest.days === null, ind.digest.days);
  ok('A3 分母 confidence = na', ind.borrowRate.denominator.confidence === 'na',
     ind.borrowRate.denominator.confidence);
  ok('A4 分母 usableAsHardTrigger = false', ind.borrowRate.denominator.usableAsHardTrigger === false);
  ok('A5 grade 不得为 good（改善）', cv.grade !== 'good', { grade: cv.grade, badge: cv.badge });
  ok('A6 grade = unknown（判定不能）', cv.grade === 'unknown', cv.grade);
  ok('A7 reasonCode = insufficientData', cv.reasonCode === 'insufficientData', cv.reasonCode);
  ok('A8 badge 含「判定不能」', /判定不能/.test(cv.badge), cv.badge);
  ok('A9 reasonFacts 含「判定できません」',
     JSON.stringify(cv.reasonFacts).includes('判定できません'), cv.reasonFacts);
  ok('A10 reasonFacts 不含「改善」', !JSON.stringify(cv.reasonFacts).includes('改善'), cv.reasonFacts);
  ok('A11 directionScore 不得为正的肯定依据（dir=0）', vr.dir === 0, vr.dir);
  ok('A12 riskScore = 0（无 hard 风险依据）', vr.risk === 0, vr.risk);
  ok('A13 position = 判定不能', cv.position.key === 'unknown', cv.position);
}

/* =========================================================================
 * 案例 B：quadrant unknown 时，不得仅靠 shortTiny / short-squeeze 推成改善
 * ======================================================================= */
console.log('\n=== B. quadrant unknown：不得仅靠 shortTiny/short-squeeze 推成改善 ===');
{
  const rows = mkRows({ buy: flat(1_000_000),
                        sell: [100000, 90000, 70000, 50000, 40000, 30000, 20000, 10000] });
  const { ind, vr, cv } = run(rows, { shares: null, price: null }, []);

  ok('B1 ratioDecomp.cause 确为 shortTiny', ind.ratioDecomp.cause === 'shortTiny', ind.ratioDecomp.cause);
  ok('B2 ratio ≥ 8（旧 short-squeeze 的触发条件成立）', ind.ratio.value >= 8, ind.ratio.value);
  ok('B3 short-squeeze 不再是 dirHits（不计分）',
     !vr.dirHits.some(h => h.id === 'short-squeeze'), vr.dirHits.map(h => h.id));
  ok('B4 short-squeeze 降级为 dirNotes（保留事实说明）',
     vr.dirNotes.some(h => h.id === 'short-squeeze'), vr.dirNotes.map(h => h.id));
  ok('B5 dir = 0（未因売残极少获得正向加分）', vr.dir === 0, vr.dir);
  ok('B6 grade 不是 good', cv.grade !== 'good', cv.grade);
  ok('B7 grade = unknown', cv.grade === 'unknown', cv.grade);
  ok('B8 首页徽章不得为 🟢改善', !/🟢/.test(cv.badge), cv.badge);
}

/* =========================================================================
 * 案例 C：売残极少 —— 可解释高倍率，但不得单独增加 directionScore
 *   两个 fixture 的 株価 / 買残 路径完全相同，只有売残的绝对水平不同
 *   → 一个 ratio<8（不触发），一个 ratio≥8 且 cause=shortTiny（触发）
 *   → 两者 dir 必须完全一致
 * ======================================================================= */
console.log('\n=== C. 売残极少：可解释高倍率，但不得单独增加 directionScore ===');
{
  const CLOSE = [100, 102, 104, 106, 108, 110, 112, 114];      // 股价上行
  const BUY   = flat(500_000);                                  // 買残持平
  const bars  = mkBars(1_000_000);

  // ratio 低：売残 1,000,000 → 100,000，倍率 0.5 → 5.0（<8，不触发）
  const lo = run(mkRows({ buy: BUY, sell: [1_000_000, 900_000, 800_000, 700_000,
                                           600_000, 500_000, 300_000, 100_000], close: CLOSE }),
                 { shares: 200_000_000, price: 114 }, bars);
  // ratio 高：売残 100,000 → 10,000，倍率 5 → 50（≥8 且 cause=shortTiny，触发 soft）
  const hi = run(mkRows({ buy: BUY, sell: [100_000, 90_000, 70_000, 50_000,
                                           40_000, 30_000, 20_000, 10_000], close: CLOSE }),
                 { shares: 200_000_000, price: 114 }, bars);

  ok('C1 低倍率 fixture 未触发 short-squeeze',
     !lo.vr.dirHits.some(h => h.id === 'short-squeeze') &&
     !lo.vr.dirNotes.some(h => h.id === 'short-squeeze'));
  ok('C2 高倍率 fixture 的 cause = shortTiny', hi.ind.ratioDecomp.cause === 'shortTiny',
     hi.ind.ratioDecomp.cause);
  ok('C3 高倍率 fixture 触发 short-squeeze（作为 soft 说明）',
     hi.vr.dirNotes.some(h => h.id === 'short-squeeze'), hi.vr.dirNotes.map(h => h.id));
  ok('C4 short-squeeze 未进入计分的 dirHits',
     !hi.vr.dirHits.some(h => h.id === 'short-squeeze'), hi.vr.dirHits.map(h => h.id));
  ok('C5 両 fixture 的 directionScore 完全一致', lo.vr.dir === hi.vr.dir,
     { lo: lo.vr.dir, hi: hi.vr.dir });
  ok('C6 両 fixture 的 riskScore 完全一致', lo.vr.risk === hi.vr.risk,
     { lo: lo.vr.risk, hi: hi.vr.risk });
  ok('C7 両 fixture 的 grade 完全一致', lo.cv.grade === hi.cv.grade,
     { lo: lo.cv.grade, hi: hi.cv.grade });
  ok('C8 売残极少仍保留为倍率的解释（causeText 可见）',
     /売残/.test(hi.ind.ratioDecomp.causeText), hi.ind.ratioDecomp.causeText);
}

/* =========================================================================
 * 案例 D：denominator confidence = low
 *   · 有可靠替代指标（消化日数 0.20 日）→ 由消化日数决定，不得因低可信分母判 重い
 *   · 无可靠替代指标（缺成交量）        → 判定不能，不得用低可信估算值凑出 重い/軽い
 * ======================================================================= */
console.log('\n=== D. denominator confidence = low：不得让 creditPosition 变成 重い/軽い ===');
{
  const CLOSE = [1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007];
  const BUY   = flat(500_000);
  const SELL  = flat(50_000);
  // shares=5,000,000；JPX 上場比 2.5% → 反推 20,000,000 → 乖离 +300% → confidence='low'
  const rows  = mkRows({ buy: BUY, sell: SELL, close: CLOSE, buyListed: 2.5, sellListed: 0.25 });
  const val   = { shares: 5_000_000, price: 1007 };

  // D-1：有成交量 → 消化日数 0.20 日（很轻）
  const withVol = run(rows, val, mkBars(2_500_000));
  ok('D1 confidence = low', withVol.ind.borrowRate.denominator.confidence === 'low',
     withVol.ind.borrowRate.denominator.confidence);
  ok('D2 usableAsHardTrigger = false', withVol.ind.borrowRate.denominator.usableAsHardTrigger === false);
  ok('D3 buyListed 被低可信分母抬到 10%', withVol.ind.borrowRate.buyListed === 10,
     withVol.ind.borrowRate.buyListed);
  ok('D4 消化日数 = 0.20（可靠且很轻）', withVol.ind.digest.days === 0.2, withVol.ind.digest.days);
  ok('D5 position 不得为 重い', withVol.cv.position.key !== 'heavy', withVol.cv.position);
  ok('D6 position = 軽い（由可靠的消化日数决定）', withVol.cv.position.key === 'light',
     withVol.cv.position);
  ok('D7 basis 如实标注分母估算值未用于判定',
     /判定に未使用/.test(withVol.cv.position.basis), withVol.cv.position.basis);
  ok('D8 risk 未因低可信分母加分', withVol.vr.risk === 0, withVol.vr.risk);
  ok('D9 riskNotes 中保留低可信提示（仅提示）',
     withVol.vr.riskNotes.some(h => h.id === 'listed-high-untrusted'),
     withVol.vr.riskNotes.map(h => h.id));

  // D-2：缺成交量 → 没有任何可靠仓位尺度
  const noVol = run(rows, val, []);
  ok('D10 缺成交量时消化日数 = null', noVol.ind.digest.days === null, noVol.ind.digest.days);
  ok('D11 position 不得为 重い / 軽い',
     noVol.cv.position.key !== 'heavy' && noVol.cv.position.key !== 'light', noVol.cv.position);
  ok('D12 position = 判定不能', noVol.cv.position.key === 'unknown', noVol.cv.position);
  ok('D13 basis 说明未使用低可信分母',
     /分母可信度不足|いずれも算出できず/.test(noVol.cv.position.basis), noVol.cv.position.basis);

  // D-3：低可信分母 + 缺成交量，即便估算值极高也不得凑出 重い
  const huge = run(mkRows({ buy: flat(5_000_000), sell: SELL, close: CLOSE,
                            buyListed: 2.5, sellListed: 0.25 }), val, []);
  ok('D14 极高的低可信估算值也不得判 重い', huge.cv.position.key !== 'heavy',
     { buyListed: huge.ind.borrowRate.buyListed, pos: huge.cv.position });
  ok('D15 同上 → 判定不能', huge.cv.position.key === 'unknown', huge.cv.position);
}

/* =========================================================================
 * 案例 E：denominator confidence = high —— 原有 ratio 行为保持
 *   JPX 上場比与発行済株式数一致（乖离 0%）→ high
 *   缺成交量时，発行済比 必须仍能决定仓位（这是原有正确行为）
 * ======================================================================= */
console.log('\n=== E. denominator confidence = high：原有 ratio 行为保持 ===');
{
  const CLOSE = [1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007];
  // shares=5,000,000；JPX 上場比 10% → 反推 5,000,000 → 乖离 0% → confidence='high'
  const rows = mkRows({ buy: flat(500_000), sell: flat(50_000), close: CLOSE,
                        buyListed: 10, sellListed: 1 });
  const val  = { shares: 5_000_000, price: 1007 };
  const r    = run(rows, val, []);    // 缺成交量 → 只能靠発行済比

  ok('E1 confidence = high', r.ind.borrowRate.denominator.confidence === 'high',
     r.ind.borrowRate.denominator.confidence);
  ok('E2 usableAsHardTrigger = true', r.ind.borrowRate.denominator.usableAsHardTrigger === true);
  ok('E3 buyListed = 10%', r.ind.borrowRate.buyListed === 10, r.ind.borrowRate.buyListed);
  ok('E4 高可信分母仍能决定 重い（原有行为保持）', r.cv.position.key === 'heavy', r.cv.position);
  ok('E5 basis 正常含発行済比', /発行済比 10\.00%/.test(r.cv.position.basis), r.cv.position.basis);
  ok('E6 basis 不含「判定に未使用」（未被排除）',
     !/判定に未使用/.test(r.cv.position.basis), r.cv.position.basis);
  ok('E7 高可信时 listed-high 仍计入风险分', r.vr.riskHits.some(h => h.id === 'listed-high'),
     r.vr.riskHits.map(h => h.id));

  // E-8：high 且比例低 → 軽い（原有行为保持）
  const light = run(mkRows({ buy: flat(50_000), sell: flat(50_000), close: CLOSE,
                             buyListed: 1, sellListed: 1 }), val, []);
  ok('E8 高可信 + 低比值 → 軽い', light.cv.position.key === 'light', light.cv.position);
}

/* =========================================================================
 * 案例 F：grade / position / reason 在数据不足时语义一致
 *   · 不得出现「unknown 的 position + 🟢改善 的 grade」这类矛盾组合
 *   · 但已确认的**风险**不得因方向判不出来而被抹掉（不得 unknown=安全）
 * ======================================================================= */
console.log('\n=== F. 数据不足时 grade / position / reason 语义一致 ===');
{
  // F-1：全缺 → 判定不能，三者一致
  const none = run(mkRows({ buy: flat(1_000_000), sell: [100000, 90000, 70000, 50000,
                                                         40000, 30000, 20000, 10000] }),
                   { shares: null, price: null }, []);
  const nf = JSON.stringify(none.cv.reasonFacts);
  ok('F1 grade=unknown 且 position=unknown（一致）',
     none.cv.grade === 'unknown' && none.cv.position.key === 'unknown',
     { grade: none.cv.grade, pos: none.cv.position.key });
  ok('F2 badge / reasonFacts / position 三者都不出现「改善」',
     !/改善/.test(none.cv.badge) && !/改善/.test(nf) && !/改善/.test(none.cv.position.basis),
     { badge: none.cv.badge, facts: none.cv.reasonFacts });
  ok('F3 badge 使用既有档位「判定不能」', /判定不能/.test(none.cv.badge), none.cv.badge);
  ok('F4 suffix =（データ不足）', none.cv.suffix === '（データ不足）', none.cv.suffix);

  // F-5：缺价格但消化日数很重 → 风险结论不得被隐藏（不是「unknown=安全」）
  const heavy = run(mkRows({ buy: flat(1_000_000), sell: flat(200_000) }),
                    { shares: null, price: null }, mkBars(100_000));  // 1000万/10万 = 10日
  ok('F5 缺价格但消化日数重 → 仍输出 悪化（不因方向未知而隐藏风险）',
     heavy.cv.grade === 'bad', { grade: heavy.cv.grade, days: heavy.ind.digest.days });
  ok('F6 同上 reasonCode = absHeavy', heavy.cv.reasonCode === 'absHeavy', heavy.cv.reasonCode);
  ok('F7 同上 position = 重い（消化日数是可靠指标）',
     heavy.cv.position.key === 'heavy', heavy.cv.position);

  // F-8：缺价格但買残激增 + 消化日数中等 → 至少 注意，不得判定不能/改善
  const surge = run(mkRows({ buy: [100_000, 120_000, 150_000, 190_000,
                                   240_000, 300_000, 380_000, 480_000],
                             sell: flat(50_000) }),
                    { shares: null, price: null }, mkBars(300_000));
  ok('F8 缺价格但買残激增 → 不得输出 改善', surge.cv.grade !== 'good', surge.cv.grade);
  ok('F9 同上：方向判不出来 → 判定不能（不得编造方向结论）',
     surge.cv.grade === 'unknown', { grade: surge.cv.grade, risk: surge.vr.risk });
  ok('F10 同上：已确认的「買残急騰」事实不得被判定不能的文案吞掉',
     JSON.stringify(surge.cv.reasonFacts).includes('買残激增'),
     surge.cv.reasonFacts);
  ok('F11 同上：仍保留 insufficientData 的主结论',
     surge.cv.reasonCode === 'insufficientData', surge.cv.reasonCode);
}

/* =========================================================================
 * 案例 G：正常完整 fixture —— 原有正常判定不发生意外大范围变化
 * ======================================================================= */
console.log('\n=== G. 正常完整 fixture：原有正常判定保持 ===');
{
  const bars = mkBars(2_000_000);
  const val  = { shares: 200_000_000, price: 114 };

  // G-1：健康形态（股价上行 + 買残回落 + 仓位轻）→ 改善
  const healthy = run(mkRows({ buy: [900_000, 880_000, 850_000, 820_000,
                                     790_000, 760_000, 730_000, 700_000],
                               sell: flat(200_000),
                               close: [100, 102, 104, 106, 108, 110, 112, 114] }),
                      val, bars);
  ok('G1 健康形态仍为 good（改善）', healthy.cv.grade === 'good',
     { grade: healthy.cv.grade, reason: healthy.cv.reasonCode, dir: healthy.vr.dir });
  ok('G2 reasonCode = improving', healthy.cv.reasonCode === 'improving', healthy.cv.reasonCode);
  ok('G3 q-strong 仍在计分 dirHits 中',
     healthy.vr.dirHits.some(h => h.id === 'q-strong'), healthy.vr.dirHits.map(h => h.id));
  ok('G4 position = 軽い', healthy.cv.position.key === 'light', healthy.cv.position);

  // G-2：下落中買残増加 → 悪化/注意，绝不是改善
  const down = run(mkRows({ buy: [700_000, 740_000, 780_000, 830_000,
                                   880_000, 930_000, 990_000, 1_050_000],
                            sell: flat(200_000),
                            close: [114, 112, 110, 108, 106, 104, 102, 100] }),
                   val, bars);
  ok('G5 下落中買残増加 不得为 good', down.cv.grade !== 'good', down.cv.grade);
  ok('G6 q-accum 仍在计分（-30）', down.vr.dirHits.some(h => h.id === 'q-accum'),
     down.vr.dirHits.map(h => h.id));
  ok('G7 down-accum 仍在风险计分中', down.vr.riskHits.some(h => h.id === 'down-accum'),
     down.vr.riskHits.map(h => h.id));

  // G-3：横盘 → 中立（不因门槛改动而变成判定不能）
  const flatFx = run(mkRows({ buy: flat(500_000), sell: flat(200_000), close: flat(1000) }),
                     val, bars);
  ok('G8 正常横盘不得变成 判定不能', flatFx.cv.grade !== 'unknown',
     { grade: flatFx.cv.grade, quad: flatFx.ind.quadrant.key });
  ok('G9 正常横盘 position 有结论', flatFx.cv.position.key !== 'unknown', flatFx.cv.position);
}

/* =========================================================================
 * H 由 test_valuation_decoupling.mjs 覆盖，这里补一条交叉确认：
 *   门槛改动后，估值仍不得影响信用需給
 * ======================================================================= */
console.log('\n=== H. 交叉确认：估值仍不影响信用需給 ===');
{
  const bars = mkBars(2_000_000);
  const mk = (extra) => Object.assign({ shares: 200_000_000, price: 114 }, extra);
  const rows = mkRows({ buy: [900_000, 880_000, 850_000, 820_000, 790_000, 760_000, 730_000, 700_000],
                        sell: flat(200_000),
                        close: [100, 102, 104, 106, 108, 110, 112, 114] });
  const a = run(rows, mk({ perResult: 10, pbrResult: 1 }), bars);
  const b = run(rows, mk({ perResult: 50, pbrResult: 8 }), bars);
  ok('H1 dir 不受估值影响', a.vr.dir === b.vr.dir, { a: a.vr.dir, b: b.vr.dir });
  ok('H2 risk 不受估值影响', a.vr.risk === b.vr.risk, { a: a.vr.risk, b: b.vr.risk });
  ok('H3 grade 不受估值影响', a.cv.grade === b.cv.grade, { a: a.cv.grade, b: b.cv.grade });
  ok('H4 position 不受估值影响', a.cv.position.key === b.cv.position.key);
  ok('H5 DIR_RULES 中不存在 per / pbr 相关规则',
     !MA2.DIR_RULES.some(r => /per|pbr|div/i.test(r.id)),
     MA2.DIR_RULES.map(r => r.id));
  ok('H6 RISK_RULES 中不存在 per / pbr 相关规则',
     !MA2.RISK_RULES.some(r => /per|pbr|div/i.test(r.id)),
     MA2.RISK_RULES.map(r => r.id));
}

console.log('\n' + '='.repeat(60));
console.log(`結果: ${pass} passed, ${fail} failed`);
console.log('='.repeat(60));
process.exit(fail ? 1 : 0);
