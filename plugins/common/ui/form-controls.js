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
