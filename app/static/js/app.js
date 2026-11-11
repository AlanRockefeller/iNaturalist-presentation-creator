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

  async function api(path, { method = 'GET', body, raw, headers } = {}) {
    const opts = { method, headers: { ...(headers || {}) } };
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

  // Mirror of related.estimate_requests() and pages_per_species(): the most
  // requests a search of n species can make. With "All rights reserved" and
  // only some other licenses ticked, licenses are checked on the server, which
  // can take up to 3 pages per species.
  // `licenses`: the ones ticked in the dialog now; else the saved ones.
  function estimateRequests(count, whose = 'others', licenses = null) {
    const lic = whose === 'others' ? (licenses || relatedSettings().others.licenses) : [];
    const pages = lic.includes('arr') && lic.length < LICENSES.length ? 3 : 1;
    let total = 0;
    for (let start = 0; start < count; start += 100) {
      const size = Math.min(100, count - start);
      total += size > 1 ? 1 + size * pages : pages;
    }
    return total;
  }
  // iNaturalist requests are paced at about one per second.
  const duration = (requests) => (requests < 60 ? 'under a minute' : `about ${plural(Math.ceil(requests / 60), 'minute')}`);

  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  // ---------------------------------------------------------------- state
  const LIMITS = {
    sources: Number(document.body.dataset.maxSources) || 20,
    imageSlides: Number(document.body.dataset.maxImageSlides) || 2000,
    relatedTaxa: Number(document.body.dataset.maxRelatedTaxa) || 500, // species per "more photos" search
    relatedKept: 1000, // species a "more photos" source remembers (models.MAX_RELATED_TAXA)
  };

  // Licenses for other people's photos, as iNaturalist names them; "arr" is
  // "all rights reserved". Mirrors related.LICENSE_LABELS and models.DEFAULT_LICENSES.
  const LICENSES = [
    ['cc0', 'CC0'], ['cc-by', 'CC BY'], ['cc-by-sa', 'CC BY-SA'], ['cc-by-nd', 'CC BY-ND'],
    ['cc-by-nc', 'CC BY-NC'], ['cc-by-nc-sa', 'CC BY-NC-SA'], ['cc-by-nc-nd', 'CC BY-NC-ND'], ['arr', 'All rights reserved'],
  ];
  const LICENSE_LABEL = new Map(LICENSES);
  const RELATED_LABEL = { mine: 'More of my photos', others: 'Other people\'s photos' };
  function defaultRelated() {
    return {
      username: '', buttons: null,
      mine: { per_taxon: 20, min_faves: 0 },
      others: { licenses: ['cc0', 'cc-by', 'cc-by-sa', 'cc-by-nd'], per_taxon: 5, min_faves: 1, research_only: true },
    };
  }

  function newProject() {
    return {
      format: 'dikarya-presentation',
      schema_version: 8,
      sources: [],
      settings: {
        title: '', presenter: '', background: 'black', title_photo: null,
        sort: { key: 'taxonomic', direction: 'asc', base_key: 'taxonomic', base_direction: 'asc' },
        grouping: 'family', dividers: false, min_faves: 0,
        annotations: { scientific: true, common_name: false, date: false, location: false, observer: null, rarity: true },
        speaker_notes: true,
        related: defaultRelated(),
        presenter_notes: true,
        about_slide: true,
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

  // Bumped whenever the user starts or opens another project. A request that
  // finishes after that belongs to the old project, so its result is dropped.
  let projectGen = 0;

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
  // The work in progress is kept in this browser so leaving the page loses
  // nothing: the project, the step, the server workspace (reused on return,
  // so iNaturalist is not searched again) and whether it was saved to a file.
  const DRAFT_KEY = 'dp-draft';
  let restoring = true; // until startup has restored (or found no) draft; never overwrite it before then
  // Observation fields at their defaults are left out of the draft (the server
  // fills them back in), as project_to_json() does for saved files, so a large
  // project still fits in the browser's storage.
  function leanState(st) {
    const out = {};
    for (const [k, v] of Object.entries(st)) {
      if (v === null || v === undefined || (k === 'status' && v === 'active')) continue;
      if (Array.isArray(v)) { if (v.length) out[k] = v; continue; }
      if (k === 'overrides') {
        const kept = Object.entries(v).filter(([, x]) => x !== null && x !== undefined);
        if (kept.length) out[k] = Object.fromEntries(kept);
        continue;
      }
      if (typeof v === 'object' && !Object.keys(v).length) continue;
      out[k] = v;
    }
    return out;
  }
  let draftWarned = false;
  function saveDraft() {
    if (restoring) return;
    try {
      if (!S.project.sources.length) { localStorage.removeItem(DRAFT_KEY); return; }
      const project = { ...S.project, observations: S.project.observations.map(leanState) };
      localStorage.setItem(DRAFT_KEY, JSON.stringify({
        project, step: S.step, workspaceId: S.workspaceId, dirty: S.dirty, savedAt: Date.now(),
      }));
      localStorage.removeItem('dp-project'); // the older draft format
    } catch (e) {
      // Storage is full or turned off. The draft already stored is older than
      // this project, so it must not be restored later as if it were current.
      try { localStorage.removeItem(DRAFT_KEY); } catch (err) { /* ignore */ }
      if (!draftWarned && S.project.observations.length) {
        draftWarned = true;
        toast('This browser cannot keep this project automatically (it may be too large). Use Save project to keep your work.', true, 9000);
      }
    }
  }
  const autosave = debounce(saveDraft, 800);

  // Mirrors of small server rules, used only for immediate display.
  function effectiveName(id) {
    const st = S.states.get(id);
    const o = S.obs.get(id);
    if (st && st.overrides && st.overrides.scientific !== null && st.overrides.scientific !== undefined) return st.overrides.scientific;
    return (o && o.inat_name) || '';
  }
  const isRelated = (src) => src.type === 'related' && !!src.related;
  const isRare = (src) => src.type === 'rare' && !!src.rare;
  // "12 observations worldwide" (rank added on the slides). Mirror of rarity_text() in presentation.py.
  function rarityText(info, rank) {
    if (!info) return '';
    const text = `${n(info.world)} observation${info.world === 1 ? '' : 's'} worldwide`;
    return rank ? `${text} · #${rank} rarest` : text;
  }
  const relatedSource = (whose) => S.project.sources.find((s) => isRelated(s) && s.related.whose === whose);
  function relatedSettings() {
    const s = S.project.settings;
    if (!s.related) s.related = defaultRelated();
    return s.related;
  }
  function observerDefault() {
    const active = S.project.sources.filter((s) => s.enabled);
    return (active.length ? active : S.project.sources).some((s) => s.type === 'url'
      || (isRelated(s) && s.related.whose === 'others') || (isRare(s) && !s.username));
  }
  function observerEnabled() {
    const v = S.project.settings.annotations.observer;
    return v === null || v === undefined ? observerDefault() : v;
  }
  // Who the presenter is: their Mine sources and the username in More photos.
  // Their photos are never credited. Mirror of own_logins() / is_own() in project.py.
  function ownLogins() {
    const out = new Set(S.project.sources.filter((s) => (s.type === 'username' || isRare(s)) && s.username).map((s) => s.username.toLowerCase()));
    const u = relatedSettings().username;
    if (u) out.add(u.toLowerCase());
    return out;
  }
  function isOwn(id) {
    const o = S.obs.get(id);
    const login = ((o && o.user && o.user.login) || '').toLowerCase();
    return !!login && ownLogins().has(login);
  }
  // The licenses an observation's photos are limited to (other people's photos
  // found only by "More from others"), or null for any. Mirror of
  // allowed_licenses() / default_photo_ids() in project.py.
  function allowedLicenses(sourceIds) {
    const limits = new Map();
    S.project.sources.forEach((s) => {
      if (isRelated(s) && s.related.whose === 'others') limits.set(s.id, relatedSettings().others.licenses);
      else if (isRare(s) && s.rare.commercial_only) limits.set(s.id, COMMERCIAL_LICENSES);
    });
    if (!sourceIds || !sourceIds.length || !sourceIds.every((sid) => limits.has(sid))) return null;
    return new Set(sourceIds.flatMap((sid) => limits.get(sid)));
  }
  const COMMERCIAL_LICENSES = ['cc0', 'cc-by', 'cc-by-sa', 'cc-by-nd']; // models.DEFAULT_LICENSES
  const photoAllowed = (p, allowed) => !allowed || allowed.has(p.license || 'arr');
  function defaultPhotoIds(id, sourceIds) {
    const o = S.obs.get(id);
    const allowed = allowedLicenses(sourceIds);
    const p = o && o.photos.find((x) => photoAllowed(x, allowed));
    return p ? [p.id] : [];
  }
  // Sources that found an observation waiting in the review dialog.
  const reviewSources = (id) => (S.pendingNew && S.pendingNew.membership && S.pendingNew.membership[String(id)]) || [];
  // Mirror of related.is_species(): only these can be searched for more photos.
  function isSpecies(o) {
    const t = o && o.taxon;
    return !!(t && t.id && typeof t.rank_level === 'number' && t.rank_level <= 10);
  }
  function enabledSourceIds() {
    return new Set(S.project.sources.filter((s) => s.enabled).map((s) => s.id));
  }
  function hiddenBySources(st, en = enabledSourceIds()) {
    if (!st.source_ids || !st.source_ids.length) return false;
    return !st.source_ids.some((sid) => en.has(sid));
  }
  // Mirror of single_user_login() in inaturalist.py: the login when a source's
  // search is limited to exactly one user, else null. Only such a source can be
  // switched between Mine and Search.
  const LOGIN_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/;
  function singleUserLogin(src) {
    let params;
    try { params = new URL(src.url).searchParams; } catch (e) { return null; }
    const logins = new Map();
    for (const key of ['user_id', 'user_login']) {
      for (const part of (params.get(key) || '').split(',')) {
        const v = part.trim();
        if (v) logins.set(v.toLowerCase(), v);
      }
    }
    if (logins.size !== 1) return null;
    const login = [...logins.values()][0];
    return LOGIN_RE.test(login) && !/^\d+$/.test(login) ? login : null;
  }
  const GROUP_RANKS = ['kingdom', 'phylum', 'class', 'order', 'family', 'genus'];
  // The group an observation is listed under, like group_of() in sorting.py:
  // the label is shown, the key tells apart groups that share a label (two
  // sources with the same label, or one genus name under two families).
  function groupOf(id, enabled = null) {
    const g = S.project.settings.grouping;
    const o = S.obs.get(id);
    if (g === 'none' || !o) return { key: '', label: '' };
    if (g === 'source') {
      const st = S.states.get(id);
      const en = enabled || enabledSourceIds();
      const ids = (st && st.source_ids) || [];
      const src = S.project.sources.find((s) => ids.includes(s.id) && en.has(s.id)) || S.project.sources.find((s) => ids.includes(s.id));
      return src ? { key: `source:${src.id}`, label: src.label || src.url } : { key: 'other', label: 'Other observations' };
    }
    const ranks = o.ranks || {};
    if (!ranks[g]) return { key: 'unclassified', label: 'Unclassified' };
    const path = GROUP_RANKS.slice(0, GROUP_RANKS.indexOf(g) + 1).map((r) => ranks[r] || '');
    return { key: JSON.stringify(path), label: ranks[g] };
  }
  const GROUP_NAMES = { kingdom: 'Kingdom', phylum: 'Phylum', class: 'Class', order: 'Order', family: 'Family', genus: 'Genus', source: 'Source' };

  function selectedCount(st) {
    const o = S.obs.get(st.id);
    if (!o) return 0;
    const ids = new Set(o.photos.map((p) => p.id));
    return st.selected_photo_ids.filter((p) => ids.has(p)).length;
  }
  function slideEligible(st, en = enabledSourceIds()) {
    const o = S.obs.get(st.id);
    if (!o || st.status !== 'active') return false;
    if ((o.faves_count || 0) < (S.project.settings.min_faves || 0)) return false;
    return !hiddenBySources(st, en);
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
    autosave();
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
    const gen = projectGen;
    try {
      const parsed = await api('/api/sources/parse', { method: 'POST', body: { url, username } });
      if (gen !== projectGen) return; // the project was replaced while this was checked
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
      if (parsed.type === 'username' && !username) {
        toast(`This search is limited to ${parsed.username}, so it counts as your own photos and photo credits start off. If they are someone else's, click Mine on the source.`, false, 9000);
      }
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

  // Mine (the presenter's own photos: credits start off) or Search (may
  // include other people's: credits start on). Only a search limited to one
  // user can be switched; Mine then means that user is the presenter.
  function setSourceOwner(src, login) {
    src.type = login ? 'username' : 'url';
    src.username = login || null;
    markDirty();
    renderSources();
  }

  function renderSources() {
    const list = $('#source-list');
    list.replaceChildren();
    $('#sources-empty').hidden = S.project.sources.length > 0;
    S.project.sources.forEach((src) => {
      if (isRelated(src)) { list.append(relatedSourceItem(src)); return; }
      if (isRare(src)) { list.append(rareSourceItem(src)); return; }
      const count = S.sourceCounts[src.id];
      const mine = src.type === 'username';
      const login = singleUserLogin(src);
      const whose = mine
        ? `Your own photos (${src.username}): photo credits start off.`
        : 'May include other people\'s photos: photo credits start on.';
      const badge = login
        ? el('button', {
          type: 'button', class: `badge ${src.type} badge-toggle`, text: mine ? 'Mine' : 'Search',
          'aria-pressed': mine ? 'true' : 'false', 'aria-label': `My own photos (${login})`,
          title: mine ? `Click if these are not your photos, but ${login}'s.` : `Click if you are ${login} and these are your own photos.`,
          onclick: () => setSourceOwner(src, mine ? null : login),
        })
        : el('span', { class: 'badge ' + src.type, text: mine ? 'Mine' : 'Search', title: whose });
      const item = el('li', { class: 'source-item' + (src.enabled ? '' : ' disabled') },
        el('input', {
          type: 'checkbox', checked: src.enabled, title: 'Include this source', 'aria-label': 'Include this source',
          onchange: (e) => { src.enabled = e.target.checked; markDirty(); renderSources(); },
        }),
        badge,
        el('div', { class: 'source-meta' },
          el('input', {
            type: 'text', class: 'label-input', value: src.label, maxLength: 120, 'aria-label': 'Source label',
            onchange: (e) => { src.label = e.target.value.trim().slice(0, 120); markDirty(); },
          }),
          el('a', { class: 'url', href: src.url, target: '_blank', rel: 'noopener noreferrer', text: src.url }),
          el('span', { class: 'who', text: whose + (login ? ` Click ${mine ? 'Mine' : 'Search'} to change.` : '') }),
        ),
        el('span', { class: 'count-pill', text: count === undefined ? '' : plural(count, 'observation') }),
        removeSourceButton(src),
      );
      list.append(item);
    });
    const enabled = S.project.sources.filter((s) => s.enabled);
    // "More photos" sources are searched only when asked (Search again), so
    // they never count as sources waiting to be loaded.
    const unloaded = enabled.filter((s) => !isRelated(s) && !S.loadedSources.has(s.id));
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
    const unloaded = enabled.filter((s) => !isRelated(s) && !S.loadedSources.has(s.id)).map((s) => s.id);
    return startLoad(unloaded.length ? unloaded : null);
  });

  function removeSourceButton(src) {
    return el('button', {
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
    });
  }

  // A "More of my photos" / "Other people's photos" source, made by the More
  // photos buttons on Select & Organize. It lists the species searched so far.
  function relatedSourceItem(src) {
    const species = src.related.taxon_ids.length;
    const kept = S.project.observations.filter((st) => st.source_ids.includes(src.id)).length;
    const what = src.related.whose === 'mine' ? 'your own photos' : 'other people\'s photos';
    return el('li', { class: 'source-item related' + (src.enabled ? '' : ' disabled') },
      el('input', {
        type: 'checkbox', checked: src.enabled, title: 'Include this source', 'aria-label': 'Include this source',
        onchange: (e) => { src.enabled = e.target.checked; markDirty(); renderSources(); },
      }),
      el('span', { class: 'badge related', text: 'More photos', title: `More ${what} of species in this project` }),
      el('div', { class: 'source-meta' },
        el('input', {
          type: 'text', class: 'label-input', value: src.label || RELATED_LABEL[src.related.whose], maxLength: 120, 'aria-label': 'Source label',
          onchange: (e) => { src.label = e.target.value.trim().slice(0, 120); markDirty(); },
        }),
        src.url ? el('a', { class: 'url', href: src.url, target: '_blank', rel: 'noopener noreferrer', text: src.url }) : null,
        el('span', { class: 'who', text: `${plural(species, 'species', 'species')} searched for ${what}, using the settings in More photos (Select & Organize). Searched only when you ask, so refreshing does not repeat it.` }),
      ),
      el('span', { class: 'count-pill', text: plural(kept, 'observation') }),
      el('button', {
        type: 'button', class: 'btn btn-small', text: 'Search again', disabled: !species || !src.enabled,
        title: 'Search all of these species again with the current More photos settings',
        onclick: () => {
          const need = estimateRequests(Math.min(species, LIMITS.relatedTaxa), src.related.whose);
          if (species > LIMITS.relatedTaxa) { toast(`One search can cover at most ${n(LIMITS.relatedTaxa)} species; this source has ${n(species)}.`, true, 9000); return; }
          if (!confirm(`Search ${plural(species, 'species', 'species')} again for ${what}? That is up to ${plural(need, 'request')} to iNaturalist (${duration(need)}). New observations are offered for review.`)) return;
          startLoad([src.id], null, { whose: src.related.whose, taxa: null, title: src.label || RELATED_LABEL[src.related.whose] });
        },
      }),
      removeSourceButton(src),
    );
  }

  // A Rare finds source: the rarest species in its search, by worldwide count
  // (rare.py). Searched when first loaded and again only with Search again.
  function rareSourceItem(src) {
    const kept = S.project.observations.filter((st) => st.source_ids.includes(src.id)).length;
    const r = src.rare;
    const how = [`the ${plural(r.top, 'rarest species', 'rarest species')}`, `${plural(r.per_taxon, 'observation')} each`];
    if (r.described_only) how.push('described species only');
    if (r.commercial_only) how.push('commercial-use photos only');
    const searched = S.loadedSources.has(src.id) || kept > 0;
    return el('li', { class: 'source-item rare' + (src.enabled ? '' : ' disabled') },
      el('input', {
        type: 'checkbox', checked: src.enabled, title: 'Include this source', 'aria-label': 'Include this source',
        onchange: (e) => { src.enabled = e.target.checked; markDirty(); renderSources(); },
      }),
      el('span', { class: 'badge rare', text: 'Rare finds', title: 'The rarest species in this search, by observations worldwide' }),
      el('div', { class: 'source-meta' },
        el('input', {
          type: 'text', class: 'label-input', value: src.label, maxLength: 120, 'aria-label': 'Source label',
          onchange: (e) => { src.label = e.target.value.trim().slice(0, 120); markDirty(); },
        }),
        el('a', { class: 'url', href: src.url, target: '_blank', rel: 'noopener noreferrer', text: src.url }),
        el('span', { class: 'who', text: `${how.join(', ')}. Searched only when you ask, so refreshing does not repeat it; the counts are updated.` }),
      ),
      el('span', { class: 'count-pill', text: plural(kept, 'observation') }),
      searched ? el('button', {
        type: 'button', class: 'btn btn-small', text: 'Search again', disabled: !src.enabled,
        title: 'Rank this search again and offer anything new for review',
        onclick: () => {
          if (!confirm(`Find the ${plural(r.top, 'rarest species', 'rarest species')} in this search again? That is about 30 to 60 requests to iNaturalist. New observations are offered for review.`)) return;
          startLoad([src.id], null, { rare: true, title: src.label || 'Rare finds' });
        },
      }) : null,
      removeSourceButton(src),
    );
  }

  $('#btn-add-rare').addEventListener('click', () => {
    const url = $('#source-url').value.trim();
    $('#source-error').textContent = '';
    if (!url) { $('#source-error').textContent = 'Paste the link to an iNaturalist observations search first.'; return; }
    if (S.project.sources.length >= LIMITS.sources) { $('#source-error').textContent = `A project can have at most ${LIMITS.sources} sources.`; return; }
    $('#rare-search').textContent = url;
    $('#rare-error').textContent = '';
    openModal($('#rare-modal'));
    $('#rare-top').focus();
  });

  $('#rare-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = $('#source-url').value.trim();
    const username = $('#source-username').value.trim().replace(/^@/, '');
    const top = Math.min(100, Math.max(1, parseInt($('#rare-top').value, 10) || 15));
    const per = Math.min(20, Math.max(1, parseInt($('#rare-per').value, 10) || 1));
    const btn = $('#btn-rare-add');
    btn.disabled = true;
    const gen = projectGen;
    try {
      const parsed = await api('/api/sources/parse', { method: 'POST', body: { url, username } });
      if (gen !== projectGen) return;
      if (S.project.sources.some((s) => isRare(s) && s.url === parsed.url)) {
        $('#rare-error').textContent = 'Rare finds for that search is already in the project.';
        return;
      }
      S.project.sources.push({
        id: newSourceId(), type: 'rare', input: parsed.input, username: parsed.username || null,
        url: parsed.url, label: `Rare finds: ${parsed.label}`, enabled: true,
        rare: { top, per_taxon: per, described_only: $('#rare-described').checked, commercial_only: $('#rare-commercial').checked },
      });
      $('#source-url').value = '';
      closeModal($('#rare-modal'));
      markDirty();
      renderSources();
      toast(hasObservations() ? 'Added. Press Load to find the rarest species; they are offered for review.' : 'Added. Press Load observations to find the rarest species.', false, 7000);
    } catch (err) {
      $('#rare-error').textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });

  // ---------------------------------------------------------------- loading
  let loadJobId = null;
  let loadCancelled = false;

  // Poll a job until it ends. A dropped connection or a server restart in
  // progress (502/503/504) is retried for a few minutes, because the job keeps
  // running on the server; only "unknown job" (404) and other errors end it.
  async function pollJob(id, onProgress) {
    let last = { state: 'running', done: 0, total: 0, message: '' };
    let failingSince = null;
    for (;;) {
      await new Promise((r) => setTimeout(r, failingSince ? 3000 : 900));
      let job;
      try {
        job = await api(`/api/jobs/${encodeURIComponent(id)}`);
      } catch (err) {
        const temporary = !err.status || [502, 503, 504].includes(err.status);
        if (!temporary) throw err;
        failingSince = failingSince || Date.now();
        if (Date.now() - failingSince > 5 * 60 * 1000) throw err;
        onProgress({ ...last, message: 'Lost contact with the server. Retrying…' });
        continue;
      }
      failingSince = null;
      last = job;
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

  // Dialogs: focus moves into one when it opens and back where it was when it
  // closes. While one is open the page behind it is inert, so Tab stays in the
  // dialog. Closing the review dialog refreshes its "Review new" button.
  const modalReturn = new Map();
  const modalOnClose = new Map(); // dialog -> function called once when it closes
  const PAGE_PARTS = '.topnav, .hero, .stepper-bar, main, .footer';
  function syncInert() {
    const open = $$('.modal').some((m) => !m.hidden);
    $$(PAGE_PARTS).forEach((part) => { part.inert = open; });
  }
  // `fallback()` gives the element to focus on closing when the one focused at
  // opening is gone (a card the list re-rendered meanwhile, say).
  const modalFallback = new Map();
  function openModal(m, fallback = null) {
    if (!m.hidden) return;
    modalReturn.set(m, document.activeElement);
    if (fallback) modalFallback.set(m, fallback); else modalFallback.delete(m);
    m.hidden = false;
    syncInert();
    const box = $('.modal-box', m);
    box.setAttribute('tabindex', '-1');
    box.focus();
  }
  function closeModal(m) {
    if (m.hidden) return;
    m.hidden = true;
    syncInert();
    if (m.id === 'review-modal') updateStats();
    const onClose = modalOnClose.get(m);
    modalOnClose.delete(m);
    if (onClose) onClose();
    let back = modalReturn.get(m);
    const fallback = modalFallback.get(m);
    modalReturn.delete(m);
    modalFallback.delete(m);
    if (!(back && back.isConnected && back !== document.body) && fallback) back = fallback();
    if (back && back.isConnected && typeof back.focus === 'function') back.focus();
  }

  // `related`: a "more photos" search ({whose, taxa (null = all of the
  // source's species), title, afterId}); its results open in a short review.
  async function startLoad(refreshSourceIds, resume = null, related = null) {
    const gen = projectGen;
    const modal = $('#load-modal');
    $('#load-message').textContent = 'Starting…';
    setBar($('#load-bar'), 0, 0);
    openModal(modal);
    loadCancelled = false;
    try {
      // A full refresh (no ids) searches every source again; the workspace is
      // still sent so "more photos" results waiting for review are kept.
      const body = {
        project: S.project,
        workspace_id: S.workspaceId,
        refresh_source_ids: refreshSourceIds,
      };
      if (related && related.taxa) body.related_taxa = related.taxa;
      const job = await api('/api/load', { method: 'POST', body });
      loadJobId = job.id;
      const final = await pollJob(job.id, (j) => {
        $('#load-message').textContent = j.message || 'Working…';
        setBar($('#load-bar'), j.done, j.total);
      });
      if (final.state === 'cancelled' || loadCancelled) { toast('Loading cancelled.'); return; }
      if (final.state !== 'done') throw new Error(final.error || 'Loading failed.');
      if (gen !== projectGen) return; // another project was opened meanwhile
      // Close this first, so a review dialog that opens next returns focus to
      // the page, not to this hidden dialog.
      closeModal(modal);
      applyLoadResult(final.result, resume, related);
    } catch (err) {
      if (gen === projectGen) toast(err.message, true, 9000);
    } finally {
      closeModal(modal);
      loadJobId = null;
    }
  }

  $('#btn-cancel-load').addEventListener('click', async () => {
    loadCancelled = true;
    if (loadJobId) { try { await api(`/api/jobs/${encodeURIComponent(loadJobId)}`, { method: 'DELETE' }); } catch (e) { /* ignore */ } }
  });

  function applyLoadResult(result, resume = null, related = null) {
    setProject(result.project);
    S.obs = new Map(result.observations.map((o) => [o.id, o]));
    S.workspaceId = result.workspace_id;
    S.loadedSources = new Set(Object.keys(result.source_results));
    S.sourceCounts = result.source_results;
    markDirty();
    const sum = result.summary;
    if (resume) {
      // Back where the user left off. New observations wait behind the Review button.
      S.dirty = resume.dirty !== false;
      S.pendingNew = sum.new ? sum : null;
      goto(STEPS.includes(resume.step) && resume.step !== 'sources' ? resume.step : 'organize');
      toast(`Restored your work${sum.new ? `; ${plural(sum.new, 'new observation')} to review` : ''}. Use New project in the top bar to start over.`, false, 7000);
    } else if (result.first_load) {
      S.pendingNew = null;
      toast(`Loaded ${plural(sum.auto_added || 0, 'observation')} from iNaturalist.`);
      goto('organize');
    } else if (related) {
      // Only what this search found; other new observations stay behind the
      // "Review new observations" button.
      S.pendingNew = sum.new ? sum : null;
      const found = new Set(sum.related_found || []);
      const ids = (sum.new_ids || []).filter((id) => found.has(id));
      goto(S.step === 'sources' ? 'organize' : S.step);
      if (ids.length) {
        showReview({ ...sum, new_ids: ids }, true, { title: related.title, afterId: related.afterId });
      } else {
        if (related.rare) {
          toast(`No new rare finds${found.size ? ': what the search found is already in the project or was ignored' : ''}. The counts are up to date.`, false, 7000);
        } else {
          const what = related.whose === 'mine' ? 'of your photos' : 'from other people';
          toast(`Nothing new ${what} with the current More photos settings${found.size ? ' (what it found is already in the project or was ignored)' : ''}.`, false, 7000);
        }
      }
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
    const en = enabledSourceIds();
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
        if (hiddenBySources(st, en)) { hiddenSources++; continue; }
        if ((o.faves_count || 0) < minF) { hiddenFaves++; continue; }
      }
      if (!findMatches(id, needle)) continue;
      const g = groupOf(id, en);
      if (S.project.settings.grouping !== 'none' && g.label && g.key !== lastGroup) {
        items.push({ type: 'group', key: g.key, label: g.label });
        lastGroup = g.key;
      }
      items.push({ type: 'obs', id, pos: posOf.get(id) });
    }
    return { items, hiddenFaves, hiddenSources };
  }

  // Removes the observations listed under a group heading (those hidden by the
  // filters stay), like "Remove from project" on each card.
  function removeGroup(key, label) {
    const en = enabledSourceIds();
    const ids = new Set(organizeItems().items.filter((it) => it.type === 'obs' && groupOf(it.id, en).key === key).map((it) => it.id));
    if (!ids.size) return;
    if (!confirm(`Remove ${plural(ids.size, 'observation')} in ${label} from the project?`)) return;
    const p = S.project;
    p.observations = p.observations.filter((s) => !ids.has(s.id));
    p.order = p.order.filter((x) => !ids.has(x));
    if (p.settings.title_photo && ids.has(p.settings.title_photo.observation_id)) p.settings.title_photo = null;
    reindex(); markDirty(); renderOrganize(true);
    toast(`Removed ${plural(ids.size, 'observation')} in ${label}.`);
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
        const rank = (GROUP_NAMES[g] || 'group').toLowerCase();
        return el('div', { class: 'group-head' + (g === 'genus' && item.label !== 'Unclassified' ? ' italic' : '') },
          el('small', { text: GROUP_NAMES[g] || '' }), el('span', { class: 'name', text: item.label }),
          el('button', {
            type: 'button', class: 'group-remove', text: '✕',
            title: `Remove every observation in this ${rank} from the project`, 'aria-label': `Remove ${rank} ${item.label}`,
            onclick: () => removeGroup(item.key, item.label),
          }));
      }
      const card = kbDecorate(obsCard(item.id, item.pos));
      renderedCards.set(item.id, card);
      return card;
    }, min);
    lastHidden = { hiddenFaves, hiddenSources };
    updateStats(hiddenFaves, hiddenSources);
    return items.filter((it) => it.type === 'obs').length;
  }

  // Screen readers hear what a sort, grouping or Find change did, rather than
  // the whole list being read out again (#org-announce is a status region).
  function announce(text) {
    const box = $('#org-announce');
    box.textContent = '';
    setTimeout(() => { box.textContent = text; }, 50); // a change is what gets read
  }
  const shownText = (count) => `${plural(count, 'observation')} shown.`;
  function groupingText() {
    const g = S.project.settings.grouping;
    return g === 'none' ? 'Not grouped.' : `Grouped by ${GROUP_NAMES[g].toLowerCase()}.`;
  }

  // The filter counts change only when the list is rebuilt, so a photo
  // checkbox (which refreshes one card) reuses them instead of rescanning.
  let lastHidden = { hiddenFaves: 0, hiddenSources: 0 };
  function updateStats(hiddenFaves = lastHidden.hiddenFaves, hiddenSources = lastHidden.hiddenSources) {
    const en = enabledSourceIds();
    let obsCount = 0;
    let slides = 0;
    let unavailable = 0;
    for (const st of S.project.observations) {
      if (st.status !== 'active') { unavailable++; continue; }
      if (!slideEligible(st, en)) continue;
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

  // ------------------------------------------------ keyboard on Select & Organize
  // Up/Down: observation. Left/Right: photo. Shift+Left/Right: move that photo.
  // Space: include or leave out that photo. Enter: open it in the viewer. The current card and photo are outlined.
  const KB = { id: null, idx: 0 };
  function kbDecorate(card) {
    if (Number(card.dataset.id) !== KB.id) return card;
    card.classList.add('kb-current');
    const thumb = $$('.thumbs .thumb:not(.missing)', card)[KB.idx];
    if (thumb) thumb.classList.add('kb-photo');
    return card;
  }
  function kbMark() {
    $$('#obs-list .kb-current, #obs-list .kb-photo').forEach((x) => x.classList.remove('kb-current', 'kb-photo'));
    const card = KB.id === null ? null : renderedCards.get(KB.id);
    if (!card || !card.isConnected) return null;
    return kbDecorate(card);
  }
  function kbMoveObs(d) {
    const cards = $$('#obs-list .obs');
    if (!cards.length) return;
    let i = cards.findIndex((c) => Number(c.dataset.id) === KB.id);
    if (i < 0) {
      // Nothing current yet (or it was filtered out): start at the first card on screen.
      i = cards.findIndex((c) => c.getBoundingClientRect().top > 200);
      if (i < 0) i = 0;
    } else {
      i = Math.max(0, Math.min(cards.length - 1, i + d));
    }
    KB.id = Number(cards[i].dataset.id);
    KB.idx = 0;
    const card = kbMark();
    if (card) card.scrollIntoView({ block: 'nearest' }); // more cards render as the list scrolls
  }
  function kbPhotos() {
    const o = KB.id === null ? null : S.obs.get(KB.id);
    const st = KB.id === null ? null : S.states.get(KB.id);
    return o && st && st.status === 'active' ? orderedPhotos(KB.id) : [];
  }
  function kbMovePhoto(d) {
    const photos = kbPhotos();
    if (!photos.length) return;
    KB.idx = Math.max(0, Math.min(photos.length - 1, KB.idx + d));
    const card = kbMark();
    const thumb = card && $('.kb-photo', card);
    if (thumb) thumb.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  function kbShiftPhoto(d) {
    const photos = kbPhotos();
    const to = KB.idx + d;
    if (!photos[KB.idx] || to < 0 || to >= photos.length) return;
    movePhoto(KB.id, photos[KB.idx].id, to);
    KB.idx = to;
    refreshCard(KB.id);
  }
  function kbToggle() {
    const p = kbPhotos()[KB.idx];
    if (!p) return;
    const st = S.states.get(KB.id);
    setPhotoSelected(st, p.id, !st.selected_photo_ids.includes(p.id));
    refreshCard(KB.id);
  }
  document.addEventListener('keydown', (e) => {
    if (S.step !== 'organize' || e.ctrlKey || e.metaKey || e.altKey) return;
    if (!$('#viewer').hidden || $$('.modal').some((m) => !m.hidden)) return;
    const t = e.target;
    if (t.closest && t.closest('input:not([type=checkbox]), textarea, select, [contenteditable]')) return;
    const k = e.key;
    // On a focused button, link or checkbox, Space and Enter keep their usual meaning.
    const activator = t.closest && t.closest('button, a, input');
    if (k === 'ArrowDown') kbMoveObs(1);
    else if (k === 'ArrowUp') kbMoveObs(-1);
    else if (k === 'ArrowRight' && e.shiftKey) kbShiftPhoto(1);
    else if (k === 'ArrowLeft' && e.shiftKey) kbShiftPhoto(-1);
    else if (k === 'ArrowRight') kbMovePhoto(1);
    else if (k === 'ArrowLeft') kbMovePhoto(-1);
    else if (k === ' ' && !activator && KB.id !== null) kbToggle();
    else if (k === 'Enter' && !activator && kbPhotos()[KB.idx]) Viewer.open(KB.id, KB.idx, stateAdapter(KB.id), null);
    else return;
    e.preventDefault();
  });
  // Clicking a card (or one of its photos) makes it current, so keys carry on from there.
  $('#obs-list').addEventListener('click', (e) => {
    const card = e.target.closest('.obs');
    if (!card) return;
    KB.id = Number(card.dataset.id);
    const thumb = e.target.closest('.thumb:not(.missing)');
    KB.idx = thumb ? Math.max(0, $$('.thumbs .thumb:not(.missing)', card).indexOf(thumb)) : 0;
    kbMark();
  }, true);

  function refreshCard(id) {
    const old = renderedCards.get(id);
    if (old && old.isConnected) {
      const card = kbDecorate(obsCard(id, Number(old.dataset.pos)));
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

  // Clockwise rotation (0, 90, 180 or 270) chosen for a photo; ObservationState.rotations.
  function rotationOf(st, pid) {
    return (st && st.rotations && st.rotations[String(pid)]) || 0;
  }
  function rotatePhoto(st, pid, delta) {
    const r = (rotationOf(st, pid) + delta + 360) % 360;
    st.rotations = st.rotations || {};
    if (r) st.rotations[String(pid)] = r; else delete st.rotations[String(pid)];
    markDirty();
  }
  const rotClass = (r) => (r ? `rot-${r}` : '');

  // An observation's photos in slide order: the user's photo_order, then the rest
  // in iNaturalist's order. Mirror of presentation.photo_positions().
  function orderedPhotos(id) {
    const o = S.obs.get(id);
    if (!o) return [];
    const st = S.states.get(id);
    const custom = (st && st.photo_order) || [];
    if (!custom.length) return o.photos;
    const byId = new Map(o.photos.map((p) => [p.id, p]));
    const out = custom.filter((pid) => byId.has(pid)).map((pid) => byId.get(pid));
    const used = new Set(out.map((p) => p.id));
    return out.concat(o.photos.filter((p) => !used.has(p.id)));
  }
  // Put photo pid at position `to` of the observation's photo order.
  function movePhoto(id, pid, to) {
    const ids = orderedPhotos(id).map((p) => p.id).filter((x) => x !== pid);
    ids.splice(Math.max(0, Math.min(ids.length, to)), 0, pid);
    const natural = S.obs.get(id).photos.map((p) => p.id);
    S.states.get(id).photo_order = ids.every((x, i) => x === natural[i]) ? [] : ids;
    markDirty();
  }
  let photoDrag = null; // {id, pid} while a photo thumbnail is dragged
  let photoMark = null;
  const clearPhotoMark = () => { if (photoMark) photoMark.classList.remove('drop-before', 'drop-after'); photoMark = null; };

  const stateAdapter = (id) => ({
    isSelected: (pid) => S.states.get(id).selected_photo_ids.includes(pid),
    toggle: (pid, on) => { setPhotoSelected(S.states.get(id), pid, on); refreshCard(id); },
    rotation: (pid) => rotationOf(S.states.get(id), pid),
    rotate: (pid, delta) => { rotatePhoto(S.states.get(id), pid, delta); refreshCard(id); },
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

  // ------------------------------------------------ more photos of the same species
  const licenseLabel = (code) => LICENSE_LABEL.get(code || 'arr') || String(code).toUpperCase();
  // The licenses of an observation's photos, e.g. "CC BY" or "CC BY, CC BY-NC".
  function licenseText(o) {
    return [...new Set(o.photos.map((p) => licenseLabel(p.license)))].join(', ');
  }

  // "More of mine" / "More from others" on a card, when turned on in More photos.
  function morePhotoButtons(id) {
    const rel = relatedSettings();
    if (rel.buttons !== true) return [];
    const o = S.obs.get(id);
    const ok = isSpecies(o);
    const name = effectiveName(id) || (o && o.inat_name) || 'this species';
    const why = ok ? '' : 'Only observations identified to species can be searched. ';
    const make = (whose, text, title) => el('button', {
      type: 'button', class: 'more-photos', text, disabled: !ok || (whose === 'mine' && !rel.username),
      title: why + (whose === 'mine' && !rel.username ? 'Enter your iNaturalist username in More photos first.' : title),
      onclick: () => findMore(whose, [o.taxon.id], { title: `More photos of ${name}`, afterId: id }),
    });
    return [
      make('mine', 'More of mine', `Find more of your own photos of ${name}, from anywhere (up to ${rel.mine.per_taxon}, most favorited first)`),
      make('others', 'More from others', `Find other people's photos of ${name} (up to ${rel.others.per_taxon}, most favorited first, ${rel.others.licenses.map(licenseLabel).join(', ')})`),
    ];
  }

  // Search these species for more photos with the More photos settings: the
  // species are added to that kind's source (made on first use), and only
  // they are searched now. What is new opens in the review dialog.
  function findMore(whose, taxonIds, ctx = {}) {
    const rel = relatedSettings();
    if (whose === 'mine' && !rel.username) { openRelated('Enter your iNaturalist username to search your own photos.'); return; }
    if (!taxonIds.length) { toast('None of the observations listed is identified to species.', true); return; }
    if (taxonIds.length > LIMITS.relatedTaxa) {
      toast(`One search can cover at most ${n(LIMITS.relatedTaxa)} species, not ${n(taxonIds.length)}. Use Find to list fewer.`, true, 9000);
      return;
    }
    let src = relatedSource(whose);
    if (!src) {
      if (S.project.sources.length >= LIMITS.sources) { toast(`A project can have at most ${LIMITS.sources} sources. Remove one first.`, true, 9000); return; }
      src = { id: newSourceId(), type: 'related', input: '', username: null, url: '', label: RELATED_LABEL[whose], enabled: true, related: { whose, taxon_ids: [] } };
      S.project.sources.push(src);
    }
    const known = new Set(src.related.taxon_ids);
    const adding = taxonIds.filter((t) => !known.has(t));
    if (known.size + adding.length > LIMITS.relatedKept) {
      toast(`"${src.label}" already lists ${n(known.size)} species; it can hold ${n(LIMITS.relatedKept)}. Remove it on Sources to start a new list.`, true, 9000);
      return;
    }
    src.related.taxon_ids.push(...adding);
    src.enabled = true;
    markDirty();
    startLoad([src.id], null, { whose, taxa: taxonIds, title: ctx.title, afterId: ctx.afterId });
  }

  // The species of the observations listed on Select & Organize (Find and the
  // filters narrow it), and how many listed are not identified to species.
  function listedSpecies() {
    const taxa = [];
    const seen = new Set();
    let skipped = 0;
    for (const it of organizeItems().items) {
      if (it.type !== 'obs') continue;
      const o = S.obs.get(it.id);
      const st = S.states.get(it.id);
      if (!o || !st || st.status !== 'active') continue;
      if (!isSpecies(o)) { skipped++; continue; }
      if (!seen.has(o.taxon.id)) { seen.add(o.taxon.id); taxa.push(o.taxon.id); }
    }
    return { taxa, skipped };
  }

  // The More photos dialog: how the searches work, and a search of every species listed.
  function openRelated(message = '') {
    const rel = relatedSettings();
    $('#rel-username').value = rel.username || defaultUsername();
    $('#rel-mine-n').value = rel.mine.per_taxon;
    $('#rel-mine-faves').value = rel.mine.min_faves;
    $$('#rel-licenses input').forEach((b) => { b.checked = rel.others.licenses.includes(b.value); });
    $('#rel-others-n').value = rel.others.per_taxon;
    $('#rel-others-faves').value = rel.others.min_faves;
    $('#rel-research').checked = rel.others.research_only;
    // Offered ticked until this project's choice is saved (null = not chosen yet).
    $('#rel-buttons').checked = rel.buttons === null || rel.buttons === undefined ? true : rel.buttons;
    $('#rel-error').textContent = message;
    updateRelatedNote();
    openModal($('#related-modal'));
  }
  function defaultUsername() {
    const mine = S.project.sources.find((s) => s.type === 'username' && s.username);
    if (mine) return mine.username;
    try { return localStorage.getItem('dp-username') || ''; } catch (e) { return ''; }
  }
  function updateRelatedNote() {
    const { taxa, skipped } = listedSpecies();
    const ticked = $$('#rel-licenses input').filter((b) => b.checked).map((b) => b.value);
    const need = estimateRequests(taxa.length, 'others', ticked.length ? ticked : null);
    const bits = [`${plural(taxa.length, 'species', 'species')} in the observations listed now`];
    if (skipped) bits.push(`${n(skipped)} identified only to genus or higher ${skipped === 1 ? 'is' : 'are'} skipped`);
    let text = bits.join('; ') + '. Use Find to list fewer first.';
    if (taxa.length > LIMITS.relatedTaxa) text += ` One search can cover at most ${n(LIMITS.relatedTaxa)}.`;
    else if (taxa.length) text += ` Other people's photos take up to ${plural(need, 'request')} to iNaturalist (${duration(need)}); your own usually far fewer.`;
    $('#rel-species-note').textContent = text;
    $('#btn-rel-all-mine').disabled = !taxa.length || taxa.length > LIMITS.relatedTaxa;
    $('#btn-rel-all-others').disabled = !taxa.length || taxa.length > LIMITS.relatedTaxa;
  }
  // Read the dialog into the project's settings. Returns false (with a message) if something is off.
  function saveRelated() {
    const err = $('#rel-error');
    const username = $('#rel-username').value.trim().replace(/^@/, '');
    if (username && (!LOGIN_RE.test(username) || /^\d+$/.test(username))) { err.textContent = 'That is not a valid iNaturalist username.'; return false; }
    const int = (sel, lo, hi) => {
      const v = Number($(sel).value);
      return Number.isInteger(v) && v >= lo && v <= hi ? v : null;
    };
    const mineN = int('#rel-mine-n', 1, 200);
    const othersN = int('#rel-others-n', 1, 200);
    const mineF = int('#rel-mine-faves', 0, 1000000);
    const othersF = int('#rel-others-faves', 0, 1000000);
    if (mineN === null || othersN === null) { err.textContent = 'How many per species must be a whole number from 1 to 200.'; return false; }
    if (mineF === null || othersF === null) { err.textContent = 'Minimum favorites must be a whole number, 0 or more.'; return false; }
    const licenses = $$('#rel-licenses input').filter((b) => b.checked).map((b) => b.value);
    if (!licenses.length) { err.textContent = 'Tick at least one license for other people\'s photos.'; return false; }
    S.project.settings.related = {
      username, buttons: $('#rel-buttons').checked,
      mine: { per_taxon: mineN, min_faves: mineF },
      others: { licenses, per_taxon: othersN, min_faves: othersF, research_only: $('#rel-research').checked },
    };
    if (username) { try { localStorage.setItem('dp-username', username); } catch (e) { /* ignore */ } }
    err.textContent = '';
    markDirty();
    return true;
  }
  $('#btn-related').addEventListener('click', () => openRelated());
  $$('#rel-licenses input').forEach((b) => b.addEventListener('change', updateRelatedNote));
  $('#btn-rel-save').addEventListener('click', () => {
    if (!saveRelated()) return;
    closeModal($('#related-modal'));
    renderOrganize(true);
  });
  [['#btn-rel-all-mine', 'mine'], ['#btn-rel-all-others', 'others']].forEach(([sel, whose]) => {
    $(sel).addEventListener('click', () => {
      if (!saveRelated()) return;
      const { taxa } = listedSpecies();
      closeModal($('#related-modal'));
      renderOrganize(true);
      findMore(whose, taxa, { title: `More ${whose === 'mine' ? 'of your photos' : 'photos from others'}: ${plural(taxa.length, 'species', 'species')}` });
    });
  });

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

    const photos = review ? o.photos : orderedPhotos(id);
    const selCount = photos.filter((p) => adapter.isSelected(p.id)).length;
    const missing = review ? [] : st.selected_photo_ids.filter((pid) => !photos.some((p) => p.id === pid));
    const card = el('div', { class: 'obs', dataset: { id: String(id), pos: String(pos || 0) } });
    // Other people's photos found only by "More from others" are limited to
    // the licenses chosen there: others are dimmed and not ticked by default.
    const allowed = allowedLicenses(review ? reviewSources(id) : st.source_ids);
    const own = isOwn(id);

    const box = el('input', {
      type: 'checkbox', checked: selCount > 0,
      title: 'Include this observation (its first photo). Tick more photos to add them.', 'aria-label': 'Include this observation',
      onchange: (e) => {
        if (e.target.checked) {
          // Never a photo outside the licenses chosen for other people's photos.
          const first = photos.find((p) => photoAllowed(p, allowed));
          if (first) adapter.toggle(first.id, true);
          else {
            e.target.checked = false;
            toast('None of this observation\'s photos has one of the licenses chosen in More photos.', true);
          }
        } else photos.forEach((p) => adapter.toggle(p.id, false));
        if (review) refreshReviewCard(id);
      },
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
          // Escape removes the input; some browsers then fire blur, which must not save.
          let finished = false;
          const save = () => {
            if (finished) return;
            finished = true;
            const v = input.value.trim();
            st.overrides.scientific = (v === '' || v === (o.inat_name || '')) ? null : v;
            markDirty();
            refreshCard(id);
          };
          input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') save();
            if (e.key === 'Escape') { finished = true; refreshCard(id); }
          });
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
      own || !o.photos.length ? null : el('span', { class: 'license', title: 'Photo license on iNaturalist', text: licenseText(o) }),
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
    if (o.rare) chips.append(el('span', { class: 'chip rare', text: `${n(o.rare.world)} worldwide`, title: `${rarityText(o.rare)} on iNaturalist; ${n(o.rare.local)} in the Rare finds search` }));
    if (!review && notesStale(id)) chips.append(el('span', { class: 'chip warn', text: `Notes are about ${st.notes_name}` }));
    if (chips.childNodes.length) info.append(chips);

    if (!review) {
      const actions = el('div', { class: 'obs-actions' },
        el('a', { href: o.uri, target: '_blank', rel: 'noopener noreferrer', text: 'iNaturalist ↗' }),
        el('button', { type: 'button', text: 'Slide text', onclick: () => toggleAnnEdit(card, id) }),
        el('button', { type: 'button', text: st.notes ? 'Notes ✓' : 'Notes', title: 'Presenter notes for this observation\'s slides', onclick: () => Notes.open(id) }),
        el('button', { type: 'button', text: 'Move to…', onclick: () => {
          const v = prompt(`Move to position (1 to ${S.project.order.length}):`, String(pos || 1));
          const k = parseInt(v, 10);
          if (k >= 1) { moveToIndex(id, k - 1); renderOrganize(true); }
        } }),
        el('button', { type: 'button', text: 'Top', onclick: () => { moveToIndex(id, 0); renderOrganize(true); } }),
        el('button', { type: 'button', text: 'Bottom', onclick: () => { moveToIndex(id, 1e9); renderOrganize(true); } }),
        ...morePhotoButtons(id),
      );
      info.append(actions);
    }

    const thumbs = el('div', { class: 'thumbs' });
    const tp = S.project.settings.title_photo;
    photos.forEach((p, idx) => {
      const sel = adapter.isSelected(p.id);
      const rot = adapter.rotation ? adapter.rotation(p.id) : 0;
      const turn = (delta, label, glyph) => el('button', {
        type: 'button', text: glyph, title: `Rotate ${label}`, 'aria-label': `Rotate photo ${idx + 1} ${label}`,
        onclick: (e) => { e.stopPropagation(); adapter.rotate(p.id, delta); },
      });
      const out = !photoAllowed(p, allowed);
      const t = el('div', { class: 'thumb' + (sel ? ' selected' : '') + (tp && tp.photo_id === p.id ? ' title-pick' : '') + (out ? ' license-out' : '') },
        el('img', { class: rotClass(rot), src: photoSize(p.url, 'small'), loading: 'lazy', decoding: 'async', alt: `Photo ${idx + 1} of ${name}`, width: 112, height: 112 }),
        el('input', {
          type: 'checkbox', checked: sel, title: 'Include this photo', 'aria-label': `Include photo ${idx + 1}`,
          onclick: (e) => e.stopPropagation(),
          onchange: (e) => { adapter.toggle(p.id, e.target.checked); if (review) refreshReviewCard(id); },
        }),
        adapter.rotate ? el('div', { class: 'rot-btns' }, turn(-90, 'left', '↺'), turn(90, 'right', '↻')) : null,
      );
      t.addEventListener('click', () => Viewer.open(id, idx, adapter, review ? () => refreshReviewCard(id) : null));
      if (!review && photos.length > 1) {
        // Drag a photo onto another of the same observation to change their slide order.
        t.draggable = true;
        t.title = 'Click to enlarge. Drag to change the order of this observation\'s photos.';
        t.addEventListener('dragstart', (e) => {
          e.stopPropagation();
          photoDrag = { id, pid: p.id };
          t.classList.add('dragging');
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('text/plain', String(p.id));
        });
        t.addEventListener('dragend', () => { photoDrag = null; t.classList.remove('dragging'); clearPhotoMark(); });
        t.addEventListener('dragover', (e) => {
          if (!photoDrag || photoDrag.id !== id) return;
          e.preventDefault();
          e.stopPropagation();
          if (photoDrag.pid === p.id) { clearPhotoMark(); return; }
          const r = t.getBoundingClientRect();
          const after = e.clientX > r.left + r.width / 2;
          if (photoMark !== t) clearPhotoMark();
          photoMark = t;
          t.classList.toggle('drop-after', after);
          t.classList.toggle('drop-before', !after);
        });
        t.addEventListener('drop', (e) => {
          if (!photoDrag || photoDrag.id !== id) return;
          e.preventDefault();
          e.stopPropagation();
          const after = t.classList.contains('drop-after');
          const moving = photoDrag.pid;
          clearPhotoMark();
          photoDrag = null;
          if (moving === p.id) return;
          const rest = photos.map((q) => q.id).filter((x) => x !== moving);
          movePhoto(id, moving, rest.indexOf(p.id) + (after ? 1 : 0));
          refreshCard(id);
        });
      }
      if (out) t.title = `${licenseLabel(p.license)}: not one of the licenses chosen for other people's photos in More photos. ${t.title || 'Click to enlarge.'}`;
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
      ['scientific', 'Scientific name', o.inat_name || ''],
      ['common_name', 'Common name', o.common_name || ''],
      ...(o.rare ? [['rarity', 'Rare finds', rarityText(o.rare)]] : []),
      ['date', 'Date', formatDate(o.observed_on)],
      ['location', 'Location', o.place_guess || ''],
      ['observer', 'Observer', (user.name || user.login) ? `Photo: ${user.name || user.login}` : ''],
    ];
    // A line's checkbox starts from Presentation Settings; a different choice is
    // stored for this observation only (ObservationState.show_lines).
    // The observer credit is left off the presenter's own photos (annotation_lines() in presentation.py).
    const settingOn = (key) => (key === 'observer' ? observerEnabled() && !isOwn(id) : !!S.project.settings.annotations[key]);
    st.show_lines = st.show_lines || {};
    const grid = el('div', { class: 'ann-edit' });
    fields.forEach(([key, label, def]) => {
      const cur = st.overrides[key];
      const own = st.show_lines[key];
      const input = el('input', {
        type: 'text', value: cur === null || cur === undefined ? '' : cur, placeholder: def || '(empty)', maxLength: 300, 'aria-label': label,
        onchange: (e) => {
          const v = e.target.value.trim();
          // Same rule as the name's "edit" button: the iNaturalist name itself is no override.
          st.overrides[key] = v === '' || (key === 'scientific' && v === def) ? null : v;
          markDirty();
          if (key === 'scientific') {
            // Redraw the card so its heading shows the new name, and keep this editor open.
            refreshCard(id);
            const fresh = renderedCards.get(id);
            if (fresh && fresh !== card) toggleAnnEdit(fresh, id);
          }
        },
      });
      grid.append(el('label', { class: 'ann-line' },
        el('input', {
          type: 'checkbox', checked: own === undefined ? settingOn(key) : own,
          title: `Show the ${label.toLowerCase()} on this observation's slides`,
          onchange: (e) => {
            if (e.target.checked === settingOn(key)) delete st.show_lines[key]; else st.show_lines[key] = e.target.checked;
            markDirty();
          },
        }),
        label), input);
    });
    grid.append(el('span'), el('span', { class: 'muted small', text: 'Tick a line to show it on this observation\'s slides, even if it is off in Presentation Settings. Leave text blank to use the iNaturalist value.' }));
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

  let sortSeq = 0;
  async function applySort(key, direction) {
    const sort = S.project.settings.sort;
    if (key === 'custom') { key = sort.base_key; direction = sort.base_direction; }
    const gen = projectGen;
    const seq = ++sortSeq;
    try {
      const res = await api('/api/sort', {
        method: 'POST',
        body: { project: S.project, workspace_id: S.workspaceId, key, direction, seed: Math.floor(Math.random() * 1e9) },
      });
      // Drop it if the project was replaced or a newer sort was asked for.
      if (gen !== projectGen || seq !== sortSeq) return;
      // Observations added or removed meanwhile: keep exactly the current ones.
      const order = res.order.filter((id) => S.states.has(id));
      const placed = new Set(order);
      S.project.order = order.concat(S.project.order.filter((id) => !placed.has(id)));
      sort.key = key;
      sort.direction = direction;
      if (key !== 'random') { sort.base_key = key; sort.base_direction = direction; }
      markDirty();
      const shown = renderOrganize();
      const option = $(`#sort-key option[value="${key}"]`);
      const how = key === 'random' ? '' : `, ${direction === 'desc' ? 'descending' : 'ascending'}`;
      announce(`Sorted by ${option ? option.textContent : key}${how}. ${groupingText()} ${shownText(shown)}`);
    } catch (err) { if (gen === projectGen && seq === sortSeq) handleApiError(err); }
  }

  $('#sort-key').addEventListener('change', (e) => {
    const key = e.target.value;
    if (S.project.settings.sort.key === 'custom' && !confirm('Re-sorting replaces your hand-arranged order. Continue?')) {
      syncToolbar(); // back to "Custom (dragged)"
      return;
    }
    const dir = ['favorites', 'observed_on', 'created_at'].includes(key) ? 'desc' : 'asc';
    // Sorting applies inside each group, so with grouping on the months would
    // start over in every group. Choosing the month sort turns grouping off to
    // give one January to December run; Group by can be turned on again after.
    if (key === 'month' && S.project.settings.grouping !== 'none') {
      S.project.settings.grouping = 'none';
      $('#grouping').value = 'none';
      markDirty();
    }
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
    if (s.key === 'custom') announce(`${groupingText()} ${shownText(renderOrganize())}`);
    else applySort(s.key, s.direction);
  });
  $('#min-faves').addEventListener('input', debounce((e) => {
    const v = Math.max(0, Math.min(1000000, parseInt(e.target.value, 10) || 0));
    S.project.settings.min_faves = v;
    markDirty();
    renderOrganize();
  }, 150));
  $('#find').addEventListener('input', debounce(() => {
    const shown = renderOrganize();
    const needle = $('#find').value.trim();
    announce(needle ? `${plural(shown, 'observation')} match "${needle}".` : shownText(shown));
  }, 200));

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
    // W x H is the photo as shown (after rotation); IW x IH is the <img> element.
    let W = 1; let H = 1; let IW = 1; let IH = 1; let rot = 0; let showing = 'large';

    const vPhotos = () => orderedPhotos(ctx.id); // same order as the card's thumbnails
    function photo() { return vPhotos()[ctx.idx]; }
    function setSize(w, h) {
      IW = w; IH = h;
      rot = ctx.adapter.rotation ? ctx.adapter.rotation(photo().id) : 0;
      [W, H] = rot === 90 || rot === 270 ? [IH, IW] : [IW, IH];
      img.style.width = `${IW}px`;
      img.style.height = `${IH}px`;
    }
    function apply() {
      // Rotate about the top-left corner, then shift so the turned photo starts at 0,0.
      const turn = {
        0: '', 90: `translate(${IH}px, 0) rotate(90deg)`,
        180: `translate(${IW}px, ${IH}px) rotate(180deg)`, 270: `translate(0, ${IW}px) rotate(270deg)`,
      }[rot];
      img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale}) ${turn}`;
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
      showing = 'large';
      setSize(p.width || 1024, p.height || 1024);
      img.src = photoSize(p.url, 'large');
      img.onload = () => {
        if (!p.width) { // dimensions unknown: use the large image's aspect
          setSize(img.naturalWidth, img.naturalHeight);
          fit();
        }
      };
      img.alt = `Photo ${ctx.idx + 1} of ${o.inat_name || 'observation'}`;
      $('#viewer-name').textContent = (S.states.has(ctx.id) ? effectiveName(ctx.id) : o.inat_name) || 'Observation';
      $('#viewer-count').textContent = `photo ${ctx.idx + 1} of ${vPhotos().length}${p.license ? ' · ' + p.license.toUpperCase() : ''}`;
      $('#v-selected').checked = ctx.adapter.isSelected(p.id);
      $('#v-rot-left').hidden = $('#v-rot-right').hidden = !ctx.adapter.rotate;
      $('#v-prev').disabled = ctx.idx === 0;
      $('#v-next').disabled = ctx.idx >= vPhotos().length - 1;
      const strip = $('#viewer-strip');
      strip.replaceChildren(...vPhotos().map((q, i) => el('button', {
        type: 'button', class: (i === ctx.idx ? 'current' : '') + (ctx.adapter.isSelected(q.id) ? ' sel' : ''),
        onclick: () => { ctx.idx = i; show(); }, 'aria-label': `Photo ${i + 1}`,
      }, el('img', { class: rotClass(ctx.adapter.rotation ? ctx.adapter.rotation(q.id) : 0), src: q.url, alt: '' }))));
      label();
      fit();
    }
    function turn(delta) {
      if (!ctx.adapter.rotate) return;
      ctx.adapter.rotate(photo().id, delta);
      show();
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
      const i = ctx.idx + d;
      if (i >= 0 && i < vPhotos().length) { ctx.idx = i; show(); }
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
    $('#v-rot-left').addEventListener('click', () => turn(-90));
    $('#v-rot-right').addEventListener('click', () => turn(90));
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
      if (e.ctrlKey || e.metaKey || e.altKey) return; // leave browser shortcuts such as Ctrl+R alone
      const k = e.key;
      // Only the viewer: not the review dialog it may have been opened from.
      if (k === 'Escape') { close(); e.stopImmediatePropagation(); }
      else if (k === 'ArrowLeft') step(-1);
      else if (k === 'ArrowRight') step(1);
      else if (k === ' ') { e.preventDefault(); toggle(); }
      else if (k === '+' || k === '=') zoomAt(1.5);
      else if (k === '-') zoomAt(1 / 1.5);
      else if (k === '0') fit();
      else if (k === '1') zoomAt(1 / scale);
      else if (k === 'r') turn(90);
      else if (k === 'R') turn(-90);
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
    const logins = [...ownLogins()];
    const yours = logins.length
      ? ` Photos by ${logins.join(' or ')} (you) are never credited.`
      : ' Every photo is credited until you give your username on Sources or in More photos.';
    if (auto) {
      note.textContent = (observerDefault()
        ? '(automatic: on, because a source may include other people\'s photos.'
        : '(automatic: off, because every source is your own photos.') + yours + ')';
    } else {
      note.append(`(set manually.${yours} `, el('button', { type: 'button', class: 'linklike', text: 'use automatic', onclick: () => { s.annotations.observer = null; markDirty(); renderSettings(); } }), ')');
    }
    $('#set-dividers').checked = s.dividers;
    $('#divider-note').textContent = s.grouping === 'none' ? '(choose “Group by” on the Select & Organize step first)' : `(grouped by ${GROUP_NAMES[s.grouping].toLowerCase()})`;
    $('#set-notes').checked = s.speaker_notes;
    $('#set-presenter-notes').checked = s.presenter_notes !== false;
    $('#ann-rarity').checked = s.annotations.rarity !== false;
    $('#set-about-slide').checked = s.about_slide !== false;
    $('#title-position').value = (s.title_photo && s.title_photo.position) || 'center';
    renderTitlePhoto();
  }

  function renderTitlePhoto() {
    const tp = S.project.settings.title_photo;
    const box = $('#title-photo-preview');
    box.replaceChildren();
    const o = tp && S.obs.get(tp.observation_id);
    const p = o && o.photos.find((q) => q.id === tp.photo_id);
    if (p) box.append(el('img', { class: rotClass(rotationOf(S.states.get(tp.observation_id), p.id)), src: photoSize(p.url, 'medium'), alt: 'Title background' }));
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
  $('#set-presenter-notes').addEventListener('change', (e) => { S.project.settings.presenter_notes = e.target.checked; markDirty(); });
  $('#ann-rarity').addEventListener('change', (e) => { S.project.settings.annotations.rarity = e.target.checked; markDirty(); });
  $('#set-about-slide').addEventListener('change', (e) => { S.project.settings.about_slide = e.target.checked; markDirty(); });
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
        closeModal($('#picker-modal'));
        renderTitlePhoto();
      },
    }, el('img', { class: rotClass(rotationOf(S.states.get(id), p.id)), src: photoSize(p.url, 'small'), loading: 'lazy', alt: effectiveName(id) })), 0, 120);
    if (!items.length) $('#picker-grid').append(el('p', { class: 'muted', text: 'No photos match.' }));
  }
  $('#btn-pick-title').addEventListener('click', () => { openModal($('#picker-modal')); renderPicker(); });
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
        slide.append(el('img', { class: `cover focus-${spec.photo.position || 'center'} ${rotClass(spec.photo.rotation)}`, src: photoSize(spec.photo.url, 'medium'), loading: 'lazy', alt: '' }));
      }
      slide.append(el('div', { class: 'center' },
        el('div', { class: 't-title' + (spec.title_size && spec.title_size !== 54 ? ` s${spec.title_size}` : ''), text: spec.title }),
        el('div', { class: 't-rule' }),
        spec.presenter ? el('div', { class: 't-presenter', text: spec.presenter }) : null));
    } else if (spec.kind === 'about') {
      slide.append(el('div', { class: 'about' },
        el('div', { class: 'a-title', text: spec.title }),
        el('ul', {}, (spec.lines || []).map((line) => el('li', { text: line })))));
    } else if (spec.kind === 'divider') {
      slide.append(el('div', { class: 'center' },
        spec.rank_label ? el('div', { class: 'd-rank', text: spec.rank_label.toUpperCase() }) : null,
        el('div', { class: 'd-name' + (spec.italic ? ' italic' : ''), text: spec.text })));
    } else {
      slide.append(el('img', { class: `photo ${rotClass(spec.rotation)}`, src: photoSize(spec.url, 'medium'), loading: 'lazy', decoding: 'async', alt: '' }));
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
    $('#preview-stats').textContent = `${plural(c.total, 'slide')}: 1 title, ${plural(c.dividers, 'divider')}, ${plural(c.images, 'photo slide')} from ${plural(c.observations, 'observation')}${c.about ? ', and an About these numbers slide' : ''}`;
    $('#preview-warnings').replaceChildren(...plan.warnings.map((w) => el('div', { class: 'warning', text: w })));
    if (previewRenderer) previewRenderer.disconnect();
    $('#btn-notes-all').hidden = !Notes.claudeOn || c.images === 0;
    Notes.updatePicks(); // a selection made earlier stays
    previewRenderer = chunkRender(grid, $('#preview-sentinel'), plan.slides, (spec, i) => {
      const wrap = el('div', { class: 'pslide' + (spec.kind === 'image' ? ' draggable' : ''), dataset: spec.kind === 'image' ? { obs: String(spec.observation_id) } : {} },
        el('span', { class: 'num', text: String(i + 1) }), slideNode(spec, plan.background));
      if (spec.kind === 'image') {
        wrap.draggable = true;
        wrap.append(Notes.button(spec.observation_id));
        if (Notes.claudeOn) wrap.append(Notes.pick(spec.observation_id));
      }
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
          el('div', { class: 'result-actions' },
            link,
            el('button', {
              type: 'button', class: 'btn btn-lg', text: 'Download project file (.json)',
              title: 'Save your selections, order and settings so you can reopen and update this presentation later',
              onclick: saveProjectFile,
            })),
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
    $('#btn-review-add-selected').disabled = chosen === 0 || !!$('#review-modal').dataset.busy;
  }

  // `ctx` (a "more photos" search): {title, afterId}. With afterId the new
  // observations can go right after that card.
  function showReview(sum, newOnly = false, ctx = null) {
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
    $('#review-title').textContent = (ctx && ctx.title) || (newOnly ? 'Review new observations' : 'Project refreshed from iNaturalist');
    setReviewPlacement(ctx && ctx.afterId);

    const ids = (sum.new_ids || []).filter((id) => S.obs.has(id) && !S.states.has(id));
    const hasNew = ids.length > 0;
    $('#review-new').hidden = !hasNew;
    ['#btn-review-ignore', '#btn-review-add-selected', '#btn-review-add-all'].forEach((s) => { $(s).hidden = !hasNew; });
    $('#btn-review-close').textContent = hasNew ? 'Decide later' : 'Continue';
    $('#btn-review-close').className = hasNew ? 'btn' : 'btn btn-primary';
    if (hasNew) {
      $('#review-new-title').textContent = `${plural(ids.length, 'unique new observation')} to review`;
      const chips = $('#review-by-source');
      const bySource = {};
      ids.forEach((id) => ((sum.membership || {})[String(id)] || []).forEach((sid) => { bySource[sid] = (bySource[sid] || 0) + 1; }));
      chips.replaceChildren(...S.project.sources.filter((s) => bySource[s.id]).map((s) => el('span', { class: 'chip', text: `${s.label}: ${n(bySource[s.id])} new` })));
      // Like add_observations: only the first photo of each starts selected
      // (the first with an allowed license, for other people's photos).
      // Reopening a review put off with "Decide later" keeps the choices made then.
      const prev = newOnly ? reviewSel : new Map();
      reviewSel = new Map(ids.map((id) => {
        const photos = S.obs.get(id).photos;
        const kept = prev.has(id) ? photos.filter((p) => prev.get(id).has(p.id)).map((p) => p.id) : defaultPhotoIds(id, reviewSources(id));
        return [id, new Set(kept)];
      }));
      $('#review-all').checked = ids.every((id) => reviewSel.get(id).size);
      if (reviewRenderer) reviewRenderer.disconnect();
      reviewRenderer = chunkRender($('#review-list'), $('#review-sentinel'), ids, (id) => obsCard(id, 0, reviewAdapter(id)), 0, 40);
      updateReviewButtons();
    }
    // After a card's More photos button the list was re-rendered, so focus
    // goes back to that card's rebuilt button (or More photos… in the toolbar).
    const after = ctx && ctx.afterId;
    openModal($('#review-modal'), ctx ? () => {
      const card = after ? renderedCards.get(after) : null;
      return (card && card.isConnected && $('.more-photos:not(:disabled)', card)) || $('#btn-related');
    } : null);
    S.reviewIds = ids;
  }

  $('#review-all').addEventListener('change', (e) => {
    for (const id of S.reviewIds || []) {
      reviewSel.set(id, e.target.checked ? new Set(defaultPhotoIds(id, reviewSources(id))) : new Set());
    }
    const count = reviewRenderer ? reviewRenderer.count : 0;
    reviewRenderer.disconnect();
    reviewRenderer = chunkRender($('#review-list'), $('#review-sentinel'), S.reviewIds, (id) => obsCard(id, 0, reviewAdapter(id)), count, 40);
    updateReviewButtons();
  });

  // "Place them": "right after <card>" is offered after a card's More photos button.
  let reviewAfterId = null;
  function setReviewPlacement(afterId) {
    const select = $('#review-placement');
    const old = $('option[value="after"]', select);
    if (old) old.remove();
    reviewAfterId = afterId && S.states.has(afterId) ? afterId : null;
    if (reviewAfterId) {
      const name = effectiveName(reviewAfterId) || `observation ${reviewAfterId}`;
      select.prepend(el('option', { value: 'after', text: `right after ${name}` }));
      select.value = 'after';
    } else if (!select.value) {
      select.value = 'sorted';
    }
  }

  async function addReviewed(ids, ignoreIds) {
    const modal = $('#review-modal');
    if (modal.dataset.busy) return; // one request at a time
    const selected = {};
    ids.forEach((id) => { selected[String(id)] = [...(reviewSel.get(id) || [])]; });
    // The server returns the whole project, so nothing may change it until the
    // reply is in: the dialog stays open (and covers the page) meanwhile, and
    // the list is inert so photo choices, including cards rendered later, can't change.
    const buttons = $$('button', modal);
    modal.dataset.busy = '1';
    buttons.forEach((b) => { b.disabled = true; });
    $('#review-new').inert = true;
    const gen = projectGen;
    try {
      const placement = $('#review-placement').value;
      const res = await api('/api/observations/add', {
        method: 'POST',
        body: {
          project: S.project, workspace_id: S.workspaceId, observation_ids: ids,
          selected_photo_ids: selected, placement, ignore_ids: ignoreIds,
          after_id: placement === 'after' ? reviewAfterId : null,
        },
      });
      if (gen !== projectGen) return; // another project was opened meanwhile
      setProject(res.project);
      markDirty();
      // Still waiting: everything new that was not added or ignored, including
      // observations a "more photos" review did not show (other searches').
      const pending = new Set([...((S.pendingNew && S.pendingNew.new_ids) || []), ...(S.reviewIds || [])]);
      const ignored = new Set(S.project.ignored_observation_ids);
      const remaining = [...pending].filter((id) => !S.states.has(id) && !ignored.has(id));
      if (S.pendingNew) S.pendingNew.new_ids = remaining;
      if (!remaining.length) S.pendingNew = null;
      closeModal($('#review-modal'));
      if (ids.length) toast(`Added ${plural(ids.length, 'observation')}.`);
      else if (ignoreIds.length) toast(`Ignored ${plural(ignoreIds.length, 'observation')}; they won't be offered again.`);
      goto(S.step === 'sources' ? 'organize' : S.step);
    } catch (err) {
      handleApiError(err);
    } finally {
      delete modal.dataset.busy;
      $('#review-new').inert = false;
      buttons.forEach((b) => { b.disabled = false; });
      updateReviewButtons();
    }
  }
  $('#btn-review-add-all').addEventListener('click', () => {
    // Every observation goes in; keep photo choices made here, else its first
    // photo (its first with an allowed license, for other people's photos).
    for (const id of S.reviewIds) {
      if (!reviewSel.get(id) || !reviewSel.get(id).size) reviewSel.set(id, new Set(defaultPhotoIds(id, reviewSources(id))));
    }
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
        if (m.id === 'load-modal' || m.dataset.busy) return;
        closeModal(m);
      }
    });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $$('.modal').forEach((m) => { if (m.id !== 'load-modal' && !m.dataset.busy) closeModal(m); });
  });

  // ---------------------------------------------------------------- presenter notes
  // An observation's notes (st.notes) go into the speaker notes of each of its
  // slides. They can be typed here, or written by Claude on the server
  // (/api/notes, app/notes.py) when the site has that set up. Mirrors of
  // server rules: counts() follows JobManager.start_notes (one note per name;
  // observations with notes are skipped unless replacing).
  const Notes = (() => {
    const claudeOn = document.body.dataset.notesEnabled === '1';
    const webOn = claudeOn && document.body.dataset.notesWeb === '1';
    $$('[data-notes-claude]').forEach((x) => { x.hidden = !claudeOn; });
    $$('[data-notes-web]').forEach((x) => { x.hidden = !webOn; });
    const WEB_KEY = 'dp-notes-web';
    const picked = new Set(); // observations selected on Preview for "Write notes for selected"
    // The "Search the web" choice is remembered in this browser.
    const webChoice = () => { try { return webOn && localStorage.getItem(WEB_KEY) === '1'; } catch (e) { return false; } };
    const setWebChoice = (on) => { try { localStorage.setItem(WEB_KEY, on ? '1' : '0'); } catch (e) { /* storage blocked */ } };
    const CODE_KEY = 'dp-notes-code';
    const modal = $('#notes-modal');
    const allModal = $('#notes-all-modal');
    const text = $('#notes-text');
    const writeBtn = $('#btn-notes-claude');
    let currentId = null;
    let jobId = null;

    const storedCode = () => { try { return localStorage.getItem(CODE_KEY) || ''; } catch (e) { return ''; } };
    const storeCode = (v) => {
      try { if (v) localStorage.setItem(CODE_KEY, v); else localStorage.removeItem(CODE_KEY); } catch (e) { /* storage blocked: ask each time */ }
    };

    function askCode(message) {
      const m = $('#notes-code-modal');
      const form = $('#notes-code-form');
      const input = $('#notes-code-input');
      $('#notes-code-message').textContent = message || 'Writing notes with Claude needs the access code for this site.';
      input.value = '';
      return new Promise((resolve) => {
        let answer = null;
        const submit = (e) => { e.preventDefault(); answer = input.value.trim(); closeModal(m); };
        form.addEventListener('submit', submit);
        modalOnClose.set(m, () => { form.removeEventListener('submit', submit); resolve(answer || null); });
        openModal(m);
        input.focus();
      });
    }

    // Start a notes job, asking for the access code when needed. null: no code given.
    async function start(ids, replace, web) {
      let code = storedCode() || await askCode();
      while (code) {
        try {
          const job = await api('/api/notes', {
            method: 'POST', headers: { 'X-Notes-Code': code },
            body: { project: S.project, workspace_id: S.workspaceId, observation_ids: ids, replace, web_search: !!web },
          });
          storeCode(code);
          return job;
        } catch (err) {
          if (err.status !== 403) throw err;
          storeCode('');
          code = await askCode(err.message);
        }
      }
      return null;
    }

    // Write notes with Claude and put them in the project. Returns the
    // finished job (with .applied), or null if it never started or another
    // project was opened meanwhile.
    async function run(ids, replace, web, onProgress) {
      const gen = projectGen;
      const job = await start(ids, replace, web);
      if (!job) return null;
      jobId = job.id;
      try {
        const final = await pollJob(job.id, onProgress);
        if (gen !== projectGen) return null;
        return { ...final, applied: apply(final.result, replace) };
      } finally {
        jobId = null;
      }
    }

    // Without "replace", an observation that got notes while Claude was
    // writing (typed meanwhile) keeps them.
    function apply(result, replace) {
      let applied = 0;
      for (const [key, v] of Object.entries((result && result.notes) || {})) {
        const st = S.states.get(Number(key));
        if (!st || (!replace && st.notes)) continue;
        st.notes = v.text;
        st.notes_name = v.name;
        applied += 1;
        updateViews(st.id);
      }
      if (applied) markDirty();
      return applied;
    }

    async function stop() {
      if (!jobId) return;
      try { await api(`/api/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' }); } catch (e) { /* ignore */ }
    }

    // The notes were written for another name than the observation has now.
    function stale(id) {
      const st = S.states.get(id);
      return !!(st && st.notes && st.notes_name && st.notes_name !== effectiveName(id));
    }

    function button(id) {
      const st = S.states.get(id);
      const has = !!(st && st.notes);
      return el('button', {
        type: 'button', class: 'notes-btn' + (has ? ' has' : '') + (stale(id) ? ' stale' : ''),
        text: has ? 'Notes ✓' : 'Notes',
        title: has ? 'Presenter notes: view or edit' : 'Add presenter notes for this observation',
        onclick: (e) => { e.stopPropagation(); open(id); },
      });
    }

    function updateViews(id) {
      $$(`.pslide[data-obs="${id}"] .notes-btn`).forEach((b) => b.replaceWith(button(id)));
      if (S.step === 'organize') refreshCard(id); // the other steps redraw when shown
    }

    // ---- one observation
    function setStatus(message, isError = false) {
      const p = $('#notes-status');
      p.textContent = message;
      p.hidden = !message;
      p.classList.toggle('error', isError);
    }
    function showState() {
      const st = S.states.get(currentId) || {};
      const warn = $('#notes-stale');
      warn.hidden = !stale(currentId);
      if (!warn.hidden) warn.textContent = `These notes were written for ${st.notes_name}. The name on the slides is now ${effectiveName(currentId) || 'empty'}.`;
      writeBtn.textContent = st.notes ? 'Rewrite with Claude' : 'Write with Claude';
    }

    function open(id) {
      const st = S.states.get(id);
      if (!st) return;
      currentId = id;
      const o = S.obs.get(id);
      $('#notes-for').textContent = effectiveName(id) || (o && o.common_name) || `Observation ${id}`;
      text.value = st.notes || '';
      setStatus('');
      showState();
      $('#notes-web').checked = webChoice();
      modalOnClose.set(modal, () => updateViews(id));
      openModal(modal);
      text.focus();
    }

    text.addEventListener('input', () => {
      const st = S.states.get(currentId);
      if (!st) return;
      const v = text.value.slice(0, 4000);
      if (v.trim()) {
        if (!st.notes) st.notes_name = effectiveName(currentId) || null;
        st.notes = v;
      } else {
        st.notes = null;
        st.notes_name = null;
      }
      markDirty();
    });

    $('#btn-notes-remove').addEventListener('click', () => {
      const st = S.states.get(currentId);
      if (st && st.notes) {
        if (!confirm('Remove the presenter notes for this observation?')) return;
        st.notes = null;
        st.notes_name = null;
        markDirty();
      }
      closeModal(modal);
    });

    writeBtn.addEventListener('click', async () => {
      if (modal.dataset.busy) { stop(); return; } // the button says "Stop" while Claude writes
      const id = currentId;
      const st = S.states.get(id);
      if (!st) return;
      if (st.notes && !confirm('Replace these notes with new ones from Claude?')) return;
      modal.dataset.busy = '1';
      text.disabled = true;
      $('#btn-notes-remove').disabled = true;
      writeBtn.textContent = 'Stop';
      setStatus('Claude is writing notes. This takes 10 to 30 seconds…');
      try {
        const web = webOn && $('#notes-web').checked;
        if (web) setStatus('Claude is searching the web and writing notes. This takes up to a minute or two…');
        const final = await run([id], true, web, (j) => {
          if (j.state === 'queued') setStatus('Waiting for other notes to finish…');
        });
        if (!final) setStatus('');
        else if (final.state === 'done' && final.applied) setStatus('');
        else if (final.state === 'cancelled') setStatus('Stopped.');
        else setStatus(final.error || final.warnings[0] || 'Claude did not write notes for this name.', true);
      } catch (err) {
        setStatus(err.message, true);
        if (err.status === 410) handleApiError(err);
      } finally {
        delete modal.dataset.busy;
        text.disabled = false;
        $('#btn-notes-remove').disabled = false;
        if (currentId === id) {
          text.value = (S.states.get(id) || {}).notes || '';
          showState();
        }
      }
    });

    // ---- every slide
    // target: the selected observation ids, or null for every slide.
    let target = null;
    function counts() {
      const slides = (S.plan && S.plan.slides) || [];
      const ids = [...new Set(slides.filter((sp) => sp.kind === 'image').map((sp) => sp.observation_id))]
        .filter((id) => S.states.has(id) && effectiveName(id).trim() && (!target || target.has(id)));
      const without = ids.filter((id) => !S.states.get(id).notes);
      const names = (list) => new Set(list.map((id) => effectiveName(id).trim().toLowerCase())).size;
      return { all: ids, without, withNotes: ids.length - without.length, names };
    }

    function updateStart() {
      const c = counts();
      const list = $('#notes-all-replace').checked ? c.all : c.without;
      const btn = $('#btn-notes-all-start');
      btn.disabled = !list.length || !!allModal.dataset.busy;
      btn.textContent = list.length ? `Write notes for ${plural(c.names(list), 'name')}` : 'Write notes';
    }

    function openAll(selected = false) {
      target = selected ? new Set(picked) : null;
      const c = counts();
      const where = target ? `of the ${plural(c.all.length, 'selected observation')}` : 'in this presentation';
      $('#notes-all-title').textContent = target ? 'Write notes for the selected slides' : 'Write notes for all slides';
      $('#notes-all-summary').textContent = c.without.length
        ? `${plural(c.without.length, 'observation')} ${where} (${plural(c.names(c.without), 'name')}) ${c.without.length === 1 ? 'has' : 'have'} no notes yet.`
        : (target ? 'Every selected observation has notes.' : 'Every observation in this presentation has notes.');
      $('#notes-all-web').checked = webChoice();
      $('#notes-all-replace').checked = false;
      $('#notes-all-replace-row').hidden = c.withNotes === 0;
      $('#notes-all-replace-label').textContent = `Also rewrite the ${plural(c.withNotes, 'observation')} that already ${c.withNotes === 1 ? 'has' : 'have'} notes`;
      $('#notes-all-progress').hidden = true;
      $('#btn-notes-all-cancel').textContent = 'Cancel';
      updateStart();
      openModal(allModal);
    }

    $('#btn-notes-all').addEventListener('click', () => openAll(false));
    $('#btn-notes-selected').addEventListener('click', () => openAll(true));
    $('#btn-notes-clear').addEventListener('click', () => { picked.clear(); updatePicks(); });
    $('#notes-all-web').addEventListener('change', (e) => setWebChoice(e.target.checked));
    $('#notes-web').addEventListener('change', (e) => setWebChoice(e.target.checked));
    $('#notes-all-replace').addEventListener('change', updateStart);
    $('#btn-notes-all-cancel').addEventListener('click', () => { if (allModal.dataset.busy) stop(); else closeModal(allModal); });
    $('#btn-notes-all-start').addEventListener('click', async () => {
      const replace = $('#notes-all-replace').checked;
      if (replace && !confirm('Replace the notes that are already there, including any you typed?')) return;
      allModal.dataset.busy = '1';
      $('#btn-notes-all-start').disabled = true;
      $('#notes-all-replace').disabled = true;
      $('#btn-notes-all-cancel').textContent = 'Stop';
      $('#notes-all-progress').hidden = false;
      $('#notes-all-message').textContent = 'Starting…';
      setBar($('#notes-all-bar'), 0, 0);
      try {
        const web = webOn && $('#notes-all-web').checked;
        const final = await run(target ? [...target] : null, replace, web, (j) => {
          $('#notes-all-message').textContent = j.message || (j.state === 'queued' ? 'Waiting for other notes to finish…' : 'Working…');
          setBar($('#notes-all-bar'), j.done, j.total);
        });
        if (!final) { $('#notes-all-progress').hidden = true; return; }
        if (target && final.state === 'done') { picked.clear(); updatePicks(); }
        const done = `Notes written for ${plural(final.applied, 'observation')}.`;
        const failed = (final.result && final.result.failed) || [];
        let message = final.state === 'done' ? done
          : final.state === 'cancelled' ? `Stopped. ${done}` : `${final.error} ${done}`;
        if (failed.length) message += ` Claude could not write notes for ${plural(failed.length, 'name')}: ${failed.slice(0, 5).join(', ')}${failed.length > 5 ? ', …' : ''}.`;
        delete allModal.dataset.busy;
        closeModal(allModal);
        toast(message, final.state === 'error' || failed.length > 0, 10000);
      } catch (err) {
        $('#notes-all-message').textContent = err.message;
        if (err.status === 410) handleApiError(err);
      } finally {
        delete allModal.dataset.busy;
        $('#notes-all-replace').disabled = false;
        $('#btn-notes-all-cancel').textContent = 'Cancel';
        updateStart();
      }
    });

    // ---- selecting slides on Preview (per observation: all its slides at once)
    function pick(id) {
      return el('input', {
        type: 'checkbox', class: 'notes-pick', checked: picked.has(id),
        title: 'Select for "Write notes for selected"', 'aria-label': 'Select this observation for Claude notes',
        onclick: (e) => e.stopPropagation(),
        onchange: (e) => {
          if (e.target.checked) picked.add(id); else picked.delete(id);
          updatePicks();
        },
      });
    }
    function updatePicks() {
      $$('.pslide[data-obs]').forEach((w) => {
        const box = $('.notes-pick', w);
        if (box) box.checked = picked.has(Number(w.dataset.obs));
      });
      $('#btn-notes-selected').hidden = !claudeOn || picked.size === 0;
      $('#btn-notes-clear').hidden = !claudeOn || picked.size === 0;
      $('#btn-notes-selected').textContent = `Write notes for ${plural(picked.size, 'selected observation')}…`;
    }

    return { claudeOn, button, open, stale, pick, updatePicks };
  })();
  const notesStale = (id) => Notes.stale(id);

  // ---------------------------------------------------------------- save / open
  $('#btn-save-project').addEventListener('click', saveProjectFile);
  async function saveProjectFile() {
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
      saveDraft();
      toast('Project saved. Open it here later to refresh it from iNaturalist.');
    } catch (err) { toast(err.message, true); }
  }

  $('#btn-open-project').addEventListener('click', () => {
    if (S.dirty && S.project.observations.length && !confirm('Open another project? Unsaved changes to this one will be lost.')) return;
    $('#project-file').click();
  });
  $('#project-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) { toast('That file is too large to be a project file.', true); return; }
    const gen = projectGen;
    try {
      const text = await file.text();
      const res = await api('/api/project/validate', { method: 'POST', raw: text });
      if (gen !== projectGen) return; // New project (or another file) was chosen meanwhile
      openProject(res.project);
    } catch (err) { if (gen === projectGen) toast(err.message, true, 9000); }
  });

  function openProject(project) {
    projectGen++; // cancels a draft restore still in progress
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

  window.addEventListener('pagehide', saveDraft);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') saveDraft(); });

  // A draft that could not be restored is moved here, so autosaving whatever the
  // user does next can never delete it. A card offers to try again or discard it.
  const UNRESTORED_KEY = 'dp-draft-unrestored';

  function readDraft(key) {
    try {
      const d = JSON.parse(localStorage.getItem(key) || 'null');
      return d && d.project && Array.isArray(d.project.sources) && d.project.sources.length ? d : null;
    } catch (e) { return null; }
  }
  function setAsideDraft(d) {
    try { localStorage.setItem(UNRESTORED_KEY, JSON.stringify(d)); } catch (e) { /* ignore */ }
  }

  // Validate a draft and continue it. Returns false if it could not be restored.
  // `replace` = the user asked for it in place of the open project.
  async function restoreDraft(d, replace = false) {
    const gen = projectGen;
    let project;
    try {
      project = (await api('/api/project/validate', { method: 'POST', body: d.project })).project;
    } catch (err) {
      toast(`Your last project could not be restored: ${err.message}`, true, 9000);
      return false;
    }
    // The user opened or started another project while this was checked. At
    // startup that discards the draft; a retry keeps the set-aside copy.
    if (gen !== projectGen) return !replace;
    // They began adding sources instead: keep their work, set the old one aside.
    if (S.project.sources.length && !replace) return false;
    projectGen++;
    setProject(project);
    S.obs = new Map();
    S.workspaceId = d.workspaceId || null;
    S.loadedSources = new Set();
    S.sourceCounts = {};
    S.pendingNew = null;
    S.dirty = d.dirty !== false;
    restoring = false;
    goto('sources');
    if (!project.observations.length || !project.sources.some((s) => s.enabled)) {
      toast('Restored your sources. Use New project in the top bar to start over.', false, 7000);
      return true;
    }
    // With a live workspace nothing is searched again; an expired one means a normal refresh.
    await startLoad(S.workspaceId ? [] : null, { step: d.step, dirty: d.dirty });
    if (!S.obs.size) { S.workspaceId = null; updateStepper(); } // load failed or was cancelled; Sources can retry
    return true;
  }

  function offerUnrestored() {
    const d = readDraft(UNRESTORED_KEY);
    $('#unrestored-card')?.remove();
    if (!d) return;
    const title = d.project.settings && d.project.settings.title;
    const card = el('div', { class: 'card subtle', id: 'unrestored-card' },
      el('p', { text: `A project you were working on${title ? ` (“${title}”)` : ''} could not be restored: ${plural(d.project.sources.length, 'source')}, ${plural((d.project.observations || []).length, 'observation')}.` }),
      el('div', { class: 'row-start' },
        el('button', { type: 'button', class: 'btn btn-primary', text: 'Try again', onclick: async () => {
          if (S.project.sources.length && !confirm('Replace the project that is open now with the earlier one?')) return;
          // The open project stays untouched unless the earlier one checks out.
          if (await restoreDraft(d, true)) {
            try { localStorage.removeItem(UNRESTORED_KEY); } catch (e) { /* ignore */ }
            card.remove();
          }
        } }),
        el('button', { type: 'button', class: 'btn', text: 'Discard it', onclick: () => {
          if (!confirm('Discard the earlier project? This cannot be undone.')) return;
          try { localStorage.removeItem(UNRESTORED_KEY); } catch (e) { /* ignore */ }
          card.remove();
        } })));
    $('#step-sources').prepend(card);
  }

  async function resumeDraft() {
    let d = readDraft(DRAFT_KEY);
    if (!d) {
      try { // the older format stored only the project
        const p = JSON.parse(localStorage.getItem('dp-project') || 'null');
        if (p && Array.isArray(p.sources) && p.sources.length) d = { project: p, step: 'organize', dirty: true };
      } catch (e) { /* ignore */ }
    }
    if (d && !(await restoreDraft(d))) setAsideDraft(d);
    // Only now may autosave touch the stored draft: it was restored or set aside.
    restoring = false;
    offerUnrestored();
  }

  $('#btn-new-project').addEventListener('click', () => {
    if (S.dirty && S.project.observations.length && !confirm('Start a new project? Unsaved changes to this one will be lost.')) return;
    try { localStorage.removeItem(DRAFT_KEY); localStorage.removeItem('dp-project'); } catch (e) { /* ignore */ }
    openProject(newProject());
  });

  renderSources();
  updateStepper();
  resumeDraft();
})();
