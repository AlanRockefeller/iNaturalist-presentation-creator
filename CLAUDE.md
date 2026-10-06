# CLAUDE.md

Guidance for Claude Code (and other coding agents) working on Dikarya
Presentations. `AGENTS.md` is a symlink to this file. README.md has the full
design: architecture, project-file format, refresh rules and slide rules.

## What this is

A FastAPI app at https://presentations.dikarya.us/ that turns iNaturalist
observations into PowerPoint decks. `presentation.dikarya.us` 301-redirects to
it. No accounts and no server-side project storage: users keep a small JSON
project file.

## Where things live

| Path | What |
|---|---|
| `/var/www/presentations` | The live code **and** the git checkout (branch `test`). Owned by `tree`, so Claude can edit it directly. |
| `/var/www/presentations/.venv` | Production virtualenv, also owned by `tree`. |
| `/var/lib/presentations` | Temporary data (workspaces, cached originals, generated decks, budgets, taxa cache). Owned by the `presentations` service user; `tree` cannot read it. |
| `/etc/systemd/system/presentations.service` | Uvicorn on `127.0.0.1:8031`, one worker, runs as `presentations`. Written by the install script. |
| `/etc/nginx/sites-available/presentations.dikarya.us.conf` | The vhost (proxy, redirect, TLS, `/api/` rate limit). Written by the install script. |
| `/usr/local/sbin/restart-presentations` | Root-owned restart wrapper (see below). Written by the install script. |
| `/etc/sudoers.d/presentations` | Lets `tree` run only that wrapper. Written by the install script. |

Run Claude from the checkout so its sandbox can write there:

```bash
cd /var/www/presentations && claude
```

## Commands

```bash
# one-time: the production venv has no pytest
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/python -m pytest              # all tests; iNaturalist is mocked, no network
node --check app/static/js/app.js       # syntax check for the browser code

# local dev server on another port (the live one uses 8031)
PRESENTATIONS_WORK_DIR=$TMPDIR/pres .venv/bin/uvicorn --factory app.main:create_app --port 8040
```

## Making a change go live

Editing files here changes the live site, so test before saving work that the
running process picks up on its own.

| Change | How it goes live |
|---|---|
| Templates (`app/templates/`) | Immediately; they are re-read from disk. |
| CSS / JS / images / fonts (`app/static/`) | Immediately on disk, but browsers cache them for 7 days. Bump `__version__` in `app/__init__.py` (it is the `?v=` cache-buster) and restart. |
| Python (`app/*.py`) | Restart with the wrapper. |
| `requirements.txt` | `.venv/bin/pip install -r requirements.txt`, then restart. |
| systemd unit, nginx vhost, TLS, the wrapper or sudoers rule, OS packages | Edit `deploy/install-presentations.sh`; the user runs it as root. |

### Restarting: `sudo /usr/local/sbin/restart-presentations`

This is the only thing `tree` may run with sudo. The wrapper:

1. imports the app as the `presentations` user (never as root) and refuses to
   restart if that fails, so a broken change cannot take the site down;
2. refuses if a presentation is being generated, because a restart cancels it;
3. restarts the service and waits up to 20 s for `/healthz`.

Always check the exit code. Do not hide it with `|| true`.

| Exit | Meaning | What to do |
|---|---|---|
| 0 | Restarted and healthy | Done. |
| 64 | Bad arguments | It takes nothing, or exactly `--force`. |
| 69 | Restarted but not answering: **the site is down** | Read the journal lines it printed, fix, restart again. |
| 70 | `systemctl restart` failed | Read the status it printed. |
| 75 | A deck is being generated | Wait and retry. Use `--force` only if the user agrees to cancel it. |
| 77 | Not run as root | Use `sudo`. |
| 78 | Import check failed; **not restarted**, old code still serving | Fix the error it printed. |

`--force` cancels in-progress generations. Ask the user before using it.

### Running the install script

Needed only for the root-owned pieces in the last row of the table above, or
on a new server. The user runs it, in place from this checkout:

```bash
sudo /var/www/presentations/deploy/install-presentations.sh
```

It is idempotent. Run from the checkout, it uses the code in place and leaves
`.git` alone; it then rebuilds the venv, re-runs the import check, rewrites the
unit, vhost, wrapper and sudoers rule, tests nginx with `nginx -t` before any
reload, and prints a status report. Never run it yourself; it needs root.

## Git

- `/var/www/presentations` is a clone of
  https://github.com/AlanRockefeller/iNaturalist-presentation-creator.
- Work on the `test` branch. Commit and push there; **the user opens the pull
  requests into `main` themselves.** Do not push to `main`.
- Commit or push only when the user asks.
- Pushing uses a write-enabled deploy key through the SSH alias
  `github-presentations` (`~/.ssh/github_presentations_deploy_ed25519`), which is
  already the `origin` URL. The `github-dikarya*` keys cannot write to this
  repo, and `gh` is not logged in, so pull requests cannot be opened from here.
- `.venv/`, `var*/`, caches and `*.pptx` are ignored.

## Logs

`tree` cannot read the systemd journal. The wrapper prints recent journal lines
when a restart fails. Otherwise ask the user to run:

```bash
journalctl -u presentations -n 100 --no-pager
```

`/healthz` is public and shows the version and how many loads and generations
are in progress:

```bash
curl -s https://presentations.dikarya.us/healthz
```

## Rules that matter

- **Respect iNaturalist's API limits.** Every API call goes through
  `INatClient._get()` (shared ~1 request/second limiter, daily budget, 429
  backoff). Use `per_page=200`, batch id lookups 200 at a time, never page past
  10,000 results. Photo downloads go through `MediaFetcher` (byte budget,
  2-hour cache). Do not add a second path to iNaturalist, and never run more
  than one Uvicorn worker: the limiter and budgets live in the process.
- **Never fetch a URL the browser supplied.** Source URLs are parsed into
  parameters (`build_source`, `parse_source_input`); photo URLs come from API
  data held on the server and are re-checked by `photo_url()`.
- **Sources** are an observations URL plus an optional username. A username
  restricts the search to that user and makes it a `username` source, which
  turns the photo credit off by default. Website-only `any` values
  (`verifiable=any`) are dropped because the API rejects them.
- **`build_slide_plan()` decides slide content and order.** Preview and
  `write_pptx()` both render it. Do not compute slide content anywhere else.
- **Image slides never crop**; only the title slide may. **Favorite counts never
  go into the PPTX.** Tests assert both.
- **Refresh never adds, drops or reorders silently**: new observations are
  offered for review, missing ones become unavailable placeholders, deleted
  selected photos stay selected and flagged, and user overrides are untouched.
- **Project-file changes**: bump `SCHEMA_VERSION` in `app/models.py` and add a
  step to `_migrate()` in `app/project.py`. Never put photos or cached
  iNaturalist metadata in the file.
- Small client-side copies of server rules exist for instant feedback
  (`removeSourceLocal`, `observerDefault`, `groupLabel`, the minimum-favorites
  filter in `app.js`). Keep them in step with `project.py` and `sorting.py`.
- All iNaturalist text goes into the page with `textContent`. The CSP forbids
  inline scripts and inline `style` attributes.
- Writing style for UI text, docs and comments: no em-dashes, plain wording.
