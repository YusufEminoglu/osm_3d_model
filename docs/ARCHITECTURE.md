# Architecture Notes

3D OSM Model is a compact pipeline: QGIS chooses an area, Overpass supplies OSM data, Python exports native-tag GeoJSON, and the browser viewer renders a procedural city.

<p align="center">
  <img src="assets/pipeline.svg" alt="3D OSM Model pipeline" width="100%">
</p>

## Runtime Flow

| Stage | File | Responsibility |
| --- | --- | --- |
| Plugin lifecycle | `main_plugin.py` | Creates the action, opens the dialog, runs the Overpass request as a cancellable background task, starts the viewer. |
| User options | `dialog.py` | Study-area source, boundary shape, coordinated QGIS/web colour theme, maximum area, optional DEM and basemap layer, last-run summary. |
| OSM fetch | `osm_download.py` | Builds Overpass queries, tries mirrors, parses OSM tags, creates memory layers. |
| Export | `builder.py` | Resolves the study-area boundary shape, clips data, reprojects to UTM, atomically publishes GeoJSON, optional rasters, and the manifest. |
| Native map styling | `qgis_styling.py` | Applies semantic categories, metric road widths, Editorial Paper colours, symbols, and draw order to the QGIS layer group. |
| Local serving | `server.py` | Runs a same-origin-only HTTP server on `127.0.0.1:8120-8139` with no-store and hardening headers. |
| Viewer | `web/src/app.js` | Loads manifest and GeoJSON, builds the Three.js scene, handles UI, interaction, high-resolution capture, and canvas recording. |
| Export assembly | `web/src/export_utils.js` | Sanitizes output names and assembles self-contained SVG/HTML snapshots and binary JPEG-backed PDF pages. |

## Layer Contract

The viewer expects data in `web/data/yerlesim/` after export. The bundled sample uses the same contract.

| File | Meaning |
| --- | --- |
| `mybuildings.geojson` | OSM buildings with `building`, `building_levels`, `height`, `roof_shape`, `roof_height`, and `name` where available. |
| `myroads.geojson` | OSM roads, excluding dedicated cycleways. |
| `mybikelanes.geojson` | OSM `highway=cycleway` features as bike-lane strips. |
| `mywaterlines.geojson` | Linear rivers, streams, canals, drains, and ditches. |
| `myblocks.geojson` | Greens, parks, landuse, natural, and leisure polygons. Water areas (lakes, ponds, riverbanks, reservoirs, bays) are included here normalized to `natural=water` (drawn blue), car parks as `landuse=parking` (paved asphalt grey), and pedestrian squares/plazas as `landuse=pedestrian` (paved stone), so the viewer's block styling renders each correctly. |
| `mytrees.geojson` | OSM tree nodes, plus procedural trees scattered inside wooded greens (forest, wood, park, …) so they don't render bald. Scatter is capped and deterministic per polygon. |
| `mybusstops.geojson` | Bus stops and public transport platform points. |
| `mybenches.geojson` | Bench points. |
| `mylights.geojson` | Street lamps. |
| `mytrashbins.geojson` | Waste basket points. |
| `roi.geojson` | Study-area boundary (circle, rounded rectangle, rectangle, or polygon) and model base context. |

## Manifest Contract

`web/data/planx_manifest.json` describes the exported project, viewer defaults, data mappings, and optional DEM status.

Important mapping keys:

| Mapping | Default field |
| --- | --- |
| `building_floors_field` | `building_levels` |
| `building_function_field` | `building` |
| `road_hierarchy_field` | `highway` |
| `waterline_width_field` | `width` |
| `bike_lane_width_field` | `width` |

## Design Principles

- Keep the QGIS flow simple: one primary action, optional advanced DEM.
- Keep OSM tags recognizable instead of hiding them behind a translated schema.
- Keep the viewer useful even before export by shipping a sample city.
- Keep Overpass requests polite with a small maximum study area and mirror fallback.
- Preserve the last successful model if a later download or filesystem write fails.
- Bound browser resources: compressed textures/DEM, shared animated models, and
  explicit Three.js GPU disposal when a scene layer is rebuilt.
- Keep presentation controls visible enough for live demos: layers, style, sun, walk, Export Studio, and measure.
- Keep export deterministic and bounded: restore the live renderer after every
  high-resolution capture, cap allocations at about 33 megapixels, embed document
  assets, and only advertise recording codecs supported by the active browser.

## Packaging Boundary

The `docs/` and `.github/` folders are GitHub presentation assets. They are excluded from the QGIS Plugin Hub package through `.zipignore`, so improving the repository page does not enlarge the plugin zip.
