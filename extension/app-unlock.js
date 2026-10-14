/* 划词解析助手 — 解锁脚本（MAIN world）
 *
 * 解析页本身**不带**云服务钥匙：云服务的免密钥通道只认调用来源域名，
 * 而域名必须公开可达，钥匙写进网页源码就等于给所有拿到网址的人开了白嫖入口。
 * 所以钥匙改由本机注入 —— 桌面助手走调试通道，浏览器扩展走这个脚本。
 *
 * 【这个仓库里的版本故意留空】钥匙是每个人自己的，随源码分发等于把
 * 发布者的额度送人。要用这个扩展，请把下面两项换成你自己的：
 *   · publishableKey —— 在 %LOCALAPPDATA%\TextLens\cloud.json 里能找到
 *     （先用桌面助手完成一次「接入设置」，这个文件就会生成）；
 *   · resourceId     —— 同上，可以留空字符串。
 * 同时把 manifest.json / content.js 里的面板域名换成**你自己发布的**那一份。
 *
 * 本文件由打包脚本（packaging/build_release.py）在出安装包时自动清空；
 * 手动改回钥匙后，这个目录就是你的本机专用件，不要提交到公开仓库。
 */
'use strict';

const CLOUD_SECRET = {
  publishableKey: "",
  resourceId: "",
};

(() => {
  let tries = 0;
  const tick = () => {
    if (typeof window.__lzrUnlock === 'function') {
      try { window.__lzrUnlock(CLOUD_SECRET); } catch (_) { /* ignore */ }
      return;
    }
    // 页面的 app.js 可能还没执行完，等一会儿再试（最长约 15 秒）
    if (++tries < 75) setTimeout(tick, 200);
  };
  tick();
})();
