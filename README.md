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
- [More photos of the same species](#more-photos-of-the-same-species)
- [Project files](#project-files)
- [How refresh works](#how-refresh-works)
- [PowerPoint generation rules](#powerpoint-generation-rules)
- [Presenter notes](#presenter-notes)
- [Security and limits](#security-and-limits)
- [Development](#development)
- [Production layout](#production-layout)
- [Deployment](#deployment)

---

## Workflow

1. **Sources**: paste the URL of an iNaturalist observations search (filter on
   iNaturalist first by taxon, place, project, dates, quality grade and so on),
   optionally with your iNaturalist username. Several sources can be combined,
   labeled and disabled. Each is marked **Mine** (your own photos) or **Search**
   (may include other people's); a search limited to one user can be switched
   between the two.
2. **Select & Organize**: one card per observation with its name, common name,
   date, place, observer, a visible favorite count (♥ 17) and a checkbox for every
   photo. Only the first photo of each observation starts checked; tick others to
   add more slides. ↺ / ↻ on a photo (or R / Shift+R in the viewer) rotates it on
   its slide. Sort (taxonomic, favorites, observation date, date added, name,
   iNaturalist order, random), group (kingdom … genus, or source), filter by minimum
   favorites, drag to reorder, edit the name or any annotation line. Click a photo
   to inspect it at 1024 px, zooming to the full-resolution original only when
   needed. **More photos…** pulls in more observations of a species already in
   the project: your own from anywhere (**More of mine** on a card) or other
   people's under the licenses you choose (**More from others**), most
   favorited first.
3. **Presentation settings**: title, presenter, optional title background photo
   (any loaded photo), black or white background, annotation lines, divider slides,
   speaker notes.
4. **Preview**: the actual slide sequence as 16:9 thumbnails, including title,
   dividers and annotations. Dragging a photo slide moves its whole observation.
   Each photo slide has a **Notes** button for its presenter notes, which Claude
   can write, for one slide or all of them (see [Presenter notes](#presenter-notes)).
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
                                                     ├─ related.py      "more photos" searches of a project's species
                                                     ├─ jobs.py         background load/generate/notes jobs
                                                     ├─ notes.py        presenter notes via the Claude Code CLI
                                                     ├─ ratelimit.py    1 req/s limiter, daily/hourly budgets
                                                     └─ diagnostics.py  request and job logging
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
| `app/related.py` | "More photos of the same species": search parameters, licenses, and the request plan (top N per species, most favorited first). |
| `app/jobs.py` | Thread-pool jobs with progress, cancellation, timeouts, per-client and global limits, cleanup. |
| `app/notes.py` | Presenter notes: what Claude is told, the `claude -p` subprocess, cleaning its answer, the access code. |
| `app/diagnostics.py` | Request IDs, one log line per request and per job, client addresses (IPv6 grouped by /64). |

**Workspaces.** Sorting, previewing and generating need metadata for up to
10,000 observations. To avoid the browser uploading megabytes on every click,
the server keeps what it fetched (trusted, server-derived data) until 24 hours
after it was last used. If a workspace expires, the client reloads from
iNaturalist automatically.

**One worker.** The iNaturalist rate limiter, daily request budget, media
byte budget and job registry are in-process. Running more than one Uvicorn
worker would multiply the request rate to iNaturalist. Generation runs in
threads, so one worker is enough.

## iNaturalist API limits

Implemented as recommended in iNaturalist's
[API Recommended Practices](https://www.inaturalist.org/pages/api+recommended+practices):

- **~1 request/second** process-wide (`RateLimiter`), shared by every user and job.
- **Daily request budget** (default 8,500/day, persisted across restarts). When
  it is exhausted, users get a friendly "try again tomorrow". One client (an
  IPv4 address or an IPv6 /64) may use at most a quarter of it per day.
- **`per_page=200`** for every search, and the v2 `fields=` parameter so responses
  contain only what the app uses.
- **Batched id lookups**: refreshing saved observations and resolving ancestor
  taxon names use comma-separated ids, 200 per request. Taxon names are cached
  on disk for 30 days.
- **No bulk paging**: a source matching more than 10,000 observations is refused
  after a single request, asking the user to add filters.
- **More photos** searches cost at most one request per species plus one per
  100 species, are checked against what is left of the day's budgets before
  they start, and run only when the user asks (never on a refresh). A
  one-species request asks for just the N it keeps (`per_page=N`) instead of 200:
  it is one page either way, and fewer bytes for iNaturalist.
- **429/5xx** are retried with backoff that respects `Retry-After`, slowing the
  shared limiter for everyone.
- **Media**: thumbnails and the zoom viewer load directly from iNaturalist in the
  user's browser. The server downloads originals only when a deck is generated,
  under a rolling budget of 4 GB/hour and 20 GB/day (iNaturalist may block above
  5 GB/hour or 24 GB/day). Originals are cached for 2 hours, so regenerating after
  a typo fix re-downloads nothing.
- **User-Agent**: `DikaryaPresentations/<version> (+https://presentations.dikarya.us; contact via dikarya.us)`.

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
  treated the same as entering that username, since it is most likely the
  presenter's own search; a numeric user id or several users leave it a `url`
  source. A bare username typed into the URL box is refused too, because on its
  own it would load a whole account.
- The source list shows each source as **Mine** (`username`) or **Search**
  (`url`). For a search limited to one user, clicking the badge switches it, for
  a talk about someone else's photos. The choice is saved in the project file.
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

## More photos of the same species

On Select & Organize, **More photos…** opens one dialog where the user sets how
"more photos" searches work, and can search every species listed at once. With
**Show More of mine and More from others on each observation** ticked, each
card gets those two buttons, which search that observation's species with the
saved settings, so they ask nothing.

- **Your iNaturalist username**: needed for your own photos. Your observations
  are left out of other people's (`not_user_id`), and your photos never get the
  observer credit.
- **My photos**: your observations of the species from anywhere, up to N per
  species (default 20), with an optional minimum of favorites.
- **Other people's photos**: limited to the ticked photo licenses (default CC0,
  CC BY, CC BY-SA and CC BY-ND, which allow commercial use), up to N per species
  (default 5), at least N favorites (default 1), research grade only (default).
  "All rights reserved" cannot be asked of the API, so with it and only some
  other licenses ticked the licenses are checked on the server instead.
- Results are sorted by favorites (`order_by=votes`), and each species keeps its
  own top N, so popular species do not crowd out rare ones. A variety counts
  under the species that was searched.
- Only observations identified to species or finer (`rank_level` 10 or lower)
  can be searched; the dialog says how many listed are skipped. One search
  covers at most 500 species (`PRESENTATIONS_MAX_RELATED_TAXA`).
- New observations open in a short review dialog ("More photos of *X*"), with
  **right after this observation** as the default placement (the order then
  counts as hand-made). Nothing is added until the user chooses. In "others"
  results, a photo outside the chosen licenses is dimmed, marked "Other license"
  and not ticked by default; the first allowed photo is.
- Behind the scenes a project has at most two `related` sources, "More of my
  photos" and "Other people's photos". Each lists the species searched so far
  (up to 1,000); the search settings live in `settings.related`, so a change
  applies to the next search. On Sources they show a **More photos** badge, the
  species count and **Search again** (all of their species, with the current
  settings, after a confirm showing the cost). They can be relabeled, disabled
  or removed like any source.
- They are **searched only when asked**. An ordinary refresh (or opening the
  file) refreshes their observations by id, which is cheap, and never repeats
  the per-species searches. Their membership only grows: searching one species
  never takes the source away from observations it found earlier.

## Project files

Saved as `<title>.dikarya-presentation.json`, typically a few kB to a few hundred
kB. Photos and full iNaturalist metadata are **not** embedded.

```json
{
  "format": "dikarya-presentation",
  "schema_version": 7,
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
     "label": "Costa Rica", "enabled": true},
    {"id": "s9a8b7c6d5e4", "type": "related", "input": "", "username": null,
     "url": "https://www.inaturalist.org/observations?not_user_id=alan_rockefeller&order=desc&order_by=votes&photo_license=cc0,cc-by,cc-by-sa,cc-by-nd&popular=true&quality_grade=research&taxon_id=47692,118289",
     "label": "Other people's photos", "enabled": true,
     "related": {"whose": "others", "taxon_ids": [47692, 118289]}}
  ],
  "settings": {
    "title": "Fungi of Costa Rica", "presenter": "Alan Rockefeller",
    "background": "black",
    "title_photo": {"observation_id": 123, "photo_id": 456, "position": "center"},
    "sort": {"key": "custom", "direction": "asc", "base_key": "taxonomic", "base_direction": "asc"},
    "grouping": "family", "dividers": true, "min_faves": 0,
    "annotations": {"scientific": true, "common_name": false, "date": false,
                    "location": false, "observer": null},
    "speaker_notes": true, "presenter_notes": true,
    "related": {"username": "alan_rockefeller", "buttons": true,
                "mine": {"per_taxon": 20, "min_faves": 0},
                "others": {"licenses": ["cc0", "cc-by", "cc-by-sa", "cc-by-nd"],
                           "per_taxon": 5, "min_faves": 1, "research_only": true}}
  },
  "observations": [
    {"id": 123, "selected_photo_ids": [456, 457], "known_photo_ids": [456, 457, 458],
     "rotations": {"457": 90}, "show_lines": {"common_name": true}, "photo_order": [457, 456],
     "overrides": {"scientific": "Psilocybe alimapensis nom. prov."},
     "source_ids": ["s1a2b3c4d5e6", "s0f9e8d7c6b5"],
     "last_inat_name": "Psilocybe sp.", "added_at": "2026-10-06T03:30:00Z",
     "notes": "• First point ...\n• Second point ...",
     "notes_name": "Psilocybe alimapensis nom. prov."}
  ],
  "order": [123],
  "ignored_observation_ids": []
}
```

- Observation fields at their defaults are left out of the file (an override
  that is not set, no rotations, `"status": "active"`, empty lists); loading
  fills them back in. Settings and sources are always written in full.
- `type` of a source: `username` (Mine: the presenter's own photos) or `url`
  (Search). Since schema 5 it is kept as saved; before, every load re-derived
  it from the URL, and loading an older file still does that once. Schema 6
  added `related` sources (see [More photos](#more-photos-of-the-same-species)):
  `related.whose` (`mine` | `others`) and the `taxon_ids` searched; their `url`
  and `username` are rebuilt from those and `settings.related` on load.
- `settings.related`: the More photos settings. `username` is the presenter's
  iNaturalist login (`""` = not given); `buttons` shows the two buttons on each
  card. Added in schema 6; an older file loads with the defaults.
- `overrides`: `null` (or left out) means "use the live iNaturalist value"; a
  string replaces it on the slide; `""` hides that line for this observation.
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
- `notes`: the observation's presenter notes (typed, or written by Claude), one
  point per line, at most 4,000 characters. `notes_name` is the name they were
  written for, so the app can point out notes about an old name.
  `settings.presenter_notes` puts them in the deck. Added in schema 7.
- **Validation** (`project.load_project`): wrong `format`, missing or non-integer
  `schema_version`, a newer schema than the server supports, out-of-range values,
  oversized lists, duplicate source ids, and source URLs that fail the same
  parser as new input are all rejected with a readable message. Unknown keys are
  ignored. Inconsistent `order`/duplicate observations are normalized.
- **Schema versions**: `_migrate()` in `app/project.py` upgrades older files one
  version at a time (currently 1 to 7). Bump `SCHEMA_VERSION` in `app/models.py`
  and add a step there when the format or its meaning changes.
- **Limits that keep a file loadable**: at most 200 photos are remembered per
  observation (iNaturalist photos past that are not offered) and the newest
  50,000 ignored observation ids are kept.

## How refresh works

Opening a project file (or pressing *Refresh from iNaturalist*):

1. The file is validated on the server.
2. Every enabled source's saved URL is searched again, except More photos
   (`related`) sources, which are searched only when the user asks.
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
  username, the observer line defaults **off**. If any source has no username
  (or is a "more from others" search), it defaults **on**, since it may include
  other people's photos. An explicit choice is saved and always wins.
- **The presenter's own photos are never credited**, whatever the setting. "The
  presenter" is every Mine source's username plus the username in More photos;
  with none given, every photo is credited. Showing the observer line in one
  observation's Slide text still credits it.
- **Favorite counts never appear in the deck**, including the notes.
- **Title slide**: title (54 pt bold; 44 pt over 70 characters, 36 pt over 120,
  so long titles stay on the slide) and presenter (28 pt) centred with a gold
  rule. The optional background photo is the only cropped image: a full-bleed
  16:9 cover crop (focus centre/top/bottom), desaturated, darkened and slightly
  softened in Pillow, never upscaled past the source. If it cannot be read the
  deck is still made, without it, and the user is told.
- **Text**: control characters (a line break pasted from PowerPoint or Word,
  say) become spaces and characters a .pptx cannot hold are dropped, both on
  input and when the deck is written.
- **Divider slides** (optional, when grouping): rank label in small gold caps
  above the group name, which is italic for genera. A new divider starts with
  each new group, even when two groups share a name (a genus name in two
  families, two sources with the same label), as in Select & Organize.
- **Speaker notes**: name, common name, iNaturalist link and the photo's
  license attribution (setting `speaker_notes`), and the observation's
  presenter notes between them, set off by blank lines (setting
  `presenter_notes`). Every photo slide of an observation has the same notes.
- Text stays editable; photos stay picture objects; nothing is rasterized.
- **Memory**: image parts are streamed from disk when the file is written, and
  media are stored without zip deflate. A 400-photo, 1 GB deck was built with a
  peak RSS increase of under 20 MB.
- The file is named from the title (`Fungi_of_Costa_Rica.pptx`, ASCII-safe),
  kept for 2 hours and then deleted with all temporary files.

## Presenter notes

Notes the presenter sees in PowerPoint's presenter view. They can be typed, or
written by Claude: up to 8 short points per organism, each starting with a label,
in this order, leaving out any Claude cannot fill reliably:

- **Described**: who described it and when, under what name, and when and by
  whom it was moved to another genus;
- **Type locality**: where the type specimen was collected;
- **Describer**: something noteworthy about the person who described it;
- **Name**: what the genus name and the species epithet mean;
- **Edibility**, **Ecology**, **Range** and **Story**.

Years, people and type localities are only given when Claude is sure of them.
A provisional name gets "not formally described" instead, and a genus-level
name (`Psilocybe sp.`) gets notes about the genus. Where notes come from:

- **One slide**: the **Notes** button on a Preview slide (or on a card in
  Select & Organize) opens the notes, with **Write with Claude**.
- **All slides**: **Write notes for all slides…** on the Preview step. It
  fills observations that have no notes; ticking "also rewrite" replaces the
  rest too. Notes are written once per name, so every observation of
  *Amanita muscaria* gets the same text, and progress shows per name.
- **Selected slides**: tick the box in the corner of Preview slides (it selects
  the whole observation), then **Write notes for N selected observations…**.
- **Search the web** (a checkbox in both dialogs, remembered in the browser):
  Claude checks the describer, year, original name, type locality and the
  name's meaning on the web first, preferring Index Fungorum, MycoBank, the
  original description and Wikipedia. It is slower (4 names per call, up to
  10 minutes per call) and uses more of the subscription.

Notes are plain text in the project file, editable afterwards. A refresh never
changes them. If an observation's name changes, its card and its Notes button
are flagged, and its notes stay until they are rewritten.

**How Claude is run** (`app/notes.py`): the server runs the Claude Code CLI
(`claude -p`) as a subprocess, up to 8 names per call and 2 calls at a time,
with a 240 s limit per call:

- every tool is turned off (`--tools ""`, no MCP servers), or with "Search the
  web" only Claude Code's WebSearch, which runs at Anthropic. WebFetch, which
  would download pages from this server, is never allowed. So whatever a name
  says, it can only change the text that comes back; the answer is structured
  JSON (`--json-schema`) and is cleaned before it is kept;
- our own system prompt replaces Claude Code's; it asks for well-established
  facts only, and fewer points about the genus or family when a name is obscure
  or provisional;
- the prompt holds names, ranks and common names only. Favorite counts,
  observers, places and dates are never sent;
- the subprocess gets a minimal environment of its own (its home and config
  in `/var/lib/presentations/claude/`, the token, no other variables), an empty
  working directory, and is killed on cancel or time-out.

**Access and limits.** Notes are on only when the CLI, a token and an access
code are all set. The browser asks for the code once and keeps it in local
storage; 10 wrong codes from one client lock it out for an hour. At most 1,500
names per UTC day across the site and 1,000 per job; one notes job runs at a
time (3 may wait). A usage-limit or sign-in error stops the job, keeping what
was written.

| Variable | Default |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | none (from `claude setup-token`; notes stay off without it) |
| `PRESENTATIONS_NOTES_ACCESS_CODE` | none (notes stay off without it) |
| `PRESENTATIONS_NOTES_CLI` | `/usr/local/lib/presentations/claude` |
| `PRESENTATIONS_NOTES_MODEL` | `claude-opus-5-5` |
| `PRESENTATIONS_NOTES_EFFORT` | `medium` |
| `PRESENTATIONS_NOTES_DAILY_BUDGET` | `1500` names per UTC day |
| `PRESENTATIONS_NOTES_MAX_PER_JOB` | `1000` names |
| `PRESENTATIONS_NOTES_WEB_SEARCH` | `1` (`0` hides "Search the web") |

**Setting it up**: the install script copies the code owner's Claude Code CLI
to `/usr/local/lib/presentations/claude` and creates
`/etc/presentations/notes.env` (root:presentations, 0640) once, empty. Run
`claude setup-token` as the code owner, put the token and an access code in
that file, and restart. Re-run the install script to update the CLI copy.

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
- **Size limits**: 8 MB request bodies (nginx and app, declared or streamed),
  20 sources, 10,000 observations, 200 photos per observation, 2,000 photo
  slides, 40 MB per image, 120 MP decode limit.
- **Runaway jobs**: at most 3 concurrent loads (12 running or waiting) and 2
  concurrent generations (6 running or waiting), 2 active jobs per client, a
  quarter of the daily iNaturalist budget per client, 20/60-minute timeouts
  counted from when a job starts, user cancellation (a waiting job ends at
  once), and a free-disk check before generation. Each generation reserves its
  expected download against the hourly and daily media budgets, so two decks
  started together cannot both run out halfway. A "client" is an IPv4 address
  or an IPv6 /64.
- **Restarts**: stopping the service cancels running jobs, so it exits at once.
- **Memory**: a finished load's result (every observation's metadata) is
  dropped two minutes after the browser fetched it, or after 15 minutes.
- **Paths**: workspace and job ids are random tokens validated by regex before
  touching the filesystem; download filenames are sanitized.
- **Errors**: no stack traces or filesystem paths are returned; `/docs` and
  `/openapi.json` are disabled.
- **Logs** (`journalctl -u presentations`), for fixing bugs, tracking use and
  spotting attacks, and no more:
  - one `event=request` line per request: method, route, status, time and what
    it did (the source URL tried, project size, slide counts, job id, ...);
  - failures are `event=request.failed`, adding the query string, the error the
    user saw and the User-Agent;
  - successful static files, `/healthz` and job-status polls are not logged,
    and neither are httpx's per-request lines or Uvicorn's access log, which
    would repeat the request line;
  - one `job.started` and one `job.finished` line per job, with its results
    (observations loaded, slides, bytes, iNaturalist requests used and left),
    plus `load.source` per source searched;
  - a crash is logged once, with its traceback.

  Every line carries `request_id` (also sent as the `X-Request-ID` response
  header), `client_ip` and, for jobs, `job_id`. Submitted values are quoted, so
  they cannot fake extra lines.
- **Claude**: presenter notes run the Claude Code CLI with every tool off and a
  minimal environment, behind an access code and daily limits; see
  [Presenter notes](#presenter-notes).
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

# run locally (temporary data goes to ./var). On the production server use
# another port: the live service listens on 8031 and another service on 8040.
.venv/bin/uvicorn --factory app.main:create_app --reload --port 8041
# -> http://127.0.0.1:8041/
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
  claude/                        home of the Claude Code CLI (presenter notes)
/etc/systemd/system/presentations.service
/etc/nginx/sites-available/presentations.dikarya.us.conf  (+ symlink in sites-enabled)
/etc/letsencrypt/live/presentations.dikarya.us/            (both hostnames)
/usr/local/sbin/restart-presentations                       (root-owned restart wrapper)
/etc/sudoers.d/presentations                                (lets tree run only that wrapper)
/usr/local/lib/presentations/claude                         (copy of tree's Claude Code CLI, for notes)
/etc/presentations/notes.env                                (notes token and access code, 0640)
```

The service listens only on `127.0.0.1:8031` (Dikarya uses 5000/8000, the image
service 8017, others 9000).

Logs: `journalctl -u presentations` (see [Security and limits](#security-and-limits)
for what is logged). Restart after a code change:
`sudo /usr/local/sbin/restart-presentations`, which first imports the code and
builds the app as the service user, and refuses while a presentation is being
generated (see CLAUDE.md for its exit codes).

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
- imports the code and builds the app (`create_app()`) as the service user;
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
