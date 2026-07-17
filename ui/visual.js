// ============================================================
// Visual view — 區塊瀏覽 + 即時白斑偵測 (透過 FastAPI 後端)
// ============================================================

const Visual = (() => {

const COLOR_MAP = {
  NG: '#f25a5a',
  GRAY: '#ffd23f',
  OK: '#6fdc8c',
};

const SLIDER_SPECS = [
  { key: 'threshold',     label: '白斑亮度閥值', min: 0, max: 255 },
  { key: 'len_ng',        label: 'NG 長度',      min: 1, max: 100 },
  { key: 'area_ng',       label: 'NG 面積',      min: 1, max: 500 },
  { key: 'len_gray_min',  label: 'GRAY 長度下限', min: 1, max: 100 },
  { key: 'area_gray_min', label: 'GRAY 面積下限', min: 1, max: 500 },
  { key: 'merge_dist',    label: '合併距離',     min: 0, max: 200 },
];

// State
let segments = [];        // [{path, filename, folder_label}]
let idx = 0;
let params = {
  threshold: 100, len_ng: 10, area_ng: 40,
  len_gray_min: 5, area_gray_min: 20, merge_dist: 30,
};
let defaultParams = { ...params };
let booted = false;
let lastDetect = null;
let inflight = false;
let rerunQueued = false;  // 偵測中又有新請求（切圖/調參）時排隊，完成後補跑最新狀態

// --- DOM refs ---
let elFolder, elFilter, elStatus;
let elImgCanvas, elMaskCanvas, elMaskImg;
let elPrev, elNext, elPickFolder, elPickFile, elReset, elSave, elSaveAll, elApply;
let elLoading;

function $(id) { return document.getElementById(id); }

function buildThresholdPanel() {
  const host = $('threshold-panel');
  host.innerHTML = SLIDER_SPECS.map(s => `
    <div class="slider-cell">
      <label>
        <span>${s.label}</span>
        <span class="val" data-val="${s.key}">${params[s.key]}</span>
      </label>
      <input type="range" min="${s.min}" max="${s.max}" step="1"
             value="${params[s.key]}" data-key="${s.key}">
    </div>
  `).join('');

  host.querySelectorAll('input[type="range"]').forEach(input => {
    input.addEventListener('input', e => {
      const key = e.target.dataset.key;
      params[key] = +e.target.value;
      const valEl = host.querySelector(`[data-val="${key}"]`);
      if (valEl) valEl.textContent = params[key];
    });
    input.addEventListener('change', () => {
      runDetect();
    });
  });
}

function refreshSliderValues() {
  SLIDER_SPECS.forEach(s => {
    const input = document.querySelector(`#threshold-panel input[data-key="${s.key}"]`);
    const val = document.querySelector(`#threshold-panel [data-val="${s.key}"]`);
    if (input) input.value = params[s.key];
    if (val) val.textContent = params[s.key];
  });
}

async function loadDefaultParams() {
  try {
    const res = await fetch('/api/default-params');
    if (res.ok) {
      const j = await res.json();
      Object.assign(params, j);
      defaultParams = { ...j };
      refreshSliderValues();
    }
  } catch (e) {
    console.warn('Failed to fetch default params:', e);
  }
}

async function loadDefaultFolder() {
  try {
    const res = await fetch('/api/default-folder');
    if (res.ok) {
      const j = await res.json();
      if (j.folder && elFolder && !elFolder.value) elFolder.value = j.folder;
    }
  } catch (e) {
    console.warn('Failed to fetch default folder:', e);
  }
}

async function loadSegments() {
  const folder = (elFolder.value || '').trim();
  if (!folder) { setStatus('請先輸入資料夾或單張圖檔路徑'); return; }
  const filter = elFilter.value || 'all';
  setStatus('掃描中…');
  try {
    const url = `/api/segments?folder=${encodeURIComponent(folder)}&filter=${encodeURIComponent(filter)}`;
    const res = await fetch(url);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const j = await res.json();
    segments = j.segments || [];
    idx = 0;
    if (segments.length === 0) {
      setStatus(`路徑下沒有符合的圖片：${folder}`);
      clearCanvas();
    } else {
      runDetect();
    }
  } catch (e) {
    setStatus(`載入失敗：${e.message}`);
    segments = []; clearCanvas();
  }
}

function setStatus(text) {
  if (!elStatus) return;
  elStatus.textContent = text || '';
}

function setStatusFromDetect(res) {
  if (!res) return setStatus('');
  const i = idx + 1, n = segments.length;
  const folderLabel = segments[idx]?.folder_label ?? '?';
  elStatus.innerHTML = `
    [${i}/${n}] 分類=<b>${folderLabel}</b>
    · 判定=<span class="status-${res.overall}">${res.overall}</span>
    · NG=${res.ng_count} GRAY=${res.gray_count}
    · <span style="font-family:var(--font-mono)">${res.filename}</span>
  `;
}

function go(delta) {
  if (segments.length === 0) return;
  idx = Math.max(0, Math.min(segments.length - 1, idx + delta));
  runDetect();
}

function showLoading(on) {
  if (!elLoading) return;
  elLoading.classList.toggle('show', on);
}

async function runDetect() {
  if (segments.length === 0) return;
  if (inflight) { rerunQueued = true; return; }  // 不丟棄：完成後會用最新 idx/params 補跑
  inflight = true;
  showLoading(true);
  const seg = segments[idx];
  try {
    const res = await fetch('/api/detect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ segment_path: seg.path, params }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    lastDetect = await res.json();
    renderResult(lastDetect);
    setStatusFromDetect(lastDetect);
  } catch (e) {
    setStatus(`偵測失敗：${e.message}`);
  } finally {
    inflight = false;
    showLoading(false);
    if (rerunQueued) {
      rerunQueued = false;
      runDetect();
    }
  }
}

function clearCanvas() {
  const c1 = $('cv-image'); if (c1) c1.getContext('2d').clearRect(0, 0, c1.width, c1.height);
  const c2 = $('cv-mask');  if (c2) c2.getContext('2d').clearRect(0, 0, c2.width, c2.height);
}

function renderResult(res) {
  drawImageWithBoxes(res);
  drawMask(res);
}

function fitToWrap(wrap, imgW, imgH) {
  const W = wrap.clientWidth, H = wrap.clientHeight;
  const scale = Math.min(W / imgW, H / imgH);
  return { w: Math.max(1, Math.floor(imgW * scale)), h: Math.max(1, Math.floor(imgH * scale)) };
}

function drawImageWithBoxes(res) {
  const cv = $('cv-image');
  const wrap = cv.parentElement;
  const { w, h } = fitToWrap(wrap, res.img_w, res.img_h);
  const dpr = window.devicePixelRatio || 1;
  cv.width = w * dpr; cv.height = h * dpr;
  cv.style.width = w + 'px'; cv.style.height = h + 'px';
  const ctx = cv.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);

  const img = new Image();
  img.onload = () => {
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(img, 0, 0, w, h);
    const sx = w / res.img_w, sy = h / res.img_h;
    res.bboxes.forEach(b => {
      if (b.status === 'OK') return;
      ctx.strokeStyle = COLOR_MAP[b.status];
      ctx.lineWidth = 1.5;
      ctx.strokeRect(b.x0 * sx, b.y0 * sy, b.w * sx, b.h * sy);
      ctx.fillStyle = COLOR_MAP[b.status];
      ctx.font = '10px ' + getComputedStyle(document.body).fontFamily;
      const labelY = b.y0 * sy - 2;
      ctx.fillText(`${b.status} ${b.w}x${b.h} A=${b.area}`, b.x0 * sx, labelY < 8 ? labelY + 14 : labelY);
    });
  };
  img.src = res.image;
}

function drawMask(res) {
  const cv = $('cv-mask');
  const wrap = cv.parentElement;
  const { w, h } = fitToWrap(wrap, res.img_w, res.img_h);
  const dpr = window.devicePixelRatio || 1;
  cv.width = w * dpr; cv.height = h * dpr;
  cv.style.width = w + 'px'; cv.style.height = h + 'px';
  const ctx = cv.getContext('2d');
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);

  // 深灰底 + 亮紅 mask 疊上去（純黑會讓細小白斑邊緣糊掉）
  ctx.fillStyle = '#1a1a1a';
  ctx.fillRect(0, 0, w, h);
  const img = new Image();
  img.onload = () => {
    ctx.drawImage(img, 0, 0, w, h);
  };
  img.src = res.mask;
}

function resetParams() {
  params = { ...defaultParams };
  refreshSliderValues();
  runDetect();
}

async function saveCurrentDetection() {
  if (!lastDetect || segments.length === 0) {
    setStatus('請先載入圖片並完成偵測');
    return;
  }
  const seg = segments[idx];
  setStatus('儲存中…');
  try {
    const res = await fetch('/api/save-detection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        segment_path: seg.path,
        overall:      lastDetect.overall,
        ng_count:     lastDetect.ng_count,
        gray_count:   lastDetect.gray_count,
        bboxes:       lastDetect.bboxes,
        params:       lastDetect.params,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const j = await res.json();
    setStatus(`已儲存 · id=${j.id} · side=${j.meta.side} · ${j.meta.date}/${j.meta.hour}時 · ${j.meta.substrate_id || '無基板'}`);
  } catch (e) {
    setStatus(`儲存失敗：${e.message}`);
  }
}

async function saveAllLoaded() {
  // 把目前載入的全部塊都偵測並寫進 DB（後端存到正式輸出夾）。寫入機制同 save-detection。
  if (segments.length === 0) { setStatus('尚未載入任何塊'); return; }
  const n = segments.length;
  if (!confirm(`要把目前載入的 ${n} 塊全部偵測並寫入 DB 嗎？塊數多時可能需要一點時間。`)) return;
  setStatus(`儲存全部 ${n} 塊中…（偵測+寫入，請稍候）`);
  if (elSaveAll) elSaveAll.disabled = true;
  try {
    const res = await fetch('/api/save-all', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ segments: segments.map(s => s.path), params }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const j = await res.json();
    setStatus(`✅ 已寫入 ${j.saved} 筆（NG ${j.NG}／GRAY ${j.GRAY}／OK ${j.OK}` +
              `${j.skipped ? '，略過 ' + j.skipped : ''}${j.failed ? '，失敗 ' + j.failed : ''}）· 切到 Dashboard 查看`);
  } catch (e) {
    setStatus(`儲存全部失敗：${e.message}`);
  } finally {
    if (elSaveAll) elSaveAll.disabled = false;
  }
}

async function applyAsProductionParams() {
  // 把目前 6 個滑桿參數寫回 split_config.json，供生產端 watch.py 沿用。
  setStatus('套用中…');
  try {
    const res = await fetch('/api/apply-params', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        threshold:     params.threshold,
        len_ng:        params.len_ng,
        area_ng:       params.area_ng,
        len_gray_min:  params.len_gray_min,
        area_gray_min: params.area_gray_min,
        merge_dist:    params.merge_dist,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const j = await res.json();
    // 套用後這組就成為新的「預設值」，重設鈕以此為基準
    defaultParams = { ...params };
    const s = j.saved || {};
    setStatus(`✅ 已套用為正式參數（生產端沿用）· 閥值=${s.threshold} NG長=${s.len_ng} NG面積=${s.area_ng}`);
  } catch (e) {
    setStatus(`套用失敗：${e.message}`);
  }
}

async function pickPath(kind) {
  setStatus(kind === 'folder' ? '請在對話框中選擇資料夾…' : '請在對話框中選擇圖檔…');
  try {
    const res = await fetch(`/api/pick?kind=${kind}`);
    const j = await res.json();
    if (!j.path) { setStatus('已取消'); return; }
    elFolder.value = j.path;
    loadSegments();
  } catch (e) {
    setStatus(`開啟對話框失敗：${e.message}`);
  }
}

function bindControls() {
  elFolder = $('vf-folder');
  elFilter = $('vf-filter');
  elStatus = $('visual-status-text');
  elPrev = $('vc-prev');
  elNext = $('vc-next');
  elPickFolder = $('vc-pick-folder');
  elPickFile = $('vc-pick-file');
  elReset = $('vc-reset');
  elSave = $('vc-save');
  elSaveAll = $('vc-save-all');
  elApply = $('vc-apply');
  elLoading = $('visual-loading');

  elPickFolder.addEventListener('click', () => pickPath('folder'));
  elPickFile.addEventListener('click', () => pickPath('file'));
  elPrev.addEventListener('click', () => go(-1));
  elNext.addEventListener('click', () => go(+1));
  elReset.addEventListener('click', resetParams);
  elSave.addEventListener('click', saveCurrentDetection);
  elSaveAll.addEventListener('click', saveAllLoaded);
  elApply.addEventListener('click', applyAsProductionParams);
  elFolder.addEventListener('keydown', e => { if (e.key === 'Enter') loadSegments(); });
  elFilter.addEventListener('change', loadSegments);

  document.addEventListener('keydown', e => {
    if (!document.getElementById('view-visual').classList.contains('active')) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowLeft') go(-1);
    else if (e.key === 'ArrowRight') go(+1);
    else if (e.key === 'Home') { idx = 0; runDetect(); }
    else if (e.key === 'End') { idx = segments.length - 1; runDetect(); }
  });
}

async function boot() {
  if (booted) return;
  booted = true;
  bindControls();
  buildThresholdPanel();
  await loadDefaultParams();
  await loadDefaultFolder();
  setStatus('點「選擇資料夾」或「選擇單張」開始，也可貼路徑後按 Enter');
}

function onResize() {
  if (booted && lastDetect) renderResult(lastDetect);
}

return { boot, onResize };

})();

window.Visual = Visual;
