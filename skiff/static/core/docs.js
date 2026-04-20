// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
//
// Initialise Swagger UI at /api/docs. Ships CSP-safe (script-src 'self')
// because every asset — swagger-ui-bundle.js, swagger-ui-standalone-preset.js,
// swagger-ui.css — lives under /static/swagger-ui/ on the same origin.
//
// Adds two integration pieces the default build doesn't do:
//
//   1. A request interceptor that attaches SKIFF's `X-Requested-With:
//      ContainerManager` CSRF header to every Try-it-out POST/DELETE/PUT
//      call. Without it the server rejects mutations with 403 even when
//      the bearer token is correct.
//
//   2. If a valid `api_token` is already in sessionStorage (the operator
//      signed in via the main UI in another tab on the same origin), the
//      Authorize button is pre-filled so Try-it-out just works. Nothing
//      fetched from cookies — stays in sync with SKIFF's "no persistent
//      browser auth" posture.
(function() {
  if (typeof SwaggerUIBundle === 'undefined') return;

  var preloadedToken = null;
  try {
    preloadedToken = sessionStorage.getItem('api_token');
  } catch (e) {
    // sessionStorage may throw in some privacy modes; degrade silently.
    preloadedToken = null;
  }

  var ui = SwaggerUIBundle({
    url: '/api/openapi.json',
    dom_id: '#swagger-ui',
    deepLinking: true,
    presets: [
      SwaggerUIBundle.presets.apis,
      // SwaggerUIStandalonePreset adds the TopbarPlugin; we hide the topbar
      // in CSS but the preset itself is harmless and matches upstream docs.
      SwaggerUIStandalonePreset,
    ],
    plugins: [SwaggerUIBundle.plugins.DownloadUrl],
    layout: 'StandaloneLayout',
    // Every Try-it-out request needs the CSRF header (mutations) + the
    // bearer token (all authenticated routes). FastAPI's spec declares
    // `bearerAuth` for protected paths so the UI handles the bearer part
    // via the Authorize button; we inject the CSRF header unconditionally.
    requestInterceptor: function(req) {
      req.headers['X-Requested-With'] = 'ContainerManager';
      return req;
    },
    onComplete: function() {
      // If we have a token in sessionStorage, pre-fill the Authorize modal
      // so the operator doesn't have to paste it twice.
      if (preloadedToken && ui && typeof ui.preauthorizeApiKey === 'function') {
        ui.preauthorizeApiKey('bearerAuth', preloadedToken);
      }
    },
  });

  window.ui = ui;
})();
