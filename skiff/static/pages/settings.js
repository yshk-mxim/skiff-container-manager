// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Settings page — single source of truth for server configuration as seen
 * from the browser. Every knob with `expose=True` in `skiff/config.py`
 * renders as one row with its current value, the source that value came
 * from (env var, defaults.toml, inline default), and its description.
 *
 * Scope is intentionally read-biased: most knobs are read ONCE at
 * process import, so changing them requires a restart. Knobs on the
 * server-side `_EDITABLE_AT_RUNTIME` whitelist render a "LIVE" badge;
 * everything else shows "RESTART" so the operator is never misled about
 * whether clicking here would actually take effect. A later commit
 * wiring `PUT /api/config/knobs/<name>` for the safe subset (SESSION_*,
 * UNDO_DELAY_SECS, RATE_LIMIT_SCALE) is a one-file change — the panel
 * already branches on `k.editable`.
 *
 * Security posture — this panel is AUTH-GATED at the backend route:
 *   - Only `expose=True` knobs are returned. API_TOKEN (secret=True)
 *     never reaches this endpoint.
 *   - `secret=True` knobs render as "(redacted)" placeholder — we don't
 *     render an empty value, so a future leak via expose=True+secret=True
 *     (which the precedence test blocks) would still surface the name
 *     for auditors without the value.
 *   - A separate `_KNOBS_HIDDEN_FROM_GUI` seam lets ops hide a specific
 *     knob from the UI even when it stays exposed via /api/config for
 *     CLI/SIEM scrapers.
 *
 * Tied to docs/configuration.md so the precedence chain renders the same
 * in both places — the subtitle links out so a first-time viewer can
 * read the full write-up.
 */
"use strict";

function _fmtKnobValue(v) {
  // Strings render as-is (escaped). Numbers pass through unchanged — we
  // don't synthesise units here because the knob's `doc` already spells
  // them out ("seconds", "MiB", "count"), and guessing from the name
  // would mis-label e.g. CONTAINER_CP_MAX_MB as MiB when it's decimal MB.
  if (v === null || v === undefined) return '';
  if (Array.isArray(v)) return v.length ? v.join(', ') : '(empty list)';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  return String(v);
}

// Render a secret value as an indistinguishable mask regardless of what
// came back from the server. Defense-in-depth: if a future bug ships
// `value` for a secret knob (server-side precedence test blocks this,
// but zero-trust), the viewer STILL won't reveal it. The mask length is
// fixed so it doesn't leak the real value's length. Uses U+2022 BULLET,
// which is selectable+copyable but visually uniform.
function _secretMask() { return '••••••••'; }

function _sourceBadge(source) {
  // env      — bold red accent: operator overrode via env, draw eyes.
  // toml     — subdued accent: fleet default, normal case.
  // default  — muted: inline fallback, never hit the TOML.
  // unset    — muted red: knob is None (probably optional + not set).
  var map = {
    env:      { text: 'ENV',     bg: 'var(--danger-hover-bg)', fg: 'var(--red)',    title: 'Overridden by an environment variable at startup' },
    toml:     { text: 'TOML',    bg: 'var(--bg-elevated)',     fg: 'var(--accent)', title: 'Value comes from skiff/_config/defaults.toml' },
    'default':{ text: 'DEFAULT', bg: 'var(--bg-elevated)',     fg: 'var(--muted)',  title: 'Inline fallback in skiff/config.py (not in defaults.toml)' },
    unset:    { text: 'UNSET',   bg: 'var(--danger-hover-bg)', fg: 'var(--muted)',  title: 'No env / TOML / inline default — knob is None' },
  };
  var s = map[source] || map['default'];
  return UI.el('span', {
    title: s.title,
    class: 'settings-badge settings-badge-source',
    style: 'background:' + s.bg + ';color:' + s.fg,
    text: s.text,
  });
}

function _editStatusBadge(status, reason) {
  // Three-state badge, each with its own class so operators can read at
  // a glance WHY a row is read-only when it is. Title carries the full
  // reason for hover-inspection without dominating the row.
  var map = {
    live:      { text: 'LIVE',      cls: 'settings-badge-live' },
    security:  { text: 'SECURITY',  cls: 'settings-badge-security' },
    lifecycle: { text: 'LIFECYCLE', cls: 'settings-badge-lifecycle' },
  };
  var s = map[status] || map.lifecycle;
  return UI.el('span', {
    title: reason || '', class: 'settings-badge ' + s.cls, text: s.text,
  });
}

function _renderKnobRow(k) {
  var row = UI.el('div', { class: 'settings-row' });
  row.setAttribute('data-knob', k.name);
  var nameCell = UI.el('div', { class: 'settings-name' });
  nameCell.appendChild(UI.el('div', { class: 'settings-name-id', text: k.name }));
  if (k.doc) {
    nameCell.appendChild(UI.el('div', { class: 'settings-name-doc', text: k.doc }));
  }
  row.appendChild(nameCell);

  // Value cell: for secret knobs, render a fixed-length mask + mark the
  // node with aria-label so screen readers announce "redacted" rather
  // than the mask characters. Never put the raw value in any attribute
  // (title, data-*) — would leak to DevTools / automation tooling.
  var valueCell;
  if (k.secret) {
    valueCell = UI.el('div', {
      class: 'settings-value settings-value-secret',
      'aria-label': 'value is redacted', title: '(redacted)',
      text: _secretMask(),
    });
  } else {
    var valueText = _fmtKnobValue(k.value);
    valueCell = UI.el('div', {
      class: 'settings-value', title: valueText, text: valueText,
    });
  }
  row.appendChild(valueCell);

  var metaCell = UI.el('div', { class: 'settings-meta' });
  metaCell.appendChild(_sourceBadge(k.source));
  metaCell.appendChild(_editStatusBadge(k.edit_status, k.edit_reason));
  if (k.edit_status === 'live' && !k.secret) {
    metaCell.appendChild(UI.el('button', {
      type: 'button', class: 'settings-edit-btn',
      title: 'Edit this knob at runtime',
      'data-testid': 'settings-edit-' + k.name,
      'aria-label': 'Edit ' + k.name,
      text: 'Edit',
      on: { click: function() { _openEditModal(k); } },
    }));
  }
  row.appendChild(metaCell);
  return row;
}

function _openEditModal(k) {
  // Ephemeral edit — PUT /api/config/knobs/<name> updates the knob for
  // this process only. UI labels this clearly so the operator isn't
  // surprised when a restart reverts.
  UI.formModal({
    title: 'Edit ' + k.name,
    fields: [{
      name: 'value', label: 'New value',
      value: k.value == null ? '' : String(k.value),
      hint: (k.doc || '') + (k.default != null
        ? ' Fleet default: ' + k.default + '.'
        : ''),
      required: false,
    }],
    submitLabel: 'Apply (this process only)',
    onSubmit: function(values) {
      return apiFetch(API + '/config/knobs/' + encodeURIComponent(k.name), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: String(values.value) }),
      }).then(function() {
        toast(k.name + ' updated (runtime-only — reverts on restart)', 'success');
        // Re-paint only this row from a fresh fetch to avoid a full
        // page reload that would scroll the operator back to the top.
        apiFetch(API + '/config/knobs').then(function(payload) {
          var updated = null;
          (payload.groups || []).forEach(function(g) {
            (g.knobs || []).forEach(function(row) {
              if (row.name === k.name) updated = row;
            });
          });
          if (!updated) return;
          var oldRow = document.querySelector(
            '.settings-row[data-knob="' + k.name + '"]'
          );
          if (oldRow && oldRow.parentNode) {
            oldRow.parentNode.replaceChild(_renderKnobRow(updated), oldRow);
          }
        }).catch(function() { /* refresh failed; next page load gets it */ });
      });
    },
  });
}

async function showSettings() {
  var main = document.getElementById('main');
  main.innerHTML = '<div class="refreshing">Loading configuration…</div>';
  if (currentPage !== 'settings') return;

  var payload;
  try {
    payload = await apiFetch(API + '/config/knobs');
  } catch (e) {
    main.innerHTML = '';
    var header = UI.el('div', { class: 'page-header' },
      UI.el('h2', { text: 'Settings' }));
    main.appendChild(header);
    main.appendChild(UI.el('div', {
      class: 'field-error', role: 'alert',
      style: 'display:block',
      text: 'Configuration unavailable: ' + (e && e.message ? e.message : 'fetch failed'),
    }));
    return;
  }
  if (currentPage !== 'settings') return;

  main.innerHTML = '';
  main.appendChild(UI.el('div', { class: 'page-header' },
    UI.el('h2', { text: 'Settings' })));

  var subtitle = UI.el('p', { class: 'settings-subtitle' });
  subtitle.appendChild(document.createTextNode(
    'Every exposed server knob. Secrets are redacted. Most values are read at '
    + 'process start — change the env var or defaults.toml and restart to apply. '));
  subtitle.appendChild(UI.el('a', {
    href: '/docs/configuration.md', target: '_blank',
    text: 'How precedence resolves →',
  }));
  main.appendChild(subtitle);

  var groups = (payload && payload.groups) || [];
  if (!groups.length) {
    main.appendChild(UI.el('div', {
      class: 'settings-empty',
      text: 'No exposed knobs. (Unexpected — /api/config/knobs returned an empty group list.)',
    }));
    return;
  }

  var search = UI.el('input', {
    type: 'search',
    class: 'search-bar',
    placeholder: 'Filter by name or description…',
    'aria-label': 'Filter configuration',
    'data-testid': 'settings-search',
  });
  main.appendChild(search);

  var wrap = UI.el('div', { class: 'settings-table' });
  main.appendChild(wrap);

  function paint(needle) {
    while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
    var q = (needle || '').toLowerCase();
    var anyMatch = false;
    groups.forEach(function(group) {
      var matches = group.knobs.filter(function(k) {
        if (k.hidden) return false;
        if (!q) return true;
        return k.name.toLowerCase().indexOf(q) !== -1
            || (k.doc || '').toLowerCase().indexOf(q) !== -1;
      });
      if (!matches.length) return;
      anyMatch = true;
      wrap.appendChild(UI.el('div', {
        class: 'settings-group',
        text: group.category + ' (' + matches.length + ')',
      }));
      matches.forEach(function(k) { wrap.appendChild(_renderKnobRow(k)); });
    });
    if (!anyMatch) {
      wrap.appendChild(UI.el('div', {
        class: 'settings-empty',
        text: 'No knobs match "' + q + '".',
      }));
    }
  }
  paint('');
  search.addEventListener('input', function() { paint(search.value); });
}

window.showSettings = showSettings;
