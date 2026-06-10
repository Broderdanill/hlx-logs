(function () {
  const search = document.getElementById('selectorSearch');
  if (search) {
    search.addEventListener('input', () => {
      const term = search.value.toLowerCase();
      document.querySelectorAll('[data-filter-area] > *').forEach((card) => {
        const haystack = (card.dataset.search || '').toLowerCase();
        card.style.display = haystack.includes(term) ? '' : 'none';
      });
    });
  }

  document.querySelectorAll('[data-preset]').forEach((button) => {
    button.addEventListener('click', () => {
      const preset = button.dataset.preset;
      const boxes = document.querySelectorAll('input[name="log_type_ids"]');
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
    });
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
        if (job.status === 'complete' && job.result_url) {
          addLine('Opening result view...');
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
