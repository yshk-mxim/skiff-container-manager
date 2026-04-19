// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
/**
 * Images page — per-page module loaded by index.html.
 *
 * Uses globals defined in app.js: API, currentPage, apiFetch, toast,
 * makeBtn, makeActionBtn, guardedAction, undoableDelete, relTime,
 * showPage, showRunModal, buildHubSearch, loadImages (itself).
 *
 * Pattern for future page extractions (volumes, networks, compose,
 * system, wizard): copy the page's load* function + its private modal
 * helpers to a pages/<name>.js file and load it after app.js in
 * index.html. No module system — window globals keep the wiring
 * identical to what the legacy single-file layout provided.
 */
"use strict";

// loadImages / showImageInspect / showPullModal defined here. app.js's
// inline copy will be removed once this file is proven in the full
// e2e suite.

async function loadImages() {
  var main = document.getElementById('main');
  main.innerHTML = '<div class="refreshing">Loading images...</div>';
  try {
    var images = await apiFetch(API + '/images');
    if (currentPage !== 'images') return;
    main.innerHTML = '';
    var header = document.createElement('div'); header.className = 'page-header';
    var h2 = document.createElement('h2'); h2.textContent = 'Images (' + images.length + ')';
    var ha = document.createElement('div'); ha.className = 'header-actions';
    var imgSearch = document.createElement('input'); imgSearch.className = 'search-bar'; imgSearch.placeholder = 'Search images...';
    ha.append(
      imgSearch,
      makeBtn('Pull image', showPullModal, 'btn primary'),
      makeActionBtn('Prune', function() {
        if (!confirm('Remove dangling (untagged) images? Used-by-container images will be kept.'))
          throw new Error('Cancelled');
        return guardedAction('prune-images', function() {
          return apiFetch(API + '/images/prune?dangling_only=true', { method: 'POST' }).then(function(r) {
            var n = r.deleted_count || 0;
            var msg = n ? 'Pruned ' + n + ' image' + (n === 1 ? '' : 's')
                        + ' (' + (r.space_reclaimed_mb || 0) + ' MB reclaimed)'
                        : 'No dangling images to prune';
            toast(msg, n ? 'success' : 'info');
            loadImages();
          });
        });
      }, 'btn small', 'Pruning\u2026'),
    );
    header.append(h2, ha);
    main.appendChild(header);
    var imgDesc = document.createElement('p'); imgDesc.style.cssText = 'color:var(--muted);font-size:12px;margin-bottom:16px';
    imgDesc.textContent = 'Images are stored on the remote Docker engine. Only images from approved registries can be pulled or run.';
    main.appendChild(imgDesc);
    var allImages = images;
    function renderImageTable(filtered) {
      var table = document.createElement('table');
      table.innerHTML = '<thead><tr><th>Repository / Tag</th><th>Image ID</th><th>Size</th><th>Created</th><th>Actions</th></tr></thead>';
      var tbody = document.createElement('tbody');
      if (filtered.length === 0) {
        var tr = document.createElement('tr'); var td = document.createElement('td');
        td.colSpan = 5; td.style.cssText = 'text-align:center;color:var(--muted);padding:40px';
        td.textContent = 'No images found'; tr.appendChild(td); tbody.appendChild(tr);
      } else {
        filtered.forEach(function(img) {
          var imgTags = Array.isArray(img.tags) ? img.tags : (img.tag ? [img.tag] : []);
          var displayTag = imgTags.length ? imgTags.join(', ') : '<none>';
          var runTag = imgTags[0] || img.id;
          var tr = document.createElement('tr');
          var tdTag = document.createElement('td'); tdTag.style.cssText = 'font-size:13px;font-weight:500'; tdTag.textContent = displayTag;
          var tdId = document.createElement('td'); tdId.className = 'container-id'; tdId.textContent = img.id;
          var tdSize = document.createElement('td'); tdSize.textContent = img.size_mb + ' MB';
          var tdCreated = document.createElement('td'); tdCreated.className = 'created-time'; tdCreated.textContent = relTime(img.created);
          var tdAct = document.createElement('td');
          var bg = document.createElement('div'); bg.className = 'btn-group';
          bg.append(
            makeBtn('Inspect', function() { showImageInspect(img.id, displayTag); }),
            makeBtn('Run', function() { showPage('containers'); showRunModal(runTag); }, 'btn'),
            makeActionBtn('Delete', function() {
              if (!confirm('Delete image ' + displayTag + '?')) throw new Error('Cancelled');
              return guardedAction('del-img-' + img.id, function() {
                return undoableDelete(API + '/images/' + encodeURIComponent(img.id) + '?force=true',
                                      'Image', loadImages);
              });
            }, 'btn danger', 'Deleting\u2026'),
          );
          tdAct.appendChild(bg);
          tr.append(tdTag, tdId, tdSize, tdCreated, tdAct); tbody.appendChild(tr);
        });
      }
      table.appendChild(tbody);
      var prev = main.querySelector('table'); if (prev) prev.remove();
      main.appendChild(table);
    }
    renderImageTable(allImages);
    imgSearch.oninput = function() {
      var q = imgSearch.value.toLowerCase();
      var filtered = q ? allImages.filter(function(img) {
        var imgTags = Array.isArray(img.tags) ? img.tags : (img.tag ? [img.tag] : []);
        return imgTags.some(function(t) { return t.toLowerCase().includes(q); }) || img.id.includes(q);
      }) : allImages;
      renderImageTable(filtered);
    };
  } catch (e) {
    main.innerHTML = '';
    var p = document.createElement('p'); p.style.color = 'var(--red)'; p.textContent = 'Failed: ' + e.message;
    main.appendChild(p);
  }
}


async function showImageInspect(id, tag) {
  try {
    var d = await apiFetch(API + '/images/' + encodeURIComponent(id) + '/inspect');
    var panel = UI.inspect({
      sections: [{
        title: 'General',
        entries: [
          ['ID', d.id], ['Tags', (d.tags || []).join(', ')], ['Size', d.size_mb + ' MB'],
          ['OS', d.os], ['Arch', d.architecture], ['Layers', d.layers], ['Created', d.created],
        ],
      }].concat(d.history && d.history.length ? [{
        title: 'Layer History',
        entries: d.history.map(function(l) { return [l.size_mb + ' MB', l.created_by || '']; }),
      }] : []),
    });

    var m;
    var tagInp = UI.el('input', {
      placeholder: 'us-docker.pkg.dev/project/repo/image',
      style: 'flex:1;padding:5px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px',
    });
    var tagTagInp = UI.el('input', {
      placeholder: 'latest', value: 'latest',
      style: 'width:80px;padding:5px 10px;border:1px solid var(--border);border-radius:4px;font-size:13px',
    });
    var tagBtn = makeActionBtn('Tag', function() {
      var repo = tagInp.value; var tagVal = tagTagInp.value || 'latest';
      var newRef = repo + ':' + tagVal;
      // Docker silently repoints a repo:tag pair to the new image id if
      // the target already exists — the old image becomes dangling. Warn
      // so the operator opts in explicitly.
      return apiFetch(API + '/images').then(function(imgs) {
        var existing = (imgs || []).find(function(img) {
          return (img.tags || []).indexOf(newRef) !== -1 && img.id !== id;
        });
        if (existing && !confirm(
          'Tag "' + newRef + '" already points to a different image.\n\n' +
          'Tagging will move the pointer; the previous image becomes dangling ' +
          'and will be pruned by `docker image prune`. Proceed?')) {
          throw new Error('Cancelled');
        }
        return apiFetch(API + '/images/' + encodeURIComponent(id) +
          '/tag?repository=' + encodeURIComponent(repo) + '&tag=' + encodeURIComponent(tagVal),
          { method: 'POST' });
      }).then(function() {
        toast('Image tagged', 'success'); m.close(); loadImages();
      });
    }, 'btn small primary');
    var pushBtns = (d.tags || []).map(function(t) {
      return makeActionBtn('Push ' + t.split('/').pop(), function() {
        if (!confirm('Push "' + t + '" to registry?')) throw new Error('Cancelled');
        return apiFetch(API + '/images/push?image=' + encodeURIComponent(t), { method: 'POST' })
          .then(function() { toast('Pushed ' + t, 'success'); });
      }, 'btn small primary', 'Pushing\u2026');
    });
    var tagSec = UI.el('div', { style: 'margin-top:16px' },
      UI.el('label', { text: 'Tag image (new repository:tag)' }),
      UI.el('div', { style: 'display:flex;gap:8px;margin-top:6px' }, tagInp, tagTagInp, tagBtn),
      UI.el('div', { style: 'display:flex;gap:8px;margin-top:8px;align-items:center' },
        UI.el('span', { style: 'font-size:12px;color:var(--muted)', text: 'Push to registry:' }),
        pushBtns,
      ),
    );
    var closeBtn = UI.el('button', {
      type: 'button', class: 'btn', text: 'Close',
      on: {click: function() { m.close(); }},
    });
    m = UI.modal({
      title: 'Image: ' + tag,
      body: UI.el('div', null, panel, tagSec),
      actions: [closeBtn],
    });
  } catch (e) {
    toast(e.message, 'error');
  }
}


function showPullModal(prefillImage) {
  // This modal is the complex case: beyond a single input it embeds the
  // Docker-Hub search panel and a dynamic "allowed registries" hint.
  // UI.formModal handles the input + submit flow; the extra body lives
  // between the form and the actions, so we use UI.modal + UI.form
  // directly here and pin the layout.
  var inp = UI.el('input', {
    id: 'pull-image',
    placeholder: 'image:tag or registry/image:tag',
    value: typeof prefillImage === 'string' ? prefillImage : '',
  });
  var hint = UI.el('p', {
    style: 'font-size:11px;color:var(--muted);margin-top:4px;margin-bottom:16px',
    text: 'Loading registry configuration\u2026',
  });
  apiFetch(API + '/config').then(function(cfg) {
    var regs = cfg.allowed_registries || [];
    hint.textContent = regs.length ? 'Allowed: ' + regs.join(', ') : 'No registry restriction configured.';
  }).catch(function() {});
  var hubSearch = buildHubSearch(function(name) { inp.value = name; });

  var cancelBtn, pullBtn;
  var m;
  cancelBtn = UI.el('button', {
    type: 'button', class: 'btn', text: 'Cancel',
    on: {click: function() { m.close(); }},
  });
  pullBtn = makeActionBtn('Pull', async function() {
    var image = inp.value.trim();
    if (!image) { toast('Image name is required', 'error'); throw new Error('no image'); }
    await apiFetch(API + '/images/pull?image=' + encodeURIComponent(image), { method: 'POST' });
    m.close();
    toast('Image pulled: ' + image, 'success');
    loadImages();
  }, 'btn primary', 'Pulling\u2026');

  var body = UI.el('div', null,
    UI.el('label', null, UI.el('span', { class: 'field-label', text: 'Image' }), inp),
    hint,
    hubSearch.section,
  );
  m = UI.modal({
    title: 'Pull image',
    body: body,
    actions: [cancelBtn, pullBtn],
  });
  inp.focus();
}
