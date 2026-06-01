# -*- coding: utf-8 -*-
"""OpenStreetMap download + clip for the 3D OSM Model plugin.

Fetches buildings, roads, greens and trees from the public Overpass API for the
bounding box of a study circle, reprojects to a metric UTM CRS, and clips every
feature to the circle. Building floor count defaults to 3 when OSM has no level
data, matching the viewer's expectation.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

# Public Overpass mirrors, tried in order. The main instance is frequently rate
# limited (HTTP 429) or slow; falling back to mirrors makes the one-button flow
# far more reliable. The first endpoint that answers with valid JSON wins.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "3D-OSM-Model-QGIS-Plugin/0.3.0 (https://github.com/YusufEminoglu/planx_3d_city)"
DEFAULT_TIMEOUT_S = 60


class OsmDownloadError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Geo helpers
# --------------------------------------------------------------------------
def utm_epsg_for(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180.0) / 6.0) + 1)
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0 else 32700) + zone


# --------------------------------------------------------------------------
# Overpass query + fetch
# --------------------------------------------------------------------------
def _overpass_query(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> str:
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    return f"""
[out:json][timeout:{DEFAULT_TIMEOUT_S}];
(
  way["building"]({bbox});
  relation["building"]({bbox});
  way["highway"]({bbox});
  way["leisure"~"park|garden|playground|pitch"]({bbox});
  way["landuse"~"forest|grass|meadow|recreation_ground|cemetery"]({bbox});
  way["natural"~"wood|scrub"]({bbox});
  node["natural"="tree"]({bbox});
);
out body geom;
""".strip()


def _fetch_one(endpoint: str, query: str, timeout_s: int) -> dict:
    parsed_endpoint = urllib.parse.urlparse(endpoint)
    if parsed_endpoint.scheme not in {"https", "http"} or not parsed_endpoint.netloc:
        raise OsmDownloadError(f"Invalid Overpass endpoint URL: {endpoint}")
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s + 10) as resp:  # nosec B310 - scheme validated above.
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise OsmDownloadError("rate-limited (HTTP 429)") from exc
        raise OsmDownloadError(f"HTTP {exc.code}: {exc.reason}") from exc
    except Exception as exc:
        raise OsmDownloadError(f"fetch failed: {exc}") from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise OsmDownloadError(f"non-JSON response ({len(payload)} bytes)") from exc


def fetch_overpass(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                   timeout_s: int = DEFAULT_TIMEOUT_S, feedback=None) -> dict:
    """Query Overpass, falling back across mirrors until one answers with JSON."""
    query = _overpass_query(min_lat, min_lon, max_lat, max_lon)
    last_error = None
    for index, endpoint in enumerate(OVERPASS_ENDPOINTS):
        host = urllib.parse.urlparse(endpoint).netloc or endpoint
        if feedback:
            prefix = "Querying" if index == 0 else f"Mirror {index} —"
            feedback(f"{prefix} {host} ...")
        try:
            return _fetch_one(endpoint, query, timeout_s)
        except OsmDownloadError as exc:
            last_error = exc
            if feedback and index + 1 < len(OVERPASS_ENDPOINTS):
                feedback(f"{host} {exc}; trying next mirror...")
    raise OsmDownloadError(
        f"All Overpass endpoints failed (last: {last_error}). Wait a minute and retry, "
        "or pick a smaller area."
    )


# --------------------------------------------------------------------------
# OSM element -> attributes
# --------------------------------------------------------------------------
def _building_floors(tags: dict) -> int:
    for key in ("building:levels", "levels"):
        value = tags.get(key)
        if value:
            try:
                return max(1, int(float(str(value).split(";")[0])))
            except (ValueError, TypeError):
                pass
    height = tags.get("height")
    if height:
        try:
            return max(1, int(round(float(str(height).rstrip(" m")) / 3.0)))
        except (ValueError, TypeError):
            pass
    return 3


def _building_function(tags: dict) -> str:
    raw = (tags.get("building") or "").lower()
    if raw in ("apartments", "residential", "house", "detached", "terrace", "dormitory"):
        return "KONUT"
    if raw in ("commercial", "retail", "supermarket", "kiosk", "office"):
        return "TICARET"
    if raw in ("school", "university", "college", "kindergarten"):
        return "EGITIM"
    if raw in ("hospital", "clinic"):
        return "SAGLIK"
    if raw in ("industrial", "warehouse", "manufacture"):
        return "SANAYI"
    if raw in ("mosque", "church", "temple", "synagogue", "cathedral", "chapel"):
        return "DINI"
    if raw in ("public", "civic", "government", "townhall"):
        return "KAMU"
    if raw in ("yes", "") and tags.get("amenity"):
        amenity = tags["amenity"].lower()
        if amenity in ("school", "university"):
            return "EGITIM"
        if amenity in ("hospital", "clinic"):
            return "SAGLIK"
        if amenity in ("place_of_worship",):
            return "DINI"
    return "KARMA"


def _road_class(tags: dict) -> str:
    return (tags.get("highway") or "").lower() or "unknown"


def _green_function(tags: dict) -> str:
    if tags.get("leisure") in ("park", "garden", "playground"):
        return "PARK"
    if tags.get("leisure") == "pitch":
        return "SPOR"
    if tags.get("landuse") in ("forest", "grass", "meadow", "recreation_ground"):
        return "YESIL_ALAN"
    if tags.get("landuse") == "cemetery":
        return "MEZARLIK"
    if tags.get("natural") in ("wood", "scrub"):
        return "ORMAN"
    return "YESIL_ALAN"


def _way_polygon(element) -> QgsGeometry | None:
    geometry = element.get("geometry") or []
    if len(geometry) < 3:
        return None
    points = [QgsPointXY(pt["lon"], pt["lat"]) for pt in geometry]
    if points[0] != points[-1]:
        points.append(points[0])
    return QgsGeometry.fromPolygonXY([points])


def _way_polyline(element) -> QgsGeometry | None:
    geometry = element.get("geometry") or []
    if len(geometry) < 2:
        return None
    points = [QgsPointXY(pt["lon"], pt["lat"]) for pt in geometry]
    return QgsGeometry.fromPolylineXY(points)


def _node_point(element) -> QgsGeometry | None:
    if "lon" not in element or "lat" not in element:
        return None
    return QgsGeometry.fromPointXY(QgsPointXY(element["lon"], element["lat"]))


def _make_layer(name: str, wkb_type: str, epsg_dest: int, fields_def):
    layer = QgsVectorLayer(f"{wkb_type}?crs=EPSG:{epsg_dest}", name, "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField(field_name, qvariant) for field_name, qvariant in fields_def])
    layer.updateFields()
    return layer, provider


def save_layer_to_geojson(layer: QgsVectorLayer, path) -> None:
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GeoJSON"
    options.fileEncoding = "UTF-8"
    QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, str(path), QgsProject.instance().transformContext(), options
    )


# --------------------------------------------------------------------------
# Main download + clip
# --------------------------------------------------------------------------
def download_osm_for_circle(circle_utm: QgsGeometry, epsg_dest: int, feedback=None) -> dict:
    """Fetch OSM for the circle's bbox, reproject to UTM, clip to the circle.

    ``circle_utm`` is the study-circle polygon in the EPSG:``epsg_dest`` metric CRS.
    Returns a dict of memory layers keyed by role plus feature counts.
    """
    project = QgsProject.instance()
    dst_crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest)
    wgs_crs = QgsCoordinateReferenceSystem.fromEpsgId(4326)
    to_wgs = QgsCoordinateTransform(dst_crs, wgs_crs, project)
    to_utm = QgsCoordinateTransform(wgs_crs, dst_crs, project)

    wgs_rect = to_wgs.transformBoundingBox(circle_utm.boundingBox())
    min_lon, min_lat = wgs_rect.xMinimum(), wgs_rect.yMinimum()
    max_lon, max_lat = wgs_rect.xMaximum(), wgs_rect.yMaximum()

    payload = fetch_overpass(min_lat, min_lon, max_lat, max_lon, feedback=feedback)
    elements = payload.get("elements") or []
    if not elements:
        raise OsmDownloadError("Overpass returned 0 elements for this area. Try a different or larger area.")

    buildings_layer, b_pr = _make_layer(
        "OSM Buildings", "Polygon", epsg_dest,
        [("osm_id", QVariant.String), ("katadedi", QVariant.Int),
         ("uipfonksiyon", QVariant.String), ("name", QVariant.String)],
    )
    roads_layer, r_pr = _make_layer(
        "OSM Roads", "LineString", epsg_dest,
        [("osm_id", QVariant.String), ("yol_turu", QVariant.String), ("name", QVariant.String)],
    )
    greens_layer, g_pr = _make_layer(
        "OSM Greens", "Polygon", epsg_dest,
        [("osm_id", QVariant.String), ("uipfonksiyon", QVariant.String), ("name", QVariant.String)],
    )
    trees_layer, t_pr = _make_layer(
        "OSM Trees", "Point", epsg_dest,
        [("osm_id", QVariant.String), ("height", QVariant.Double)],
    )

    counts = {"buildings": 0, "roads": 0, "greens": 0, "trees": 0, "skipped": 0}

    def clip_to_circle(geom_wgs: QgsGeometry):
        g = QgsGeometry(geom_wgs)
        if g.transform(to_utm):
            return None
        if not g.intersects(circle_utm):
            return None
        clipped = g.intersection(circle_utm)
        if clipped.isEmpty() or not clipped.isGeosValid():
            # Fall back to the original geometry if the intersection is degenerate.
            return g if g.within(circle_utm) else None
        return clipped

    for element in elements:
        etype = element.get("type")
        tags = element.get("tags") or {}

        if etype == "node" and tags.get("natural") == "tree":
            geom = _node_point(element)
            if not geom:
                continue
            g = QgsGeometry(geom)
            if g.transform(to_utm) or not circle_utm.contains(g):
                counts["skipped"] += 1
                continue
            height = tags.get("height")
            try:
                height_val = float(str(height).rstrip(" m")) if height else 6.0
            except (ValueError, TypeError):
                height_val = 6.0
            feat = QgsFeature()
            feat.setGeometry(g)
            feat.setAttributes([str(element.get("id", "")), round(height_val, 1)])
            t_pr.addFeatures([feat])
            counts["trees"] += 1
            continue

        if etype in ("way", "relation") and tags.get("building"):
            base = _way_polygon(element)
            if not base:
                continue
            clipped = clip_to_circle(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")),
                _building_floors(tags),
                _building_function(tags),
                tags.get("name", ""),
            ])
            b_pr.addFeatures([feat])
            counts["buildings"] += 1
            continue

        if etype == "way" and tags.get("highway"):
            base = _way_polyline(element)
            if not base:
                continue
            clipped = clip_to_circle(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([str(element.get("id", "")), _road_class(tags), tags.get("name", "")])
            r_pr.addFeatures([feat])
            counts["roads"] += 1
            continue

        if etype == "way" and (tags.get("leisure") or tags.get("landuse") or tags.get("natural")):
            base = _way_polygon(element)
            if not base:
                continue
            clipped = clip_to_circle(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([str(element.get("id", "")), _green_function(tags), tags.get("name", "")])
            g_pr.addFeatures([feat])
            counts["greens"] += 1
            continue

        counts["skipped"] += 1

    for layer in (buildings_layer, roads_layer, greens_layer, trees_layer):
        layer.updateExtents()

    return {
        "epsg": epsg_dest,
        "counts": counts,
        "buildings": buildings_layer,
        "roads": roads_layer,
        "greens": greens_layer,
        "trees": trees_layer,
    }
