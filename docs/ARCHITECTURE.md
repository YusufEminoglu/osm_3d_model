# Architecture Notes

3D OSM Model is a compact pipeline: QGIS chooses an area, Overpass supplies OSM data, Python exports native-tag GeoJSON, and the browser viewer renders a procedural city.

<p align="center">
  <img src="assets/pipeline.svg" alt="3D OSM Model pipeline" width="100%">
</p>

## Runtime Flow

| Stage | File | Responsibility |
| --- | --- | --- |
| Plugin lifecycle | `main_plugin.py` | Creates the action, opens the dialog, runs export, starts the viewer. |
| User options | `dialog.py` | Study-area source, boundary shape, web colour theme, maximum area, optional DEM and basemap layer, last-run summary. |
| OSM fetch | `osm_download.py` | Builds Overpass queries, tries mirrors, parses OSM tags, creates memory layers. |
| Export | `builder.py` | Resolves the study-area boundary shape, clips data, reprojects to UTM, writes GeoJSON and manifest. |
| Local serving | `server.py` | Runs a small HTTP server on `127.0.0.1:8120-8139`. |
| Viewer | `web/src/app.js` | Loads manifest and GeoJSON, builds the Three.js scene, handles UI and interaction. |

## Layer Contract

The viewer expects data in `web/data/yerlesim/` after export. The bundled sample uses the same contract.

| File | Meaning |
| --- | --- |
| `mybuildings.geojson` | OSM buildings with `building`, `building_levels`, `height`, and `name` where available. |
| `myroads.geojson` | OSM roads, excluding dedicated cycleways. |
| `mybikelanes.geojson` | OSM `highway=cycleway` features as bike-lane strips. |
| `mywaterlines.geojson` | Linear rivers, streams, canals, drains, and ditches. |
| `myblocks.geojson` | Greens, parks, landuse, natural, and leisure polygons. Water areas (lakes, ponds, riverbanks, reservoirs, bays) are included here normalized to `natural=water` (drawn blue), and car parks as `landuse=parking` (drawn paved grey), so the viewer's block styling renders each correctly. |
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
- Keep presentation controls visible enough for live demos: layers, style, sun, walk, screenshot, measure.

## Packaging Boundary

The `docs/` and `.github/` folders are GitHub presentation assets. They are excluded from the QGIS Plugin Hub package through `.zipignore`, so improving the repository page does not enlarge the plugin zip.
