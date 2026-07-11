# Changelog

## [1.1.2] - 2026-07-12

- Keep viewer lighting stable during idle by removing activity-dependent SSAO, making it opt-in, and migrating saved settings.

## [1.1.1] - 2026-07-11

- Fix QGIS 4 export startup by resolving the QEventLoop input-exclusion flag through the PyQt5/PyQt6-compatible enum path.

## [1.1.0] - 2026-07-11

- Advanced Export Studio with high-resolution image, PDF, SVG, HTML and live video export; Pyramid roof fixes; Editorial Paper native QGIS styling; performance and security hardening.

### Release highlights

- Replaced the one-click screenshot tool with an advanced Export Studio. The web
  viewer now exports clean high-resolution PNG, JPEG, and WebP images; embedded
  SVG and self-contained HTML snapshots; A4 PDF presentation pages; and clipboard
  PNGs. Output presets cover the current viewport through 4K, with custom bounded
  dimensions and lossy-format quality controls.
- Added live canvas recording with runtime-detected WebM/MP4 codecs, selectable
  30/60 fps and bitrate, an elapsed-time indicator, and safe finalization. The
  exported media contains only the 3D scene, while camera movement, walk mode,
  traffic, weather, and solar animation continue live during recording.
- Split binary/document export assembly into a small tested module. CI now checks
  PDF xref offsets, safe SVG/HTML escaping, file-name sanitization, viewer syntax,
  and dead JavaScript declarations.
- Fixed the Pyramid roof option so the global Style-dock selection is applied to
  every building function instead of being silently replaced by automatic
  Flat/Gable roofs. Closed footprint rings are cleaned before meshing, concave
  buildings get a guaranteed interior apex, and OSM pyramidal/hipped/gabled/
  skillion aliases plus roof height tags are preserved and understood.
- Native QGIS output now arrives as a polished planning map based on the sibling
  OSM Quick 3D renderer: buildings and ground areas are categorized by function,
  roads are categorized by hierarchy with metric widths, water/trees/furniture
  have coordinated symbols, and the study-area base is correctly drawn last.
- Added Editorial Paper (warm/elegant) as the shared default map and web theme.
- Moved Overpass HTTP/cache work to a cancellable QGIS background task, keeping the
  dialog and QGIS interface responsive while public mirrors answer or time out.
- Hardened OSM ingestion: invalid/partial Overpass responses are never cached,
  cache writes are atomic, split or reversed relation members are stitched into
  valid rings, public-transport platforms are included, and roof levels augment
  both tagged and inferred building heights.
- Bounded exact-polygon requests by their real Overpass bounding box and reject
  circles that cannot fit inside the selected area instead of expanding them past
  the user's selection.
- Made GeoJSON, manifest, DEM, and basemap publication transactional; a failed run
  preserves the last working viewer data, stale optional rasters are removed, and
  DEM exports use a browser-sized compressed resolution.
- Repeated runs now replace one plugin-owned QGIS layer group rather than
  accumulating duplicate layers.
- Fixed the five reported Flake8 W503 findings and added repository CI for Python
  compilation, Flake8, Bandit medium/high findings, and viewer syntax.
- Reduced viewer GPU/CPU load with shared road, pedestrian, and vehicle resources,
  bounded traffic populations, debounced scene rebuilds, complete recursive GPU
  disposal, correctly resized post-processing, and screenshot capture without
  preserveDrawingBuffer.
- Fixed stale/misreported DEM loading, manifest/UI HTML escaping, local-server
  wildcard CORS exposure, and runtime version drift across metadata, manifests,
  User-Agent, and citation data.
- Replaced nine large PNG textures with visually equivalent full-resolution WebP
  assets (about 8.4 MB down to 1.5 MB) and removed unused game, legacy recorder, tour,
  tumulus/mosque renderers, legacy renderer, dummy GeoJSON, SVG, and vendor code.
  Model Studio no longer requests nonexistent bundled GLB files; uploaded custom
  tree and furniture models retain their procedural fallbacks.

## [1.0.1] - 2026-06-18

- docs: add CITATION.cff for Zenodo DOI integration

## [1.0.0] - 2026-06-12

- **Verified 1.0:** the complete pipeline — Overpass fetch with mirror fallback and disk cache, OSM parsing (buildings including multipolygon relations with holes, every road class, cycleways, pedestrian streets vs plaza areas, car parks, water areas and waterway ribbons, greens with deterministic tree scatter, street furniture nodes), all four boundary shapes with the area clamp, GeoJSON export, the viewer manifest, optional DEM warp, optional basemap render and the local viewer server — now runs end-to-end in a **real headless QGIS** on both QGIS 3.44 LTR and QGIS 4.0 as part of the release process (106 checks per version), alongside a GUI check of the dialog (17 checks per version) and headless-Chrome renders of the exported city (fresh profile, returning user with saved settings, and a themed export).
- **Faster clipping of dense areas:** the study boundary is GEOS-prepared once before the thousands of per-feature intersects/contains tests, so downloading large, feature-rich areas spends visibly less time clipping; tree scattering inside parks and forests uses the same prepared containment test.
- **Clip robustness:** when a feature only grazes the study boundary the intersection can come back as a mixed geometry collection carrying degenerate point/line slivers; only the parts matching the layer's geometry type are now kept, so no invisible degenerate features reach the export or the viewer.
- **Clearer Overpass errors:** when Overpass answers with zero elements but reports a server-side problem (query timeout, memory limit) in its `remark` field, the error message now includes that remark instead of only suggesting a different area.


## [0.18.0] - 2026-06-06

- **Five stylised look themes (final stable release):** the dialog's "Web theme" picker (and the viewer's `COLOR_THEMES` / builder's `_THEMES`) gain five playful palettes beside the five calm ones — **Anime** (bright cel-shaded pastels, light/coastal asset theme, ceramic roofs), **Cartoon** (bold primary buildings on dark roads, terracotta tile), **Pixar** (warm, friendly creams and oranges, Mediterranean assets, clay roofs), **Futuristic City** (dark neon base + cyan/violet glass towers, Dense-Urban assets, solar roofs) and **Classic Era** (vintage sepia stone, Civic-Heritage assets, terracotta tile). Each leans on the light, tintable procedural facades (so the building colour reads through the facade×colour multiply blend) and, like every theme, recolours the **3D content only** — buildings, roads, base/island, greens and roofs — never the viewer toolbar or panels. No schema bump (themes are export-driven; returning users keep their saved theme). This is the **final planned feature release**; the plugin is now considered stable.

## [0.17.0] - 2026-06-06

- **Per-building colour variation:** every building of a given function used to be drawn in the *exact* same shade, which reads as one flat artificial mass. Each building now gets a small (±7% HSL-lightness) variation, `jitterBuildingColor`, seeded by its own footprint centroid — so it is **deterministic** (the same area always looks identical, cache-safe), preserves hue & saturation (the active **web theme** still reads), and is **free** (wall materials are already created per building, so no extra draw calls or frame-rate cost). A block of housing or offices now reads as many individual buildings.

## [0.16.0] - 2026-06-06

- **Pedestrian squares & plazas:** pedestrian/footway *areas* (`highway=pedestrian`/`footway` with `area=yes`), town squares (`place=square`) and marketplaces are now drawn as flat paved **stone** ground (a new `pedestrian` block style, lighter than the asphalt of car parks). Previously a pedestrian area fell through to the road branch and was drawn as a thin **ring tracing its outline** instead of a filled square — a real correctness fix, not just new coverage. An ordinary `highway=pedestrian` way *without* `area=yes` is a pedestrian street and still renders as a road. Verified by the node `defaultBlockCategoryStyle` harness (pedestrian/plaza/square/marketplace → stone, never green or a road) and the qgis-stubbed routing test (a pedestrian *area* → blocks, a pedestrian *street* → roads).

## [0.15.0] - 2026-06-05

- **Car parks:** OSM `amenity=parking` areas are now downloaded and drawn as flat paved asphalt-grey ground (a new `parking` block style), a common part of real cities that was previously absent. They join the blocks layer as `landuse=parking`; the viewer checks `PARKING` **before** the green `PARK` rule (since `"PARKING".includes("PARK")`) so a car park never renders as a green park.
- **More complete green cover:** added orchards, vineyards, farmland, allotments, village greens, greenfield, grassland, heath, nature reserves, golf courses and commons to the download and to the viewer's green block styling, so mixed and edge-of-town areas read correctly instead of being skipped or coloured arbitrarily. Orchards, nature reserves and village greens are also tree-bearing, so they get scattered trees (v0.14.0). Verified with a node harness that evaluates the real `defaultBlockCategoryStyle` (parking → grey, all new types → green) plus the qgis-stubbed routing/scatter test.

## [0.14.0] - 2026-06-05

- **Planted parks, woods and forests:** wooded green areas (`leisure=park`/`garden`, `landuse=forest`/`grass`/`meadow`/`recreation_ground`/`cemetery`, `natural=wood`/`scrub`) are now planted with procedural trees scattered inside each polygon, at a density that suits the type (dense in forests, sparse on grass). OpenStreetMap usually maps these as plain areas with no individual tree points, so they used to render as flat green patches; they now read as genuinely wooded. The scatter is **globally capped** (≤500 trees) and **deterministic** (seeded by each polygon's footprint with a stable, process-independent integer — not Python's per-process string `hash`), so a re-run or a cache hit produces an identical city. Trees render through the existing instanced tree layer, so there is no viewer change and no measurable frame-rate cost.

## [0.13.0] - 2026-06-05

- **Water areas (new):** lakes, ponds, reservoirs, river polygons (riverbanks) and bays — OSM `natural=water`, `waterway=riverbank`, `landuse=reservoir`/`basin` — are now downloaded and drawn as flat blue water surfaces, clipped to the study boundary. They join the greens/blocks layer normalized to `natural=water`, which the viewer already styles as water, so no viewer change was needed. Previously only **linear** waterways (streams, narrow rivers) were shown, so any river, lake or coastline mapped as an *area* — the common case — was silently dropped.
- Fixed the export result message showing `r=None m` for rectangle and polygon boundaries; it now reports the footprint size, and water areas are folded into the waterways total in both the QGIS message bar and the dialog summary.

## [0.12.3] - 2026-06-05

- **New plugin icon:** the toolbar and Plugin Manager icon is now a crisp 256×256 PNG (an isometric flat-roofed 3D city on the salmon tile) that renders reliably in QGIS, replacing the SVG that could appear blank in some builds. The references in `metadata.txt`, `main_plugin.py` and `dialog.py` now point at `icons/icon.png` (regenerated by `scratch/make_osm_3d_model_icon.ps1`).

## [0.12.2] - 2026-06-05

- **Smarter roof shapes:** buildings no longer default to a pyramid roof on every block. House-like OSM building types (`house`, `detached`, `terrace`, `bungalow`, …) up to 3 levels get a pitched **gable** roof; everything else (apartments, commercial, industrial, civic, large blocks) gets a **flat** roof, which reads far more like a real city. An explicit OSM `roof_shape` tag, or a per-function roof shape set in the Style dock, still wins.

## [0.12.1] - 2026-06-05

- **Road widths by class:** roads are now drawn at a width matched to their OSM `highway` class (motorway/trunk/primary wide → service/track/footway narrow) instead of a flat 8 m on every road. The "Road width" control acts as a global multiplier (8 m = 1.0×). Roads with an explicit OSM `width` tag now use that real carriageway width (it was previously ignored because the manifest had no `road_width_field` mapping). Pedestrians follow the sidewalk edge of the actual road they walk along, so they no longer float on wide roads or stand in the carriageway on narrow ones.

## [0.12.0] - 2026-06-05

- **OSM download cache (new):** Overpass responses are cached on disk for a week, keyed by the exact area query. Re-running the same area — or just nudging a shape/theme option — reuses the cached data and skips the network entirely, which dodges the public Overpass API's frequent HTTP-429 rate limiting. A new "Clear OSM download cache" button in the dialog's Advanced section forces a fresh fetch.
- **Relation (multipolygon) coverage:** buildings, parks, forests and other green areas mapped as OSM *relations* are now imported. Previously only simple closed ways were read, so large building complexes with inner courtyards and relation-mapped parks/woods were silently dropped.

## [0.11.3] - 2026-06-04

- Reduced the model base buffer from 10 m to 5 m: the base/island now extends 5 m beyond the study boundary (was 10 m), for a tighter platform margin around the city.

## [0.11.2] - 2026-06-03

- Fixed traffic, cyclists and pedestrians moving too slowly (almost stopped) on some machines: their motion was tied to the frame rate, so it slowed whenever the frame rate dipped. Movement is now frame-rate-independent (calibrated to 60 fps), so speed stays consistent. The kerb sidewalks also no longer cast shadows (a flat ground strip casts none anyway), which restores some frame rate.

## [0.11.1] - 2026-06-03

- Sidewalk width now scales with the OSM road class, clamped to a realistic 0.5–2.5 m: wide along primary/secondary/pedestrian streets, narrow along service ways and paths (was a fixed 1.6 m on every road).

## [0.11.0] - 2026-06-03

- **Sidewalks on both sides:** procedural sidewalks are now raised kerbs with a curb face on both sides of every road, and use a double-sided material so a road's geometry winding can no longer cull the sidewalk on one side (the cause of sidewalks appearing on only one side). They are a touch wider and lighter for clarity.
- **Basemap underlay (new):** pick a basemap layer — raster, XYZ/satellite tiles, or vector — in the dialog's Advanced section and the plugin renders it to an image draped under the city, clipped to the study boundary.
- **New "Basemap & Texture" viewer panel:** style the basemap live — opacity, blend mode (Normal, Multiply, Screen, Add, Difference), brightness, contrast, saturation, tint, drape height, and whether it catches building shadows.

## [0.10.2] - 2026-06-03

- **Hotfix:** the browser viewer crashed with `persisted is not defined` and stayed stuck on an empty "Waiting for project metadata" scene for anyone whose browser had saved viewer settings from a previous session (most returning users). The colour-theme code added in 0.10.0 referenced a `persisted` variable that was scoped to a `try` block; it is now function-scoped, so the manifest theme applies without crashing. A first-time browser (empty localStorage) was unaffected, which is why automated testing — always run on a fresh profile — missed it.

## [0.10.1] - 2026-06-03

- Themes now read on the **buildings**, not just the base and roads: each non-default theme uses a set of light, tintable facades (limestone, stucco, glass, off-white) so the building colour palette shows through, and the building tints are a touch more saturated. The default **Plugin tones** theme is unchanged.

## [0.10.0] - 2026-06-03

- New **Web theme** option in the dialog sets the colour palette of the exported 3D city: Plugin tones (salmon & grey, default), Tinted gray + teal, Teal + salmon, Light purple + soft black, or Warm sand + slate.
- Themes recolour the city **content only** — buildings, roads, the base/island and its skirt, greens, and the roof texture — and never the viewer's toolbar or panels.
- The chosen theme is written into the export and applied automatically when the viewer opens. Re-opening the same export keeps any manual colour tweaks made in the Style dock; choosing a new theme in QGIS overrides them.
- The viewer building palette (`getSemanticColor`) and base/road/green colours are now driven by `COLOR_THEMES` (viewer) / `_THEMES` (builder); the default theme reproduces the previous muted greys exactly.
- Bumped the settings schema to 12 and the asset cache-bust to `osm0.10.0`; synced metadata, viewer manifest, sample manifest, and Overpass User-Agent to 0.10.0.

## [0.9.0] - 2026-06-03

- New **Boundary shape** option in the dialog: inscribed circle (default), rounded rectangle, rectangle (extent), or the exact selected polygon. The viewer renders any of them — previous exports always used the inscribed circle.
- The model base still extends 10 m beyond the boundary, now with softly rounded corners on every shape, so a plain rectangle reads as a gently rounded presentation platform.
- Rectangle and polygon boundaries are area-clamped about their centre, keeping the OSM request within the maximum study area like the circle.
- The chosen shape is remembered between runs and shown in the live area estimate and the last-run summary.
- Internal cleanup: removed the unused `connect_roads` snap-and-dissolve helper; renamed the circle-specific export internals (`download_osm_for_area`, `compute_study_area`) to be shape-agnostic; synced the viewer manifest and Overpass User-Agent to 0.9.0.

## [0.8.2] - 2026-06-02

- Restored the six apartment/residential facade textures (renamed from the old region-specific set) and made them the default building facade, kept with the grey massing colours.
- Roads are now used as raw OpenStreetMap geometry; removed the snap+dissolve `connect_roads` step (it fragmented geometry without improving the network).
- Model base buffer around the study circle: 20 m → 10 m.
- Changed the dashboard toggle button icon (☰ → ▦).

## [0.8.1] - 2026-06-02

- Fixed the QGIS Plugin Hub upload error caused by a literal `%` in the changelog (ConfigParser interpolation); reworded so metadata parses.
- Roads exported as MultiLineString (post dissolve) now render full procedural carriageways and sidewalks on **both** sides — the viewer previously skipped any non-LineString road.
- Default building colours switched to muted greys/slates (European / North-American massing); default facades and roofs lead with stone/glass/charcoal instead of terracotta brick.
- Roof texture options renamed to systematic, descriptive labels (e.g. "Slate tile", "Standing-seam metal") instead of RoofA/RoofB; internal keys unchanged.
- Cyclists ride automatically whenever a bike-lane layer exists.
- Procedural building setback now defaults to 0.
- Trucks ~1% and buses ~2% of traffic; the rest are city cars/vans.
- Removed the redundant top-level "Block color" control (covered by Block Categories).

## [0.8.0] - 2026-06-02

- Elite viewer toolbar: the floating circular buttons (absolutely positioned, which left dead gaps where the language and advanced buttons used to be) are now a single self-packing glass tray with even spacing and hairline separators grouping dashboard / docks / walk / tools.
- Dock toggle buttons now show an active state, so the toolbar highlights which panel (Layers, Style, Scene, Model Studio) is currently open.
- Removed the stale "Switch language (TR/EN)" entry from the Help & shortcuts overlay, since the viewer has been English-only since 0.7.0.

## [0.7.0] - 2026-06-02

- 100% OpenStreetMap schema; procedural roads/sidewalks/bike lanes; pedestrian-vehicle separation; +20m model base; English-only simplified viewer (Urban Controls and tumulus removed); ~5MB lighter assets

## [0.6.0] - 2026-06-02

- Water (rivers/streams/canals), street furniture (stops/benches/lamps/bins) and function-aware default building heights; sample city showcases them

## [0.5.0] - 2026-06-01

- Dissolved roads with clean intersections (round joins, flat caps); crosswalks and lane markings removed; sidewalks both sides

All notable changes to this plugin are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: [SemVer](https://semver.org/).

## [0.4.0] - 2026-06-01

- Connected road network (snap+dissolve), calmer default traffic (~1/5), MIT LICENSE, Qt6-safe enums, stable release
