# Dikarya Presentations

**https://presentations.dikarya.us/** builds PowerPoint decks from iNaturalist
observations, sized and laid out for projectors. (`presentation.dikarya.us` permanently redirects here.)
Licensed under the [GNU GPL v3](LICENSE).

Pick one or more iNaturalist sources, choose photos, put them in order, preview the
real slide sequence and download a 16:9 `.pptx`. Built for mushroom-club talks and
conferences: decks of hundreds of photos and 500 to 1,000+ slides are expected.

No accounts, no OAuth, no server-side project storage. Users keep their work in a
small JSON project file; the server holds only short-lived temporary data.

---

## Contents

- [Workflow](#workflow)
- [Architecture](#architecture)
- [iNaturalist API limits](#inaturalist-api-limits)
- [Sources, merging and de-duplication](#sources-merging-and-de-duplication)
- [Project files](#project-files)
- [How refresh works](#how-refresh-works)
- [PowerPoint generation rules](#powerpoint-generation-rules)
- [Security and limits](#security-and-limits)
- [Development](#development)
- [Production layout](#production-layout)
- [Deployment](#deployment)

---

## Workflow

1. **Sources**: paste the URL of an iNaturalist observations search (filter on
   iNaturalist first by taxon, place, project, dates, quality grade and so on),
   optionally with your iNaturalist username. Several sources can be combined,
   labeled and disabled.
2. **Select & Organize**: one card per observation with its name, common name,
   date, place, observer, a visible favorite count (♥ 17) and a checkbox for every
   photo. Only the first photo of each observation starts checked; tick others to
   add more slides. ↺ / ↻ on a photo (or R / Shift+R in the viewer) rotates it on
   its slide. Sort (taxonomic, favorites, observation date, date added, name,
   iNaturalist order, random), group (kingdom … genus, or source), filter by minimum
   favorites, drag to reorder, edit the name or any annotation line. Click a photo
   to inspect it at 1024 px, zooming to the full-resolution original only when
   needed.
3. **Presentation settings**: title, presenter, optional title background photo
   (any loaded photo), black or white background, annotation lines, divider slides,
   speaker notes.
4. **Preview**: the actual slide sequence as 16:9 thumbnails, including title,
   dividers and annotations. Dragging a photo slide moves its whole observation.
5. **Generate**: the server downloads the full-quality photos, builds the deck with
   progress (`Downloading photos: 47 / 218`, `Building slides: …`), and offers the
   download, with a button to save the project file next to it.

## Architecture

```
browser (vanilla JS, no framework)          FastAPI (single Uvicorn worker)
  project state: sources, selections,  ──►  /api/*  ──►  services
  overrides, order, settings                         ├─ inaturalist.py  URL parsing, API client, normalization
  thumbnails straight from iNat CDN                  ├─ project.py      validation, merge, refresh diff, edits
                                                     ├─ sorting.py      sorting, grouping, filters, insertion
                                                     ├─ presentation.py slide plan + python-pptx writer
                                                     ├─ images.py       trusted downloads, cache, Pillow prep
                                                     ├─ workspace.py    temp store of fetched metadata
                                                     ├─ jobs.py         background load/generate jobs
                                                     └─ ratelimit.py    1 req/s limiter, daily/hourly budgets
```

| Module | Responsibility |
|---|---|
| `app/main.py` | App factory, routes, body-size limit, security headers, error handling. No business logic. |
| `app/models.py` | Pydantic models for the project file and request bodies (strict types and bounds). |
| `app/inaturalist.py` | Turns a URL and optional username into search parameters; the rate-limited v2 API client (pagination, batched id and taxa lookups, 429 backoff); normalization; name formatting; photo-URL allowlist. |
| `app/project.py` | Project-file load/validate/migrate, source merging, refresh diffing, add/ignore/remove-source, observer default, safe filenames. |
| `app/sorting.py` | Sort keys, grouping, min-favorites/source filters, sorted insertion that keeps a hand-made order. |
| `app/presentation.py` | `build_slide_plan()` (used by both Preview and Generate, so they always match) and the PPTX writer. |
| `app/images.py` | Downloads originals from allowlisted hosts with size caps and a byte budget, a 2-hour cache, EXIF/format fixes, title-photo treatment. |
| `app/workspace.py` | Gzipped JSON of the metadata the last load fetched, keyed by an unguessable id, expiring after 24 h. |
| `app/jobs.py` | Thread-pool jobs with progress, cancellation, timeouts, per-client and global limits, cleanup. |

**Workspaces.** Sorting, previewing and generating need metadata for up to
10,000 observations. To avoid the browser uploading megabytes on every click,
the server keeps what it fetched (trusted, server-derived data) for 24
hours. If a workspace expires, the client reloads from iNaturalist automatically.

**One worker.** The iNaturalist rate limiter, daily request budget, media
byte budget and job registry are in-process. Running more than one Uvicorn
worker would multiply the request rate to iNaturalist. Generation runs in
threads, so one worker is enough.

## iNaturalist API limits

Implemented as recommended in iNaturalist's
[API Recommended Practices](https://www.inaturalist.org/pages/api+recommended+practices):

- **~1 request/second** process-wide (`RateLimiter`), shared by every user and job.
- **Daily request budget** (default 8,500/day, persisted across restarts). When
  it is exhausted, users get a friendly "try again tomorrow".
- **`per_page=200`** for every search, and the v2 `fields=` parameter so responses
  contain only what the app uses.
- **Batched id lookups**: refreshing saved observations and resolving ancestor
  taxon names use comma-separated ids, 200 per request. Taxon names are cached
  on disk for 30 days.
- **No bulk paging**: a source matching more than 10,000 observations is refused
  after a single request, asking the user to add filters.
- **429/5xx** are retried with backoff that respects `Retry-After`, slowing the
  shared limiter for everyone.
- **Media**: thumbnails and the zoom viewer load directly from iNaturalist in the
  user's browser. The server downloads originals only when a deck is generated,
  under a rolling budget of 4 GB/hour and 20 GB/day (iNaturalist may block above
  5 GB/hour or 24 GB/day). Originals are cached for 2 hours, so regenerating after
  a typo fix re-downloads nothing.
- **User-Agent**: `DikaryaPresentations/1.0 (+https://presentations.dikarya.us)`.

All limits are environment variables (see `app/config.py`).

## Sources, merging and de-duplication

The add-source form has two fields: an **observations URL** (required) and an
**iNaturalist username** (optional).

- The URL (also iNaturalist Network domains, `/projects/<slug>`,
  `/observations/<id>`, `/people/<login>`) is parsed into search parameters.
  Paging and website-only parameters are dropped, as are values of `any`
  (`verifiable=any`, `quality_grade=any`), which the website uses to mean "no
  filter" but the API rejects. Unstable orderings (`order_by=random`) are
  removed. The server never fetches the URL itself; it only sends the parsed
  parameters to `api.inaturalist.org`.
- With a username, the search is restricted to that user (`user_id=<login>` is
  added, keeping every other filter) and the source is a `username` source,
  meaning "the presenter's own photos". A URL that already filters by a
  different user is refused. A URL that already filters to exactly one user by
  login (`user_id=<login>`, `user_login=<login>` or `/observations/<login>`) is
  treated the same as entering that username; a numeric user id or several
  users leave it a `url` source. A bare username typed into the URL box is refused
  too, because on its own it would load a whole account.
- Each source records `type` (`username` | `url`), the original `input`, the
  canonical `url` (always `https://www.inaturalist.org/observations?...`, including
  the user filter), the `username` when given, an editable `label`, and `enabled`.
- Results from all enabled sources form one pool, de-duplicated by observation
  id. Each observation remembers **every** source that matched it
  (`source_ids`).
- **Disabling** a source hides observations that *only* it matched; nothing is
  deleted. **Removing** a source deletes observations no other source matched.
  Observations shared with another source stay.
- Grouping by source places an observation under its first enabled source.
- Adding a source to a loaded project queries only that source. Its new
  observations go to the review dialog.

## Project files

Saved as `<title>.dikarya-presentation.json`, typically a few kB to a few hundred
kB. Photos and full iNaturalist metadata are **not** embedded.

```json
{
  "format": "dikarya-presentation",
  "schema_version": 4,
  "saved_at": "2026-10-06T03:40:00Z",
  "app_version": "1.0.0",
  "sources": [
    {"id": "s1a2b3c4d5e6", "type": "username", "input": "alan_rockefeller",
     "username": "alan_rockefeller",
     "url": "https://www.inaturalist.org/observations?user_id=alan_rockefeller",
     "label": "alan_rockefeller", "enabled": true},
    {"id": "s0f9e8d7c6b5", "type": "url",
     "input": "https://www.inaturalist.org/observations?place_id=6924&taxon_id=47170",
     "username": null,
     "url": "https://www.inaturalist.org/observations?place_id=6924&taxon_id=47170",
     "label": "Costa Rica", "enabled": true}
  ],
  "settings": {
    "title": "Fungi of Costa Rica", "presenter": "Alan Rockefeller",
    "background": "black",
    "title_photo": {"observation_id": 123, "photo_id": 456, "position": "center"},
    "sort": {"key": "custom", "direction": "asc", "base_key": "taxonomic", "base_direction": "asc"},
    "grouping": "family", "dividers": true, "min_faves": 0,
    "annotations": {"scientific": true, "common_name": false, "date": false,
                    "location": false, "observer": null},
    "speaker_notes": true
  },
  "observations": [
    {"id": 123, "selected_photo_ids": [456, 457], "known_photo_ids": [456, 457, 458],
     "rotations": {"457": 90}, "show_lines": {"common_name": true}, "photo_order": [457, 456],
     "overrides": {"scientific": "Psilocybe alimapensis nom. prov.", "common_name": null,
                   "date": null, "location": null, "observer": null},
     "source_ids": ["s1a2b3c4d5e6", "s0f9e8d7c6b5"], "status": "active",
     "last_inat_name": "Psilocybe sp.", "added_at": "2026-10-06T03:30:00Z"}
  ],
  "order": [123],
  "ignored_observation_ids": []
}
```

- `overrides`: `null` means "use the live iNaturalist value"; a string replaces
  it on the slide; `""` hides that line for this observation.
- `annotations.observer: null` means automatic (see below); `true`/`false` is the
  user's explicit choice.
- `sort.key = "custom"` marks a hand-arranged order. `base_key` records the
  automatic sort that new observations are inserted by.
- `show_lines`: per-observation choice to show (`true`) or leave out (`false`)
  a slide line (`scientific`, `common_name`, `date`, `location`, `observer`),
  whatever the presentation settings say; missing lines follow the settings.
  Added in schema 3.
- `photo_order`: the user's order for the observation's photos (dragged on the
  Select & Organize step); photos not listed follow in iNaturalist's order.
  Slides use this order. Added in schema 4.
- `rotations`: clockwise degrees (90, 180 or 270) by photo id; unrotated photos
  are left out. Added in schema 2; a schema 1 file loads with none.
- `known_photo_ids` / `last_inat_name` exist only so a refresh can report deleted
  or new photos and changed identifications.
- **Validation** (`project.load_project`): wrong `format`, missing or non-integer
  `schema_version`, a newer schema than the server supports, out-of-range values,
  oversized lists, duplicate source ids, and source URLs that fail the same
  parser as new input are all rejected with a readable message. Unknown keys are
  ignored. Inconsistent `order`/duplicate observations are normalized.
- **Schema versions**: `_migrate()` in `app/project.py` is where `v1` to `v2`
  upgrades go. Bump `SCHEMA_VERSION` in `app/models.py` when the format changes
  incompatibly.

## How refresh works

Opening a project file (or pressing *Refresh from iNaturalist*):

1. The file is validated on the server.
2. Every enabled source's saved URL is searched again.
3. Saved observations that no source returned are looked up by id (batched), so
   they are refreshed even if they no longer match a search.
4. `refresh_project()` compares live data with the project:
   - existing observations get current names, photos and membership. User
     overrides, selections and order are never touched;
   - an observation that no longer exists becomes an **unavailable placeholder**
     (kept, labeled, skipped when generating; it comes back if it reappears);
   - a selected photo that was deleted stays selected and is **flagged in red**.
     No replacement is chosen automatically; the user picks one;
   - photos added on iNaturalist to existing observations are reported, not
     selected;
   - observations that are new to the project (and not previously ignored) are
     **reported, not added**, with per-source counts and a unique total.
5. A summary dialog shows the counts. When there are new observations it offers
   **Add selected**, **Add all** or **Ignore**, with the first photo of each
   checked by default and placement either *by the current sort* (each new observation goes
   right after its nearest predecessor in the automatic sort, so a hand-made
   order is preserved) or *at the end*. Ignored ids are stored and not offered
   again.

The very first load of a project with no observations adds everything, with the
first photo of each selected, in default taxonomic order.

## PowerPoint generation rules

- **16:9 widescreen** (13.333 × 7.5 in).
- **Image slides never crop.** Each photo is scaled proportionally to the
  largest size that fits, centred both ways. Unused space shows the background
  (black by default, or white).
- **Full resolution**: the iNaturalist `original` (typically up to 2048 px) is
  embedded **byte-for-byte**. It is re-encoded (JPEG q95, 4:4:4) only when
  necessary: EXIF rotation (PowerPoint ignores the orientation tag), CMYK, or a
  format PowerPoint cannot show (WebP). A photo the user rotated is still
  embedded unchanged; the picture is turned on the slide (so it can be turned
  back in PowerPoint) and sized so the turned photo fits without cropping.
- **Annotation** (lower-left, small margin, no backing box): the scientific/taxon
  name in *italic* ~28 pt. Optional lines below it at 18 pt: English common name,
  date (`November 14, 2026`), iNaturalist place description, observer
  (`Photo: <display name or login>`). Only the scientific name is italic. On black
  slides the text is white with a subtle dark shadow; on white slides it is
  near-black with a light halo, so it stays readable over the white letterbox.
- **Names**: the current iNaturalist identification at whatever rank
  (`Amanita muscaria`, `Psilocybe sp.`, `Hygrophoraceae`,
  `Amanita muscaria var. guessowii`, `Amanita sect. Caesareae`). An observation
  identified only as **Life** gets no taxon annotation. A "Provisional Species
  Name" observation field is offered as a one-click override.
- **Observer default**: if every enabled source was added with the presenter's
  username, the observer line defaults **off**. If any source has no username, it
  defaults **on**, since that search may include other people's photos. An
  explicit choice is saved and always wins.
- **Favorite counts never appear in the deck**, including the notes.
- **Title slide**: title (54 pt bold) and presenter (28 pt) centred with a gold
  rule. The optional background photo is the only cropped image: a full-bleed
  16:9 cover crop (focus centre/top/bottom), desaturated, darkened and slightly
  softened in Pillow, never upscaled past the source.
- **Divider slides** (optional, when grouping): rank label in small gold caps
  above the group name, which is italic for genera.
- **Speaker notes** (optional): name, common name, iNaturalist link and the
  photo's license attribution.
- Text stays editable; photos stay picture objects; nothing is rasterized.
- **Memory**: image parts are streamed from disk when the file is written, and
  media are stored without zip deflate. A 400-photo, 1 GB deck was built with a
  peak RSS increase of under 20 MB.
- The file is named from the title (`Fungi_of_Costa_Rica.pptx`, ASCII-safe),
  kept for 2 hours and then deleted with all temporary files.

## Security and limits

- **No arbitrary fetching (SSRF)**: source URLs are parsed, never fetched; only
  `api.inaturalist.org` is contacted with the parsed parameters. Photo downloads
  use URLs taken from API responses held server-side, re-checked against an
  allowlist (`inaturalist-open-data.s3.amazonaws.com`, `static.inaturalist.org`,
  `https`, `/photos/<id>/<size>.<ext>`), with redirects disabled. The browser
  never supplies a URL to download.
- **Input validation**: Pydantic models with bounds on every list and string; the
  source parser rejects other hosts, credentials, ports, control characters,
  unknown parameter shapes and filterless searches.
- **Size limits**: 8 MB request bodies (nginx and app), 20 sources, 10,000
  observations, 200 photos per observation, 2,000 photo slides, 40 MB per image,
  120 MP decode limit.
- **Runaway jobs**: at most 3 concurrent loads and 2 concurrent generations, 6
  queued generations, 2 active jobs per client, 20/60-minute timeouts, user
  cancellation, and a free-disk check before generation.
- **Paths**: workspace and job ids are random tokens validated by regex before
  touching the filesystem; download filenames are sanitized.
- **Errors**: no stack traces or filesystem paths are returned; `/docs` and
  `/openapi.json` are disabled.
- **Headers**: strict CSP (`script-src 'self'`; images only from self and the two
  iNaturalist photo hosts), `X-Frame-Options: DENY`, `nosniff`, HSTS via nginx.
- **nginx**: per-IP rate limit on `/api/` (10 r/s, burst 60) and the shared
  Dikarya bad-bot map.
- **systemd**: runs as the unprivileged `presentations` user with
  `ProtectSystem=strict`; the only writable path is `/var/lib/presentations`.

## Development

```bash
cd /var/www/presentations            # or any checkout
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# tests (iNaturalist is mocked; no network needed)
.venv/bin/python -m pytest

# run locally (temporary data goes to ./var)
.venv/bin/uvicorn --factory app.main:create_app --reload --port 8031
# -> http://127.0.0.1:8031/
```

Tests cover source parsing and malicious-input rejection, pagination and batching
against a fake iNaturalist, rate limiting and budgets, merging and overlap,
refresh diffing (new/unavailable observations, deleted photos, overrides surviving
refresh, ignored ids), observer defaults, every sort and grouping, min-favorites
filtering, manual-order preservation, project JSON round-trip and validation, and
generated PPTX files inspected with python-pptx (16:9, uncropped centred photos,
byte-for-byte media, backgrounds, annotations, dividers, title-slide crop,
no favorite counts), plus end-to-end API flows.

## Production layout

```
/var/www/presentations/          git checkout and live code (owner tree, group presentations,
  app/  tests/  deploy/  ...     read-only to the service)
  .venv/                         virtualenv created by the install script
/var/lib/presentations/          temporary data (service user only)
  workspaces/  media-cache/  jobs/  state/   (budgets, taxa cache)
/etc/systemd/system/presentations.service
/etc/nginx/sites-available/presentations.dikarya.us.conf  (+ symlink in sites-enabled)
/etc/letsencrypt/live/presentations.dikarya.us/            (both hostnames)
/usr/local/sbin/restart-presentations                       (root-owned restart wrapper)
/etc/sudoers.d/presentations                                (lets tree run only that wrapper)
```

The service listens only on `127.0.0.1:8031` (Dikarya uses 5000/8000, the image
service 8017, others 9000).

Logs: `journalctl -u presentations`. Restart after a code change:
`sudo /usr/local/sbin/restart-presentations`, which import-checks the code as the
service user first and refuses while a presentation is being generated (see
CLAUDE.md for its exit codes).

## Deployment

**Everyday changes** are made in the `/var/www/presentations` checkout and go live
as described in CLAUDE.md: templates immediately, Python after
`sudo /usr/local/sbin/restart-presentations`. No root needed.

**Root-owned pieces** (systemd unit, nginx vhost, TLS, the restart wrapper and
its sudoers rule, OS packages) come from one reviewed, idempotent script. Run it
in place from the checkout:

```bash
sudo /var/www/presentations/deploy/install-presentations.sh
```

For a first install on a new server, stage the code elsewhere and run the script
from there; it copies the code into `/var/www/presentations` (never touching a
`.git` directory) and refuses a staging tree with group/world-writable files or
unexpected owners:

```bash
rsync -a --delete --exclude .venv --exclude 'var*' ./ /tmp/presentations-src/
chmod -R go-w /tmp/presentations-src
cp deploy/install-presentations.sh /tmp/install-presentations.sh
sudo bash /tmp/install-presentations.sh
```

The script's header lists every step. It:

- checks preconditions;
- installs `python3-venv`/`rsync`/`certbot` only if missing;
- creates the `presentations` system user;
- syncs code to `/var/www/presentations` and builds `.venv`;
- runs an import check;
- installs and restarts the hardened systemd unit and waits for `/healthz`;
- writes the dedicated nginx vhost, running `nginx -t` before every reload and
  restoring the previous file if the test fails;
- sets up TLS;
- installs the restart wrapper and the sudoers rule for the code owner.

For TLS, the script reuses `/etc/letsencrypt/live/presentations.dikarya.us` if
it already covers both names. Otherwise it first confirms that both DNS names
reach this server (over HTTP, or by public DNS matching dikarya.us, since this
host has no NAT hairpin), then requests one certificate with certbot's
**webroot** method (`/var/www/letsencrypt`), as the labels and images subdomains
do. Until then the site is served over HTTP. The script ends with service
status, port, the `nginx -t` result, hostnames and redirect checks.

Environment overrides: `SRC_DIR`, `CODE_OWNER`, `PORT`, `SKIP_APT=1`,
`SKIP_CERTBOT=1`.

**DNS**: `presentations.dikarya.us` and `presentation.dikarya.us` must both
point at this server before the certificate can be issued. If they don't yet,
the script leaves the site on HTTP and says so. Re-run it once DNS is in place.

## License

Dikarya Presentations is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See [LICENSE](LICENSE) for the full text.

This program comes with a warranty: I guarantee it will work perfectly. If it
doesn't do exactly what you want, let me know and I'll add the feature.
