/* Dikarya Presentations browser client.
 *
 * The browser holds the project (sources, selections, overrides, order,
 * settings). The server holds a temporary "workspace" of iNaturalist metadata
 * and does all searching, sorting, slide planning and PowerPoint generation.
 * All text from iNaturalist is inserted with textContent, never as HTML.
 */
(() => {
  'use strict';

  // ---------------------------------------------------------------- helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k === 'dataset') Object.assign(node.dataset, v);
        else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
        else if (k in node && typeof v !== 'string') node[k] = v;
        else node.setAttribute(k, v === true ? '' : v);
      }
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid);
    }
    return node;
  }

  const fmt = new Intl.NumberFormat('en-US');
  const n = (x) => fmt.format(x || 0);
  const plural = (count, one, many) => `${n(count)} ${count === 1 ? one : (many || one + 's')}`;
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  function formatDate(iso) {
    if (!iso || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return '';
    const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
    if (!m || m > 12) return '';
    return `${MONTHS[m - 1]} ${d}, ${y}`;
  }
  const photoSize = (url, size) => url.replace(/\/square\.(\w+)$/, `/${size}.$1`);

  let toastTimer = null;
  function toast(message, isError = false, ms = 4200) {
    const t = $('#toast');
    t.textContent = message;
    t.classList.toggle('error', isError);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ms);
  }

  async function api(path, { method = 'GET', body, raw } = {}) {
    const opts = { method, headers: {} };
    if (raw !== undefined) { opts.body = raw; opts.headers['Content-Type'] = 'application/json'; }
    else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
    let res;
    try { res = await fetch(path, opts); } catch (e) { throw new Error('Could not reach the server. Check your connection.'); }
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) {
      const err = new Error((data && data.detail) || `Request failed (HTTP ${res.status}).`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  // ---------------------------------------------------------------- state
  const LIMITS = {
    sources: Number(document.body.dataset.maxSources) || 20,
    imageSlides: Number(document.body.dataset.maxImageSlides) || 2000,
  };

  function newProject() {
    return {
      format: 'dikarya-presentation',
      schema_version: 1,
      sources: [],
      settings: {
        title: '', presenter: '', background: 'black', title_photo: null,
        sort: { key: 'taxonomic', direction: 'asc', base_key: 'taxonomic', base_direction: 'asc' },
        grouping: 'family', dividers: false, min_faves: 0,
        annotations: { scientific: true, common_name: false, date: false, location: false, observer: null },
        speaker_notes: true,
      },
      observations: [],
      order: [],
      ignored_observation_ids: [],
    };
  }

  const S = {
    project: newProject(),
    states: new Map(),       // observation id -> project observation state
    obs: new Map(),          // observation id -> iNaturalist metadata (from server)
    workspaceId: null,
    loadedSources: new Set(),// source ids present in the current workspace
    sourceCounts: {},
    pendingNew: null,        // last refresh's unreviewed new observations
    dirty: false,
    step: 'sources',
    plan: null,
  };

  function setProject(project) {
    S.project = project;
    reindex();
  }
  function reindex() {
    S.states = new Map(S.project.observations.map((st) => [st.id, st]));
  }
  function markDirty() {
    S.dirty = true;
    autosave();
  }
  const autosave = debounce(() => {
    try {
      localStorage.setItem('dp-project', JSON.stringify(S.project));
    } catch (e) { /* storage full or unavailable: project files still work */ }
  }, 800);

  // Mirrors of small server rules, used only for immediate display.
  function effectiveName(id) {
    const st = S.states.get(id);
    const o = S.obs.get(id);
    if (st && st.overrides && st.overrides.scientific !== null && st.overrides.scientific !== undefined) return st.overrides.scientific;
    return (o && o.inat_name) || '';
  }
  function observerDefault() {
    const active = S.project.sources.filter((s) => s.enabled);
    return (active.length ? active : S.project.sources).some((s) => s.type === 'url');
  }
  function observerEnabled() {
    const v = S.project.settings.annotations.observer;
    return v === null || v === undefined ? observerDefault() : v;
  }
  function enabledSourceIds() {
    return new Set(S.project.sources.filter((s) => s.enabled).map((s) => s.id));
  }
  function hiddenBySources(st) {
    if (!st.source_ids || !st.source_ids.length) return false;
    const en = enabledSourceIds();
    return !st.source_ids.some((sid) => en.has(sid));
  }
  function groupLabel(id) {
    const g = S.project.settings.grouping;
    if (g === 'none') return '';
    const o = S.obs.get(id);
    if (!o) return '';
    if (g === 'source') {
      const st = S.states.get(id);
      const en = enabledSourceIds();
      const ids = (st && st.source_ids) || [];
      const src = S.project.sources.find((s) => ids.includes(s.id) && en.has(s.id)) || S.project.sources.find((s) => ids.includes(s.id));
      return src ? (src.label || src.url) : 'Other observations';
    }
    return (o.ranks && o.ranks[g]) || 'Unclassified';
  }
  const GROUP_NAMES = { kingdom: 'Kingdom', phylum: 'Phylum', class: 'Class', order: 'Order', family: 'Family', genus: 'Genus', source: 'Source' };

  function selectedCount(st) {
    const o = S.obs.get(st.id);
    if (!o) return 0;
    const ids = new Set(o.photos.map((p) => p.id));
    return st.selected_photo_ids.filter((p) => ids.has(p)).length;
  }
  function slideEligible(st) {
    const o = S.obs.get(st.id);
    if (!o || st.status !== 'active') return false;
    if ((o.faves_count || 0) < (S.project.settings.min_faves || 0)) return false;
    return !hiddenBySources(st);
  }

  // ---------------------------------------------------------------- steps
  const STEPS = ['sources', 'organize', 'settings', 'preview', 'generate'];
  function hasObservations() { return S.project.observations.length > 0 && !!S.workspaceId; }

  function updateStepper() {
    $$('#stepper button').forEach((b) => {
      const step = b.dataset.step;
      b.disabled = step !== 'sources' && !hasObservations();
      b.classList.toggle('active', step === S.step);
      b.classList.toggle('done', STEPS.indexOf(step) < STEPS.indexOf(S.step));
    });
  }

  function goto(step) {
    if (step !== 'sources' && !hasObservations()) return;
    S.step = step;
    STEPS.forEach((s) => { $(`#step-${s}`).hidden = s !== step; });
    updateStepper();
    window.scrollTo({ top: 0 });
    if (step === 'sources') renderSources();
    if (step === 'organize') renderOrganize();
    if (step === 'settings') renderSettings();
    if (step === 'preview') renderPreview();
    if (step === 'generate') renderGenerate();
  }

  $('#stepper').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-step]');
    if (b && !b.disabled) goto(b.dataset.step);
  });
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-goto]');
    if (b) goto(b.dataset.goto);
  });

  // ---------------------------------------------------------------- 1. sources
  function newSourceId() {
    const a = new Uint8Array(6);
    crypto.getRandomValues(a);
    return 's' + Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
  }

  // The username box is remembered between sources and visits.
  try { $('#source-username').value = localStorage.getItem('dp-username') || ''; } catch (e) { /* ignore */ }

  $('#source-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const urlInput = $('#source-url');
    const url = urlInput.value.trim();
    const username = $('#source-username').value.trim().replace(/^@/, '');
    $('#source-error').textContent = '';
    if (!url) {
      $('#source-error').textContent = 'Paste the link to an iNaturalist observations search.';
      return;
    }
    if (S.project.sources.length >= LIMITS.sources) {
      $('#source-error').textContent = `A project can have at most ${LIMITS.sources} sources.`;
      return;
    }
    const btn = $('#btn-add-source');
    btn.disabled = true;
    try {
      const parsed = await api('/api/sources/parse', { method: 'POST', body: { url, username } });
      if (S.project.sources.some((s) => s.url === parsed.url)) {
        $('#source-error').textContent = 'That source is already in the project.';
        return;
      }
      S.project.sources.push({
        id: newSourceId(), type: parsed.type, input: parsed.input, username: parsed.username,
        url: parsed.url, label: parsed.label, enabled: true,
      });
      urlInput.value = '';
      try { localStorage.setItem('dp-username', username); } catch (err) { /* ignore */ }
      markDirty();
      renderSources();
    } catch (err) {
      $('#source-error').textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });

  function removeSourceLocal(sourceId) {
    // Mirror of project.remove_source: observations only this source matched leave.
    const p = S.project;
    p.sources = p.sources.filter((s) => s.id !== sourceId);
    const dropped = new Set();
    p.observations = p.observations.filter((st) => {
      if (!st.source_ids.includes(sourceId)) return true;
      st.source_ids = st.source_ids.filter((s) => s !== sourceId);
      if (!st.source_ids.length) { dropped.add(st.id); return false; }
      return true;
    });
    p.order = p.order.filter((id) => !dropped.has(id));
    if (p.settings.title_photo && dropped.has(p.settings.title_photo.observation_id)) p.settings.title_photo = null;
    S.loadedSources.delete(sourceId);
    if (S.pendingNew) {
      // New observations waiting for review that only this source matched can no longer be added.
      const m = S.pendingNew.membership || {};
      S.pendingNew.new_ids = (S.pendingNew.new_ids || []).filter((id) =>
        (m[String(id)] || []).some((sid) => sid !== sourceId && p.sources.some((s) => s.id === sid)));
      if (S.pendingNew.new_by_source) delete S.pendingNew.new_by_source[sourceId];
      if (!S.pendingNew.new_ids.length) S.pendingNew = null;
    }
    reindex();
    return dropped.size;
  }

  function renderSources() {
    const list = $('#source-list');
    list.replaceChildren();
    $('#sources-empty').hidden = S.project.sources.length > 0;
    S.project.sources.forEach((src) => {
      const count = S.sourceCounts[src.id];
      const item = el('li', { class: 'source-item' + (src.enabled ? '' : ' disabled') },
        el('input', {
          type: 'checkbox', checked: src.enabled, title: 'Include this source', 'aria-label': 'Include this source',
          onchange: (e) => { src.enabled = e.target.checked; markDirty(); renderSources(); },
        }),
        el('span', { class: 'badge ' + src.type, text: src.type === 'username' ? 'Mine' : 'Search', title: src.type === 'username' ? `Your observations (${src.username}); photo credits start off` : 'May include other people\'s photos; photo credits start on' }),
        el('div', { class: 'source-meta' },
          el('input', {
            type: 'text', class: 'label-input', value: src.label, maxLength: 120, 'aria-label': 'Source label',
            onchange: (e) => { src.label = e.target.value.trim().slice(0, 120); markDirty(); },
          }),
          el('a', { class: 'url', href: src.url, target: '_blank', rel: 'noopener noreferrer', text: src.url }),
        ),
        el('span', { class: 'count-pill', text: count === undefined ? '' : plural(count, 'observation') }),
        el('button', {
          type: 'button', class: 'btn btn-small btn-danger', text: 'Remove',
          onclick: () => {
            const exclusive = S.project.observations.filter((st) => st.source_ids.length === 1 && st.source_ids[0] === src.id).length;
            if (exclusive && !confirm(`Remove "${src.label}"? ${plural(exclusive, 'observation')} found only by this source will leave the project.`)) return;
            const dropped = removeSourceLocal(src.id);
            markDirty();
            renderSources();
            updateStepper();
            if (dropped) toast(`Removed ${plural(dropped, 'observation')} that only this source found.`);
          },
        }),
      );
      list.append(item);
    });
    const enabled = S.project.sources.filter((s) => s.enabled);
    const unloaded = enabled.filter((s) => !S.loadedSources.has(s.id));
    const btn = $('#btn-load');
    btn.disabled = enabled.length === 0;
    if (!hasObservations()) {
      btn.textContent = 'Load observations';
      $('#load-hint').textContent = enabled.length ? '' : 'Add a source to begin.';
    } else if (unloaded.length) {
      btn.textContent = `Load ${plural(unloaded.length, 'new source')}`;
      $('#load-hint').textContent = 'New observations will be offered for review, not added automatically.';
    } else {
      btn.textContent = 'Refresh from iNaturalist';
      $('#load-hint').textContent = 'Re-query every source for updated names and new observations.';
    }
  }

  $('#btn-load').addEventListener('click', () => {
    const enabled = S.project.sources.filter((s) => s.enabled);
    if (!enabled.length) return;
    if (!hasObservations()) return startLoad(null);
    const unloaded = enabled.filter((s) => !S.loadedSources.has(s.id)).map((s) => s.id);
    return startLoad(unloaded.length ? unloaded : null);
  });

  // ---------------------------------------------------------------- loading
  let loadJobId = null;
  let loadCancelled = false;

  async function pollJob(id, onProgress) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 900));
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
      onProgress(job);
      if (['done', 'error', 'cancelled'].includes(job.state)) return job;
    }
  }

  function setBar(bar, done, total) {
    const prog = bar.parentElement;
    if (!total) { prog.classList.add('indeterminate'); return; }
    prog.classList.remove('indeterminate');
    bar.style.width = `${Math.min(100, Math.round((100 * done) / total))}%`;
  }

  async function startLoad(refreshSourceIds) {
    const modal = $('#load-modal');
    $('#load-message').textContent = 'Starting…';
    setBar($('#load-bar'), 0, 0);
    modal.hidden = false;
    loadCancelled = false;
    try {
      const job = await api('/api/load', {
        method: 'POST',
        body: {
          project: S.project,
          workspace_id: refreshSourceIds ? S.workspaceId : null,
          refresh_source_ids: refreshSourceIds,
        },
      });
      loadJobId = job.id;
      const final = await pollJob(job.id, (j) => {
        $('#load-message').textContent = j.message || 'Working…';
        setBar($('#load-bar'), j.done, j.total);
      });
      if (final.state === 'cancelled' || loadCancelled) { toast('Loading cancelled.'); return; }
      if (final.state !== 'done') throw new Error(final.error || 'Loading failed.');
      applyLoadResult(final.result);
    } catch (err) {
      toast(err.message, true, 9000);
    } finally {
      modal.hidden = true;
      loadJobId = null;
    }
  }

  $('#btn-cancel-load').addEventListener('click', async () => {
    loadCancelled = true;
    if (loadJobId) { try { await api(`/api/jobs/${encodeURIComponent(loadJobId)}`, { method: 'DELETE' }); } catch (e) { /* ignore */ } }
  });

  function applyLoadResult(result) {
    setProject(result.project);
    S.obs = new Map(result.observations.map((o) => [o.id, o]));
    S.workspaceId = result.workspace_id;
    S.loadedSources = new Set(Object.keys(result.source_results));
    S.sourceCounts = result.source_results;
    markDirty();
    const sum = result.summary;
    if (result.first_load) {
      S.pendingNew = null;
      toast(`Loaded ${plural(sum.auto_added || 0, 'observation')} from iNaturalist.`);
      goto('organize');
    } else {
      S.pendingNew = sum.new ? sum : null;
      showReview(sum);
      if (S.step === 'sources') goto('organize'); else goto(S.step);
    }
  }

  // ---------------------------------------------------------------- 2. organize
  let listRenderer = null;
  let renderedCards = new Map();

  function chunkRender(container, sentinel, items, renderItem, minCount = 0, chunk = 80) {
    let i = 0;
    let io = null;
    function more(count) {
      const frag = document.createDocumentFragment();
      const end = Math.min(items.length, i + (count || chunk));
      for (; i < end; i++) frag.append(renderItem(items[i], i));
      container.append(frag);
      if (i >= items.length && io) { io.disconnect(); io = null; }
    }
    container.replaceChildren();
    more(Math.max(chunk, minCount));
    if (i < items.length) {
      // An observer only fires when intersection changes, so re-observe after
      // each chunk: if the sentinel is still near the viewport it fires again.
      io = new IntersectionObserver((entries) => {
        if (!entries.some((e) => e.isIntersecting) || !io) return;
        more();
        if (io) { io.unobserve(sentinel); io.observe(sentinel); }
      }, { rootMargin: '1500px' });
      io.observe(sentinel);
    }
    return { disconnect() { if (io) io.disconnect(); io = null; }, get count() { return i; } };
  }

  function findMatches(id, needle) {
    if (!needle) return true;
    const o = S.obs.get(id);
    const hay = [effectiveName(id), o && o.inat_name, o && o.common_name, o && o.place_guess, o && o.user && o.user.name, o && o.user && o.user.login, String(id)]
      .filter(Boolean).join(' ').toLowerCase();
    return hay.includes(needle);
  }

  function organizeItems() {
    const needle = $('#find').value.trim().toLowerCase();
    const minF = S.project.settings.min_faves || 0;
    const items = [];
    let hiddenFaves = 0;
    let hiddenSources = 0;
    let lastGroup = null;
    const posOf = new Map(S.project.order.map((id, i) => [id, i + 1]));
    for (const id of S.project.order) {
      const st = S.states.get(id);
      if (!st) continue;
      const o = S.obs.get(id);
      if (st.status === 'active' && o) {
        if (hiddenBySources(st)) { hiddenSources++; continue; }
        if ((o.faves_count || 0) < minF) { hiddenFaves++; continue; }
      }
      if (!findMatches(id, needle)) continue;
      const g = groupLabel(id);
      if (S.project.settings.grouping !== 'none' && g && g !== lastGroup) {
        items.push({ type: 'group', label: g });
        lastGroup = g;
      }
      items.push({ type: 'obs', id, pos: posOf.get(id) });
    }
    return { items, hiddenFaves, hiddenSources };
  }

  function renderOrganize(keepCount = false) {
    syncToolbar();
    const { items, hiddenFaves, hiddenSources } = organizeItems();
    const min = keepCount && listRenderer ? listRenderer.count : 0;
    if (listRenderer) listRenderer.disconnect();
    renderedCards = new Map();
    listRenderer = chunkRender($('#obs-list'), $('#obs-sentinel'), items, (item) => {
      if (item.type === 'group') {
        const g = S.project.settings.grouping;
        return el('div', { class: 'group-head' + (g === 'genus' && item.label !== 'Unclassified' ? ' italic' : '') },
          el('small', { text: GROUP_NAMES[g] || '' }), el('span', { class: 'name', text: item.label }));
      }
      const card = obsCard(item.id, item.pos);
      renderedCards.set(item.id, card);
      return card;
    }, min);
    updateStats(hiddenFaves, hiddenSources);
  }

  function updateStats(hiddenFaves, hiddenSources) {
    if (hiddenFaves === undefined) ({ hiddenFaves, hiddenSources } = organizeItems());
    let obsCount = 0;
    let slides = 0;
    let unavailable = 0;
    for (const st of S.project.observations) {
      if (st.status !== 'active') { unavailable++; continue; }
      if (!slideEligible(st)) continue;
      const c = selectedCount(st);
      if (c) { obsCount++; slides += c; }
    }
    const bits = [`${plural(S.project.observations.length, 'observation')}`, `${plural(slides, 'photo slide')} from ${plural(obsCount, 'observation')}`];
    if (hiddenFaves) bits.push(`${n(hiddenFaves)} hidden by the favorites filter`);
    if (hiddenSources) bits.push(`${n(hiddenSources)} hidden (source disabled)`);
    if (unavailable) bits.push(`${n(unavailable)} unavailable`);
    const line = $('#org-stats');
    line.replaceChildren(document.createTextNode(bits.join(' · ')));
    if (slides > LIMITS.imageSlides) line.append(el('span', { class: 'chip warn', text: ` Over the ${n(LIMITS.imageSlides)}-slide limit` }));
    if (S.pendingNew && S.pendingNew.new_ids && S.pendingNew.new_ids.length) {
      line.append(' ', el('button', { type: 'button', class: 'btn btn-small btn-gold', text: `Review ${plural(S.pendingNew.new_ids.length, 'new observation')}`, onclick: () => showReview(S.pendingNew, true) }));
    }
  }

  function refreshCard(id) {
    const old = renderedCards.get(id);
    if (old && old.isConnected) {
      const card = obsCard(id, Number(old.dataset.pos));
      old.replaceWith(card);
      renderedCards.set(id, card);
    }
    updateStats();
  }

  function setPhotoSelected(st, pid, on) {
    const has = st.selected_photo_ids.includes(pid);
    if (on && !has) {
      // keep iNaturalist photo order within the selection
      const o = S.obs.get(st.id);
      const order = o ? o.photos.map((p) => p.id) : [];
      st.selected_photo_ids.push(pid);
      st.selected_photo_ids.sort((a, b) => {
        const ia = order.indexOf(a); const ib = order.indexOf(b);
        return (ia < 0 ? 1e9 : ia) - (ib < 0 ? 1e9 : ib);
      });
    } else if (!on && has) {
      st.selected_photo_ids = st.selected_photo_ids.filter((p) => p !== pid);
    }
    markDirty();
  }

  const stateAdapter = (id) => ({
    isSelected: (pid) => S.states.get(id).selected_photo_ids.includes(pid),
    toggle: (pid, on) => { setPhotoSelected(S.states.get(id), pid, on); refreshCard(id); },
  });

  function moveObservation(id, targetId, after) {
    const order = S.project.order.filter((x) => x !== id);
    let idx = order.indexOf(targetId);
    if (idx < 0) return;
    if (after) idx += 1;
    order.splice(idx, 0, id);
    S.project.order = order;
    markCustomOrder();
  }
  function moveToIndex(id, index) {
    const order = S.project.order.filter((x) => x !== id);
    order.splice(Math.max(0, Math.min(order.length, index)), 0, id);
    S.project.order = order;
    markCustomOrder();
  }
  function markCustomOrder() {
    const sort = S.project.settings.sort;
    if (sort.key !== 'custom' && sort.key !== 'random') { sort.base_key = sort.key; sort.base_direction = sort.direction; }
    sort.key = 'custom';
    markDirty();
  }

  function obsCard(id, pos, adapter) {
    const st = S.states.get(id);
    const o = S.obs.get(id);
    const review = !!adapter;
    adapter = adapter || stateAdapter(id);

    if (!o || (st && st.status === 'unavailable')) {
      return el('div', { class: 'obs unavailable', dataset: { id: String(id), pos: String(pos || 0) } },
        el('div', { class: 'handle', title: 'Drag to reorder' }, '⠿', el('span', { class: 'pos', text: pos ? `#${pos}` : '' })),
        el('div'),
        el('div', { class: 'obs-info' },
          el('div', { class: 'sci', text: (st && st.overrides.scientific) || (st && st.last_inat_name) || `Observation ${id}` }),
          el('div', { class: 'chips' }, el('span', { class: 'chip warn', text: 'Unavailable on iNaturalist' })),
          el('div', { class: 'meta', text: 'Deleted, made private, or hidden. It is kept here as a placeholder and is skipped when generating.' }),
          el('div', { class: 'obs-actions' },
            el('a', { href: `https://www.inaturalist.org/observations/${id}`, target: '_blank', rel: 'noopener noreferrer', text: 'Check on iNaturalist ↗' }),
            el('button', {
              type: 'button', text: 'Remove from project',
              onclick: () => {
                S.project.observations = S.project.observations.filter((s) => s.id !== id);
                S.project.order = S.project.order.filter((x) => x !== id);
                reindex(); markDirty(); renderOrganize(true);
              },
            }),
          ),
        ),
        el('div'),
      );
    }

    const photos = o.photos;
    const selCount = photos.filter((p) => adapter.isSelected(p.id)).length;
    const missing = review ? [] : st.selected_photo_ids.filter((pid) => !photos.some((p) => p.id === pid));
    const card = el('div', { class: 'obs', dataset: { id: String(id), pos: String(pos || 0) } });

    const box = el('input', {
      type: 'checkbox', checked: selCount === photos.length && photos.length > 0,
      indeterminate: selCount > 0 && selCount < photos.length,
      title: 'Select or deselect all photos of this observation', 'aria-label': 'Select all photos of this observation',
      onchange: (e) => { photos.forEach((p) => adapter.toggle(p.id, e.target.checked)); if (review) refreshReviewCard(id); },
    });

    // name (editable)
    const nameRow = el('div', { class: 'sci' });
    const name = review ? (o.inat_name || '') : effectiveName(id);
    nameRow.append(el('span', { text: name || '(no taxon name)' }));
    const overridden = !review && st.overrides.scientific !== null && st.overrides.scientific !== undefined;
    if (!review) {
      nameRow.append(el('button', {
        type: 'button', class: 'edit', text: 'edit', title: 'Edit the name shown on the slides',
        onclick: () => {
          const input = el('input', { type: 'text', value: name, maxLength: 300, 'aria-label': 'Name on slides' });
          const save = () => {
            const v = input.value.trim();
            st.overrides.scientific = (v === '' || v === (o.inat_name || '')) ? null : v;
            markDirty();
            refreshCard(id);
          };
          input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); if (e.key === 'Escape') refreshCard(id); });
          input.addEventListener('blur', save);
          nameRow.replaceChildren(input);
          input.focus();
          input.select();
        },
      }));
    }
    const info = el('div', { class: 'obs-info' }, nameRow);
    if (overridden) {
      info.append(el('div', { class: 'overridden-note' }, `iNaturalist: ${o.inat_name || '(none)'} · `,
        el('button', { type: 'button', text: 'use iNaturalist name', onclick: () => { st.overrides.scientific = null; markDirty(); refreshCard(id); } })));
    } else if (!review && o.provisional_name && o.provisional_name !== o.inat_name) {
      info.append(el('div', { class: 'overridden-note' }, `Provisional name on iNaturalist: ${o.provisional_name} · `,
        el('button', { type: 'button', text: 'use it', onclick: () => { st.overrides.scientific = o.provisional_name; markDirty(); refreshCard(id); } })));
    }
    if (o.common_name) info.append(el('div', { class: 'common', text: o.common_name }));
    const user = o.user || {};
    info.append(el('div', { class: 'meta' },
      el('span', { text: formatDate(o.observed_on) || 'No date' }),
      o.place_guess ? el('span', { text: o.place_guess }) : null,
      el('span', { text: user.name || user.login || '' }),
      el('span', { class: 'faves', title: 'iNaturalist favorites (never shown in the PowerPoint)', text: `♥ ${n(o.faves_count)}` }),
    ));
    const chips = el('div', { class: 'chips' });
    if (S.project.sources.length > 1 && st) {
      st.source_ids.forEach((sid) => {
        const src = S.project.sources.find((s) => s.id === sid);
        if (src) chips.append(el('span', { class: 'chip', text: src.label || 'source' }));
      });
    }
    if (review && S.pendingNew && S.pendingNew.membership) {
      (S.pendingNew.membership[String(id)] || []).forEach((sid) => {
        const src = S.project.sources.find((s) => s.id === sid);
        if (src) chips.append(el('span', { class: 'chip', text: src.label || 'source' }));
      });
    }
    if (missing.length) chips.append(el('span', { class: 'chip warn', text: `${plural(missing.length, 'selected photo')} deleted on iNaturalist` }));
    if (chips.childNodes.length) info.append(chips);

    if (!review) {
      const actions = el('div', { class: 'obs-actions' },
        el('a', { href: o.uri, target: '_blank', rel: 'noopener noreferrer', text: 'iNaturalist ↗' }),
        el('button', { type: 'button', text: 'Slide text', onclick: () => toggleAnnEdit(card, id) }),
        el('button', { type: 'button', text: 'Move to…', onclick: () => {
          const v = prompt(`Move to position (1 to ${S.project.order.length}):`, String(pos || 1));
          const k = parseInt(v, 10);
          if (k >= 1) { moveToIndex(id, k - 1); renderOrganize(true); }
        } }),
        el('button', { type: 'button', text: 'Top', onclick: () => { moveToIndex(id, 0); renderOrganize(true); } }),
        el('button', { type: 'button', text: 'Bottom', onclick: () => { moveToIndex(id, 1e9); renderOrganize(true); } }),
      );
      info.append(actions);
    }

    const thumbs = el('div', { class: 'thumbs' });
    const tp = S.project.settings.title_photo;
    photos.forEach((p, idx) => {
      const sel = adapter.isSelected(p.id);
      const t = el('div', { class: 'thumb' + (sel ? ' selected' : '') + (tp && tp.photo_id === p.id ? ' title-pick' : '') },
        el('img', { src: photoSize(p.url, 'small'), loading: 'lazy', decoding: 'async', alt: `Photo ${idx + 1} of ${name}`, width: 112, height: 112 }),
        el('input', {
          type: 'checkbox', checked: sel, title: 'Include this photo', 'aria-label': `Include photo ${idx + 1}`,
          onclick: (e) => e.stopPropagation(),
          onchange: (e) => { adapter.toggle(p.id, e.target.checked); if (review) refreshReviewCard(id); },
        }),
      );
      t.addEventListener('click', () => Viewer.open(id, idx, adapter, review ? () => refreshReviewCard(id) : null));
      thumbs.append(t);
    });
    missing.forEach((pid) => {
      thumbs.append(el('div', { class: 'thumb missing', title: `Photo ${pid}` },
        el('span', {}, 'Photo deleted on iNaturalist', el('br'),
          el('button', { type: 'button', class: 'btn btn-small', text: 'Dismiss', onclick: () => { setPhotoSelected(st, pid, false); st.known_photo_ids = st.known_photo_ids.filter((x) => x !== pid); refreshCard(id); } }))));
    });

    if (review) {
      card.append(el('div', { class: 'obs-check' }, box), info, thumbs);
      return card;
    }
    const handle = el('div', { class: 'handle', title: 'Drag to reorder' }, '⠿', el('span', { class: 'pos', text: pos ? `#${pos}` : '' }));
    handle.addEventListener('mousedown', () => { card.draggable = true; });
    handle.addEventListener('touchstart', () => { card.draggable = true; }, { passive: true });
    card.addEventListener('dragend', () => { card.draggable = false; });
    card.append(handle, el('div', { class: 'obs-check' }, box), info, thumbs);
    return card;
  }

  function toggleAnnEdit(card, id) {
    const info = $('.obs-info', card);
    const existing = $('.ann-edit', info);
    if (existing) { existing.remove(); return; }
    const st = S.states.get(id);
    const o = S.obs.get(id);
    const user = o.user || {};
    const fields = [
      ['common_name', 'Common name', o.common_name || ''],
      ['date', 'Date', formatDate(o.observed_on)],
      ['location', 'Location', o.place_guess || ''],
      ['observer', 'Observer', (user.name || user.login) ? `Photo: ${user.name || user.login}` : ''],
    ];
    const grid = el('div', { class: 'ann-edit' });
    fields.forEach(([key, label, def]) => {
      const cur = st.overrides[key];
      grid.append(el('label', { text: label }), el('input', {
        type: 'text', value: cur === null || cur === undefined ? '' : cur, placeholder: def || '(empty)', maxLength: 300,
        onchange: (e) => { const v = e.target.value.trim(); st.overrides[key] = v === '' ? null : v; markDirty(); },
      }));
    });
    grid.append(el('span'), el('span', { class: 'muted small', text: 'Leave blank to use the iNaturalist value. Which lines appear is set in Presentation Settings.' }));
    info.append(grid);
  }

  // drag & drop within the list
  (() => {
    const list = $('#obs-list');
    let dragId = null;
    let marked = null;
    const clear = () => { if (marked) marked.classList.remove('drop-before', 'drop-after'); marked = null; };
    list.addEventListener('dragstart', (e) => {
      const card = e.target.closest('.obs');
      if (!card || !card.draggable) return;
      dragId = Number(card.dataset.id);
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(dragId));
    });
    list.addEventListener('dragover', (e) => {
      if (dragId === null) return;
      const card = e.target.closest('.obs');
      if (!card || Number(card.dataset.id) === dragId) { clear(); return; }
      e.preventDefault();
      const r = card.getBoundingClientRect();
      const after = e.clientY > r.top + r.height / 2;
      if (marked !== card) clear();
      marked = card;
      card.classList.toggle('drop-after', after);
      card.classList.toggle('drop-before', !after);
    });
    list.addEventListener('drop', (e) => {
      if (dragId === null || !marked) return;
      e.preventDefault();
      const target = Number(marked.dataset.id);
      const after = marked.classList.contains('drop-after');
      clear();
      moveObservation(dragId, target, after);
      dragId = null;
      renderOrganize(true);
    });
    list.addEventListener('dragend', () => {
      clear();
      $$('.obs.dragging', list).forEach((c) => c.classList.remove('dragging'));
      dragId = null;
    });
  })();

  // toolbar
  function syncToolbar() {
    const s = S.project.settings;
    $('#sort-key').value = s.sort.key;
    $('#sort-dir').textContent = s.sort.direction === 'desc' ? '↓ Desc' : '↑ Asc';
    $('#sort-dir').disabled = s.sort.key === 'random' || s.sort.key === 'custom';
    $('#grouping').value = s.grouping;
    $('#min-faves').value = String(s.min_faves || 0);
  }

  async function applySort(key, direction) {
    const sort = S.project.settings.sort;
    if (key === 'custom') { key = sort.base_key; direction = sort.base_direction; }
    try {
      const res = await api('/api/sort', {
        method: 'POST',
        body: { project: S.project, workspace_id: S.workspaceId, key, direction, seed: Math.floor(Math.random() * 1e9) },
      });
      S.project.order = res.order;
      sort.key = key;
      sort.direction = direction;
      if (key !== 'random') { sort.base_key = key; sort.base_direction = direction; }
      markDirty();
      renderOrganize();
    } catch (err) { handleApiError(err); }
  }

  $('#sort-key').addEventListener('change', (e) => {
    const key = e.target.value;
    const dir = ['favorites', 'observed_on', 'created_at'].includes(key) ? 'desc' : 'asc';
    applySort(key, dir);
  });
  $('#sort-dir').addEventListener('click', () => {
    const s = S.project.settings.sort;
    applySort(s.key, s.direction === 'desc' ? 'asc' : 'desc');
  });
  $('#btn-sort').addEventListener('click', () => {
    const s = S.project.settings.sort;
    if (s.key === 'custom' && !confirm('Re-sorting replaces your hand-arranged order. Continue?')) return;
    applySort(s.key, s.direction);
  });
  $('#grouping').addEventListener('change', (e) => {
    S.project.settings.grouping = e.target.value;
    markDirty();
    const s = S.project.settings.sort;
    if (s.key === 'custom') renderOrganize();
    else applySort(s.key, s.direction);
  });
  $('#min-faves').addEventListener('input', debounce((e) => {
    const v = Math.max(0, Math.min(1000000, parseInt(e.target.value, 10) || 0));
    S.project.settings.min_faves = v;
    markDirty();
    renderOrganize();
  }, 150));
  $('#find').addEventListener('input', debounce(() => renderOrganize(), 200));

  function bulkSelect(on) {
    const { items } = organizeItems();
    items.filter((it) => it.type === 'obs').forEach((it) => {
      const st = S.states.get(it.id);
      const o = S.obs.get(it.id);
      if (!st || !o || st.status !== 'active') return;
      st.selected_photo_ids = on ? o.photos.map((p) => p.id) : [];
    });
    markDirty();
    renderOrganize(true);
  }
  $('#btn-select-all').addEventListener('click', () => bulkSelect(true));
  $('#btn-select-none').addEventListener('click', () => {
    if (confirm('Deselect every photo of the observations shown?')) bulkSelect(false);
  });

  function handleApiError(err) {
    if (err.status === 410) {
      toast('Your session data expired on the server. Reloading from iNaturalist…', true);
      S.workspaceId = null;
      startLoad(null);
      return;
    }
    toast(err.message, true, 8000);
  }

  // ---------------------------------------------------------------- viewer
  const Viewer = (() => {
    const root = $('#viewer');
    const stage = $('#viewer-stage');
    const img = $('#viewer-img');
    let ctx = null; // {id, idx, adapter, after}
    let scale = 1; let tx = 0; let ty = 0; let fitScale = 1;
    let W = 1; let H = 1; let showing = 'large';

    function photo() { return S.obs.get(ctx.id).photos[ctx.idx]; }
    function apply() {
      img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
      const pct = Math.round(scale * 100);
      $('#v-zoom-label').textContent = Math.abs(scale - fitScale) < 0.001 ? 'Fit' : `${pct}%`;
      // Load the original only when the large image would be upscaled.
      const p = photo();
      if (showing === 'large' && scale * W > 1024 * 1.05 && (W > 1100 || H > 1100)) {
        showing = 'loading';
        const url = photoSize(p.url, 'original');
        const pre = new Image();
        pre.onload = () => { if (ctx && photo().id === p.id) { img.src = url; showing = 'original'; label(); } };
        pre.onerror = () => { showing = 'large'; };
        pre.src = url;
        label('Loading full resolution…');
      }
    }
    function label(text) {
      $('#viewer-res').textContent = text || (showing === 'original' ? `Full resolution · ${W} × ${H}` : `Preview (1024 px) · original ${W} × ${H}`);
    }
    function fit() {
      const r = stage.getBoundingClientRect();
      fitScale = Math.min(r.width / W, r.height / H, 1e3);
      scale = fitScale;
      tx = (r.width - W * scale) / 2;
      ty = (r.height - H * scale) / 2;
      apply();
    }
    function zoomAt(factor, cx, cy) {
      const r = stage.getBoundingClientRect();
      if (cx === undefined) { cx = r.width / 2; cy = r.height / 2; }
      const ns = Math.max(fitScale * 0.5, Math.min(8, scale * factor));
      tx = cx - ((cx - tx) * ns) / scale;
      ty = cy - ((cy - ty) * ns) / scale;
      scale = ns;
      apply();
    }
    function show() {
      const o = S.obs.get(ctx.id);
      const p = photo();
      W = p.width || 1024; H = p.height || 1024;
      showing = 'large';
      img.style.width = `${W}px`;
      img.style.height = `${H}px`;
      img.src = photoSize(p.url, 'large');
      img.onload = () => {
        if (!p.width) { // dimensions unknown: use the large image's aspect
          W = img.naturalWidth; H = img.naturalHeight;
          img.style.width = `${W}px`; img.style.height = `${H}px`;
          fit();
        }
      };
      img.alt = `Photo ${ctx.idx + 1} of ${o.inat_name || 'observation'}`;
      $('#viewer-name').textContent = (S.states.has(ctx.id) ? effectiveName(ctx.id) : o.inat_name) || 'Observation';
      $('#viewer-count').textContent = `photo ${ctx.idx + 1} of ${o.photos.length}${p.license ? ' · ' + p.license.toUpperCase() : ''}`;
      $('#v-selected').checked = ctx.adapter.isSelected(p.id);
      $('#v-prev').disabled = ctx.idx === 0;
      $('#v-next').disabled = ctx.idx >= o.photos.length - 1;
      const strip = $('#viewer-strip');
      strip.replaceChildren(...o.photos.map((q, i) => el('button', {
        type: 'button', class: (i === ctx.idx ? 'current' : '') + (ctx.adapter.isSelected(q.id) ? ' sel' : ''),
        onclick: () => { ctx.idx = i; show(); }, 'aria-label': `Photo ${i + 1}`,
      }, el('img', { src: q.url, alt: '' }))));
      label();
      fit();
    }
    function open(id, idx, adapter, after) {
      ctx = { id, idx, adapter, after };
      root.hidden = false;
      document.body.style.overflow = 'hidden';
      show();
    }
    function close() {
      if (!ctx) return;
      root.hidden = true;
      document.body.style.overflow = '';
      img.removeAttribute('src');
      const after = ctx.after;
      ctx = null;
      if (after) after();
    }
    function step(d) {
      const o = S.obs.get(ctx.id);
      const i = ctx.idx + d;
      if (i >= 0 && i < o.photos.length) { ctx.idx = i; show(); }
    }
    function toggle() {
      const p = photo();
      const on = !ctx.adapter.isSelected(p.id);
      ctx.adapter.toggle(p.id, on);
      $('#v-selected').checked = on;
      const btn = $('#viewer-strip').children[ctx.idx];
      if (btn) btn.classList.toggle('sel', on);
    }

    $('#v-close').addEventListener('click', close);
    $('#v-prev').addEventListener('click', () => step(-1));
    $('#v-next').addEventListener('click', () => step(1));
    $('#v-zoom-in').addEventListener('click', () => zoomAt(1.5));
    $('#v-zoom-out').addEventListener('click', () => zoomAt(1 / 1.5));
    $('#v-zoom-fit').addEventListener('click', fit);
    $('#v-zoom-100').addEventListener('click', () => zoomAt(1 / scale));
    $('#v-selected').addEventListener('change', (e) => { if (ctx.adapter.isSelected(photo().id) !== e.target.checked) toggle(); });
    stage.addEventListener('wheel', (e) => {
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      zoomAt(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    let pan = null;
    stage.addEventListener('pointerdown', (e) => {
      if (e.target.closest('button')) return;
      pan = { x: e.clientX, y: e.clientY, tx, ty };
      stage.setPointerCapture(e.pointerId);
      stage.classList.add('panning');
    });
    stage.addEventListener('pointermove', (e) => {
      if (!pan) return;
      tx = pan.tx + e.clientX - pan.x;
      ty = pan.ty + e.clientY - pan.y;
      apply();
    });
    const endPan = () => { pan = null; stage.classList.remove('panning'); };
    stage.addEventListener('pointerup', endPan);
    stage.addEventListener('pointercancel', endPan);
    stage.addEventListener('dblclick', (e) => {
      const r = stage.getBoundingClientRect();
      if (scale > fitScale * 1.01) fit(); else zoomAt(Math.max(2, 1 / scale), e.clientX - r.left, e.clientY - r.top);
    });
    window.addEventListener('resize', () => { if (ctx) fit(); });
    document.addEventListener('keydown', (e) => {
      if (!ctx) return;
      if (e.target.tagName === 'INPUT' && e.target.type === 'text') return;
      const k = e.key;
      if (k === 'Escape') close();
      else if (k === 'ArrowLeft') step(-1);
      else if (k === 'ArrowRight') step(1);
      else if (k === ' ') { e.preventDefault(); toggle(); }
      else if (k === '+' || k === '=') zoomAt(1.5);
      else if (k === '-') zoomAt(1 / 1.5);
      else if (k === '0') fit();
      else if (k === '1') zoomAt(1 / scale);
      else return;
      e.preventDefault();
    });
    return { open, close };
  })();

  // ---------------------------------------------------------------- 3. settings
  function renderSettings() {
    const s = S.project.settings;
    $('#set-title').value = s.title;
    $('#set-presenter').value = s.presenter;
    $$('input[name=bg]').forEach((r) => { r.checked = r.value === s.background; });
    $('#ann-scientific').checked = s.annotations.scientific;
    $('#ann-common').checked = s.annotations.common_name;
    $('#ann-date').checked = s.annotations.date;
    $('#ann-location').checked = s.annotations.location;
    $('#ann-observer').checked = observerEnabled();
    const auto = s.annotations.observer === null || s.annotations.observer === undefined;
    const note = $('#observer-note');
    note.replaceChildren();
    if (auto) {
      note.textContent = observerDefault()
        ? '(automatic: on, because a source has no username and may include other people\'s photos)'
        : '(automatic: off, because every source has your username, so these are your own photos)';
    } else {
      note.append('(set manually · ', el('button', { type: 'button', class: 'linklike', text: 'use automatic', onclick: () => { s.annotations.observer = null; markDirty(); renderSettings(); } }), ')');
    }
    $('#set-dividers').checked = s.dividers;
    $('#divider-note').textContent = s.grouping === 'none' ? '(choose “Group by” on the Select & Organize step first)' : `(grouped by ${GROUP_NAMES[s.grouping].toLowerCase()})`;
    $('#set-notes').checked = s.speaker_notes;
    $('#title-position').value = (s.title_photo && s.title_photo.position) || 'center';
    renderTitlePhoto();
  }

  function renderTitlePhoto() {
    const tp = S.project.settings.title_photo;
    const box = $('#title-photo-preview');
    box.replaceChildren();
    const o = tp && S.obs.get(tp.observation_id);
    const p = o && o.photos.find((q) => q.id === tp.photo_id);
    if (p) box.append(el('img', { src: photoSize(p.url, 'medium'), alt: 'Title background' }));
    else box.append(el('span', { class: 'muted small', text: tp ? 'Photo unavailable' : 'None' }));
    $('#btn-clear-title').disabled = !tp;
  }

  $('#set-title').addEventListener('input', (e) => { S.project.settings.title = e.target.value.slice(0, 200); markDirty(); });
  $('#set-presenter').addEventListener('input', (e) => { S.project.settings.presenter = e.target.value.slice(0, 200); markDirty(); });
  $$('input[name=bg]').forEach((r) => r.addEventListener('change', () => { S.project.settings.background = r.value; markDirty(); }));
  [['#ann-scientific', 'scientific'], ['#ann-common', 'common_name'], ['#ann-date', 'date'], ['#ann-location', 'location']].forEach(([sel, key]) => {
    $(sel).addEventListener('change', (e) => { S.project.settings.annotations[key] = e.target.checked; markDirty(); });
  });
  $('#ann-observer').addEventListener('change', (e) => { S.project.settings.annotations.observer = e.target.checked; markDirty(); renderSettings(); });
  $('#set-dividers').addEventListener('change', (e) => { S.project.settings.dividers = e.target.checked; markDirty(); });
  $('#set-notes').addEventListener('change', (e) => { S.project.settings.speaker_notes = e.target.checked; markDirty(); });
  $('#title-position').addEventListener('change', (e) => {
    if (S.project.settings.title_photo) { S.project.settings.title_photo.position = e.target.value; markDirty(); }
  });
  $('#btn-clear-title').addEventListener('click', () => { S.project.settings.title_photo = null; markDirty(); renderTitlePhoto(); });

  // title photo picker
  let pickerRenderer = null;
  function renderPicker() {
    const all = $('#picker-all').checked;
    const needle = $('#picker-find').value.trim().toLowerCase();
    const items = [];
    const tp = S.project.settings.title_photo;
    for (const id of S.project.order) {
      const st = S.states.get(id);
      const o = S.obs.get(id);
      if (!st || !o || st.status !== 'active') continue;
      if (needle && !findMatches(id, needle)) continue;
      for (const p of o.photos) {
        if (all || st.selected_photo_ids.includes(p.id)) items.push([id, p]);
      }
    }
    if (pickerRenderer) pickerRenderer.disconnect();
    pickerRenderer = chunkRender($('#picker-grid'), $('#picker-sentinel'), items, ([id, p]) => el('button', {
      type: 'button', class: tp && tp.photo_id === p.id ? 'current' : '', title: effectiveName(id),
      onclick: () => {
        S.project.settings.title_photo = { observation_id: id, photo_id: p.id, position: $('#title-position').value || 'center' };
        markDirty();
        $('#picker-modal').hidden = true;
        renderTitlePhoto();
      },
    }, el('img', { src: photoSize(p.url, 'small'), loading: 'lazy', alt: effectiveName(id) })), 0, 120);
    if (!items.length) $('#picker-grid').append(el('p', { class: 'muted', text: 'No photos match.' }));
  }
  $('#btn-pick-title').addEventListener('click', () => { $('#picker-modal').hidden = false; renderPicker(); });
  $('#picker-all').addEventListener('change', renderPicker);
  $('#picker-find').addEventListener('input', debounce(renderPicker, 200));

  // ---------------------------------------------------------------- 4. preview
  let previewRenderer = null;
  async function fetchPlan() {
    S.plan = await api('/api/plan', { method: 'POST', body: { project: S.project, workspace_id: S.workspaceId } });
    return S.plan;
  }

  function slideNode(spec, bg) {
    const slide = el('div', { class: `slide bg-${bg}` });
    if (spec.kind === 'title') {
      if (spec.photo) {
        slide.classList.add('has-photo');
        slide.append(el('img', { class: `cover focus-${spec.photo.position || 'center'}`, src: photoSize(spec.photo.url, 'medium'), loading: 'lazy', alt: '' }));
      }
      slide.append(el('div', { class: 'center' },
        el('div', { class: 't-title', text: spec.title }),
        el('div', { class: 't-rule' }),
        spec.presenter ? el('div', { class: 't-presenter', text: spec.presenter }) : null));
    } else if (spec.kind === 'divider') {
      slide.append(el('div', { class: 'center' },
        spec.rank_label ? el('div', { class: 'd-rank', text: spec.rank_label.toUpperCase() }) : null,
        el('div', { class: 'd-name' + (spec.italic ? ' italic' : ''), text: spec.text })));
    } else {
      slide.append(el('img', { class: 'photo', src: photoSize(spec.url, 'medium'), loading: 'lazy', decoding: 'async', alt: '' }));
      if (spec.lines.length) {
        slide.append(el('div', { class: 'ann' }, spec.lines.map((l) => el('div', { class: l.size >= 28 ? 'l28' : 'l18', text: l.text }))));
      }
    }
    return slide;
  }

  async function renderPreview(keepCount = false) {
    const grid = $('#preview-grid');
    const min = keepCount && previewRenderer ? previewRenderer.count : 0;
    if (!keepCount) { grid.replaceChildren(el('p', { class: 'muted', text: 'Building preview…' })); }
    let plan;
    try { plan = await fetchPlan(); } catch (err) { handleApiError(err); return; }
    const c = plan.counts;
    $('#preview-stats').textContent = `${plural(c.total, 'slide')}: 1 title, ${plural(c.dividers, 'divider')}, ${plural(c.images, 'photo slide')} from ${plural(c.observations, 'observation')}`;
    $('#preview-warnings').replaceChildren(...plan.warnings.map((w) => el('div', { class: 'warning', text: w })));
    if (previewRenderer) previewRenderer.disconnect();
    previewRenderer = chunkRender(grid, $('#preview-sentinel'), plan.slides, (spec, i) => {
      const wrap = el('div', { class: 'pslide' + (spec.kind === 'image' ? ' draggable' : ''), dataset: spec.kind === 'image' ? { obs: String(spec.observation_id) } : {} },
        el('span', { class: 'num', text: String(i + 1) }), slideNode(spec, plan.background));
      if (spec.kind === 'image') wrap.draggable = true;
      return wrap;
    }, min, 60);
  }

  (() => {
    const grid = $('#preview-grid');
    let dragObs = null;
    let marked = null;
    const clear = () => { if (marked) marked.classList.remove('drop-before', 'drop-after'); marked = null; };
    grid.addEventListener('dragstart', (e) => {
      const w = e.target.closest('.pslide.draggable');
      if (!w) return;
      dragObs = Number(w.dataset.obs);
      $$(`.pslide[data-obs="${dragObs}"]`, grid).forEach((x) => x.classList.add('dragging'));
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(dragObs));
    });
    grid.addEventListener('dragover', (e) => {
      if (dragObs === null) return;
      const w = e.target.closest('.pslide.draggable');
      if (!w || Number(w.dataset.obs) === dragObs) { clear(); return; }
      e.preventDefault();
      const r = w.getBoundingClientRect();
      const after = e.clientX > r.left + r.width / 2;
      if (marked !== w) clear();
      marked = w;
      w.classList.toggle('drop-after', after);
      w.classList.toggle('drop-before', !after);
    });
    grid.addEventListener('drop', (e) => {
      if (dragObs === null || !marked) return;
      e.preventDefault();
      const target = Number(marked.dataset.obs);
      const after = marked.classList.contains('drop-after');
      clear();
      moveObservation(dragObs, target, after);
      dragObs = null;
      renderPreview(true);
    });
    grid.addEventListener('dragend', () => {
      clear();
      $$('.pslide.dragging', grid).forEach((x) => x.classList.remove('dragging'));
      dragObs = null;
    });
  })();

  // ---------------------------------------------------------------- 5. generate
  let genJobId = null;
  async function renderGenerate() {
    $('#gen-summary').textContent = 'Counting slides…';
    try {
      const plan = await fetchPlan();
      const c = plan.counts;
      const mb = Math.round(c.images * 1.2);
      $('#gen-summary').textContent = `${plural(c.total, 'slide')}: ${plural(c.images, 'photo slide')} from ${plural(c.observations, 'observation')}${c.dividers ? `, ${plural(c.dividers, 'divider')}` : ''}, and a title slide. Expect a file of roughly ${n(mb)} MB.`;
      $('#btn-generate').disabled = c.images === 0 || c.images > LIMITS.imageSlides || !!genJobId;
      if (c.images === 0) $('#gen-summary').textContent = 'No photos are selected. Go back to Select & Organize and choose some photos.';
      if (c.images > LIMITS.imageSlides) $('#gen-summary').textContent += ` That is over the ${n(LIMITS.imageSlides)}-photo limit; deselect some photos.`;
    } catch (err) { handleApiError(err); }
  }

  $('#btn-generate').addEventListener('click', async () => {
    const btn = $('#btn-generate');
    btn.disabled = true;
    $('#gen-result').hidden = true;
    $('#gen-progress').hidden = false;
    $('#gen-message').textContent = 'Starting…';
    setBar($('#gen-bar'), 0, 0);
    try {
      const job = await api('/api/generate', { method: 'POST', body: { project: S.project, workspace_id: S.workspaceId } });
      genJobId = job.id;
      $('#btn-cancel-gen').hidden = false;
      const final = await pollJob(job.id, (j) => {
        $('#gen-message').textContent = j.message || (j.state === 'queued' ? 'Waiting for other presentations to finish…' : 'Working…');
        setBar($('#gen-bar'), j.done, j.total);
      });
      const out = $('#gen-result');
      out.hidden = false;
      if (final.state === 'done') {
        const mb = (final.result.bytes / 1048576).toFixed(1);
        const link = el('a', { class: 'btn btn-gold btn-lg', href: final.download_url, download: final.filename, text: `Download ${final.filename}` });
        out.replaceChildren(el('div', { class: 'result-ok' },
          el('p', { text: `Your presentation is ready: ${plural(final.result.slides, 'slide')}, ${mb} MB. The file is deleted from the server after two hours.` }),
          link,
          final.warnings.length ? el('ul', { class: 'small' }, final.warnings.map((w) => el('li', { text: w }))) : null));
        link.click();
      } else {
        out.replaceChildren(el('div', { class: 'result-err', text: final.error || 'Generation failed.' }));
      }
    } catch (err) {
      $('#gen-result').hidden = false;
      $('#gen-result').replaceChildren(el('div', { class: 'result-err', text: err.message }));
      if (err.status === 410) handleApiError(err);
    } finally {
      genJobId = null;
      $('#btn-cancel-gen').hidden = true;
      $('#gen-progress').hidden = true;
      btn.disabled = false;
    }
  });
  $('#btn-cancel-gen').addEventListener('click', async () => {
    if (genJobId) { try { await api(`/api/jobs/${encodeURIComponent(genJobId)}`, { method: 'DELETE' }); } catch (e) { /* ignore */ } }
  });

  // ---------------------------------------------------------------- review (refresh)
  let reviewSel = new Map(); // id -> Set(photo ids)
  let reviewRenderer = null;

  function reviewAdapter(id) {
    return {
      isSelected: (pid) => reviewSel.has(id) && reviewSel.get(id).has(pid),
      toggle: (pid, on) => {
        if (!reviewSel.has(id)) reviewSel.set(id, new Set());
        if (on) reviewSel.get(id).add(pid); else reviewSel.get(id).delete(pid);
      },
    };
  }
  function refreshReviewCard(id) {
    const old = $(`#review-list .obs[data-id="${id}"]`);
    if (old) old.replaceWith(obsCard(id, 0, reviewAdapter(id)));
    updateReviewButtons();
  }
  function updateReviewButtons() {
    const chosen = [...reviewSel.values()].filter((s) => s.size).length;
    $('#btn-review-add-selected').textContent = `Add selected (${n(chosen)})`;
    $('#btn-review-add-selected').disabled = chosen === 0;
  }

  function showReview(sum, newOnly = false) {
    const box = $('#review-summary');
    box.replaceChildren();
    const lines = [];
    if (!newOnly) {
      lines.push(`${n(sum.updated)} existing ${sum.updated === 1 ? 'observation' : 'observations'} updated`);
      lines.push(`${plural(sum.new || 0, 'new observation')} found`);
      if (sum.unavailable) lines.push(`${plural(sum.unavailable, 'observation')} no longer available`);
      if (sum.restored) lines.push(`${plural(sum.restored, 'observation')} available again`);
      if (sum.name_changed) lines.push(`${plural(sum.name_changed, 'observation')} now ${sum.name_changed === 1 ? 'has' : 'have'} a different iNaturalist name (your own name edits are kept)`);
      const missing = Object.values(sum.missing_photos || {}).reduce((a, b) => a + b.length, 0);
      if (missing) lines.push(`${plural(missing, 'selected photo')} deleted from iNaturalist. They are flagged in red; pick a replacement.`);
      const newPhotos = Object.values(sum.new_photos || {}).reduce((a, b) => a + b.length, 0);
      if (newPhotos) lines.push(`${plural(newPhotos, 'photo')} added on iNaturalist to existing observations (not selected)`);
      if (sum.no_longer_matching) lines.push(`${plural(sum.no_longer_matching, 'observation')} no longer match${sum.no_longer_matching === 1 ? 'es' : ''} any source but ${sum.no_longer_matching === 1 ? 'is' : 'are'} kept`);
    }
    box.append(...lines.map((t, i) => el('div', { class: i < 2 ? 'big' : '', text: t })));
    $('#review-title').textContent = newOnly ? 'Review new observations' : 'Project refreshed from iNaturalist';

    const ids = (sum.new_ids || []).filter((id) => S.obs.has(id) && !S.states.has(id));
    const hasNew = ids.length > 0;
    $('#review-new').hidden = !hasNew;
    ['#btn-review-ignore', '#btn-review-add-selected', '#btn-review-add-all'].forEach((s) => { $(s).hidden = !hasNew; });
    $('#btn-review-close').textContent = hasNew ? 'Decide later' : 'Continue';
    $('#btn-review-close').className = hasNew ? 'btn' : 'btn btn-primary';
    if (hasNew) {
      $('#review-new-title').textContent = `${plural(ids.length, 'unique new observation')} to review`;
      const chips = $('#review-by-source');
      chips.replaceChildren(...S.project.sources.filter((s) => (sum.new_by_source || {})[s.id]).map((s) => el('span', { class: 'chip', text: `${s.label}: ${n(sum.new_by_source[s.id])} new` })));
      reviewSel = new Map(ids.map((id) => [id, new Set(S.obs.get(id).photos.map((p) => p.id))]));
      $('#review-all').checked = true;
      if (reviewRenderer) reviewRenderer.disconnect();
      reviewRenderer = chunkRender($('#review-list'), $('#review-sentinel'), ids, (id) => obsCard(id, 0, reviewAdapter(id)), 0, 40);
      updateReviewButtons();
    }
    $('#review-modal').hidden = false;
    S.reviewIds = ids;
  }

  $('#review-all').addEventListener('change', (e) => {
    for (const id of S.reviewIds || []) {
      reviewSel.set(id, e.target.checked ? new Set(S.obs.get(id).photos.map((p) => p.id)) : new Set());
    }
    const count = reviewRenderer ? reviewRenderer.count : 0;
    reviewRenderer.disconnect();
    reviewRenderer = chunkRender($('#review-list'), $('#review-sentinel'), S.reviewIds, (id) => obsCard(id, 0, reviewAdapter(id)), count, 40);
    updateReviewButtons();
  });

  async function addReviewed(ids, ignoreIds) {
    const selected = {};
    ids.forEach((id) => { selected[String(id)] = [...(reviewSel.get(id) || [])]; });
    try {
      const res = await api('/api/observations/add', {
        method: 'POST',
        body: {
          project: S.project, workspace_id: S.workspaceId, observation_ids: ids,
          selected_photo_ids: selected, placement: $('#review-placement').value, ignore_ids: ignoreIds,
        },
      });
      setProject(res.project);
      markDirty();
      const remaining = (S.reviewIds || []).filter((id) => !S.states.has(id) && !S.project.ignored_observation_ids.includes(id));
      if (S.pendingNew) S.pendingNew.new_ids = remaining;
      if (!remaining.length) S.pendingNew = null;
      $('#review-modal').hidden = true;
      if (ids.length) toast(`Added ${plural(ids.length, 'observation')}.`);
      else if (ignoreIds.length) toast(`Ignored ${plural(ignoreIds.length, 'observation')}; they won't be offered again.`);
      goto(S.step === 'sources' ? 'organize' : S.step);
    } catch (err) { handleApiError(err); }
  }
  $('#btn-review-add-all').addEventListener('click', () => {
    for (const id of S.reviewIds) reviewSel.set(id, new Set(S.obs.get(id).photos.map((p) => p.id)));
    addReviewed(S.reviewIds.slice(), []);
  });
  $('#btn-review-add-selected').addEventListener('click', () => {
    addReviewed(S.reviewIds.filter((id) => reviewSel.get(id) && reviewSel.get(id).size), []);
  });
  $('#btn-review-ignore').addEventListener('click', () => {
    if (!confirm(`Ignore all ${n(S.reviewIds.length)} new observations? They will not be offered again for this project.`)) return;
    addReviewed([], S.reviewIds.slice());
  });

  // generic modal close
  $$('.modal').forEach((m) => {
    m.addEventListener('click', (e) => {
      if (e.target === m || e.target.closest('[data-close]')) {
        if (m.id === 'load-modal') return;
        m.hidden = true;
        if (m.id === 'review-modal') updateStats();
      }
    });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $$('.modal').forEach((m) => { if (m.id !== 'load-modal') m.hidden = true; });
  });

  // ---------------------------------------------------------------- save / open
  $('#btn-save-project').addEventListener('click', async () => {
    if (!S.project.sources.length) { toast('Add a source before saving a project.', true); return; }
    try {
      const data = await api('/api/project/export', { method: 'POST', body: { project: S.project } });
      const blob = new Blob([JSON.stringify(data, null, 1)], { type: 'application/json' });
      const base = (S.project.settings.title || 'presentation').replace(/[^A-Za-z0-9 _-]+/g, '').trim().replace(/\s+/g, '_').slice(0, 80) || 'presentation';
      const a = el('a', { href: URL.createObjectURL(blob), download: `${base}.dikarya-presentation.json` });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      S.dirty = false;
      toast('Project saved. Open it here later to refresh it from iNaturalist.');
    } catch (err) { toast(err.message, true); }
  });

  $('#btn-open-project').addEventListener('click', () => {
    if (S.dirty && S.project.observations.length && !confirm('Open another project? Unsaved changes to this one will be lost.')) return;
    $('#project-file').click();
  });
  $('#project-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) { toast('That file is too large to be a project file.', true); return; }
    try {
      const text = await file.text();
      const res = await api('/api/project/validate', { method: 'POST', raw: text });
      openProject(res.project);
    } catch (err) { toast(err.message, true, 9000); }
  });

  function openProject(project) {
    setProject(project);
    S.obs = new Map();
    S.workspaceId = null;
    S.loadedSources = new Set();
    S.sourceCounts = {};
    S.pendingNew = null;
    S.dirty = false;
    goto('sources');
    if (project.sources.some((s) => s.enabled)) startLoad(null);
  }

  // ---------------------------------------------------------------- startup
  $('#btn-theme').addEventListener('click', () => {
    const dark = document.documentElement.classList.toggle('dark');
    try { localStorage.setItem('dp-theme', dark ? 'dark' : 'light'); } catch (e) { /* ignore */ }
  });
  window.addEventListener('beforeunload', (e) => {
    if (S.dirty && S.project.observations.length) { e.preventDefault(); e.returnValue = ''; }
  });

  function offerRestore() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem('dp-project') || 'null'); } catch (e) { saved = null; }
    if (!saved || !Array.isArray(saved.sources) || !saved.sources.length) return;
    const count = (saved.observations || []).length;
    const card = el('div', { class: 'card subtle' },
      el('p', {}, `Resume the project you were working on in this browser${saved.settings && saved.settings.title ? ` (“${saved.settings.title}”)` : ''}: ${plural(saved.sources.length, 'source')}, ${plural(count, 'observation')}?`),
      el('div', { class: 'row-start' },
        el('button', { type: 'button', class: 'btn btn-primary', text: 'Resume', onclick: async () => {
          card.remove();
          try {
            const res = await api('/api/project/validate', { method: 'POST', body: saved });
            openProject(res.project);
          } catch (err) { toast(err.message, true); }
        } }),
        el('button', { type: 'button', class: 'btn', text: 'Start a new project', onclick: () => { card.remove(); try { localStorage.removeItem('dp-project'); } catch (e) { /* ignore */ } } })));
    $('#step-sources').prepend(card);
  }

  renderSources();
  updateStepper();
  offerRestore();
})();
