# -*- coding: utf-8 -*-
"""Study-circle computation, OSM clip/export and viewer manifest writing.

Given a source area geometry (selected polygon or map-canvas extent), this module:
  1. picks a metric UTM CRS,
  2. fits the largest inscribed circle inside the area (pole of inaccessibility),
  3. clamps it to a maximum study area (~300 ha),
  4. downloads + clips OSM data to the circle,
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
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

from .osm_download import connect_roads, download_osm_for_circle, save_layer_to_geojson, utm_epsg_for

MODEL_NAME = "3D OSM Model"
MAX_STUDY_HA_DEFAULT = 300.0
MIN_RADIUS_M = 30.0

# Salmon-tinted grey ("yavru agzi") palette for the viewer defaults.
THEME = {
    "islandColor": "#dcc7bd",
    "terrainOutsideColor": "#e7d7d0",
    "terrainSideColor": "#b8978a",
    "roadColor": "#9a8c84",
    "parkColor": "#9fae8a",
    "sportColor": "#8fa07c",
}


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
# GeoJSON writing
# --------------------------------------------------------------------------
INIT_LOADED_FILES = (
    "myblocks", "mybuildings", "myroads", "mytrees", "mylights", "mybenches",
    "mytrashbins", "mybusstops", "myfences", "mywaterlines", "mymosques", "mytumulus",
    "mysidewalks",
)


def _write_empty_fc(path: Path, name: str) -> None:
    path.write_text(
        json.dumps({"type": "FeatureCollection", "name": name, "features": []}, ensure_ascii=False),
        encoding="utf-8",
    )


def _roi_layer_from_circle(circle_utm: QgsGeometry, epsg_dest: int) -> QgsVectorLayer:
    layer = QgsVectorLayer(f"Polygon?crs=EPSG:{epsg_dest}", "Study circle", "memory")
    pr = layer.dataProvider()
    pr.addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()
    feat = QgsFeature()
    feat.setGeometry(circle_utm)
    feat.setAttributes(["3D OSM Model study circle"])
    pr.addFeatures([feat])
    layer.updateExtents()
    return layer


# --------------------------------------------------------------------------
# Optional DEM clip
# --------------------------------------------------------------------------
def _export_dem(dem_layer, circle_utm: QgsGeometry, epsg_dest: int, dem_path: Path, feedback=None) -> bool:
    """Best-effort DEM warp/clip to the circle bbox in the target UTM CRS."""
    try:
        import processing  # noqa: WPS433 (QGIS runtime import)

        bbox = circle_utm.boundingBox()
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
def _viewer_defaults(latitude: float, has_dem: bool) -> dict:
    return {
        # Visible content
        "showBuildings": True,
        "showRoads": True,
        "showTrees": True,
        "showIslands": True,
        # Circular study area + solid base
        "showTerrainSides": True,
        "terrainSideDrop": 8.0,
        "terrainSideColor": THEME["terrainSideColor"],
        "showOutsideRoiTerrain": False,
        "terrainOutsideColor": THEME["terrainOutsideColor"],
        "islandColor": THEME["islandColor"],
        "islandTransparency": 0.0,
        "parkColor": THEME["parkColor"],
        "sportColor": THEME["sportColor"],
        "roadColor": THEME["roadColor"],
        "flattenIslands": True,
        # Procedural detail OFF by default, procedural road traces ON
        "showLedges": False,
        "showStorefronts": False,
        "showRoadMarkings": True,
        "buildingMode": "Extruded + roof",
        # Lively scene: realistic trees plus moving pedestrians and mixed traffic
        # (cars, vans, buses, trucks).
        "treeRenderMode": "Realistic",
        "assetTheme": "Modern Urban",
        "showCars": True,
        # Sparse default traffic: ~1/5 of the previous density (user request).
        "carDensity": 0.09,
        "trafficSpeed": 1.0,
        "showPedestrians": True,
        "pedestrianDensity": 0.5,
        # Keep the rest off so the scene stays fast.
        "showFurniture": False,
        "showCrosswalks": False,
        "showSidewalks": True,
        "showPedestrianPaths": False,
        "showParcels": False,
        "showHardscape": False,
        "showFences": False,
        "showWaterlines": False,
        "showMosques": False,
        "showTumulus": False,
        "demMeshQuality": 140,
        "latitude": float(latitude),
        "dayOfYear": 172,
        # Open at golden hour: warm low sun, long soft shadows on the massing.
        "timeOfDay": 17.0,
        "autoTime": False,
        "fogDensity": 0.00035,
    }


def _write_manifest(web_root: Path, epsg_dest: int, latitude: float, has_dem: bool) -> Path:
    manifest = {
        "schema": "planx-3d-city-manifest/v1",
        "plugin": "osm_3d_model",
        "version": "0.3.0",
        "mode": "vector",
        "flexibleInputs": True,
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": {"title": "3D OSM Model", "fileName": ""},
        "requiredInputs": [],
        "optionalInputs": ["buildings", "roads", "trees", "blocks"],
        "terrainTexture": None,
        "baseMapTexture": None,
        "roadAccess": None,
        "fieldMappings": {
            "building_floors_field": "katadedi",
            "landuse_function_field": "uipfonksiyon",
            "road_hierarchy_field": "yol_turu",
            "block_category_field": "uipfonksiyon",
        },
        "analysisDefaults": {"roadColorMode": "Default"},
        "viewerDefaults": _viewer_defaults(latitude, has_dem),
        "assetTheme": "Modern Urban",
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
                     add_to_project: bool = True, feedback=None) -> dict:
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

    # Source geometry -> UTM, then fit the inscribed circle.
    geom_utm = QgsGeometry(source_geom)
    to_utm = QgsCoordinateTransform(source_crs, dst_crs, project)
    if geom_utm.transform(to_utm):
        raise BuildError("Could not reproject the selected area to a metric CRS.")
    center, radius = largest_inscribed_circle(geom_utm, max_ha)
    circle_utm = QgsGeometry.fromPointXY(center).buffer(radius, 96)
    area_ha = round(math.pi * radius * radius / 10000.0, 1)
    if feedback:
        feedback(f"Study circle: r = {radius:.0f} m ({area_ha} ha), EPSG:{epsg_dest}.")

    # OSM download + clip.
    osm = download_osm_for_circle(circle_utm, epsg_dest, feedback=feedback)
    # Weld road segments into a connected network (snap + dissolve, best-effort).
    osm["roads"] = connect_roads(osm["roads"], tolerance_m=2.0, feedback=feedback)

    # Write GeoJSON the viewer expects.
    web_path = Path(web_root)
    vector_dir = web_path / "data" / "yerlesim"
    vector_dir.mkdir(parents=True, exist_ok=True)

    roi_layer = _roi_layer_from_circle(circle_utm, epsg_dest)
    save_layer_to_geojson(roi_layer, vector_dir / "roi.geojson")
    save_layer_to_geojson(osm["buildings"], vector_dir / "mybuildings.geojson")
    save_layer_to_geojson(osm["roads"], vector_dir / "myroads.geojson")
    save_layer_to_geojson(osm["trees"], vector_dir / "mytrees.geojson")
    save_layer_to_geojson(osm["greens"], vector_dir / "myblocks.geojson")

    written_named = {"mybuildings", "myroads", "mytrees", "myblocks"}
    for name in INIT_LOADED_FILES:
        if name not in written_named:
            _write_empty_fc(vector_dir / f"{name}.geojson", name)

    # Optional DEM.
    has_dem = False
    if dem_layer is not None:
        dem_dir = web_path / "data" / "dem"
        dem_dir.mkdir(parents=True, exist_ok=True)
        has_dem = _export_dem(dem_layer, circle_utm, epsg_dest, dem_dir / "mydem.tif", feedback=feedback)

    _write_manifest(web_path, epsg_dest, cen_wgs.y(), has_dem)

    if add_to_project:
        roi_layer.setName("OSM Study Circle")
        project.addMapLayer(roi_layer)
        for key, label in (("greens", "OSM Greens"), ("buildings", "OSM Buildings"),
                           ("roads", "OSM Roads"), ("trees", "OSM Trees")):
            layer = osm.get(key)
            if layer is not None and layer.featureCount() > 0:
                layer.setName(label)
                project.addMapLayer(layer)

    return {
        "epsg": epsg_dest,
        "radius_m": round(radius, 1),
        "area_ha": area_ha,
        "counts": osm["counts"],
        "has_dem": has_dem,
    }
