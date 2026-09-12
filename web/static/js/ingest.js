// Shared staging/upload/tag/folder-drop logic between the click-triggered
// upload drawer (_upload_drawer.html, most pages) and the hover/drag-
// revealed pop-out (_gallery_drawer.html, home page only) -- #229. The two
// templates used to carry near-identical ~800-line copies of this (every
// dual-behavior PR had to be re-applied to both files within the same
// hour -- see #229's own history). They render the exact same control ids
// and are never both present on the same page at once, so this reads the
// DOM directly rather than taking a root/prefix param -- the one real
// behavioral divergence between the two callers is what happens when
// something gets staged (slide the click-triggered drawer open, vs.
// reveal + pin the hover pop-out), passed in as onOpen.
//
// Fixed in passing while unifying: the gallery drawer's copy of
// uploadAndTrack never sent folder_name on a real upload (only the click-
// triggered drawer did), so #240's folder-name auto-match silently never
// fired for a folder dropped from the home page specifically. Folder-name
// tracking now lives in this one shared addFiles/stageFolderDrop path, so
// both callers get it.

function createIngestController({ onOpen }) {
  const fileInput = document.getElementById('file-input');
  const stagedList = document.getElementById('staged-list');
  const stagedFilesContainer = document.getElementById('staged-files-container');
  const stagedMoreIndicator = document.getElementById('staged-more-indicator');
  const dropzoneText = document.getElementById('dropzone-text');
  const tagBox = document.getElementById('upload-tag-box');
  const tagAddInput = document.getElementById('upload-tag-add-input');
  const uploadError = document.getElementById('upload-error');
  const submitBtn = document.getElementById('submit-btn');
  const youtubeUrlInput = document.getElementById('youtube-url-input');
  const youtubeAddBtn = document.getElementById('youtube-add-btn');
  const linkToggleBtn = document.getElementById('link-toggle-btn');
  const youtubeAddRow = document.getElementById('youtube-add-row');
  const projectSelect = document.getElementById('upload-project-select');
  const newProjectInput = document.getElementById('upload-new-project-input');
  let tags = [];
  // stagedFiles holds a mix of real File objects (from the dropzone) and
  // plain {__isYoutube: true, name, url} placeholders (from the YouTube
  // field below) — same staged list, same status/remove/submit machinery,
  // so a manually-added link behaves like "just another staged item"
  // rather than a second parallel upload flow (see issue #25).
  let stagedFiles = [];
  let fileStates = []; // parallel to stagedFiles once upload starts — empty before submit
  let isUploading = false;
  const MAX_VISIBLE_FILES = 3;
  // Files that have finished (paused briefly, then hidden) or been manually
  // removed — indexes into stagedFiles/fileStates. Deliberately NOT spliced
  // out of those arrays: runWithConcurrency's lanes each hold a FIXED index
  // captured when the batch started, and splicing mid-batch would shift
  // every later index down, causing a still-in-flight lane's updateFileStatus
  // call to land on the wrong (shifted) file. Hiding via this set instead
  // keeps indices stable for the whole batch; the arrays only actually
  // shrink/reset between batches (see the submit handler's success/failure
  // tails), where dismissedIndices is reset alongside them.
  let dismissedIndices = new Set();

  // #240: name of the top-level folder from the most recent folder drop, or
  // '' for individually dropped/picked files. Cleared once a batch finishes.
  let droppedFolderName = '';

  // #184: any well-formed http(s) URL is accepted -- the server classifies
  // it (YouTube vs. a plain web page — object_types.classify_url) rather
  // than the client deciding up front, so this is deliberately loose, just
  // catching obviously-not-a-URL input before it hits the network.
  const LINK_URL_RE = /^https?:\/\/.+\..+/i;

  // Recognizes an Imgur post/album link specifically (i.imgur.com/...,
  // imgur.com/<id>, imgur.com/a/<id>, imgur.com/gallery/<id>) so a staged
  // link item can be routed to /api/imgur/import-url (#203) instead of the
  // generic /api/content at submit time — see core/imgur_import.py's
  // parse_imgur_url for the server-side counterpart of this same shape.
  const IMGUR_URL_RE = /imgur\.com\//i;

  const STATUS_META = {
    queued:          { dot: '#57543F', label: 'Waiting…' },
    uploading:       { dot: '#57543F', label: 'Uploading…' },
    'ocr-pending':   { dot: '#BA7517', label: 'Extracting text…' },
    done:            { dot: '#7FA37C', label: 'Ready' },
    'ocr-failed':    { dot: '#BA7517', label: 'Uploaded — text extraction failed' },
    'upload-failed': { dot: '#E24B4A', label: 'Upload failed' },
  };

  function updateFileStatus(i, status, message, slug) {
    // slug is sticky once known — a later call that doesn't pass one (e.g.
    // the terminal "done"/"ocr-failed" update) keeps whatever was already
    // recorded, so a retry button can still find its target row afterward.
    const existingSlug = fileStates[i] && fileStates[i].slug;
    fileStates[i] = { status, message: message || STATUS_META[status].label, slug: slug !== undefined ? slug : existingSlug };
    renderStaged();

    // If this file just completed (done or ocr-failed), pause briefly on the
    // completed state so the user sees the success/failure visual feedback,
    // then remove it from the visible list. The next queued file will backfill
    // into the visible 3 slots.
    if ((status === 'done' || status === 'ocr-failed') && isUploading) {
      setTimeout(() => {
        dismissedIndices.add(i);
        renderStaged();
      }, 900);
    }
  }

  function updateSubmitButton() {
    if (isUploading) return; // don't let a mid-upload re-render re-enable the button
    submitBtn.disabled = stagedFiles.length === 0;
    const fileCount = stagedFiles.filter(f => !f.__isYoutube && !f.__isImgur).length;
    const linkCount = stagedFiles.length - fileCount;
    if (stagedFiles.length === 0) {
      submitBtn.textContent = 'Ingest to Constructicon';
    } else if (linkCount === 0) {
      submitBtn.textContent = fileCount > 1 ? `Ingest ${fileCount} files to Constructicon` : 'Ingest to Constructicon';
    } else if (fileCount === 0) {
      submitBtn.textContent = linkCount > 1 ? `Ingest ${linkCount} links to Constructicon` : 'Ingest link to Constructicon';
    } else {
      submitBtn.textContent = `Ingest ${stagedFiles.length} items to Constructicon`;
    }
  }

  function renderStaged() {
    stagedList.innerHTML = '';
    stagedMoreIndicator.style.display = 'none';
    stagedMoreIndicator.textContent = '';

    const activeIndices = stagedFiles.map((_, idx) => idx).filter(idx => !dismissedIndices.has(idx));

    if (activeIndices.length === 0) {
      stagedFilesContainer.style.display = 'none';
      dropzoneText.textContent = 'Drag files here or click to browse';
    } else {
      stagedFilesContainer.style.display = 'block';
      dropzoneText.textContent = `${activeIndices.length} item${activeIndices.length > 1 ? 's' : ''} staged — drop more or click to add`;

      // Render only the first MAX_VISIBLE_FILES still-active files
      const visibleIndices = activeIndices.slice(0, MAX_VISIBLE_FILES);
      for (const i of visibleIndices) {
        const file = stagedFiles[i];
        const row = document.createElement('div');
        row.className = 'staged-file';
        row.innerHTML = `
          <div class="staged-thumb">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
              <rect x="3" y="4" width="18" height="16" rx="2" stroke="#57543F" stroke-width="1.4"/>
              <circle cx="8" cy="9" r="1.6" stroke="#57543F" stroke-width="1.4"/>
              <path d="M3 16L8.5 11.5L13 15L16.5 12L21 16" stroke="#57543F" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
          <div style="flex:1;min-width:0">
            <div class="staged-name mono"></div>
            <div class="staged-meta"></div>
          </div>
          <button type="button" class="staged-remove">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M6 6L18 18M6 18L18 6" stroke="#6E6B57" stroke-width="2" stroke-linecap="round"/></svg>
          </button>`;
        row.querySelector('.staged-name').textContent = file.name;
        const state = fileStates[i];
        const metaEl = row.querySelector('.staged-meta');
        if (state) {
          const meta = STATUS_META[state.status];
          let metaHtml = `<span class="staged-status-dot" style="background:${meta.dot}"></span>${state.message}`;
          if (state.status === 'ocr-failed' && state.slug) {
            metaHtml += ` <button type="button" class="staged-retry-ocr-btn" data-index="${i}">Retry</button>`;
          }
          metaEl.innerHTML = metaHtml;
          const retryBtn = metaEl.querySelector('.staged-retry-ocr-btn');
          if (retryBtn) {
            retryBtn.addEventListener('click', (e) => {
              e.preventDefault();
              retryOcrForStagedFile(i);
            });
          }
          // Add visual feedback when a file is completed
          if (state.status === 'done' || state.status === 'ocr-failed') {
            row.classList.add('completed');
          }
        } else {
          metaEl.textContent = file.__isImgur
            ? 'Imgur link — ready to import'
            : file.__isYoutube
              ? 'Link — ready to add'
              : `${(file.size / 1024 / 1024).toFixed(1)} MB — ready to upload`;
        }
        const removeBtn = row.querySelector('.staged-remove');
        if (state && (state.status === 'uploading' || state.status === 'ocr-pending')) {
          removeBtn.style.visibility = 'hidden'; // mid-flight — nothing to cancel into
        } else {
          removeBtn.addEventListener('click', (e) => {
            e.preventDefault();
            if (isUploading) {
              // Mid-batch (this file just hasn't been picked up by a lane
              // yet): dismiss rather than splice, same reasoning as the
              // completion-timeout above — other staged files' lanes hold
              // fixed indices into these same arrays, and splicing would
              // shift them.
              dismissedIndices.add(i);
              renderStaged();
            } else {
              // Pre-upload: no lanes exist yet, a real splice is safe and
              // keeps stagedFiles/updateSubmitButton's counts simple.
              stagedFiles.splice(i, 1);
              fileStates.splice(i, 1);
              renderStaged();
              updateSubmitButton();
            }
          });
        }
        stagedList.appendChild(row);
      }

      // Show the "+X more" indicator if there are active files beyond the visible 3
      if (activeIndices.length > MAX_VISIBLE_FILES) {
        const moreCount = activeIndices.length - MAX_VISIBLE_FILES;
        stagedMoreIndicator.textContent = `+ ${moreCount} more queued`;
        stagedMoreIndicator.style.display = 'flex';
      }
    }
    updateSubmitButton();
  }

  // Helper to stage a link after validation and Imgur detection.
  // Used by both manual link-add (#25) and .url-file-drop paths.
  function stageLink(url, showError = true) {
    uploadError.style.display = 'none';
    const trimmedUrl = url.trim();
    if (!trimmedUrl) return false;
    if (!LINK_URL_RE.test(trimmedUrl)) {
      if (showError) {
        uploadError.textContent = "That doesn't look like a valid link — paste a full http:// or https:// URL.";
        uploadError.style.display = 'block';
      }
      return false;
    }
    const isImgur = IMGUR_URL_RE.test(trimmedUrl);
    stagedFiles.push({ name: trimmedUrl, size: 0, __isYoutube: !isImgur, __isImgur: isImgur, url: trimmedUrl });
    renderStaged();
    return true;
  }

  // folderName: set when this batch came from a folder drop (#240's
  // auto-match reads it back via droppedFolderName in uploadAndTrack below)
  // — omit/pass '' for an individual file pick or drop.
  async function addFiles(fileList, folderName) {
    droppedFolderName = folderName || '';
    onOpen();
    for (const file of fileList) {
      // .url file support (#199) — Windows Internet Shortcut files are plain
      // INI format; extract the URL and stage as a link, same path as pasted links.
      if (file.name.toLowerCase().endsWith('.url')) {
        try {
          const content = await file.text();
          const match = content.match(/^URL=(.+)$/im);
          if (match && match[1]) {
            stageLink(match[1], false); // don't error on malformed URL here
          } else {
            // Malformed .url file — fall back to uploading it as a file
            stagedFiles.push(file);
          }
        } catch (err) {
          // Error reading file — fall back to uploading as-is
          stagedFiles.push(file);
        }
      } else {
        stagedFiles.push(file);
      }
    }
    renderStaged();
  }

  fileInput.addEventListener('change', () => {
    addFiles(fileInput.files);
    fileInput.value = '';
  });

  // Toggle open/closed for the link field (#203) — hidden until asked
  // for, same reasoning as newProjectInput's reveal-on-select below.
  linkToggleBtn.addEventListener('click', () => {
    const opening = youtubeAddRow.style.display === 'none';
    youtubeAddRow.style.display = opening ? 'flex' : 'none';
    if (opening) youtubeUrlInput.focus();
  });

  // Manual link add (#25, generalized by #184, Imgur-aware by #203) —
  // stages a placeholder object alongside any real staged Files, so it
  // rides the exact same staged-list UI, submit button, and per-item
  // status tracking as a file drop. Routed to the right endpoint at
  // submit time based on which flag got set here.
  function addYoutubeLink() {
    const url = youtubeUrlInput.value;
    if (stageLink(url, true)) {
      onOpen();
      youtubeUrlInput.value = '';
    }
  }
  youtubeAddBtn.addEventListener('click', (e) => {
    e.preventDefault();
    addYoutubeLink();
  });
  youtubeUrlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addYoutubeLink();
    }
  });

  // Import from Imgur (#200) — one server-side round trip against the
  // owner's public gallery submissions (see web/app.py's
  // /api/imgur/import); the server does its own dedup against already-
  // imported items, so this is safe to click again later to pick up
  // anything new. Deliberately not staged item-by-item the way a file
  // drop or a single pasted link is — the whole batch is one request.
  const imgurImportBtn = document.getElementById('imgur-import-btn');
  const imgurImportStatus = document.getElementById('imgur-import-status');
  imgurImportBtn.addEventListener('click', async () => {
    imgurImportBtn.disabled = true;
    imgurImportBtn.textContent = 'Importing…';
    imgurImportStatus.textContent = '';
    imgurImportStatus.classList.remove('error-text');
    try {
      const res = await fetch('/api/imgur/import', { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || 'Import failed');
      imgurImportStatus.textContent = `Imported ${body.imported} new item${body.imported === 1 ? '' : 's'} (${body.skipped} already had, ${body.found} found total).`;
      if (body.imported > 0) {
        document.dispatchEvent(new CustomEvent('constructicon:upload-complete', { detail: { failures: [] } }));
      }
    } catch (err) {
      imgurImportStatus.textContent = err.message;
      imgurImportStatus.classList.add('error-text');
    } finally {
      imgurImportBtn.disabled = false;
      imgurImportBtn.textContent = 'Import from Imgur';
    }
  });

  function renderTags() {
    tagBox.querySelectorAll('.chip').forEach(c => c.remove());
    tags.forEach((tag, i) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = tag;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = '×';
      btn.addEventListener('click', () => { tags.splice(i, 1); renderTags(); });
      chip.appendChild(btn);
      tagBox.insertBefore(chip, tagAddInput);
    });
  }

  // Issue #88: Tag autocomplete from existing tags — a real dropdown, not
  // placeholder text (placeholder is invisible the moment the input has any
  // value, i.e. exactly when suggestions would need to show).
  let allTags = [];
  fetch('/api/tags').then(res => res.ok ? res.json() : []).then(data => { allTags = data; }).catch(() => {});

  const tagSuggestions = document.getElementById('upload-tag-suggestions');
  let tagMatches = [];
  let tagActiveIndex = -1;

  function addTag(value) {
    const trimmed = value.trim();
    if (!trimmed) return;
    tags.push(trimmed);
    tagAddInput.value = '';
    renderTags();
    hideTagSuggestions();
  }

  function hideTagSuggestions() {
    tagSuggestions.style.display = 'none';
    tagSuggestions.innerHTML = '';
    tagMatches = [];
    tagActiveIndex = -1;
  }

  function renderTagSuggestions() {
    tagSuggestions.innerHTML = '';
    if (tagMatches.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'tag-suggestion-empty';
      empty.textContent = 'No matching tags';
      tagSuggestions.appendChild(empty);
    } else {
      tagMatches.forEach((tag, i) => {
        const item = document.createElement('div');
        item.className = 'tag-suggestion-item' + (i === tagActiveIndex ? ' active' : '');
        item.textContent = tag.path;
        // mousedown (not click) fires before the input's blur handler, so
        // the dropdown doesn't get hidden out from under the click.
        item.addEventListener('mousedown', (e) => { e.preventDefault(); addTag(tag.name); });
        tagSuggestions.appendChild(item);
      });
    }
    tagSuggestions.style.display = 'block';
  }

  function updateTagSuggestions() {
    const inputValue = tagAddInput.value.trim().toLowerCase();
    if (!inputValue) { hideTagSuggestions(); return; }
    tagMatches = allTags
      .filter(tag => tag.name.toLowerCase().includes(inputValue) || tag.path.toLowerCase().includes(inputValue))
      .slice(0, 10);
    tagActiveIndex = -1;
    renderTagSuggestions();
  }

  tagAddInput.addEventListener('input', updateTagSuggestions);
  tagAddInput.addEventListener('blur', hideTagSuggestions);

  tagAddInput.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' && tagMatches.length > 0) {
      e.preventDefault();
      tagActiveIndex = Math.min(tagActiveIndex + 1, tagMatches.length - 1);
      renderTagSuggestions();
    } else if (e.key === 'ArrowUp' && tagMatches.length > 0) {
      e.preventDefault();
      tagActiveIndex = Math.max(tagActiveIndex - 1, 0);
      renderTagSuggestions();
    } else if (e.key === 'Enter' && tagAddInput.value.trim()) {
      e.preventDefault();
      if (tagActiveIndex >= 0 && tagMatches[tagActiveIndex]) {
        addTag(tagMatches[tagActiveIndex].name);
      } else {
        addTag(tagAddInput.value);
      }
    } else if (e.key === 'Escape') {
      hideTagSuggestions();
    }
  });

  // Folder drop (#134) — dropping a folder (vs. individual files) stages
  // every file found anywhere under it (subfolders included, flattened —
  // no sub-projects, deliberately simplified from the original nested
  // design, see #134's comment thread) and auto-selects a project named
  // after the top-level folder, reusing an existing same-named project if
  // one exists rather than creating a duplicate. Only the FIRST top-level
  // folder in a drop is handled this way; anything else dropped alongside
  // it is ignored for this pass — dropping one folder at a time is the
  // supported case (see #134's motivating example).
  function readAllEntries(dirReader) {
    // FileSystemDirectoryReader.readEntries only returns one batch per call
    // (browsers cap it, e.g. 100) — must keep calling until it comes back
    // empty to see everything in a large folder.
    return new Promise((resolve, reject) => {
      let all = [];
      (function readBatch() {
        dirReader.readEntries((entries) => {
          if (!entries.length) { resolve(all); return; }
          all = all.concat(entries);
          readBatch();
        }, reject);
      })();
    });
  }

  async function walkDirectory(dirEntry) {
    const files = [];
    async function walk(entry) {
      if (entry.isFile) {
        files.push(await new Promise((resolve, reject) => entry.file(resolve, reject)));
      } else if (entry.isDirectory) {
        for (const child of await readAllEntries(entry.createReader())) await walk(child);
      }
    }
    await walk(dirEntry);
    return files;
  }

  async function selectProjectForFolder(name) {
    const res = await fetch('/api/projects');
    const projects = await res.json();
    const match = projects.find(p => p.title.trim().toLowerCase() === name.trim().toLowerCase());
    if (match) {
      await loadProjects(match.id);
    } else {
      await loadProjects();
      projectSelect.value = '__new__';
      newProjectInput.style.display = 'block';
      newProjectInput.value = name;
    }
  }

  // Runs `worker` over `items` with at most `limit` in flight at once — a
  // folder of dozens of screenshots shouldn't fire that many requests (and
  // that much concurrent Tesseract OCR) at the server all at once.
  async function runWithConcurrency(items, limit, worker) {
    const results = new Array(items.length);
    let next = 0;
    async function lane() {
      while (next < items.length) {
        const i = next++;
        results[i] = await worker(items[i], i);
      }
    }
    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane));
    return results;
  }

  async function pollOcrStatus(slug, timeoutMs = 30000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      await new Promise(r => setTimeout(r, 1200));
      try {
        const res = await fetch(`/api/image/${slug}`);
        if (res.ok) {
          const item = await res.json();
          if (item.ocr_status !== 'pending') return item.ocr_status;
        }
      } catch (e) { /* transient — keep polling */ }
    }
    return 'timeout';
  }

  async function uploadAndTrack(file, i, description, tagList, projectId) {
    updateFileStatus(i, 'uploading');
    const form = new FormData();
    form.append('file', file);
    form.append('description', description);
    form.append('tags', JSON.stringify(tagList));
    form.append('project_id', projectId || '');
    form.append('modified_at', file.lastModified);
    form.append('folder_name', droppedFolderName || '');

    let res;
    try {
      res = await fetch('/api/upload', { method: 'POST', body: form });
    } catch (err) {
      updateFileStatus(i, 'upload-failed', err.message);
      return { ok: false, message: `${file.name}: ${err.message}` };
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: 'Upload failed' }));
      const prefix = res.status === 409 ? 'Duplicate' : 'Upload failed';
      const message = `${prefix} — ${body.detail || 'unknown error'}`;
      updateFileStatus(i, 'upload-failed', message);
      return { ok: false, message: `${file.name}: ${message}` };
    }

    const item = await res.json();
    if (item.ocr_status !== 'pending') {
      updateFileStatus(i, 'done', undefined, item.slug);
      return { ok: true };
    }
    updateFileStatus(i, 'ocr-pending', undefined, item.slug);
    const finalStatus = await pollOcrStatus(item.slug);
    updateFileStatus(i, finalStatus === 'failed' ? 'ocr-failed' : 'done', undefined, item.slug);
    return { ok: true };
  }

  async function addContentAndTrack(url, i, description, tagList, projectId) {
    // Content-only counterpart to uploadAndTrack above — POSTs to
    // /api/content instead of /api/upload (see web/app.py's
    // api_create_content). No media_type here (#184) -- the server
    // classifies the URL itself (object_types.classify_url: YouTube vs. a
    // plain web page) rather than the client deciding up front.
    updateFileStatus(i, 'uploading', 'Adding link…');
    const form = new FormData();
    form.append('external_url', url);
    form.append('description', description);
    form.append('tags', JSON.stringify(tagList));
    form.append('project_id', projectId || '');

    let res;
    try {
      res = await fetch('/api/content', { method: 'POST', body: form });
    } catch (err) {
      updateFileStatus(i, 'upload-failed', err.message);
      return { ok: false, message: `${url}: ${err.message}` };
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: 'Failed to add link' }));
      const message = `Failed to add — ${body.detail || 'unknown error'}`;
      updateFileStatus(i, 'upload-failed', message);
      return { ok: false, message: `${url}: ${message}` };
    }

    const item = await res.json();
    if (item.ocr_status !== 'pending') {
      updateFileStatus(i, 'done', undefined, item.slug);
      return { ok: true };
    }
    updateFileStatus(i, 'ocr-pending', undefined, item.slug);
    const finalStatus = await pollOcrStatus(item.slug);
    updateFileStatus(i, finalStatus === 'failed' ? 'ocr-failed' : 'done', undefined, item.slug);
    return { ok: true };
  }

  async function addImgurUrlAndTrack(url, i) {
    // Imgur counterpart to addContentAndTrack (#203) — POSTs to
    // /api/imgur/import-url instead of /api/content, since resolving a
    // pasted Imgur post/album page link into a real direct image (and its
    // metadata) needs a Client-ID-authenticated Imgur API call the server
    // has to make, not something the generic content-add path does.
    updateFileStatus(i, 'uploading', 'Importing from Imgur…');
    let res;
    try {
      res = await fetch('/api/imgur/import-url', { method: 'POST', body: new URLSearchParams({ url }) });
    } catch (err) {
      updateFileStatus(i, 'upload-failed', err.message);
      return { ok: false, message: `${url}: ${err.message}` };
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: 'Import failed' }));
      const message = `Import failed — ${body.detail || 'unknown error'}`;
      updateFileStatus(i, 'upload-failed', message);
      return { ok: false, message: `${url}: ${message}` };
    }

    const item = await res.json();
    if (item.skipped) {
      updateFileStatus(i, 'done', 'Already imported');
      return { ok: true };
    }
    if (item.ocr_status !== 'pending') {
      updateFileStatus(i, 'done', undefined, item.slug);
      return { ok: true };
    }
    updateFileStatus(i, 'ocr-pending', undefined, item.slug);
    const finalStatus = await pollOcrStatus(item.slug);
    updateFileStatus(i, finalStatus === 'failed' ? 'ocr-failed' : 'done', undefined, item.slug);
    return { ok: true };
  }

  async function retryOcrForStagedFile(i) {
    const slug = fileStates[i] && fileStates[i].slug;
    if (!slug) return;
    updateFileStatus(i, 'ocr-pending', undefined, slug);
    try {
      const res = await fetch(`/api/image/${slug}/ocr`, { method: 'POST' });
      if (!res.ok) {
        updateFileStatus(i, 'ocr-failed', 'Retry failed to start', slug);
        return;
      }
    } catch (err) {
      updateFileStatus(i, 'ocr-failed', 'Retry failed to start', slug);
      return;
    }
    const finalStatus = await pollOcrStatus(slug);
    updateFileStatus(i, finalStatus === 'failed' ? 'ocr-failed' : 'done', undefined, slug);
  }

  document.getElementById('upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    uploadError.style.display = 'none';
    if (!stagedFiles.length || isUploading) return;
    isUploading = true;
    dismissedIndices = new Set();
    submitBtn.disabled = true;
    submitBtn.textContent = 'Uploading...';

    const description = e.target.description.value;
    const tagList = tags.slice();

    // Resolve the Project selection once per submit (not once per staged
    // file) — picking "+ New project..." should create exactly one project
    // that every staged item in this batch attaches to, not a separate
    // project per file (#1).
    let projectId = projectSelect.value;
    if (projectId === '__new__') {
      const newTitle = newProjectInput.value.trim();
      if (!newTitle) {
        isUploading = false;
        submitBtn.disabled = false;
        updateSubmitButton();
        uploadError.textContent = 'Enter a name for the new project, or choose "No project".';
        uploadError.style.display = 'block';
        return;
      }
      try {
        const res = await fetch('/api/projects', { method: 'POST', body: new URLSearchParams({ title: newTitle }) });
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: 'Failed to create project' }));
          throw new Error(body.detail || 'Failed to create project');
        }
        const project = await res.json();
        projectId = project.id;
        await loadProjects(project.id); // so a second batch in this session can reuse it from the dropdown
      } catch (err) {
        isUploading = false;
        submitBtn.disabled = false;
        updateSubmitButton();
        uploadError.textContent = `Couldn't create project: ${err.message}`;
        uploadError.style.display = 'block';
        return;
      }
    }

    fileStates = stagedFiles.map(() => ({ status: 'queued', message: STATUS_META.queued.label }));
    renderStaged();

    // One at a time, deliberately: even 2 concurrent tesseract runs was
    // enough to push an otherwise-quick image over the OCR timeout, which
    // read as a failure at upload that then "mysteriously" worked fine on a
    // solo retry from the detail page — a fully sequential queue removes
    // that contention rather than just papering over it with a bigger cap.
    const results = await runWithConcurrency(stagedFiles, 1, (file, i) =>
      file.__isImgur
        ? addImgurUrlAndTrack(file.url, i)
        : file.__isYoutube
          ? addContentAndTrack(file.url, i, description, tagList, projectId)
          : uploadAndTrack(file, i, description, tagList, projectId)
    );

    tags = [];
    renderTags();
    e.target.reset();
    resetProjectSelect();
    isUploading = false;

    const failures = results.filter(r => !r.ok).map(r => r.message);
    document.dispatchEvent(new CustomEvent('constructicon:upload-complete', { detail: { failures } }));

    // #240: the auto-match step may have queued "which project?" questions
    // for this batch — let the admin pane refresh its badge/queue now
    // rather than on the next page load.
    document.dispatchEvent(new CustomEvent('constructicon:pending-decisions-changed'));

    if (failures.length === 0) {
      stagedFiles = [];
      fileStates = [];
      dismissedIndices = new Set();
      droppedFolderName = '';
      renderStaged();
      return;
    }

    // Keep only the ones that actually failed staged, so they're easy to retry.
    // (stagedFiles/fileStates are still the full original batch here, indices
    // matching results 1:1 — completed ones were only ever dismissed for
    // display, never spliced out, so this alignment holds.)
    const keptFiles = [], keptStates = [];
    stagedFiles.forEach((f, i) => {
      if (!results[i].ok) { keptFiles.push(f); keptStates.push(fileStates[i]); }
    });
    stagedFiles = keptFiles;
    fileStates = keptStates;
    dismissedIndices = new Set();
    renderStaged();
    uploadError.textContent = `Some uploads failed: ${failures.join('; ')}`;
    uploadError.style.display = 'block';
  });

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // Project dropdown (#1) — replaces the old Client select. Populated from
  // GET /api/projects (every known project, same list the home page's
  // Projects column shows), plus a permanent "+ New project..." option at
  // the bottom that reveals a text input instead of immediately posting —
  // the actual project only gets created on submit (see the form submit
  // handler), so opening the drawer and closing it again without uploading
  // anything never litters the site with an empty project.
  async function loadProjects(selectId) {
    const res = await fetch('/api/projects');
    const projects = await res.json();
    projectSelect.querySelectorAll('option[data-project]').forEach(o => o.remove());
    const newOption = projectSelect.querySelector('option[value="__new__"]');
    const html = projects.map(p => `<option value="${p.id}" data-project>${escapeHtml(p.title)}</option>`).join('');
    if (newOption) newOption.insertAdjacentHTML('beforebegin', html);
    else projectSelect.insertAdjacentHTML('beforeend', html + '<option value="__new__">+ New project…</option>');
    if (selectId !== undefined) projectSelect.value = String(selectId);
  }
  function resetProjectSelect() {
    projectSelect.value = '';
    newProjectInput.style.display = 'none';
    newProjectInput.value = '';
  }
  projectSelect.addEventListener('change', () => {
    const isNew = projectSelect.value === '__new__';
    newProjectInput.style.display = isNew ? 'block' : 'none';
    if (isNew) newProjectInput.focus();
  });
  loadProjects();

  // Exposed for each template's own drag/drop-state-machine wiring (open/
  // close mechanics differ per caller, so the actual window 'drop' listener
  // stays in each template) — addFiles for a plain file drop/pick,
  // walkDirectory + selectProjectForFolder for a folder drop.
  return { addFiles, walkDirectory, selectProjectForFolder };
}
