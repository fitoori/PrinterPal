(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const state = {
    selectedFile: null,
    printers: [],
    config: null,
    ui: {
      dark: false,
      eink: false,
    },
  };

  function setFooterStatus(text) {
    const el = $('ppFooterStatus');
    if (el) el.textContent = text;
  }

  function showMsg(elId, msg, isError) {
    const el = $(elId);
    if (!el) return;
    el.textContent = msg || '';
    el.classList.toggle('pp-msg-error', !!isError);
  }

  async function apiGet(url) {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${txt}`);
    }
    return await res.json();
  }

  async function apiPost(url, bodyObj) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: bodyObj ? JSON.stringify(bodyObj) : '{}',
    });
    const txt = await res.text();
    let data = null;
    try { data = txt ? JSON.parse(txt) : null; } catch (_) { /* noop */ }
    if (!res.ok) {
      const msg = (data && (data.error || data.message)) ? (data.error || data.message) : txt;
      throw new Error(`${res.status} ${res.statusText}: ${msg}`);
    }
    return data;
  }

  function formatTime(ts) {
    try {
      const d = new Date(ts * 1000);
      return d.toLocaleString();
    } catch (_) {
      return '';
    }
  }

  function applyUiClasses() {
    document.body.classList.toggle('mode-dark', state.ui.dark && !state.ui.eink);
    document.body.classList.toggle('mode-eink', state.ui.eink);

    const darkBtn = $('ppMenuDarkMode');
    const einkBtn = $('ppMenuEinkMode');
    if (darkBtn) darkBtn.setAttribute('aria-checked', state.ui.dark ? 'true' : 'false');
    if (einkBtn) einkBtn.setAttribute('aria-checked', state.ui.eink ? 'true' : 'false');
  }

  function loadUiPrefs() {
    const defaults = window.PRINTERPAL_UI_DEFAULTS || {};
    const lsDark = localStorage.getItem('printerpal_ui_dark');
    const lsEink = localStorage.getItem('printerpal_ui_eink');
    state.ui.dark = (lsDark === null) ? !!defaults.default_dark_mode : (lsDark === '1');
    state.ui.eink = (lsEink === null) ? !!defaults.default_eink_mode : (lsEink === '1');
    applyUiClasses();
  }

  function saveUiPrefs() {
    localStorage.setItem('printerpal_ui_dark', state.ui.dark ? '1' : '0');
    localStorage.setItem('printerpal_ui_eink', state.ui.eink ? '1' : '0');
  }

  function toggleDropdown(open) {
    const dd = $('ppMenuDropdown');
    const btn = $('ppMenuButton');
    if (!dd || !btn) return;

    const willOpen = (open === undefined) ? !dd.classList.contains('open') : !!open;
    dd.classList.toggle('open', willOpen);
    btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  }

  function renderFileList(files) {
    const list = $('ppFileList');
    if (!list) return;
    list.innerHTML = '';

    if (!files || files.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'pp-muted';
      empty.textContent = 'No uploads yet.';
      list.appendChild(empty);
      return;
    }

    files.forEach((f) => {
      const row = document.createElement('div');
      row.className = 'pp-file';
      row.setAttribute('role', 'listitem');
      row.tabIndex = 0;

      if (state.selectedFile && state.selectedFile.name === f.name) {
        row.classList.add('selected');
      }

      const left = document.createElement('div');
      const name = document.createElement('div');
      name.className = 'pp-file-name';
      name.textContent = f.name;
      const meta = document.createElement('div');
      meta.className = 'pp-file-meta';
      meta.textContent = `${f.size_h} • ${formatTime(f.mtime)}`;
      left.appendChild(name);
      left.appendChild(meta);

      const right = document.createElement('div');
      const dl = document.createElement('a');
      dl.href = `/uploads/${encodeURIComponent(f.name)}`;
      dl.textContent = 'Download';
      dl.className = 'pp-file-meta';
      dl.addEventListener('click', (e) => e.stopPropagation());
      right.appendChild(dl);

      row.appendChild(left);
      row.appendChild(right);

      const pick = () => selectFile(f);
      row.addEventListener('click', pick);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter') pick(); });

      list.appendChild(row);
    });
  }

  function renderPrinters(printers, defaultPrinter) {
    const sel = $('ppPrinterSelect');
    if (!sel) return;

    sel.innerHTML = '';

    const optAuto = document.createElement('option');
    optAuto.value = '';
    optAuto.textContent = defaultPrinter ? `System default (${defaultPrinter})` : 'System default';
    sel.appendChild(optAuto);

    (printers || []).forEach((p) => {
      const opt = document.createElement('option');
      opt.value = p.name;
      const label = `${p.name} • ${p.state}${p.accepting === false ? ' • not accepting' : ''}`;
      opt.textContent = label;
      sel.appendChild(opt);
    });

    if (state.config && state.config.printing && state.config.printing.default_printer) {
      sel.value = state.config.printing.default_printer;
    }
  }

  function renderQueue(jobs) {
    const el = $('ppQueue');
    if (!el) return;
    el.innerHTML = '';

    if (!jobs || jobs.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'pp-muted';
      empty.textContent = 'Queue is empty.';
      el.appendChild(empty);
      return;
    }

    jobs.forEach((j) => {
      const item = document.createElement('div');
      item.className = 'pp-queue-item';
      item.textContent = j.raw || `${j.job_id}`;
      el.appendChild(item);
    });
  }

  function setPreviewVisible(visible) {
    const img = $('ppPreviewImg');
    const ph = $('ppPreviewPlaceholder');
    if (img) img.style.display = visible ? 'block' : 'none';
    if (ph) ph.style.display = visible ? 'none' : 'block';
  }

  function updatePreview() {
    const img = $('ppPreviewImg');
    if (!img) return;

    if (!state.selectedFile) {
      setPreviewVisible(false);
      return;
    }

    const mode = $('ppModeSelect') ? $('ppModeSelect').value : (window.PRINTERPAL_DEFAULT_MODE || 'grayscale');
    const page = $('ppPageInput') ? Math.max(1, parseInt($('ppPageInput').value || '1', 10)) : 1;
    const w = Math.min(1400, Math.max(320, Math.floor((img.parentElement ? img.parentElement.clientWidth : 720) - 24)));

    img.onload = () => { setPreviewVisible(true); };
    img.onerror = () => {
      setPreviewVisible(false);
      showMsg('ppActionMsg', 'Preview failed. (Is the file type supported?)', true);
    };

    const url = `/api/preview/${encodeURIComponent(state.selectedFile.name)}?mode=${encodeURIComponent(mode)}&page=${page}&w=${w}&_=${Date.now()}`;
    img.src = url;
  }

  function selectFile(file) {
    state.selectedFile = file;
    renderFileList(state.lastFiles || []);
    $('ppPrintBtn').disabled = false;
    showMsg('ppActionMsg', '', false);
    updatePreview();
  }

  async function refreshAllOnce() {
    const [files, status, cfg] = await Promise.all([
      apiGet('/api/files'),
      apiGet('/api/status'),
      apiGet('/api/config'),
    ]);

    state.lastFiles = files.files || [];
    renderFileList(state.lastFiles);

    state.config = cfg.config || null;
    populateSettings(state.config);

    const s = status;
    $('ppCupsStatus').textContent = s.cups_available ? 'Available' : 'Not available';
    $('ppDefaultPrinter').textContent = s.default_printer || '—';
    $('ppActiveJobs').textContent = (s.stats && (s.stats.active_jobs !== undefined)) ? String(s.stats.active_jobs) : '—';
    $('ppCompletedJobs').textContent = (s.stats && (s.stats.completed_jobs !== undefined)) ? String(s.stats.completed_jobs) : '—';

    renderPrinters(s.printers || [], s.default_printer || '');
    renderQueue(s.jobs || []);

    setFooterStatus('Live');
  }

  function populateSettings(cfg) {
    if (!cfg) return;
    const p = cfg.printing || {};
    const a = cfg.airprint || {};

    const dp = $('ppCfgDefaultPrinter');
    const pr = $('ppCfgPreviewDpi');
    const pd = $('ppCfgPrintDpi');
    const th = $('ppCfgThreshold');
    const mp = $('ppCfgMaxPages');
    const ap = $('ppCfgAirPrint');

    if (dp) dp.value = p.default_printer || '';
    if (pr) pr.value = p.preview_dpi;
    if (pd) pd.value = p.print_dpi;
    if (th) th.value = p.bw_threshold;
    if (mp) mp.value = p.max_pdf_pages_process;
    if (ap) ap.checked = !!a.auto_enable;

    const modeSel = $('ppModeSelect');
    if (modeSel && p.default_mode) modeSel.value = p.default_mode;

    const copies = $('ppCopiesInput');
    if (copies && p.default_copies) copies.value = p.default_copies;
  }

  async function doPrint() {
    const btn = $('ppPrintBtn');
    if (!state.selectedFile) return;

    const mode = $('ppModeSelect').value;
    const page = parseInt($('ppPageInput').value || '1', 10);
    if (!Number.isFinite(page) || page < 1) {
      showMsg('ppActionMsg', 'Invalid page number.', true);
      return;
    }

    const copies = parseInt($('ppCopiesInput').value || '1', 10);
    if (!Number.isFinite(copies) || copies < 1 || copies > 99) {
      showMsg('ppActionMsg', 'Copies must be 1–99.', true);
      return;
    }

    const printer = $('ppPrinterSelect').value;

    btn.disabled = true;
    showMsg('ppActionMsg', 'Sending job to CUPS…', false);

    try {
      const res = await apiPost('/api/print', {
        filename: state.selectedFile.name,
        mode,
        printer,
        copies,
      });
      showMsg('ppActionMsg', res.lp_stdout ? `Queued: ${res.lp_stdout}` : 'Queued.', false);
    } catch (e) {
      showMsg('ppActionMsg', `Print failed: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      // Refresh status quickly.
      try { await refreshStatusOnly(); } catch (_) { /* noop */ }
    }
  }

  async function refreshStatusOnly() {
    const status = await apiGet('/api/status');
    $('ppCupsStatus').textContent = status.cups_available ? 'Available' : 'Not available';
    $('ppDefaultPrinter').textContent = status.default_printer || '—';
    $('ppActiveJobs').textContent = (status.stats && (status.stats.active_jobs !== undefined)) ? String(status.stats.active_jobs) : '—';
    $('ppCompletedJobs').textContent = (status.stats && (status.stats.completed_jobs !== undefined)) ? String(status.stats.completed_jobs) : '—';
    renderPrinters(status.printers || [], status.default_printer || '');
    renderQueue(status.jobs || []);
  }

  function wireMenu() {
    const btn = $('ppMenuButton');
    if (btn) {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleDropdown();
      });
    }

    document.addEventListener('click', () => toggleDropdown(false));

    const dark = $('ppMenuDarkMode');
    const eink = $('ppMenuEinkMode');
    const restart = $('ppMenuRestartHost');
    const airprint = $('ppMenuEnsureAirPrint');

    if (dark) {
      dark.addEventListener('click', (e) => {
        e.stopPropagation();
        state.ui.dark = !state.ui.dark;
        if (state.ui.dark && state.ui.eink) state.ui.eink = false;
        saveUiPrefs();
        applyUiClasses();
      });
    }

    if (eink) {
      eink.addEventListener('click', (e) => {
        e.stopPropagation();
        state.ui.eink = !state.ui.eink;
        if (state.ui.eink) state.ui.dark = false;
        saveUiPrefs();
        applyUiClasses();
      });
    }

    if (restart) {
      restart.addEventListener('click', async (e) => {
        e.stopPropagation();
        toggleDropdown(false);
        const ok = confirm('Restart the host (Raspberry Pi) now? This will interrupt prints.');
        if (!ok) return;
        showMsg('ppActionMsg', 'Restart requested…', false);
        try {
          await apiPost('/api/restart-host', {});
          showMsg('ppActionMsg', 'Host restart command sent.', false);
        } catch (err) {
          showMsg('ppActionMsg', `Restart failed: ${err.message}`, true);
        }
      });
    }

    if (airprint) {
      airprint.addEventListener('click', async (e) => {
        e.stopPropagation();
        toggleDropdown(false);
        showMsg('ppActionMsg', 'Refreshing AirPrint advertising…', false);
        try {
          const res = await apiPost('/api/airprint/ensure', {});
          showMsg('ppActionMsg', res.output ? res.output : 'AirPrint refresh completed.', false);
        } catch (err) {
          showMsg('ppActionMsg', `AirPrint refresh failed: ${err.message}`, true);
        }
      });
    }
  }

  function wireSettings() {
    const open = $('ppOpenSettings');
    const panel = $('ppSettingsPanel');
    const close = $('ppCloseSettings');
    const save = $('ppSaveSettings');

    if (open && panel) {
      open.addEventListener('click', () => {
        panel.hidden = !panel.hidden;
      });
    }

    if (close && panel) {
      close.addEventListener('click', () => {
        panel.hidden = true;
      });
    }

    if (save) {
      save.addEventListener('click', async () => {
        if (!state.config) {
          showMsg('ppSettingsMsg', 'Config not loaded.', true);
          return;
        }

        const cfg = JSON.parse(JSON.stringify(state.config));
        cfg.printing = cfg.printing || {};
        cfg.airprint = cfg.airprint || {};

        const dp = $('ppCfgDefaultPrinter');
        const pr = $('ppCfgPreviewDpi');
        const pd = $('ppCfgPrintDpi');
        const th = $('ppCfgThreshold');
        const mp = $('ppCfgMaxPages');
        const ap = $('ppCfgAirPrint');

        cfg.printing.default_printer = dp ? String(dp.value || '').trim() : '';
        cfg.printing.preview_dpi = pr ? parseInt(pr.value, 10) : cfg.printing.preview_dpi;
        cfg.printing.print_dpi = pd ? parseInt(pd.value, 10) : cfg.printing.print_dpi;
        cfg.printing.bw_threshold = th ? parseInt(th.value, 10) : cfg.printing.bw_threshold;
        cfg.printing.max_pdf_pages_process = mp ? parseInt(mp.value, 10) : cfg.printing.max_pdf_pages_process;
        cfg.airprint.auto_enable = ap ? !!ap.checked : cfg.airprint.auto_enable;

        showMsg('ppSettingsMsg', 'Saving…', false);
        try {
          const res = await apiPost('/api/config', { config: cfg });
          state.config = res.config;
          populateSettings(state.config);
          showMsg('ppSettingsMsg', 'Saved.', false);
          await refreshStatusOnly();
        } catch (err) {
          showMsg('ppSettingsMsg', `Save failed: ${err.message}`, true);
        }
      });
    }
  }

  function wirePreviewControls() {
    const mode = $('ppModeSelect');
    const page = $('ppPageInput');

    if (mode) mode.addEventListener('change', updatePreview);
    if (page) page.addEventListener('change', updatePreview);

    window.addEventListener('resize', () => {
      // Light debounce.
      if (state._resizeTimer) clearTimeout(state._resizeTimer);
      state._resizeTimer = setTimeout(updatePreview, 120);
    });
  }

  function wireActions() {
    const refresh = $('ppRefreshFiles');
    if (refresh) refresh.addEventListener('click', async () => {
      try {
        const files = await apiGet('/api/files');
        state.lastFiles = files.files || [];
        renderFileList(state.lastFiles);
        showMsg('ppActionMsg', 'Files refreshed.', false);
      } catch (e) {
        showMsg('ppActionMsg', `Refresh failed: ${e.message}`, true);
      }
    });

    const printBtn = $('ppPrintBtn');
    if (printBtn) printBtn.addEventListener('click', doPrint);
  }

  function wireSse() {
    let es = null;
    try {
      es = new EventSource('/events');
    } catch (_) {
      setFooterStatus('No live updates');
      return;
    }

    es.addEventListener('status', (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        setFooterStatus('Live');

        if (payload.files) {
          state.lastFiles = payload.files;
          renderFileList(state.lastFiles);
        }

        const s = payload.status || {};
        $('ppCupsStatus').textContent = s.cups_available ? 'Available' : 'Not available';
        $('ppDefaultPrinter').textContent = s.default_printer || '—';
        $('ppActiveJobs').textContent = (s.stats && (s.stats.active_jobs !== undefined)) ? String(s.stats.active_jobs) : '—';
        $('ppCompletedJobs').textContent = (s.stats && (s.stats.completed_jobs !== undefined)) ? String(s.stats.completed_jobs) : '—';
        renderPrinters(s.printers || [], s.default_printer || '');
        renderQueue(s.jobs || []);

        // Keep preview in sync if file still exists.
        if (state.selectedFile) {
          const exists = (state.lastFiles || []).some((f) => f.name === state.selectedFile.name);
          if (!exists) {
            state.selectedFile = null;
            $('ppPrintBtn').disabled = true;
            setPreviewVisible(false);
          }
        }
      } catch (_) {
        // ignore parse errors
      }
    });

    es.addEventListener('error', () => {
      setFooterStatus('Reconnecting…');
    });
  }

  async function init() {
    loadUiPrefs();
    wireMenu();
    wireSettings();
    wirePreviewControls();
    wireActions();

    // Default mode on load.
    const modeSel = $('ppModeSelect');
    if (modeSel && window.PRINTERPAL_DEFAULT_MODE) {
      modeSel.value = window.PRINTERPAL_DEFAULT_MODE;
    }

    // Disable print until a file is selected.
    const printBtn = $('ppPrintBtn');
    if (printBtn) printBtn.disabled = true;

    try {
      await refreshAllOnce();
      wireSse();
    } catch (e) {
      setFooterStatus('Disconnected');
      showMsg('ppActionMsg', `Startup failed: ${e.message}`, true);
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
