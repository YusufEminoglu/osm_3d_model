# Changelog

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

