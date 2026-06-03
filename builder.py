# -*- coding: utf-8 -*-
"""Study-area computation, OSM clip/export and viewer manifest writing.

Given a source area geometry (selected polygon or map-canvas extent), this module:
  1. picks a metric UTM CRS,
  2. resolves the chosen boundary shape (inscribed circle, rounded rectangle,
     rectangle or the exact polygon), clamped to a maximum study area (~300 ha),
  3. downloads + clips OSM data to that boundary,
  4. buffers the boundary +BASE_BUFFER_M (soft corners) into the model base,
  5. writes the GeoJSON layers + manifest the bundled viewer reads.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QTransform

from .osm_download import download_osm_for_area, save_layer_to_geojson, utm_epsg_for

MODEL_NAME = "3D OSM Model"
MAX_STUDY_HA_DEFAULT = 300.0
MIN_RADIUS_M = 30.0
# The model base/island extends this many metres beyond the OSM study boundary, so
# the city sits on a small platform margin (OSM data stays clipped to the boundary).
BASE_BUFFER_M = 10.0

# Easy colour themes for the exported city. These recolour the 3D content only
# (buildings, roads, base/island, greens, roofs) — never the viewer's toolbar/panel
# chrome. The scene colours here mirror app.js COLOR_THEMES so a fresh export and a
# live theme switch in the viewer look identical. The per-function BUILDING palette
# lives only in the viewer (getSemanticColor); here we carry the scene colours plus
# the matching roof texture and asset theme. "Plugin Tones" is the historical
# salmon-and-grey default.
DEFAULT_THEME = "Plugin Tones"
_THEMES = {
    "Plugin Tones": {
        "islandColor": "#dcc7bd", "terrainOutsideColor": "#e7d7d0", "terrainSideColor": "#b8978a",
        "roadColor": "#9a8c84", "parkColor": "#9fae8a", "sportColor": "#8fa07c",
        "roofTexture": "USShingle", "assetTheme": "Modern Urban",
    },
    "Tinted Gray Teal": {
        "islandColor": "#dde6e3", "terrainOutsideColor": "#e6efec", "terrainSideColor": "#7fb0a8",
        "roadColor": "#36433f", "parkColor": "#6fa589", "sportColor": "#5f9579",
        "roofTexture": "StandingSeam", "assetTheme": "Modern Urban",
    },
    "Teal & Salmon": {
        "islandColor": "#e7d7d0", "terrainOutsideColor": "#dfeae7", "terrainSideColor": "#4f8c84",
        "roadColor": "#2f4a46", "parkColor": "#5e9e7e", "sportColor": "#4f8e70",
        "roofTexture": "StandingSeam", "assetTheme": "Modern Urban",
    },
    "Light Purple & Black": {
        "islandColor": "#e7e2f0", "terrainOutsideColor": "#efecf6", "terrainSideColor": "#9b8fb0",
        "roadColor": "#2a2a30", "parkColor": "#8a9e6e", "sportColor": "#7a8e60",
        "roofTexture": "USShingle", "assetTheme": "Modern Urban",
    },
    "Warm Sand & Slate": {
        "islandColor": "#e8ddc7", "terrainOutsideColor": "#efe7d4", "terrainSideColor": "#c2a878",
        "roadColor": "#46413a", "parkColor": "#8aa05e", "sportColor": "#7a9050",
        "roofTexture": "GermanTile", "assetTheme": "Modern Urban",
    },
}


def resolve_theme(name: str) -> tuple:
    """Return (theme_name, palette) for a requested theme, falling back to default."""
    name = name if name in _THEMES else DEFAULT_THEME
    return name, _THEMES[name]


class BuildError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Inscribed circle
# --------------------------------------------------------------------------
def _boundary_line(geom: QgsGeometry) -> QgsGeometry:
    line = geom.convertToType(QgsWkbTypes.LineGeometry, True)
    if line and not line.isEmpty():
        return line
    return geom


def largest_inscribed_circle(geom_utm: QgsGeometry, max_ha: float):
    """Return (center QgsPointXY, radius_m) of the largest circle inside geom_utm,
    clamped so the circle area never exceeds ``max_ha`` hectares."""
    bbox = geom_utm.boundingBox()
    precision = max(1.0, min(bbox.width(), bbox.height()) / 100.0)
    boundary = _boundary_line(geom_utm)

    center = None
    radius = 0.0
    try:
        poi = geom_utm.poleOfInaccessibility(precision)
        # Some bindings return (geometry, distance); others return just the point.
        if isinstance(poi, tuple):
            poi_geom = poi[0]
            radius = float(poi[1]) if len(poi) > 1 and poi[1] is not None else 0.0
        else:
            poi_geom = poi
        if poi_geom and not poi_geom.isEmpty():
            center = poi_geom.asPoint()
            if radius <= 0.0:
                radius = poi_geom.distance(boundary)
    except Exception:
        center = None

    if center is None:
        pos = geom_utm.pointOnSurface()
        if pos is None or pos.isEmpty():
            pos = geom_utm.centroid()
        center = pos.asPoint()
        radius = pos.distance(boundary)

    if not math.isfinite(radius) or radius <= 0:
        # Degenerate selection: fall back to half the shorter bbox side.
        radius = max(MIN_RADIUS_M, min(bbox.width(), bbox.height()) / 2.0)

    r_max = math.sqrt(max(1.0, max_ha) * 10000.0 / math.pi)
    clamped = min(radius, r_max)
    return center, max(MIN_RADIUS_M, clamped)


# --------------------------------------------------------------------------
# Study-area boundary shapes
# --------------------------------------------------------------------------
# How the study boundary is derived from the selected area. The viewer reads the
# resulting polygon (roi.geojson) generically, so any of these shapes renders the
# same way — only the footprint changes.
SHAPE_CIRCLE = "circle"     # largest inscribed circle (default; legacy behaviour)
SHAPE_ROUNDED = "rounded"   # bounding box with visibly rounded corners
SHAPE_EXTENT = "extent"     # bounding box rectangle (lightly softened on the base)
SHAPE_POLYGON = "polygon"   # the selected polygon / canvas rectangle as-is
VALID_SHAPES = (SHAPE_CIRCLE, SHAPE_ROUNDED, SHAPE_EXTENT, SHAPE_POLYGON)
SHAPE_LABELS = {
    SHAPE_CIRCLE: "Inscribed circle",
    SHAPE_ROUNDED: "Rounded rectangle",
    SHAPE_EXTENT: "Rectangle",
    SHAPE_POLYGON: "Exact polygon",
}


def _area_max_m2(max_ha: float) -> float:
    return max(1.0, float(max_ha)) * 10000.0


def _scale_about_centroid(geom: QgsGeometry, scale: float) -> QgsGeometry:
    """Scale ``geom`` by ``scale`` about its own centroid (keeps it centred)."""
    centre = geom.centroid().asPoint()
    transform = QTransform()
    transform.translate(centre.x(), centre.y())
    transform.scale(scale, scale)
    transform.translate(-centre.x(), -centre.y())
    out = QgsGeometry(geom)
    out.transform(transform)
    return out


def _clamp_polygon_area(geom: QgsGeometry, max_ha: float) -> QgsGeometry:
    """Shrink a polygon about its centroid so its area never exceeds ``max_ha`` ha."""
    max_m2 = _area_max_m2(max_ha)
    area = geom.area()
    if area <= max_m2 or area <= 0:
        return geom
    return _scale_about_centroid(geom, math.sqrt(max_m2 / area))


def _clamp_bbox_area(bbox: QgsRectangle, max_ha: float) -> QgsRectangle:
    """Return ``bbox`` shrunk about its centre so width*height ≤ ``max_ha`` ha (aspect kept)."""
    max_m2 = _area_max_m2(max_ha)
    width, height = bbox.width(), bbox.height()
    if width <= 0 or height <= 0 or width * height <= max_m2:
        return bbox
    scale = math.sqrt(max_m2 / (width * height))
    cx, cy = bbox.center().x(), bbox.center().y()
    half_w, half_h = width * scale / 2.0, height * scale / 2.0
    return QgsRectangle(cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def _corner_radius(bbox: QgsRectangle) -> float:
    """A pleasant, size-aware corner radius for the rounded-rectangle boundary."""
    short = min(bbox.width(), bbox.height())
    return max(12.0, min(80.0, short * 0.16))


def _rounded_rect_geom(bbox: QgsRectangle, radius: float) -> QgsGeometry:
    """A rectangle with rounded corners: inset by r, then buffer back out by r."""
    r = max(0.0, float(radius))
    half = min(bbox.width(), bbox.height()) / 2.0
    if r <= 0 or half <= 0:
        return QgsGeometry.fromRect(bbox)
    r = min(r, half * 0.98)
    inner = QgsRectangle(
        bbox.xMinimum() + r, bbox.yMinimum() + r,
        bbox.xMaximum() - r, bbox.yMaximum() - r,
    )
    return QgsGeometry.fromRect(inner).buffer(r, 18)


def _soft_base(clip_geom: QgsGeometry) -> QgsGeometry:
    """Model base/island = boundary buffered +BASE_BUFFER_M with round joins.

    The round-join buffer softens every corner by ``BASE_BUFFER_M`` metres, so a
    plain rectangle boundary still reads as a gently rounded platform in the viewer.
    """
    return clip_geom.buffer(BASE_BUFFER_M, 18)


def compute_study_area(geom_utm: QgsGeometry, max_ha: float, shape: str):
    """Resolve the study boundary for ``shape`` into (clip_geom, base_geom, info).

    ``clip_geom`` is the boundary OSM data is clipped to; ``base_geom`` is the
    visible model base (``clip_geom`` buffered +BASE_BUFFER_M with soft corners).
    ``info`` carries human-readable dimensions for the dialog summary.
    """
    shape = (shape or SHAPE_CIRCLE).lower()
    if shape not in VALID_SHAPES:
        shape = SHAPE_CIRCLE

    if shape == SHAPE_CIRCLE:
        center, radius = largest_inscribed_circle(geom_utm, max_ha)
        clip = QgsGeometry.fromPointXY(center).buffer(radius, 96)
        base = QgsGeometry.fromPointXY(center).buffer(radius + BASE_BUFFER_M, 96)
        return clip, base, {
            "shape": shape, "shape_label": SHAPE_LABELS[shape],
            "radius_m": round(radius, 1),
            "area_ha": round(math.pi * radius * radius / 10000.0, 1),
        }

    if shape == SHAPE_POLYGON:
        clip = _clamp_polygon_area(QgsGeometry(geom_utm), max_ha)
        fixed = clip.makeValid()
        if fixed is not None and not fixed.isEmpty():
            clip = fixed
    else:  # SHAPE_EXTENT or SHAPE_ROUNDED
        bbox = _clamp_bbox_area(geom_utm.boundingBox(), max_ha)
        clip = (_rounded_rect_geom(bbox, _corner_radius(bbox))
                if shape == SHAPE_ROUNDED else QgsGeometry.fromRect(bbox))

    base = _soft_base(clip)
    bb = clip.boundingBox()
    return clip, base, {
        "shape": shape, "shape_label": SHAPE_LABELS[shape],
        "radius_m": None,
        "area_ha": round(clip.area() / 10000.0, 1),
        "width_m": round(bb.width(), 1),
        "depth_m": round(bb.height(), 1),
    }


# --------------------------------------------------------------------------
# GeoJSON writing
# --------------------------------------------------------------------------
INIT_LOADED_FILES = (
    "myblocks", "mybuildings", "myroads", "mytrees", "mylights", "mybenches",
    "mytrashbins", "mybusstops", "myfences", "mywaterlines", "mymosques",
    "mysidewalks", "mybikelanes",
)


def _write_empty_fc(path: Path, name: str) -> None:
    path.write_text(
        json.dumps({"type": "FeatureCollection", "name": name, "features": []}, ensure_ascii=False),
        encoding="utf-8",
    )


def _roi_layer_from_polygon(base_utm: QgsGeometry, epsg_dest: int) -> QgsVectorLayer:
    """Memory layer holding the model base footprint (any boundary shape)."""
    layer = QgsVectorLayer(f"MultiPolygon?crs=EPSG:{epsg_dest}", "Study area", "memory")
    pr = layer.dataProvider()
    pr.addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()
    geom = QgsGeometry(base_utm)
    geom.convertToMultiType()  # the provider is MultiPolygon; accept single parts too
    feat = QgsFeature()
    feat.setGeometry(geom)
    feat.setAttributes(["3D OSM Model study area"])
    pr.addFeatures([feat])
    layer.updateExtents()
    return layer


# --------------------------------------------------------------------------
# Optional DEM clip
# --------------------------------------------------------------------------
def _export_dem(dem_layer, base_utm: QgsGeometry, epsg_dest: int, dem_path: Path, feedback=None) -> bool:
    """Best-effort DEM warp/clip to the model base bbox in the target UTM CRS."""
    try:
        import processing  # noqa: WPS433 (QGIS runtime import)

        bbox = base_utm.boundingBox()
        extent = f"{bbox.xMinimum()},{bbox.xMaximum()},{bbox.yMinimum()},{bbox.yMaximum()} [EPSG:{epsg_dest}]"
        processing.run("gdal:warpreproject", {
            "INPUT": dem_layer,
            "SOURCE_CRS": dem_layer.crs(),
            "TARGET_CRS": QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest),
            "RESAMPLING": 1,
            "TARGET_EXTENT": extent,
            "TARGET_EXTENT_CRS": QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest),
            "OUTPUT": str(dem_path),
        })
        return dem_path.exists()
    except Exception as exc:  # DEM is optional; never block the export.
        if feedback:
            feedback(f"DEM skipped ({exc}).")
        return False


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------
def _viewer_defaults(latitude: float, has_dem: bool, theme_name: str = DEFAULT_THEME) -> dict:
    name, pal = resolve_theme(theme_name)
    return {
        # Easy colour theme (content colours only; the viewer reapplies it by name).
        "colorTheme": name,
        # Visible content
        "showBuildings": True,
        "showRoads": True,
        "showTrees": True,
        "showIslands": True,
        # Study area + solid base, coloured by the active theme.
        "showTerrainSides": True,
        "terrainSideDrop": 8.0,
        "terrainSideColor": pal["terrainSideColor"],
        "showOutsideRoiTerrain": False,
        "terrainOutsideColor": pal["terrainOutsideColor"],
        "islandColor": pal["islandColor"],
        "islandTransparency": 0.0,
        "parkColor": pal["parkColor"],
        "sportColor": pal["sportColor"],
        "roadColor": pal["roadColor"],
        "roofTexture": pal["roofTexture"],
        "flattenIslands": True,
        # Procedural detail OFF by default, procedural road traces ON
        "showLedges": False,
        "showStorefronts": False,
        "showRoadMarkings": True,
        "buildingMode": "Extruded + roof",
        # Lively scene: realistic trees plus moving pedestrians and mixed traffic
        # (cars, vans, buses, trucks).
        "treeRenderMode": "Realistic",
        "assetTheme": pal["assetTheme"],
        "showCars": True,
        # Sparse default traffic: ~1/5 of the previous density (user request).
        "carDensity": 0.09,
        "trafficSpeed": 1.0,
        "showPedestrians": True,
        "pedestrianDensity": 0.5,
        # OSM street furniture (bus stops, benches, lamps, bins) on by default;
        # street lamps glow at night.
        "showFurniture": True,
        "showLights": True,
        "showBenches": True,
        "showBins": True,
        "showBusStops": True,
        # OSM waterways drawn as flowing ribbons; per-feature width via "width".
        "showWaterlines": True,
        "waterlineWidth": 3.0,
        # OSM cycleways drawn as green bike-lane strips (procedural, like PlanX 3D
        # City); cyclists ride whenever a bike-lane layer is present.
        "showBikeLanes": True,
        "showBikes": True,
        "bikeDensity": 0.1,
        # Procedural sidewalks both sides; pedestrians keep to them, off the road.
        "showSidewalks": True,
        # Keep the rest off so the scene stays fast.
        "showCrosswalks": False,
        "showPedestrianPaths": False,
        "showParcels": False,
        "showHardscape": False,
        "showFences": False,
        "showMosques": False,
        "demMeshQuality": 140,
        "latitude": float(latitude),
        "dayOfYear": 172,
        # Open at golden hour: warm low sun, long soft shadows on the massing.
        "timeOfDay": 17.0,
        "autoTime": False,
        "fogDensity": 0.00035,
    }


def _write_manifest(web_root: Path, epsg_dest: int, latitude: float, has_dem: bool,
                    theme_name: str = DEFAULT_THEME) -> Path:
    theme_name, theme_pal = resolve_theme(theme_name)
    manifest = {
        "schema": "planx-3d-city-manifest/v1",
        "plugin": "osm_3d_model",
        "version": "0.10.2",
        "mode": "vector",
        "flexibleInputs": True,
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": {"title": "3D OSM Model", "fileName": ""},
        "requiredInputs": [],
        "optionalInputs": ["buildings", "roads", "trees", "blocks"],
        "terrainTexture": None,
        "baseMapTexture": None,
        "roadAccess": None,
        # OSM-native column names (the export now carries raw OSM tags, no PlanX
        # Turkish schema). The viewer maps these through to floors/function/width.
        "fieldMappings": {
            "building_floors_field": "building_levels",
            "building_function_field": "building",
            "landuse_function_field": "landuse",
            "road_hierarchy_field": "highway",
            "block_category_field": "landuse",
            "waterline_width_field": "width",
            "bike_lane_width_field": "width",
        },
        "analysisDefaults": {"roadColorMode": "Default"},
        "viewerDefaults": _viewer_defaults(latitude, has_dem, theme_name),
        "assetTheme": theme_pal["assetTheme"],
        "colorTheme": theme_name,
        "inputs": [],
        "summary": {"source": "OpenStreetMap", "epsg": epsg_dest, "hasDem": has_dem},
    }
    manifest_path = web_root / "data" / "planx_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def build_and_export(source_geom: QgsGeometry, source_crs: QgsCoordinateReferenceSystem,
                     web_root: str, dem_layer=None, max_ha: float = MAX_STUDY_HA_DEFAULT,
                     add_to_project: bool = True, shape: str = SHAPE_CIRCLE,
                     theme: str = DEFAULT_THEME, feedback=None) -> dict:
    if source_geom is None or source_geom.isEmpty():
        raise BuildError("No area selected. Draw/select a polygon or zoom to the area first.")

    project = QgsProject.instance()
    wgs_crs = QgsCoordinateReferenceSystem.fromEpsgId(4326)

    # Centroid in WGS84 -> pick UTM zone.
    to_wgs = QgsCoordinateTransform(source_crs, wgs_crs, project)
    centroid_pt = source_geom.centroid().asPoint()
    cen_wgs = to_wgs.transform(centroid_pt)
    epsg_dest = utm_epsg_for(cen_wgs.x(), cen_wgs.y())
    dst_crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest)

    # Source geometry -> UTM, then resolve the chosen boundary shape.
    geom_utm = QgsGeometry(source_geom)
    to_utm = QgsCoordinateTransform(source_crs, dst_crs, project)
    if geom_utm.transform(to_utm):
        raise BuildError("Could not reproject the selected area to a metric CRS.")
    # Resolve the chosen boundary shape: ``clip_utm`` is what OSM is clipped to,
    # ``base_utm`` is the visible model base (clip buffered +BASE_BUFFER_M with
    # softly rounded corners). OSM stays clipped to the inner boundary; only the
    # platform (roi + DEM) uses the wider ring.
    clip_utm, base_utm, area_info = compute_study_area(geom_utm, max_ha, shape)
    area_ha = area_info["area_ha"]
    if feedback:
        if area_info.get("radius_m"):
            shape_desc = f"{area_info['shape_label']} r = {area_info['radius_m']:.0f} m"
        else:
            shape_desc = (f"{area_info['shape_label']} "
                          f"{area_info.get('width_m', 0):.0f} × {area_info.get('depth_m', 0):.0f} m")
        feedback(f"Study area: {shape_desc} ({area_ha} ha) +{BASE_BUFFER_M:.0f} m base, EPSG:{epsg_dest}.")

    # OSM download + clip (to the inner boundary, not the buffered base).
    osm = download_osm_for_area(clip_utm, epsg_dest, feedback=feedback)
    # Roads are used as raw OSM ways (single-part LineStrings). The earlier
    # snap+dissolve "connect_roads" step was removed: it produced MultiLineString
    # geometries and did not actually improve the network, so the native OSM
    # roads are kept as-is.

    # Write GeoJSON the viewer expects.
    web_path = Path(web_root)
    vector_dir = web_path / "data" / "yerlesim"
    vector_dir.mkdir(parents=True, exist_ok=True)

    roi_layer = _roi_layer_from_polygon(base_utm, epsg_dest)
    save_layer_to_geojson(roi_layer, vector_dir / "roi.geojson")
    save_layer_to_geojson(osm["buildings"], vector_dir / "mybuildings.geojson")
    save_layer_to_geojson(osm["roads"], vector_dir / "myroads.geojson")
    save_layer_to_geojson(osm["trees"], vector_dir / "mytrees.geojson")
    save_layer_to_geojson(osm["greens"], vector_dir / "myblocks.geojson")
    save_layer_to_geojson(osm["waterlines"], vector_dir / "mywaterlines.geojson")
    save_layer_to_geojson(osm["bikelanes"], vector_dir / "mybikelanes.geojson")
    save_layer_to_geojson(osm["busstops"], vector_dir / "mybusstops.geojson")
    save_layer_to_geojson(osm["benches"], vector_dir / "mybenches.geojson")
    save_layer_to_geojson(osm["lights"], vector_dir / "mylights.geojson")
    save_layer_to_geojson(osm["trashbins"], vector_dir / "mytrashbins.geojson")

    written_named = {"mybuildings", "myroads", "mytrees", "myblocks",
                     "mywaterlines", "mybikelanes", "mybusstops", "mybenches", "mylights", "mytrashbins"}
    for name in INIT_LOADED_FILES:
        if name not in written_named:
            _write_empty_fc(vector_dir / f"{name}.geojson", name)

    # Optional DEM.
    has_dem = False
    if dem_layer is not None:
        dem_dir = web_path / "data" / "dem"
        dem_dir.mkdir(parents=True, exist_ok=True)
        has_dem = _export_dem(dem_layer, base_utm, epsg_dest, dem_dir / "mydem.tif", feedback=feedback)

    _write_manifest(web_path, epsg_dest, cen_wgs.y(), has_dem, theme)

    if add_to_project:
        roi_layer.setName("OSM Study Area")
        project.addMapLayer(roi_layer)
        for key, label in (("greens", "OSM Greens"), ("buildings", "OSM Buildings"),
                           ("roads", "OSM Roads"), ("bikelanes", "OSM Bike Lanes"),
                           ("waterlines", "OSM Waterways"),
                           ("trees", "OSM Trees"), ("busstops", "OSM Bus Stops"),
                           ("benches", "OSM Benches"), ("lights", "OSM Street Lights"),
                           ("trashbins", "OSM Trash Bins")):
            layer = osm.get(key)
            if layer is not None and layer.featureCount() > 0:
                layer.setName(label)
                project.addMapLayer(layer)

    return {
        "epsg": epsg_dest,
        "shape": area_info["shape"],
        "shape_label": area_info["shape_label"],
        "theme": resolve_theme(theme)[0],
        "radius_m": area_info.get("radius_m"),
        "width_m": area_info.get("width_m"),
        "depth_m": area_info.get("depth_m"),
        "area_ha": area_ha,
        "counts": osm["counts"],
        "has_dem": has_dem,
    }
