/* =========================================================================
 * 信用需給分析引擎 v3
 * -------------------------------------------------------------------------
 * v3 相比 v2 的关键修正：
 *  1. 新增「株価 × 信用買残」四象限判断 —— 信用残方向必须结合股价方向，
 *     否则「股价下跌 + 买残增加」会被误判为「温和建仓」（方向性错误）。
 *  2. 新增「買残消化日数」= 信用買残 ÷ 20日平均成交量。
 *     信用倍率在卖残极少时会失真，消化日数用「绝对量 vs 换手」看真实压力。
 *  3. 信用倍率拆解为买残侧 / 卖残侧贡献，回答「倍率高到底因为什么」。
 *  4. 方向（Direction）与风险（Risk）**分离**，不再混成一个分数。
 *     「信用风险高」≠「股价一定跌」。
 *  5. 13週前比在无周次数据时返回 null，绝不用日次 5 期冒充。
 * ========================================================================= */

/* ---------------- 基础工具 ---------------- */
const sd = (a, b, d = 2) =>
  (a == null || b == null || b === 0) ? null : +(a / b).toFixed(d);

function pctPos(v, lo, hi) {
  if (v == null || lo == null || hi == null || hi === lo) return null;
  return Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100));
}

/**
 * 绝对倍率水平 —— 与「区间位置」彻底分离。
 * 这是纯 UI 风险标签，只按倍率数值本身定级，
 * 绝不因为它落在某个局部区间的高/低位就改变。
 * 它也不参与总信用需给结论（总判断由消化日数/発行済比/买残变化/四象限等共同决定）。
 */
function absoluteRatioLevel(ratio) {
  if (ratio == null || !isFinite(ratio)) return { level: 'unknown', label: '—', color: '#8b96ad', emoji: '' };
  if (ratio < 1)  return { level: 'short', label: '売り長',     color: '#38bdf8', emoji: '🔵' };
  if (ratio < 3)  return { level: 'ok',    label: '均衡～良好', color: '#22c55e', emoji: '🟢' };
  if (ratio < 5)  return { level: 'warn',  label: 'やや高め',   color: '#fbbf24', emoji: '🟡' };
  if (ratio < 10) return { level: 'high',  label: '高め',       color: '#f5a524', emoji: '🟠' };
  return              { level: 'vhigh', label: 'かなり高い', color: '#ff5a6e', emoji: '🔴' };
}

function stat(arr) {
  const xs = (arr || []).filter((x) => x != null && isFinite(x));
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  return {
    min: s[0], max: s[s.length - 1], last: xs[xs.length - 1],
    mean: +(xs.reduce((a, b) => a + b, 0) / xs.length).toFixed(2),
    median: s[Math.floor(s.length / 2)], n: xs.length,
  };
}

/** 变化率：末值 vs N 期前 */
function chg(rows, field, periods = 1) {
  if (!rows || rows.length <= periods) return null;
  const a = rows[rows.length - 1][field];
  const b = rows[rows.length - 1 - periods][field];
  if (a == null || b == null || b === 0) return null;
  return +(((a - b) / Math.abs(b)) * 100).toFixed(1);
}

/* =========================================================================
 * 1b. 统一比较窗口（v4 核心修正）
 * -------------------------------------------------------------------------
 * 修正前的严重缺陷：
 *   buyChg用 chg(rows,'buy',span) → 最近 N 期（如 5 期）
 *   priceChg 用 px[0].close → px[last].close → **整个 rows 的首尾**
 * 二者窗口不同，却在同一句判断里使用。
 * 实测反例（10 行）：最近 5 期股价 150→120（-20%）、买残 -20%，
 * 但整窗股价 100→120（+20%）→ priceChg 被算成 +20%，
 * 于是「股价涨 + 买残减」被判为 strong（🟢 強い/健全）——方向完全相反。
 *
 * 本函数是**唯一**的比较窗口定义处：
 *   - 起止都固定在信用残日期上（rows 的第 span 期与最后 1 期）
 *   - 股价/买残/卖残必须使用**同一对**起止日期
 *   - 首尾缺行情时**返回 null，绝不向内/向外扩大窗口**
 * ========================================================================= */
function comparisonWindow(rows, span) {
  const n = (rows && rows.length) ? rows.length : 0;
  // 单行（或空）时不存在可比较的区间 —— 返回 null，绝不构造 from > to 的假窗口
  if (n < 2) return null;
  // span 的语义 = **间隔数**（与 chg(rows,field,span) / 「近N期」文案完全一致）。
  // 窗口 = rows[n-1-span .. n-1]，共 span+1 行。
  // 例：10 行、span=5 → rows[4..9]，即「近5期」= 5 个间隔。
  // 这样 priceChg 与 buyChg 的起点严格相同（同一个 rows[fromIdx]）。
  const s = Math.max(1, Math.min(span || 1, n - 1));
  const fromIdx = n - 1 - s;
  const toIdx = n - 1;
  const from = rows[fromIdx], to = rows[toIdx];
  return {
    from: from.date,
    to: to.date,
    periods: s,
    rows: s + 1,
    fromIdx, toIdx,
    // 明确要求：两端都必须有价格，缺一端就不能给 priceChg
    fromPrice: from.close != null ? from.close : null,
    toPrice: to.close != null ? to.close : null,
    hasBothPrices: from.close != null && to.close != null,
  };
}

/** 按显式窗口两端算变化率 —— 与 comparisonWindow 严格同源 */
function chgInWindow(rows, field, win) {
  if (!win) return null;
  const a = rows[win.toIdx] ? rows[win.toIdx][field] : null;
  const b = rows[win.fromIdx] ? rows[win.fromIdx][field] : null;
  if (a == null || b == null || b === 0) return null;
  return +(((a - b) / Math.abs(b)) * 100).toFixed(1);
}

function ma(rows, field, n) {
  if (!rows || rows.length < n) return null;
  const xs = rows.slice(-n).map((r) => r[field]).filter((x) => x != null);
  if (xs.length < n) return null;
  return Math.round(xs.reduce((a, b) => a + b, 0) / n);
}

function slope(rows, field) {
  const pts = rows.map((r, i) => [i, r[field]]).filter((p) => p[1] != null);
  if (pts.length < 3) return null;
  const n = pts.length;
  const sx = pts.reduce((a, p) => a + p[0], 0);
  const sy = pts.reduce((a, p) => a + p[1], 0);
  const sxy = pts.reduce((a, p) => a + p[0] * p[1], 0);
  const sxx = pts.reduce((a, p) => a + p[0] * p[0], 0);
  const den = n * sxx - sx * sx;
  return den === 0 ? null : Math.round((n * sxy - sx * sy) / den);
}

// wan()（残量转万股）由页面主脚本提供。此处用独立命名，
// 避免内联进单文件时同名顶层 const 冲突导致整段脚本静默失效。
const toWan = (n) => (n == null ? '—' : (n / 10000).toFixed(1));

/* =========================================================================
 * 1c. 公司行动（拆并股）检测 —— fail-safe 设计
 * -------------------------------------------------------------------------
 * 问题：Yahoo 的 close/adjClose 已做**价格**复权，但 JPX 的買残/売残是
 * **实际股数**，没有做股数复权。1 拆 4 时买残 100万 → 400万（+300%），
 * 价格复权后却几乎不变 → 会被误判成 long-surge + short-build。
 *
 * 本轮**不猜拆股比例、也不自动修正数值**（那会引入新的错误来源）。
 * 采取 fail-safe：检测到比较窗口内有公司行动 → 标记 corporateActionAffected，
 * 由调用方暂停 buyChg/sellChg/quadrant/long-surge/short-build/hard trigger。
 *
 * 检测依据（按可靠性排序）：
 *   1. Yahoo chart API 的 events.splits（实测经代理可取到，285A 有 3:1 记录）
 *   2. 无法取得 events 时 → **不猜测**，但若残量变化与价格复权变化
 *      出现「典型拆股特征」（残量大幅跳变而价格未同步反向跳变），
 *      仍置 unknown（宁可 unknown，不输出虚假变化）。
 * ========================================================================= */

/** 从 Yahoo bars 里取 splitRatio / split 事件日期集合 */
function splitsFromBars(bars) {
  if (!bars || !bars.length) return { splits: [], known: false };
  // bars 若带 splits 元数据（engine.fetchYahooBars 会把 events.splits 挂上）
  const meta = bars.__splits;
  if (Array.isArray(meta)) {
    return { splits: meta, known: true };
  }
  return { splits: [], known: false };
}

/**
 * 判断比较窗口内是否发生公司行动。
 * @param cmp      comparisonWindow 的结果
 * @param barsMeta {splits:[{date,ratio}], known:boolean}
 * @param rows     用于 fail-safe 特征判断
 */
function detectCorporateAction(cmp, barsMeta, rows) {
  const out = {
    affected: false,
    status: 'none',      // 'none' | 'confirmed' | 'suspected'
    reasons: [], splits: [], text: '', hits: [],
  };
  if (!cmp || !cmp.from || !cmp.to) return out;

  // ---- 路径 1：明确的公司行动事件（confirmed）----
  const sp = (barsMeta && barsMeta.splits) || [];
  for (const s of sp) {
    if (!s || !s.date) continue;
    if (s.date >= cmp.from && s.date <= cmp.to) {
      out.affected = true;
      out.status = 'confirmed';
      out.splits.push(s);
      out.reasons.push(`${s.date} ${s.ratio || ''}`.trim());
      out.hits.push({ kind: 'event', date: s.date, ratio: s.ratio || null });
    }
  }

  /* ---- 路径 2：events 未知 + 残量/价格背离特征 → suspected ----
     ★ 必须遍历比较窗口内的**所有相邻 row pair**。
       旧实现只比较最后两行（rows[n-1] / rows[n-2]），
       若拆股发生在窗口中段（例如 day2），而最后两行完全正常，
       就会漏检 → 虚假方向判定被放行。
     阈值沿用不变：buyJump > 0.5 且 |priceMove| < 0.2。 */
  if (!out.affected && barsMeta && barsMeta.known === false && rows) {
    // 只在比较窗口覆盖的行范围内扫描
    const iFrom = (cmp.fromIdx != null) ? cmp.fromIdx : 0;
    const iTo = (cmp.toIdx != null) ? cmp.toIdx : rows.length - 1;

    for (let i = iFrom + 1; i <= iTo; i++) {
      const a = rows[i], b = rows[i - 1];
      if (!a || !b || a.buy == null || b.buy == null || b.buy <= 0) continue;

      const buyJump = Math.abs((a.buy - b.buy) / b.buy);
      if (buyJump <= 0.5) continue;                    // 阈值：> 50%

      // 价格用同期复权价（引擎里 a.close 就是 adjClose 对齐结果）。
      // 缺任一端价格 → 该 pair 不参与判定（不猜）。
      const pxA = (a.close != null) ? a.close : null;
      const pxB = (b.close != null) ? b.close : null;
      const pxMove = (pxA != null && pxB != null && pxB !== 0)
        ? Math.abs((pxA - pxB) / pxB) : null;
      if (pxMove == null || pxMove >= 0.2) continue;  // 阈值：< 20%

      out.affected = true;
      out.status = 'suspected';
      out.reasons.push(
        `${b.date}→${a.date} 残量跳变 ${(buyJump * 100).toFixed(0)}% 而股价仅变动 ${(pxMove * 100).toFixed(1)}%`);
      out.hits.push({
        kind: 'heuristic', from: b.date, to: a.date,
        buyJump: +(buyJump * 100).toFixed(1), priceMove: +(pxMove * 100).toFixed(1),
      });
      break;                                          // 任意一对命中即停止
    }
  }

  if (out.affected) {
    // confirmed 与 suspected 使用不同措辞 —— 前者是既定事实，后者只是可能
    out.text = out.status === 'confirmed'
      ? '株式分割・併合を検出。信用残変化の方向判定を停止'
      : '株式分割・併合の可能性があります。信用残変化の方向判定を停止';
    if (out.reasons.length) out.text += '（' + out.reasons.join('、') + '）';
  }
  return out;
}

/* =========================================================================
 * 1d. 信用倍率拆解：倍率高到底因为什么？
 * -------------------------------------------------------------------------
 * 倍率 = 買残 ÷ 売残。卖残极少时倍率会失真。
 * 通过「買残/売残各自变化」判断主因，并给出可读结论。
 * ========================================================================= */
function decomposeRatio(rows, span) {
  const last = rows[rows.length - 1];
  const ratio = last.ratio;
  const buyChg = chg(rows, 'buy', span);
  const sellChg = chg(rows, 'sell', span);

  const buys = rows.map(r => r.buy).filter(x => x != null);
  const sells = rows.map(r => r.sell).filter(x => x != null);
  const buyHigh = buys.length > 1 ? pctPos(last.buy, Math.min(...buys), Math.max(...buys)) : null;
  const sellLow = sells.length > 1 ? pctPos(last.sell, Math.min(...sells), Math.max(...sells)) : null;

  const out = {
    ratio: ratio, cause: 'unknown', causeText: '', level: 'info',
    buyChg, sellChg, buyHigh, sellLow,
    buyListed: last.buyListed ?? null,
  };
  if (ratio == null) { out.causeText = '数据不足'; return out; }

  const buyUp   = buyChg != null && buyChg >  3;
  const sellUp   = sellChg != null && sellChg >  3;
  const sellDown = sellChg != null && sellChg < -3;
  const sellVeryLow = sellLow != null && sellLow <= 20;

  const pc = (v) => (v == null ? '—' : (v > 0 ? '+' : '') + v + '%');

  if (buyUp && sellDown) {                    // Case C 双因
    out.cause = 'both'; out.level = 'warn';
    out.causeText = `近${span}期为「買残増加（${pc(buyChg)}）＋売残減少（${pc(sellChg)}）」的复合要因。`;
  } else if (buyUp) {                         // Case A 买残堆积
    out.cause = 'longBuild'; out.level = (buyChg >= 20) ? 'warn' : 'info';
    out.causeText = `倍率主要来自「信用買い残の積み上がり」（買残 ${pc(buyChg)}）。`;
  } else if (sellVeryLow || (sellDown && !sellUp)) {   // Case B 卖残极少
    out.cause = 'shortTiny'; out.level = 'info';
    out.causeText = `倍率主要来自「売残が極めて少ない」ため高く見えています（売残 ${pc(sellChg)}、区間位置${sellLow != null ? Math.round(sellLow) + '%' : '—'}）。`;
  } else {
    out.cause = 'mixed';
    out.causeText = `倍率の変化は緩やか（買残 ${pc(buyChg)}、売残 ${pc(sellChg)}）。`;
  }
  return out;
}

/* =========================================================================
 * 2. 株価 × 信用買残 四象限（v3 核心修正）
 * ========================================================================= */
function quadrant(rows, span, ctx) {
  const cmp = comparisonWindow(rows, span);

  // ★ 统一窗口：股价与买残用同一对起止日期
  //   首尾缺价格 → priceChg = null（绝不缩小/扩大窗口去找别的日子）
  let priceChg = null;
  if (cmp && cmp.hasBothPrices && cmp.fromPrice) {
    priceChg = +(((cmp.toPrice - cmp.fromPrice) / cmp.fromPrice) * 100).toFixed(1);
  }
  const buyChg = cmp ? chgInWindow(rows, 'buy', cmp) : null;
  const sellChg = cmp ? chgInWindow(rows, 'sell', cmp) : null;

  // ★ 公司行动（拆并股）影响 → 方向判定一律停止
  const ca = (ctx && ctx.corporateAction) || null;
  const base = {
    comparison: cmp,
    priceChg, buyChg, sellChg,
    corporateActionAffected: !!(ca && ca.affected),
    corporateActionStatus: (ca && ca.status) || 'none',
    corporateAction: ca || { affected: false, status: 'none' },
  };

  if (ca && ca.affected) {
    return Object.assign({}, base, {
      key: 'corporateAction', label: '— 会社行為の影響', level: 'unknown',
      color: '#b06cff',
      text: (ca.text || '比較期間に株式分割・併合の影響があります。') +
            ' 信用残変化の方向判定を停止しています。',
    });
  }

  if (buyChg == null || priceChg == null) {
    return Object.assign({}, base, {
      key: 'unknown', label: '判定不能', level: 'unknown', color: '#8b96ad',
      text: (priceChg == null)
        ? '比較窓の始点または終点に株価データがないため、方向を判定できません。'
        : '株価または信用残の推移が短く、四象限を判定できません。',
    });
  }

  const PD = 0.5, BD = 1.0;   // 判定阈值（接近噪音的变化不算方向）
  const pUp = priceChg > PD,  pDn = priceChg < -PD,  pFlat = !pUp && !pDn;
  const bUp = buyChg > BD,    bDn = buyChg < -BD,    bFlat = !bUp && !bDn;

  if (pUp && bDn) {
    return Object.assign({}, base, {
      key: 'strong', label: '🟢 強い / 健全', level: 'ok', color: '#22c55e',
      text: '株価上昇中に信用買い残が減少。需給の改善であり、健全な調整。' });
  }
  if (pUp && bUp) {
    return Object.assign({}, base, {
      key: 'chasing', label: '🟡 注意', level: 'warn', color: '#f5a524',
      text: '上昇局面で信用買いが積み上がっています。上昇に伴う追高は、後の反転に注意。' });
  }
  if (pDn && bDn) {
    // level=neutral：去杠杆 = 信用整理，方向上偏建设性（见 q-deleverage 的正向含义）。
    // 此前标成 warn 会让「信用整理」看起来像风险信号，与事实相反。
    return Object.assign({}, base, {
      key: 'deleverage', label: '🟡 去杠杆', level: 'neutral', color: '#f5a524',
      text: '株価下落と同時に信用整理が進行。売り圧の消化が進展。' });
  }
  if (pDn && bUp) {
    return Object.assign({}, base, {
      key: 'accumulateDown', label: '🔴 下落中の買い残増加', level: 'alert', color: '#ff5a6e',
      text: '下落局面で信用買いが増加。ナンピン・信用買い積み上がりの可能性があり、短期需給は悪化。' });
  }
  // ---- 显式 neutral zone：股价横ばい时退化为「只看买残方向」 ----
  // 之前这里统一落 flat（label「两者都横ばい」），会把「股价横盘 + 买残暴增」
  // 误描述成「两者都横ばい」，丢失买残在动的信息。
  if (pFlat && bUp) {
    return Object.assign({}, base, {
      key: 'marginBuildFlat', label: '🟡 買残増加（株価横ばい）', level: 'warn', color: '#f5a524',
      text: '株価は横ばいだが信用買い残が増加。方向は未定だが、杠杆买盘在積み上がり。' });
  }
  if (pFlat && bDn) {
    return Object.assign({}, base, {
      key: 'marginDeclineFlat', label: '🟢 買残減少（株価横ばい）', level: 'ok', color: '#22c55e',
             text: '株価は横ばいで信用買い残が減少。筹码在温和消化。' });
  }
  if (bFlat && (pUp || pDn)) {
    // 股价动但买残几乎不变：方向由股价自身决定，信用面中性
    return Object.assign({}, base, {
      key: 'priceMoveOnly', label: '— 買残横ばい', level: 'info', color: '#8b96ad',
             text: '株価は' + (pUp ? '上昇' : '下落') + 'したが、信用買い残は横ばい。信用面の変化は小さい。' });
  }
  return Object.assign({}, base, {
    key: 'flat', label: '— 方向性弱', level: 'info', color: '#8b96ad',
    text: '株価・信用残ともに横ばい。方向性の読み取り材料が不足。' });
}

/* =========================================================================
 * 3. 買残消化日数 = 信用買残 ÷ N日平均成交量
 * -------------------------------------------------------------------------
 * 这才是「买残到底重不重」的核心尺度。
 * 信用倍率在卖残极少时失真，但消化日数不会。
 * ========================================================================= */
/**
 * 買残消化日数 = 信用買残 ÷ N日平均出来高
 * ==================================================================
 * 纯函数签名（不接收 marginRows，天然不会被其期数污染）：
 *
 *   digestDays({ marginBuy, priceBarsAll, lastMarginDate })
 *
 * 参数
 *   marginBuy      最新信用買残（股）
 *   priceBarsAll   Yahoo 完整日线 [{date, close, vol}, ...]（约 485 期）
 *   lastMarginDate 信用残最新日，作为成交量窗口的**右边界**
 *
 * 返回
 *   { days, avgVolume, period, samples, fallback,
 *     days5, avgVolume5, winFrom, winTo, cutoff, level, label, note }
 *
 * 降级规则
 *   >= 20 条有效成交量 → 20 日主口径，fallback=false
 *   5 ~ 19 条         → 降级 5 日，  fallback=true（UI 必须标注）
 *   < 5 条            → 不计算（days=null）
 *
 * 硬约束
 *   · lastMarginDate 之后的行情绝不参与（避免未来数据）
 *   · 取「最后 N 个有效交易日」，不写死日期区间（节假日安全）
 *   · days5 同样取自 priceBarsAll —— 定义是「市场最近 5 个交易日」，
 *     而非「恰好有信用残数据的那几天」
 *   · aligned（同期对齐）数据完全不参与本函数，它只服务四象限判断
 */
function digestDays(opts) {
  const o = opts || {};
  const marginBuy = o.marginBuy;
  const barsAll = o.priceBarsAll || [];
  const cutoff = o.lastMarginDate || (barsAll.length ? barsAll[barsAll.length - 1].date : null);

  const empty = {
    days: null, avgVolume: null, period: null, samples: 0, fallback: false,
    days5: null, avgVolume5: null, level: 'na', label: '—',
    note: '行情データに有効な出来高がありません', cutoff: cutoff,
  };
  if (marginBuy == null || marginBuy <= 0 || !barsAll.length || !cutoff) return empty;

  // 有效成交量序列：date <= cutoff 且 vol > 0，按日期升序
  const valid = barsAll
    .filter(function (b) { return b.date <= cutoff && b.vol != null && b.vol > 0; })
    .sort(function (a, b) { return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0); });

  if (valid.length < 5) {
    return Object.assign({}, empty, {
      samples: valid.length,
      note: '有効出来高が ' + valid.length + ' 件のみ（5 未満のため算出不可）',
    });
  }

  const avgOf = (n) => {
    if (valid.length < n) return null;
    const w = valid.slice(-n);                 // 最后 n 个有效交易日
    return {
      avg: Math.round(w.reduce(function (a, b) { return a + b.vol; }, 0) / w.length),
      n: w.length,
      from: w[0].date,
      to: w[w.length - 1].date,
    };
  };

  const r20 = avgOf(20);
  const r5 = avgOf(5);

  // 主口径：够 20 用 20；5~19 降级 5 并标记 fallback
  const use = r20 ? { r: r20, period: 20, fallback: false }
                  : { r: r5, period: 5, fallback: true };

  const days = +(marginBuy / use.r.avg).toFixed(2);
  const days5 = r5 ? +(marginBuy / r5.avg).toFixed(2) : null;

  let level, label;
  if (days < 0.5)      { level = 'ok';    label = '軽'; }
  else if (days < 1)   { level = 'ok';    label = '正常'; }
  else if (days < 3)   { level = 'warn';  label = '注意'; }
  else if (days < 5)   { level = 'alert'; label = '偏重'; }
  else                 { level = 'alert'; label = '混雑'; }

  return {
    days: days,
    avgVolume: use.r.avg,
    period: use.period,
    samples: use.r.n,
    fallback: use.fallback,
    days5: days5,
    avgVolume5: r5 ? r5.avg : null,
    // 窗口信息（可追溯）
    winFrom: use.r.from,
    winTo: use.r.to,
    cutoff: cutoff,
    totalValid: valid.length,
    level: level,
    label: label,
    note: use.fallback
      ? '有効出来高 ' + valid.length + ' 件（20 未満）のため ' + use.period +
        ' 日均量に降格。信用需給の判定には 20 日基準を推奨します。'
      : null,
  };
}/* =========================================================================
 * 4. 主指标计算
 * ========================================================================= */
function computeIndicators(rows, val, ctx) {
  const last = rows[rows.length - 1] || {};
  const R = {};
  const ctx2 = ctx || {};

  // 样本自适应周期（日次数据当前仅 5~6 期）
  const span = Math.max(1, Math.min(5, rows.length - 1));
  R.span = span;
  R.dataRange = {
    n: rows.length,
    from: rows[0] ? rows[0].date : null,
    to: rows[rows.length - 1] ? rows[rows.length - 1].date : null,
    has13w: false,      // 由周次数据注入后置为 true
  };

  /* 0. 统一比较窗口 + 公司行动检测（v4）
     顺序很重要：先定窗口 → 再检测该窗口内是否有拆并股 →
     若有，则 chgN/四象限一律置 null（fail-safe，绝不输出虚假变化）。 */
  const cmp = comparisonWindow(rows, span);
  R.comparison = cmp;

  const caBars = ctx2.barsMeta || splitsFromBars(ctx2.priceBarsAll);
  const ca = detectCorporateAction(cmp, caBars, rows);
  R.corporateAction = ca;
  const caAffected = !!ca.affected;
  R.corporateActionAffected = caAffected;
  R.corporateActionStatus = ca.status || 'none';

  /** 受公司行动影响时，一律返回 null（不猜、不修正） */
  const chgSafe = (field, periods) =>
    caAffected ? null : chg(rows, field, periods);

  /* 1. 信用倍率 + 拆解 */
  const ratios = rows.map(r => r.ratio).filter(x => x != null && x > 0);
  R.ratio = {
    value: last.ratio,
    min: ratios.length ? Math.min(...ratios) : null,
    max: ratios.length ? Math.max(...ratios) : null,
    mean: stat(ratios)?.mean ?? null,
    // 短样本时不输出中长期 percentile，改为标注窗口大小
    rangeWindow: ratios.length,
    pos: ratios.length > 1
      ? pctPos(last.ratio, Math.min(...ratios), Math.max(...ratios)) : null,
    chg1: chg(rows, 'ratio', 1),
    chgN: chg(rows, 'ratio', span),
    // 绝对倍率水平（与区间位置无关，纯 UI 标签）
    absoluteLevel: absoluteRatioLevel(last.ratio),
  };
  R.ratioDecomp = decomposeRatio(rows, span);

  /* 2/3. 買残 / 売残 */
  const ma5buy = ma(rows, 'buy', 5), ma20buy = ma(rows, 'buy', 20);
  const ma5sell = ma(rows, 'sell', 5), ma20sell = ma(rows, 'sell', 20);
  const maNbuy = ma(rows, 'buy', span), maNsell = ma(rows, 'sell', span);
  R.long = {
    value: last.buy,
    chg1: chgSafe('buy', 1),
    chgN: chgSafe('buy', span),
    chg13w: null,            // 无周次数据时保持 null，不伪造
    ma5: ma5buy, ma20: ma20buy, maN: maNbuy,
    aboveMa5: ma5buy != null ? last.buy > ma5buy : null,
    aboveMa20: ma20buy != null ? last.buy > ma20buy : null,
    aboveMaN: (maNbuy != null && last.buy != null) ? last.buy > maNbuy : null,
    listedRatio: last.buyListed ?? null,
    corporateActionAffected: caAffected,
  };
  R.short = {
    value: last.sell,
    chg1: chgSafe('sell', 1),
    chgN: chgSafe('sell', span),
    chg13w: null,
    ma5: ma5sell, ma20: ma20sell, maN: maNsell,
    aboveMa5: ma5sell != null ? last.sell > ma5sell : null,
    aboveMa20: ma20sell != null ? last.sell > ma20sell : null,
    aboveMaN: (maNsell != null && last.sell != null) ? last.sell > maNsell : null,
    listedRatio: last.sellListed ?? null,
    corporateActionAffected: caAffected,
  };

  /* 4. 变化率趋势 */
  R.trend = {
    buySlope: slope(rows, 'buy'),
    sellSlope: slope(rows, 'sell'),
    ratioSlope: slope(rows, 'ratio'),
    netFlow: (R.long.chgN != null && R.short.chgN != null)
      ? +(R.long.chgN - R.short.chgN).toFixed(1) : null,
    span,
  };

  /* 5. 買残/売残 対 発行済株式数
     口径说明（重要）：
     - JPX 的「上場比」字段（last.buyListed）分母是「上場株式数」，不是発行済株式数；
     - Ganan 提供的是「発行済株式数」（val.shares）。
     两者口径不同，不能混用。此处主口径 = 自己用 val.shares 算「買残 ÷ 発行済株式数」，
     JPX 上場比作为审计对比保留。拿不到発行済株式数时 fallback 到 JPX 上場比并如实标注。 */
  const shares = (val && val.shares > 0) ? val.shares : null;

  // 分母稳定性检测：JPX 上場比反推的「上場株式数」 vs Ganan 発行済株式数。
  // 若两者乖离 > 20%，说明某一来源可能有时点差（分割/增资/旧财报），
  // 此时「買残/発行済比」的绝对值不可完全信任，UI 需警示。
  let denomStable = null, denomNote = null, impliedListed = null, denomDiffPct = null;
  if (shares && last.buyListed != null && last.buyListed >= 1) {
    // 上場比 >= 1% 才反推；太小时（如 0.1%）四舍五入误差巨大，反推不可靠
    impliedListed = last.buy / (last.buyListed / 100);
    const diff = (impliedListed - shares) / shares * 100;
    denomDiffPct = +diff.toFixed(1);
    denomStable = Math.abs(diff) <= 20;
    if (!denomStable) {
      denomNote = `発行済株式数（${shares.toLocaleString()}）と JPX 上場比反推値（${Math.round(impliedListed).toLocaleString()}）が ${diff >= 0 ? '+' : ''}${diff.toFixed(0)}% 乖離。分割・増資・旧データの可能性があり、「買残/発行済比」の絶対値は参考値です。`;
    }
  } else if (shares && last.buyListed != null && last.buyListed < 1) {
    denomStable = null;
    denomNote = '上場比が 1% 未満のため、反推による分母安定性は判定できません。';
  }

  /* ---- denominator 完整结构（issue 5）----
     UI 与评分都从这里读，不再各自猜测分母是谁。
     confidence:
       'high'      分母 = Ganan 発行済株式数，且与 JPX 上場比交叉验证一致（乖离 ≤20%）
       'unverified'分母 = Ganan 発行済株式数，但**无法**交叉验证
                    （上場比 <1% 时四舍五入误差过大，或 JPX 未给上場比）
       'low'       分母与 JPX 上場比乖离 >20% —— 有明确证据表明分母不可信
       'na'        拿不到発行済株式数，只能用 JPX 上場比（分母=上場株式数）
     ★ 只有 'low'（有证据不可信）才禁止作为 hard trigger。
       'unverified' 只是「没机会验证」，不等于「已证明不可信」，
       若一并禁用会让绝大多数小盘股（上場比<1%）永远无法参与评级。*/
  const denomConfidence = !shares ? 'na'
    : (denomStable === true ? 'high'
      : (denomStable === false ? 'low' : 'unverified'));

  const denominator = {
    value: shares != null ? shares
          : (impliedListed ? Math.round(impliedListed) : null),
    type: shares != null ? '発行済株式数' : '上場株式数',
    date: last.date || null,
    source: shares ? 'Ganan 発行済株式数'
            : (impliedListed ? 'JPX 上場比から反推' : null),
    stable: denomStable,
    confidence: denomConfidence,
    diffPct: denomDiffPct,
    note: denomNote,
    /** 该分母能否作为评级 hard trigger 的依据。
     *  'low'（有证据不可信）与 'na'（分母根本不是发行済株式数，
     *  而是上場株式数，两者口径不同）都不得作为 hard trigger。 */
    usableAsHardTrigger: (denomConfidence === 'high' || denomConfidence === 'unverified'),
  };

  R.borrowRate = {
    // 分母（発行済株式数，来自 Ganan）
    shares: shares,
    sharesSource: shares ? 'Ganan 発行済株式数' : (last.buyListed != null ? 'JPX 上場比（分母=上場株式数）' : null),
    // 主口径：買残/売残 ÷ 発行済株式数
    buyListed: (shares && last.buy != null) ? +((last.buy / shares) * 100).toFixed(2)
             : (last.buyListed ?? null),
    sellListed: (shares && last.sell != null) ? +((last.sell / shares) * 100).toFixed(2)
              : (last.sellListed ?? null),
    // 审计：JPX 原始上場比（分母=上場株式数），供对比
    buyListedJpx: last.buyListed ?? null,
    sellListedJpx: last.sellListed ?? null,
    // 分母稳定性
    denomStable: denomStable,
    denomNote: denomNote,
    denominator: denominator,
    impliedListedShares: impliedListed ? Math.round(impliedListed) : null,
    daysToCover: null,
    note: shares ? null : '発行済株式数未取得，fallback 到 JPX 上場比（分母=上場株式数）',
  };

  /* 6. 買残消化日数（核心）
     只传 marginBuy / priceBarsAll / lastMarginDate 三个标量（数据源：完整行情 + 信用残最新日截断），
     不把 rows（仅 1~5 期）传入，避免污染成交量窗口。 */
  const _barsAll = ctx && ctx.priceBarsAll ? ctx.priceBarsAll
                : (val && val.bars) ? val.bars : null;
  const _lastMDate = (ctx && ctx.lastMarginDate) || (rows.length ? rows[rows.length-1].date : null);
  R.digest = digestDays({
    marginBuy: last.buy,
    priceBarsAll: _barsAll,
    lastMarginDate: _lastMDate,
  });

  /* 7. 株価 × 買残 四象限（核心）—— 传入公司行动检测结果 */
  R.quadrant = quadrant(rows, span, { corporateAction: ca });

  /* 估值类（不属于信用需给，单独模块） */
  const price = val && val.price;
  const mcap = val && val.mcap ? val.mcap * 1e6 : null;
  R.creditToMcap = {
    price: price ?? null, mcap,
    creditValue: (last.buy != null && price) ? last.buy * price : null,
    ratio: (mcap && last.buy != null && price)
      ? +((last.buy * price) / mcap * 100).toFixed(2) : null,
    note: mcap ? null : '站点未提供时価総額，无法计算',
  };
  R.per = { now: (val?.perFcst || val?.perResult) ?? null,
            result: val?.perResult ?? null, forecast: val?.perFcst ?? null };
  R.pbr = { now: (val?.pbrFcst || val?.pbrResult) ?? null,
            result: val?.pbrResult ?? null, forecast: val?.pbrFcst ?? null };
  R.dividend = { yield: val?.divYield ?? null, dps: val?.dps ?? null };

  return R;
}

/* =========================================================================
 * 5. 异常值检测
 * ========================================================================= */
/**
 * 异常值检测 —— 分为两类（P1-6）
 *   kind:'data'   データ異常：数据本身可疑（计算不一致、异常跳变、单位/日期错误）
 *   kind:'note'   分析上の注意：数据没错，但分析前提受限（样本不足、周次缺失等）
 * 正常时返回空数组，UI 显示「データ整合性OK」。
 */
function detectAnomalies(rows, ind, val) {
  const out = [];
  const last = rows[rows.length - 1] || {};
  const push = (kind, level, t, d) => out.push({ kind, level, title: t, detail: d });

  /* ========== A. データ異常 ========== */

  // A1 倍率计算不一致
  if (last.buy != null && last.sell != null && last.sell > 0 && last.ratio != null) {
    const calc = +(last.buy / last.sell).toFixed(2);
    if (Math.abs(calc - last.ratio) > 0.02) {
      push('data', 'err', '倍率の計算が一致しません',
        `表示値 ${last.ratio}倍 と 買残÷売残 = ${calc}倍 が異なります。数据源の整合性を確認してください。`);
    }
  }

  // A2 单位异常：残量级突变（与相邻期相差 2 个数量级）
  if (rows.length >= 2) {
    const prev = rows[rows.length - 2];
    for (const [k, label] of [['buy', '信用買残'], ['sell', '信用売残']]) {
      const a = last[k], b = prev[k];
      if (!a || !b) continue;
      const r = a / b;
      if (r > 100 || r < 0.01) {
        push('data', 'err', `${label}の単位異常疑い`,
          `${prev.date} ${b.toLocaleString()} → ${last.date} ${a.toLocaleString()}（${Math.round(r*100)}%）` +
          `。桁ずれ（株/口、1万株単位切替等）の可能性があります。`);
      }
    }
  }

  // A3 单日异常跳变（倍率）
  const r1 = ind.ratio.chg1;
  if (r1 != null && Math.abs(r1) >= 25) {
    push('data', 'warn', '信用倍率の単日急変',
      `前営業日比 ${r1 > 0 ? '+' : ''}${r1}%（${last.ratio}倍）。` +
      `大口取引や投信/大股东の異動が疑われます。`);
  }

  // A4 売残单日激增
  const s1 = ind.short.chg1;
  if (s1 != null && s1 >= 40) {
    push('data', 'warn', '信用売残の単日急増',
      `前営業日比 +${s1}%。空売り建倉の集中の可能性。`);
  }

  // A5 日期异常：日次数据里出现非递增或重复
  const seen = new Set();
  let dateIssue = null;
  for (let i = 0; i < rows.length; i++) {
    const d = rows[i].date;
    if (seen.has(d)) { dateIssue = `重複日付: ${d}`; break; }
    seen.add(d);
    if (i > 0 && d <= rows[i - 1].date) { dateIssue = `日付が逆行: ${rows[i-1].date} → ${d}`; break; }
  }
  if (dateIssue) push('data', 'err', '日付の整合性エラー', dateIssue);

  // A6 买卖残极度失衡（倍率 > 50 或 < 1）—— 数据可读性提示
  if (last.buy != null && last.sell != null && last.sell > 0) {
    const r = last.buy / last.sell;
    if (r >= 50 || r <= 0.5) {
      push('data', 'info', '倍率の極端値',
        `買残は売残の ${r.toFixed(1)} 倍（${last.ratio}倍）。` +
        `倍率だけでは需給の軽重は判断できません — 「買残消化日数」と「上場比」を確認してください。`);
    }
  }

  /* ========== B. 分析上の注意 ========== */

  // B1 样本数不足
  const n = rows.length;
  if (n < 20) {
    push('note', 'info', 'サンプル数が不足しています',
      `現在のデータ ${n} 期（JPX 方式和 2026-09-25 稼働分の日次データのみ）。` +
      `20日均量・中期の百分位・長期の高安値は算出できません。`);
  }

  // B2 13週データ未取得
  push('note', 'info', '13週データは未取得',
    'JPX の「銘柄別信用取引週末残高」は 2026-09-28 の制度変更以降、日次公表に統合され、' +
    '個票の週次実績がWebsite 上で公開されていません。' +
    'そのため 13週前比・26週/52週位置に真实的数値は表示せず「—」としています。');

  // B3 短期高安值（非中长期极值）
  if (n > 0 && n < 20) {
    const bmax = Math.max(...rows.map(r => r.buy || 0));
    const bmin = Math.min(...rows.map(r => r.buy || 0));
    if (last.buy != null && last.buy >= bmax) {
      push('note', 'info', `${n}営業日高値`,
        `現在の信用買残 ${toWan(last.buy)} 万株は直近 ${n} 営業日の最高値。` +
        `中长期の最高値ではありません。`);
    } else if (last.buy != null && last.buy <= bmin) {
      push('note', 'info', `${n}営業日安値`,
        `現在の信用買残 ${toWan(last.buy)} 万株は直近 ${n} 営業日の最安値。` +
        `中长期の最安値ではありません。`);
    }
  }

  // B4 BPS 未验证 → PBR 不可用
  if (ind && ind.borrowRate && !ind.borrowRate.bpsVerified) {
    push('note', 'info', 'PBR はデータ未検証',
      'BPS が财报実値（API 由来）を得ていないため、PBR 基準価格と PBR 区间は算出していません。' +
      '「現値 ÷ サイトPBR」からの逆算は時点ずれを含むため採用しません。');
  }

  return out;
}

/* =========================================================================
 * 6. 规则引擎 —— 方向与风险分离
 * -------------------------------------------------------------------------
 *  Risk（信用风险 0~100）：只回答「信用需給是否紧张」，不预测涨跌。
 *  Dir（方向 -100~+100）：回答多空倾向。
 *  「信用风险高」≠「股价一定跌」，故两者必须分开显示。
 *
 * ★ 作用域铁律（2026-10-06）
 * -------------------------------------------------------------------------
 * 信用需給评分**只允许**使用信用交易自身的指标：
 *   株価×信用買残方向 · 信用買残变化 · 信用売残变化 · 買残消化日数
 *   買残/発行済比 · 信用倍率 · 信用需給リスク
 *
 * PER / PBR / EPS / BPS / 配当 / 估值区间 属于「企業・バリュエーション」，
 * **一律不得进入 DIR_RULES 或 RISK_RULES**。
 * 反例（已修正）：曾有 `per-high w:12 zone:'dn'` —— 「PER 35倍，估值不便宜」
 * 会把信用筹码完全健康、但估值偏高的股票判成「短期信用需給：弱気」。
 * 这是概念混淆：估值高低不改变信用需給的好坏，两者必须完全解耦。
 * 保留在 VALUATION_SIGNALS 里仅供详细页展示，不参与任何评分。
 * ========================================================================= */

const RISK_RULES = [
  { id: 'digest-veryhigh', w: 30,
    test: (i) => i.digest.days != null && i.digest.days >= 5,
    why: (i) => `買残消化日数 ${i.digest.days} 日（≥5日），按 ${i.digest.period}日均成交量需${i.digest.days}日才能消化，仓位偏重。` },
  { id: 'digest-high', w: 18,
    test: (i) => i.digest.days != null && i.digest.days >= 3 && i.digest.days < 5,
    why: (i) => `買残消化日数 ${i.digest.days} 日（3~5日），仓位偏重。` },
  // 注意：这里**不再**有「digest < 1 日」的风险规则。
  // 曾有一条 `digest-normal w:10`（消化 <1 日），但它是在 RISK_RULES（纯加分）里
  // 给「仓位轻」加分，方向完全反了 —— 会让 0.3 日、0.68 日这种轻仓股票
  // 无辜 +10 风险分，系统性高估低仓位股票的风险。已删除。
  { id: 'down-accum', w: 28,
    test: (i) => i.quadrant.key === 'accumulateDown',
    why: (i) => `股价 ${i.quadrant.priceChg}% 与買残 ${i.quadrant.buyChg}% 反向，处于「下落中的信用買い增加」。` },
  { id: 'chasing', w: 16,
    test: (i) => i.quadrant.key === 'chasing',
    why: (i) => `股价 ${i.quadrant.priceChg}% 上涨同时買残 ${i.quadrant.buyChg}% 增加，上涨伴随杠杆买盘堆积。` },
  { id: 'long-surge', w: 20,
    // 拆并股期间 chgN 已被置 null（computeIndicators 的 chgSafe），此处再显式守卫一次
    test: (i) => !i.corporateActionAffected && i.long.chgN != null && i.long.chgN >= 25,
    why: (i) => `近${i.span}期買残激增 ${i.long.chgN}%，追高迹象明显。` },
  { id: 'short-build', w: 20,
    test: (i) => !i.corporateActionAffected && i.short.chgN != null && i.short.chgN >= 20 && i.short.aboveMaN === true,
    why: (i) => `近${i.span}期売残增加 ${i.short.chgN}% 并站上同周期均线，空头在建仓。` },
  { id: 'listed-high', w: 14,
    // ★ 分母可信度门禁（issue 5）：
    //   denomStable === false（发行済 vs JPX上場比乖离 >20%）时，
    //   buyListed 的绝对值不可信，**不得**作为风险加分项。
    //   否则 3905（乖离 +20%，buyListed 31.58%）这类会仅凭低可信分母被拉高。
    test: (i) => {
      const bl = i.borrowRate;
      if (!bl || bl.buyListed == null || bl.buyListed < 4) return false;
      const d = bl.denominator;
      return !!(d && d.usableAsHardTrigger);
    },
    why: (i) => `買残占発行済株式数 ${i.borrowRate.buyListed}%，融资仓位占公司总股本比例偏高。` },
  { id: 'listed-high-untrusted', w: 0, soft: true,
    // 同上条件但分母不可信 → 只作为**提示**记录，不加风险分
    test: (i) => {
      const bl = i.borrowRate;
      if (!bl || bl.buyListed == null || bl.buyListed < 4) return false;
      const d = bl.denominator;
      return d && !d.usableAsHardTrigger;
    },
    why: (i) => `買残/分母 = ${i.borrowRate.buyListed}%（分母可信度不足：${
      (i.borrowRate.denominator && i.borrowRate.denominator.confidence) || 'na'
    }）。参考値であり、加点には使用していません。` },
  // ⚠ 已删除 `deleveraging w:10`（2026-10-06）
  // 「股价下跌 + 買残下跌」= 信用整理/去杠杆，是杠杆资金在**退出**，
  // 属于建设性动作。把它加进 RISK_RULES（纯加分体系）方向完全反了：
  // 越是在去杠杆，风险分越高。方向侧的 q-deleverage（zone:'up'）保留，
  // 那里表达的是「改善」含义，位置正确。
];

const DIR_RULES = [
  { id: 'q-strong', w: 30, zone: 'up',
    test: (i) => i.quadrant.key === 'strong',
    why: (i) => `股价 ${i.quadrant.priceChg}% 上升同时買残 ${i.quadrant.buyChg}%，股价上涨而杠杆买盘在撤，属健康形态。` },
  { id: 'q-deleverage', w: 10, zone: 'up',
    test: (i) => i.quadrant.key === 'deleverage',
    why: (i) => `股价 ${i.quadrant.priceChg}% 回落同时買残 ${i.quadrant.buyChg}%，多为获利了结或被动整理。` },
  { id: 'q-accum', w: 30, zone: 'dn',
    test: (i) => i.quadrant.key === 'accumulateDown',
    why: (i) => `股价 ${i.quadrant.priceChg}% 下跌而買残 ${i.quadrant.buyChg}%，下跌中杠杆资金在接盘，短期偏空。` },
  { id: 'q-chase', w: 14, zone: 'dn',
    test: (i) => i.quadrant.key === 'chasing',
    why: (i) => `股价 ${i.quadrant.priceChg}% 上涨伴随買残 ${i.quadrant.buyChg}%，上涨质量存疑。` },
  /* ★ A1（2026-10-09）：「売残が極めて少ない」**不再**作为正向方向加分。
     ------------------------------------------------------------------
     原实现：`short-squeeze w:26 zone:'up'`，仅凭「倍率≥8 且倍率主因=売残极少」
     就给 +26 方向分。问题在于这两个条件**只依赖信用残 rows 自身** ——
     株価・出来高・発行済株式数全部缺失时依然会触发，于是出现
     「四象限=判定不能 / 消化日数=null / position=判定不能」的股票
     却拿到 dir=26 → 🟢 短期信用需給：改善（案例 A 已复现）。

     语义纠正：「売残が少ない」只能解释**倍率为什么偏高**（这个解释职责
     已由 decomposeRatio 的 cause='shortTiny' / causeText 承担），
     它不能推导出「軋空余力」「回补燃料」或「方向偏多」——
     信用売残并不等于市场全部空头头寸。

     现在改为 soft：保留为事实说明（详细页「この判定になった理由」可见），
     w=0，不进入 directionScore，也不得单独推动 creditVerdict 的 改善。*/
  { id: 'short-squeeze', w: 0, zone: 'up', soft: true,
    test: (i) => i.ratioDecomp.cause === 'shortTiny' && i.ratio.value != null && i.ratio.value >= 8,
    why: (i) => `倍率 ${i.ratio.value}倍 は「売残が極めて少ない」ため高く見えています。` +
                `売残の少なさは倍率の高さを説明する**事実**であり、` +
                `ショートカバー余地・方向の強気を示す根拠にはなりません` +
                `（方向スコアには加点していません）。` },
  // ⚠ 这里**刻意没有** per-high（PER）与 div-good（配当利回り）。
  // 两者都是「企業・バリュエーション」维度，混进信用需給评分会造成概念混淆：
  // 「信用筹码健康 + 估值偏高」不应该被表述为「短期需給弱気」。
  // 已移至 VALUATION_SIGNALS（仅展示，不计分）。
];

/* 估值侧信号 —— **不参与任何信用需給评分**，仅在详细页作为
   「企業・バリュエーション 与信用需給是两套独立判断」的说明材料展示。*/
const VALUATION_SIGNALS = [
  { id: 'per-high', label: 'PER 偏高',
    test: (i) => i.per.now != null && i.per.now >= 25,
    text: (i) => `PER ${i.per.now}倍 —— 属估值判断，与信用需給无关。` },
  { id: 'div-good', label: '配当利回り良好',
    test: (i) => i.dividend.yield != null && i.dividend.yield >= 3,
    text: (i) => `予想配当利回り ${i.dividend.yield}% —— 属估值判断，与信用需給无关。` },
];

function runRules(ind, val) {
  const rHits = [], dHits = [], rNotes = [], dNotes = [];
  for (const r of RISK_RULES) {
    let ok = false;
    try { ok = !!r.test(ind, val); } catch (e) { ok = false; }
    if (!ok) continue;
    const item = { ...r, whyText: r.why(ind, val) };
    // soft 规则（如分母不可信时的 listed-high）只记录提示，不参与加总
    if (r.soft) { rNotes.push(item); } else { rHits.push(item); }
  }
  for (const r of DIR_RULES) {
    let ok = false;
    try { ok = !!r.test(ind, val); } catch (e) { ok = false; }
    if (!ok) continue;
    const item = { ...r, whyText: r.why(ind, val) };
    // soft 方向规则（如「売残极少」）只作为事实说明记录，**不参与方向加总**
    if (r.soft) { dNotes.push(item); } else { dHits.push(item); }
  }

  /* 信用风险分 0~100，越高越紧张（只累加非 soft 规则） */
  const risk = Math.max(0, Math.min(100, rHits.reduce((a, h) => a + h.w, 0)));
  let riskLabel, riskColor;
  if (risk >= 70)      { riskLabel = '高い';riskColor = '#ff5a6e'; }
  else if (risk >= 45) { riskLabel = 'やや高い';   riskColor = '#f5a524'; }
  else if (risk >= 25) { riskLabel = '中程度';     riskColor = '#fbbf24'; }
  else                 { riskLabel = '低い';       riskColor = '#22c55e'; }

  /* 方向分 -100~+100，正为偏多 */
  const dir = Math.max(-100, Math.min(100,
    dHits.reduce((a, h) => a + (h.zone === 'up' ? h.w : -h.w), 0)));
  let dirLabel, dirColor;
  if (dir >= 30)      { dirLabel = '強気';    dirColor = '#22c55e'; }
  else if (dir >= 10) { dirLabel = 'やや強気'; dirColor = '#4ade80'; }
  else if (dir > -10) { dirLabel = '中立';    dirColor = '#8b96ad'; }
  else if (dir > -30) { dirLabel = 'やや弱気'; dirColor = '#fbbf24'; }
  else                { dirLabel = '弱気';    dirColor = '#ff5a6e'; }

  return {
    risk, riskLabel, riskColor, riskHits: rHits,
    riskNotes: rNotes,          // soft：仅提示，不计分
    dir, dirLabel, dirColor, dirHits: dHits,
    dirNotes: dNotes,           // ★ A1：soft 方向说明（売残极少等），不计入 dir
    quadrant: ind.quadrant,
  };
}

/* =========================================================================
 * 7. 信用需給 最終等级 —— 首页唯一展示的结论
 * -------------------------------------------------------------------------
 * 设计约束（用户 2026-10-06 明确要求）：
 *  · 首页**只给等级**，不暴露 0~100 内部分数（分数是模型参数，不是用户语言）
 *  · 等级只由信用需給自身指标决定，估值（PER/PBR/配当）不参与
 *  · 「信用ポジション」用 軽い/普通/重い 三档表达仓位轻重，同样不给数字
 *
 * 定级原则：红色只留给「绝对量真的重」或「下落中买残增加且仓位不轻」，
 * 否则会出现「🔴 悪化」却同时三个绿色 KPI 的自相矛盾。
 * ========================================================================= */
const GRADES = {
  good: { key: 'good', emoji: '🟢', label: '改善', color: '#22c55e', bg: 'rgba(34,197,94,.12)' },
  mid:  { key: 'mid',  emoji: '🟡', label: '中立', color: '#fbbf24', bg: 'rgba(251,191,36,.12)' },
  warn: { key: 'warn', emoji: '🟠', label: '注意', color: '#f5a524', bg: 'rgba(245,165,36,.12)' },
  bad:  { key: 'bad',  emoji: '🔴', label: '悪化', color: '#ff5a6e', bg: 'rgba(255,90,110,.12)' },
  // unknown 不是「第四档风险」，而是「判不出来」——
  // 数据不足或受公司行动影响时，必须显示它，绝不退化成绿灯。
  unknown: { key: 'unknown', emoji: '⚪', label: '判定不能', color: '#8b96ad', bg: 'rgba(139,150,173,.12)' },
};

/**
 * 信用ポジション（仓位轻重）—— 三档，不给数字
 * 以「買残消化日数」为主尺度（相对成交量），買残/発行済比 为辅助。
 */
function creditPosition(ind) {
  const dg = ind.digest, bl = ind.borrowRate;
  const denom = (bl && bl.denominator) || null;
  /* ★ A1（2026-10-09）：仓位轻重必须服从分母可信度契约。
     ------------------------------------------------------------------
     此前 creditPosition 无条件读取 bl.buyListed，是本文件里唯一一个
     **没有**做分母门禁的仓位判断点 —— RISK_RULES['listed-high'] 与
     creditVerdict 的 listedHeavy 都已经用 usableAsHardTrigger 守住了，
     唯独这里漏了，于是出现案例 B：
       消化日数 0.20 日（极轻） + 低可信分母算出的発行済比 10%
       → 分母不可信、grade 正确地停在 中立，但 position 却显示 重い。
     现在复用**同一个**契约（不新造第二套 confidence 规则）：
       confidence ∈ {high, unverified} → 可参与 hard 判定
       confidence ∈ {low, na}          → 只展示，不决定 重い/軽い */
  const denomTrusted = !!(denom && denom.usableAsHardTrigger);
  const d = dg && dg.days != null ? dg.days : null;
  const pRaw = bl && bl.buyListed != null ? bl.buyListed : null;
  const p = denomTrusted ? pRaw : null;          // 分母不可信 → 不参与判定
  const pExcluded = (pRaw != null && !denomTrusted);

  /* ★ fail-safe（issue 4）：
     原则「没有发现风险 ≠ 已证明仓位轻」。
     当**可靠的**尺度一个都拿不到时，**必须**返回 unknown，
     绝不能落到 else 分支输出绿色「軽い」。
     ★ A1 补充：分母不可信导致的 p=null 也算「没有可靠尺度」，
     不得拿被排除的估算值去凑一个 重い/軽い 出来。*/
  if (d == null && p == null) {
    const why = (d == null && pRaw == null)
      ? '消化日数・発行済比 いずれも算出できず'
      : '消化日数 算出できず、発行済比 は分母可信度不足のため判定に未使用';
    return { key: 'unknown', label: '判定不能', color: '#8b96ad',
             basis: why, unknown: true };
  }

  let key, label, color;
  if ((d != null && d >= 3) || (p != null && p >= 10))      { key='heavy'; label='重い';   color='#ff5a6e'; }
  else if ((d != null && d >= 1) || (p != null && p >= 3))  { key='normal';label='普通';   color='#f5a524'; }
  else                                                        { key='light'; label='軽い';   color='#22c55e'; }

  const basis = [];
  if (d != null) basis.push('消化日数 ' + d.toFixed(2) + '日');
  if (p != null) basis.push('発行済比 ' + p.toFixed(2) + '%');
  // 部分缺失要如实说明，不能让用户以为两个尺度都验证过
  if (d == null) basis.push('消化日数 不明');
  // 被分母门禁排除的估算值：展示出来，但明确标注「未用于判定」
  if (p == null && pExcluded) basis.push('発行済比 ' + pRaw.toFixed(2) + '%（分母可信度不足・判定に未使用）');
  else if (p == null) basis.push('発行済比 不明');
  return { key, label, color, basis: basis.join(' / '), unknown: false };
}

/**
 * 信用需給 最終等级
 * @param ind   computeIndicators 的结果
 * @param vr    runRules 的结果（只用 dir / risk 两个内部量做阈值判定，不对外展示）
 */
function creditVerdict(ind, vr) {
  const dir = vr.dir, risk = vr.risk;
  const dg = ind.digest, q = ind.quadrant;
  const bl = ind.borrowRate;

  // 绝对量是否真的重 —— 只看消化日数（仓位 vs 成交量）
  const absHeavy = (dg.days != null && dg.days >= 3);
  const absLight = (dg.days != null && dg.days < 1.5);
  // 追高型：仓位不重但短期买残暴涨
  const surging  = (ind.long.chgN != null && ind.long.chgN > 100) && absLight;

  /* ★ issue 5：分母可信度门禁。
     denomStable === false（发行済 vs JPX上場比 乖离 >20%）时，
     buyListed 的绝对值不可信 —— 不得作为 hard trigger 打出红色。*/
  const denom = bl.denominator || null;
  const denomTrusted = !!(denom && denom.usableAsHardTrigger);
  const listedHeavy = (bl.buyListed != null && bl.buyListed >= 10) && denomTrusted;

  /* ★ issue 3：比较窗口内有拆并股 → 一切方向/残量判断停止。
     这是一票否决（fail-safe）：宁可 unknown，不输出虚假变化。*/
  const caAffected = !!ind.corporateActionAffected;

  /* ★ A1（2026-10-09）：数据有效性门槛 —— 「证据不足」不得被包装成好消息。
     ------------------------------------------------------------------
     四象限（株価 × 信用買残方向）判不出来时，说明「方向」这件事根本没有
     可信输入，此时**不得**输出 🟢改善 或「方向スコア xx（需給の改善傾向）」
     这类肯定结论（案例 A 已复现：quadrant=unknown / digest=null /
     position=判定不能，却因历史遗留的 short-squeeze +26 拿到 dir=26 → 🟢改善）。

     ★ 这不是「unknown = 悪化」：
       风险侧的 hard 结论（消化日数过重 / 下落中買残増加 / 買残急騰 / リスク偏高）
       依然照常输出 —— 那些结论依赖的数据本身是可靠的，隐藏真实风险更糟。
       本门槛只拦「拿不出正面证据却给出正面结论」这一种情况。

     ★ 措辞遵循产品既有语义：证据不足 → 既有档位「判定不能」（GRADES.unknown）。*/
  const quadrantKnown = (q && q.key !== 'unknown' && q.key !== 'corporateAction');

  let g, reasonCode = '', reasonFacts = [];

  if (caAffected) {
    g = GRADES.unknown;
    reasonCode = 'corporateAction';
    reasonFacts = [
      (ind.corporateAction && ind.corporateAction.text) ||
        '比較期間に株式分割・併合の影響があります。',
      'buyChg / sellChg / 四象限 / long-surge / short-build はいずれも停止しています。',
    ];
  } else if (absHeavy) {
    g = GRADES.bad; reasonCode = 'absHeavy';
    reasonFacts = ['買残消化日数 ' + dg.days.toFixed(2) + '日（≥3日）＝ 絶対量が重い'];
  } else if (q.key === 'accumulateDown' && !absLight) {
    g = GRADES.bad; reasonCode = 'accumulateDown';
    reasonFacts = ['株価下落中に買残が増加（下落中の信用買い積み上がり）'];
  } else if (listedHeavy && q.key !== 'strong') {
    g = GRADES.bad; reasonCode = 'listedHeavy';
    reasonFacts = ['買残/発行済株式数 ' + bl.buyListed + '%（分母可信）'];
  } else if (surging || dir < -20 || risk >= 55) {
    g = GRADES.warn; reasonCode = 'caution';
    if (surging)   reasonFacts.push('短期買残の急騰（' + ind.long.chgN + '%）');
    /* ★ A4.3：reasonFacts 会被首页 Hero 直接渲染 —— **禁止**写入内部评分裸数字
       （方向スコア / 信用リスク 的 0~100 数值）。首页只做定性解释；
       数值本身保留在详细页「判定ロジック」折叠区的 verdictAudit（dir / risk）。*/
    if (dir < -20) reasonFacts.push('需給の方向が弱気優勢');
    if (risk >= 55) reasonFacts.push('信用需給リスクが偏高');
  } else if (!quadrantKnown) {
    /* 四象限判不出来 → 方向结论无从谈起。
       放在 bad / warn 之后：若已有可靠的负面 hard 依据，那些结论优先保留，
       不因为「方向判不出来」就把已确认的风险也一起抹成 判定不能。*/
    g = GRADES.unknown; reasonCode = 'insufficientData';
    reasonFacts = [
      '株価または出来高データが不足しているため、短期需給の方向は判定できません。',
      (q && q.text) || '四象限（株価 × 信用買残）を判定できません。',
    ];
    /* ★ 但「方向判不出来」不等于「什么都没发生」。
       已确认的可靠事实（買残急騰・消化日数等）必须继续透出，
       否则用户会以为「无数据」而漏掉真实存在的信号。
       注意：只透出**事实原文**，不改变等级，也不做方向性结论。*/
    if (vr && Array.isArray(vr.riskHits)) {
      vr.riskHits.forEach(function (h) { if (h.whyText) reasonFacts.push(h.whyText); });
    }
  } else if (quadrantKnown && dir >= 10 && risk < 45 && !absHeavy) {
    g = GRADES.good; reasonCode = 'improving';
    // 定性表述：此前此处写入 '方向スコア ' + dir 的裸数字，会泄漏到首页。
    reasonFacts.push('需給の方向が改善傾向');
  } else {
    g = GRADES.mid; reasonCode = 'neutral';
    // 同上：改为定性表述，裸分数仅在详细页审计区保留。
    reasonFacts.push('需給の方向・リスクともに中性圏');
  }

  // 分母不可信时补充说明（不改变等级，但必须让用户看到）
  if (!denomTrusted && bl.buyListed != null && bl.buyListed >= 4 && g.key !== 'unknown') {
    reasonFacts.push('※ 買残/分母 ' + bl.buyListed + '% は分母可信度不足のため格付けに未使用');
  }

  const suffix = (g.key === 'unknown')
    ? '（データ不足）'
    : absHeavy ? '（買残の消化に ' + dg.days.toFixed(1) + '日）'
    : surging ? '（買残の急騰）'
    : (risk >= 55 ? '（信用需給が偏重）' : '');

  return {
    grade: g.key,
    emoji: g.emoji,
    label: g.label,
    color: g.color,
    bg: g.bg,
    // 首页只展示这一行；suffix 用于说明「为什么是这个等级」的一句话提示
    badge: g.emoji + ' 短期信用需給：' + g.label,
    suffix: suffix,
    /* ★ issue 9：verdict 是唯一结论来源。
       UI 不得自行重推等级，buildWhy 只能引用 reasonFacts，
       否则会出现「徽章悪化 / 文案改善傾向」这类自相矛盾。*/
    reasonCode: reasonCode,
    reasonFacts: reasonFacts,
    /* ★ A4.3：内部评分**不再**经 reasonFacts 泄漏到首页。
       若将来有合法场景需要数值，只能从这里取（供详细页审计/调试），
       首页渲染路径严禁读取 dirScore / riskScore。*/
    dirScore: vr.dir,
    riskScore: vr.risk,
    comparison: ind.comparison || null,
    corporateActionAffected: caAffected,
    corporateActionStatus: ind.corporateActionStatus || 'none',
    denominatorConfidence: denom ? denom.confidence : 'na',
    position: creditPosition(ind),
  };
}

/* =========================================================================
 * 8. 内部评分的对外暴露面（详细页「判定ロジック」专用）
 * -------------------------------------------------------------------------
 * 首页不调用这里。仅当用户对结论有疑问、需要审计时才展开。
 * ========================================================================= */
function verdictAudit(ind, vr, val) {
  const rItems = vr.riskHits.map(function (h) {
    return { id: h.id, w: h.w, text: h.whyText };
  });
  const dItems = vr.dirHits.map(function (h) {
    return { id: h.id, w: (h.zone === 'up' ? h.w : -h.w), zone: h.zone, text: h.whyText };
  });
  const valHits = VALUATION_SIGNALS.filter(function (s) {
    try { return !!s.test(ind, val); } catch (e) { return false; }
  }).map(function (s) { return { id: s.id, label: s.label, text: s.text(ind) }; });

  const noteItems = (vr.riskNotes || []).map(function (h) {
    return { id: h.id, w: 0, text: h.whyText, soft: true };
  });
  /* ★ A1：方向侧 soft 说明（売残极少 等 —— 只解释事实，不给方向加分）*/
  const dirNoteItems = (vr.dirNotes || []).map(function (h) {
    return { id: h.id, w: 0, zone: h.zone, text: h.whyText, soft: true };
  });

  return {
    dir: vr.dir, dirLabel: vr.dirLabel, dirColor: vr.dirColor,
    risk: vr.risk, riskLabel: vr.riskLabel, riskColor: vr.riskColor,
    dirItems: dItems, riskItems: rItems,
    riskNotes: noteItems,          // soft：分母不可信等「仅提示不计分」的规则
    dirNotes: dirNoteItems,        // ★ A1：方向侧「仅说明不计分」的规则
    valuationExcluded: valHits,
    quadrant: ind.quadrant,
    comparison: ind.comparison || null,
    corporateActionAffected: !!ind.corporateActionAffected,
    corporateActionStatus: ind.corporateActionStatus || 'none',
    denominator: (ind.borrowRate && ind.borrowRate.denominator) || null,
  };
}

window.MA2 = {
  computeIndicators, detectAnomalies, runRules,
  decomposeRatio, quadrant, digestDays, absoluteRatioLevel,
  creditVerdict, creditPosition, verdictAudit,
  // v4 新增：统一比较窗口 / 公司行动检测
  comparisonWindow, chgInWindow, detectCorporateAction, splitsFromBars,
  stat, chg, ma, slope, sd, pctPos, toWan,
  RISK_RULES, DIR_RULES, VALUATION_SIGNALS, GRADES,
};
