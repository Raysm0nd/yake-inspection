// ============================================================
// 整合 UI 主控：tab 切換 + 主題切換
// ============================================================

(function () {
  const VIEWS = ['visual', 'dashboard'];
  let currentView = 'dashboard';

  function activateView(view) {
    if (!VIEWS.includes(view)) return;
    currentView = view;

    document.querySelectorAll('[data-tab]').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === view);
    });
    VIEWS.forEach(v => {
      const el = document.getElementById('view-' + v);
      if (el) el.classList.toggle('active', v === view);
    });

    // 切換到對應 view 後再 boot（避免初始化計算 layout 時容器隱藏）
    if (view === 'visual' && window.Visual) {
      window.Visual.boot();
      window.Visual.onResize();
    } else if (view === 'dashboard' && window.Dashboard) {
      // refresh 內部：首次走完整 boot，之後重撈 DB（保留篩選）→ Visual 剛存的資料即時反映
      window.Dashboard.refresh();
    }
  }

  // Theme toggle
  const SUN = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
  const MOON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

  function bindTheme() {
    const btn = document.querySelector('[data-theme-toggle]');
    const html = document.documentElement;
    let d = html.getAttribute('data-theme') || 'dark';
    btn.innerHTML = d === 'dark' ? SUN : MOON;
    btn.addEventListener('click', () => {
      d = d === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-theme', d);
      btn.innerHTML = d === 'dark' ? SUN : MOON;
      if (window.Dashboard) window.Dashboard.rerender();
      if (window.Visual) window.Visual.onResize();
    });
  }

  function bindTabs() {
    document.querySelectorAll('[data-tab]').forEach(t => {
      t.addEventListener('click', () => activateView(t.dataset.tab));
    });
  }

  function bindResize() {
    let raf = null;
    window.addEventListener('resize', () => {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        if (window.Dashboard) window.Dashboard.onResize();
        if (window.Visual) window.Visual.onResize();
      });
    });
  }

  // Modal close (dashboard uses one, even if currently unused)
  window.closeShotModal = function () {
    const bd = document.getElementById('modal-backdrop');
    if (bd) bd.classList.remove('show');
  };
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') window.closeShotModal();
  });

  window.addEventListener('load', () => {
    bindTheme();
    bindTabs();
    bindResize();
    activateView('dashboard');  // 預設停在 dashboard
  });

  // Exposed for inline handlers in HTML
  window.activateView = activateView;
})();
