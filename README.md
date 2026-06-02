# 3D OSM Model

A fast, single-button QGIS plugin: pick an area, download OpenStreetMap data, and
instantly open it as an interactive 3D city in your browser. Built on the PlanX 3D
City viewer engine.

## What it does

1. You choose a study area — the **current map view** or **selected polygon
   feature(s)** in the active layer.
2. The plugin fits the **largest circle that fits inside your selection** as the
   study boundary, clamped to a maximum area (default 150 ha, up to ~300 ha). The
   **model base extends +20 m beyond that circle**, so the city sits on a small
   platform margin while the OSM data stays clipped to the inner circle.
3. It downloads OpenStreetMap **buildings, roads, cycleways, waterways, greens,
   trees and street furniture** (bus stops, benches, street lamps, waste baskets)
   for that circle, clips them to it, and reprojects to a metric UTM CRS. Layers
   keep their **native OSM columns** (`building`, `building_levels`, `highway`,
   `waterway`, `leisure`/`landuse`/`natural`, `width`) — no translated schema.
4. It writes the GeoJSON layers + a viewer manifest and opens the 3D viewer.

## One button

**Create OSM layers & export 3D viewer** does everything at once.

## Defaults

- **Buildings:** coloured by their **OSM `building` function** (residential,
  commercial, education, health, …). With no OSM floor/height data the default
  floor count is **function-aware**; tagged roof levels add to the massing.
- **Roads & sidewalks:** fully **procedural** — per-road ribbons with lane
  markings and sidewalks on **both sides** (PlanX 3D City logic). Pedestrians keep
  to the sidewalks and cars to car-capable roads.
- **Bike lanes:** OSM cycleways (`highway=cycleway`) render as green bike-lane
  strips — **on by default**; cyclists are an optional toggle.
- **Water:** rivers, streams, canals, drains and ditches render as flowing
  ribbons whose width scales with the waterway class — **on by default**.
- **Street furniture:** bus stops, benches, street lamps and waste baskets —
  **on by default**; street lamps glow after dark.
- **Boundary:** largest inscribed circle; model base buffered +20 m; terrain
  clipped to the base.
- **DEM:** optional — pick a raster in the dialog and it is clipped to the circle.
  Without a DEM the base is flat.
- **Look:** the scene opens at **golden hour** — warm low sun, long soft shadows.
- **Theme:** one unified salmon-tinted grey ("yavru ağzı") identity across the QGIS
  dialog, the web toolbar/panels and the 3D scene; a light/dark toggle lives in
  **Scene & Sun**.

## In the 3D viewer

The interface is **100% English** and deliberately lean. A small, OSM-focused
toolbar (top-left): **Layers**, **Style**, **Scene & Sun** (time of day, weather,
theme, effects, camera bookmarks, plus a Life & traffic section for traffic speed
and car/pedestrian density), **Model Studio**, **Walk mode**, plus
**📷 Screenshot**, **📏 Measure** (pick two ground points to read the distance)
and **❔ Help** (mouse/keyboard shortcuts). The old "Urban Controls" advanced
panel has been removed. The dashboard shows live counts — buildings, greens,
average floors, and estimated population, dwellings and vehicles — and lists only
OSM-derived layers.

Open the viewer directly (without running an export) and it loads a small
**bundled sample city** so the scene is never empty.

## Notes

- Overpass (OpenStreetMap) is a shared community service. Keep areas small
  (the ~300 ha cap keeps requests polite and fast). If the main Overpass server
  is rate-limited or slow, the download **falls back across mirror servers**
  automatically.
- Map data is **© OpenStreetMap contributors** (ODbL); the viewer shows this
  attribution, and you must keep it when sharing exports or screenshots.
- The dialog **remembers your last options** (study-area source and maximum
  area) and offers an *"open viewer automatically after export"* toggle.
- `web/data/` ships a small sample city (now including a stream plus bus stops,
  benches, lamps and bins) and is the runtime export sink: when you run the
  plugin, the real OSM export overwrites it. Only the optional DEM raster is
  excluded from the packaged zip.
- The viewer runs from a local HTTP server on `127.0.0.1:8120-8139`.
