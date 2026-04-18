// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
// Served at /static/core/docs.js. Stitches the current origin into the
// /api/docs landing page: the curl example's <code> block gets the
// origin as textContent, and the two "Open in ..." buttons get the
// spec URL in their href query string.
//
// Moved out of the HTML's inline <script> so /api/docs can ship with a
// strict CSP (script-src 'self') — no 'unsafe-inline', no CDN.
(function() {
  var origin = location.origin;
  var originEl = document.getElementById('origin');
  if (originEl) originEl.textContent = origin;
  var editorLink = document.getElementById('editor-link');
  if (editorLink) {
    editorLink.href =
      'https://editor.swagger.io/?url=' + encodeURIComponent(origin + '/api/openapi.json');
  }
  var petstoreLink = document.getElementById('petstore-link');
  if (petstoreLink) {
    petstoreLink.href =
      'https://petstore.swagger.io/?url=' + encodeURIComponent(origin + '/api/openapi.json');
  }
})();
