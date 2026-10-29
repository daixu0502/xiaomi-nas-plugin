/* One updater UI for every plugin. Requests use the existing authenticated bridge. */
(function (global) {
  'use strict';
  var client = global.XiaomiPluginClient;
  if (!client) return;
  var doc = client.document, root = doc.documentElement, opener = root.querySelector('footer');
  if (!opener) return;
  var plugin = root.dataset.minasPlugin, dialog = doc.createElement('dialog');
  opener.classList.add('workbench-update-entry'); opener.setAttribute('role', 'button'); opener.tabIndex = 0;
  opener.setAttribute('aria-haspopup', 'dialog'); opener.setAttribute('aria-label', '查看插件版本并检查更新'); opener.title = '点击版本号检查更新';
  dialog.className = 'workbench-update-dialog';
  dialog.setAttribute('aria-labelledby', plugin + '-update-heading');
  // Static markup only. Release notes, errors and versions always use textContent.
  dialog.innerHTML = '<form method="dialog" class="workbench-update-heading"><h2 id="' + plugin + '-update-heading">插件更新</h2><button type="submit" class="secondary" aria-label="关闭插件更新">关闭</button></form>' +
    '<div class="workbench-update-content"><div class="workbench-update-actions"><button type="button" class="secondary" data-update="check">检查更新</button><button type="button" class="primary" data-update="install" disabled>立即更新</button></div>' +
    '<p data-update="confirm" hidden>更新期间可能短暂重启本插件服务。<button type="button" class="primary" data-update="confirmInstall">确认更新</button><button type="button" class="secondary" data-update="cancelInstall">取消</button></p><p class="workbench-update-error" data-update="error" role="alert" hidden></p>' +
    '<div class="workbench-update-versions"><div><span>当前版本</span><strong data-update="installed">—</strong></div><div><span>最新版本</span><strong data-update="latest">尚未检查</strong></div></div>' +
    '<p data-update="hint">检查 GitHub 正式发布，更新会保留配置与任务。</p><pre class="workbench-update-notes" data-update="notes" hidden></pre>' +
    '<div data-update="progressBlock" hidden><progress data-update="progress" max="100" value="0"></progress><p data-update="message" role="status" aria-live="polite"></p><ol class="workbench-update-log" data-update="logs"></ol></div>' +
    '<section class="workbench-update-proxy" aria-label="更新代理设置"><div class="workbench-proxy-heading"><div><h3>更新代理</h3><p>仅用于检查和下载安装包</p></div><label class="workbench-proxy-toggle"><input class="workbench-proxy-switch-input" type="checkbox" role="switch" aria-label="使用更新代理" data-update="proxyEnabled"><span class="workbench-proxy-switch-track" aria-hidden="true"></span></label></div>' +
    '<label class="workbench-proxy-address">代理地址<div class="workbench-proxy-endpoint"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.1 0l3-3a5 5 0 0 0-7.1-7.1l-1.7 1.7M14 11a5 5 0 0 0-7.1 0l-3 3a5 5 0 0 0 7.1 7.1l1.7-1.7"/></svg><input type="url" data-update="proxyUrl" placeholder="http://127.0.0.1:7890" autocomplete="off" spellcheck="false"></div></label>' +
    '<p class="workbench-proxy-help">支持 HTTP / HTTPS，八个插件共用此设置</p><details class="workbench-proxy-auth"><summary>代理认证<span>可选</span></summary><div class="workbench-update-auth"><label>用户名<input data-update="proxyUsername" placeholder="代理用户名" autocomplete="off"></label><label>密码<input type="password" data-update="proxyPassword" placeholder="代理密码" autocomplete="new-password"></label></div>' +
    '<label class="workbench-update-check"><input type="checkbox" data-update="clearPassword">清除已保存的密码</label></details><div class="workbench-proxy-footer"><span data-update="proxySaved" role="status"></span><button type="button" class="secondary" data-update="saveProxy">保存代理设置</button></div></section>' +
    '</div>';
  (root.querySelector('.desktop-frame') || doc.body).appendChild(dialog);
  var nodes = {}, timer = 0, disposed = false, busy = false, state = null, proxyDirty = false, requestSerial = 0, backgroundWatch = false;
  dialog.querySelectorAll('[data-update]').forEach(function (node) { nodes[node.dataset.update] = node; });
  function error(message) { nodes.error.hidden = !message; nodes.error.textContent = message || ''; }
  function working(on) {
    busy = on; nodes.check.disabled = on; nodes.saveProxy.disabled = on;
    nodes.install.disabled = on || proxyDirty || !state || !state.latest || !state.latest.available;
    nodes.confirmInstall.disabled = on;
  }
  function api(action, data) {
    return client.request({plugin:plugin, cgi:plugin + '.cgi', action:'plugin_update_' + action, options:{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data || {})}}).then(function (response) { return response.json(); }).then(function (value) {
      if (!value.ok) throw new Error(value.error || '在线更新失败'); return value;
    });
  }
  function render(value, loadProxy) {
    state = value;
    var available = Boolean(value.latest && value.latest.available);
    opener.dataset.updateAvailable = String(available);
    opener.setAttribute('aria-label', available ? '有新版本 ' + value.latest.version + '，点击版本号更新插件' : '查看插件版本并检查更新');
    opener.title = available ? '有新版本 ' + value.latest.version + '，点击更新' : '点击版本号检查更新';
    nodes.installed.textContent = value.installedVersion || '—'; nodes.latest.textContent = value.latest ? value.latest.version : '尚未检查';
    nodes.hint.textContent = value.latest ? (value.latest.available ? '有新版本可用 · ' + (value.latest.size / 1024 / 1024).toFixed(1) + ' MiB' : '当前已是最新版本') : '检查 GitHub 正式发布，更新会保留配置与任务。';
    nodes.notes.textContent = value.latest ? value.latest.notes || '本次发布未提供更新说明。' : ''; nodes.notes.hidden = !value.latest;
    if (loadProxy && value.proxy) {
      nodes.proxyEnabled.checked = value.proxy.enabled; nodes.proxyUrl.value = value.proxy.url || ''; nodes.proxyUsername.value = value.proxy.username || '';
      nodes.proxyPassword.value = ''; nodes.proxyPassword.placeholder = value.proxy.hasPassword ? '已保存，留空保持原密码' : '未设置'; nodes.clearPassword.checked = false; proxyDirty = false;
    }
    var job = value.job || {};
    if (job.running && (job.action === 'check' || job.action === 'install')) backgroundWatch = true;
    nodes.progressBlock.hidden = !job.message && !(job.logs || []).length; nodes.message.textContent = job.message || ''; nodes.message.hidden = !job.message || job.message === nodes.hint.textContent; nodes.progress.hidden = !job.running; nodes.progress.value = job.percent || 0;
    nodes.logs.replaceChildren(); (job.logs || []).filter(function (item) { return item.message !== nodes.hint.textContent; }).forEach(function (item) { var li = doc.createElement('li'); li.textContent = new Date(item.time * 1000).toLocaleTimeString() + ' · ' + item.message; nodes.logs.appendChild(li); });
    working(Boolean(job.running));
    if (!job.running) backgroundWatch = false;
    if (job.phase === 'failed' || job.phase === 'interrupted') error(job.message);
    schedulePoll();
  }
  function schedulePoll() {
    global.clearTimeout(timer);
    if (!disposed && (dialog.open || backgroundWatch) && state && state.job && state.job.running) timer = global.setTimeout(function () { load(false); }, 1500);
  }
  function load(loadProxy, automatic) {
    var serial = ++requestSerial;
    return api('status').then(function (value) { if (!disposed && serial === requestSerial) { render(value, loadProxy); if (automatic && value.checkDue && !(value.job && value.job.running)) perform('check', {background:true}); } }).catch(function (failure) { if (!disposed && serial === requestSerial) { error(failure.message); working(Boolean(state && state.job && state.job.running)); if (dialog.open || backgroundWatch) timer = global.setTimeout(function () { load(false); }, 4000); } });
  }
  function perform(action, data, loadProxy) {
    var serial = ++requestSerial;
    if (action === 'check' || action === 'install') backgroundWatch = true;
    error(''); working(true);
    return api(action, data).then(function (value) { if (!disposed && serial === requestSerial) render(value, loadProxy); }).catch(function (failure) { if (!disposed && serial === requestSerial) { error(failure.message); working(false); } });
  }
  function proxyValue() { return {enabled:nodes.proxyEnabled.checked, url:nodes.proxyUrl.value.trim(), username:nodes.proxyUsername.value, password:nodes.proxyPassword.value, clearPassword:nodes.clearPassword.checked}; }
  function saveProxy() {
    return perform('proxy', {proxy:proxyValue()}, true).then(function () { if (!nodes.error.hidden) return; nodes.proxySaved.textContent = '设置已保存'; nodes.proxySaved.dataset.state = 'saved'; });
  }
  ['proxyEnabled','proxyUrl','proxyUsername','proxyPassword','clearPassword'].forEach(function (key) { nodes[key].addEventListener('input', function () { proxyDirty = true; nodes.proxySaved.textContent = '有未保存的更改'; nodes.proxySaved.dataset.state = 'dirty'; working(busy); }); });
  function openUpdater() {
    error(''); nodes.confirm.hidden = true;
    if (client.openDialog) client.openDialog(dialog); else dialog.showModal();
    working(true); load(true).then(function () { if (!state) working(false); });
  }
  opener.addEventListener('click', openUpdater);
  opener.addEventListener('keydown', function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openUpdater(); } });
  nodes.saveProxy.addEventListener('click', saveProxy);
  nodes.check.addEventListener('click', function () {
    if (proxyDirty) { saveProxy().then(function () { if (nodes.error.hidden) perform('check'); }); }
    else perform('check');
  });
  nodes.install.addEventListener('click', function () { nodes.confirm.hidden = false; nodes.confirmInstall.focus(); });
  nodes.cancelInstall.addEventListener('click', function () { nodes.confirm.hidden = true; nodes.install.focus(); });
  nodes.confirmInstall.addEventListener('click', function () { if (!state || !state.latest) return; nodes.confirm.hidden = true; perform('install', {version:state.latest.version}); });
  dialog.addEventListener('close', function () { nodes.proxyPassword.value = ''; schedulePoll(); });
  function dispose() { disposed = true; global.clearTimeout(timer); global.removeEventListener('unmount', dispose); global.removeEventListener('pagehide', dispose); }
  global.addEventListener('unmount', dispose); global.addEventListener('pagehide', dispose);
  load(true, true);
})(window);
