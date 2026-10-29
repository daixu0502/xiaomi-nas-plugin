/* Shared workbench chrome. Keep the original version node and its live binding. */
(function (global) {
  'use strict';
  var client = global.XiaomiPluginClient;
  if (!client) return;
  var doc = client.document, root = doc.documentElement;
  var shell = root.querySelector('.shell'), heading = root.querySelector('.plugin-header h1');
  if (!shell || !heading) return;
  var name = heading.textContent.trim(), navObserver = null, scheduled = 0, menuSerial = 0;
  var version = shell.querySelector('footer');
  var originalNext = version && version.nextSibling, originalParent = version && version.parentNode;
  var helperVersion = version && version.querySelector('#helper-version');
  var versionObserver = null;
  root.classList.add('workbench-ui');
  var themeKey = 'xiaomi-nas-plugin-theme', themeChoice = 'system', themeSelect = doc.createElement('select');
  var themeMedia = global.matchMedia('(prefers-color-scheme: dark)');
  themeSelect.id = 'workbenchTheme'; themeSelect.setAttribute('aria-label', '外观主题');
  [['system','跟随系统'],['light','浅色'],['dark','深色']].forEach(function (item) {
    var option = doc.createElement('option'); option.value = item[0]; option.textContent = item[1]; themeSelect.appendChild(option);
  });
  function readTheme(value) { return ['system','light','dark'].includes(value) ? value : 'system'; }
  try { themeChoice = readTheme(global.localStorage.getItem(themeKey)); } catch (_) {}
  function applyTheme() {
    root.dataset.themePreference = themeChoice;
    root.dataset.theme = themeChoice === 'system' ? (themeMedia.matches ? 'dark' : 'light') : themeChoice;
    themeSelect.value = themeChoice;
  }
  themeSelect.addEventListener('change', function () {
    themeChoice = readTheme(themeSelect.value); applyTheme();
    try { global.localStorage.setItem(themeKey, themeChoice); } catch (_) {}
  });
  function themeStorage(event) { if (event.key === themeKey || event.key === null) { themeChoice = readTheme(event.newValue); applyTheme(); } }
  if (themeMedia.addEventListener) themeMedia.addEventListener('change', applyTheme); else themeMedia.addListener(applyTheme);
  global.addEventListener('storage', themeStorage);
  var header = heading.closest('.plugin-header'), headerActions = doc.createElement('div'), themeLabel = doc.createElement('label');
  headerActions.className = 'workbench-header-actions'; themeLabel.className = 'workbench-theme'; themeLabel.appendChild(themeSelect);
  headerActions.appendChild(themeLabel);
  header.querySelectorAll(':scope > button').forEach(function (button) { headerActions.appendChild(button); });
  header.appendChild(headerActions); applyTheme();
  client.enhanceSelect(themeSelect);
  var refreshRuns = new Map(), refreshDisposed = false;
  function refresh(button, operation) {
    if (refreshRuns.has(button)) return refreshRuns.get(button).promise;
    var state = {started:Date.now(), busy:button.getAttribute('aria-busy'), timer:0, release:null};
    function clean() {
      button.removeAttribute('data-workbench-refreshing');
      if (state.busy === null) button.removeAttribute('aria-busy'); else button.setAttribute('aria-busy', state.busy);
      refreshRuns.delete(button);
    }
    button.setAttribute('data-workbench-refreshing', 'true'); button.setAttribute('aria-busy', 'true');
    refreshRuns.set(button, state);
    state.promise = Promise.resolve().then(operation).finally(function () {
      return new Promise(function (resolve) {
        state.release = function () { global.clearTimeout(state.timer); clean(); resolve(); };
        var remaining = Math.max(0, 400 - (Date.now() - state.started));
        if (refreshDisposed || !remaining) state.release(); else state.timer = global.setTimeout(state.release, remaining);
      });
    });
    return state.promise;
  }
  function alignRow(card, headSelector, actionsSelector) {
    var head = card.querySelector(headSelector), actions = card.querySelector(actionsSelector);
    if (!head || !actions || card.classList.contains('workbench-row')) return;
    var badge = head.querySelector(':scope > .badge'); if (badge) actions.insertBefore(badge, actions.firstChild);
    var content = doc.createElement('div'); content.className = 'workbench-row-content';
    Array.from(card.children).filter(function (item) { return item !== actions; }).forEach(function (item) { content.appendChild(item); });
    card.insertBefore(content, actions); card.classList.add('workbench-row'); actions.classList.add('workbench-row-actions');
  }
  function actionsMenu(actions, keep) {
    var secondary = Array.from(actions.children).filter(function (item) { return !keep(item); });
    if (!secondary.length) return;
    var reference = Array.from(actions.children).find(function (item) { return keep(item) && item.tagName === 'BUTTON' && item.getAttribute('role') !== 'switch'; });
    var toggle = doc.createElement('button'), body = doc.createElement('div');
    toggle.type = 'button'; toggle.className = (reference ? reference.className + ' ' : '') + 'workbench-more';
    toggle.textContent = '更多'; body.className = 'workbench-more-body';
    body.id = root.dataset.minasPlugin + '-workbench-actions-' + (++menuSerial); body.hidden = true; body.setAttribute('role', 'group'); body.setAttribute('aria-label', '更多操作');
    toggle.setAttribute('aria-controls', body.id); toggle.setAttribute('aria-expanded', 'false');
    secondary.forEach(function (item) { body.appendChild(item); }); actions.append(toggle, body);
    function setOpen(open) { body.hidden = !open; toggle.setAttribute('aria-expanded', String(open)); }
    toggle.addEventListener('click', function () { setOpen(body.hidden); });
    function closeMenu(event) { if (event.key === 'Escape') { event.stopPropagation(); setOpen(false); toggle.focus(); } }
    toggle.addEventListener('keydown', closeMenu); body.addEventListener('keydown', closeMenu);
  }
  global.XiaomiPluginWorkbench = { actionsMenu: actionsMenu, alignRow: alignRow, refresh: refresh };
  function text(node, value) { if (node.textContent !== value) node.textContent = value; }
  function updateTitle() {
    var nav = root.querySelector('.desktop-frame > .tabs');
    var active = nav && nav.querySelector('.tab.active');
    text(heading, root.classList.contains('desktop-client') && active ? active.textContent.trim() : name);
  }
  function formatVersion() {
    if (!version) return;
    var raw = version.textContent.trim(), marker = raw.indexOf('公共权限组件');
    var first = (marker < 0 ? raw : raw.slice(0, marker)).replace(/[\s·]+$/, '');
    var second = marker < 0 ? '' : raw.slice(marker).replace(/^公共权限组件\s*/, '');
    var lines = version.querySelectorAll(':scope > .workbench-version-line');
    if (lines.length === (marker < 0 ? 1 : 2) && lines[0].textContent === first &&
        (marker < 0 || lines[1].textContent === '公共权限组件 ' + second)) return;
    if (versionObserver) versionObserver.disconnect();
    var one = doc.createElement('span'); one.className = 'workbench-version-line'; one.textContent = first;
    version.replaceChildren(one);
    if (marker >= 0) {
      var two = doc.createElement('span'); two.className = 'workbench-version-line'; two.textContent = '公共权限组件 ';
      if (helperVersion) { helperVersion.textContent = second; two.appendChild(helperVersion); }
      else two.appendChild(doc.createTextNode(second));
      version.appendChild(two);
    }
    if (versionObserver) versionObserver.observe(version, {subtree:true, childList:true, characterData:true});
  }
  root.querySelectorAll('.workbench-subnav').forEach(function (nav) {
    var container = nav.parentNode;
    function choose(button) {
      nav.querySelectorAll('[data-section]').forEach(function (item) { item.setAttribute('aria-selected', String(item === button)); });
      container.querySelectorAll('[data-section-panel]').forEach(function (panel) { panel.hidden = panel.dataset.sectionPanel !== button.dataset.section; });
      container.dispatchEvent(new CustomEvent('workbench:section', {bubbles:true, detail:{section:button.dataset.section}}));
    }
    nav.addEventListener('click', function (event) { var button = event.target.closest('[data-section]'); if (button) choose(button); });
    nav.addEventListener('keydown', function (event) {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      var buttons = Array.from(nav.querySelectorAll('[data-section]')), index = buttons.indexOf(event.target);
      if (index < 0) return; event.preventDefault();
      index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
      choose(buttons[index]); buttons[index].focus();
    });
  });
  function decorate() {
    scheduled = 0;
    var frame = root.querySelector('.desktop-frame');
    if (!root.classList.contains('desktop-client') || !frame) {
      if (version && originalParent && version.parentNode !== originalParent) {
        originalParent.insertBefore(version, originalNext && originalNext.parentNode === originalParent ? originalNext : null);
      }
      updateTitle(); return;
    }
    var nav = frame.querySelector(':scope > .tabs');
    if (!nav) {
      nav = doc.createElement('nav'); nav.className = 'tabs workbench-single-nav';
      nav.setAttribute('aria-label', '功能页面');
      var tab = doc.createElement('button'); tab.type = 'button'; tab.className = 'tab active';
      tab.textContent = '概览'; tab.setAttribute('aria-current', 'page');
      tab.addEventListener('click', function () { shell.scrollTop = 0; });
      nav.appendChild(tab); frame.insertBefore(nav, shell);
    }
    var oldBar = frame.querySelector(':scope > .workbench-topbar'); if (oldBar) oldBar.remove();
    if (version && version.parentNode !== nav) {
      version.classList.add('workbench-version'); nav.appendChild(version);
    }
    if (!navObserver) {
      navObserver = new MutationObserver(updateTitle);
      navObserver.observe(nav, {subtree: true, attributes: true, attributeFilter: ['class']});
    }
    updateTitle();
  }
  var observer = new MutationObserver(function () {
    if (!scheduled) scheduled = global.requestAnimationFrame(decorate);
  });
  observer.observe(root, {attributes: true, attributeFilter: ['class']});
  if (version) {
    versionObserver = new MutationObserver(formatVersion); formatVersion();
    versionObserver.observe(version, {subtree:true, childList:true, characterData:true});
  }
  decorate();
  function dispose() {
    refreshDisposed = true;
    refreshRuns.forEach(function (state) { if (state.release) state.release(); });
    observer.disconnect(); if (navObserver) navObserver.disconnect(); if (versionObserver) versionObserver.disconnect();
    if (scheduled) global.cancelAnimationFrame(scheduled);
    if (themeMedia.removeEventListener) themeMedia.removeEventListener('change', applyTheme); else themeMedia.removeListener(applyTheme);
    global.removeEventListener('storage', themeStorage);
    global.removeEventListener('unmount', dispose); global.removeEventListener('pagehide', dispose);
  }
  global.addEventListener('unmount', dispose); global.addEventListener('pagehide', dispose);
})(window);
