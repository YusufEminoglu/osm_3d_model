# 3D OSM Model 1.10.0 — development set

Written 2026-11-02 by the master engineer from two independent audits of 1.9.1
(O3-CODE: download and build; O3-UX: dialog, truth and demand), both run in
QGIS 3.44.12 and 4.2.0 against fake servers on 127.0.0.1 with zero public
requests. Evidence: the audit folders under the session scratchpad
(`osm3d/o3-code`, `osm3d/o3-ux`).

1.9.1 is the family's most downloaded plugin (28,187 Hub downloads since
2026-06-01). The build itself is sound: the geometry matched an independent UTM
computation, the local server binds 127.0.0.1 only, and the viewer loads
nothing from a CDN. What hurts users is everything around the build: how it
asks the Overpass servers, what it says while it waits and when it fails, where
it keeps the user's work, and how it treats a first-time user on an empty
project.

## 1. Problems this release fixes (verified on both runtimes unless marked)

| # | Harm | Evidence |
|---|------|----------|
| P1 | Every HTTP error (429, 406, 400, 403, 502, 504) is retried on the same host 1.2 s later: 6 requests in 3.7 s per click. The 2026 rule is to wait at least 30 s after 429/406, and apps with many 429s get banned. | `osm_download.py:260-275`: QgsBlockingNetworkRequest reports 4xx/5xx as an error, so the status branch never runs. |
| P2 | A dropped connection costs 24 requests per click on 3.44 (Qt 5 re-sends the POST 3 times, times the plugin's own retry, times 3 hosts), 6 on 4.2. | fake server that closes without answering |
| P3 | `[timeout:60]` with no `[maxsize]`; QGIS aborts at 60 s and reports it as an empty success, so the user reads "non-JSON response (0 bytes): Expecting value". All hosts hanging: 180 s of one unchanged line. | `:65`, `:189`, `:247`, `:281` |
| P4 | Cancel does nothing during a request (the task ends 52-58 s later, in a red Critical box). Esc and close are ignored during a run; the toolbar icon does nothing. | `post(..., None)` `:257`; `dialog.py:548-558`; `main_plugin.py:152` |
| P5 | The failure message keeps the last three failures only, drops the first host, repeats the last, shows raw Qt strings and always advises "pick a smaller area". No Referer; QGIS replaces the User-Agent. | `failures[-3:]` `:368` |
| P6 | An empty answer is cached for a week; the next run sends nothing and blames the area. A cache-served run still says "Downloading…". | `:285-297`, `:357`; `main_plugin.py:218` |
| P7 | On an empty project, "Add OSM basemap" zooms to the world and Run builds a 150 ha circle in the open sea at 3°E 0°N. The area readout measures the map view (≈77 ha), not the model (30.6 ha). | `main_plugin.py:441-460`, `dialog.py:583-591` |
| P8 | The user's work is not kept: the layers are memory layers (0 features after save and reopen); exports go into the plugin's own folder, are overwritten on every run, and a plugin update wipes them; "Open data folder" opens the install folder; re-export fails with WinError 5 when a layer file is open. | `builder.py:396-411`, `:762` |
| P9 | Partial results read as complete: a failed DEM or basemap shows for 0.2 s and then "✓ Export ready". With unreachable tiles the basemap blocks QGIS for 65 s (605 tile requests) and publishes a fully transparent PNG. | `builder.py:485`, `:535`, `:551` |
| P10 | Model fidelity: a multipolygon island inside a hole is lost; `roof:levels` are added to the wall height and the viewer adds a roof on top of OSM `height` (which already includes the roof), so tagged buildings are too tall; `highway=proposed` and tunnels are drawn as surface roads; 63 of 64 "OSM trees" are planted by the plugin; 2.5 levels round to 2; `min_height` is ignored; open building ways are closed into polygons; one absurd `building:levels` value sinks the whole export. | `osm_download.py:446-448`, `:648`, `:767-774`, `:831`, `:1129`; `app.js:5107-5111, 6820` (READ) |
| P11 | Bug reports go nowhere: `tracker=` (GitLab) is a 404. GitHub issues work and have never received one. | metadata.txt |
| P12 | Legibility: Run is white on #b8978a (2.68:1); under Night Mapping the spin-box value is ≤ 2.1:1 and the Advanced toggle 1.53:1; at a 1366×728 work area with Advanced open, Run sits below the fold. | `dialog.py:738` |
| P13 | Server: no Host check (a DNS-rebinding page could read the basemap and DEM), directory listing on; all 20 ports busy gives an error box after a successful build. PNG, JPEG, PDF and video exports carry no OSM attribution (READ). | `server.py`, `main_plugin.py:287`, `export_utils.js` |
| P14 | Truth: metadata promises PDF, SVG and HTML exports that are raster snapshots; README facade text stale since 1.9.0, 15 "ecosystem" links point to this repo, `docs/index.html` redirects to a file that does not exist, Quick start shows a personal `C:\Users\YE` path; jargon in the dialog ("canvas extent", "Overpass mirrors", "EPSG:32635", "PlanX 3D City engine"). | README:139, :298-308 |
| P15 | Tests are not honest: 6 of 7 harmful mutations pass both suites; the resilience suite asserts the 1.2 s retry after a 429 as correct and stubs away the real status handling. | O3-CODE mutation run |

Demand (O3-UX research, last 18 months): export to Blender/SketchUp/Rhino/3D
printing is the most requested missing feature (competing web tools all export
GLB/OBJ/STL); start from a place name; clear errors under the 2026 Overpass
rules; honest heights. Sun, shadows and fly-through video are already a
strength. Hub search words missing: threejs, 3d viewer, 3d visualization,
extrusion.

## 2. What 1.10.0 delivers

1. **A polite, honest Overpass client** (P1-P6, P15), ported from 02AgentOSM
   1.6.0 (`core/transport.py`, `core/mirrors.py`, `_post_once`), which was
   proven against the same 2026 rules: the HTTP status is read even when QGIS
   reports an error; hosts rest after a failure (429 → 60 s, 403/406 → 1 h,
   5xx → 45 s, timeout → 120 s, doubling, capped) and the rests and the last
   host that worked are remembered across QGIS sessions; at most one request
   per host per click (a re-ask after a dropped connection only on Qt 6, after
   5 s); `[timeout:N][maxsize:134217728]` matched to QGIS's own network timeout
   (transfer ≤ 55 s); the transfer is cancelled at once by Cancel or when the
   answer passes 128 MiB; Referer plus the User-Agent suffix where QGIS allows;
   empty or remark answers never cached; one failure message naming every host
   asked and what it answered, with advice that fits the cause.
2. **A wait the user can read and stop** (P4): a status line naming the host
   and the elapsed seconds, a Cancel button that works within 2 s, Esc/close
   hide the dialog while the build continues, and the toolbar icon always
   brings it back.
3. **Start from a place** (P7): a place search (QGIS's own Nominatim geocoder),
   the area readout reports the model's boundary, the boundary is drawn on the
   map before Run, and a world-scale view is refused with a hint instead of
   building open sea.
4. **The user's work is kept** (P8): every run gets its own folder under the
   QGIS profile; the layers are written to a GeoPackage there and load from it,
   so they survive saving the project and updating the plugin; "Open data
   folder" opens that run; "Reopen last viewer" opens the last run (or says
   "Open sample city" before the first run); "Delete old runs" is the user's
   choice, never automatic.
5. **Partial results say so** (P9): DEM and basemap problems stay in the final
   summary; the basemap renders under a 20 s deadline without freezing QGIS
   and a blank result is not published; a cache-served run says how old the
   copy is and offers a fresh download; the summary separates OSM trees from
   planted ones and says how many building heights are estimated.
6. **A more faithful model** (P10).
7. **GLB export from the viewer** (demand #1): one button saves the static
   city as a binary glTF in metres, for Blender, SketchUp, Rhino or a slicer.
8. **Legible, honest, reachable** (P11-P14): palette-based contrast that holds
   in Night Mapping and Blend of Gray, Run always visible at 1366×728, Host
   check and no directory listing, OSM attribution on every raster export,
   honest metadata and README, `tracker=` on GitHub issues, Hub tags.

Out of scope for 1.10.0: OBJ/STL export, terrain fetched automatically,
areas above 300 ha, the hosted manual on geophilo.com (lives in the portal
repository; a follow-up), Postpass fallback.

## 3. Contracts between the implementers

### C1 — download (O3-NET provides, O3-UX consumes)

```python
# osm_download.py
class OsmDownloadError(RuntimeError): ...        # str(e) is the final user text
class OsmDownloadCancelled(OsmDownloadError): ...  # str(e) == "Download cancelled."

@dataclass(frozen=True)
class FetchOutcome:
    payload: dict              # validated Overpass JSON
    from_cache: bool
    saved_at: Optional[float]  # epoch seconds the answer was downloaded
    host: str                  # host that answered; "" when from_cache

def fetch_overpass_ex(min_lat, min_lon, max_lat, max_lon, *, use_cache=True,
                      status=None, feedback=None) -> FetchOutcome: ...
```

- `status(text)` is called on the worker thread with one short plain sentence
  per step ("Asking overpass-api.de…", "overpass-api.de is busy (HTTP 429), so
  it rests for a minute. Asking overpass.private.coffee…", "Using the copy
  downloaded 3 hours ago; no request sent."). It never contains elapsed
  seconds; the dialog adds those.
- `feedback` is a `QgsFeedback`; cancelling it aborts the transfer at once and
  `fetch_overpass_ex` raises `OsmDownloadCancelled` within 2 s.
- `fetch_overpass(...)` keeps its 1.9.1 signature and returns the payload.
- `OVERPASS_ENDPOINTS` stays a module-level tuple read at call time (the test
  guard replaces it).

### C2 — runs and build (O3-OUT provides, O3-UX consumes)

```python
# builder.py
def runs_root() -> Path            # <QGIS profile>/osm_3d_model/runs, created on demand
def new_run_dir(label: str = "") -> Path   # runs_root()/YYYYmmdd-HHMMSS[-label], unique, created
def latest_run_dir() -> Optional[Path]     # newest run with a valid data/planx_manifest.json
def delete_runs_except(keep: Optional[Path]) -> int   # number of run folders removed
def build_and_export(..., run_dir: Optional[Path] = None) -> dict   # None → new_run_dir()
```

The summary dict keeps every 1.9.1 key and adds `run_dir` (str), `gpkg_path`
(str), `warnings` (list of plain sentences, e.g. "The DEM was skipped: it does
not overlap the area."), `buildings`, `estimated_heights` (buildings whose
height came from the default for their type), `osm_trees`, `planted_trees`.

```python
# server.py
class ServerUnavailable(RuntimeError): ...   # str(e) is user text
Osm3dServer.set_data_dir(path: Optional[Path])  # URL /data/... → path/data/...;
                                                # None → the bundled sample (web/data)
Osm3dServer.start() -> str                      # raises ServerUnavailable when all ports are busy
```

### C3 — building heights (O3-NET writes the attributes, O3-OUT's viewer reads them)

Building features carry: `height` = wall top above ground in metres (OSM
`height` minus the roof rise for a non-flat roof, never below 2 m; else
levels × 3 m; else the default for the type), `roof_height` = roof rise in
metres (`roof:height`, else `roof:levels` × 3 m, else 0 = viewer default),
`min_height` = base of the walls (`min_height`, else `building:min_level` ×
3 m, else 0), `height_source` = `"height"`, `"levels"` or `"default"`. The
viewer extrudes walls from `min_height` to `height` and uses `roof_height`
instead of its default rise when it is above 0.

### C4 — place search (O3-UX)

`place_search.GEOCODER_ENDPOINT` is the module-level endpoint passed to
`QgsNominatimGeocoder` (QGIS's own service, `https://nominatim.qgis.org/search`
unless the probe shows otherwise); the test guard replaces it.

## 4. Implementers and file ownership

| Agent | Owns (nobody else edits these) |
|-------|--------------------------------|
| O3-NET | `osm_download.py`, new `net/` package, `tests/overpass_resilience.py`, new `tests/test_overpass_rules_qgis.py`, new `tests/test_osm_fidelity_qgis.py` |
| O3-UX | `dialog.py`, `main_plugin.py`, new `place_search.py`, new `tests/test_dialog_qgis.py`, `tests/smoke_plugin.py` |
| O3-OUT | `builder.py`, `server.py`, `qgis_styling.py`, `web/**` except `material_catalogue.js` and `facade_dynamic.js` (kept identical with PlanX 3D City), new `tests/test_runs_qgis.py`, `tests/*.mjs` |
| O3-DOCS | `README.md`, `CHANGELOG.md`, `CITATION.cff`, `metadata.txt`, `docs/**` except this file, `THIRD_PARTY_NOTICES.md`, `.zipignore` |
| master | this file, `tests/_offline.py`, `plugins.toml`, the release |

## 5. Rules for every implementer

1. Every QGIS suite calls `tests._offline.isolate_network()` before anything
   else; zero public requests, ever. No browser or file manager opens
   (`_offline.block_desktop()` records them).
2. No windows on the owner's desktop: `QT_QPA_PLATFORM=offscreen`.
3. QGIS 3.28 floor, Python 3.9 syntax, both runtimes (3.44 LTR, 4.2); no
   `QgsField(name, QVariant/int)` and no unprobed `QMetaType` (TRAPS 3.2b);
   palette, not QSS borders or backgrounds, on check boxes (TRAPS 3.7).
4. Hub scan: no bare `try/except: pass` (use `contextlib.suppress`), no
   `random`, no shell subprocess, no `# nosec`.
5. Red before green: every new test fails on 1.9.1 for the reason it names.
6. No git writes; the master commits and releases.

## 6. Status

- 2026-11-02: audits done; set written; implementers launched.
