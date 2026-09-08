# B14 · Web-app attribution — the browser stops being one opaque blob

**Status: IMPLEMENTED** on `feat/brave-webapp-attribution` (2026-09-08). All
decisions D1–D7 taken as recommended. 524 pytest green, ruff + mypy clean. The
sections below are the design as built; §9 records the decisions.

**Branch:** `feat/brave-webapp-attribution`
**Author's context:** written 2026-09-08. The graph currently shows `app:brave-browser`
as a single node wired to `domain:habits`. That is a lie of resolution: Gmail,
Gemini, GitHub, and YouTube are not the same activity, and the operator spends
most of the day inside exactly those. This phase gives the browser sub-identity —
`webapp:gmail`, `webapp:github`, … — under a strict allowlist, so the raw window
title never crosses into cognition.

Supersedes the "per-tab tracking breaks the B13 names-only contract" objection:
the operator owns the contract and is changing it. The privacy protection moves
from "don't look" to "look, match against an allowlist, keep only the label."

---

## 1 · Goal

- The graph gains `webapp:<label>` nodes for an allowlisted set of sites.
- `webapp:gmail.access_count` == number of times Gmail was focused. Same for the rest.
- Each `webapp:` node is wired to its browser (`app:brave-browser`) **and** to its
  own routing domain (`webapp:github → domain:engineering`, not `habits`).
- Unrecognised sites collapse to `app:brave-browser`, exactly as today. Nothing new stored.
- The raw window title is confined to the sensing layer. Only the matched label
  reaches the `EventBus`, the correlator, the graph, or any log.

Non-goals (call-outs, not silent omissions):

- Background tabs. Only the focused tab is ever visible via the compositor title.
- Full URLs / browsing history. Not read. Not an extension. Title text only.
- Per-document resolution ("which Google Doc"). The label is `google-docs`, full stop.
- Firefox / Chrome support in round 1 — the matcher is browser-agnostic by design
  (§6) but only `brave-browser` is validated here.

---

## 2 · What exists today (the pipeline, verbatim)

```
compositor
  │  ext_foreign_toplevel_list_v1  (app_id, title per toplevel)
  │  zcosmic_toplevel_info_v1      (which toplevel is `activated`)
  ▼
WaylandWindowSource._recompute_focus()          src/neuropaca/sensing/activity/window.py:161
  │  fires on_switch(WindowInfo(app_id, title))
  │  ── ONLY when app_id != self._focused_app_id  ← gap #1
  ▼
ActivityCollector._on_window_switch(window)      src/neuropaca/sensing/activity/collector.py:139
  │  dedup: `if window.app_id == self._focused_app_id: return`  ← gap #2
  │  publishes APP_SWITCH  {app_id, title, previous_app_id}
  ▼
SignalCorrelator.on_app_switch(event)            src/neuropaca/diagnosis/correlator.py:122
  │  domain = AppMap.classify(app_id)            src/neuropaca/diagnosis/app_map.py:116
  │  _classify_into_graph(app_id, domain):
  │      upsert_node(f"app:{app_id}", APP, {label})           correlator.py:180
  │      add_edge(app_node, domain, PART_OF)   (once, _known_apps guard)  correlator.py:182
  │  builds synthetic MetricSnapshot(collector="activity",
  │      data={app_id, previous_app_id, title, domain})       correlator.py:130
  │  _ingest(snapshot) → runs activity patterns → maybe writes nodes
  ▼
graph.json   (scheduler.save() periodically + once at clean shutdown)
```

Facts that shape the design:

| Fact | Source | Consequence for B14 |
|---|---|---|
| `_recompute_focus` fires only on `app_id` change | `window.py:164` | Gmail→Gemini tab (both `brave-browser`) produces **zero events** |
| `_on_window_switch` re-dedups on `app_id` | `collector.py:140` | second guard, same effect |
| `title` is in the `APP_SWITCH` payload but **no pattern reads it** | grep: patterns read `app_id`, `domain` only | we can stop shipping the raw title with no loss |
| `upsert_node` bumps `access_count` every call | `graph_memory.py:660` | the focus counter already exists — just point it at `webapp:` ids |
| `_known_apps` guards the one-time domain edge | `correlator.py:70,181` | need a parallel `_known_webapps` |
| Brave's Wayland `app_id` is `brave-browser` | live probe 2026-09-08 | matcher keys off this to know "this is a browser" |
| Brave title format: `<page title> - Brave` | live probe: `Inbox (351) - bhanot1054@gmail.com - Gmail - Brave` | title **leaks the email address + unread count** → must never persist raw |
| `AppMap` values are full `domain:<slug>` ids, validated against `DOMAIN_SLUGS` at load | `app_map.py:49,67` | the webapp map reuses this exact validation |
| Node schema is at v3; enum/type additions bump it | `graph_memory.py:47`, `enums.py:106` | new `NodeType.WEBAPP` ⇒ v4, forward-incompatible |

---

## 3 · The two problems

1. **No event fires on a tab switch.** Both the Wayland source and the collector
   dedup on `app_id`, and a tab switch keeps `app_id == "brave-browser"`.

2. **Even if it fired, there's nowhere for the identity to land.** `AppMap` maps
   `app_id → domain`. It has no notion of "the same app_id means different things
   depending on the title."

---

## 4 · Design

### 4.1 The privacy boundary is the collector

```
 ┌─────────────── sensing/activity ───────────────┐
 │  raw title lives here and ONLY here            │
 │                                                │
 │  window.py   →  collector.py                   │
 │                   │  derive_webapp(app_id, title) → "gmail" | None
 │                   │  raw title dropped from the payload
 │                   ▼                            │
 └──────────  APP_SWITCH {app_id, webapp, previous_app_id, previous_webapp}  ──────────┐
                                                                                       ▼
                                                              correlator, graph, logs, CSV
                                                              — see only the allowlisted label
```

`derive_webapp()` is a **pure function** in a new module
`src/neuropaca/sensing/activity/webapp.py`. It:

1. Returns `None` immediately if `app_id` is not in the configured browser set
   (`{"brave-browser"}` in round 1). Non-browsers are untouched.
2. Strips a known browser suffix from the title (` - Brave`, ` — Mozilla Firefox`,
   ` - Chromium`, ` - Google Chrome`).
3. Splits the remainder on the common title delimiters (` - `, ` — `, ` · `, ` | `)
   and lowercases each segment.
4. Returns the first segment that is a key in the loaded **WebAppMap**, else `None`.
5. Never returns, logs, or retains any substring of the title that isn't the
   matched key.

This is deliberately not a regex engine. The allowlist is a set of site-name
tokens (`gmail`, `youtube`, `github`, `google gemini`, …); matching is exact
set membership against delimiter-split segments. A regex fallback for sites whose
title doesn't segment cleanly (see §6, GitHub) is a documented follow-up, not
round 1.

### 4.2 Why the collector, not the correlator

- The correlator is where classification (`AppMap`) lives, so it's the tempting home.
- But the correlator receiving the raw title means the raw title is on the bus,
  in every `Event` object, and one careless `_log.debug("%r", event)` leaks it.
  Keeping it in the collector makes the leak *structurally* impossible, the same
  way B13 made "census reads a cmdline" structurally impossible.
- The dedup logic (§4.3) needs `derive_webapp` anyway to decide whether a title
  change is a *meaningful* focus change. That logic belongs with the source.

### 4.3 Dedup: the "focus key"

Replace the `app_id` equality guard, in both `window.py` and `collector.py`, with
a **focus key** = `(app_id, derive_webapp(app_id, title))`.

- Gmail "Inbox (351)" and "Inbox (352)" → both `("brave-browser", "gmail")` → no event.
- Gmail → Gemini → `("brave-browser","gmail")` ≠ `("brave-browser","gemini")` → one event.
- A YouTube video's title ticking the timestamp → `("brave-browser","youtube")` stable → no event.
- Any non-browser app → webapp is `None` → key is `(app_id, None)` → behaves exactly as today.

`window.py` needs `derive_webapp` too (to compute the key). Options:
- **(a)** pass the browser set + WebAppMap down into `WaylandWindowSource`. Adds config to a thin transport.
- **(b)** `window.py` fires on **every `(app_id, title)` change**; `collector.py`
  does the focus-key dedup. `window.py` stays dumb; the collector, which already
  holds `config`, owns the matcher.

**Recommend (b).** `window.py` becomes a faithful "focus state changed" transport;
all policy is in the collector. Cost: `window.py` calls the callback more often
(every Gmail unread-count tick). That callback is a dict comparison + early return
in the collector — cheap, and title churn on the *focused* window is human-paced,
not a firehose.

### 4.4 Graph shape

```
                 domain:engineering            domain:comms          domain:habits
                        ▲                            ▲                     ▲
                        │ PART_OF                    │ PART_OF             │ PART_OF
                   webapp:github                webapp:gmail         app:brave-browser
                        │                            │                     ▲
                        └────── PART_OF ─────────────┴──── PART_OF ────────┘
                                                    (webapp → its browser)
```

- `app:brave-browser` — unchanged. Still `PART_OF domain:habits` (its "resting"
  classification when no webapp is identified).
- `webapp:<label>` — new. `NodeType.WEBAPP`. Two `PART_OF` edges:
  - `→ app:brave-browser` (structural: it's a thing inside the browser)
  - `→ domain:<slug>` (semantic: its own routing domain, from the WebAppMap)
- Both edges written once, guarded by `_known_webapps` (parallel to `_known_apps`).
- `access_count` on `webapp:gmail` is the focus counter, for free, via `upsert_node`.

A `webapp:` node wired to two different `domain:*` hubs (its own + inherited)
would trip the cross-domain "bridge" score (`graph_memory.py:858`). It is wired
to exactly one `domain:*` (its own) plus one `app:*`, so it does not bridge.
Good — a webapp is not a bridge concept.

### 4.5 Schema version

New `NodeType.WEBAPP` ⇒ bump `_SCHEMA_VERSION` 3 → 4 (`graph_memory.py:47`).
`_MIN_READABLE_SCHEMA_VERSION` stays 1 (v3 graphs load fine — they just have no
`webapp:` nodes). A v4 graph will **not** load on v3 code — acceptable, single-user,
documented in the schema-version comment block.

Alternative considered: reuse `NodeType.APP` with the `webapp:` id prefix, no enum
change, no schema bump. Rejected — `webapp` is a real, distinct kind of node and
the project's schema discipline (v2, v3 each earned a bump for less) says model it
honestly. Noted as a rejected alternative for the paper.

---

## 5 · Config additions (`src/neuropaca/core/config.py`)

```python
# B14 · web-app attribution. When the focused window belongs to one of
# `webapp_browser_app_ids`, the collector matches the title against
# `webapp_map_path` and emits the matched label (e.g. "gmail") instead of
# treating the whole browser as one node. The raw title never leaves the
# collector. Empty browser set or missing map => feature is inert, browser
# stays one `app:` node (B13 behaviour).
webapp_tracking_enabled: bool = True
webapp_map_path: str = "data/webapp_map.default.toml"
webapp_browser_app_ids: tuple[str, ...] = ("brave-browser",)
```

- `webapp_tracking_enabled = False` → `derive_webapp` short-circuits to `None`
  everywhere. One-line kill switch, matching `activity_enabled` / `agents_enabled`.
- Added to the `_INT_FIELDS` / validation lists as appropriate (string path gets
  the same "file may not exist yet, warn don't raise" treatment as `app_map_path`).
- `neuropaca.toml`, `neuropaca.soak.toml`, `neuropaca.b13.toml` gain the three keys
  (documented, defaulted on for `.toml` and `.b13.toml`, **off** for the pure-soak
  `.soak.toml` unless we want the soak to exercise it — see §11 open question).

---

## 6 · The WebAppMap file (`data/webapp_map.default.toml`)

Same contract as `app_map.default.toml`: user-editable, read once at
`SignalCorrelator.initialize()` — **wait**, the matcher runs in the collector, so
this is read at `ActivityCollector.start()` instead. Domains validated against
`DOMAIN_SLUGS`; unknown domain or malformed row → warn + skip, never raise.

```toml
# webapp_map.default.toml — focused browser tab -> (webapp label, routing domain).
#
# The collector strips the browser suffix from the window title, splits on
# " - " / " — " / " · " / " | ", lowercases each segment, and looks for the
# FIRST segment that is a key below. The key is what gets stored as
# `webapp:<key>`. Everything else in the title is discarded and never leaves
# the sensing layer.
#
# domain MUST be one of the 10 routing domains:
#   engineering  research  tools  system  habits
#   projects     meetings  comms  mental_models  learning

[webapp]
"gmail"          = "comms"
"google gemini"  = "tools"
"gemini"         = "tools"
"youtube"        = "habits"
"github"         = "engineering"
"stack overflow" = "engineering"
"google docs"    = "projects"
"google sheets"  = "projects"
"notion"         = "projects"
"reddit"         = "habits"
"chatgpt"        = "tools"
"claude"         = "tools"
"linear"         = "projects"
"figma"          = "projects"
```

**These are starting guesses.** Like `app_map`'s Brave→habits call, the operator
tunes them on real usage. The full list is a dogfood output, not a spec.

Known awkward cases, recorded now:

| Site | Real title (approx) | Segments after suffix strip | Matches? |
|---|---|---|---|
| Gmail | `Inbox (351) - x@gmail.com - Gmail` | `inbox (351)` / `x@gmail.com` / `gmail` | ✅ `gmail` |
| YouTube | `<video> - YouTube` | `<video>` / `youtube` | ✅ `youtube` |
| Google Gemini | `Gemini` or `<chat> - Google Gemini` | … / `google gemini` | ✅ (both keys present) |
| GitHub | `owner/repo: description · GitHub` OR `Issue title · owner/repo` | segments vary; `github` sometimes absent | ⚠️ partial — needs the regex fallback follow-up |
| Google Docs | `<doc name> - Google Docs` | `<doc name>` / `google docs` | ✅ but **doc name is in a discarded segment** — fine, we only keep `google docs` |

The GitHub gap is explicitly out of scope for round 1; the delimiter matcher
catches the common `... · GitHub` form and misses issue/PR pages. Round 2:
optional per-key `pattern = "..."` regex in the TOML row.

---

## 7 · Component-by-component changes

### 7.1 `src/neuropaca/sensing/activity/webapp.py` — NEW

```python
"""B14 · derive an allowlisted web-app label from a focused window title.

Pure, synchronous, no I/O beyond the one-shot TOML load in `WebAppMap.from_file`.
The raw title is an argument and a local — it is never returned, logged, or stored.
"""
```

- `WebAppMap` — mirror of `AppMap`: `from_file` / `from_dict` / `empty`,
  `DOMAIN_SLUGS` validation, `rule_count`, `.domain_for(label) -> "domain:<slug>" | None`.
- `_BROWSER_SUFFIXES: tuple[str, ...]` — `(" - Brave", " — Mozilla Firefox", …)`.
- `_DELIMITERS` — `(" - ", " — ", " · ", " | ")`.
- `derive_webapp(app_id, title, *, browsers, webapp_map, enabled) -> str | None`
  — the function from §4.1. ~15 lines.
- 100% unit-testable with literal title strings; no Wayland, no daemon.

### 7.2 `src/neuropaca/sensing/activity/window.py`

- `_Toplevel` already carries `title`. Add `self._focused_title: str = ""` alongside
  `self._focused_app_id`.
- `_recompute_focus`: fire the callback when **`(app_id, title)`** differs from
  `(self._focused_app_id, self._focused_title)`, not just `app_id`. Update both.
- `WindowInfo` unchanged (already `{app_id, title}`).
- `FakeWindowSource.emit` unchanged — tests can already drive title.

~6 line change. Risk: title-churn wakeups; mitigated in the collector.

### 7.3 `src/neuropaca/sensing/activity/collector.py`

- `ActivityCollector.__init__`: build the matcher context from `config` —
  `self._webapp_map`, `self._browsers`, `self._webapp_enabled`. Load the map at
  `start()` (like the Wayland sources start there), warn-not-raise on a bad file.
- Track `self._focus_key: tuple[str, str | None] | None` instead of
  `self._focused_app_id`.
- `_on_window_switch(window)`:
  ```python
  webapp = derive_webapp(window.app_id, window.title,
                         browsers=self._browsers, webapp_map=self._webapp_map,
                         enabled=self._webapp_enabled)
  key = (window.app_id, webapp)
  if key == self._focus_key:
      return
  previous_app_id, previous_webapp = self._focus_key or (None, None)
  self._focus_key = key
  self._switches += 1
  self.event_bus.publish(Event(
      event_type=EventType.APP_SWITCH,
      source="sensing.activity",
      payload={"app_id": window.app_id, "webapp": webapp,
               "previous_app_id": previous_app_id, "previous_webapp": previous_webapp},
  ))
  ```
- **`title` removed from the payload.** This is the privacy boundary. (grep first
  to confirm no consumer reads `payload["title"]` — current grep says none.)
- `health()` detail can gain `· N tabs` (distinct webapps seen) if cheap.

### 7.4 `src/neuropaca/core/enums.py`

- `EventType.APP_SWITCH` comment → `payload {app_id, webapp, previous_app_id, previous_webapp}`.
- `NodeType.WEBAPP = auto()` — with a comment: `webapp:<label>`; `PART_OF` its
  browser `app:` node and `PART_OF` its routing domain; `access_count` is the
  focus count. Schema v4.

### 7.5 `src/neuropaca/core/graph_memory.py`

- `_SCHEMA_VERSION = 4`; extend the version-history comment block.
- No other change — `webapp:` ids are just nodes; `NodeType(raw)` will accept the
  new value once the enum has it. Confirm the load path does `NodeType(s)` and
  that a v4 write / v3 read fails cleanly with the existing "refusing to load a
  newer schema" branch (`graph_memory.py:895`).

### 7.6 `src/neuropaca/diagnosis/correlator.py`

- `__init__`: `self._known_webapps: set[str] = set()`.
- `on_app_switch(event)`:
  ```python
  app_id  = event.payload.get("app_id")
  webapp  = event.payload.get("webapp")   # str | None
  if not isinstance(app_id, str) or not app_id:
      return
  app_domain = self._app_map.classify(app_id) or ""
  if app_domain:
      await self._classify_into_graph(app_id, app_domain)   # unchanged path
  if isinstance(webapp, str) and webapp:
      await self._classify_webapp_into_graph(webapp, app_id)
  # synthetic snapshot — domain is the WEBAPP's domain when known, else the app's
  effective_domain = (self._webapp_map.domain_for(webapp) if webapp else "") or app_domain
  snapshot = MetricSnapshot(collector_name="activity", timestamp=event.timestamp, data={
      "app_id": app_id,
      "webapp": webapp,
      "previous_app_id": event.payload.get("previous_app_id"),
      "domain": effective_domain,
  })
  await self._ingest(snapshot)
  ```
  Note: `title` no longer in the snapshot `data`.
- New `_classify_webapp_into_graph(webapp, app_id)`:
  ```python
  node_id = f"webapp:{webapp}"
  await self._graph.upsert_node(node_id, NodeType.WEBAPP, {"label": webapp})
  if webapp not in self._known_webapps:
      await self._graph.add_edge(node_id, f"app:{app_id}", RelationType.PART_OF)
      dom = self._webapp_map.domain_for(webapp)
      if dom:
          await self._graph.add_edge(node_id, dom, RelationType.PART_OF)
      self._known_webapps.add(webapp)
  ```
- The correlator needs the WebAppMap too (for `domain_for`). It already reads
  `app_map_path` at `initialize()`; add `self._webapp_map = WebAppMap.from_file(
  config.webapp_map_path)` there. Two loads of the same file (collector +
  correlator) is fine — it's tiny and read-once. Alternative: correlator derives
  the domain from a `webapp_domain` field the collector puts in the payload, so
  only the collector loads the map. **Recommend the payload field** —
  `payload["webapp_domain"]` — one loader, and the correlator stays out of the
  matcher's business. Revised payload: `{app_id, webapp, webapp_domain,
  previous_app_id, previous_webapp}`.

### 7.7 `src/neuropaca/diagnosis/patterns.py` — the behaviour decisions

These are real behaviour changes. Each needs an explicit yes/no (see §9).

- **`FocusSessionPattern`** (`patterns.py:326`) keys on `_str_field(current, "domain")`.
  With §7.6, `domain` in the activity snapshot becomes the *webapp's* domain when
  a webapp is focused. So **20 minutes in GitHub tabs now fires a FOCUS_SESSION**
  (`webapp:github → engineering ∈ _FOCUS_DOMAINS`), where before "brave = habits"
  never did. The node it attributes is currently `app:{app_id}` (`patterns.py:389`)
  → change to `webapp:{webapp}` when present, else `app:{app_id}`.
- **`DistractionPattern`** (`patterns.py:426`) counts `APP_SWITCH` events and
  distinct `app_id`s. Two effects:
  - More events now fire (tab switches within Brave). Rapid Gmail↔Slack-web↔Gemini
    hopping now counts as distraction. Arguably correct.
  - `distinct` should key on `webapp or app_id` so the thrashed-node specs are
    `webapp:` nodes when known.
- **`IdlePattern._last_app_spec`** (`patterns.py:318`) → attribute the pre-idle
  focus to `webapp:` when known.
- `_str_field` helper handles `None` fine (returns `""`), so `webapp=None` snapshots
  are safe everywhere.

### 7.8 `scripts/b9_soak_state.py` / dashboard

- The soak "Census" line and dashboard can gain a "Top web-apps by focus count"
  section, read from `webapp:*` node `access_count`. Nice-to-have, not blocking.

---

## 8 · Privacy hardening & the leak tests

The whole phase is only acceptable if the raw title is provably contained.

1. **Payload test** — `test_app_switch_payload_has_no_raw_title`: drive
   `FakeWindowSource.emit("brave-browser", "Inbox (351) - x@gmail.com - Gmail - Brave")`,
   assert the published `APP_SWITCH` payload contains `webapp == "gmail"` and **no
   value in the payload contains `"Inbox"`, `"@gmail.com"`, or `"351"`**.
2. **Graph grep test** — `test_graph_json_after_webapp_soak_has_no_title_text`:
   run the collector+correlator against a scripted sequence of realistic titles
   for ~1 min, `save()`, read `graph.json` as text, assert none of the
   PII-ish fragments (`@`, `Inbox`, `(351)`, the doc names) appear. Only
   `webapp:gmail`, `webapp:google-docs`, labels.
3. **No-log test** — assert `derive_webapp` and `_on_window_switch` emit no log
   record containing the raw title (caplog).
4. **Unmatched-site test** — `emit("brave-browser", "Some Bank - Account Summary - Brave")`
   → `webapp is None`, payload identical shape to a non-browser switch, nothing
   `webapp:` written to the graph.
5. **Kill-switch test** — `webapp_tracking_enabled = False` → `derive_webapp`
   always `None`, zero `webapp:` nodes, `APP_SWITCH` behaves exactly as B13.
6. `raw_metrics.csv` unaffected — `RawMetricsRecorder` subscribes to
   `METRIC_COLLECTED` only (`raw_recorder.py:75`), never `APP_SWITCH`. Add a
   regression assert anyway.

---

## 9 · Decisions the operator needs to make

| # | Decision | Recommendation |
|---|---|---|
| D1 | New `NodeType.WEBAPP` + schema v4, or reuse `NodeType.APP` with `webapp:` prefix (no bump)? | **New type + v4.** Matches project schema discipline. |
| D2 | Does a focused webapp's domain **override** `brave = habits` for `FocusSessionPattern`? (i.e. 20 min in GitHub = a focus session) | **Yes.** That's the entire point — the browser is no longer one blob. |
| D3 | Do tab switches inside Brave feed `DistractionPattern`? | **Yes**, with `distinct` keyed on `webapp or app_id`. Gmail↔Reddit↔YouTube hopping *is* distraction. |
| D4 | `webapp_map` domain for Gemini / ChatGPT / Claude — `tools`, `research`, or `learning`? | Start `tools`; operator retunes. |
| D5 | Should `neuropaca.soak.toml` (pure soak) enable webapp tracking, or only `.toml` / `.b13.toml`? | Enable in **all three** — the soak should exercise the new path, and it's the only way to get the §8.2 real-title grep test data at scale. |
| D6 | Round-1 allowlist contents — is §6's list right for *your* day? | Operator edits before merge. |
| D7 | `webapp:` label for multi-word sites — `google-docs` (slugified) or `google docs` (raw key)? | **Slugify** to `webapp:google-docs` for id hygiene; keep `label = "Google Docs"`. |

---

## 10 · Test plan

| Layer | File | Cases |
|---|---|---|
| `derive_webapp` | `tests/test_webapp_derive.py` (new) | each allowlisted site's real title → label; browser suffix variants; non-browser app_id → None; disabled → None; unmatched → None; delimiter variants; title with no delimiter |
| `WebAppMap` | `tests/test_webapp_map.py` (new) | load default file; unknown domain skipped+warned; missing file → empty+warn; `domain_for` |
| `window.py` | `tests/test_activity.py` (extend) | fires on title-only change; `(app_id,title)` dedup |
| `collector.py` | `tests/test_activity.py` (extend) | focus-key dedup; payload shape; no raw title; switch count; map load at start |
| `correlator` | `tests/test_diagnosis.py` (extend) | `webapp:` node + both `PART_OF` edges written once; `_known_webapps` guard; `webapp=None` path == B13; synthetic snapshot `domain` = webapp domain |
| patterns | `tests/test_diagnosis_activity_patterns.py` (extend) | FocusSession fires for `webapp:github`; Distraction counts tab switches; nodes attributed to `webapp:` |
| privacy | `tests/test_webapp_privacy.py` (new) | §8.1–8.5 |
| schema | `tests/test_core_foundation.py` (extend) | v4 round-trips; v3 file still loads; v4 file refused on a forced-v3 reader |

Full suite green, `ruff check .` (bare, no `--fix` — see the RUF002/E501 memory), `mypy` clean.

---

## 11 · Rejected alternatives (paper deliverable)

| Alternative | Why rejected |
|---|---|
| Read Brave's `History` SQLite / session store for real URLs | Deep inspection of browser internals; a whole new trust surface; the title already gives focused-tab identity at near-zero cost |
| Browser extension with `tabs` permission | New component, new packaging, new permission prompt, and it can see background tabs — scope creep beyond "what am I looking at" |
| Regex-parse the full title into structured fields | Titles are localised, unstable, and PII-bearing; an allowlist of site tokens keeps the blast radius to a fixed vocabulary |
| Store the raw title on the node, filter at query time | The raw title would be on disk in `graph.json`; filtering-at-read is a policy that can be forgotten, containment-at-write cannot |
| Do the matching in the correlator | Puts the raw title on the `EventBus`; one stray debug log leaks it |
| Reuse `NodeType.APP`, no schema bump | `webapp` is a genuinely distinct node kind; the project bumped schema for less |
| Fire `APP_SWITCH` on every `(app_id,title)` change, dedup downstream | Firehose of Gmail-unread-count ticks onto the bus; the focus-key collapses them at the source |

---

## 12 · Phasing

- **B14-A** — `webapp.py` (`derive_webapp` + `WebAppMap`), `webapp_map.default.toml`,
  config keys, full unit tests. No pipeline wiring yet. *Mergeable alone.*
- **B14-B** — wire `window.py` + `collector.py`; `APP_SWITCH` payload change;
  privacy tests. Feature visible on the bus, not yet in the graph.
- **B14-C** — correlator `webapp:` nodes + edges, schema v4, pattern decisions
  (D2/D3). Graph now carries web-apps.
- **B14-D** — soak/dashboard "top web-apps" surface; re-tune `webapp_map` from a
  short dogfood run; docs (`Architecture.md` §3.6 node types, `phases.md` B14
  entry, `RESEARCH_DOSSIER.md` sensing table).

## 13 · Exit criteria

1. Focusing Gmail then Gemini then Gmail leaves `webapp:gmail.access_count == 2`,
   `webapp:gemini.access_count == 1` in `graph.json`.
2. `webapp:github` is wired `PART_OF domain:engineering` and `PART_OF app:brave-browser`.
3. 20 min in GitHub tabs produces one `FOCUS_SESSION` signal attributed to
   `webapp:github` (D2 = yes).
4. `graph.json` after a 1-hour real-use window contains **no** email address, no
   unread count, no document name — only allowlisted labels. (grep test, §8.2, run for real.)
5. `webapp_tracking_enabled = False` reproduces B13 `APP_SWITCH` behaviour exactly
   (byte-identical payload shape minus the new keys).
6. Full suite + `ruff check .` + `mypy` green.

---

## 14 · Files touched (summary)

```
NEW  src/neuropaca/sensing/activity/webapp.py
NEW  data/webapp_map.default.toml
NEW  tests/test_webapp_derive.py
NEW  tests/test_webapp_map.py
NEW  tests/test_webapp_privacy.py
EDIT src/neuropaca/sensing/activity/window.py        (~6 lines: title in focus key)
EDIT src/neuropaca/sensing/activity/collector.py     (matcher, focus key, payload)
EDIT src/neuropaca/diagnosis/correlator.py           (webapp node/edge path)
EDIT src/neuropaca/diagnosis/patterns.py             (D2/D3: FocusSession, Distraction, Idle attribution)
EDIT src/neuropaca/core/enums.py                     (NodeType.WEBAPP, payload comment)
EDIT src/neuropaca/core/graph_memory.py              (_SCHEMA_VERSION = 4 + comment)
EDIT src/neuropaca/core/config.py                    (3 keys + validation)
EDIT neuropaca.toml, neuropaca.soak.toml, neuropaca.b13.toml   (3 keys)
EDIT tests/test_activity.py, test_diagnosis.py,
     test_diagnosis_activity_patterns.py, test_core_foundation.py
EDIT Architecture.md, phases.md, RESEARCH_DOSSIER.md (B14-D)
```
