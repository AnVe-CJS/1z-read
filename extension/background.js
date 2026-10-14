/* 划词解析助手 — 后台脚本
 *
 * 只负责一件事：当页面内嵌面板不可用（目标站点禁止被内嵌）时，
 * 改为打开一个独立小窗口承载解析界面，并把后续的划词结果转发进去。
 */
'use strict';

const POPUP_WIDTH = 420;
const POPUP_HEIGHT = 620;
const RELAY_RETRY_MS = 400;
const RELAY_MAX_TRIES = 12;

let popupWindowId = null;
let popupTabId = null;

chrome.windows.onRemoved.addListener((windowId) => {
  if (windowId === popupWindowId) {
    popupWindowId = null;
    popupTabId = null;
  }
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function openPopupWindow(options = {}) {
  const w = Math.max(320, Math.min(options.width || POPUP_WIDTH, 720));
  const h = Math.max(380, Math.min(options.height || POPUP_HEIGHT, 900));

  const createData = { url: options.url, type: 'popup', width: w, height: h, focused: true };
  if (Number.isFinite(options.left)) createData.left = Math.max(0, Math.round(options.left));
  if (Number.isFinite(options.top)) createData.top = Math.max(0, Math.round(options.top));

  const win = await chrome.windows.create(createData);
  popupWindowId = win.id;
  popupTabId = win.tabs && win.tabs[0] ? win.tabs[0].id : null;
  return popupTabId;
}

async function focusPopup() {
  if (popupWindowId == null) return false;
  try {
    await chrome.windows.update(popupWindowId, { focused: true, drawAttention: true });
    return true;
  } catch (_) {
    popupWindowId = null;
    popupTabId = null;
    return false;
  }
}

/** 页面脚本可能还没注入完成，重试投递；页面侧会缓存早期消息。 */
async function relayToPopup(selection) {
  if (popupTabId == null) return false;

  for (let i = 0; i < RELAY_MAX_TRIES; i += 1) {
    try {
      await chrome.tabs.sendMessage(popupTabId, { type: 'textlens:selection', selection });
      return true;
    } catch (_) {
      await sleep(RELAY_RETRY_MS);
    }
  }

  popupTabId = null;
  popupWindowId = null;
  return false;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return undefined;

  if (msg.type === 'textlens:open-popup') {
    (async () => {
      try {
        await openPopupWindow(msg.options || {});
      } catch (err) {
        sendResponse({ ok: false, error: String(err && err.message) });
        return;
      }
      const delivered = msg.selection && msg.selection.text
        ? await relayToPopup(msg.selection)
        : true;
      sendResponse({ ok: true, delivered, tabId: popupTabId, windowId: popupWindowId });
    })();
    return true;
  }

  if (msg.type === 'textlens:push-selection') {
    (async () => {
      const alive = await focusPopup();
      const ok = alive ? await relayToPopup(msg.selection) : false;
      sendResponse({ ok });
    })();
    return true;
  }

  if (msg.type === 'textlens:close-popup') {
    if (popupWindowId != null) {
      chrome.windows.remove(popupWindowId).catch(() => { /* already gone */ });
    }
    popupWindowId = null;
    popupTabId = null;
    sendResponse({ ok: true });
    return undefined;
  }

  return undefined;
});
