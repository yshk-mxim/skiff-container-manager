// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/*
 * SKIFF UI widgets — a tiny, CSP-safe DOM primitives library.
 *
 * All helpers here build DOM via createElement + textContent (never innerHTML
 * with interpolated data), so there is a single, auditable XSS boundary that
 * every call site inherits. If a caller needs raw HTML for a trusted static
 * template (e.g., the wizard's card layout), they can still set innerHTML
 * directly at the call site — the library doesn't get in the way.
 *
 * Naming: the namespace is `window.UI` so app.js can reference it as `UI.el()`
 * etc. without a module system. One global object, every primitive hangs
 * off it. No dependencies beyond the browser.
 *
 * Design constraints:
 *   - No innerHTML with user/server data. Every text assignment is textContent.
 *   - No new globals except `UI`. Helpers are properties of that one object.
 *   - Every helper returns the root DOM node so caller can .append / decorate.
 *   - Zero framework: no reactivity, no virtual DOM. Keep it boring.
 */
"use strict";

(function(root) {
  // ── CSP-safe inline style helper ────────────────────────────────────
  // Strict CSP (`style-src 'self'`, no 'unsafe-inline') blocks every form
  // of element-level style assignment from JS: `element.style.X = "..."`,
  // `element.style.cssText = "..."`, `element.setAttribute("style", ...)`,
  // and inline `style=""` attributes in HTML. CSP nonces only cover
  // `<style>` elements, not inline style attributes, so they don't help.
  //
  // CSSOM mutations on an already-loaded same-origin stylesheet (here
  // `/static/styles.css`) are NOT subject to `style-src`: the sheet was
  // permitted at load time, and `CSSStyleSheet.insertRule` /
  // `CSSStyleRule.style.setProperty` mutate that loaded sheet in place
  // without introducing new inline content. So we route every JS-set
  // style through a unique `_csp_N` class whose rule lives in styles.css.
  //
  //   UI.setStyle(el, "color:red; padding:8px")  → replace cssText
  //   UI.setStyle(el, "color", "red")            → set one property
  //
  // Each element gets one `_csp_N` class on first call; subsequent calls
  // mutate that one rule. WeakMap'd so the element→className lookup
  // doesn't pin the element from GC. The rule itself remains in the sheet
  // for the page's lifetime (bounded for a SPA that periodically reloads).
  var _cspStyleSheet = null;
  var _cspClassMap = new WeakMap();
  var _cspClassRules = new Map();
  var _cspClassCounter = 0;

  function _cspGetSheet() {
    if (_cspStyleSheet) return _cspStyleSheet;
    for (var i = 0; i < document.styleSheets.length; i++) {
      var s = document.styleSheets[i];
      if (s.href && s.href.indexOf('/static/styles.css') !== -1) {
        _cspStyleSheet = s;
        return _cspStyleSheet;
      }
    }
    // Fallback: first same-origin sheet whose cssRules we can read.
    // Accessing cssRules throws SecurityError on cross-origin sheets.
    for (var j = 0; j < document.styleSheets.length; j++) {
      try {
        void document.styleSheets[j].cssRules;
        _cspStyleSheet = document.styleSheets[j];
        return _cspStyleSheet;
      } catch (e) { /* cross-origin sheet, skip */ }
    }
    return null;
  }

  function _cspGetOrCreateRule(node) {
    var className = _cspClassMap.get(node);
    if (className) {
      var existing = _cspClassRules.get(className);
      if (existing) return existing;
    }
    var sheet = _cspGetSheet();
    if (!sheet) return null;
    if (!className) {
      className = '_csp_' + (++_cspClassCounter);
      node.classList.add(className);
      _cspClassMap.set(node, className);
    }
    var idx = sheet.insertRule('.' + className + ' {}', sheet.cssRules.length);
    var rule = sheet.cssRules[idx];
    _cspClassRules.set(className, rule);
    return rule;
  }

  /**
   * Apply CSS to an element under a strict `style-src 'self'` CSP (no
   * `'unsafe-inline'`). Routes the assignment through a per-element
   * `_csp_N` rule inserted into /static/styles.css via the CSSOM API,
   * so it survives a CSP that would block `element.style.X = ...`.
   *
   * Two call shapes:
   *
   *   UI.setStyle(el, "color:red; padding:8px")  → replace cssText
   *   UI.setStyle(el, "color", "red")            → set one property
   *
   * Passing `""` or `null` as the value removes the property. The
   * underlying rule persists for the page lifetime (acceptable for a
   * SPA that periodically navigates / reloads).
   */
  // CSSStyleDeclaration.setProperty expects the CSS property name in
  // kebab-case (`max-width`), but JS callers naturally write the
  // CSSStyleDeclaration camelCase form (`maxWidth`). Auto-convert so
  // both styles work without ceremony.
  function _toKebab(s) {
    return String(s).replace(/[A-Z]/g, function(m) { return '-' + m.toLowerCase(); });
  }

  /**
   * Apply CSS to an element under a strict `style-src 'self'` CSP (no
   * `'unsafe-inline'`). Routes the assignment through a per-element
   * `_csp_N` rule inserted into /static/styles.css via the CSSOM API,
   * so it survives a CSP that would block `element.style.X = ...`.
   *
   * Two call shapes:
   *
   *   UI.setStyle(el, "color:red; padding:8px")  → replace cssText
   *   UI.setStyle(el, "color", "red")            → set one property
   *
   * The single-property form accepts both kebab-case (`max-width`) and
   * camelCase (`maxWidth`) — JS callers naturally write the
   * CSSStyleDeclaration form so the helper auto-kebabs. Passing `""`
   * or `null` as the value removes the property. The underlying rule
   * persists for the page lifetime (acceptable for a SPA that
   * periodically reloads).
   */
  function setStyle(node, propOrCssText, value) {
    if (!node) return;
    var rule = _cspGetOrCreateRule(node);
    if (!rule) return;
    if (value === undefined) {
      // Full cssText replacement.
      rule.style.cssText = propOrCssText;
    } else if (value === '' || value == null) {
      rule.style.removeProperty(_toKebab(propOrCssText));
    } else {
      rule.style.setProperty(_toKebab(propOrCssText), String(value));
    }
  }

  /**
   * Read a style property value previously assigned via `setStyle`.
   * Under strict CSP, `element.style.X` returns `""` (we never write
   * to the inline attribute), so call sites that previously read
   * inline styles must switch to `UI.getStyle`. Falls back to
   * `getComputedStyle` if no `_csp_N` rule has been created yet,
   * so CSS-stylesheet defaults remain observable.
   */
  function getStyle(node, prop) {
    if (!node) return '';
    var className = _cspClassMap.get(node);
    if (className) {
      var rule = _cspClassRules.get(className);
      if (rule) {
        var v = rule.style.getPropertyValue(_toKebab(prop));
        if (v) return v;
      }
    }
    try {
      return window.getComputedStyle(node).getPropertyValue(_toKebab(prop));
    } catch (e) {
      return '';
    }
  }

  /**
   * Build an HTML element with attributes and children in one call.
   *
   *   UI.el('div', {class: 'foo', style: 'margin:4px', data: {id: 42}},
   *     'Some text', UI.el('span', {}, 'more'))
   *
   * Special attribute handling:
   *   - `class`     → element.className
   *   - `style`     → routed through `UI.setStyle` so a `_csp_N` rule
   *                   is inserted into styles.css (CSP-strict safe)
   *   - `dataset`   → attrs.dataset is a dict applied to element.dataset
   *   - `on`        → {click: fn, …} attaches listeners
   *   - `text`      → element.textContent (convenient shorthand)
   *   - `html`      → element.innerHTML (EXPLICIT opt-in; caller guarantees no XSS)
   *   - everything else becomes a plain attribute via setAttribute
   */
  function el(tag, attrs, ...children) {
    var n = document.createElement(tag);
    if (attrs) {
      // Process `class` first so any later `_csp_N` class added by
      // `setStyle` via the `style:` attribute uses classList.add (which
      // it does) rather than fighting an overwriting `n.className = v`.
      if (typeof attrs.class === 'string') { n.className = attrs.class; }
      Object.keys(attrs).forEach(function(k) {
        if (k === 'class') return;  // already handled above
        var v = attrs[k];
        if (v == null || v === false) return;
        if (k === 'style')    { setStyle(n, v); return; }
        if (k === 'text')     { n.textContent = v; return; }
        if (k === 'html')     { n.innerHTML = v; return; }  // deliberate opt-in
        if (k === 'dataset')  { Object.keys(v).forEach(function(dk) { n.dataset[dk] = v[dk]; }); return; }
        if (k === 'on')       { Object.keys(v).forEach(function(evt) { n.addEventListener(evt, v[evt]); }); return; }
        n.setAttribute(k, v);
      });
    }
    children.flat(Infinity).forEach(function(child) {
      if (child == null || child === false) return;
      if (typeof child === 'string' || typeof child === 'number') {
        n.appendChild(document.createTextNode(String(child)));
      } else {
        n.appendChild(child);
      }
    });
    return n;
  }

  /**
   * Inspect-style key/value row. Used in container Inspect, volume Inspect,
   * integrations panel, and every modal that lays out a list of attributes.
   *
   *   UI.kvRow('ID', c.id)
   *   UI.kvRow('Labels', {env: 'prod'})         // object is JSON-stringified
   *   UI.kvRow('Status', UI.el('span', {...}))  // a DOM node stays as-is
   */
  function kvRow(label, value) {
    var v;
    if (value && typeof value === 'object' && !(value instanceof Node)) {
      v = el('div', { class: 'v mono', text: JSON.stringify(value, null, 2) });
    } else if (value instanceof Node) {
      v = el('div', { class: 'v' }, value);
    } else {
      v = el('div', { class: 'v mono', text: value == null ? '' : String(value) });
    }
    return el('div', { class: 'inspect-kv' },
      el('div', { class: 'k', text: label }),
      v,
    );
  }

  /**
   * Inspect-section with heading + a list of key/value rows.
   *
   *   UI.kvSection('General', [['ID', c.id], ['Name', c.name]])
   */
  function kvSection(title, entries) {
    var sec = el('div', { class: 'inspect-section' },
      el('h4', { text: title }),
    );
    (entries || []).forEach(function(e) { sec.appendChild(kvRow(e[0], e[1])); });
    return sec;
  }

  /**
   * Copy-to-clipboard helper with a graceful fallback.
   *
   * The Clipboard API rejects in several real scenarios (non-HTTPS origins,
   * iframed contexts, permission-denied / unfocused browsers). The fallback
   * copies via a hidden textarea + execCommand, and then also selects the
   * text so the user can always press ⌘C/Ctrl-C manually.
   */
  function copy(text, onSuccess, onFallback) {
    function fallback() {
      var ta = el('textarea', { style: 'position:fixed;top:-1000px;left:-1000px;opacity:0' });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); if (onSuccess) onSuccess(); }
      catch (e) { if (onFallback) onFallback(); }
      document.body.removeChild(ta);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function() { if (onSuccess) onSuccess(); },
        fallback,
      );
    } else {
      fallback();
    }
  }

  /**
   * Single-line copy command. Styled as an inline <code> with a Copy button.
   * Use for short CLI commands, single-field values (tokens, URLs, paths).
   */
  function copyCmd(text, label) {
    var btn = el('button', {
      class: 'btn small', type: 'button', text: 'Copy',
      on: { click: function() {
        copy(text,
          function() { btn.textContent = 'Copied!'; setTimeout(function() { btn.textContent = 'Copy'; }, 1500); },
          function() { btn.textContent = 'Select \u2318C'; },
        );
      }},
    });
    var code = el('code', {
      style: 'flex:1;background:var(--bg-elevated);color:var(--text);border:1px solid var(--border);'
           + 'border-radius:6px;padding:6px 10px;font-size:12px;font-family:inherit;'
           + 'white-space:nowrap;overflow-x:auto',
      text: text,
    });
    return el('div', { style: 'margin:4px 0' },
      label && el('div', {
        style: 'font-size:11px;color:var(--muted);margin-bottom:3px;font-weight:500',
        text: label,
      }),
      el('div', { style: 'display:flex;gap:6px;align-items:center' }, code, btn),
    );
  }

  /**
   * Multi-line copy block. Styled as a <pre> with a Copy button pinned to
   * the top-right corner. Use for YAML/JSON/config snippets.
   */
  function copyBlock(text, label) {
    var btn = el('button', {
      class: 'btn small', type: 'button', text: 'Copy',
      style: 'position:absolute;top:6px;right:6px',
      on: { click: function() {
        copy(text,
          function() { btn.textContent = 'Copied!'; setTimeout(function() { btn.textContent = 'Copy'; }, 1500); },
          function() { btn.textContent = 'Select \u2318C'; },
        );
      }},
    });
    var pre = el('pre', {
      style: 'background:var(--bg-elevated);color:var(--text);border:1px solid var(--border);'
           + 'border-radius:6px;padding:10px 12px;font-size:12px;white-space:pre;'
           + 'overflow-x:auto;margin:0;max-height:260px;overflow-y:auto',
      text: text,
    });
    return el('div', { style: 'margin:4px 0' },
      label && el('div', {
        style: 'font-size:11px;color:var(--muted);margin-bottom:3px;font-weight:500',
        text: label,
      }),
      el('div', { style: 'position:relative' }, pre, btn),
    );
  }

  /**
   * Standard modal. Returns { modal, box, close }.
   *
   * `actions` is an array of DOM nodes (typically buttons). `close()` removes
   * the modal from the DOM and optionally calls opts.onClose().
   */
  /**
   * Trigger a client-side JSON download for `data` with the given
   * filename. Used by inspect views so operators can export a
   * container / volume / network / image definition for backup or
   * diffing without copying from the browser's pretty-print.
   *
   * @param {Object|Array} data — JSON-serialisable
   * @param {string} filename — e.g. "container-abc123.json"
   */
  function downloadJson(data, filename) {
    var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function() { URL.revokeObjectURL(url); }, 500);
  }

  /**
   * Centred overlay modal. Spec: `{ title, body, actions }`.
   * Click on backdrop closes. Returns `{ modal, box, close }`.
   */
  function modal(opts) {
    opts = opts || {};
    var box = el('div', { class: 'modal' });
    var m = el('div', { class: 'modal-bg' }, box);
    function close() {
      if (m.parentNode) m.parentNode.removeChild(m);
      if (opts.onClose) opts.onClose();
    }
    if (!opts.noClickOutside) {
      m.addEventListener('click', function(e) { if (e.target === m) close(); });
    }
    if (opts.title) {
      box.appendChild(el('h3', { text: opts.title }));
    }
    if (opts.body instanceof Node) box.appendChild(opts.body);
    if (Array.isArray(opts.actions)) {
      box.appendChild(el('div', { class: 'actions' }, opts.actions));
    }
    document.body.appendChild(m);
    return { modal: m, box: box, close: close };
  }

  /**
   * Toast helper — mirrors the app.js `toast()` but usable from module code
   * before app.js has defined it. Falls through to app.js's version when
   * that is available, to keep the single notification queue.
   */
  function toast(message, kind) {
    if (typeof window.toast === 'function' && window.toast !== toast) {
      return window.toast(message, kind);
    }
    var t = el('div', { class: 'toast ' + (kind || 'info'), text: message });
    var container = document.querySelector('.toast-container') ||
      document.body.appendChild(el('div', { class: 'toast-container' }));
    container.appendChild(t);
    setTimeout(function() { if (t.parentNode) t.parentNode.removeChild(t); }, 3000);
  }

  /**
   * Inline contextual-help icon. `ⓘ` shown inline, hovering shows an
   * explanatory tooltip (native `title` attribute for robustness across
   * browsers including screen readers, which announce the title via
   * `aria-describedby`). Usage:
   *
   *   label.appendChild(UI.helpIcon("Explanatory text shown on hover."))
   *
   * Keeps help copy next to the field instead of in separate docs — reduces
   * the "open another tab to read what this checkbox does" friction.
   */
  function helpIcon(tooltip) {
    return el('span', {
      class: 'inline-help',
      role: 'img',
      'aria-label': tooltip,
      title: tooltip,
      text: 'i',
    });
  }

  /**
   * Declarative form factory.
   *
   * Each field:
   *   { name: 'password', label: 'API token', type: 'password',
   *     required: true, help: 'min 16 chars' }
   *
   * Returns { form, getValues, setValues, onSubmit(fn), rootNode }. The `submit`
   * callback receives the accumulated values object. Validation: `required`
   * fields produce a 'required' message; `validator` is an optional
   * (value) -> error?: string function run on submit.
   */
  function form(spec) {
    spec = spec || {};
    var fields = spec.fields || [];
    var byName = {};
    var fieldRows = fields.map(function(f) {
      var input;
      if (f.type === 'textarea') {
        input = el('textarea', {
          name: f.name, rows: f.rows || 4,
          placeholder: f.placeholder || '',
        });
      } else if (f.type === 'select') {
        input = el('select', { name: f.name },
          (f.options || []).map(function(opt) {
            return el('option', { value: opt.value, selected: opt.selected || null,
              text: opt.label || opt.value,
            });
          }),
        );
      } else if (f.type === 'checkbox') {
        // Checkboxes wrap differently: the label sits to the RIGHT of
        // the box (vs above for text/select). Render the input without
        // the outer `.field > label > input` sandwich so spec-submitted
        // booleans surface correctly via `.checked`.
        input = el('input', {
          type: 'checkbox',
          name: f.name,
          checked: f.value ? 'checked' : null,
        });
      } else {
        input = el('input', {
          type: f.type || 'text',
          name: f.name,
          placeholder: f.placeholder || '',
          value: f.value == null ? '' : String(f.value),
        });
      }
      byName[f.name] = input;
      // Checkboxes flow inline: <input><span>label</span>(?) so the
      // clicky target is natural for a novice user. Text/select/textarea
      // keep the vertical layout (label above input).
      if (f.type === 'checkbox') {
        var cbLabel = el('label', { class: 'field field-checkbox' },
          input,
          el('span', { class: 'field-label', text: f.label || f.name }),
          f.help ? helpIcon(f.help) : null,
        );
        return cbLabel;
      }
      var label = el('label', { class: 'field' },
        el('span', { class: 'field-label', text: f.label || f.name },
          f.help ? helpIcon(f.help) : null,
        ),
        input,
        f.hint ? el('small', { class: 'field-hint', text: f.hint }) : null,
      );
      return label;
    });

    var errorBanner = el('div', { class: 'field-error', style: 'display:none', role: 'alert', 'aria-live': 'polite' });

    function setError(msg) {
      errorBanner.textContent = msg == null ? '' : String(msg);
      setStyle(errorBanner, 'display', msg ? 'block' : 'none');
    }
    function clearError() { setError(''); }

    function getValues() {
      var values = {};
      Object.keys(byName).forEach(function(k) {
        var inp = byName[k];
        if (inp.type === 'checkbox') {
          values[k] = !!inp.checked;
        } else {
          values[k] = inp.value;
        }
      });
      return values;
    }
    function setValues(v) {
      Object.keys(v || {}).forEach(function(k) {
        if (byName[k]) byName[k].value = v[k];
      });
    }

    var submitHandler = null;
    function onSubmit(fn) { submitHandler = fn; }

    function validate() {
      for (var i = 0; i < fields.length; i++) {
        var f = fields[i];
        var v = byName[f.name].value;
        if (f.required && (v == null || v === '')) {
          return f.label + ' is required';
        }
        if (typeof f.validator === 'function') {
          var err = f.validator(v);
          if (err) return err;
        }
      }
      return null;
    }

    var frm = el('form', {
      class: 'ui-form',
      on: { submit: function(ev) {
        ev.preventDefault();
        var err = validate();
        if (err) {
          setError(err);
          return;
        }
        clearError();
        if (submitHandler) submitHandler(getValues());
      }},
    }, fieldRows, errorBanner);

    return {
      form: frm,
      getValues: getValues,
      setValues: setValues,
      onSubmit: onSubmit,
      setError: setError,
      clearError: clearError,
    };
  }

  /**
   * Modal that wraps a declarative form.
   *
   *   UI.formModal({
   *     title: 'Create network',
   *     fields: [
   *       {name: 'name', label: 'Network name', required: true, placeholder: 'my-network'},
   *       {name: 'driver', label: 'Driver', type: 'select',
   *        options: [{value: 'bridge'}, {value: 'overlay'}, {value: 'macvlan'}]},
   *     ],
   *     submitLabel: 'Create',
   *     cancelLabel: 'Cancel',           // optional, default 'Cancel'
   *     onSubmit: async (values) => { ... },
   *   })
   *
   * On submit the onSubmit callback receives the accumulated values.
   * Returns the submit's resolved value. The modal closes automatically
   * on successful submit; throws stay visible for the form's error banner.
   *
   * Collapses the eight hand-rolled "create X" modals that each repeated
   * `createElement('div.modal-bg'); createElement('div.modal'); ...
   * createElement('div.actions')` inline. Adding a new modal is now
   * one call with a config object.
   */
  function formModal(spec) {
    spec = spec || {};
    var fields = spec.fields || [];
    var frm = form({fields: fields});
    var cancelBtn = el('button', {
      type: 'button', class: 'btn', text: spec.cancelLabel || 'Cancel',
      on: {click: function() { m.close(); if (spec.onCancel) spec.onCancel(); }},
    });
    var submitBtn = el('button', {
      type: 'submit', class: 'btn primary', text: spec.submitLabel || 'Submit',
    });
    // Wire submit onto the form root so the browser Enter key works.
    frm.form.appendChild(el('div', { class: 'actions' }, cancelBtn, submitBtn));

    // Submit plumbing: render every failure in the form's error banner so the
    // user always sees WHY their input was rejected. Async onSubmit() that
    // rejects (e.g. apiFetch throwing on a 4xx envelope) used to silently
    // swallow the error — now its `err.message` (already populated from the
    // server envelope's `detail.message` by apiFetch) goes straight to the
    // banner. Submit button is disabled+relabelled while the promise is
    // pending so double-clicks can't fire a second request.
    var submitPending = false;
    var _submitLabel = submitBtn.textContent;
    function _setPending(p) {
      submitPending = p;
      submitBtn.disabled = p;
      submitBtn.classList.toggle('loading', p);
      submitBtn.textContent = p ? (spec.pendingLabel || 'Working…') : _submitLabel;
    }
    frm.onSubmit(function(values) {
      if (submitPending) return;
      frm.clearError();
      var result;
      try {
        result = spec.onSubmit(values);
      } catch (e) {
        frm.setError(e && e.message ? e.message : 'Submit failed');
        return;
      }
      if (result && typeof result.then === 'function') {
        _setPending(true);
        result.then(
          function() { _setPending(false); m.close(); },
          function(err) {
            _setPending(false);
            var msg = (err && err.message) ? err.message : 'Submit failed';
            frm.setError(msg);
          },
        );
      } else {
        m.close();
      }
    });

    var m = modal({title: spec.title, body: frm.form, noClickOutside: spec.noClickOutside});
    // Auto-focus the first text input so the modal is keyboard-ready.
    var first = frm.form.querySelector('input:not([type=hidden]), select, textarea');
    if (first) first.focus();
    return {close: m.close, form: frm};
  }

  /**
   * Declarative table factory.
   *
   *   UI.table({
   *     columns: [
   *       {key: 'name', label: 'Name'},
   *       {key: 'status', label: 'Status',
   *        render: function(row) { return UI.el('span', {class: 'pill'}, row.status); }},
   *     ],
   *     rows: items,
   *     rowActions: function(row) { return [UI.el('button', {...}, 'Stop')]; },
   *     emptyMessage: 'No items yet',
   *   })
   *
   * Returns the root <div> containing the <table>. Callers can rebuild via
   * calling UI.table() again and replacing the previous node.
   */
  function table(spec) {
    spec = spec || {};
    var columns = spec.columns || [];
    var rows = spec.rows || [];
    if (!rows.length) {
      return el('div', { class: 'ui-table-empty', text: spec.emptyMessage || 'No items' });
    }
    var head = el('thead',
      el('tr',
        columns.map(function(c) { return el('th', { text: c.label || c.key }); }),
        spec.rowActions ? el('th', { text: 'Actions', class: 'actions-col' }) : null,
      ),
    );
    var body = el('tbody',
      rows.map(function(row) {
        return el('tr',
          columns.map(function(c) {
            if (typeof c.render === 'function') {
              var v = c.render(row);
              if (v instanceof Node) return el('td', {}, v);
              return el('td', { text: v == null ? '' : String(v) });
            }
            var raw = row[c.key];
            return el('td', { text: raw == null ? '' : String(raw) });
          }),
          spec.rowActions
            ? el('td', { class: 'actions-col' }, spec.rowActions(row) || [])
            : null,
        );
      }),
    );
    return el('div', { class: 'ui-table' }, el('table', {}, head, body));
  }

  /**
   * Inspect-panel factory — resource detail views share the same structure.
   *
   *   UI.inspect({
   *     sections: [
   *       {title: 'General', entries: [['ID', c.id], ['Name', c.name]]},
   *       {title: 'Config', entries: [...]},
   *     ],
   *     actions: [UI.el('button', {...}, 'Reload')],
   *   })
   */
  function inspect(spec) {
    spec = spec || {};
    var panel = el('div', { class: 'inspect-panel' });
    (spec.sections || []).forEach(function(s) {
      panel.appendChild(kvSection(s.title, s.entries));
    });
    if (Array.isArray(spec.actions) && spec.actions.length) {
      panel.appendChild(el('div', { class: 'inspect-actions' }, spec.actions));
    }
    return panel;
  }

  // ───────────────────────────────────────────────────────────────────────────
  // Page / navigation registry
  // ───────────────────────────────────────────────────────────────────────────
  //
  // Every UI-visible page declares itself once via `UI.registerPage({...})`.
  // The sidebar, command palette, wizard intro, and persona-visibility logic
  // all read from the same registry so adding a new page is O(1) — no hand-
  // edited HTML sidebar, no hand-edited palette-actions array, no split
  // source of truth.

  var _pages = [];
  /**
   * Register a page with the navigation factory.
   *
   *   UI.registerPage({
   *     id: 'containers',
   *     label: 'Containers',
   *     personas: ['homelab', 'dev', 'sre'],  // null ⇒ visible to all
   *     keywords: ['ps', 'docker'],           // for the command palette
   *   })
   *
   * Called once per page module. Sidebar, palette, and wizard intro read
   * from the shared registry — adding a page is O(1) elsewhere.
   */
  function registerPage(p) {
    if (!p || !p.id) throw new Error('UI.registerPage: id is required');
    if (_pages.some(function(x) { return x.id === p.id; })) {
      throw new Error('UI.registerPage: duplicate id ' + p.id);
    }
    _pages.push({
      id: p.id,
      label: p.label || p.id,
      icon: p.icon || null,
      href: p.href || ('#' + p.id),
      personas: Array.isArray(p.personas) ? p.personas : null,  // null = visible to all
      keywords: Array.isArray(p.keywords) ? p.keywords : [],
      helpRef: p.helpRef || null,
      order: typeof p.order === 'number' ? p.order : 100,
      // `external: true` on a registration tells the sidebar renderer
      // to emit <a href target=_blank> instead of wiring showPage().
      // Used by the api-docs entry (opens Swagger UI in a new tab).
      external: !!p.external,
    });
  }
  /**
   * List registered pages, sorted by their `order` value. When `persona`
   * is passed, filters to pages visible to that persona (persona in the
   * page's `personas` list, or no list at all).
   */
  function getPages(persona) {
    var list = _pages.slice().sort(function(a, b) { return a.order - b.order; });
    if (!persona) return list;
    return list.filter(function(p) {
      return p.personas == null || p.personas.indexOf(persona) !== -1;
    });
  }
  /**
   * Look up a single page by id. Returns null if unregistered.
   */
  function getPage(id) {
    for (var i = 0; i < _pages.length; i++) {
      if (_pages[i].id === id) return _pages[i];
    }
    return null;
  }
  function _resetPagesForTests() { _pages.length = 0; }

  /**
   * Look up a user-facing string by dotted key from `window.SKIFF_STRINGS`.
   *
   *   t('containers.actions.start')                   → "Start"
   *   t('containers.confirm.remove', {name: 'web-1'}) → "Remove container web-1?…"
   *
   * Placeholder substitution: occurrences of `{key}` in the looked-up
   * string are replaced with the matching value from `vars`. Values are
   * coerced to string via `String(v)`; HTML escaping happens at the DOM
   * call site (every caller uses textContent, not innerHTML).
   *
   * Missing keys return the key itself so the miss is visible in the UI
   * — the alternative of returning an empty string silently hides bugs.
   *
   * This is the pre-i18n surface: `strings.en.js` is the only bundle
   * today. A future commit adds `strings.<lang>.js` + a picker that flips
   * which window-global the helper reads from. Call sites don't change.
   */
  function t(key, vars) {
    var dict = root.SKIFF_STRINGS || {};
    var parts = String(key).split(".");
    var node = dict;
    for (var i = 0; i < parts.length; i++) {
      if (node && typeof node === "object" && parts[i] in node) {
        node = node[parts[i]];
      } else {
        return key; // missing — show the key so the UI shouts about it
      }
    }
    if (typeof node !== "string") return key;
    if (!vars) return node;
    var has = Object.prototype.hasOwnProperty;
    return node.replace(/\{(\w+)\}/g, function(match, name) {
      // `hasOwnProperty` instead of `in` so a vars object that inherits
      // from a non-null prototype (a future caller-supplied Map-like)
      // can't smuggle inherited properties into substitutions.
      return has.call(vars, name) ? String(vars[name]) : match;
    });
  }

  root.UI = {
    el: el,
    setStyle: setStyle,
    getStyle: getStyle,
    kvRow: kvRow,
    kvSection: kvSection,
    copy: copy,
    copyCmd: copyCmd,
    copyBlock: copyBlock,
    modal: modal,
    toast: toast,
    helpIcon: helpIcon,
    form: form,
    formModal: formModal,
    table: table,
    inspect: inspect,
    downloadJson: downloadJson,
    registerPage: registerPage,
    getPages: getPages,
    getPage: getPage,
    t: t,
    _resetPagesForTests: _resetPagesForTests,
  };
  // Also expose `t` as a bare global so call sites can write `t(key)`
  // without having to type `UI.t(key)` each time. Same visibility.
  root.t = t;
})(window);
