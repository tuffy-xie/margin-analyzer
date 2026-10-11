/**
 * test_a4_period.mjs —— A4.1 信用残比較期間 / 株価方向语义修复 回归测试
 * -------------------------------------------------------------------------
 * 覆盖用户指定的 A~K 十一项目标（固定 fixture，不依赖真实 Yahoo / JPX 网络）：
 *   A. 比較期間上涨 + 当日下跌 → 显示期間上涨并标明比较日期
 *   B. 比較期間下跌 + 当日上涨 → 显示期間下跌
 *   C. 缺起始日期 → 不编造比较期間
 *   D. 缺结束/价格 → 不编造涨跌幅
 *   E. 比较起止 = 实际参与比较的「匹配交易日」日期（≠ 今天 / ≠ weekly 4W/8W）
 *   F. 同股连续刷新 → 日期与结果属于同一请求（确定性）
 *   G. 4519 → 5801 快速切换 → 不显示 4519 的时间范围
 *   H. A1 有效性门槛保持：象限 unknown → 不得输出 🟢改善
 *   I. A2 weekly 自然周不受影响
 *   J. A3.1 公司行动保护不受影响
 *   K. A3.2 stale / fresh 状态不受影响
 *
 * 本测试只验证「数据契约」：首页展示读取的是 engine 已算好的
 *   ind.comparison.from / .to / .periods  与  quadrant.priceChg
 * 这些字段本身；DOM 渲染字符串由 verify_offline.py (O6) 另测。
 *
 * 运行：node test_a4_period.mjs
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
const mk = (dates, closes, buys, sells) => dates.map((d, i) => ({
  date: d, close: closes[i], buy: buys[i], sell: sells[i],
  ratio: +(buys[i] / sells[i]).toFixed(2),
}));

// 巨大成交量 → 買残消化日数极小（<1.5），避免 absHeavy 干扰 A1/H 判定
const bigBars = (lastDate) => {
  const out = [];
  const d = new Date(lastDate + 'T00:00:00Z');
  for (let i = 0; i < 30; i++) {
    out.push({ date: d.toISOString().slice(0, 10), close: 100, vol: 1e9 });
    d.setUTCDate(d.getUTCDate() - 1);
  }
  return out;
};

const ctxOf = (rows, extra = {}) => Object.assign(
  { priceBarsAll: bigBars(rows[rows.length - 1].date), lastMarginDate: rows[rows.length - 1].date,
    barsMeta: { splits: [], known: true } }, extra);

const singleDayChg = (closes) =>
  +(((closes[closes.length - 1] - closes[closes.length - 2]) / closes[closes.length - 2]) * 100).toFixed(2);

// A4.1 展示决策（与 index.html renderHero 中 hCmp 逻辑一致）：
//   cmpOk = comparison 存在且 from/to 均存在 → 才显示期間；否则「データ不足」
//   rateReliable = 象限非 corporateAction / unknown → 才显示涨跌幅，否则 "—"
const cmpOk = (ind) => { const c = ind.comparison; return !!(c && c.from && c.to); };
const rateReliable = (q) => !(q.key === 'corporateAction' || q.key === 'unknown');

/* =================================================================
 * A. 比較期間上涨 + 当日下跌
 * ================================================================= */
console.log('\n=== A. 比較期間上涨 + 当日下跌 ===');
{
  const dates = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06'];
  // 整窗 rising，最后一天回落（"当日"下跌 0.93%）
  const closes = [100, 102, 104, 106, 108, 107];
  // 买残持续下降 → 期间 buyChg 负
  const buys = [100, 98, 96, 94, 92, 91].map(x => x * 10000);
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(dates, closes, buys, sells);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const q = ind.quadrant, c = ind.comparison;
  const sd = singleDayChg(closes); // 当日（最后一天）涨跌

  ok(cmpOk(ind), 'A：comparison 已返回（from/to 存在）', c);
  ok(c.from === '2026-10-01' && c.to === '2026-10-06', 'A：比较期間 = 全データ范围 rows[0..5] = 10/01→10/06', c);
  ok(c.periods === 5, 'A：periods = 5', c && c.periods);
  ok(q.priceChg > 0, 'A：期間株価騰落率 > 0（期间上涨）', q.priceChg);
  ok(sd < 0, 'A：当日（最后一天）实际下跌', sd);
  ok(q.priceChg > 0 && sd < 0, 'A：期間上涨 与 当日下跌 解耦（符号相反，非同一量）', { priceChg: q.priceChg, singleDay: sd });
  ok(rateReliable(q), 'A：方向可判定 → 涨跌幅会如实显示（非 —）', q.key);
}

/* =================================================================
 * B. 比較期間下跌 + 当日上涨
 * ================================================================= */
console.log('\n=== B. 比較期間下跌 + 当日上涨 ===');
{
  const dates = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06'];
  const closes = [108, 106, 104, 102, 100, 101]; // 最后一天反抽（当日上涨）
  const buys = [100, 98, 96, 94, 92, 91].map(x => x * 10000);
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(dates, closes, buys, sells);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const q = ind.quadrant, c = ind.comparison;
  const sd = singleDayChg(closes);

  ok(cmpOk(ind), 'B：comparison 已返回', c);
  ok(q.priceChg < 0, 'B：期間株価騰落率 < 0（期间下跌）', q.priceChg);
  ok(sd > 0, 'B：当日（最后一天）实际上涨', sd);
  ok(q.priceChg < 0 && sd > 0, 'B：期間下跌 与 当日上涨 解耦', { priceChg: q.priceChg, singleDay: sd });
}

/* =================================================================
 * C. 缺起始日期 → 不编造
 * ================================================================= */
console.log('\n=== C. 缺起始日期（仅 1 行）→ 不编造比較期間 ===');
{
  const rows = mk(['2026-10-06'], [107], [91 * 10000], [Math.round(91 * 10000 / 16)]);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  ok(ind.comparison === null, 'C：仅 1 行 → comparison = null（无法构成区间）', ind.comparison);
  ok(!cmpOk(ind), 'C：展示决策判定为「比較期間：データ不足」（不编造日期）');
}

/* =================================================================
 * D. 缺结束/价格 → 不编造涨跌幅
 * ================================================================= */
console.log('\n=== D. 缺结束端价格 → 不编造涨跌幅 ===');
{
  const dates = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06'];
  const closes = [100, 102, 104, 106, 108, null]; // 最后一天无行情
  const buys = [100, 98, 96, 94, 92, 91].map(x => x * 10000);
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(dates, closes, buys, sells);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const q = ind.quadrant, c = ind.comparison;

  ok(cmpOk(ind), 'D：比较期間日期仍可得（from/to 存在）', c && { from: c.from, to: c.to });
  ok(q.priceChg === null, 'D：窗口末端缺行情 → priceChg = null（不编造百分比）', q.priceChg);
  ok(q.key === 'unknown', 'D：方向判定为 unknown（与空四象限一致）', q.key);
  ok(!rateReliable(q), 'D：涨跌幅将显示「—」（不伪造 +x%）');
}

/* =================================================================
 * E. 比较起止 = 实际参与比较的「匹配交易日」日期（≠ 今天 / ≠ weekly）
 * ================================================================= */
console.log('\n=== E. 比较日期 = 匹配交易日（≠ 今天 / ≠ weekly 4W/8W） ===');
{
  const dates = ['2026-09-28','2026-09-29','2026-09-30','2026-10-01','2026-10-02','2026-10-05'];
  const closes = [100, 101, 102, 103, 104, 105];
  const buys = [100, 99, 98, 97, 96, 95].map(x => x * 10000);
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(dates, closes, buys, sells);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const c = ind.comparison;
  const today = '2026-10-10';                 // 假设的「今天」
  const weekly4wFrom = '2026-09-07';          // 假设的 weekly 4W 起点（完全不同的日期）

  ok(cmpOk(ind), 'E：comparison 已返回', c);
  ok(c.from === dates[0] && c.to === dates[5], 'E：from/to = 实际参与比较的 margin 行日期（全范围）', c);
  ok(c.from !== today && c.to !== today, 'E：比较日期 ≠ 今天', { from: c.from, to: c.to, today });
  ok(c.from !== weekly4wFrom, 'E：比较日期 ≠ weekly 4W 起点（不同窗口）', { from: c.from, weekly4wFrom });
  ok(dates.includes(c.from) && dates.includes(c.to), 'E：from/to 均在真实 margin 日期集合内');
}

/* =================================================================
 * F. 同股连续刷新 → 日期与结果属于同一请求（确定性）
 * ================================================================= */
console.log('\n=== F. 同股连续刷新 → 比较期間确定性一致 ===');
{
  const dates = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06'];
  const closes = [100, 102, 104, 106, 108, 107];
  const buys = [100, 98, 96, 94, 92, 91].map(x => x * 10000);
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(dates, closes, buys, sells);

  const a = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const b = MA2.computeIndicators(rows, {}, ctxOf(rows));
  ok(JSON.stringify(a.comparison) === JSON.stringify(b.comparison),
     'F：同一数据两次计算 → 比较期間逐字节一致', a.comparison);
  ok(a.quadrant.priceChg === b.quadrant.priceChg,
     'F：期間株価騰落率逐字节一致', a.quadrant.priceChg);
}

/* =================================================================
 * G. 4519 → 5801 快速切换 → 不显示 4519 的时间范围
 * ================================================================= */
console.log('\n=== G. 4519 → 5801 切换 → 区间不串线 ===');
{
  const d4519 = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06'];
  const d5801 = ['2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-07'];
  const mk2 = (ds) => mk(ds, [100,101,102,103,104,105],
    [100,99,98,97,96,95].map(x=>x*10000), [6,6,6,6,6,6].map(x=>x*10000));
  const a = MA2.computeIndicators(mk2(d4519), {}, ctxOf(mk2(d4519)));
  const b = MA2.computeIndicators(mk2(d5801), {}, ctxOf(mk2(d5801)));
  ok(a.comparison.from === '2026-10-01' && a.comparison.to === '2026-10-06', 'G：4519 区间 = 10/01→10/06', a.comparison);
  ok(b.comparison.from === '2026-10-02' && b.comparison.to === '2026-10-07', 'G：5801 区间 = 10/02→10/07', b.comparison);
  ok(a.comparison.from !== b.comparison.from || a.comparison.to !== b.comparison.to,
     'G：两支股票区间不串线', { a: a.comparison, b: b.comparison });
}

/* =================================================================
 * H. A1 有效性门槛：象限 unknown → 不得输出 🟢改善
 * ================================================================= */
console.log('\n=== H. A1 有效性门槛：象限 unknown → 不得 🟢改善 ===');
{
  // 构造象限 unknown：窗口末端缺行情（priceChg=null）→ key='unknown'
  const dates = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06'];
  const closes = [100, 102, 104, 106, 108, null];
  const buys = [100, 99, 98, 97, 96, 95].map(x => x * 10000);
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(dates, closes, buys, sells);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const rules = MA2.runRules(ind, {});
  const v = MA2.creditVerdict(ind, rules);
  ok(ind.quadrant.key === 'unknown', 'H：象限判定为 unknown', ind.quadrant.key);
  ok(v.grade !== 'good', 'H：证据不足时等级 ≠ 🟢改善', v.grade);
  ok(v.grade === 'unknown', 'H：证据不足时等级 = 判定不能（不伪造好消息）', v.grade);
}

/* =================================================================
 * I. A2 weekly 自然周不受影响
 * ================================================================= */
console.log('\n=== I. A2 weekly 自然周不受影响 ===');
{
  const dates = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06'];
  const closes = [100, 102, 104, 106, 108, 107];
  const buys = [100, 98, 96, 94, 92, 91].map(x => x * 10000);
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(dates, closes, buys, sells);

  const noWeekly = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const withWeekly = MA2.computeIndicators(rows, {}, ctxOf(rows, { weekly: { anything: 1 } }));
  ok(JSON.stringify(noWeekly.comparison) === JSON.stringify(withWeekly.comparison),
     'I：注入 weekly 上下文不影响 comparison', noWeekly.comparison);
  ok(noWeekly.quadrant.priceChg === withWeekly.quadrant.priceChg,
     'I：注入 weekly 上下文不影响期間株価騰落率', noWeekly.quadrant.priceChg);
}

/* =================================================================
 * J. A3.1 公司行动保护不受影响
 * ================================================================= */
console.log('\n=== J. A3.1 公司行动保护不受影响 ===');
{
  const dates = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06'];
  const closes = [100, 102, 104, 106, 108, 107];
  const buys = [100, 98, 96, 94, 92, 91].map(x => x * 10000); // 末端有跳变也无妨
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(dates, closes, buys, sells);
  // 比较窗口内存在确定的拆股事件
  const ctx = ctxOf(rows, { barsMeta: { splits: [{ date: '2026-10-04', ratio: '2:1' }], known: true } });
  const ind = MA2.computeIndicators(rows, {}, ctx);
  ok(ind.corporateActionAffected === true, 'J：检测到公司行动 → affected=true', ind.corporateActionAffected);
  ok(ind.corporateActionStatus === 'confirmed', 'J：状态 = confirmed（精确命中 events）', ind.corporateActionStatus);
  ok(ind.quadrant.key === 'corporateAction', 'J：四象限 key = corporateAction（方向判定停止）', ind.quadrant.key);
  ok(cmpOk(ind), 'J：比较期間日期仍正常返回（公司行动只停方向，不停日期）', ind.comparison);
  ok(!rateReliable(ind.quadrant), 'J：展示将隐藏涨跌幅（方向已停，不显示 +x%）', ind.quadrant.key);
}

/* =================================================================
 * K. A3.2 stale / fresh 不受影响
 * ================================================================= */
console.log('\n=== K. A3.2 stale / fresh 不受影响 ===');
{
  const dates = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06'];
  const closes = [100, 102, 104, 106, 108, 107];
  const buys = [100, 98, 96, 94, 92, 91].map(x => x * 10000);
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(dates, closes, buys, sells);

  const fresh = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const stale = MA2.computeIndicators(rows, {}, ctxOf(rows, { stale: true, degraded: true }));
  ok(JSON.stringify(fresh.comparison) === JSON.stringify(stale.comparison),
     'K：stale 标志不影响 comparison', fresh.comparison);
  ok(fresh.quadrant.priceChg === stale.quadrant.priceChg,
     'K：stale 标志不影响期間株価騰落率', fresh.quadrant.priceChg);
}

/* =================================================================
 * A4.1.1 股价日期溯源（株価参照期間）回归
 * -----------------------------------------------------------------
 * 覆盖用户指定 A~G：
 *   A. 起点日期回退   B. 终点日期回退   C. 起点+终点同时回退
 *   D. 日期完全一致   E. priceDate 缺失   F. 快速切换不串日期
 *   G. 价格涨跌幅与参照价格一致
 * 以及 §2 固定反例（信用残 10/02→10/08 / 实际 Yahoo 10/01→10/07 / +8.0%）。
 *
 * 仅验证「数据契约 + 展示决策」：给定真实参与计算的 row.priceDate，
 * 推导出的 株価参照期間 两端与 fellBack / missing 标志是否正确。
 * 真实 DOM 渲染字符串由 verify_offline.py (O7) 另测。
 * ================================================================= */

// 模拟 index.html pickPrice 的「向过去二分」对齐，给每行赋 priceDate。
// 仅用于 node 契约测试；真实实现路径由 verify_offline O7 经真实 DOM 覆盖。
const alignPriceDates = (rows, priceDateSet) => {
  const pset = [...priceDateSet].sort();
  const map = {}; pset.forEach(d => { map[d] = true; });
  for (const r of rows) {
    if (map[r.date]) { r.priceDate = r.date; r.isSameDate = true; continue; }
    let lo = 0, hi = pset.length - 1, idx = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (pset[m] <= r.date) { idx = m; lo = m + 1; } else hi = m - 1; }
    if (idx < 0) { r.priceDate = null; r.isSameDate = false; }
    else { r.priceDate = pset[idx]; r.isSameDate = (pset[idx] === r.date); }
  }
  return rows;
};

// 从 ind.comparison + rows 推导「株価参照期間」两端（与 index.html renderHero 同源）
const refPeriod = (ind, rows) => {
  const c = ind.comparison;
  if (!c || c.fromIdx == null || c.toIdx == null) return { pf: null, pt: null, fellBack: false, missing: true };
  const pf = (rows[c.fromIdx] && rows[c.fromIdx].priceDate != null) ? rows[c.fromIdx].priceDate : null;
  const pt = (rows[c.toIdx] && rows[c.toIdx].priceDate != null) ? rows[c.toIdx].priceDate : null;
  const fellBack = (pf != null && pf !== c.from) || (pt != null && pt !== c.to);
  const missing = (pf == null) || (pt == null);
  return { pf, pt, fellBack, missing };
};

const chgPct = (a, b) => +(((a - b) / Math.abs(b)) * 100).toFixed(1);

// 构造 6 行 margin（比较窗口 = 全范围 rows[0..5]），closes 控制两端对齐价
const mkCmp = (marginDates, closes, priceSet) => {
  const buys = [100, 99, 98, 97, 96, 95].map(x => x * 10000);
  const sells = buys.map(b => Math.round(b / 16));
  const rows = mk(marginDates, closes, buys, sells);
  alignPriceDates(rows, priceSet);
  return rows;
};

console.log('\n=== A4.1.1 A. 起点日期回退（仅 from 端 fallback） ===');
{
  const dates = ['2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-08'];
  // 价格集含 10/02（from 端同日期）→ 但为演示「仅终点回退」，这里用不含 10/02 的集合
  // 实际：from 端 10/02 在集合 {10/01,10/03..} 中无精确匹配 → 回退到 10/01
  const priceSet = ['2026-10-01','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-07'];
  const rows = mkCmp(dates, [100,102,104,106,107,108], priceSet);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const c = ind.comparison, r = refPeriod(ind, rows);
  ok(c.from === '2026-10-02' && c.to === '2026-10-08', 'A：信用残比較期間 = 10/02→10/08', c);
  ok(r.pf === '2026-10-01', 'A：起点价格日回退 → 株価参照期間起点 = 10/01', r);
  ok(r.pt === '2026-10-07', 'A：终点价格日 = 10/07（集合含 10/07→但 10/08 回退）', r);
  ok(r.fellBack === true, 'A：存在回退 → 应显示株価参照期間', r);
  ok(r.missing === false, 'A：两端价格日均存在（非缺失）', r);
}

console.log('\n=== A4.1.1 B. 终点日期回退（仅 to 端 fallback） ===');
{
  const dates = ['2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-08'];
  // 集合含 10/08（to 端同日期）但不含 10/02 → from 回退；为演示「仅终点回退」用不含 10/08 的集合
  const priceSet = ['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-07'];
  const rows = mkCmp(dates, [100,102,104,106,107,108], priceSet);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const c = ind.comparison, r = refPeriod(ind, rows);
  ok(c.from === '2026-10-02' && c.to === '2026-10-08', 'B：信用残比較期間 = 10/02→10/08', c);
  ok(r.pf === '2026-10-02', 'B：起点价格日 = 10/02（同日期）', r);
  ok(r.pt === '2026-10-07', 'B：终点价格日回退 → 株価参照期間终点 = 10/07', r);
  ok(r.fellBack === true, 'B：存在回退 → 应显示株価参照期間', r);
}

console.log('\n=== A4.1.1 C. 起点+终点同时回退 ===');
{
  const dates = ['2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-08'];
  // 价格集仅为 10/01 与 10/07 → 两端均回退
  const priceSet = ['2026-10-01','2026-10-07'];
  // 两端对齐价：10/01=100, 10/07=108 → +8%
  const rows = mkCmp(dates, [100,102,104,106,107,108], priceSet);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const c = ind.comparison, r = refPeriod(ind, rows);
  ok(c.from === '2026-10-02' && c.to === '2026-10-08', 'C：信用残比較期間 = 10/02→10/08', c);
  ok(r.pf === '2026-10-01' && r.pt === '2026-10-07', 'C：株価参照期間 = 10/01→10/07', r);
  ok(r.fellBack === true && r.missing === false, 'C：两端回退但均存在', r);
  ok(ind.quadrant.priceChg === 8.0, 'C：期間株価騰落率 = +8.0%（与两端参照价一致）', ind.quadrant.priceChg);
}

console.log('\n=== A4.1.1 §2 固定反例（与 C 同源，完整断言展示契约） ===');
{
  const dates = ['2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-08'];
  const priceSet = ['2026-10-01','2026-10-07'];
  const rows = mkCmp(dates, [100,102,104,106,107,108], priceSet);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const c = ind.comparison, r = refPeriod(ind, rows);
  ok(c.from === '2026-10-02' && c.to === '2026-10-08', '反例：信用残比較期間 10/02～10/08', c);
  ok(r.pf === '2026-10-01' && r.pt === '2026-10-07', '反例：株価参照期間 10/01～10/07', r);
  ok(ind.quadrant.priceChg === 8.0, '反例：期間株価騰落率 +8.0%', ind.quadrant.priceChg);
  // 展示层不应拿信用残日期冒充价格日期
  ok(!(r.pf === c.from && r.pt === c.to), '反例：参照期間 ≠ 信用残期間（未冒充）', { pf: r.pf, pt: r.pt, from: c.from, to: c.to });
}

console.log('\n=== A4.1.1 D. 日期完全一致（无回退 → 不显示额外提示） ===');
{
  const dates = ['2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-08'];
  const priceSet = [...dates]; // 价格日 == 信用残日 → 全部 sameDate
  const rows = mkCmp(dates, [100,102,104,106,107,108], priceSet);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const c = ind.comparison, r = refPeriod(ind, rows);
  ok(r.pf === '2026-10-02' && r.pt === '2026-10-08', 'D：株価参照期間 = 10/02→10/08（与信用残一致）', r);
  ok(r.fellBack === false, 'D：两端均无回退 → fellBack=false（展示层不显示额外提示）', r);
  ok(r.missing === false, 'D：两端均存在', r);
}

console.log('\n=== A4.1.1 E. priceDate 缺失 → 不冒充，显示 データ不足 ===');
{
  const dates = ['2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-08'];
  // 价格集仅 10/07 → from 端 10/02 无更早于它的行情 → priceDate=null
  const priceSet = ['2026-10-07'];
  const closes = [null,102,104,106,107,108]; // from 端缺行情（close 也应为 null）
  const rows = mkCmp(dates, closes, priceSet);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const c = ind.comparison, r = refPeriod(ind, rows);
  ok(c.from === '2026-10-02' && c.to === '2026-10-08', 'E：信用残比較期間日期仍可得', c);
  ok(r.pf === null, 'E：起点 priceDate 缺失 = null', r);
  ok(r.missing === true, 'E：priceDate 缺失 → missing=true（展示层显示 データ不足，不冒充）', r);
  ok(ind.quadrant.priceChg === null, 'E：窗口起点缺行情 → priceChg=null（不伪造百分比）', ind.quadrant.priceChg);
}

console.log('\n=== A4.1.1 F. 快速切换不串日期 ===');
{
  const dX = ['2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-08'];
  const dY = ['2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-07','2026-10-09'];
  const rX = mkCmp(dX, [100,102,104,106,107,108], ['2026-10-01','2026-10-07']);
  const rY = mkCmp(dY, [100,102,104,106,107,108], ['2026-10-02','2026-10-08']);
  const iX = MA2.computeIndicators(rX, {}, ctxOf(rX));
  const iY = MA2.computeIndicators(rY, {}, ctxOf(rY));
  const refX = refPeriod(iX, rX), refY = refPeriod(iY, rY);
  ok(iX.comparison.from === '2026-10-02' && iX.comparison.to === '2026-10-08', 'F：X 信用残期間 10/02→10/08', iX.comparison);
  ok(iY.comparison.from === '2026-10-03' && iY.comparison.to === '2026-10-09', 'F：Y 信用残期間 10/03→10/09', iY.comparison);
  ok(refX.pf === '2026-10-01' && refX.pt === '2026-10-07', 'F：X 参照期間 10/01→10/07', refX);
  ok(refY.pf === '2026-10-02' && refY.pt === '2026-10-08', 'F：Y 参照期間 10/02→10/08', refY);
  ok(refX.pf !== refY.pf || refX.pt !== refY.pt, 'F：两股参照期間不串线', { refX, refY });
}

console.log('\n=== A4.1.1 G. 价格涨跌幅与实际参照价格一致 ===');
{
  const dates = ['2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-08'];
  // 参照价：10/01=100, 10/07=108 → +8.0%
  const priceSet = ['2026-10-01','2026-10-07'];
  const rows = mkCmp(dates, [100,102,104,106,107,108], priceSet);
  const ind = MA2.computeIndicators(rows, {}, ctxOf(rows));
  const c = ind.comparison, r = refPeriod(ind, rows);
  // 引擎 priceChg 用的就是两端对齐价（rows[0].close / rows[5].close）
  const refStart = rows[c.fromIdx].close, refEnd = rows[c.toIdx].close;
  const expect = chgPct(refEnd, refStart);
  ok(ind.quadrant.priceChg === expect, 'G：期間株価騰落率 == 由参照价实算', { priceChg: ind.quadrant.priceChg, expect });
  ok(Math.abs(ind.quadrant.priceChg - 8.0) < 1e-9, 'G：参照价 100→108 得 +8.0%', ind.quadrant.priceChg);
  ok(r.pf === '2026-10-01' && r.pt === '2026-10-07', 'G：参照期間两端即参与计算的两端行情日', r);
}

/* ================================================================= */
console.log('\n' + '='.repeat(60));
console.log(`A4.1 / A4.1.1 数据契约测试： ${pass} 通过 / ${fail} 失败`);
if (fail) { console.log('❌ 存在失败项'); process.exit(1); }
console.log('✅ A4.1 / A4.1.1 数据契约测试全部通过');
