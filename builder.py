# -*- coding: utf-8 -*-
"""Study-area computation, OSM clip/export and viewer manifest writing.

Given a source area geometry (selected polygon or map-canvas extent), this module:
  1. picks a metric UTM CRS,
  2. resolves the chosen boundary shape (inscribed circle, rounded rectangle,
     rectangle or the exact polygon), clamped to a maximum study area (~300 ha),
  3. downloads + clips OSM data to that boundary,
  4. buffers the boundary +BASE_BUFFER_M (soft corners) into the model base,
  5. writes one *run*: the GeoJSON layers + manifest the bundled viewer reads,
     the optional DEM and basemap, and a GeoPackage holding every QGIS layer.

Every run gets its own folder under the QGIS profile
(``<profile>/osm_3d_model/runs/YYYYmmdd-HHMMSS[-label]``), so two runs never
overwrite each other, a file left open from an earlier run never breaks a new
one, and a plugin update never touches the user's work.  The plugin's own
``web/data`` is the bundled sample city and is never written by a build.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
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
        "waterColor": "#9ab8c2", "roofTexture": "ClayTile", "assetTheme": "Civic Heritage",
    },
    "Plugin Tones": {
        "islandColor": "#dcc7bd", "terrainOutsideColor": "#e7d7d0", "terrainSideColor": "#b8978a",
        "roadColor": "#9a8c84", "parkColor": "#9fae8a", "sportColor": "#8fa07c",
        "roofTexture": "Shingle", "assetTheme": "Modern Urban",
    },
    "Tinted Gray Teal": {
        "islandColor": "#dde6e3", "terrainOutsideColor": "#e6efec", "terrainSideColor": "#7fb0a8",
        "roadColor": "#36433f", "parkColor": "#6fa589", "sportColor": "#5f9579",
        "roofTexture": "MetalSeam", "assetTheme": "Modern Urban",
    },
    "Teal & Salmon": {
        "islandColor": "#e7d7d0", "terrainOutsideColor": "#dfeae7", "terrainSideColor": "#4f8c84",
        "roadColor": "#2f4a46", "parkColor": "#5e9e7e", "sportColor": "#4f8e70",
        "roofTexture": "MetalSeam", "assetTheme": "Modern Urban",
    },
    "Light Purple & Black": {
        "islandColor": "#e7e2f0", "terrainOutsideColor": "#efecf6", "terrainSideColor": "#9b8fb0",
        "roadColor": "#2a2a30", "parkColor": "#8a9e6e", "sportColor": "#7a8e60",
        "roofTexture": "Shingle", "assetTheme": "Modern Urban",
    },
    "Warm Sand & Slate": {
        "islandColor": "#e8ddc7", "terrainOutsideColor": "#efe7d4", "terrainSideColor": "#c2a878",
        "roadColor": "#46413a", "parkColor": "#8aa05e", "sportColor": "#7a9050",
        "roofTexture": "Slate", "assetTheme": "Modern Urban",
    },
    # Stylised "look" themes (v0.18.0) — scene colours mirror app.js COLOR_THEMES.
    "Anime": {
        "islandColor": "#e9f1e4", "terrainOutsideColor": "#eef5ec", "terrainSideColor": "#b9d0c0",
        "roadColor": "#7d8a96", "parkColor": "#7fc785", "sportColor": "#6fb8a0",
        "roofTexture": "ClayTile", "assetTheme": "Coastal Light",
    },
    "Cartoon": {
        "islandColor": "#fbe7c6", "terrainOutsideColor": "#fdf1da", "terrainSideColor": "#d9b98a",
        "roadColor": "#4a4540", "parkColor": "#5fbf57", "sportColor": "#4fae87",
        "roofTexture": "ClayTile", "assetTheme": "Modern Urban",
    },
    "Pixar": {
        "islandColor": "#ecdcc2", "terrainOutsideColor": "#f4ead6", "terrainSideColor": "#c9a981",
        "roadColor": "#6f655c", "parkColor": "#7bb069", "sportColor": "#6aa085",
        "roofTexture": "ClayTile", "assetTheme": "Mediterranean",
    },
    "Futuristic City": {
        "islandColor": "#2b3038", "terrainOutsideColor": "#2a3340", "terrainSideColor": "#1f2730",
        "roadColor": "#1a1d24", "parkColor": "#2f9e7e", "sportColor": "#2f8eae",
        "roofTexture": "Solar", "assetTheme": "Dense Urban",
    },
    "Classic Era": {
        "islandColor": "#d8c8a8", "terrainOutsideColor": "#e4d6b8", "terrainSideColor": "#b89c72",
        "roadColor": "#6b6052", "parkColor": "#8a9560", "sportColor": "#7a8550",
        "roofTexture": "ClayTile", "assetTheme": "Civic Heritage",
    },
}


def resolve_theme(name: str) -> tuple:
    """Return (theme_name, palette) for a requested theme, falling back to default."""
    name = name if name in _THEMES else DEFAULT_THEME
    return name, _THEMES[name]


class BuildError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Runs: one folder per build under the QGIS profile
# --------------------------------------------------------------------------
RUNS_SUBDIR = ("osm_3d_model", "runs")
GPKG_NAME = "layers.gpkg"
MANIFEST_RELPATH = ("data", "planx_manifest.json")
#: The viewer and its bundled sample city; a build never writes here.
BUNDLED_WEB_ROOT = Path(__file__).resolve().parent / "web"


def runs_root() -> Path:
    """``<QGIS profile>/osm_3d_model/runs``, created on demand.

    The profile folder is the one QGIS itself uses for settings, so the runs
    follow the user's profile (and a test's throwaway profile) and survive a
    plugin update, which replaces the plugin folder.
    """
    settings_dir = str(QgsApplication.qgisSettingsDirPath() or "").strip()
    if not settings_dir:
        raise BuildError("QGIS has no profile folder, so the model has nowhere to be saved.")
    root = Path(settings_dir).joinpath(*RUNS_SUBDIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_label(label: str) -> str:
    """A short, filesystem-safe ASCII version of ``label`` ("İzmir Konak" -> "Izmir-Konak")."""
    text = unicodedata.normalize("NFKD", str(label or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-_")
    return text[:40].strip("-_")


def new_run_dir(label: str = "") -> Path:
    """Create and return a new, unique ``runs_root()/YYYYmmdd-HHMMSS[-label]`` folder."""
    root = runs_root()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = _run_label(label)
    base = f"{stamp}-{suffix}" if suffix else stamp
    for index in range(1, 1000):
        candidate = root / (base if index == 1 else f"{base}-{index}")
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise BuildError(f"Could not create a new run folder in {root}.")


def _manifest_is_valid(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and str(payload.get("schema", "")).startswith("planx-3d-city-manifest")


def _run_folders(root: Path) -> List[Path]:
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    return [child for child in children
            if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")]


def latest_run_dir() -> Optional[Path]:
    """The newest run whose ``data/planx_manifest.json`` is valid, or None.

    The manifest is written last, so a run that failed half-way is never
    offered as the latest one.
    """
    best = None
    for child in _run_folders(runs_root()):
        manifest = child.joinpath(*MANIFEST_RELPATH)
        if not manifest.is_file() or not _manifest_is_valid(manifest):
            continue
        try:
            key = (manifest.stat().st_mtime, child.name)
        except OSError:
            continue
        if best is None or key > best[0]:
            best = (key, child)
    return best[1] if best else None


def _run_folders_in_project(root: Path) -> set:
    """Run folders that a layer of the open project reads from."""
    used = set()
    try:
        root_resolved = root.resolve()
    except OSError:
        return used
    for layer in QgsProject.instance().mapLayers().values():
        source = str(layer.source() or "").split("|", 1)[0]
        if not source:
            continue
        with contextlib.suppress(OSError, ValueError):
            relative = Path(source).resolve().relative_to(root_resolved)
            if relative.parts:
                used.add(root_resolved / relative.parts[0])
    return used


#: Folder names new_run_dir() gives: date-time, an optional label and counter.
_RUN_NAME = re.compile(r"^\d{8}-\d{6}(?:-[^\\/]*)?$")
_REPARSE_POINT = 0x400  # stat.FILE_ATTRIBUTE_REPARSE_POINT on Windows


def _is_link(path: Path) -> bool:
    """A symlink, or on Windows any reparse point (a junction included;
    Path.is_junction only arrived in Python 3.12)."""
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(str(path)), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & _REPARSE_POINT)


def delete_runs_except(keep: Optional[Path]) -> int:
    """Delete every run folder except ``keep``; return how many were removed.

    A run that a layer of the open project still reads from is kept, and so is
    a run with a file open in another program: the folder is first renamed
    (which Windows refuses while a file inside is open) and only then deleted,
    so a run is either removed completely or left intact.
    """
    root = runs_root()
    keep_resolved = None
    if keep is not None:
        with contextlib.suppress(OSError):
            keep_resolved = Path(keep).resolve()
    in_use = _run_folders_in_project(root)
    removed = 0
    try:
        children = sorted(root.iterdir())
    except OSError:
        return 0
    for child in children:
        # only run folders this plugin made, and leftovers of an earlier delete
        if not (_RUN_NAME.match(child.name) or child.name.startswith(".deleting-")):
            continue
        if not child.is_dir() or _is_link(child):
            continue
        try:
            resolved = child.resolve()
        except OSError:
            continue
        if resolved == keep_resolved or resolved in in_use:
            continue
        doomed = root / f".deleting-{uuid.uuid4().hex}"
        try:
            child.rename(doomed)
        except OSError:
            continue  # something inside is open; leave the whole run as it is
        shutil.rmtree(doomed, ignore_errors=True)
        if not child.name.startswith("."):
            removed += 1
    return removed


# --------------------------------------------------------------------------
# Inscribed circle
# --------------------------------------------------------------------------
def _boundary_line(geom: QgsGeometry) -> QgsGeometry:
    line_geom = getattr(getattr(QgsWkbTypes, "GeometryType", QgsWkbTypes), "LineGeometry")
    line = geom.convertToType(line_geom, True)
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
    with contextlib.suppress(OSError):
        path.unlink()


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
    # The field is declared in the provider URI: no QVariant/QMetaType type
    # enum is needed, which keeps QGIS 3.28 and QGIS 4 on the same code path.
    layer = QgsVectorLayer(f"MultiPolygon?crs=EPSG:{epsg_dest}&field=name:string(80)", "Study area", "memory")
    pr = layer.dataProvider()
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
def _short_reason(exc: BaseException) -> str:
    """One line of an exception, for a plain sentence ("could not clip it (...)")."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= 160 else text[:157] + "..."


def _dem_overlaps(dem_layer, bbox: QgsRectangle, epsg_dest: int) -> Optional[bool]:
    """True/False when the DEM's extent can be compared with the area, else None."""
    with contextlib.suppress(Exception):
        transform = QgsCoordinateTransform(
            dem_layer.crs(), QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest), QgsProject.instance())
        extent = transform.transformBoundingBox(dem_layer.extent())
        if extent.isEmpty() or not all(math.isfinite(v) for v in (
                extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum())):
            return None
        return bool(extent.intersects(bbox))
    return None


def _export_dem(dem_layer, base_utm: QgsGeometry, epsg_dest: int, dem_path: Path,
                feedback=None) -> tuple:
    """Warp/clip the DEM to the model base bbox in the target UTM CRS.

    Returns ``(True, None)`` on success, else ``(False, sentence)`` where the
    sentence tells the user, in plain words, why the DEM was left out.  The
    DEM is optional: a failure never blocks the export.
    """
    warning = _dem_problem(dem_layer, base_utm, epsg_dest)
    if warning is None:
        try:
            _clip_dem(dem_layer, base_utm, epsg_dest, dem_path)
        except Exception as exc:  # DEM is optional; never block the export.
            warning = f"The DEM was skipped: QGIS could not clip it ({_short_reason(exc)})."
    if warning is not None:
        if feedback:
            feedback(warning)
        return False, warning
    return True, None


def _dem_problem(dem_layer, base_utm: QgsGeometry, epsg_dest: int) -> Optional[str]:
    if not hasattr(dem_layer, "isValid") or not dem_layer.isValid():
        return "The DEM was skipped: the layer could not be read (its file may have moved)."
    if _dem_overlaps(dem_layer, base_utm.boundingBox(), epsg_dest) is False:
        return "The DEM was skipped: it does not overlap the area."
    return None


def _clip_dem(dem_layer, base_utm: QgsGeometry, epsg_dest: int, dem_path: Path) -> None:
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
            raise BuildError("QGIS did not create the clipped file")
        tmp_path.replace(dem_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            _remove_file(tmp_path)


# --------------------------------------------------------------------------
# Optional basemap underlay
# --------------------------------------------------------------------------
#: The whole basemap step (probe + full render) never takes longer than this.
BASEMAP_DEADLINE_S = 20.0
#: A small first render of the same area: when its tiles do not arrive (or it
#: draws nothing) the full render is not started, so an unreachable tile
#: server costs a handful of requests instead of hundreds.
BASEMAP_PROBE_S = 6.0
BASEMAP_PROBE_PX = 128
BASEMAP_MAX_PX = 2048


def _event_loop_flags():
    from qgis.PyQt.QtCore import QEventLoop

    flags = getattr(QEventLoop, "ProcessEventsFlag", QEventLoop)
    return getattr(flags, "ExcludeUserInputEvents")


def _render_layer_image(layer, extent: QgsRectangle, epsg_dest: int, width: int, height: int,
                        deadline_s: float):
    """Render ``layer`` asynchronously; return ``(image or None, timed_out)``.

    QGIS keeps painting and answering the window manager while the tiles
    arrive (a local event loop that leaves out user input, the same rule the
    dialog's progress updates follow).  When ``deadline_s`` passes, the render
    is cancelled.
    """
    from qgis.core import QgsMapRendererParallelJob, QgsMapSettings
    from qgis.PyQt.QtCore import QEventLoop, QSize, QTimer
    from qgis.PyQt.QtGui import QColor

    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest))
    settings.setExtent(extent)
    settings.setOutputSize(QSize(int(width), int(height)))
    settings.setBackgroundColor(QColor(0, 0, 0, 0))  # transparent where the layer has no data
    with contextlib.suppress(Exception):
        settings.setFlag(QgsMapSettings.Flag.Antialiasing, True)

    job = QgsMapRendererParallelJob(settings)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    job.finished.connect(loop.quit)
    job.start()
    timer.start(max(1, int(deadline_s * 1000)))
    if job.isActive():
        loop.exec(_event_loop_flags())
    timer.stop()
    if job.isActive():
        job.cancelWithoutBlocking()
        grace = time.monotonic() + 2.0
        while job.isActive() and time.monotonic() < grace:
            loop.processEvents(_event_loop_flags(), 50)
        if job.isActive():
            job.cancel()  # last resort: waits for the render threads
        return None, True
    return job.renderedImage(), False


def _image_bytes(image) -> bytes:
    from qgis.PyQt.QtGui import QImage

    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    size = image.sizeInBytes() if hasattr(image, "sizeInBytes") else image.byteCount()
    pointer = image.constBits()
    if hasattr(pointer, "setsize"):
        pointer.setsize(size)
    return bytes(pointer.asstring(size))


def _image_is_transparent(image) -> bool:
    """True when every pixel is fully transparent (premultiplied ARGB 0)."""
    raw = _image_bytes(image)
    return not raw or raw.count(0) == len(raw)


def _image_is_blank(image) -> bool:
    """True when the image is fully transparent or one single colour."""
    raw = _image_bytes(image)
    if len(raw) < 4:
        return True
    return raw == raw[:4] * (len(raw) // 4)


def _basemap_size(bbox: QgsRectangle, max_px: int, min_px: int) -> tuple:
    w_m, h_m = bbox.width(), bbox.height()
    if w_m >= h_m:
        return max_px, max(min_px, int(round(max_px * h_m / w_m)))
    return max(min_px, int(round(max_px * w_m / h_m))), max_px


def _export_basemap(basemap_layer, base_utm: QgsGeometry, epsg_dest: int,
                    out_path: Path, feedback=None) -> tuple:
    """Render a basemap layer to a transparent PNG over the model base bbox (UTM).

    Works for raster, XYZ/tiled and vector layers via the QGIS map renderer,
    under ``BASEMAP_DEADLINE_S`` and without freezing QGIS.  Returns
    ``(bbox dict, None)`` on success, else ``(None, sentence)`` where the
    sentence says in plain words why there is no basemap.  A blank result
    (fully transparent or a single colour) is never published.
    """
    try:
        bbox, warning = _render_basemap(basemap_layer, base_utm, epsg_dest, out_path)
    except Exception as exc:  # basemap is optional; never block the export.
        bbox, warning = None, f"The basemap was skipped: QGIS could not draw it ({_short_reason(exc)})."
    if warning is not None:
        _remove_file(out_path)
        if feedback:
            feedback(warning)
    return bbox, warning


def _render_basemap(basemap_layer, base_utm: QgsGeometry, epsg_dest: int, out_path: Path) -> tuple:
    if basemap_layer is None or not hasattr(basemap_layer, "isValid") or not basemap_layer.isValid():
        return None, "The basemap was skipped: the layer could not be read."
    bbox = base_utm.boundingBox()
    if bbox.width() <= 0 or bbox.height() <= 0:
        return None, "The basemap was skipped: the model area is empty."
    started = time.monotonic()

    probe_w, probe_h = _basemap_size(bbox, BASEMAP_PROBE_PX, 32)
    probe, timed_out = _render_layer_image(basemap_layer, bbox, epsg_dest, probe_w, probe_h,
                                           min(BASEMAP_PROBE_S, BASEMAP_DEADLINE_S))
    if timed_out:
        return None, (f"The basemap was skipped: its map tiles did not arrive within "
                      f"{BASEMAP_PROBE_S:.0f} seconds. Check the internet connection, "
                      f"or choose another basemap layer.")
    if probe is None or probe.isNull() or _image_is_transparent(probe):
        return None, ("The basemap was skipped: it drew nothing in this area (the layer has no data "
                      "here, or its tile server refused the requests).")

    remaining = BASEMAP_DEADLINE_S - (time.monotonic() - started)
    if remaining <= 0.5:
        return None, f"The basemap was skipped: it did not finish drawing within {BASEMAP_DEADLINE_S:.0f} seconds."
    width, height = _basemap_size(bbox, BASEMAP_MAX_PX, 256)
    image, timed_out = _render_layer_image(basemap_layer, bbox, epsg_dest, width, height, remaining)
    if timed_out:
        return None, f"The basemap was skipped: it did not finish drawing within {BASEMAP_DEADLINE_S:.0f} seconds."
    if image is None or image.isNull() or _image_is_blank(image):
        return None, ("The basemap was skipped: it came out blank (a single colour or nothing at all), "
                      "so it was not used.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(out_path.parent), prefix=".basemap-", suffix=".png", delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
        if not image.save(str(tmp_path), "PNG") or tmp_path.stat().st_size <= 0:
            return None, "The basemap was skipped: QGIS could not save the image."
        tmp_path.replace(out_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            _remove_file(tmp_path)
    return {
        "minx": bbox.xMinimum(), "miny": bbox.yMinimum(),
        "maxx": bbox.xMaximum(), "maxy": bbox.yMaximum(),
    }, None


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


def _write_manifest(run_root: Path, epsg_dest: int, latitude: float, has_dem: bool,
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
        # OSM-native column names (the export carries raw OSM tags). The viewer
        # maps these through to floors/function/width.
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
        "summary": {"source": "OpenStreetMap", "epsg": epsg_dest, "hasDem": has_dem,
                    "attribution": OSM_ATTRIBUTION},
    }
    manifest_path = run_root.joinpath(*MANIFEST_RELPATH)
    _write_json_atomic(manifest_path, manifest)
    return manifest_path


OSM_ATTRIBUTION = "© OpenStreetMap contributors"
PROJECT_OWNED_PROPERTY = "osm_3d_model/owned"

# role in the download dict -> (GeoPackage table, project layer name), in the
# legend order: point furniture and roads above polygons, the study-area base
# at the very bottom.
PROJECT_LAYERS = (
    ("lights", "street_lights", "OSM Street Lights"),
    ("busstops", "bus_stops", "OSM Bus Stops"),
    ("benches", "benches", "OSM Benches"),
    ("trashbins", "trash_bins", "OSM Trash Bins"),
    ("trees", "trees", "OSM Trees"),
    ("bikelanes", "bike_lanes", "OSM Bike Lanes"),
    ("roads", "roads", "OSM Roads"),
    ("buildings", "buildings", "OSM Buildings"),
    ("waterlines", "waterways", "OSM Waterways"),
    ("greens", "greens_water", "OSM Greens & Water"),
    ("roi", "study_area", "OSM Study Area"),
)

# GeoJSON file the viewer reads -> role in the download dict.
VIEWER_LAYERS = (
    ("roi", "roi"),
    ("mybuildings", "buildings"), ("myroads", "roads"),
    ("mytrees", "trees"), ("myblocks", "greens"),
    ("mywaterlines", "waterlines"), ("mybikelanes", "bikelanes"),
    ("mybusstops", "busstops"), ("mybenches", "benches"),
    ("mylights", "lights"), ("mytrashbins", "trashbins"),
)


def _writer_enum(scope: str, name: str):
    """``QgsVectorFileWriter.<scope>.<name>`` (QGIS 4), else the unscoped member (QGIS 3)."""
    owner = getattr(QgsVectorFileWriter, scope, None)
    value = getattr(owner, name, None) if owner is not None else None
    return value if value is not None else getattr(QgsVectorFileWriter, name)


def _is_point_layer(layer) -> bool:
    return QgsWkbTypes.displayString(QgsWkbTypes.flatType(layer.wkbType())).lower().endswith("point")


def _write_geopackage(gpkg_path: Path, tables: list) -> None:
    """Write ``(table, layer)`` pairs into one GeoPackage, published at once.

    The file is assembled under a temporary name next to the target and then
    renamed, so a reader never sees a half-written GeoPackage.
    """
    gpkg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = gpkg_path.with_name(f".{gpkg_path.stem}-{uuid.uuid4().hex}.gpkg")
    context = QgsProject.instance().transformContext()
    no_error = _writer_enum("WriterError", "NoError")
    try:
        for index, (table, layer) in enumerate(tables):
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = table
            options.fileEncoding = "UTF-8"
            options.actionOnExistingFile = _writer_enum(
                "ActionOnExistingFile", "CreateOrOverwriteFile" if index == 0 else "CreateOrOverwriteLayer")
            # Clipping can turn a line or polygon into a multi-part one inside
            # a single-part memory layer; a GeoPackage table needs one type.
            options.forceMulti = not _is_point_layer(layer)
            result = QgsVectorFileWriter.writeAsVectorFormatV3(layer, str(tmp_path), context, options)
            error = result[0] if isinstance(result, tuple) else result
            message = str(result[1]) if isinstance(result, tuple) and len(result) > 1 else ""
            if error != no_error:
                detail = f": {message}" if message else ""
                raise BuildError(f"QGIS could not write the {table} table of {gpkg_path.name}{detail}")
        if not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
            raise BuildError(f"QGIS did not create {gpkg_path.name}.")
        try:
            tmp_path.replace(gpkg_path)
        except OSError as exc:
            raise BuildError(
                f"{gpkg_path} is open in another program, so the layers could not be saved "
                f"({_short_reason(exc)}).") from exc
        tmp_path = None
    finally:
        if tmp_path is not None:
            for leftover in (tmp_path, tmp_path.with_name(tmp_path.name + "-wal"),
                             tmp_path.with_name(tmp_path.name + "-shm")):
                _remove_file(leftover)


def _gpkg_layer(gpkg_path: Path, table: str, name: str) -> QgsVectorLayer:
    layer = QgsVectorLayer(f"{gpkg_path.as_posix()}|layername={table}", name, "ogr")
    if not layer.isValid():
        raise BuildError(f"QGIS could not open the {table} table of {gpkg_path}.")
    return layer


def _remove_owned_layers(project: QgsProject) -> None:
    root = project.layerTreeRoot()
    for layer in list(project.mapLayers().values()):
        if layer.customProperty(PROJECT_OWNED_PROPERTY, False):
            project.removeMapLayer(layer.id())
    for node in list(root.children()):
        if node.customProperty(PROJECT_OWNED_PROPERTY, False):
            root.removeChildNode(node)


def _replace_project_layers(project: QgsProject, gpkg_path: Path, feature_counts: dict,
                            theme_name: str, theme_palette: dict) -> None:
    """Replace the previous run with this run's layers, read from its GeoPackage.

    The layers keep the 1.9.1 names and the native styling of qgis_styling, and
    because they live in a file (not in memory) they come back with their
    features when the project is saved and opened again.  Empty layers stay in
    the GeoPackage but are not added to the project (as in 1.9.1); the study
    area always is.
    """
    loaded = {role: _gpkg_layer(gpkg_path, table, label) for role, table, label in PROJECT_LAYERS
              if role == "roi" or feature_counts.get(role, 0) > 0}
    roi_layer = loaded.pop("roi")
    style_export_layers(roi_layer, loaded, theme_name, theme_palette)

    _remove_owned_layers(project)
    group = project.layerTreeRoot().insertGroup(0, MODEL_NAME)
    group.setCustomProperty(PROJECT_OWNED_PROPERTY, True)
    group.setCustomProperty("osm_3d_model/theme", theme_name)
    group.setCustomProperty("osm_3d_model/run_dir", str(gpkg_path.parent))
    for role, _table, _label in PROJECT_LAYERS:
        layer = roi_layer if role == "roi" else loaded.get(role)
        if layer is None:
            continue
        layer.setCustomProperty(PROJECT_OWNED_PROPERTY, True)
        project.addMapLayer(layer, False)
        group.addLayer(layer)


def _write_viewer_layers(run_root: Path, layers: dict) -> None:
    """Write the viewer's GeoJSON files and publish ``data/layers`` in one step."""
    vector_dir = run_root / "data" / "layers"
    vector_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=".layers-", dir=str(vector_dir.parent)))
    try:
        for name, role in VIEWER_LAYERS:
            out_path = staging_dir / f"{name}.geojson"
            save_layer_to_geojson(layers[role], out_path)
            _validate_geojson(out_path)
        try:
            _publish_directory(staging_dir, vector_dir)
        except OSError as exc:
            raise BuildError(
                f"A file in {vector_dir} is open in another program, so the layers could not be "
                f"replaced ({_short_reason(exc)}).") from exc
        staging_dir = None
    finally:
        if staging_dir is not None and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def _count(counts: dict, key: str) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return int(counts.get(key) or 0)
    return 0


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def build_and_export(source_geom: QgsGeometry, source_crs: QgsCoordinateReferenceSystem,
                     web_root: Optional[str] = None, dem_layer=None, basemap_layer=None,
                     max_ha: float = MAX_STUDY_HA_DEFAULT,
                     add_to_project: bool = True, shape: str = SHAPE_CIRCLE,
                     theme: str = DEFAULT_THEME, use_cache: bool = True,
                     feedback=None, prepared: Optional[dict] = None,
                     osm_payload: Optional[dict] = None,
                     run_dir: Optional[Path] = None) -> dict:
    """Download, clip and publish one run; return the summary.

    ``run_dir`` is the folder the run is written to (``None``: a new folder
    from :func:`new_run_dir`).  ``web_root`` is accepted for compatibility with
    1.9.1 callers and is never written: the plugin's ``web/data`` stays the
    bundled sample city.  The run holds ``data/planx_manifest.json``,
    ``data/layers/*.geojson``, ``data/dem/mydem.tif`` and
    ``data/basemap/basemap.png`` for the viewer, and ``layers.gpkg`` with every
    QGIS layer; the project layers load from that GeoPackage.

    DEM and basemap problems never stop the export: each becomes a plain
    sentence in ``summary["warnings"]``.
    """
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
    counts = dict(osm.get("counts") or {})
    layers = {role: osm[role] for _name, role in VIEWER_LAYERS if role != "roi"}
    layers["roi"] = _roi_layer_from_polygon(base_utm, epsg_dest)

    created_run = run_dir is None
    run_root = new_run_dir() if created_run else Path(run_dir)
    with contextlib.suppress(OSError):
        if run_root.resolve() == BUNDLED_WEB_ROOT.resolve():
            raise BuildError("The bundled sample city is never overwritten; build into a run folder.")
    run_root.mkdir(parents=True, exist_ok=True)
    gpkg_path = run_root / GPKG_NAME
    warnings: List[str] = []
    try:
        _write_viewer_layers(run_root, layers)
        if feedback:
            feedback("Saving the layers to a GeoPackage...")
        _write_geopackage(gpkg_path, [(table, layers[role]) for role, table, _label in PROJECT_LAYERS])

        # Optional DEM.
        has_dem = False
        dem_path = run_root / "data" / "dem" / "mydem.tif"
        if dem_layer is not None:
            has_dem, warning = _export_dem(dem_layer, base_utm, epsg_dest, dem_path, feedback=feedback)
            if warning:
                warnings.append(warning)
        if not has_dem:
            _remove_file(dem_path)

        # Optional basemap underlay rendered to a PNG over the base bbox.
        basemap_bbox = None
        basemap_path = run_root / "data" / "basemap" / "basemap.png"
        if basemap_layer is not None:
            if feedback:
                feedback("Rendering basemap underlay...")
            basemap_bbox, warning = _export_basemap(
                basemap_layer, base_utm, epsg_dest, basemap_path, feedback=feedback)
            if warning:
                warnings.append(warning)
        if basemap_bbox is None:
            _remove_file(basemap_path)

        # The manifest is written last: a run without one is incomplete and
        # is never offered by latest_run_dir().
        _write_manifest(run_root, epsg_dest, cen_wgs.y(), has_dem, theme_name, basemap_bbox)
    except BaseException:
        if created_run:
            shutil.rmtree(run_root, ignore_errors=True)
        raise

    if add_to_project:
        _replace_project_layers(project, gpkg_path,
                                {role: layer.featureCount() for role, layer in layers.items()},
                                theme_name, theme_palette)

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
        "has_basemap": basemap_bbox is not None,
        "run_dir": str(run_root),
        "gpkg_path": str(gpkg_path),
        "warnings": warnings,
        "buildings": _count(counts, "buildings"),
        "estimated_heights": _count(counts, "estimated_heights"),
        "osm_trees": _count(counts, "osm_trees"),
        "planted_trees": _count(counts, "planted_trees"),
    }
