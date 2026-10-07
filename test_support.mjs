/**
 * test_support.mjs —— 「👀 注目」短期支持位回归测试
 * ------------------------------------------------------------------
 * 验证 calcShortSupport 的核心约束（直接抽 index.html 里的真实函数）：
 *   A. 多个 pivot low（4011/4018/4025）→ 聚类到约 4020 附近，touches >= 3
 *   B. 下方较强区域(touch多) vs 单次更近区域 → 优先较强有效区域
 *   C. 无可靠 pivot → fallback 到 20 日低点（type='20d_low'）
 *   D. 行情不足(<20根) → support none，不显示价格
 *   E. 旧逻辑 currentPrice*0.98 已彻底移除（源码级断言）
 *   F. 未来 K 线不能参与 pivot（最后两根永不成 pivot）
 *   G. 支撑位严格来自真实 bars（不从别处伪造）
 *   H. 关注：不得使用 currentPrice * 固定比例（距离不由比例决定）
 *
 * 运行：node test_support.mjs
 */
import fs from 'fs';
import vm from 'vm';

const html = fs.readFileSync('./index.html', 'utf8');

/* 从 index.html 抽出 calcShortSupport 的源码并注入沙箱执行，
   确保测的是「页面上真正跑的那份实现」，而不是复制品。 */
function extractFunc(src, name){
  const start = src.indexOf('function ' + name);
  if (start < 0) throw new Error('未找到函数 ' + name);
  // 找到函数体结束的第一个 "\n}"（顶层右花括号）
  let i = src.indexOf('{', start);
  let depth = 0;
  for (; i < src.length; i++){
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}'){ depth--; if (depth === 0){ i++; break; } }
  }
  return src.slice(start, i);
}

const fnSrc = extractFunc(html, 'calcShortSupport');
const ctx = { window:{}, console, isFinite, Math, Date };
vm.createContext(ctx);
vm.runInContext(fnSrc, ctx);
const calcShortSupport = ctx.calcShortSupport;

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); }
};

/* ---------- 构造 bars 工具 ---------- */
// 生成一根：close 为给定值
const B = (date, close) => ({ date, close, adjClose: close, vol: 1000 });
// 由 close 数组造 bars，日期用递增日
function mkBars(closes, startDay = 1){
  return closes.map((c, i) => {
    const d = String(startDay + i).padStart(2, '0');
    return B('2026-01-' + d, c);
  });
}

/* ============================================================
 * A. 聚类：4011 / 4018 / 4025 → 一个区域，中心≈4020，touches>=3
 * ============================================================ */
{
  // 造一个 60 根序列，其中 index 20/30/40 分别是局部低点 4011/4018/4025
  const closes = [];
  for (let i = 0; i < 60; i++) closes.push(4100);           // 基准高位
  closes[20] = 4011; closes[21] = 4050;                     // 让 20 成为 pivot（后2根更高）
  closes[30] = 4018; closes[31] = 4050;
  closes[40] = 4025; closes[41] = 4050;
  // pivot 需要「前2根」也更高：把低点前一根压低不必要（基准 4100 已高于低点）
  const bars = mkBars(closes);
  const sup = calcShortSupport(bars, 4100);
  ok('A. 三个相近 pivot 聚类为 1 个区域', sup.type === 'pivot_cluster', 'type=' + sup.type);
  ok('A. touches >= 3', sup.touches >= 3, 'touches=' + sup.touches);
  ok('A. 中心价≈4020（4011/4018/4025 均值附近）',
     sup.price >= 4015 && sup.price <= 4021, 'price=' + sup.price);
  ok('A. price < currentPrice（当前价下方）', sup.price < 4100, 'price=' + sup.price);
  ok('A. distancePct 为正（距离按下方计）', sup.distancePct > 0, 'distancePct=' + sup.distancePct);
}

/* ============================================================
 * B. 优先级：下方 touch 多的强区域 vs 更近但只 touch 1 次的区域 → 选强区域
 * ============================================================ */
{
  const closes = [];
  for (let i = 0; i < 60; i++) closes.push(4200);
  // 远端强区域：三个 pivot 聚在 ~4000（touch>=3）
  closes[15] = 4000; closes[16] = 4050;
  closes[25] = 4002; closes[26] = 4050;
  closes[35] = 3998; closes[36] = 4050;
  // 近端单次区域：只有一个 pivot 在 ~4180（离 4200 更近，但只 touch 1）
  closes[45] = 4180; closes[46] = 4195;
  const bars = mkBars(closes);
  const sup = calcShortSupport(bars, 4200);
  ok('B. 优先选择 touch 更多的强区域（约4000，非更近的4180）',
     sup.price >= 3995 && sup.price <= 4005, 'price=' + sup.price);
  ok('B. type=pivot_cluster', sup.type === 'pivot_cluster', 'type=' + sup.type);
  ok('B. touches>=3', sup.touches >= 3, 'touches=' + sup.touches);
}

/* ============================================================
 * C. 无可靠 pivot → fallback 20 日低点
 * ============================================================ */
{
  // 单调下行序列：无 pivot low（无局部反转），但有 >=20 根
  const closes = [];
  for (let i = 0; i < 60; i++) closes.push(4300 - i * 5);  // 4300 → 4005 递减
  const bars = mkBars(closes);
  const sup = calcShortSupport(bars, 4300);
  // 单调序列最后一个(最低)不是 pivot（缺后2根），故应走 20d_low
  ok('C. 无 pivot 时 fallback type=20d_low', sup.type === '20d_low', 'type=' + sup.type);
  ok('C. 20日低点低于当前价', sup.price < 4300, 'price=' + sup.price);
  ok('C. 20日低点≈序列最低（4005附近）', sup.price >= 4000 && sup.price <= 4010, 'price=' + sup.price);
}

/* ============================================================
 * D. 行情不足（<20根）→ none，不显示价格
 * ============================================================ */
{
  const closes = [];
  for (let i = 0; i < 10; i++) closes.push(4300 - i * 5);  // 只有 10 根
  const bars = mkBars(closes);
  const sup = calcShortSupport(bars, 4300);
  ok('D. <20根且无 pivot → type=none', sup.type === 'none', 'type=' + sup.type);
  ok('D. none 时 price=null（不硬凑价格）', sup.price === null, 'price=' + sup.price);
}
{
  // 空 bars / null
  const sup0 = calcShortSupport([], 1000);
  const sup1 = calcShortSupport(null, 1000);
  const sup2 = calcShortSupport(mkBars([100,99,98,97,96,95,94,93]), null);
  ok('D. 空/无效输入 → none + price=null',
     sup0.type==='none' && sup1.type==='none' && sup2.type==='none' &&
     sup0.price===null && sup1.price===null && sup2.price===null);
}

/* ============================================================
 * E. 旧逻辑 currentPrice*0.98 已彻底移除（源码级，忽略注释）
 * ============================================================ */
{
  // 先剥掉 /* */ 与 // 注释，避免把「说明我不做什么」的注释误判为代码
  const codeOnly = html
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1');
  ok('E. index.html 代码中不再有 0.98（机械 -2% 已删除）', !/0\.98/.test(codeOnly));
  ok('E. 代码中不存在 price*0.9x 之类固定比例支撑', !/price\s*\*\s*0\.9/.test(codeOnly));
}

/* ============================================================
 * F. 未来 K 线不能参与 pivot：最后两根永不被标记为 pivot
 * ============================================================ */
{
  // 中间放一个「真 pivot」4000(idx30, 前后都是 4100)，
  // 最后一天放一个更低的 3990(idx59) —— 若实现错误地把最后一天
  // 也当作 pivot，则会与 4000 聚成 touches=2 的簇；正确实现只认 idx30。
  const closes = [];
  for (let i = 0; i < 60; i++) closes.push(4100);
  closes[30] = 4000;   // 真 pivot：idx29/28=4100, idx31/32=4100
  closes[59] = 3990;   // 最后一天最低，但缺后2根 → 绝不能是 pivot
  const sup = calcShortSupport(mkBars(closes), 4100);
  ok('F. 最后一天(最低3990)未被当作 pivot（touches 仍为 1）',
     sup.price !== 3990 && sup.touches === 1,
     'price=' + sup.price + ' touches=' + sup.touches);
  ok('F. 选中的仍是中间的真 pivot(4000)', sup.price === 4000, 'price=' + sup.price);

  // 倒数第二天同理：idx58 缺 idx60 → 不是 pivot
  const closes2 = [];
  for (let i = 0; i < 60; i++) closes2.push(4100);
  closes2[30] = 4000;
  closes2[58] = 3985; closes2[59] = 4050;   // idx58 缺 idx60 → 不是 pivot
  const sup2 = calcShortSupport(mkBars(closes2), 4100);
  ok('F. 倒数第二天(3985)未被当作 pivot',
     sup2.price === 4000 && sup2.touches === 1,
     'price=' + sup2.price + ' touches=' + sup2.touches);
}

/* ============================================================
 * G. 支撑严格来自 bars：篡改 bars 会改变结果（证明非固定公式）
 * ============================================================ */
{
  const closes = [];
  for (let i = 0; i < 60; i++) closes.push(4100);
  closes[20] = 4011; closes[21] = 4050;
  closes[30] = 4018; closes[31] = 4050;
  const a = calcShortSupport(mkBars(closes), 4100);
  const closes2 = closes.slice();
  closes2[20] = 3900;   // 把同一个 pivot 改到更低
  const b = calcShortSupport(mkBars(closes2), 4100);
  ok('G. 支撑随真实 bars 变化（非固定公式）', a.price !== b.price,
     'A=' + a.price + ' B=' + b.price);
}

/* ============================================================
 * H. 距离不来自固定比例：distancePct 随真实价位变化
 * ============================================================ */
{
  // 用远离 2% 的价位构造，确保 distancePct 不是固定 -2%
  const closes = [];
  for (let i = 0; i < 60; i++) closes.push(4100);
  closes[20] = 3900; closes[21] = 4050;   // 支撑≈3900，距 4100 约 4.9%
  const bars = mkBars(closes);
  const s1 = calcShortSupport(bars, 4100);
  const expect = +(((4100 - s1.price) / 4100) * 100).toFixed(1);
  ok('H. distancePct 与 price/current 自洽（不是硬编码 -2%）',
     Math.abs(s1.distancePct - expect) < 0.11,
     'got=' + s1.distancePct + ' expect=' + expect);
  ok('H. distancePct 与 2.0 明显不同（真实约 4.9%）',
     Math.abs(s1.distancePct - 2.0) > 1.0,
     'distancePct=' + s1.distancePct);
  // 换 currentPrice，distancePct 应随之改变（进一步证明非常量）
  const s2 = calcShortSupport(bars, 3900);   // 此时 3900 不再低于现价
  ok('H. 提高 currentPrice 后支撑选择随之变化（非常量公式）',
     s2.type === 'none' || s2.price !== s1.price || s2.distancePct !== s1.distancePct,
     's1=' + s1.price + '@' + s1.distancePct + '% s2=' + s2.price + '@' + s2.distancePct + '%');
}

/* ========================================================== */
console.log('\n  test_support: ' + pass + ' 通过, ' + fail + ' 失败');
if (fail > 0) process.exit(1);
