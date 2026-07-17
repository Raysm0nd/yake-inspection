// ============================================================
// Dashboard view — 從 glass-defect-ui.html 萃取，改為消費
// detection_record schema (見 /api/records)
// ============================================================

const Dashboard = (() => {

// --- Constants ---
const GW = 1500, GH = 1850;  // 玻璃尺寸：Y2/Y1 短邊 1500，X1/X2 長邊 1850
const TYPES = [
  { id: 'yake', label: 'Yake（刮痕）', color: '#4f9ea3' },
];
const TC = Object.fromEntries(TYPES.map(t => [t.id, t.color]));

const EDGE_TO_SIDE = { top: 'Y2', bottom: 'Y1', left: 'X2', right: 'X1' };
const SIDE_TO_EDGE = { Y2: 'top', Y1: 'bottom', X2: 'left', X1: 'right' };

const SHORT_INTERVALS = [
  'Shot002_021', 'Shot022_041', 'Shot042_061',
  'Shot062_081', 'Shot082_101', 'Shot102_121',
];
const LONG_INTERVALS = SHORT_INTERVALS.concat([
  'Shot122_141', 'Shot142_161', 'Shot162_181', 'Shot182_185',
]);
function isShortSide(side) { return side === 'Y1' || side === 'Y2'; }
function intervalsForEdge(edge) {
  return isShortSide(EDGE_TO_SIDE[edge]) ? SHORT_INTERVALS : LONG_INTERVALS;
}

const INTERVAL_COLORS = [
  '#4f9ea3', '#fdab43', '#d163a7', '#e8af34',
  '#6daa45', '#bb653b', '#9a7ad8', '#52a8d8',
  '#ff7a5c', '#a5a000',
];
function intervalKey(edge, interval) { return edge + '|' + interval; }
function colorForKey(key) {
  const i = Array.from(selectedIntervals).indexOf(key);
  return i < 0 ? null : INTERVAL_COLORS[i % INTERVAL_COLORS.length];
}

// --- State ---
let defects = [];
let substrateIds = [];
let visible = new Set(['yake']);

let filterState = {
  dateFrom: '', dateTo: '',
  hourFrom: 0, hourTo: 23,
  substrate: 'all',
};

// 使用者是否手動改過日期範圍。false 時，每次載入都把日期範圍自動對齊
// 「DB 目前實際資料涵蓋範圍」，確保 Visual 剛存的資料一定落在範圍內、立即找得到。
let userTouchedDate = false;

let detailBarRects = [];
let selectedEdge = 'top';
let selectedIntervals = new Set();

// --- Schema mapping ---
// records (detection_record) → defects (內部繪圖結構)
function recordsToDefects(records) {
  const list = [];
  records.forEach((r, idx) => {
    if (!r.defect_status || r.defect_status === 'OK') return;  // 只畫 NG/GRAY

    const edge = SIDE_TO_EDGE[r.side];
    if (!edge) return;

    // 由 shot_label 取中點，沿邊方向的座標（shot_label 可能為 null，防呆跳過）
    const m = (r.shot_label || '').match(/Shot(\d+)_(\d+)/);
    if (!m) return;
    const shotMid = (parseInt(m[1]) + parseInt(m[2])) / 2;
    const isShort = isShortSide(r.side);
    const intervals = isShort ? SHORT_INTERVALS : LONG_INTERVALS;
    // 取此 interval 在所有 shot 範圍中的位置 (0..1)
    // 上限採 intervals 末端的區間終點（短邊=121, 長邊=185），才不會擠在上緣
    const lastEnd = parseInt(intervals[intervals.length - 1].match(/_(\d+)$/)[1]);
    const along = shotMid / lastEnd;

    // 邊緣帶內隨機 (0..70mm)
    const seedR = Math.sin(idx * 9301 + 49297) * 233280;
    const rand1 = Math.abs(seedR - Math.floor(seedR));
    const rand2 = Math.abs(Math.sin(idx * 12345) * 67890 % 1);
    const margin = rand1 * 70;

    let x, y;
    if (edge === 'top')    { x = along * GW; y = margin; }
    else if (edge === 'bottom') { x = along * GW; y = GH - margin; }
    else if (edge === 'left')   { y = along * GH; x = margin; }
    else                        { y = along * GH; x = GW - margin; }

    // 微擾沿邊位置
    const intervalSpan = isShort ? GW / 6 : GH / 10;
    if (edge === 'top' || edge === 'bottom') {
      x += (rand2 - 0.5) * intervalSpan * 0.6;
      x = Math.max(0, Math.min(GW, x));
    } else {
      y += (rand2 - 0.5) * intervalSpan * 0.6;
      y = Math.max(0, Math.min(GH, y));
    }

    list.push({
      id: r.id,
      type: r.defect_type || 'yake',
      edge, side: r.side,
      x: Math.round(x), y: Math.round(y),
      interval: r.shot_label,
      substrate: r.substrate_id,
      ts: new Date(r.inspect_ts).getTime(),
    });
  });
  return list;
}

// --- Data load ---
let usingMock = false;  // 後端撈不到資料時 fallback 假資料，畫面上必須明確標示

async function loadData({ resetDateFilter = true } = {}) {
  let records = [];
  try {
    const res = await fetch('/api/records?n=700');
    if (res.ok) {
      const j = await res.json();
      records = j.records || [];
    }
  } catch (e) {
    console.warn('Failed to fetch /api/records, falling back to client-side mock:', e);
  }
  usingMock = records.length === 0;
  if (usingMock) {
    records = generateClientMock(700);
  }
  const badge = document.getElementById('mock-indicator');
  if (badge) badge.style.display = usingMock ? '' : 'none';
  defects = recordsToDefects(records);

  // 收集出現過的 substrate_id 給 filter 用
  substrateIds = Array.from(new Set(records.map(r => r.substrate_id))).sort();

  // 日期範圍初始化規則：
  //  - 模擬資料：清空日期範圍（= 顯示全部），絕不可用假日期污染之後的真實查詢
  //    （舊 bug：DB 空時把範圍設成模擬的 2024 年，之後 Visual 存的真實資料被擋掉）
  //  - 真實資料：只要使用者沒手動改過日期，就把範圍對齊「DB 目前實際資料涵蓋範圍」，
  //    這樣 Visual 剛存的那筆一定落在範圍內、切回 Dashboard 立刻找得到
  const fd = document.getElementById('f-date-from');
  const td = document.getElementById('f-date-to');
  if (usingMock) {
    filterState.dateFrom = '';
    filterState.dateTo = '';
    if (fd) fd.value = '';
    if (td) td.value = '';
  } else if (resetDateFilter || !userTouchedDate) {
    const dates = records.map(r => r.date).filter(Boolean).sort();
    if (dates.length > 0) {
      filterState.dateFrom = formatDate(dates[0]);
      filterState.dateTo = formatDate(dates[dates.length - 1]);
      if (fd) fd.value = filterState.dateFrom;
      if (td) td.value = filterState.dateTo;
    }
  }

  updateHourHint().catch(e => console.warn('hour hint failed:', e));
}

async function updateHourHint() {
  const hint = document.getElementById('hour-available-hint');
  if (!hint) return;
  const df = filterState.dateFrom ? filterState.dateFrom.replaceAll('-', '') : '';
  const dt = filterState.dateTo   ? filterState.dateTo.replaceAll('-', '')   : '';
  const qs = new URLSearchParams();
  if (df) qs.set('date_from', df);
  if (dt) qs.set('date_to', dt);
  try {
    const res = await fetch('/api/hours-available?' + qs.toString());
    if (!res.ok) return;
    const j = await res.json();
    const present = new Set((j.hours || []).map(h => String(h)));
    if (present.size === 0) {
      hint.textContent = '（無資料）';
      hint.classList.remove('has-gap');
      return;
    }
    const missing = [];
    for (let i = 0; i < 24; i++) {
      const hh = String(i).padStart(2, '0');
      if (!present.has(hh)) missing.push(hh);
    }
    if (missing.length === 0) {
      hint.textContent = `全 24 時段皆有資料`;
      hint.classList.remove('has-gap');
    } else if (missing.length >= 20) {
      hint.textContent = `僅時段 ${Array.from(present).sort().join(',')} 有資料`;
      hint.classList.add('has-gap');
    } else {
      hint.textContent = `缺 ${missing.join(',')} 時`;
      hint.classList.add('has-gap');
    }
  } catch (_) {
    /* silent */
  }
}

function formatDate(yyyymmdd) {
  if (!yyyymmdd || yyyymmdd.length !== 8) return '';
  return `${yyyymmdd.slice(0,4)}-${yyyymmdd.slice(4,6)}-${yyyymmdd.slice(6,8)}`;
}

// 後端不可用時 fallback（保證 dashboard 開得起來）
function generateClientMock(n) {
  const sides = ['Y1', 'Y2', 'X1', 'X2'];
  const subs = ['6T5CABM','6T5CACM','6T5CADM','6T5CBBM','6T5CBCM',
                '6T5CCBM','6T5CDBM','6T5DABM','6T5DBBM','6T5DCBM'];
  const out = [];
  for (let i = 0; i < n; i++) {
    const side = sides[Math.floor(Math.random()*4)];
    const ints = isShortSide(side) ? SHORT_INTERVALS : LONG_INTERVALS;
    const shot_label = ints[Math.floor(Math.random()*ints.length)];
    const m = shot_label.match(/Shot(\d+)_(\d+)/);
    const sn = Math.floor(parseInt(m[1]) + Math.random()*(parseInt(m[2])-parseInt(m[1])));
    const day = 13 + Math.floor(Math.random()*30);
    const month = day > 30 ? 5 : 4;
    const d = day > 30 ? day - 30 : day;
    const date = `2024${String(month).padStart(2,'0')}${String(d).padStart(2,'0')}`;
    const hour = String(Math.floor(Math.random()*24)).padStart(2,'0');
    const ng = Math.random() < 0.4 ? 1 : 0;
    const gr = ng === 0 && Math.random() < 0.3 ? 1 : 0;
    out.push({
      id: i,
      inspect_ts: `${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}T${hour}:00:00`,
      date, hour,
      substrate_id: subs[Math.floor(Math.random()*subs.length)],
      side, image_layer: 'CenterImage',
      shot_number: sn, segment_index: Math.floor(Math.random()*20)+1,
      shot_label,
      defect_status: ng>0?'NG':(gr>0?'GRAY':'OK'),
      defect_type: 'yake',
      ng_count: ng, gray_count: gr,
    });
  }
  return out;
}

// --- Filters ---
function filteredDefects() {
  return defects.filter(d => {
    if (!visible.has(d.type)) return false;
    const dt = new Date(d.ts);
    const dayStr = `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
    if (filterState.dateFrom && dayStr < filterState.dateFrom) return false;
    if (filterState.dateTo && dayStr > filterState.dateTo) return false;
    const h = dt.getHours();
    if (h < filterState.hourFrom || h > filterState.hourTo) return false;
    if (filterState.substrate !== 'all' && d.substrate !== filterState.substrate) return false;
    return true;
  });
}

function populateSubstrateOptions() {
  const sel = document.getElementById('f-substrate');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="all">全部</option>' +
    substrateIds.map(s => `<option value="${s}">${s}</option>`).join('');
  sel.value = cur || 'all';
}

function readFilters() {
  filterState.dateFrom = document.getElementById('f-date-from').value;
  filterState.dateTo = document.getElementById('f-date-to').value;
  // 不能用 `+value || fallback`：輸入 0 是 falsy，會把 0 時誤當成沒填
  const hf = parseInt(document.getElementById('f-hour-from').value, 10);
  const ht = parseInt(document.getElementById('f-hour-to').value, 10);
  filterState.hourFrom = Number.isNaN(hf) ? 0 : hf;
  filterState.hourTo = Number.isNaN(ht) ? 23 : ht;
  filterState.substrate = document.getElementById('f-substrate').value;
}

function applyFilters() {
  readFilters();
  drawAll();
  renderDetail();
  updateKPIs();
  updateHourHint().catch(() => {});
}

function resetFilters() {
  // 重設為資料範圍，並解除「使用者手動改過日期」標記（讓後續 refresh 重新自動對齊）
  userTouchedDate = false;
  if (defects.length > 0) {
    const dates = defects.map(d => {
      const dt = new Date(d.ts);
      return `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
    }).sort();
    document.getElementById('f-date-from').value = dates[0];
    document.getElementById('f-date-to').value = dates[dates.length-1];
  }
  document.getElementById('f-hour-from').value = '0';
  document.getElementById('f-hour-to').value = '23';
  document.getElementById('f-substrate').value = 'all';
  applyFilters();
}

// --- Canvas refs ---
let gcv, gctx, tcv, tctx, bcv, bctx, lcv, lctx, rcv, rctx;
let dcv, dctx;

function initRefs() {
  gcv = document.getElementById('glass-cv'); gctx = gcv.getContext('2d');
  tcv = document.getElementById('cv-top'); tctx = tcv.getContext('2d');
  bcv = document.getElementById('cv-bottom'); bctx = bcv.getContext('2d');
  lcv = document.getElementById('cv-left'); lctx = lcv.getContext('2d');
  rcv = document.getElementById('cv-right'); rctx = rcv.getContext('2d');
}

function setCanvas(cv, ctx, w, h) {
  const dpr = window.devicePixelRatio || 1;
  cv.width = w * dpr; cv.height = h * dpr;
  cv.style.width = w + 'px'; cv.style.height = h + 'px';
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);
}

function resize() {
  const grid = document.getElementById('map-grid');
  const wrap = document.getElementById('glass-wrap');
  const sideW = 88, gap = 4, distH = 70;

  // 用 map-grid 的 parent（overview-card 內容區）當可用空間依據
  const container = grid.parentElement;
  const cardStyle = getComputedStyle(container);
  const padX = parseFloat(cardStyle.paddingLeft) + parseFloat(cardStyle.paddingRight);
  const padY = parseFloat(cardStyle.paddingTop) + parseFloat(cardStyle.paddingBottom);
  const header = container.querySelector('.card-header');
  const headerH = header ? header.getBoundingClientRect().height : 0;

  const availW = Math.max(1, container.clientWidth - padX - sideW * 2 - gap * 2);
  const availH = Math.max(1, container.clientHeight - padY - headerH - distH * 2 - gap * 2);

  // 兩個約束都顧，取比較嚴格的那個決定玻璃尺寸
  let glassW = availW;
  let glassH = Math.round(availW * GH / GW);
  if (glassH > availH) {
    glassH = availH;
    glassW = Math.round(availH * GW / GH);
  }
  glassW = Math.max(200, glassW);
  glassH = Math.max(200, glassH);

  // 讓 grid template 精準等於 glass 尺寸，避免側邊 dist-panel 與 glass 之間出現空隙
  grid.style.gridTemplateColumns = `${sideW}px ${glassW}px ${sideW}px`;
  grid.style.gridTemplateRows = `${distH}px ${glassH}px ${distH}px`;

  wrap.style.width = glassW + 'px';
  wrap.style.height = glassH + 'px';
  setCanvas(gcv, gctx, glassW, glassH);

  setCanvas(lcv, lctx, sideW, glassH);
  setCanvas(rcv, rctx, sideW, glassH);
  setCanvas(tcv, tctx, glassW, distH);
  setCanvas(bcv, bctx, glassW, distH);

  return { cw: glassW, ch: glassH, sideW, distH };
}

// --- Glass map ---
function drawGlass(cw, ch) {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  gctx.clearRect(0, 0, cw, ch);
  gctx.fillStyle = isDark ? '#1b1c19' : '#eef0f2';
  gctx.fillRect(0, 0, cw, ch);

  const mx = 65 / GW * cw, my = 65 / GH * ch;
  gctx.fillStyle = isDark ? 'rgba(79,158,163,0.05)' : 'rgba(1,105,111,0.05)';
  gctx.fillRect(0, 0, cw, my);
  gctx.fillRect(0, ch - my, cw, my);
  gctx.fillRect(0, 0, mx, ch);
  gctx.fillRect(cw - mx, 0, mx, ch);

  gctx.strokeStyle = isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.05)';
  gctx.lineWidth = 1;
  for (let x = 0; x <= GW; x += 100) {
    const sx = x / GW * cw;
    gctx.beginPath(); gctx.moveTo(sx, 0); gctx.lineTo(sx, ch); gctx.stroke();
  }
  for (let y = 0; y <= GH; y += 100) {
    const sy = y / GH * ch;
    gctx.beginPath(); gctx.moveTo(0, sy); gctx.lineTo(cw, sy); gctx.stroke();
  }

  gctx.fillStyle = isDark ? 'rgba(255,255,255,0.18)' : 'rgba(0,0,0,0.2)';
  gctx.font = '9px Satoshi,sans-serif'; gctx.textAlign = 'center';
  for (let x = 0; x <= GW; x += 200) gctx.fillText(x, x / GW * cw, ch - 4);
  gctx.textAlign = 'left';
  for (let y = 0; y <= GH; y += 200) gctx.fillText(y, 4, y / GH * ch - 2);
}

// --- Edge line charts ---
function intervalCounts(edge) {
  const intervals = intervalsForEdge(edge);
  const counts = new Array(intervals.length).fill(0);
  filteredDefects().forEach(d => {
    if (d.edge !== edge) return;
    const i = intervals.indexOf(d.interval);
    if (i >= 0) counts[i]++;
  });
  return { intervals, counts };
}

function drawDistLine(ctx, panelW, panelH, edge) {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  ctx.clearRect(0, 0, panelW, panelH);

  const isTop = edge === 'top';
  const PAD_L = 10, PAD_R = 10, PAD_T = isTop ? 8 : 20, PAD_B = isTop ? 20 : 8;
  const plotW = panelW - PAD_L - PAD_R;
  const plotH = panelH - PAD_T - PAD_B;
  const baseY = PAD_T + plotH;

  const { intervals, counts } = intervalCounts(edge);
  const N = intervals.length;
  const maxC = Math.max(...counts, 1);
  const color = TC.yake;

  for (let i = 0; i < N; i++) {
    const x0 = PAD_L + i / N * plotW;
    const w = plotW / N;
    const selCol = colorForKey(intervalKey(edge, intervals[i]));
    if (selCol) {
      ctx.fillStyle = selCol + '33';
      ctx.fillRect(x0, PAD_T, w, plotH);
    } else if (i % 2 === 0) {
      ctx.fillStyle = isDark ? 'rgba(255,255,255,0.025)' : 'rgba(0,0,0,0.025)';
      ctx.fillRect(x0, PAD_T, w, plotH);
    }
  }

  ctx.strokeStyle = isDark ? 'rgba(255,255,255,0.18)' : 'rgba(0,0,0,0.18)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(PAD_L, baseY); ctx.lineTo(PAD_L + plotW, baseY); ctx.stroke();

  for (let i = 0; i <= N; i++) {
    const fx = PAD_L + i / N * plotW;
    ctx.beginPath(); ctx.moveTo(fx, baseY - 3); ctx.lineTo(fx, baseY + 3); ctx.stroke();
  }

  ctx.beginPath();
  for (let i = 0; i < N; i++) {
    const fx = PAD_L + (i + 0.5) / N * plotW;
    const fy = baseY - (counts[i] / maxC) * plotH;
    if (i === 0) ctx.moveTo(fx, baseY);
    ctx.lineTo(fx, fy);
  }
  ctx.lineTo(PAD_L + (N - 0.5) / N * plotW, baseY);
  ctx.closePath();
  ctx.fillStyle = color + '33'; ctx.fill();

  ctx.beginPath();
  for (let i = 0; i < N; i++) {
    const fx = PAD_L + (i + 0.5) / N * plotW;
    const fy = baseY - (counts[i] / maxC) * plotH;
    if (i === 0) ctx.moveTo(fx, fy); else ctx.lineTo(fx, fy);
  }
  ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();

  for (let i = 0; i < N; i++) {
    const fx = PAD_L + (i + 0.5) / N * plotW;
    const fy = baseY - (counts[i] / maxC) * plotH;
    const selCol = colorForKey(intervalKey(edge, intervals[i]));
    if (selCol) {
      ctx.beginPath(); ctx.arc(fx, fy, 5, 0, Math.PI * 2);
      ctx.fillStyle = selCol; ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
    } else {
      ctx.beginPath(); ctx.arc(fx, fy, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = isDark ? '#161714' : '#f9f8f5'; ctx.fill();
      ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.stroke();
    }
  }

  ctx.fillStyle = isDark ? 'rgba(255,255,255,0.45)' : 'rgba(0,0,0,0.45)';
  ctx.font = '9px Satoshi,sans-serif';
  ctx.textAlign = 'center';
  const labelY = isTop ? baseY + 12 : PAD_T - 4;
  ctx.textBaseline = 'alphabetic';
  for (let i = 0; i < N; i++) {
    const fx = PAD_L + (i + 0.5) / N * plotW;
    ctx.fillText(String(i + 1), fx, labelY);
  }
}

function drawDistLineSide(ctx, panelW, panelH, edge) {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  ctx.clearRect(0, 0, panelW, panelH);
  const PAD_T = 24, PAD_B = 10, PAD_L = 14, PAD_R = 8;
  const plotW = panelW - PAD_L - PAD_R;
  const plotH = panelH - PAD_T - PAD_B;
  const baseX = PAD_L;
  const { intervals, counts } = intervalCounts(edge);
  const N = intervals.length;
  const maxC = Math.max(...counts, 1);
  const color = TC.yake;

  for (let i = 0; i < N; i++) {
    const y0 = PAD_T + i / N * plotH;
    const h = plotH / N;
    const selCol = colorForKey(intervalKey(edge, intervals[i]));
    if (selCol) {
      ctx.fillStyle = selCol + '33';
      ctx.fillRect(baseX, y0, plotW, h);
    } else if (i % 2 === 0) {
      ctx.fillStyle = isDark ? 'rgba(255,255,255,0.025)' : 'rgba(0,0,0,0.025)';
      ctx.fillRect(baseX, y0, plotW, h);
    }
  }

  ctx.strokeStyle = isDark ? 'rgba(255,255,255,0.18)' : 'rgba(0,0,0,0.18)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(baseX, PAD_T); ctx.lineTo(baseX, PAD_T + plotH); ctx.stroke();

  for (let i = 0; i <= N; i++) {
    const fy = PAD_T + i / N * plotH;
    ctx.beginPath(); ctx.moveTo(baseX - 3, fy); ctx.lineTo(baseX + 3, fy); ctx.stroke();
  }

  ctx.beginPath();
  ctx.moveTo(baseX, PAD_T);
  for (let i = 0; i < N; i++) {
    const fy = PAD_T + (i + 0.5) / N * plotH;
    const fx = baseX + (counts[i] / maxC) * plotW;
    ctx.lineTo(fx, fy);
  }
  ctx.lineTo(baseX, PAD_T + plotH);
  ctx.closePath();
  ctx.fillStyle = color + '33'; ctx.fill();

  ctx.beginPath();
  for (let i = 0; i < N; i++) {
    const fy = PAD_T + (i + 0.5) / N * plotH;
    const fx = baseX + (counts[i] / maxC) * plotW;
    if (i === 0) ctx.moveTo(fx, fy); else ctx.lineTo(fx, fy);
  }
  ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();

  for (let i = 0; i < N; i++) {
    const fy = PAD_T + (i + 0.5) / N * plotH;
    const fx = baseX + (counts[i] / maxC) * plotW;
    const selCol = colorForKey(intervalKey(edge, intervals[i]));
    if (selCol) {
      ctx.beginPath(); ctx.arc(fx, fy, 5, 0, Math.PI * 2);
      ctx.fillStyle = selCol; ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
    } else {
      ctx.beginPath(); ctx.arc(fx, fy, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = isDark ? '#161714' : '#f9f8f5'; ctx.fill();
      ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.stroke();
    }
  }

  ctx.fillStyle = isDark ? 'rgba(255,255,255,0.45)' : 'rgba(0,0,0,0.45)';
  ctx.font = '9px Satoshi,sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i < N; i++) {
    const fy = PAD_T + (i + 0.5) / N * plotH;
    ctx.fillText(String(i + 1), baseX - 5, fy);
  }
  ctx.textBaseline = 'alphabetic';
}

// --- Detail bar chart ---
function drawDetail() {
  if (!dcv) {
    dcv = document.getElementById('detail-cv');
    dctx = dcv.getContext('2d');
  }
  const wrap = dcv.parentElement;
  const W = wrap.clientWidth, H = wrap.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  dcv.width = W * dpr; dcv.height = H * dpr;
  dcv.style.width = W + 'px'; dcv.style.height = H + 'px';
  dctx.setTransform(1, 0, 0, 1, 0, 0);
  dctx.scale(dpr, dpr);

  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  dctx.clearRect(0, 0, W, H);

  document.getElementById('detail-edge-name').textContent = EDGE_TO_SIDE[selectedEdge];

  const PAD_L = 44, PAD_R = 14, PAD_T = 14, PAD_B = 46;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;
  const baseY = PAD_T + plotH;
  const { intervals, counts } = intervalCounts(selectedEdge);
  const N = intervals.length;
  const maxC = Math.max(...counts, 1);
  const color = TC.yake;
  const slotW = plotW / N;
  const barW = Math.min(slotW * 0.7, 56);
  const rects = [];

  dctx.strokeStyle = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.05)';
  dctx.fillStyle = isDark ? '#787970' : '#7a7974';
  dctx.font = '10px Satoshi,sans-serif';
  dctx.textAlign = 'right';
  dctx.textBaseline = 'middle';
  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const v = Math.round(maxC * i / yTicks);
    const fy = baseY - (i / yTicks) * plotH;
    dctx.beginPath(); dctx.moveTo(PAD_L, fy); dctx.lineTo(PAD_L + plotW, fy); dctx.stroke();
    dctx.fillText(String(v), PAD_L - 6, fy);
  }
  dctx.textBaseline = 'alphabetic';

  intervals.forEach((label, i) => {
    const c = counts[i];
    const h = (c / maxC) * plotH;
    const cx = PAD_L + (i + 0.5) * slotW;
    const bx = cx - barW / 2;
    const by = baseY - h;
    if (c > 0) {
      dctx.fillStyle = color + 'cc';
      dctx.fillRect(bx, by, barW, h);
      dctx.strokeStyle = color;
      dctx.lineWidth = 1;
      dctx.strokeRect(bx, by, barW, h);
      dctx.fillStyle = isDark ? 'rgba(255,255,255,0.8)' : 'rgba(0,0,0,0.75)';
      dctx.font = '10px Satoshi,sans-serif';
      dctx.textAlign = 'center';
      dctx.fillText(c, cx, by - 4);
    } else {
      dctx.fillStyle = isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)';
      dctx.fillRect(bx, baseY - 2, barW, 2);
    }
    rects.push({ x: bx, y: PAD_T, w: barW, h: plotH, interval: label, count: c });
  });

  dctx.fillStyle = isDark ? '#a8a89e' : '#5a5950';
  dctx.font = '10px Satoshi,sans-serif';
  intervals.forEach((label, i) => {
    const cx = PAD_L + (i + 0.5) * slotW;
    dctx.save();
    dctx.translate(cx, baseY + 8);
    dctx.rotate(-Math.PI / 4);
    dctx.textAlign = 'right';
    dctx.textBaseline = 'middle';
    dctx.fillText(label.replace('Shot', ''), 0, 0);
    dctx.restore();
  });

  detailBarRects = rects;
}

function drawAll() {
  const { cw, ch, sideW, distH } = resize();
  drawGlass(cw, ch);
  drawDistLine(tctx, parseInt(tcv.style.width), distH, 'top');
  drawDistLine(bctx, parseInt(bcv.style.width), distH, 'bottom');
  drawDistLineSide(lctx, sideW, ch, 'left');
  drawDistLineSide(rctx, sideW, ch, 'right');
  updateEdgeTotals();
}

function updateEdgeTotals() {
  const totals = ['top', 'right', 'bottom', 'left'].map(edge => ({
    edge,
    label: EDGE_TO_SIDE[edge],
    total: intervalCounts(edge).counts.reduce((a, b) => a + b, 0),
  }));

  const cv = document.getElementById('mini-edges');
  if (!cv) return;
  const wrap = cv.parentElement;
  const w = wrap.clientWidth, h = wrap.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  cv.width = w * dpr; cv.height = h * dpr;
  cv.style.width = w + 'px'; cv.style.height = h + 'px';
  const ctx = cv.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);

  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  const barColor = TC.yake || '#4f9ea3';
  const textCol = isDark ? 'rgba(255,255,255,0.7)' : 'rgba(0,0,0,0.7)';
  const mutedCol = isDark ? 'rgba(255,255,255,0.45)' : 'rgba(0,0,0,0.45)';
  ctx.clearRect(0, 0, w, h);

  const N = totals.length;
  const gap = 8;
  const barW = (w - gap * (N - 1)) / N;
  const labelH = 12;
  const valueH = 12;
  const barMaxH = h - labelH - valueH - 4;
  const maxV = Math.max(...totals.map(t => t.total), 1);

  ctx.font = '11px Satoshi,sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';

  totals.forEach((t, i) => {
    const x = i * (barW + gap);
    const bh = Math.max(2, (t.total / maxV) * barMaxH);
    const by = labelH + 2 + (barMaxH - bh);

    ctx.fillStyle = mutedCol;
    ctx.fillText(t.label, x + barW / 2, 0);

    ctx.fillStyle = barColor + 'cc';
    ctx.fillRect(x, by, barW, bh);

    ctx.fillStyle = textCol;
    ctx.fillText(String(t.total), x + barW / 2, labelH + 2 + barMaxH + 2);
  });
}

// --- Right card mode switch ---
function renderDetail() {
  const cwrap = document.getElementById('detail-canvas-wrap');
  const list = document.getElementById('detail-list');
  const clearBtn = document.getElementById('clear-intervals-btn');
  const title = document.getElementById('detail-card-title');
  const sub = document.getElementById('detail-card-sub');

  if (selectedIntervals.size === 0) {
    cwrap.style.display = '';
    list.style.display = 'none';
    clearBtn.style.display = 'none';
    title.innerHTML = `<b id="detail-edge-name">${EDGE_TO_SIDE[selectedEdge]}</b> 各 Shot 區間缺陷分布`;
    sub.textContent = '點擊左側折線圖中的區間（可多選），右側將列出該區間的基板 ID';
    drawDetail();
  } else {
    cwrap.style.display = 'none';
    list.style.display = 'flex';
    clearBtn.style.display = '';
    title.textContent = `已選 ${selectedIntervals.size} 個 Shot 區間 · 基板 ID 明細`;
    sub.textContent = '再次點擊相同區間可取消選取；不同顏色代表不同 Shot 區間';
    renderSubstrateList();
  }
}

function renderSubstrateList() {
  const list = document.getElementById('detail-list');
  const selected = Array.from(selectedIntervals).map((key, idx) => {
    const [edge, interval] = key.split('|');
    return { key, edge, interval, color: INTERVAL_COLORS[idx % INTERVAL_COLORS.length] };
  });

  const byId = {};
  for (const s of selected) {
    filteredDefects().forEach(d => {
      if (d.edge === s.edge && d.interval === s.interval) {
        if (!byId[d.substrate]) byId[d.substrate] = {};
        byId[d.substrate][s.key] = (byId[d.substrate][s.key] || 0) + 1;
      }
    });
  }
  const rows = Object.entries(byId).map(([sid, counts]) => ({
    sid, counts,
    total: Object.values(counts).reduce((a, b) => a + b, 0),
  })).sort((a, b) => b.total - a.total);

  const legend = selected.map(s => `
    <span class="interval-chip" style="--c:${s.color}">
      <span class="dot"></span>${EDGE_TO_SIDE[s.edge]} · ${s.interval.replace('Shot', '')}
    </span>
  `).join('');

  const header = `
    <div class="substrate-list-header">
      <span>基板 ID</span>
      <span>各區間筆數（依顏色）</span>
      <span style="text-align:right">總計</span>
    </div>`;

  const body = rows.length === 0
    ? '<div class="detail-list-empty">所選區間在目前篩選下沒有資料</div>'
    : rows.map(r => {
        const chips = selected.map(s => {
          const c = r.counts[s.key] || 0;
          if (c === 0) return `<span class="interval-marker off">·</span>`;
          return `<span class="interval-marker" style="--c:${s.color}">${c}</span>`;
        }).join('');
        return `
          <div class="substrate-row">
            <span class="substrate-id">${r.sid}</span>
            <span class="substrate-chips">${chips}</span>
            <span class="substrate-total">${r.total}</span>
          </div>`;
      }).join('');

  list.innerHTML = `
    <div class="detail-legend">
      <span class="detail-legend-label">已選區間：</span>${legend}
    </div>
    ${header}
    <div class="substrate-list">${body}</div>
  `;
}

// --- Interaction ---
function selectEdge(edge) {
  if (edge === selectedEdge) return;
  selectedEdge = edge;
  document.querySelectorAll('#view-dashboard .dist-panel').forEach(p => {
    p.classList.toggle('selected', p.dataset.edge === edge);
  });
  renderDetail();
}

function toggleInterval(edge, interval) {
  const key = intervalKey(edge, interval);
  if (selectedIntervals.has(key)) selectedIntervals.delete(key);
  else selectedIntervals.add(key);
  drawAll();
  renderDetail();
}

function clearIntervalSelection() {
  selectedIntervals.clear();
  drawAll();
  renderDetail();
}

function hitTestInterval(cv, edge, mx, my) {
  const intervals = intervalsForEdge(edge);
  const isHoriz = (edge === 'top' || edge === 'bottom');
  const W = cv.clientWidth, H = cv.clientHeight;
  if (isHoriz) {
    const PAD_L = 10, PAD_R = 10;
    const plotW = W - PAD_L - PAD_R;
    if (mx < PAD_L || mx > PAD_L + plotW) return -1;
    const i = Math.floor((mx - PAD_L) / (plotW / intervals.length));
    return Math.max(0, Math.min(intervals.length - 1, i));
  } else {
    const PAD_T = 24, PAD_B = 10;
    const plotH = H - PAD_T - PAD_B;
    if (my < PAD_T || my > PAD_T + plotH) return -1;
    const i = Math.floor((my - PAD_T) / (plotH / intervals.length));
    return Math.max(0, Math.min(intervals.length - 1, i));
  }
}

function bindShotClicks() {
  ['top', 'bottom', 'left', 'right'].forEach(edge => {
    const cv = document.getElementById('cv-' + edge);
    cv.style.cursor = 'pointer';
    cv.addEventListener('click', e => {
      const r = cv.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      selectEdge(edge);
      const i = hitTestInterval(cv, edge, mx, my);
      if (i >= 0) {
        const intervals = intervalsForEdge(edge);
        toggleInterval(edge, intervals[i]);
      } else {
        renderDetail();
      }
    });
  });

  const cv = document.getElementById('detail-cv');
  cv.addEventListener('click', e => {
    const r = cv.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const hit = detailBarRects.find(b => mx >= b.x && mx <= b.x + b.w && my >= b.y && my <= b.y + b.h);
    if (hit) toggleInterval(selectedEdge, hit.interval);
  });
}

// --- KPI / Charts ---
function updateKPIs() {
  const total = filteredDefects().length;
  animNum('kv-yake', total);
}
function animNum(id, target) {
  const el = document.getElementById(id);
  if (!el) return;
  const from = parseInt(el.textContent) || 0;
  const dur = 550, t0 = performance.now();
  const step = now => {
    const p = Math.min((now - t0) / dur, 1);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(from + (target - from) * e);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function toggleType(type, btn) {
  if (visible.has(type)) { visible.delete(type); btn.className = 'chip off'; }
  else { visible.add(type); btn.className = `chip on-${type}`; }
  drawAll(); renderDetail(); updateKPIs();
}

function bindFilterInputs() {
  // 日期輸入：使用者一動就標記 userTouchedDate，之後 refresh 不再覆寫他選的範圍
  ['f-date-from', 'f-date-to'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => { userTouchedDate = true; applyFilters(); });
  });
  ['f-hour-from', 'f-hour-to', 'f-substrate'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', applyFilters);
  });
}

// --- Boot ---
let booted = false;
async function boot() {
  if (booted) return;
  booted = true;
  initRefs();
  await loadData();
  populateSubstrateOptions();
  bindFilterInputs();
  bindShotClicks();
  rerender();
}

function rerender() {
  if (!booted) return;
  drawAll();
  renderDetail();
  updateKPIs();
}

// 每次切到 Dashboard 分頁時呼叫：重撈 DB（讓 Visual 剛存的資料即時出現），
// 但保留使用者當前的日期/時段/基板篩選；首次則走完整 boot。
async function refresh() {
  if (!booted) { await boot(); return; }
  await loadData({ resetDateFilter: false });
  populateSubstrateOptions();
  applyFilters();   // 依現有篩選重新計算並重畫
}

function onResize() {
  if (!booted) return;
  drawAll();
  renderDetail();
}

return {
  boot, refresh, rerender, onResize,
  resetFilters, toggleType, clearIntervalSelection,
};

})();

window.Dashboard = Dashboard;
