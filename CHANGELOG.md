# Changelog

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

