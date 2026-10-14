/* 划词解析助手 — 解析页桥接脚本
 *
 * 只在解析页（面板自己那份域名）内运行。
 * 把扩展后台发来的划词结果转交给页面，并把页面的就绪/关闭信号回传。
 * 页面尚未加载完时先缓存，等页面报 ready 再补发，避免早期消息丢失。
 */
'use strict';

(() => {
  if (window.__textLensRelayInjected) return;
  window.__textLensRelayInjected = true;

  let pageReady = false;
  let buffered = null;

  function deliver(selection) {
    window.postMessage({ type: 'textlens:selection', ...selection }, '*');
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || typeof msg !== 'object') return undefined;

    if (msg.type === 'textlens:selection' && msg.selection) {
      if (pageReady) deliver(msg.selection);
      else buffered = msg.selection;
      sendResponse({ ok: true, delivered: pageReady });
      return undefined;
    }

    if (msg.type === 'textlens:ping') {
      sendResponse({ ok: true });
      return undefined;
    }

    return undefined;
  });

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || typeof data !== 'object') return;

    if (data.type === 'textlens:ready') {
      pageReady = true;
      if (buffered) {
        deliver(buffered);
        buffered = null;
      }
      chrome.runtime.sendMessage({ type: 'textlens:page-ready', standalone: window.top === window }, () => {
        void chrome.runtime.lastError;
      });
    } else if (data.type === 'textlens:close') {
      /* 只有独立窗口场景需要真的关窗；内嵌场景由页面直接通知宿主页 */
      if (window.top === window) {
        chrome.runtime.sendMessage({ type: 'textlens:close-popup' }, () => {
          void chrome.runtime.lastError;
        });
      }
    }
  });
})();
