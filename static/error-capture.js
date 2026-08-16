// 全局错误捕获（诊断用）：错误记录到 window.__errs，标签页标题仅保留安全字符。
// 外置文件以便 CSP 收紧为 script-src 'self'（无内联脚本）。
window.__errs = [];
function __recordErr(msg) {
  window.__errs.push(msg);
  const safe = String(msg).replace(/[^0-9A-Za-z\u4e00-\u9fa5 :.\[\]-]/g, '');
  document.title = 'ERR[' + window.__errs.length + ']: ' + safe.slice(0, 100);
}
window.addEventListener('error', e => __recordErr((e.message || 'err') + ' @' + (e.filename || '').split('/').pop() + ':' + e.lineno));
window.addEventListener('unhandledrejection', e => __recordErr('REJ: ' + (e.reason && e.reason.message ? e.reason.message : String(e.reason)).slice(0, 140)));
