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
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QTransform

from . import PLUGIN_VERSION
from .osm_download import download_osm_for_area, save_layer_to_geojson, utm_epsg_for
from .qgis_styling import style_export_layers

MODEL_NAME = "3D OSM Model"
MAX_STUDY_HA_DEFAULT = 300.0
MIN_RADIUS_M = 30.0
# A max-area circle necessarily has a 4/pi larger request bbox. Exact polygons
# get the same hard Overpass request budget, preventing thin/diagonal/multipart
# selections from turning a small study area into an enormous bbox download.
MAX_REQUEST_BBOX_FACTOR = 4.0 / math.pi
# The model base/island extends this many metres beyond the OSM study boundary, so
# the city sits on a small platform margin (OSM data stays clipped to the boundary).
BASE_BUFFER_M = 5.0

# Easy colour themes for the exported city. These recolour the 3D content only
# (buildings, roads, base/island, greens, roofs) — never the viewer's toolbar/panel
# chrome. The scene colours here mirror app.js COLOR_THEMES so a fresh export and a
# live theme switch in the viewer look identical. The per-function BUILDING palette
# lives only in the viewer (getSemanticColor); here we carry the scene colours plus
# the matching roof texture and asset theme. Editorial Paper is shared with the
# native OSM Quick 3D plugin and is the default for both web and QGIS outputs.
DEFAULT_THEME = "Editorial Paper"
_THEMES = {
    "Editorial Paper": {
        "islandColor": "#e6dfd3", "terrainOutsideColor": "#fdfbf7", "terrainSideColor": "#b9aa96",
        "roadColor": "#7c5c43", "parkColor": "#c2c5aa", "sportColor": "#aeb89a",
        "waterColor": "#9ab8c2", "roofTexture": "RoofA", "assetTheme": "Civic Heritage",
    },
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
    # Stylised "look" themes (v0.18.0) — scene colours mirror app.js COLOR_THEMES.
    "Anime": {
        "islandColor": "#e9f1e4", "terrainOutsideColor": "#eef5ec", "terrainSideColor": "#b9d0c0",
        "roadColor": "#7d8a96", "parkColor": "#7fc785", "sportColor": "#6fb8a0",
        "roofTexture": "CeramicLight", "assetTheme": "Coastal Light",
    },
    "Cartoon": {
        "islandColor": "#fbe7c6", "terrainOutsideColor": "#fdf1da", "terrainSideColor": "#d9b98a",
        "roadColor": "#4a4540", "parkColor": "#5fbf57", "sportColor": "#4fae87",
        "roofTexture": "TurkishTile", "assetTheme": "Modern Urban",
    },
    "Pixar": {
        "islandColor": "#ecdcc2", "terrainOutsideColor": "#f4ead6", "terrainSideColor": "#c9a981",
        "roadColor": "#6f655c", "parkColor": "#7bb069", "sportColor": "#6aa085",
        "roofTexture": "RoofA", "assetTheme": "Mediterranean",
    },
    "Futuristic City": {
        "islandColor": "#2b3038", "terrainOutsideColor": "#2a3340", "terrainSideColor": "#1f2730",
        "roadColor": "#1a1d24", "parkColor": "#2f9e7e", "sportColor": "#2f8eae",
        "roofTexture": "SolarRoof", "assetTheme": "Dense Urban",
    },
    "Classic Era": {
        "islandColor": "#d8c8a8", "terrainOutsideColor": "#e4d6b8", "terrainSideColor": "#b89c72",
        "roadColor": "#6b6052", "parkColor": "#8a9560", "sportColor": "#7a8550",
        "roofTexture": "TurkishTile", "assetTheme": "Civic Heritage",
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
        raise BuildError("The selected area has no usable interior for an inscribed circle.")

    r_max = math.sqrt(max(1.0, max_ha) * 10000.0 / math.pi)
    clamped = min(radius, r_max)
    if clamped < MIN_RADIUS_M:
        raise BuildError(
            f"The selected area is too small or narrow for the 3D model "
            f"(largest inscribed radius {clamped:.1f} m; minimum {MIN_RADIUS_M:.0f} m)."
        )
    return center, clamped


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


def _validate_exact_request_bbox(geom: QgsGeometry, max_ha: float) -> None:
    bbox = geom.boundingBox()
    bbox_m2 = bbox.width() * bbox.height()
    limit_m2 = _area_max_m2(max_ha) * MAX_REQUEST_BBOX_FACTOR
    if not math.isfinite(bbox_m2) or bbox_m2 <= 0:
        raise BuildError("The exact polygon has an empty or invalid bounding box.")
    if bbox_m2 > limit_m2 * (1.0 + 1e-9):
        raise BuildError(
            f"The exact polygon's Overpass request box is {bbox_m2 / 10000.0:,.1f} ha, "
            f"above the safe {limit_m2 / 10000.0:,.1f} ha limit. "
            "Use a compact selection, Rectangle, Rounded rectangle, or Inscribed circle."
        )


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
        _validate_exact_request_bbox(clip, max_ha)
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


def prepare_study_area(source_geom: QgsGeometry, source_crs: QgsCoordinateReferenceSystem,
                       max_ha: float = MAX_STUDY_HA_DEFAULT,
                       shape: str = SHAPE_CIRCLE) -> dict:
    """Prepare immutable run geometry and scalar network inputs on the QGIS thread."""
    if source_geom is None or source_geom.isEmpty():
        raise BuildError("No area selected. Draw/select a polygon or zoom to the area first.")
    if source_crs is None or not source_crs.isValid():
        raise BuildError("The selected area has no valid coordinate reference system.")

    project = QgsProject.instance()
    wgs_crs = QgsCoordinateReferenceSystem.fromEpsgId(4326)
    to_wgs = QgsCoordinateTransform(source_crs, wgs_crs, project)
    centroid_geom = source_geom.centroid()
    if centroid_geom is None or centroid_geom.isEmpty():
        raise BuildError("Could not determine the selected area's centroid.")
    centroid_wgs = to_wgs.transform(centroid_geom.asPoint())
    if not math.isfinite(centroid_wgs.x()) or not math.isfinite(centroid_wgs.y()):
        raise BuildError("Could not transform the selected area to WGS84.")

    epsg_dest = utm_epsg_for(centroid_wgs.x(), centroid_wgs.y())
    dst_crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest)
    geom_utm = QgsGeometry(source_geom)
    to_utm = QgsCoordinateTransform(source_crs, dst_crs, project)
    if geom_utm.transform(to_utm):
        raise BuildError("Could not reproject the selected area to a metric CRS.")
    clip_utm, base_utm, area_info = compute_study_area(geom_utm, max_ha, shape)

    utm_to_wgs = QgsCoordinateTransform(dst_crs, wgs_crs, project)
    wgs_rect = utm_to_wgs.transformBoundingBox(clip_utm.boundingBox())
    return {
        "epsg": epsg_dest,
        "centroid_wgs": QgsPointXY(centroid_wgs),
        "wgs_bbox": (
            wgs_rect.yMinimum(), wgs_rect.xMinimum(),
            wgs_rect.yMaximum(), wgs_rect.xMaximum(),
        ),
        "clip_utm": QgsGeometry(clip_utm),
        "base_utm": QgsGeometry(base_utm),
        "area_info": dict(area_info),
    }


# --------------------------------------------------------------------------
# GeoJSON writing
# --------------------------------------------------------------------------
def _remove_file(path: Path) -> None:
    """Remove an optional generated artifact without masking the real export."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _validate_geojson(path: Path) -> None:
    """Fail the run before publishing a missing or malformed viewer layer."""
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise BuildError(f"Could not validate exported GeoJSON {path.name}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise BuildError(f"Exported GeoJSON {path.name} is not a FeatureCollection.")
    if not isinstance(payload.get("features"), list):
        raise BuildError(f"Exported GeoJSON {path.name} has no features array.")


def _publish_directory(staging_dir: Path, target_dir: Path) -> None:
    """Atomically replace a generated directory, restoring the old one on error."""
    backup_dir = target_dir.parent / f".{target_dir.name}-backup-{uuid.uuid4().hex}"
    moved_old = False
    try:
        if target_dir.exists():
            target_dir.replace(backup_dir)
            moved_old = True
        staging_dir.replace(target_dir)
    except Exception:
        if moved_old and backup_dir.exists() and not target_dir.exists():
            backup_dir.replace(target_dir)
        raise
    finally:
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON beside its destination and publish it with one filesystem swap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}-", suffix=".tmp", delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
        tmp_path.replace(path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            _remove_file(tmp_path)


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
    tmp_path = None
    try:
        import processing  # noqa: WPS433 (QGIS runtime import)

        bbox = base_utm.boundingBox()
        extent = f"{bbox.xMinimum()},{bbox.xMaximum()},{bbox.yMinimum()},{bbox.yMaximum()} [EPSG:{epsg_dest}]"
        # A bounded raster is substantially faster to transfer and tessellate in
        # the browser than an unmodified high-resolution source DEM.
        target_resolution = max(1.0, max(bbox.width(), bbox.height()) / 512.0)
        dem_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=str(dem_path.parent), prefix=".mydem-", suffix=".tif", delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
        processing.run("gdal:warpreproject", {
            "INPUT": dem_layer,
            "SOURCE_CRS": dem_layer.crs(),
            "TARGET_CRS": QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest),
            "RESAMPLING": 1,
            "TARGET_RESOLUTION": target_resolution,
            "TARGET_EXTENT": extent,
            "TARGET_EXTENT_CRS": QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest),
            "CREATION_OPTIONS": "COMPRESS=DEFLATE|TILED=YES|PREDICTOR=2",
            "OUTPUT": str(tmp_path),
        })
        if not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
            raise BuildError("QGIS did not create the clipped DEM.")
        tmp_path.replace(dem_path)
        tmp_path = None
        return True
    except Exception as exc:  # DEM is optional; never block the export.
        if feedback:
            feedback(f"DEM skipped ({exc}).")
        return False
    finally:
        if tmp_path is not None:
            _remove_file(tmp_path)


# --------------------------------------------------------------------------
# Optional basemap underlay
# --------------------------------------------------------------------------
def _export_basemap(basemap_layer, base_utm: QgsGeometry, epsg_dest: int,
                    out_path: Path, feedback=None):
    """Render a basemap layer to a transparent PNG over the model base bbox (UTM).

    Works for raster, XYZ/tiled and vector layers via the QGIS map renderer.
    Returns the bbox dict {minx,miny,maxx,maxy} on success, else None. Any failure
    (network tiles, missing renderer) is swallowed so the export never breaks.
    """
    tmp_path = None
    try:
        from qgis.core import QgsMapSettings, QgsMapRendererCustomPainterJob
        from qgis.PyQt.QtCore import QSize
        from qgis.PyQt.QtGui import QColor, QImage, QPainter

        bbox = base_utm.boundingBox()
        w_m, h_m = bbox.width(), bbox.height()
        if w_m <= 0 or h_m <= 0:
            return None
        max_px = 2048
        if w_m >= h_m:
            wpx, hpx = max_px, max(256, int(round(max_px * h_m / w_m)))
        else:
            hpx, wpx = max_px, max(256, int(round(max_px * w_m / h_m)))

        settings = QgsMapSettings()
        settings.setLayers([basemap_layer])
        settings.setDestinationCrs(QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest))
        settings.setExtent(bbox)
        settings.setOutputSize(QSize(wpx, hpx))
        settings.setBackgroundColor(QColor(0, 0, 0, 0))  # transparent where the layer has no data
        try:
            settings.setFlag(QgsMapSettings.Flag.Antialiasing, True)
        except Exception:
            pass

        image = QImage(QSize(wpx, hpx), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            job = QgsMapRendererCustomPainterJob(settings, painter)
            job.renderSynchronously()
        finally:
            painter.end()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=str(out_path.parent), prefix=".basemap-", suffix=".png", delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
        if image.save(str(tmp_path), "PNG") and tmp_path.stat().st_size > 0:
            tmp_path.replace(out_path)
            tmp_path = None
            return {
                "minx": bbox.xMinimum(), "miny": bbox.yMinimum(),
                "maxx": bbox.xMaximum(), "maxy": bbox.yMaximum(),
            }
    except Exception as exc:  # basemap is optional; never block the export.
        if feedback:
            feedback(f"Basemap skipped ({exc}).")
    finally:
        if tmp_path is not None:
            _remove_file(tmp_path)
    return None


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
        "demMeshQuality": 140,
        "latitude": float(latitude),
        "dayOfYear": 172,
        # Open at golden hour: warm low sun, long soft shadows on the massing.
        "timeOfDay": 17.0,
        "autoTime": False,
        "fogDensity": 0.00035,
    }


def _write_manifest(web_root: Path, epsg_dest: int, latitude: float, has_dem: bool,
                    theme_name: str = DEFAULT_THEME, basemap_bbox=None) -> Path:
    theme_name, theme_pal = resolve_theme(theme_name)
    manifest = {
        "schema": "planx-3d-city-manifest/v1",
        "plugin": "osm_3d_model",
        "version": PLUGIN_VERSION,
        "mode": "vector",
        "flexibleInputs": True,
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": {"title": "3D OSM Model", "fileName": ""},
        "requiredInputs": [],
        "optionalInputs": ["buildings", "roads", "trees", "blocks"],
        "terrainTexture": None,
        "baseMapTexture": None,
        # Optional QGIS basemap rendered to an image, draped under the city.
        "basemap": ({"image": "data/basemap/basemap.png", "bbox": basemap_bbox}
                    if basemap_bbox else None),
        "roadAccess": None,
        # OSM-native column names (the export now carries raw OSM tags, no PlanX
        # Turkish schema). The viewer maps these through to floors/function/width.
        "fieldMappings": {
            "building_floors_field": "building_levels",
            "building_function_field": "building",
            "landuse_function_field": "landuse",
            "road_hierarchy_field": "highway",
            "road_width_field": "width",
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
    _write_json_atomic(manifest_path, manifest)
    return manifest_path


PROJECT_OWNED_PROPERTY = "osm_3d_model/owned"


def _replace_project_layers(project: QgsProject, roi_layer: QgsVectorLayer, osm: dict,
                            theme_name: str, theme_palette: dict) -> None:
    """Replace the previous run with a styled, correctly ordered layer group."""
    root = project.layerTreeRoot()
    for layer in list(project.mapLayers().values()):
        if layer.customProperty(PROJECT_OWNED_PROPERTY, False):
            project.removeMapLayer(layer.id())
    for node in list(root.children()):
        if node.customProperty(PROJECT_OWNED_PROPERTY, False):
            root.removeChildNode(node)

    group = root.insertGroup(0, MODEL_NAME)
    group.setCustomProperty(PROJECT_OWNED_PROPERTY, True)
    group.setCustomProperty("osm_3d_model/theme", theme_name)
    style_export_layers(roi_layer, osm, theme_name, theme_palette)

    # QGIS draws the legend from top to bottom. Keep point furniture and roads
    # above polygons, and the study-area paper/base fill at the very bottom.
    layers = [
        (osm.get("lights"), "OSM Street Lights"),
        (osm.get("busstops"), "OSM Bus Stops"),
        (osm.get("benches"), "OSM Benches"),
        (osm.get("trashbins"), "OSM Trash Bins"),
        (osm.get("trees"), "OSM Trees"),
        (osm.get("bikelanes"), "OSM Bike Lanes"),
        (osm.get("roads"), "OSM Roads"),
        (osm.get("buildings"), "OSM Buildings"),
        (osm.get("waterlines"), "OSM Waterways"),
        (osm.get("greens"), "OSM Greens & Water"),
        (roi_layer, "OSM Study Area"),
    ]
    for layer, label in layers:
        if layer is None or (layer is not roi_layer and layer.featureCount() <= 0):
            continue
        layer.setName(label)
        layer.setCustomProperty(PROJECT_OWNED_PROPERTY, True)
        project.addMapLayer(layer, False)
        group.addLayer(layer)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def build_and_export(source_geom: QgsGeometry, source_crs: QgsCoordinateReferenceSystem,
                     web_root: str, dem_layer=None, basemap_layer=None,
                     max_ha: float = MAX_STUDY_HA_DEFAULT,
                     add_to_project: bool = True, shape: str = SHAPE_CIRCLE,
                     theme: str = DEFAULT_THEME, use_cache: bool = True,
                     feedback=None, prepared: dict | None = None,
                     osm_payload: dict | None = None) -> dict:
    project = QgsProject.instance()
    if prepared is None:
        prepared = prepare_study_area(source_geom, source_crs, max_ha, shape)
    required = {"epsg", "centroid_wgs", "clip_utm", "base_utm", "area_info"}
    if not isinstance(prepared, dict) or not required.issubset(prepared):
        raise BuildError("The prepared study area is incomplete.")
    epsg_dest = int(prepared["epsg"])
    cen_wgs = QgsPointXY(prepared["centroid_wgs"])
    clip_utm = QgsGeometry(prepared["clip_utm"])
    base_utm = QgsGeometry(prepared["base_utm"])
    area_info = dict(prepared["area_info"])
    area_ha = area_info["area_ha"]
    theme_name, theme_palette = resolve_theme(theme)
    if feedback:
        if area_info.get("radius_m"):
            shape_desc = f"{area_info['shape_label']} r = {area_info['radius_m']:.0f} m"
        else:
            shape_desc = (f"{area_info['shape_label']} "
                          f"{area_info.get('width_m', 0):.0f} × {area_info.get('depth_m', 0):.0f} m")
        feedback(f"Study area: {shape_desc} ({area_ha} ha) +{BASE_BUFFER_M:.0f} m base, EPSG:{epsg_dest}.")

    # OSM download + clip (to the inner boundary, not the buffered base).
    osm = download_osm_for_area(
        clip_utm, epsg_dest, feedback=feedback, use_cache=use_cache,
        payload=osm_payload,
    )
    # Roads are used as raw OSM ways (single-part LineStrings). The earlier
    # snap+dissolve "connect_roads" step was removed: it produced MultiLineString
    # geometries and did not actually improve the network, so the native OSM
    # roads are kept as-is.

    # Write GeoJSON the viewer expects.
    web_path = Path(web_root)
    vector_dir = web_path / "data" / "yerlesim"
    vector_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=".yerlesim-", dir=str(vector_dir.parent)))

    roi_layer = _roi_layer_from_polygon(base_utm, epsg_dest)
    layer_files = {
        "roi": roi_layer,
        "mybuildings": osm["buildings"], "myroads": osm["roads"],
        "mytrees": osm["trees"], "myblocks": osm["greens"],
        "mywaterlines": osm["waterlines"], "mybikelanes": osm["bikelanes"],
        "mybusstops": osm["busstops"], "mybenches": osm["benches"],
        "mylights": osm["lights"], "mytrashbins": osm["trashbins"],
    }
    try:
        for name, layer in layer_files.items():
            out_path = staging_dir / f"{name}.geojson"
            save_layer_to_geojson(layer, out_path)
            _validate_geojson(out_path)
        _publish_directory(staging_dir, vector_dir)
        staging_dir = None
    finally:
        if staging_dir is not None and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

    # Optional DEM.
    has_dem = False
    dem_path = web_path / "data" / "dem" / "mydem.tif"
    if dem_layer is not None:
        has_dem = _export_dem(dem_layer, base_utm, epsg_dest, dem_path, feedback=feedback)
    if not has_dem:
        _remove_file(dem_path)

    # Optional basemap underlay rendered to a PNG over the base bbox.
    basemap_bbox = None
    if basemap_layer is not None:
        if feedback:
            feedback("Rendering basemap underlay...")
        basemap_bbox = _export_basemap(
            basemap_layer, base_utm, epsg_dest,
            web_path / "data" / "basemap" / "basemap.png", feedback=feedback,
        )
    if basemap_bbox is None:
        _remove_file(web_path / "data" / "basemap" / "basemap.png")

    _write_manifest(web_path, epsg_dest, cen_wgs.y(), has_dem, theme_name, basemap_bbox)

    if add_to_project:
        _replace_project_layers(project, roi_layer, osm, theme_name, theme_palette)

    return {
        "epsg": epsg_dest,
        "shape": area_info["shape"],
        "shape_label": area_info["shape_label"],
        "theme": theme_name,
        "radius_m": area_info.get("radius_m"),
        "width_m": area_info.get("width_m"),
        "depth_m": area_info.get("depth_m"),
        "area_ha": area_ha,
        "counts": osm["counts"],
        "has_dem": has_dem,
    }
