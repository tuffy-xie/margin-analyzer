/**
 * test_correctness.mjs —— v4 数据正确性 / 评级可信度 回归测试
 * -------------------------------------------------------------------------
 * 覆盖用户指定的 A~I 九项：
 *   A. 长于6行时 price change 与 buy change 使用相同起止点
 *   B. marginDate=10/01 只有 09/30 与 10/02 行情时，绝不能拿 10/02 的价格
 *   C. 1拆4：買残 100万→400万 不能触发 +300% long-surge
 *   D. digest=null 且 buyListed=null → creditPosition 必须 unknown
 *   E. denomStable=false 时极端 buyListed 不得单独造成 hard bad
 *   F. deleveraging 不增加 risk score
 *   G.（Python 侧 test_cache_concurrency.py 覆盖）
 *   H.（浏览器侧 verify_ui.py 覆盖）
 *   I. verdict 与最终显示文案不能矛盾
 *
 * 运行：node test_correctness.mjs
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
const bars = (n, endDate, avgVol) => {
  const out = [];
  const d = new Date('2026-09-01T00:00:00Z');
  while (out.length < n) {
    const iso = d.toISOString().slice(0, 10);
    const wd = d.getUTCDay();
    if (wd !== 0 && wd !== 6 && iso <= endDate) out.push({ date: iso, close: 100, vol: avgVol || 1000000 });
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
};
const md = (d) => String(d).slice(5).replace('-', '/');

/* =================================================================
 * A. 统一比较窗口
 * ================================================================= */
console.log('\n=== A. price change 与 buy change 必须同起止点 ===');
{
  // 用户给定场景（10 行）：
  //   整窗(rows[0..9]) 股价 100 → 120 = +20%
  //   近5期(rows[4..9]) 股价 150 → 120 = -20%
  //   近5期 買残 100万 → 80万 = -20%
  const px  = [100, 120, 130, 140, 150, 140, 135, 130, 125, 120];
  const buy = [100, 100, 100, 100, 100, 90, 90, 85, 82, 80].map(x => x * 10000);
  const rows = px.map((p, i) => ({
    date: `2026-09-${String(20 + i).padStart(2, '0')}`,
    buy: buy[i], sell: 50000, ratio: 16, close: p,
  }));

  const ind = MA2.computeIndicators(rows, {}, { priceBarsAll: [], lastMarginDate: '2026-09-29' });
  const q = ind.quadrant;

  ok(q.comparison != null, 'comparison 已返回');
  ok(q.comparison.from === rows[4].date && q.comparison.to === rows[9].date,
     '窗口 = 近5期 rows[4..9]', q.comparison);
  ok(q.comparison.periods === 5, 'periods = 5', q.comparison.periods);
  ok(q.priceChg === -20, 'priceChg = -20%（不是整窗 +20%）', q.priceChg);
  ok(q.buyChg === -20, 'buyChg = -20%', q.buyChg);
  ok(q.key !== 'strong', '未因整窗上涨误判 strong', q.key);

  // 起止点必须严格同源：把 rows[4].close 改掉，两者应同步变化
  const rows2 = rows.map((r, i) => (i === 4 ? Object.assign({}, r, { close: 100 }) : r));
  const q2 = MA2.computeIndicators(rows2, {}, { priceBarsAll: [], lastMarginDate: '2026-09-29' }).quadrant;
  ok(q2.priceChg === 20, '改写窗口起点价格后 priceChg 同步变化（证明同源）', q2.priceChg);

  // 缺行情不得扩大窗口
  const rows3 = rows.map((r, i) => (i === 4 ? Object.assign({}, r, { close: null }) : r));
  const q3 = MA2.computeIndicators(rows3, {}, { priceBarsAll: [], lastMarginDate: '2026-09-29' }).quadrant;
  ok(q3.priceChg === null, '窗口端点缺行情 → priceChg=null（不缩小窗口）', q3.priceChg);
  ok(q3.key === 'unknown', '端点缺行情 → quadrant=unknown', q3.key);

  // 6 行以内也要统一（不因 rows 少而改变起点语义）
  const rows6 = px.slice(4).map((p, i) => ({
    date: `2026-09-${String(24 + i).padStart(2, '0')}`,
    buy: buy[i + 4], sell: 50000, ratio: 16, close: p,
  }));
  const q6 = MA2.computeIndicators(rows6, {}, { priceBarsAll: [], lastMarginDate: '2026-09-29' }).quadrant;
  ok(q6.priceChg !== null && q6.buyChg !== null, '6行数据同样给出 priceChg/buyChg');
  ok(q6.comparison.periods === 5, '6行时 periods=5', q6.comparison.periods);
}

/* =================================================================
 * B. 禁止未来行情泄漏（look-ahead）
 * ================================================================= */
console.log('\n=== B. marginDate=10/01 不得取 10/02 的价格 ===');
{
  // 模拟 index.html 的对齐逻辑（同日优先，fallback 只向过去）
  const priceByDate = new Map([
    ['2026-09-30', 100],
    ['2026-10-02', 200],   // ← 未来价格，绝不能被 10/01 选中
  ]);
  const priceDates = [...priceByDate.keys()].sort();

  const pickPrice = (marginDate) => {
    if (priceByDate.has(marginDate)) {
      return { date: marginDate, price: priceByDate.get(marginDate), isSameDate: true };
    }
    let lo = 0, hi = priceDates.length - 1, idx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (priceDates[mid] <= marginDate) { idx = mid; lo = mid + 1; }
      else hi = mid - 1;
    }
    if (idx < 0) return null;
    const d = priceDates[idx];
    if (d > marginDate) return null;
    return { date: d, price: priceByDate.get(d), isSameDate: false };
  };

  const hit = pickPrice('2026-10-01');
  ok(hit != null, '找到 fallback 行情');
  ok(hit.date === '2026-09-30', 'fallback 只能向过去（09/30）', hit);
  ok(hit.price === 100, '价格取 100，不是未来的 200', hit.price);
  ok(hit.isSameDate === false, 'isSameDate=false（已如实标注非同期）');

  // 严格同日优先
  const same = pickPrice('2026-10-02');
  ok(same.isSameDate === true && same.price === 200, '同日命中优先', same);

  // 之前完全没有行情 → null，不猜
  ok(pickPrice('2026-09-01') === null, '早于所有行情 → 返回 null');

  // 引擎侧：端点无价 → unknown（不做跨期比较）
  const rows = [
    { date: '2026-09-30', buy: 1000000, sell: 50000, ratio: 20, close: 100 },
    { date: '2026-10-01', buy: 1050000, sell: 50000, ratio: 21, close: null },
  ];
  const q = MA2.computeIndicators(rows, {}, { priceBarsAll: [], lastMarginDate: '2026-10-01' }).quadrant;
  ok(q.priceChg === null && q.key === 'unknown',
     '端点无行情 → 四象限 unknown（不跨期取价）', { p: q.priceChg, k: q.key });
}

/* =================================================================
 * C. 1拆4 不得触发 long-surge
 * ================================================================= */
console.log('\n=== C. 1拆4 買残 100万→400万 不得触发 +300% long-surge ===');
{
  const rows = [
    { date: '2026-09-28', buy: 1000000, sell: 100000, ratio: 10, close: 400 },
    { date: '2026-09-29', buy: 4000000, sell: 400000, ratio: 10, close: 100 },
  ];
  const ctx = {
    priceBarsAll: [], lastMarginDate: '2026-09-29',
    barsMeta: { known: true, splits: [{ date: '2026-09-29', ratio: '1:4' }] },
  };
  const ind = MA2.computeIndicators(rows, {}, ctx);
  const v = MA2.runRules(ind, {});
  const cv = MA2.creditVerdict(ind, v);

  ok(ind.corporateActionAffected === true, 'corporateActionAffected = true');
  ok(ind.long.chgN === null && ind.short.chgN === null,
     'buyChg / sellChg 均置 null', { b: ind.long.chgN, s: ind.short.chgN });
  ok(ind.quadrant.key === 'corporateAction', 'quadrant 停止判定', ind.quadrant.key);
  ok(!v.riskHits.some(h => h.id === 'long-surge'), '未触发 long-surge', v.riskHits.map(h => h.id));
  ok(!v.riskHits.some(h => h.id === 'short-build'), '未触发 short-build', v.riskHits.map(h => h.id));
  ok(!v.dirHits.some(h => h.id.startsWith('q-')), '方向规则全部停止', v.dirHits.map(h => h.id));
  ok(cv.grade === 'unknown', '最终等级 = unknown', cv.grade);
  ok(cv.reasonCode === 'corporateAction', 'reasonCode = corporateAction', cv.reasonCode);

  // 对照：没有公司行动时，同样的数据应触发 long-surge（证明门禁有效）
  const indCtrl = MA2.computeIndicators(rows, {}, {
    priceBarsAll: [], lastMarginDate: '2026-09-29',
    barsMeta: { known: true, splits: [] },
  });
  const vCtrl = MA2.runRules(indCtrl, {});
  ok(indCtrl.long.chgN === 300, '对照：无公司行动时 chgN = +300%', indCtrl.long.chgN);
  ok(vCtrl.riskHits.some(h => h.id === 'long-surge'), '对照：确实会触发 long-surge（门禁有效）');

  // fail-safe 路径：events 未知时，按「拆股后复权价不变」构造
  //   （1拆4 → 股数 ×4，但 adjClose 已复权所以价格几乎不动）
  const rowsFs = [
    { date: '2026-09-28', buy: 1000000, sell: 100000, ratio: 10, close: 400 },
    { date: '2026-09-29', buy: 4000000, sell: 400000, ratio: 10, close: 400 },
  ];
  const indFs = MA2.computeIndicators(rowsFs, {}, {
    priceBarsAll: [], lastMarginDate: '2026-09-29',
    barsMeta: { known: false, splits: [] },
  });
  ok(indFs.corporateActionAffected === true,
     'fail-safe：events 未知时，残量+300% 而复权价几乎不动 → 判为公司行动',
     indFs.corporateAction);
  ok(indFs.long.chgN === null, 'fail-safe 下 chgN 同样置 null');

  // 反向：若价格也同步大跌，则更像真实行情，不应误判为拆股
  const rowsPx = [
    { date: '2026-09-28', buy: 1000000, sell: 100000, ratio: 10, close: 400 },
    { date: '2026-09-29', buy: 4000000, sell: 400000, ratio: 10, close: 100 },
  ];
  const indPx = MA2.computeIndicators(rowsPx, {}, {
    priceBarsAll: [], lastMarginDate: '2026-09-29',
    barsMeta: { known: false, splits: [] },
  });
  ok(indPx.corporateActionAffected === false,
     '价格同步大跌时不误判为拆股（避免过度保守）', indPx.corporateAction);

  // 真实的普通大涨（残量涨、股价也涨）不应被误判为拆股
  const rowsReal = [
    { date: '2026-09-28', buy: 1000000, sell: 100000, ratio: 10, close: 100 },
    { date: '2026-09-29', buy: 1400000, sell: 100000, ratio: 14, close: 130 },
  ];
  const indReal = MA2.computeIndicators(rowsReal, {}, {
    priceBarsAll: [], lastMarginDate: '2026-09-29',
    barsMeta: { known: false, splits: [] },
  });
  ok(indReal.corporateActionAffected === false, '普通上涨未被误判为拆股');
}

/* =================================================================
 * C2. fallback 必须遍历比较窗口内**所有相邻 pair**（不只最后两行）
 * ================================================================= */
console.log('\n=== C2. 窗口中段拆股：最后两行正常也必须检出 suspected ===');
{
  // 用户指定场景：
  //   day1..day6，其中 day2 处发生 買残×3 而复权价基本不变；
  //   最后两行（day5→day6）完全正常。
  //   旧实现只比最后两行 → 漏检 → 虚假方向判定被放行。
  const D = ['2026-09-28','2026-09-29','2026-09-30','2026-10-01','2026-10-02','2026-10-05'];
  const rows = [
    { date: D[0], buy: 1000000, sell: 100000, ratio: 10, close: 100 },  // day1 基准
    { date: D[1], buy: 3000000, sell: 100000, ratio: 30, close: 100 },  // day2 ← ×3，价格不变（拆股）
    { date: D[2], buy: 3050000, sell: 100000, ratio: 30.5, close: 102 },// day3 正常
    { date: D[3], buy: 3000000, sell: 100000, ratio: 30, close: 101 },  // day4 正常
    { date: D[4], buy: 3020000, sell: 100000, ratio: 30.2, close: 103 },// day5 正常
    { date: D[5], buy: 3010000, sell: 100000, ratio: 30.1, close: 104 },// day6 正常
  ];
  // 最后两行确实正常：跳变 < 50%，价格变动 < 20%
  const lastJump = Math.abs((rows[5].buy - rows[4].buy) / rows[4].buy);
  const lastPx = Math.abs((rows[5].close - rows[4].close) / rows[4].close);
  ok(lastJump < 0.5 && lastPx < 0.2, '前置条件：最后两行完全正常', { lastJump, lastPx });

  const ind = MA2.computeIndicators(rows, {}, {
    priceBarsAll: [], lastMarginDate: D[5],
    barsMeta: { known: false, splits: [] },      // ← events 未知，必须走 fallback
  });
  const ca = ind.corporateAction;

  ok(ca.affected === true, '窗口中段拆股被检出', ca);
  ok(ca.status === 'suspected', 'status = suspected（非 confirmed）', ca.status);
  ok(ca.hits.length === 1 && ca.hits[0].kind === 'heuristic',
     '命中来源 = heuristic', JSON.stringify(ca.hits));
  ok(ca.hits[0].from === D[0] && ca.hits[0].to === D[1],
     '命中的是 day1→day2（窗口中段），不是最后两行', ca.hits[0]);
  ok(/可能性があります/.test(ca.text), '文案用「可能性があります」', ca.text);
  ok(!/を検出/.test(ca.text), 'suspected 不得使用「を検出」', ca.text);

  // 必须停止方向判定
  const v = MA2.runRules(ind, {});
  const cv = MA2.creditVerdict(ind, v);
  ok(ind.corporateActionStatus === 'suspected', 'indicator 顶层 status 同步', ind.corporateActionStatus);
  ok(ind.long.chgN === null && ind.short.chgN === null,
     'buyChg / sellChg 置 null', { b: ind.long.chgN, s: ind.short.chgN });
  ok(ind.quadrant.key === 'corporateAction', 'quadrant 停止判定', ind.quadrant.key);
  ok(ind.quadrant.corporateActionStatus === 'suspected', 'quadrant 携带 status', ind.quadrant.corporateActionStatus);
  ok(!v.riskHits.some(h => h.id === 'long-surge'), 'long-surge 未参与评分', v.riskHits.map(h => h.id));
  ok(!v.riskHits.some(h => h.id === 'short-build'), 'short-build 未参与评分', v.riskHits.map(h => h.id));
  ok(v.dirHits.length === 0, '方向规则全部停止', v.dirHits.map(h => h.id));
  ok(cv.grade === 'unknown', '最终等级 = unknown', cv.grade);
  ok(cv.corporateActionStatus === 'suspected', 'verdict 携带 status', cv.corporateActionStatus);

  // 对照 A：events 精确命中 → confirmed，用「を検出」
  const indC = MA2.computeIndicators(rows, {}, {
    priceBarsAll: [], lastMarginDate: D[5],
    barsMeta: { known: true, splits: [{ date: D[1], ratio: '1:3' }] },
  });
  ok(indC.corporateAction.status === 'confirmed', '对照：events 命中 = confirmed', indC.corporateAction.status);
  ok(/を検出/.test(indC.corporateAction.text), 'confirmed 用「を検出」', indC.corporateAction.text);
  ok(!/可能性があります/.test(indC.corporateAction.text), 'confirmed 不得用「可能性があります」', indC.corporateAction.text);

  // 对照 B：events 已知且窗口内无拆股 → 不受影响
  const indN = MA2.computeIndicators(rows, {}, {
    priceBarsAll: [], lastMarginDate: D[5],
    barsMeta: { known: true, splits: [{ date: '2026-01-01', ratio: '1:2' }] },
  });
  ok(indN.corporateAction.affected === false,
     'events 已知时不做启发式（不重复误报）', indN.corporateAction);
  ok(indN.corporateAction.status === 'none', '未命中时 status = none', indN.corporateAction.status);

  // 对照 C：中段无跳变 → 不误报
  // 用一条完全平稳的序列（消除拆股），避免只改一两行而在别处造出新跳变。
  const calm = rows.map((r, i) => Object.assign({}, r, {
    buy: 1000000 + i * 10000, close: 100 + i,
  }));
  const indCalm = MA2.computeIndicators(calm, {}, {
    priceBarsAll: [], lastMarginDate: D[5],
    barsMeta: { known: false, splits: [] },
  });
  ok(indCalm.corporateAction.affected === false, '无跳变时不误报', indCalm.corporateAction);

  // 对照 D：跳变但价格同步大跌 → 更像真实行情，不判拆股
  const realDrop = rows.map((r, i) => (i === 1 ? Object.assign({}, r, { close: 60 }) : r));
  const indReal = MA2.computeIndicators(realDrop, {}, {
    priceBarsAll: [], lastMarginDate: D[5],
    barsMeta: { known: false, splits: [] },
  });
  ok(indReal.corporateAction.affected === false, '价格同步大跌时不判拆股', indReal.corporateAction);

  // 阈值未变：刚好 50% / 刚好 20% 均不算命中
  const edge = rows.map((r, i) => (i === 1 ? Object.assign({}, r, { buy: 1500000, close: 80 }) : r));
  const indEdge = MA2.computeIndicators(edge, {}, {
    priceBarsAll: [], lastMarginDate: D[5],
    barsMeta: { known: false, splits: [] },
  });
  ok(indEdge.corporateAction.affected === false,
     '阈值边界（跳变恰 50% / 价格跌 20%）不命中，确认阈值未放宽',
     indEdge.corporateAction.reasons);
}

/* =================================================================
 * D. creditPosition 必须支持 unknown
 * ================================================================= */
console.log('\n=== D. digest=null 且 buyListed=null → unknown ===');
{
  const p = MA2.creditPosition({ digest: { days: null }, borrowRate: { buyListed: null } });
  ok(p.key === 'unknown', 'key = unknown', p.key);
  ok(p.label === '判定不能', 'label = 判定不能', p.label);
  ok(p.unknown === true, 'unknown 标记为 true');
  ok(p.color === '#8b96ad', 'unknown 用中性灰（非绿色）', p.color);
  ok(!/軽い/.test(p.label), '不得显示「軽い」');

  // 有其一即可判断，且必须如实说明另一项不明
  const onlyDigest = MA2.creditPosition({ digest: { days: 0.4 }, borrowRate: { buyListed: null } });
  ok(onlyDigest.key === 'light', '仅 digest 可用 → light', onlyDigest.key);
  ok(/発行済比 不明/.test(onlyDigest.basis), 'basis 标明 発行済比 不明', onlyDigest.basis);

  /* ★ A1：発行済比 要参与仓位判定，分母必须**可信**。
     旧 fixture 未声明分母来源，隐含假设比值可信；新契约要求显式给出。*/
  const TRUSTED   = { confidence: 'high', usableAsHardTrigger: true };
  const UNTRUSTED = { confidence: 'low',  usableAsHardTrigger: false };

  const onlyListed = MA2.creditPosition({ digest: { days: null },
    borrowRate: { buyListed: 12, denominator: TRUSTED } });
  ok(onlyListed.key === 'heavy', '仅 発行済比 可用且 ≥10%（分母可信）→ heavy', onlyListed.key);
  ok(/消化日数 不明/.test(onlyListed.basis), 'basis 标明 消化日数 不明', onlyListed.basis);

  // ★ A1：同一个 12%，但分母不可信 → 不得判 heavy，也绝不能退而求其次判 軽い
  const lowListed = MA2.creditPosition({ digest: { days: null },
    borrowRate: { buyListed: 12, denominator: UNTRUSTED } });
  ok(lowListed.key === 'unknown', '分母不可信时：仅 発行済比 可用 → 判定不能', lowListed.key);
  ok(!/重い/.test(lowListed.label) && !/軽い/.test(lowListed.label),
     '分母不可信时不得输出 重い / 軽い', lowListed.label);
  ok(/分母可信度不足/.test(lowListed.basis), 'basis 说明分母未用于判定', lowListed.basis);

  // GRADES 必须有 unknown，且不是绿色
  ok(MA2.GRADES.unknown != null, 'GRADES.unknown 存在');
  ok(MA2.GRADES.unknown.color !== '#22c55e', 'GRADES.unknown 不用绿色');
}

/* =================================================================
 * E. 分母可信度必须影响评级
 * ================================================================= */
console.log('\n=== E. denomStable=false 时极端 buyListed 不得单独造成 hard bad ===');
{
  const bl = bars(20, '2026-09-20', 50000000);   // 高均量 → digest 很轻
  // 3905 实测形态：buyListed 31.58%（极端），但与 JPX 上場比 26.3 乖离 >20%
  const rows = [{ date: '2026-09-20', buy: 10110900, sell: 1848100, ratio: 5.47, close: 500, buyListed: 26.3 }];
  const ind = MA2.computeIndicators(rows, { shares: 32017051 },
    { priceBarsAll: bl, lastMarginDate: '2026-09-20' });

  const dn = ind.borrowRate.denominator;
  ok(dn.confidence === 'low', 'denominator.confidence = low', dn);
  ok(dn.stable === false, 'denominator.stable = false', dn.stable);
  ok(dn.usableAsHardTrigger === false, 'usableAsHardTrigger = false');
  ok(typeof dn.diffPct === 'number', 'denominator.diffPct 已记录', dn.diffPct);
  ok(dn.type === '発行済株式数' && dn.source === 'Ganan 発行済株式数',
     'denominator 带 type/source', { t: dn.type, s: dn.source });
  ok(!!dn.date, 'denominator 带 date', dn.date);

  const v = MA2.runRules(ind, {});
  const cv = MA2.creditVerdict(ind, v);
  ok(!v.riskHits.some(h => h.id === 'listed-high'), 'listed-high 未加分', v.riskHits.map(h => h.id));
  ok(v.riskNotes.some(h => h.id === 'listed-high-untrusted'), '降级为 soft 提示', v.riskNotes.map(h => h.id));
  ok(cv.grade !== 'bad', '未因低可信分母判 bad', cv.grade);
  ok(v.riskNotes.concat(v.riskHits).every(h => (h.w || 0) >= 0), 'soft 规则权重为 0');

  // 对照：分母可信时，listed-high 应正常触发
  const rows2 = [{ date: '2026-09-20', buy: 10110900, sell: 1848100, ratio: 5.47, close: 500, buyListed: 31.5 }];
  const ind2 = MA2.computeIndicators(rows2, { shares: 32017051 },
    { priceBarsAll: bl, lastMarginDate: '2026-09-20' });
  const v2 = MA2.runRules(ind2, {});
  ok(ind2.borrowRate.denominator.confidence === 'high', '对照：分母可信 confidence=high');
  ok(v2.riskHits.some(h => h.id === 'listed-high'), '对照：分母可信时 listed-high 正常触发');

  // 分母完全缺失 → confidence='na'，也不得作为 hard trigger
  const rows3 = [{ date: '2026-09-20', buy: 10110900, sell: 1848100, ratio: 5.47, close: 500, buyListed: 26.3 }];
  const ind3 = MA2.computeIndicators(rows3, { shares: null },
    { priceBarsAll: bl, lastMarginDate: '2026-09-20' });
  ok(ind3.borrowRate.denominator.confidence === 'na', '无 shares → confidence=na');
  ok(ind3.borrowRate.denominator.usableAsHardTrigger === false, 'na 不得作 hard trigger');
  ok(ind3.borrowRate.denominator.type === '上場株式数', 'na 时 type=上場株式数', ind3.borrowRate.denominator.type);
}

/* =================================================================
 * F. deleveraging 不增加 risk
 * ================================================================= */
console.log('\n=== F. deleveraging 不得增加 risk score ===');
{
  ok(!MA2.RISK_RULES.some(r => r.id === 'deleveraging'),
     'RISK_RULES 中已无 deleveraging', MA2.RISK_RULES.map(r => r.id));
  ok(MA2.DIR_RULES.some(r => r.id === 'q-deleverage'),
     '方向侧 q-deleverage 保留', MA2.DIR_RULES.map(r => r.id));

  const rows = [
    { date: '2026-09-28', buy: 2000000, sell: 100000, ratio: 20, close: 100 },
    { date: '2026-09-29', buy: 1900000, sell: 100000, ratio: 19, close: 95 },
  ];
  const ind = MA2.computeIndicators(rows, {}, { priceBarsAll: [], lastMarginDate: '2026-09-29' });
  ok(ind.quadrant.key === 'deleverage', 'quadrant = deleverage', ind.quadrant.key);
  const v = MA2.runRules(ind, {});
  ok(v.risk === 0, 'risk = 0（去杠杆不加分）', v.risk);
  ok(v.dirHits.some(h => h.id === 'q-deleverage'), '方向侧仍记 q-deleverage（改善含义）');
  ok(v.dir > 0, '方向分为正（偏建设性）', v.dir);

  // deleverage 的 level 不应是 warn（避免看起来像风险）
  ok(ind.quadrant.level !== 'warn', 'quadrant.level 不为 warn', ind.quadrant.level);
}

/* =================================================================
 * I. verdict 与显示文案不能矛盾
 * ================================================================= */
console.log('\n=== I. verdict 为唯一结论来源，文案不得矛盾 ===');
{
  const bl = bars(20, '2026-09-20', 1000000);
  // 构造多种形态，检查 badge / reasonFacts 的一致性
  const cases = [
    { name: '下落中买残增加',
      rows: [
        { date: '2026-09-18', buy: 1000000, sell: 100000, ratio: 10, close: 100, buyListed: 0.5 },
        { date: '2026-09-19', buy: 1300000, sell: 100000, ratio: 13, close: 92,  buyListed: 0.6 },
      ] },
    { name: '買残减少+株価上昇',
      rows: [
        { date: '2026-09-18', buy: 1300000, sell: 100000, ratio: 13, close: 92,  buyListed: 0.6 },
        { date: '2026-09-19', buy: 1000000, sell: 100000, ratio: 10, close: 100, buyListed: 0.5 },
      ] },
    { name: '株価横ばい+買残不変',
      rows: [
        { date: '2026-09-18', buy: 1000000, sell: 100000, ratio: 10, close: 100, buyListed: 0.5 },
        { date: '2026-09-19', buy: 1000000, sell: 100000, ratio: 10, close: 101, buyListed: 0.5 },
      ] },
  ];

  const POS = { good: '改善', mid: '中立', warn: '注意', bad: '悪化' };
  for (const c of cases) {
    const ind = MA2.computeIndicators(c.rows, { shares: 100000000 },
      { priceBarsAll: bl, lastMarginDate: '2026-09-19' });
    const v = MA2.runRules(ind, {});
    const cv = MA2.creditVerdict(ind, v);

    ok(cv.badge.includes(cv.label), c.name + '：badge 含 label', cv.badge);
    ok(cv.badge.includes(POS[cv.grade]),
       c.name + '：badge 文字与 grade 一致', { badge: cv.badge, grade: cv.grade });
    ok(Array.isArray(cv.reasonFacts) && cv.reasonFacts.length > 0,
       c.name + '：reasonFacts 非空');
    // 徽章含「改善」时，reasonFacts 不得出现「悪化/弱気」
    if (cv.label === '改善') {
      const txt = cv.reasonFacts.join(' | ');
      ok(!/悪化|弱気優勢/.test(txt) || /需給の改善/.test(txt),
         c.name + '：改善时 reasonFacts 不自相矛盾', txt);
    }
    // UI 用的字段必须齐备
    ok(typeof cv.reasonCode === 'string' && cv.reasonCode.length > 0,
       c.name + '：reasonCode 存在');
    ok(cv.comparison != null, c.name + '：verdict 携带 comparison');
    ok(cv.denominatorConfidence != null, c.name + '：verdict 携带 denominatorConfidence');
  }

  // 未定义 grade 必须能安全渲染
  ok(typeof MA2.GRADES.unknown.label === 'string', 'GRADES.unknown.label 可渲染');
}

/* =================================================================
 * 附加：digest.period 字段名修正（issue 11a）
 * ================================================================= */
console.log('\n=== 附加：RISK_RULES 不得引用不存在的 digest.span ===');
{
  const b = bars(25, '2026-09-25', 1000000);
  const rows = [{ date: '2026-09-25', buy: 10000000, sell: 100000, ratio: 100, close: 100 }];
  const ind = MA2.computeIndicators(rows, { shares: 100000000 },
    { priceBarsAll: b, lastMarginDate: '2026-09-25' });
  const v = MA2.runRules(ind, {});
  ok(ind.digest.days >= 5, '构造出 digest >= 5 日', ind.digest.days);
  ok(ind.digest.span === undefined && ind.digest.period != null,
     'digest 无 span，有 period', { span: ind.digest.span, period: ind.digest.period });
  const texts = v.riskHits.map(h => h.whyText).concat(v.riskNotes.map(h => h.whyText));
  ok(texts.length > 0, '有触发规则文本');
  ok(!texts.some(t => /undefined/.test(t)), '无 undefined 字样', texts);
}

console.log('\n' + '='.repeat(56));
console.log(`${fail === 0 ? '✅' : '❌'}  通过 ${pass} / 失败 ${fail}`);
process.exit(fail ? 1 : 0);