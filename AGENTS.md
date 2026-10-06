# AGENTS.md

Guidance for coding agents (Claude Code etc.) working on Dikarya Presentations.
`CLAUDE.md` is a symlink to this file. README.md has the full design.

## Commands

```bash
.venv/bin/python -m pytest            # all tests; iNaturalist is mocked
.venv/bin/uvicorn --factory app.main:create_app --reload --port 8031
```

Production is `presentations.service` (root-managed). Agents cannot restart it;
deployment is `deploy/install-presentations.sh`, run by a human as root.

## Rules that matter

- **Respect iNaturalist's API limits.** Every API call goes through
  `INatClient._get()` (shared ~1 req/s limiter, daily budget, 429 backoff). Use
  `per_page=200`, batch id lookups 200 at a time, never page past 10,000 results.
  Media downloads go through `MediaFetcher` (byte budget, 2 h cache). Never add a
  second code path that talks to iNaturalist, and never run more than one Uvicorn
  worker: the limiter and budgets are in-process.
- **Never fetch a URL the browser supplied.** Sources are parsed into parameters
  (`parse_source_input`); photo URLs come from API data held server-side and are
  re-checked by `photo_url()`.
- **`build_slide_plan()` decides slide content and order.** Preview renders it
  and `write_pptx()` renders it. Do not compute slide
  content anywhere else.
- **Image slides never crop**; only the title slide may. **Favorite counts never
  go into the PPTX** (tests assert both).
- **Refresh never adds, drops or reorders silently**: new observations are
  reported for review; missing ones become unavailable placeholders; deleted
  selected photos stay selected and flagged; user overrides are untouched.
- **Project-file changes**: bump `SCHEMA_VERSION` in `app/models.py` and add a
  step to `_migrate()` in `app/project.py`. Never embed photos or cached iNat
  metadata in the file.
- Small client-side mirrors exist for instant UI feedback (`removeSourceLocal`,
  `observerDefault`, `groupLabel`, the min-favorites filter in `app.js`); keep
  them in step with `project.py` / `sorting.py`.
- All iNaturalist text goes into the DOM via `textContent`; the CSP forbids
  inline scripts and inline `style` attributes.
