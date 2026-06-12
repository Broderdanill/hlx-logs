(function () {
  const search = document.getElementById('selectorSearch');
  const hideZeroLogs = document.getElementById('hideZeroLogs');
  const rowLooksZeroSize = (row) => {
    const cell = row.querySelector('.log-size-cell');
    if (!cell) return false;
    const raw = (cell.textContent || '').trim();
    if (!raw || raw === '-') return false;
    const parts = raw.split(/\n|,/).map((part) => part.trim()).filter(Boolean);
    let sawZero = false;
    for (const part of parts) {
      const value = (part.includes('=') ? part.split('=').pop() : part).trim().toLowerCase();
      if (/[1-9]/.test(value)) return false;
      if (/\b0(?:[.,]0+)?\s*(?:b|bytes|kb|mb|gb)?\b/.test(value)) sawZero = true;
    }
    return sawZero;
  };
  const applyLogFilters = () => {
    const term = (search?.value || '').toLowerCase();
    const hideZero = !!hideZeroLogs?.checked;
    document.querySelectorAll('.dense-table[data-filter-area] > [data-search]').forEach((card) => {
      const haystack = (card.dataset.search || '').toLowerCase();
      const matchesSearch = haystack.includes(term);
      const matchesSize = !hideZero || !rowLooksZeroSize(card);
      card.style.display = (matchesSearch && matchesSize) ? '' : 'none';
    });
  };
  if (search) {
    search.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') event.preventDefault();
    });
    search.addEventListener('input', applyLogFilters);
  }
  if (hideZeroLogs) hideZeroLogs.addEventListener('change', applyLogFilters);

  const applyPreset = (preset) => {
    const boxes = document.querySelectorAll('input[name="log_type_ids"]');
    if (preset === 'custom') {
      boxes.forEach((box) => (box.checked = false));
      return;
    }
    if (preset === 'none') {
      boxes.forEach((box) => (box.checked = false));
    } else if (preset === 'all') {
      boxes.forEach((box) => (box.checked = true));
    } else {
      boxes.forEach((box) => {
        const row = box.closest('[data-preset-tags]');
        const tags = (row?.dataset.presetTags || '').toLowerCase();
        box.checked = tags.includes(preset);
      });
    }
  };
  const presetSelect = document.querySelector('[data-preset-select]');
  if (presetSelect) {
    presetSelect.addEventListener('change', () => applyPreset(presetSelect.value));
  }

  document.querySelectorAll('[data-preset]').forEach((button) => {
    button.addEventListener('click', () => applyPreset(button.dataset.preset));
  });

  if (window.HLX_JOB_ID) {
    const fill = document.getElementById('progressFill');
    const msg = document.getElementById('progressMessage');
    const count = document.getElementById('progressCount');
    const lines = document.getElementById('activityLines');
    const seen = [];
    const addLine = (text) => {
      if (!text || seen[seen.length - 1] === text) return;
      seen.push(text);
      const div = document.createElement('div');
      div.textContent = text;
      lines.prepend(div);
      while (lines.children.length > 7) lines.removeChild(lines.lastChild);
    };
    const poll = async () => {
      try {
        const res = await fetch(`/api/jobs/${window.HLX_JOB_ID}`, {headers: {'Accept': 'application/json'}});
        const job = await res.json();
        const total = Math.max(job.total || 1, 1);
        const current = Math.min(job.current || 0, total);
        const pct = Math.round((current / total) * 100);
        fill.style.width = `${pct}%`;
        msg.textContent = job.message || 'Fetching logs...';
        count.textContent = `${current} / ${total}`;
        addLine(job.message);
        if ((job.status === 'complete' || job.status === 'complete_with_warnings') && job.result_url) {
          addLine(job.status === 'complete_with_warnings' ? 'Opening result view with warnings...' : 'Opening result view...');
          setTimeout(() => { window.location.href = job.result_url; }, 500);
          return;
        }
        if (job.status === 'error') {
          addLine(job.error || 'Collection failed');
          msg.textContent = job.error || 'Collection failed';
          fill.classList.add('failed');
          return;
        }
        setTimeout(poll, 900);
      } catch (err) {
        addLine(`Progress update failed: ${err}`);
        setTimeout(poll, 1500);
      }
    };
    poll();
  }
})();

// 0.0.55: Home page create-collection tabs.
(function () {
  const tabs = Array.from(document.querySelectorAll('[data-home-tab]'));
  if (!tabs.length) return;
  const panels = Array.from(document.querySelectorAll('[data-home-panel]'));
  const activate = (name) => {
    tabs.forEach((tab) => {
      const active = tab.dataset.homeTab === name;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    panels.forEach((panel) => {
      const active = panel.dataset.homePanel === name;
      panel.classList.toggle('active', active);
      panel.hidden = !active;
    });
  };
  tabs.forEach((tab) => tab.addEventListener('click', () => activate(tab.dataset.homeTab)));
  activate('fetch');
})();

// 0.0.34: delayed loading overlay. It should not remain visible when
// navigating back/forward from the browser cache or during quick view changes.
(function () {
  const overlay = document.getElementById('globalLoading');
  if (!overlay) return;
  let timer = null;
  const hide = () => {
    if (timer) { clearTimeout(timer); timer = null; }
    overlay.hidden = true;
  };
  const show = (text) => {
    hide();
    timer = setTimeout(() => {
      const strong = overlay.querySelector('strong');
      if (strong && text) strong.textContent = text;
      overlay.hidden = false;
    }, 450);
  };
  window.addEventListener('pageshow', hide);
  window.addEventListener('load', hide);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) hide(); });
  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', () => {
      if (form.dataset.noLoading === 'true') return;
      const enctype = (form.getAttribute('enctype') || '').toLowerCase();
      show(form.dataset.loadingMessage || (enctype.includes('multipart') ? 'Uploading logs...' : 'Loading...'));
    });
  });
  document.querySelectorAll('a').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
      const href = link.getAttribute('href') || '';
      if (!href || href.startsWith('#') || href.includes('/download')) return;
      if (link.target && link.target !== '_self') return;
      if (href.includes('/results/') || href.includes('/collections') || href === '/' || href.includes('/config')) {
        show('Loading...');
      }
    });
  });
})();

// 0.0.19: Mermaid rendering for visual flow. The source remains visible if the
// browser cannot load Mermaid from CDN in a locked-down environment.
(function () {
  const el = document.getElementById('mermaidDiagram');
  if (!el) return;
  let mermaidZoom = 1.0;
  const applyMermaidZoom = () => {
    const svg = el.querySelector('svg');
    if (!svg) return;
    const viewBox = svg.getAttribute('viewBox');
    let baseW = 1600, baseH = 700;
    if (viewBox) {
      const parts = viewBox.split(/\s+/).map(Number);
      if (parts.length === 4 && parts[2] && parts[3]) { baseW = parts[2]; baseH = parts[3]; }
    } else {
      baseW = svg.getBBox ? Math.max(900, Math.ceil(svg.getBBox().width || 1600)) : 1600;
      baseH = svg.getBBox ? Math.max(500, Math.ceil(svg.getBBox().height || 700)) : 700;
    }
    const w = Math.max(700, Math.round(baseW * mermaidZoom));
    const h = Math.max(420, Math.round(baseH * mermaidZoom));
    // Use !important because the stylesheet intentionally constrains Mermaid SVGs
    // in several places for dark-mode layout. Inline normal styles can be ignored.
    svg.style.setProperty('width', w + 'px', 'important');
    svg.style.setProperty('height', h + 'px', 'important');
    svg.style.setProperty('max-width', 'none', 'important');
    el.style.setProperty('min-width', w + 'px');
    el.style.setProperty('min-height', h + 'px');
    const reset = document.querySelector('[data-mermaid-zoom="reset"]');
    if (reset) reset.textContent = Math.round(mermaidZoom * 100) + '%';
  };
  const actorPalette = {
    client: {fill: '#063b52', stroke: '#00b9f6', text: '#e9fbff'},
    active_link: {fill: '#0b3a28', stroke: '#49c449', text: '#eaffef'},
    guide: {fill: '#4a3608', stroke: '#ffcc33', text: '#fff5c2'},
    filter: {fill: '#0d315b', stroke: '#6ea8ff', text: '#eef6ff'},
    escalation: {fill: '#32164b', stroke: '#c97aff', text: '#f5e9ff'},
    service: {fill: '#4a250b', stroke: '#ff9f43', text: '#fff0dd'},
    error: {fill: '#4a1110', stroke: '#ff4d3f', text: '#ffe2df'},
    data: {fill: '#073343', stroke: '#2a70ad', text: '#e9fbff'},
    sql: {fill: '#3b2d08', stroke: '#ffcc33', text: '#fff5c2'},
    system: {fill: '#1c2b3d', stroke: '#7f93a8', text: '#e8f1fb'},
  };
  const kindFromActorName = (name) => {
    name = String(name || '').toLowerCase();
    if (name.startsWith('active_link_')) return 'active_link';
    if (name.startsWith('client_')) return 'client';
    if (name.startsWith('guide_')) return 'guide';
    if (name.startsWith('filter_')) return 'filter';
    if (name.startsWith('escalation_')) return 'escalation';
    if (name.startsWith('service_')) return 'service';
    if (name.startsWith('error_')) return 'error';
    if (name.startsWith('data_')) return 'data';
    if (name.startsWith('sql_')) return 'sql';
    return 'system';
  };
  const colorMermaidActors = (svg) => {
    svg.querySelectorAll('rect.actor').forEach((rect) => {
      const kind = kindFromActorName(rect.getAttribute('name'));
      const palette = actorPalette[kind] || actorPalette.system;
      rect.setAttribute('fill', palette.fill);
      rect.setAttribute('stroke', palette.stroke);
      rect.style.fill = palette.fill;
      rect.style.stroke = palette.stroke;
      const group = rect.closest('g');
      if (group) group.querySelectorAll('text.actor, text.actor tspan').forEach((txt) => {
        txt.setAttribute('fill', palette.text);
        txt.style.fill = palette.text;
        txt.style.fontWeight = '700';
      });
    });
  };
  const forceDarkMermaid = () => {
    const svg = el.querySelector('svg');
    if (!svg) return;
    svg.style.backgroundColor = '#06192b';
    let bg = svg.querySelector('rect.hlx-mermaid-bg');
    const vb = svg.getAttribute('viewBox');
    if (!bg && vb) {
      const parts = vb.split(/\s+/).map(Number);
      if (parts.length === 4) {
        bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        bg.setAttribute('class', 'hlx-mermaid-bg');
        bg.setAttribute('x', parts[0]); bg.setAttribute('y', parts[1]);
        bg.setAttribute('width', parts[2]); bg.setAttribute('height', parts[3]);
        bg.setAttribute('fill', '#06192b');
        svg.insertBefore(bg, svg.firstChild);
      }
    }
    colorMermaidActors(svg);
  };
  const initMermaid = () => {
    if (!window.mermaid) return;
    try {
      window.mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        maxTextSize: 2000000,
        theme: 'dark',
        themeVariables: {
          background: '#06192b',
          mainBkg: '#092943',
          secondaryColor: '#0e3b65',
          primaryColor: '#092943',
          primaryTextColor: '#f7f7f7',
          primaryBorderColor: '#00b9f6',
          lineColor: '#00b9f6',
          textColor: '#f7f7f7',
          actorBkg: '#0b263f',
          actorBorder: '#00b9f6',
          actorTextColor: '#f7f7f7',
          signalColor: '#f7f7f7',
          signalTextColor: '#f7f7f7',
          noteBkgColor: '#082138',
          noteTextColor: '#f7f7f7',
          noteBorderColor: '#2a70ad'
        },
        sequence: { mirrorActors: true, wrap: true, width: 360, messageAlign: 'left', showSequenceNumbers: false, actorMargin: 90, boxMargin: 12 }
      });
      window.mermaid.run({ nodes: [el] }).then(() => {
        forceDarkMermaid();
        applyMermaidZoom();
      });
    } catch (err) {
      console.warn('Mermaid rendering failed', err);
    }
  };
  if (window.mermaid) {
    initMermaid();
  } else {
    const script = document.createElement('script');
    script.src = '/static/mermaid.min.js';
    script.onload = initMermaid;
    script.onerror = () => console.warn('Could not load Mermaid renderer; source remains visible.');
    document.head.appendChild(script);
  }

  document.querySelectorAll('[data-mermaid-zoom]').forEach((button) => {
    button.addEventListener('click', () => {
      const action = button.dataset.mermaidZoom;
      if (action === 'in') mermaidZoom = Math.min(3.0, mermaidZoom + 0.15);
      else if (action === 'out') mermaidZoom = Math.max(0.35, mermaidZoom - 0.15);
      else mermaidZoom = 1.0;
      applyMermaidZoom();
    });
  });

  document.querySelectorAll('[data-workflow-type]').forEach((button) => {
    button.addEventListener('click', () => {
      const type = button.dataset.workflowType;
      const current = new Set((document.getElementById('workflowTypesInput')?.value || '').split(',').filter(Boolean));
      const allTypes = Array.from(document.querySelectorAll('[data-workflow-type]')).map((b) => b.dataset.workflowType);
      if (current.size === 0) allTypes.forEach((t) => current.add(t));
      if (current.has(type)) current.delete(type); else current.add(type);
      const params = new URLSearchParams(window.location.search);
      params.set('tab', 'visual');
      params.set('wf_types', current.size ? Array.from(current).join(',') : 'none');
      window.location.search = params.toString();
    });
  });

  const downloadButton = document.querySelector('[data-download-mermaid]');
  if (downloadButton) {
    downloadButton.addEventListener('click', () => {
      const svg = el.querySelector('svg');
      if (!svg) return;
      forceDarkMermaid();
      const clone = svg.cloneNode(true);
      clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
      clone.style.backgroundColor = '#06192b';
      const blob = new Blob([new XMLSerializer().serializeToString(clone)], {type: 'image/svg+xml;charset=utf-8'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'hlx-logs-workflow.svg';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
  }
  const copyButton = document.querySelector('[data-copy-mermaid]');
  if (copyButton) {
    copyButton.addEventListener('click', async () => {
      const source = document.getElementById('mermaidSource')?.textContent || el.textContent || '';
      try {
        await navigator.clipboard.writeText(source);
        copyButton.textContent = 'Copied';
        setTimeout(() => (copyButton.textContent = 'Copy Mermaid'), 1200);
      } catch (_) {
        copyButton.textContent = 'Copy failed';
        setTimeout(() => (copyButton.textContent = 'Copy Mermaid'), 1200);
      }
    });
  }
})();

// 0.0.20: top action to reveal upload-to-collection form without taking space in the main result view.
(function () {
  const button = document.querySelector('[data-toggle-upload]');
  const panel = document.getElementById('uploadMorePanel');
  if (!button || !panel) return;
  button.addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  });
})();

// 0.0.24: visible-column picker for log view.
(function () {
  const hidden = document.getElementById('visibleColumnsInput');
  if (!hidden) return;
  const sync = () => {
    const selected = Array.from(document.querySelectorAll('[data-column-toggle]:checked')).map((el) => el.value);
    hidden.value = selected.length ? selected.join(',') : 'time,transaction,message';
  };
  document.querySelectorAll('[data-column-toggle]').forEach((el) => el.addEventListener('change', sync));
  sync();
})();

// 0.0.43: save all AR log-control rows from the separate Log settings page.
(function () {
  const button = document.querySelector('[data-save-log-control-all]');
  const form = document.getElementById('logControlForm');
  if (!button || !form) return;

  const appendHidden = (name, value) => {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value == null ? '' : String(value);
    form.appendChild(input);
  };

  const markDirty = (row) => {
    if (row) {
      row.classList.add('log-control-dirty');
      row.dataset.logControlDirty = 'true';
    }
  };
  document.querySelectorAll('[data-log-control-row]').forEach((row) => {
    row.querySelectorAll('.log-filename-input,.log-control-enabled').forEach((field) => {
      field.addEventListener('change', () => markDirty(row));
      field.addEventListener('input', () => markDirty(row));
    });
  });

  button.addEventListener('click', () => {
    form.replaceChildren();
    let count = 0;
    document.querySelectorAll('[data-log-control-row]').forEach((row) => {
      const key = row.dataset.logKey || '';
      if (!key || row.dataset.logControlDirty !== 'true') return;
      const filenameInput = row.querySelector('.log-filename-input');
      const enabledInput = row.querySelector('.log-control-enabled');
      appendHidden('log_keys', key);
      appendHidden('log_control_filenames', filenameInput ? filenameInput.value : '');
      appendHidden('log_control_enabled_values', enabledInput && enabledInput.checked ? 'on' : 'off');
      count += 1;
    });
    if (!count) {
      button.textContent = 'No changes to save';
      setTimeout(() => { button.textContent = 'Save log settings'; }, 1300);
      return;
    }
    button.disabled = true;
    button.textContent = 'Saving...';
    form.submit();
  });
})();

// 0.0.45: Log settings templates for AR Debug-mode bitmask checkboxes.
(function () {
  const select = document.getElementById('logSettingsTemplate');
  if (!select) return;
  select.addEventListener('change', () => {
    const values = new Set((select.value || '').split(',').map((v) => v.trim()).filter(Boolean));
    document.querySelectorAll('[data-log-control-row]').forEach((row) => {
      const key = row.dataset.logKey || '';
      const checkbox = row.querySelector('.log-control-enabled');
      if (checkbox) checkbox.checked = values.has(key);
    });
  });
})();

// 0.0.57: disable settings fields that are not active, for a cleaner Log settings UI.
(function () {
  const syncRow = (row) => {
    const enabled = row.querySelector('.log-control-enabled');
    const filename = row.querySelector('.log-filename-input');
    if (!enabled || !filename) return;
    filename.disabled = !enabled.checked;
    filename.readOnly = !enabled.checked;
    row.classList.toggle('log-control-inactive', !enabled.checked);
  };
  const syncAllRows = () => document.querySelectorAll('[data-log-control-row]').forEach(syncRow);
  syncAllRows();
  document.querySelectorAll('[data-log-control-row] .log-control-enabled').forEach((box) => {
    box.addEventListener('change', () => syncRow(box.closest('[data-log-control-row]')));
  });
  const restrictToggle = document.querySelector('input[name="restrict_log_users_enabled"]');
  const restrictInput = document.querySelector('input[name="restrict_log_users"]');
  const syncRestrict = () => {
    if (!restrictToggle || !restrictInput) return;
    restrictInput.disabled = !restrictToggle.checked;
    restrictInput.readOnly = !restrictToggle.checked;
  };
  syncRestrict();
  if (restrictToggle) restrictToggle.addEventListener('change', syncRestrict);
  const template = document.getElementById('logSettingsTemplate');
  if (template) template.addEventListener('change', () => window.setTimeout(syncAllRows, 0));
})();
