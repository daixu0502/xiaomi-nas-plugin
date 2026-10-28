/* Shared workbench chrome. Keep the original version node and its live binding. */
(function (global) {
  'use strict';
  var client = global.XiaomiPluginClient;
  if (!client) return;
  var doc = client.document, root = doc.documentElement;
  var shell = root.querySelector('.shell'), heading = root.querySelector('.plugin-header h1');
  if (!shell || !heading) return;
  var name = heading.textContent.trim(), navObserver = null, scheduled = 0;
  var version = shell.querySelector('footer');
  var originalNext = version && version.nextSibling, originalParent = version && version.parentNode;
  var helperVersion = version && version.querySelector('#helper-version');
  var versionObserver = null;
  root.classList.add('workbench-ui');
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
    observer.disconnect(); if (navObserver) navObserver.disconnect(); if (versionObserver) versionObserver.disconnect();
    if (scheduled) global.cancelAnimationFrame(scheduled);
    global.removeEventListener('unmount', dispose); global.removeEventListener('pagehide', dispose);
  }
  global.addEventListener('unmount', dispose); global.addEventListener('pagehide', dispose);
})(window);
