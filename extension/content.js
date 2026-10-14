/* 划词解析助手 — 内容脚本
 *
 * 流程：
 *   鼠标划词 → 选区旁出现「解析」小按钮 → 点击后弹出面板
 *   面板内优先内嵌已发布的网页应用（iframe 与模型接口同源，才能调用平台的免密钥模型）；
 *   若目标站点禁止被内嵌，握手超时后自动降级为独立小窗口。
 */
'use strict';

(() => {
  if (window.__textLensInjected) return;
  window.__textLensInjected = true;

  const APP_BASE = 'https://text-select-ai.app.workbuddy.host/';
  const PANEL_W = 404;
  const PANEL_H = 568;
  const READY_TIMEOUT_MS = 3000;
  const EDGE = 10;

  let host = null;
  let root = null;
  let bubbleEl = null;
  let panelEl = null;
  let barEl = null;
  let frameEl = null;
  let loadingEl = null;

  let frameReady = false;
  let readyTimer = null;
  let pendingSelection = null;
  let lastSentKey = '';
  let lastSelectionText = '';
  let panelOpen = false;
  let panelPinned = false;
  let usingFallback = false;
  let dragState = null;
  let hideTimer = null;

  /* ───────────────────────── 样式 ───────────────────────── */

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }

    .bubble {
      position: fixed;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      height: 28px;
      padding: 0 11px 0 9px;
      border: 1px solid #d5d9e0;
      border-radius: 999px;
      background: #ffffff;
      box-shadow: 0 1px 2px rgba(16,24,40,.08), 0 6px 18px rgba(16,24,40,.14);
      color: #1b1f26;
      font: 600 12px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
            "Microsoft YaHei", sans-serif;
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
      transition: transform .1s ease, box-shadow .1s ease;
    }
    .bubble:hover {
      transform: translateY(-1px);
      box-shadow: 0 2px 4px rgba(16,24,40,.1), 0 10px 24px rgba(16,24,40,.18);
    }
    .bubble .spark { color: #2f6bff; font-size: 13px; line-height: 1; }
    .bubble[hidden] { display: none; }

    .panel {
      position: fixed;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border: 1px solid #e6e8ec;
      border-radius: 14px;
      background: #ffffff;
      box-shadow: 0 4px 10px rgba(16,24,40,.1), 0 24px 56px rgba(16,24,40,.22);
      font: 13px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
            "Microsoft YaHei", sans-serif;
      color: #1b1f26;
      animation: lens-in .14s ease-out;
    }
    .panel[hidden] { display: none; }
    .panel.dragging { box-shadow: 0 8px 20px rgba(16,24,40,.16), 0 32px 72px rgba(16,24,40,.28); }

    @keyframes lens-in {
      from { opacity: 0; transform: translateY(6px) scale(.985); }
      to   { opacity: 1; transform: none; }
    }

    .bar {
      flex: 0 0 auto;
      display: flex;
      align-items: center;
      gap: 8px;
      height: 36px;
      padding: 0 6px 0 12px;
      background: #fbfbfc;
      border-bottom: 1px solid #e6e8ec;
      cursor: grab;
      user-select: none;
    }
    .bar.dragging { cursor: grabbing; }
    .bar .title {
      flex: 1 1 auto;
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
      font-size: 12px;
      font-weight: 600;
      color: #3b414b;
      letter-spacing: .2px;
    }
    .bar .title .spark { color: #2f6bff; font-size: 12px; }
    .bar .btn {
      flex: 0 0 auto;
      height: 24px;
      padding: 0 7px;
      border: 1px solid transparent;
      border-radius: 6px;
      background: transparent;
      color: #5c6470;
      font: 500 12px/1 inherit;
      cursor: pointer;
    }
    .bar .btn:hover { background: #eef0f3; color: #1b1f26; }

    .body { position: relative; flex: 1 1 auto; min-height: 0; background: #ffffff; }

    iframe {
      display: block;
      width: 100%;
      height: 100%;
      border: 0;
      background: #ffffff;
    }

    .loading {
      position: absolute;
      inset: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 10px;
      background: #ffffff;
      color: #8a9099;
      font-size: 12px;
    }
    .loading[hidden] { display: none; }
    .loading .spin {
      width: 18px; height: 18px;
      border: 2px solid #e6e8ec;
      border-top-color: #2f6bff;
      border-radius: 50%;
      animation: lens-spin .7s linear infinite;
    }
    @keyframes lens-spin { to { transform: rotate(360deg); } }
  `;

  /* ───────────────────────── 构建 UI ───────────────────────── */

  function applyStyles(shadow) {
    /* 优先用 CSS 构造样式表：不经过 <style> 元素，不受页面 CSP 限制 */
    try {
      if (typeof CSSStyleSheet === 'function') {
        const sheet = new CSSStyleSheet();
        sheet.replaceSync(CSS);
        shadow.adoptedStyleSheets = [sheet];
        return;
      }
    } catch (_) { /* 回退 */ }
    const style = document.createElement('style');
    style.textContent = CSS;
    shadow.appendChild(style);
  }

  function ensureHost() {
    if (host && host.isConnected) return;

    host = document.createElement('div');
    host.setAttribute('data-textlens-root', '1');
    host.style.position = 'fixed';
    host.style.left = '0';
    host.style.top = '0';
    host.style.width = '0';
    host.style.height = '0';
    host.style.zIndex = '2147483647';
    host.style.pointerEvents = 'none';

    root = host.attachShadow({ mode: 'closed' });
    applyStyles(root);

    const wrap = document.createElement('div');
    wrap.style.pointerEvents = 'auto';
    root.appendChild(wrap);

    /* 划词后的小按钮 */
    bubbleEl = document.createElement('button');
    bubbleEl.type = 'button';
    bubbleEl.className = 'bubble';
    bubbleEl.hidden = true;
    bubbleEl.innerHTML = '<span class="spark">✦</span><span>解析</span>';
    bubbleEl.addEventListener('mousedown', (e) => e.preventDefault());
    bubbleEl.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      openPanel();
    });
    wrap.appendChild(bubbleEl);

    /* 解析面板 */
    panelEl = document.createElement('div');
    panelEl.className = 'panel';
    panelEl.hidden = true;

    barEl = document.createElement('div');
    barEl.className = 'bar';

    const titleEl = document.createElement('div');
    titleEl.className = 'title';
    titleEl.innerHTML = '<span class="spark">✦</span><span>划词解析</span>';

    const btnNewWindow = document.createElement('button');
    btnNewWindow.type = 'button';
    btnNewWindow.className = 'btn';
    btnNewWindow.textContent = '新窗口';
    btnNewWindow.title = '在独立标签页中打开';
    btnNewWindow.addEventListener('click', (e) => {
      e.stopPropagation();
      openExternalTab();
    });

    const btnClose = document.createElement('button');
    btnClose.type = 'button';
    btnClose.className = 'btn';
    btnClose.textContent = '✕';
    btnClose.title = '关闭';
    btnClose.addEventListener('click', (e) => {
      e.stopPropagation();
      closePanel();
    });

    barEl.appendChild(titleEl);
    barEl.appendChild(btnNewWindow);
    barEl.appendChild(btnClose);

    const bodyEl = document.createElement('div');
    bodyEl.className = 'body';

    frameEl = document.createElement('iframe');
    frameEl.setAttribute('allow', 'clipboard-write');
    frameEl.setAttribute('referrerpolicy', 'origin');

    loadingEl = document.createElement('div');
    loadingEl.className = 'loading';
    loadingEl.innerHTML = '<div class="spin"></div><div>正在准备解析面板…</div>';

    bodyEl.appendChild(frameEl);
    bodyEl.appendChild(loadingEl);

    panelEl.appendChild(barEl);
    panelEl.appendChild(bodyEl);
    wrap.appendChild(panelEl);

    bindDrag();
    (document.body || document.documentElement).appendChild(host);
  }

  /* ───────────────────────── 拖拽 ───────────────────────── */

  function bindDrag() {
    barEl.addEventListener('pointerdown', (e) => {
      if (e.target && e.target.closest && e.target.closest('.btn')) return;
      e.preventDefault();
      const rect = panelEl.getBoundingClientRect();
      dragState = { dx: e.clientX - rect.left, dy: e.clientY - rect.top };
      panelEl.classList.add('dragging');
      barEl.classList.add('dragging');
      frameEl.style.pointerEvents = 'none';
      try { barEl.setPointerCapture(e.pointerId); } catch (_) { /* noop */ }
      panelPinned = true;
    });

    barEl.addEventListener('pointermove', (e) => {
      if (!dragState) return;
      const w = panelEl.offsetWidth;
      const h = panelEl.offsetHeight;
      let left = e.clientX - dragState.dx;
      let top = e.clientY - dragState.dy;
      left = Math.max(EDGE, Math.min(left, window.innerWidth - w - EDGE));
      top = Math.max(EDGE, Math.min(top, window.innerHeight - h - EDGE));
      panelEl.style.left = `${Math.round(left)}px`;
      panelEl.style.top = `${Math.round(top)}px`;
    });

    const endDrag = (e) => {
      if (!dragState) return;
      dragState = null;
      panelEl.classList.remove('dragging');
      barEl.classList.remove('dragging');
      frameEl.style.pointerEvents = '';
      try { barEl.releasePointerCapture(e.pointerId); } catch (_) { /* noop */ }
    };
    barEl.addEventListener('pointerup', endDrag);
    barEl.addEventListener('pointercancel', endDrag);
  }

  /* ───────────────────────── 选区 ───────────────────────── */

  function currentSelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
    const text = sel.toString().trim();
    if (!text) return null;
    let rect = null;
    try {
      rect = sel.getRangeAt(0).getBoundingClientRect();
    } catch (_) {
      return null;
    }
    if (!rect || (rect.width === 0 && rect.height === 0)) return null;
    return { text, rect };
  }

  function showBubble(rect) {
    if (!bubbleEl) return;
    const bw = bubbleEl.offsetWidth || 66;
    const bh = bubbleEl.offsetHeight || 28;

    let left = rect.left + rect.width / 2 - bw / 2;
    left = Math.max(EDGE, Math.min(left, window.innerWidth - bw - EDGE));

    let top = rect.bottom + 8;
    if (top + bh > window.innerHeight - EDGE) top = Math.max(EDGE, rect.top - bh - 8);

    bubbleEl.style.left = `${Math.round(left)}px`;
    bubbleEl.style.top = `${Math.round(top)}px`;
    bubbleEl.hidden = false;
  }

  function hideBubble() {
    if (bubbleEl) bubbleEl.hidden = true;
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideBubble, 60);
  }

  function isInsideHost(e) {
    return !!(host && e && e.composedPath && e.composedPath().includes(host));
  }

  function onMouseDown(e) {
    if (isInsideHost(e)) return;
    scheduleHide();
  }

  function onMouseUp(e) {
    if (isInsideHost(e)) return;

    setTimeout(() => {
      const found = currentSelection();
      if (!found) { hideBubble(); return; }

      const key = found.text.slice(0, 200);

      if (panelOpen) {
        if (key !== lastSentKey) sendSelection(found.text);
        hideBubble();
        return;
      }

      if (key === lastSentKey) return;
      ensureHost();
      showBubble(found.rect);
    }, 12);
  }

  /* ───────────────────────── 面板 ───────────────────────── */

  function viewportRect() {
    return {
      width: Math.min(PANEL_W, window.innerWidth - EDGE * 2),
      height: Math.min(PANEL_H, window.innerHeight - EDGE * 2),
    };
  }

  function positionPanel(rect) {
    const { width: w, height: h } = viewportRect();

    let left = rect ? rect.right + 12 : window.innerWidth - w - 24;
    if (left + w > window.innerWidth - EDGE) left = rect ? rect.left - w - 12 : EDGE;
    if (left < EDGE) left = Math.max(EDGE, window.innerWidth - w - EDGE);

    let top = rect ? rect.top : 60;
    if (top + h > window.innerHeight - EDGE) top = Math.max(EDGE, window.innerHeight - h - EDGE);
    if (top < EDGE) top = EDGE;

    panelEl.style.left = `${Math.round(left)}px`;
    panelEl.style.top = `${Math.round(top)}px`;
    panelEl.style.width = `${Math.round(w)}px`;
    panelEl.style.height = `${Math.round(h)}px`;
  }

  function openPanel() {
    ensureHost();

    const found = currentSelection();
    if (found) lastSelectionText = found.text;

    if (!panelOpen) {
      if (!panelPinned) positionPanel(found ? found.rect : null);
      else {
        const { width: w, height: h } = viewportRect();
        panelEl.style.width = `${Math.round(w)}px`;
        panelEl.style.height = `${Math.round(h)}px`;
      }
      panelEl.hidden = false;
      panelOpen = true;

      if (!frameEl.getAttribute('src')) {
        usingFallback = false;
        frameReady = false;
        loadingEl.hidden = false;
        frameEl.setAttribute('src', `${APP_BASE}?embed=1`);
        clearTimeout(readyTimer);
        readyTimer = setTimeout(onReadyTimeout, READY_TIMEOUT_MS);
      }
    } else {
      panelEl.hidden = false;
    }

    hideBubble();
    if (lastSelectionText) sendSelection(lastSelectionText);
  }

  /* ───────────────────────── 与解析页通信 ───────────────────────── */

  function sendSelection(text) {
    if (!text) return;
    lastSelectionText = text;
    lastSentKey = text.slice(0, 200);

    const payload = {
      text,
      title: document.title || '',
      url: location.href,
    };

    if (usingFallback) {
      chrome.runtime.sendMessage({ type: 'textlens:push-selection', selection: payload }, () => {
        void chrome.runtime.lastError;
      });
      return;
    }

    if (!frameReady) {
      pendingSelection = payload;
      return;
    }
    postToFrame(payload);
  }

  function postToFrame(payload) {
    try {
      frameEl.contentWindow.postMessage({ type: 'textlens:selection', ...payload }, '*');
    } catch (_) { /* noop */ }
  }

  function flushPending() {
    if (!pendingSelection) return;
    const p = pendingSelection;
    pendingSelection = null;
    postToFrame(p);
  }

  function onReadyTimeout() {
    if (frameReady) return;
    /* 目标站点禁止被内嵌 → 降级为独立小窗口 */
    usingFallback = true;
    fallbackToWindow();
  }

  function fallbackToWindow() {
    const rect = panelEl.getBoundingClientRect();
    const text = lastSelectionText || (pendingSelection && pendingSelection.text) || '';
    const payload = { text, title: document.title || '', url: location.href };

    chrome.runtime.sendMessage({
      type: 'textlens:open-popup',
      options: {
        url: `${APP_BASE}?embed=popup`,
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
      },
      selection: text ? payload : null,
    }, () => {
      void chrome.runtime.lastError;
    });

    closePanel();
  }

  function openExternalTab() {
    if (!lastSelectionText) {
      window.open(APP_BASE, '_blank', 'noopener');
      return;
    }
    const params = new URLSearchParams({
      text: lastSelectionText,
      title: document.title || '',
      url: location.href,
    });
    window.open(`${APP_BASE}?${params.toString()}`, '_blank', 'noopener');
    closePanel();
  }

  function closePanel() {
    clearTimeout(readyTimer);
    if (!panelEl) return;
    panelEl.hidden = true;
    panelOpen = false;
    hideBubble();
  }

  /* ───────────────────────── 全局事件 ───────────────────────── */

  window.addEventListener('message', (event) => {
    const data = event.data;
    if (!data || typeof data !== 'object') return;
    if (!frameEl || event.source !== frameEl.contentWindow) return;

    if (data.type === 'textlens:ready') {
      frameReady = true;
      clearTimeout(readyTimer);
      if (loadingEl) loadingEl.hidden = true;
      flushPending();
    } else if (data.type === 'textlens:close') {
      closePanel();
    }
  });

  document.addEventListener('mousedown', onMouseDown, true);
  document.addEventListener('mouseup', onMouseUp, true);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && panelOpen) closePanel();
  }, true);

  window.addEventListener('scroll', () => {
    hideBubble();
  }, true);

  window.addEventListener('resize', () => {
    hideBubble();
    if (panelOpen && !panelPinned && panelEl) positionPanel(null);
  });
})();
