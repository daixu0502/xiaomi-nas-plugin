(function (global) {
  'use strict';

  // micro-app may expose the host's html/body. Never mutate those nodes.
  var nativeDocument = global.document;
  var pluginRoot = nativeDocument.querySelector('[data-minas-plugin="dockermanager"]');
  if (!pluginRoot) throw new Error('Plugin root is missing');
  var pluginBody = pluginRoot.querySelector('.minas-plugin-body');
  var document = new Proxy(nativeDocument, { get: function (target, key) {
    if (key === 'documentElement') return pluginRoot;
    if (key === 'body' || key === 'head') return pluginBody;
    if (key === 'getElementById') return function (id) { return pluginRoot.querySelector('#' + global.CSS.escape(id)); };
    if (key === 'querySelector' || key === 'querySelectorAll' || key === 'addEventListener' || key === 'removeEventListener') return pluginRoot[key].bind(pluginRoot);
    var value = target[key]; return typeof value === 'function' ? value.bind(target) : value;
  }});
  if (pluginRoot.parentElement === nativeDocument.body && !global.__MICRO_APP_ENVIRONMENT__ && !global.microApp) {
    nativeDocument.body.style.margin = '0';
    pluginRoot.style.minHeight = '100vh';
    if (/SmartStorage|Electron/i.test(global.navigator.userAgent || '')) pluginRoot.style.height = '100vh';
  }


  if ((global.navigator && /SmartStorage|Electron/i.test(global.navigator.userAgent || '')) ||
      global.__MICRO_APP_ENVIRONMENT__ ||
      (global.microApp && typeof global.microApp.dispatch === 'function')) {
    document.documentElement.classList.add('desktop-client');
  }

  // The desktop app embeds plugins in micro-app; viewport-fixed UI escapes its window.
  function installDesktopLayout(shell, tabs) {
    var parent = shell.parentElement;
    var host = parent;
    while (host) {
      var tag = (host.tagName || '').toLowerCase();
      if (/^micro-app(?:-|$)/.test(tag) && !/^micro-app-(body|head|html)$/.test(tag)) break;
      host = host.parentElement || (host.getRootNode && host.getRootNode().host) || null;
    }
    var frame = document.createElement('div');
    frame.className = 'desktop-frame';
    parent.insertBefore(frame, shell);
    if (tabs) frame.appendChild(tabs);
    frame.appendChild(shell);
    // Hoist overlays out of scrolling content, including native dialogs and toasts.
    var overlays = 'dialog,.modal-backdrop,.modal,.busy,.toast,.mi-picker-backdrop,.mi-picker-sheet,.mi-confirm-backdrop,.mi-confirm-dialog,.sheet,.dialog,.backdrop,.detail-sheet';
    Array.prototype.slice.call(parent.querySelectorAll(overlays)).forEach(function (child) {
      var owner = child.parentElement.closest(overlays);
      if (!owner || !pluginRoot.contains(owner)) frame.appendChild(child);
    });
    if (parent === pluginBody) {
      parent.style.setProperty('min-height', '0', 'important');
      parent.style.setProperty('height', '100%', 'important');
      parent.style.setProperty('margin', '0', 'important');
      parent.style.setProperty('overflow', 'hidden', 'important');
    }
    var boundary = host || document.documentElement;
    var outer = host && host.parentElement;
    var pending = 0;
    function updateSize() {
      pending = 0;
      if (!frame.isConnected) return;
      var height = boundary.clientHeight;
      // The micro-app is the boundary; an absolutely positioned host may have a short flow parent.
      if (!height && outer && outer.clientHeight > 0) height = outer.clientHeight;
      if (height > 0) frame.style.height = height + 'px';
      frame.classList.toggle('desktop-compact', frame.clientWidth < 680);
    }
    function scheduleSize() {
      if (!pending) pending = global.requestAnimationFrame(updateSize);
    }
    var observer = global.ResizeObserver ? new global.ResizeObserver(scheduleSize) : null;
    if (observer) {
      observer.observe(boundary);
      if (outer) observer.observe(outer);
      observer.observe(frame);
    }
    global.addEventListener('resize', scheduleSize);
    function dispose() {
      if (observer) observer.disconnect();
      if (pending) global.cancelAnimationFrame(pending);
      global.removeEventListener('resize', scheduleSize);
      global.removeEventListener('unmount', dispose);
      global.removeEventListener('pagehide', dispose);
    }
    global.addEventListener('unmount', dispose);
    global.addEventListener('pagehide', dispose);
    updateSize();
    return frame;
  }

  // showModal() enters the browser top layer and ignores the micro-app boundary.
  // Desktop uses a non-top-layer dialog, a local backdrop and local focus isolation.
  function openDialog(dialog) {
    var frame = pluginRoot.querySelector('.desktop-frame');
    if (!frame) { dialog.showModal(); return; }
    if (dialog.open) return;
    var previous = nativeDocument.activeElement;
    var backdrop = nativeDocument.createElement('div');
    backdrop.className = 'plugin-dialog-backdrop';
    backdrop.setAttribute('aria-hidden', 'true');
    frame.appendChild(backdrop);
    frame.appendChild(dialog);
    var backgrounds = Array.prototype.slice.call(frame.children).filter(function (el) { return el.matches('.shell,.tabs'); });
    var inertBefore = backgrounds.map(function (el) { return el.inert; });
    backgrounds.forEach(function (el) { el.inert = true; });
    function keys(event) {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); dialog.close('cancel');
      } else if (event.key === 'Tab') {
        var controls = Array.prototype.slice.call(dialog.querySelectorAll('button,input,select,textarea,a[href],[tabindex]')).filter(function (el) {
          return !el.disabled && el.tabIndex >= 0 && el.getClientRects().length;
        });
        var first = controls[0], last = controls[controls.length - 1];
        if (!first) { event.preventDefault(); dialog.focus(); }
        else if (event.shiftKey && nativeDocument.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && nativeDocument.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
    function cleanup() {
      backdrop.remove(); dialog.removeEventListener('keydown', keys);
      backgrounds.forEach(function (el, i) { el.inert = inertBefore[i]; });
      if (previous && previous.isConnected && pluginRoot.contains(previous)) previous.focus({ preventScroll: true });
    }
    dialog.addEventListener('keydown', keys);
    dialog.addEventListener('close', cleanup, { once: true });
    try { dialog.show(); } catch (error) { dialog.removeEventListener('close', cleanup); cleanup(); throw error; }
  }

  function installDesktopWheelSupport() {
    var shell = document.querySelector('.shell');
    if (!document.documentElement.classList.contains('desktop-client') || !shell || shell.getAttribute('data-desktop-wheel') === '1') return;
    shell.setAttribute('data-desktop-wheel', '1');
    var tabs = shell.querySelector('.tabs');
    var frame = installDesktopLayout(shell, tabs);
    if (tabs) tabs.addEventListener('click', function (event) {
      if (event.target.closest('.tab')) global.requestAnimationFrame(function () { shell.scrollTop = 0; });
    });
    // Control sizes are owned by scoped palette.css, including dynamically added controls.
    global.addEventListener('wheel', function (event) {
      if (event.ctrlKey || !event.deltaY || !shell.contains(event.target)) return;
      var node = event.target, scroller = null, direction = event.deltaY > 0 ? 1 : -1;
      while (node && node !== document.body) {
        if (node.scrollHeight > node.clientHeight + 1) {
          var overflowY = global.getComputedStyle(node).overflowY;
          var canScroll = direction > 0 ? node.scrollTop < node.scrollHeight - node.clientHeight - 1 : node.scrollTop > 1;
          if (/auto|scroll|overlay/.test(overflowY) && canScroll) { scroller = node; break; }
        }
        if (node === shell) break;
        node = node.parentElement;
      }
      if (!scroller && shell.scrollHeight > shell.clientHeight + 1) scroller = shell;
      if (!scroller) return;
      var delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? scroller.clientHeight : 1);
      scroller.scrollTop += delta;
      event.preventDefault();
      event.stopPropagation();
    }, { capture: true, passive: false });
  }

  installDesktopWheelSupport();

  var deviceInfo = null;
  var pendingInfo = null;

  function isDesktopClient() {
    var result = Boolean(global.__MICRO_APP_ENVIRONMENT__ ||
      (global.microApp && typeof global.microApp.dispatch === 'function' &&
        typeof global.microApp.addDataListener === 'function'));
    if (result) {
      document.documentElement.classList.add('desktop-client');
      installDesktopWheelSupport();
    }
    return result;
  }

  function normalizeInfo(response) {
    var value = response && (response.deviceInfo || response.data || response);
    if (value && value.data && !value.cgiToken) value = value.data;
    if (!value || !value.cgiPort || !value.cgiToken) return null;
    return value;
  }

  function loadDeviceInfo(forceRefresh) {
    if (!isDesktopClient()) return Promise.resolve(null);
    if (deviceInfo && !forceRefresh) return Promise.resolve(deviceInfo);
    if (pendingInfo && !forceRefresh) return pendingInfo;
    pendingInfo = new Promise(function (resolve) {
      var settled = false;
      var timer = global.setTimeout(function () { finish(null); }, 5000);
      function finish(response) {
        if (settled) return;
        settled = true;
        global.clearTimeout(timer);
        var info = normalizeInfo(response);
        if (info) deviceInfo = info;
        resolve(info);
      }
      try {
        var bridge = global.microApp;
        if (!bridge || typeof bridge.dispatch !== 'function') return finish(null);
        if (typeof bridge.removeDataListener === 'function') bridge.removeDataListener();
        bridge.addDataListener(function (response) {
          if (response && response.cmd && response.cmd !== 'getDeviceInfo') return;
          finish(response);
        });
        bridge.dispatch({ params: { cmd: 'getDeviceInfo', type: forceRefresh ? 'refresh' : '' } });
      } catch (error) { finish(null); }
    }).then(function (info) { pendingInfo = null; return info; });
    return pendingInfo;
  }

  function pluginUserId() {
    var sources = [global.location.pathname, global.location.href, document.referrer || ''];
    for (var i = 0; i < sources.length; i += 1) {
      var match = String(sources[i]).match(/\/plugin\/(?:u)?(\d+)(?:\/|$)/);
      if (match) return match[1];
    }
    return '';
  }

  function withAuthorization(options, token) {
    var result = {}, headers = {};
    Object.keys(options || {}).forEach(function (key) { result[key] = options[key]; });
    if (global.Headers && result.headers instanceof global.Headers) result.headers.forEach(function (value, key) { headers[key] = value; });
    else Object.keys(result.headers || {}).forEach(function (key) { headers[key] = result.headers[key]; });
    headers.Authorization = token;
    result.headers = headers;
    return result;
  }

  function request(settings) {
    var query = settings.action ? '?action=' + encodeURIComponent(settings.action) : '';
    var relativeUrl = settings.cgi + query;
    var originalOptions = settings.options || {};
    function send(info) {
      var uid = pluginUserId();
      var url = uid ? '/plugin/' + encodeURIComponent(uid) + '/' + encodeURIComponent(settings.plugin) + '/' + relativeUrl : relativeUrl;
      return global.fetch(url, info ? withAuthorization(originalOptions, info.cgiToken) : originalOptions);
    }
    return loadDeviceInfo(false).then(send).then(function (response) {
      if (!isDesktopClient() || (response.status !== 401 && response.status !== 403)) return response;
      deviceInfo = null;
      return loadDeviceInfo(true).then(send);
    });
  }

  global.XiaomiPluginClient = { request: request, document: document, openDialog: openDialog };
})(window);

/* BEGIN SHARED FORM CONTROLS */
/* Shared scoped form controls. The native fields remain the source of truth. */
(function (global) {
  'use strict';
  var client = global.XiaomiPluginClient;
  if (!client || client.enhanceSelect) return;
  var doc = client.document, root = doc.documentElement, nativeDoc = root.ownerDocument;
  var controls = new Map(), serial = 0, current = null, scheduled = false, refreshCurrent = false;
  function element(tag, cls, text) {
    var el = nativeDoc.createElement(tag); if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text; return el;
  }
  function label(select) {
    var explicit = select.getAttribute('aria-label');
    if (explicit) return explicit;
    var labels = Array.from(select.labels || []);
    if (labels.length) {
      var copy = labels[0].cloneNode(true);
      copy.querySelectorAll('select,button,.technical,small,.hint,.help').forEach(function (el) { el.remove(); });
      return copy.textContent.trim() || '选择选项';
    }
    return '选择选项';
  }
  function selected(select) {
    var values = Array.from(select.selectedOptions).map(function (o) { return o.textContent.trim(); });
    return values.join('、') || select.getAttribute('placeholder') || '请选择';
  }
  function sync(select) {
    var state = controls.get(select); if (!state) return;
    var value = selected(select), title = label(select), trigger = state.trigger;
    if (state.text.textContent !== value) state.text.textContent = value;
    if (trigger.title !== value) trigger.title = value;
    if (trigger.getAttribute('aria-label') !== title + '：' + value) trigger.setAttribute('aria-label', title + '：' + value);
    var disabled = select.matches(':disabled');
    if (trigger.disabled !== disabled) trigger.disabled = disabled;
    if (trigger.hidden !== select.hidden) trigger.hidden = select.hidden;
    if (select.nextElementSibling !== trigger) select.after(trigger);
    if (current && current.select === select && (disabled || !select.isConnected || select.hidden)) close();
  }
  function close(restore) {
    if (!current) return;
    var state = current; current = null;
    state.layer.remove(); state.trigger.setAttribute('aria-expanded', 'false');
    state.trigger.removeAttribute('aria-controls');
    if (state.pending) global.cancelAnimationFrame(state.pending);
    if (state.resize) state.resize.disconnect();
    nativeDoc.removeEventListener('focusin', state.focus, true);
    nativeDoc.removeEventListener('pointerdown', state.outside, true);
    nativeDoc.removeEventListener('scroll', state.schedule, true);
    global.removeEventListener('resize', state.schedule);
    if (global.visualViewport) {
      global.visualViewport.removeEventListener('resize', state.schedule);
      global.visualViewport.removeEventListener('scroll', state.schedule);
    }
    if (restore !== false && state.trigger.isConnected && !state.trigger.disabled) state.trigger.focus({preventScroll:true});
  }
  function position(state) {
    if (current !== state) return;
    state.pending = 0;
    if (!state.trigger.isConnected || !state.trigger.getClientRects().length) { close(false); return; }
    var anchor = state.trigger.getBoundingClientRect(), plane = state.layer.getBoundingClientRect();
    var viewport = global.visualViewport;
    var bounds = {
      left:Math.max(plane.left, viewport ? viewport.offsetLeft : 0),
      top:Math.max(plane.top, viewport ? viewport.offsetTop : 0),
      right:Math.min(plane.right, (viewport ? viewport.offsetLeft + viewport.width : global.innerWidth)),
      bottom:Math.min(plane.bottom, (viewport ? viewport.offsetTop + viewport.height : global.innerHeight))
    };
    if (state.frame) {
      var frame = state.frame.getBoundingClientRect();
      bounds.left = Math.max(bounds.left, frame.left); bounds.top = Math.max(bounds.top, frame.top);
      bounds.right = Math.min(bounds.right, frame.right); bounds.bottom = Math.min(bounds.bottom, frame.bottom);
    }
    if (anchor.bottom <= bounds.top || anchor.top >= bounds.bottom || anchor.right <= bounds.left || anchor.left >= bounds.right) { close(false); return; }
    // Convert viewport coordinates to the portal's local coordinates. This also
    // handles Xiaomi's CSS zoom and a transformed containing native dialog.
    var sx = plane.width / (state.layer.offsetWidth || 1), sy = plane.height / (state.layer.offsetHeight || 1);
    if (sx <= 0 || sy <= 0) { close(false); return; }
    var gap = 6 * sy, edge = 8 * sx, availableWidth = Math.max(0, bounds.right - bounds.left - 2 * edge);
    var width = Math.min(Math.max(anchor.width, 240 * sx), availableWidth);
    var left = Math.max(bounds.left + edge, Math.min(anchor.left, bounds.right - edge - width));
    var below = Math.max(0, bounds.bottom - 8 * sy - anchor.bottom - gap);
    var above = Math.max(0, anchor.top - gap - bounds.top - 8 * sy);
    var panel = state.panel;
    panel.style.width = width / sx + 'px';
    var panelStyle = global.getComputedStyle(panel);
    var content = Array.from(panel.children).filter(function (el) { return !el.hidden; });
    var naturalHeight = content.reduce(function (sum, el) { return sum + (el === state.list ? el.scrollHeight : el.offsetHeight); }, 0) +
      Math.max(0, content.length - 1) * (parseFloat(panelStyle.rowGap) || 0) +
      parseFloat(panelStyle.paddingTop) + parseFloat(panelStyle.paddingBottom) + parseFloat(panelStyle.borderTopWidth) + parseFloat(panelStyle.borderBottomWidth);
    var desired = Math.min(360, naturalHeight) * sy;
    var up = below < desired && above > below;
    var available = up ? above : below;
    if (available < 60 * sy || width < 80 * sx) { close(false); return; }
    panel.style.maxHeight = Math.min(360, available / sy) + 'px';
    panel.style.left = (left - plane.left) / sx + 'px';
    var height = panel.getBoundingClientRect().height;
    panel.style.top = ((up ? anchor.top - gap - height : anchor.bottom + gap) - plane.top) / sy + 'px';
    panel.dataset.placement = up ? 'above' : 'below';
    panel.style.visibility = 'visible';
  }
  function open(select) {
    sync(select); if (select.matches(':disabled') || select.hidden) return;
    if (current && current.select === select) { close(); return; }
    close(false);
    var control = controls.get(select), frame = root.querySelector('.desktop-frame');
    // A panel inside a native modal must stay in that dialog's top layer.
    var dialog = select.closest('dialog[open]');
    var parent = dialog || frame || doc.body;
    var layer = element('div', 'ui-choice-layer'), panel = element('section', 'ui-choice-panel');
    if (dialog || frame) layer.classList.add('ui-choice-local');
    panel.setAttribute('aria-label', label(select));
    var list = element('div', 'ui-choice-options'); list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', label(select));
    list.id = 'ui-choice-options-' + (++serial); control.trigger.setAttribute('aria-controls', list.id);
    if (select.multiple) list.setAttribute('aria-multiselectable', 'true');
    panel.appendChild(list);
    var footer = element('div', 'ui-choice-footer'), count = element('span');
    footer.appendChild(count);
    var done = element('button', 'ui-choice-done', '完成'); done.type = 'button'; done.hidden = !select.multiple; footer.appendChild(done); panel.appendChild(footer);
    layer.appendChild(panel);
    footer.hidden = !select.multiple;
    parent.appendChild(layer);
    control.trigger.setAttribute('aria-expanded', 'true');
    function inside(event) { var path = event.composedPath ? event.composedPath() : []; return path.includes(panel) || path.includes(control.trigger) || panel.contains(event.target) || control.trigger.contains(event.target); }
    function outside(event) { if (!inside(event)) close(false); }
    var state = {select:select, trigger:control.trigger, layer:layer, panel:panel, list:list, frame:frame, focus:outside, outside:outside, render:render, pending:0};
    function schedule() { if (current === state && !state.pending) state.pending = global.requestAnimationFrame(function () { position(state); }); }
    state.schedule = schedule; current = state;
    nativeDoc.addEventListener('focusin', outside, true);
    nativeDoc.addEventListener('pointerdown', outside, true);
    nativeDoc.addEventListener('scroll', schedule, {capture:true, passive:true});
    global.addEventListener('resize', schedule);
    if (global.visualViewport) { global.visualViewport.addEventListener('resize', schedule); global.visualViewport.addEventListener('scroll', schedule); }
    if (global.ResizeObserver) {
      state.resize = new global.ResizeObserver(schedule);
      state.resize.observe(control.trigger); state.resize.observe(parent);
    }
    function render() {
      var group = null, visible = 0;
      list.replaceChildren();
      Array.from(select.options).forEach(function (option) {
        if (option.hidden || option.parentElement.hidden) return;
        if (option.parentElement.tagName === 'OPTGROUP' && group !== option.parentElement) {
          group = option.parentElement; list.appendChild(element('div', 'ui-choice-group', group.label));
        }
        var row = element('button', 'ui-choice-option'); row.type = 'button'; row.tabIndex = -1; row.setAttribute('role', 'option'); row.setAttribute('aria-selected', String(option.selected));
        row.disabled = option.disabled || (option.parentElement.tagName === 'OPTGROUP' && option.parentElement.disabled);
        row.append(element('span', 'ui-choice-copy', option.textContent.trim()), element('span', 'ui-choice-mark', option.selected ? '✓' : ''));
        row.addEventListener('click', function () {
          if (select.matches(':disabled') || !select.isConnected || row.disabled) { close(); return; }
          if (select.multiple) option.selected = !option.selected;
          else select.selectedIndex = option.index;
          if (!select.multiple) close(); else render();
          sync(select);
          select.dispatchEvent(new Event('input', {bubbles:true}));
          select.dispatchEvent(new Event('change', {bubbles:true}));
        });
        list.appendChild(row); visible++;
      });
      if (!visible) list.appendChild(element('div', 'ui-choice-empty', '没有匹配的选项'));
      count.textContent = visible + ' 个选项' + (select.multiple ? ' · 已选 ' + select.selectedOptions.length + ' 项' : ' · 点击选择');
      schedule();
    }
    done.onclick = function () { close(); };
    layer.addEventListener('keydown', function (event) {
      // Keep parent plugin shortcuts and other open dialogs from handling this key.
      event.stopPropagation();
      if (event.key === 'Escape') { event.preventDefault(); close(); return; }
      var options = Array.from(list.querySelectorAll('button:not(:disabled)'));
      if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
        event.preventDefault(); var index = options.indexOf(nativeDoc.activeElement), next;
        if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = options.length - 1;
        else next = (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
        if (options[next]) options[next].focus();
      } else if (event.key === 'Tab') {
        // Return the tab sequence to the original field, then let the browser
        // move to the next/previous control. A dropdown is not a modal dialog.
        close();
      }
    });
    render();
    position(state);
    if (current !== state) return;
    var initial = list.querySelector('[aria-selected="true"]:not(:disabled)') || list.querySelector('button:not(:disabled)') || control.trigger;
    initial.focus({preventScroll:true});
    if (list.contains(initial)) list.scrollTop = Math.max(0, initial.offsetTop - list.offsetTop - (list.clientHeight - initial.offsetHeight) / 2);
  }
  function enhance(select) {
    if (controls.has(select)) { sync(select); return; }
    if (select.getAttribute('data-native-select') === 'true') return;
    var trigger = element('button', 'ui-select-trigger'), text = element('span', 'ui-select-value'); trigger.type = 'button';
    trigger.id = (select.id || 'ui-select') + '-choice-' + (++serial);
    trigger.setAttribute('aria-haspopup', 'listbox'); trigger.setAttribute('aria-expanded', 'false');
    trigger.append(text, element('span', 'ui-select-chevron'));
    controls.set(select, {trigger:trigger, text:text, tabindex:select.getAttribute('tabindex'), aria:select.getAttribute('aria-hidden'), descriptors:{}});
    select.classList.add('ui-select-native'); select.tabIndex = -1; select.setAttribute('aria-hidden', 'true'); select.after(trigger);
    var state = controls.get(select);
    // App assignments do not emit change events. Hook only this field, never global prototypes.
    ['value','selectedIndex'].forEach(function (name) {
      var original = Object.getOwnPropertyDescriptor(select, name);
      if (original && !original.configurable) return;
      var proto = Object.getPrototypeOf(select), descriptor;
      while (proto && !descriptor) { descriptor = Object.getOwnPropertyDescriptor(proto, name); proto = Object.getPrototypeOf(proto); }
      if (!descriptor || !descriptor.set || !descriptor.get) return;
      state.descriptors[name] = original;
      Object.defineProperty(select, name, {configurable:true, get:function () { return descriptor.get.call(this); }, set:function (value) { descriptor.set.call(this, value); sync(this); }});
    });
    select._miSync = function () { sync(select); };
    trigger.addEventListener('click', function (event) { event.preventDefault(); open(select); });
    trigger.addEventListener('keydown', function (event) { if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); open(select); } });
    // Clicking a field's label activates its native select. Route that activation
    // to the same menu instead of focusing an invisible native control.
    select.addEventListener('click', function (event) { event.preventDefault(); open(select); });
    select.addEventListener('change', select._miSync); select.addEventListener('input', select._miSync);
    sync(select);
  }
  function scan() {
    scheduled = false;
    controls.forEach(function (state, select) {
      if (!root.contains(select)) { if (current && current.select === select) close(false); state.trigger.remove(); controls.delete(select); }
    });
    root.querySelectorAll('select').forEach(enhance);
    if (current && refreshCurrent) current.render();
    if (current) current.schedule();
    refreshCurrent = false;
  }
  var observer = new MutationObserver(function (records) {
    if (current && records.some(function (record) { return record.target === current.select || current.select.contains(record.target); })) refreshCurrent = true;
    // Ignore our own panel and trigger updates to avoid observation loops.
    if (records.every(function (record) { var el = record.target.nodeType === 1 ? record.target : record.target.parentElement; return el && el.closest('.ui-choice-layer,.ui-select-trigger'); })) return;
    if (!scheduled) { scheduled = true; queueMicrotask(scan); }
  });
  observer.observe(root, {subtree:true, childList:true, characterData:true, attributes:true, attributeFilter:['disabled','selected','hidden','label','aria-label']});
  client.enhanceSelect = enhance; client.syncFormControls = scan;
  scan();
  function dispose() { close(false); observer.disconnect(); controls.clear(); }
  global.addEventListener('unmount', dispose, {once:true}); global.addEventListener('pagehide', dispose, {once:true});
})(window);
/* END SHARED FORM CONTROLS */

/* BEGIN SHARED MOBILE NAVIGATION */
/* Event-driven mobile navigation. No timers or network polling. */
(function (global) {
  'use strict';
  var client=global.XiaomiPluginClient;
  if (!client) return;
  var root=client.document.documentElement, nav=root.querySelector('.tabs');
  if (!nav || nav.dataset.mobileNavigation==='ready') return;
  nav.dataset.mobileNavigation='ready';
  var originalLabel=nav.getAttribute('aria-label') || '功能导航';
  var frame=0, reveal=false, smooth=false, activeBefore=null, disposed=false;
  function visibleButtons() {
    return Array.from(nav.querySelectorAll('.tab')).filter(function (button) { return !button.hidden && button.getClientRects().length && !button.disabled; });
  }
  function refresh() {
    frame=0;
    if (disposed || !nav.isConnected) return;
    if (root.classList.contains('desktop-client')) { reveal=false; smooth=false; return; }
    var buttons=visibleButtons(), active=buttons.find(function (button) { return button.classList.contains('active'); });
    buttons.forEach(function (button) {
      var wanted=button===active?'page':null;
      if (wanted && button.getAttribute('aria-current')!==wanted) button.setAttribute('aria-current',wanted);
      else if (!wanted && button.hasAttribute('aria-current')) button.removeAttribute('aria-current');
    });
    if (active!==activeBefore) reveal=true;
    activeBefore=active;
    if (reveal && active) {
      var boundary=nav.getBoundingClientRect(), item=active.getBoundingClientRect(), scale=boundary.width/(nav.offsetWidth || 1), next=nav.scrollLeft;
      var inset=6*scale;
      if (item.left<boundary.left+inset) next-=(boundary.left+inset-item.left)/scale;
      else if (item.right>boundary.right-inset) next+=(item.right-boundary.right+inset)/scale;
      next=Math.max(0,Math.min(next,nav.scrollWidth-nav.clientWidth));
      if (Math.abs(next-nav.scrollLeft)>1) nav.scrollTo({left:next,behavior:smooth&&!global.matchMedia('(prefers-reduced-motion: reduce)').matches?'smooth':'auto'});
    }
    reveal=false; smooth=false;
    var overflow=nav.scrollWidth>nav.clientWidth+1;
    nav.dataset.scrollLeft=String(overflow&&nav.scrollLeft>1);
    nav.dataset.scrollRight=String(overflow&&nav.scrollLeft<nav.scrollWidth-nav.clientWidth-1);
    var label=originalLabel+(overflow?'，左右滑动查看更多功能':'');
    if (nav.getAttribute('aria-label')!==label) nav.setAttribute('aria-label',label);
  }
  function schedule(shouldReveal,shouldSmooth) {
    if (disposed) return;
    reveal=reveal||!!shouldReveal; smooth=smooth||!!shouldSmooth;
    if (!frame) frame=global.requestAnimationFrame(refresh);
  }
  function click(event) { if (event.target.closest('.tab')) schedule(true,true); }
  function scroll() { schedule(false,false); }
  function resize() { schedule(true,false); }
  function keys(event) {
    if (root.classList.contains('desktop-client') || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    var current=event.target.closest('.tab'), buttons=visibleButtons(), index=buttons.indexOf(current);
    if (index<0) return;
    event.preventDefault();
    var next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;
    buttons[next].focus({preventScroll:true}); buttons[next].click();
  }
  // Content swipe gestures must not activate another page while a user pans the menu.
  function gesture(event) { event.stopPropagation(); }
  nav.addEventListener('click',click);
  nav.addEventListener('keydown',keys);
  nav.addEventListener('scroll',scroll,{passive:true});
  nav.addEventListener('touchstart',gesture,{passive:true});
  nav.addEventListener('touchend',gesture,{passive:true});
  global.addEventListener('resize',resize);
  var mutation=new MutationObserver(function () { schedule(true,false); });
  mutation.observe(nav,{subtree:true,childList:true,attributes:true,attributeFilter:['class','hidden']});
  var rootMutation=new MutationObserver(resize);rootMutation.observe(root,{attributes:true,attributeFilter:['class']});
  var observer=global.ResizeObserver?new global.ResizeObserver(resize):null;
  if (observer) observer.observe(nav);
  if (root.ownerDocument.fonts) root.ownerDocument.fonts.ready.then(resize);
  schedule(true,false);
  function dispose() {
    disposed=true;if(frame)global.cancelAnimationFrame(frame);mutation.disconnect();rootMutation.disconnect();if(observer)observer.disconnect();
    nav.removeEventListener('click',click);nav.removeEventListener('keydown',keys);nav.removeEventListener('scroll',scroll);
    nav.removeEventListener('touchstart',gesture);nav.removeEventListener('touchend',gesture);global.removeEventListener('resize',resize);
    global.removeEventListener('pagehide',dispose);global.removeEventListener('unmount',dispose);
  }
  global.addEventListener('pagehide',dispose);global.addEventListener('unmount',dispose);
})(window);
/* END SHARED MOBILE NAVIGATION */

/* BEGIN SHARED WORKBENCH */
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
/* END SHARED WORKBENCH */
