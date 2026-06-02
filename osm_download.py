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
USER_AGENT = "3D-OSM-Model-QGIS-Plugin/0.8.2 (https://github.com/YusufEminoglu/osm_3d_model)"
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
  way["waterway"~"river|stream|canal|drain|ditch"]({bbox});
  way["leisure"~"park|garden|playground|pitch"]({bbox});
  way["landuse"~"forest|grass|meadow|recreation_ground|cemetery"]({bbox});
  way["natural"~"wood|scrub"]({bbox});
  node["natural"="tree"]({bbox});
  node["highway"="bus_stop"]({bbox});
  node["amenity"="bench"]({bbox});
  node["highway"="street_lamp"]({bbox});
  node["amenity"="waste_basket"]({bbox});
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
# Default floor counts by OSM ``building`` type, used only when OSM has no
# building:levels / height. Housing reads taller than retail; light industry and
# worship halls read low. Unknown buildings keep the historical default of 3.
_DEFAULT_FLOORS_BY_OSM = {
    "apartments": 4, "residential": 4, "dormitory": 5,
    "house": 2, "detached": 2, "terrace": 2, "semidetached_house": 2, "bungalow": 1,
    "commercial": 3, "office": 5, "retail": 2, "supermarket": 1, "kiosk": 1,
    "industrial": 1, "warehouse": 1, "manufacture": 1, "hangar": 1,
    "school": 3, "university": 4, "college": 4, "kindergarten": 2,
    "hospital": 5, "clinic": 3,
    "church": 1, "mosque": 1, "temple": 1, "synagogue": 1, "cathedral": 2, "chapel": 1,
    "public": 3, "civic": 3, "government": 4, "townhall": 3,
    "garage": 1, "garages": 1, "shed": 1, "hut": 1, "roof": 1, "carport": 1,
}


def _parse_osm_number(value):
    """Parse an OSM numeric tag ('12', '12.5', '12 m', '3;4') -> float or None."""
    if value is None:
        return None
    try:
        return float(str(value).split(";")[0].strip().rstrip(" m").strip())
    except (ValueError, TypeError):
        return None


def _building_levels(tags: dict) -> int:
    """Floor count for the export's OSM-native ``building_levels`` column.

    Priority: building:levels (+ roof:levels) -> height / 3 m -> a default by the
    OSM ``building`` type -> 3.
    """
    base = None
    for key in ("building:levels", "levels"):
        n = _parse_osm_number(tags.get(key))
        if n is not None:
            base = max(1, int(round(n)))
            break
    if base is None:
        h = _parse_osm_number(tags.get("height"))
        if h is not None and h > 0:
            base = max(1, int(round(h / 3.0)))
    if base is None:
        return _DEFAULT_FLOORS_BY_OSM.get((tags.get("building") or "").lower(), 3)
    roof = _parse_osm_number(tags.get("roof:levels"))
    if roof:
        base += max(0, int(round(roof)))
    return max(1, base)


# Default carriageway/channel width (m) per OSM waterway class, used when the way
# has no explicit ``width``. The viewer draws each waterline as a ribbon of this
# width via the manifest's ``waterline_width_field`` mapping.
_WATERWAY_WIDTH = {
    "river": 8.0,
    "canal": 6.0,
    "stream": 2.5,
    "drain": 1.5,
    "ditch": 1.2,
}


def _waterway_class(tags: dict) -> str:
    return (tags.get("waterway") or "").lower() or "stream"


def _waterway_width(tags: dict) -> float:
    for key in ("width", "est_width"):
        n = _parse_osm_number(tags.get(key))
        if n is not None and n > 0:
            return max(0.5, n)
    return _WATERWAY_WIDTH.get(_waterway_class(tags), 3.0)


def _tag(tags: dict, key: str) -> str:
    """Lower-cased OSM tag value, or '' when absent — emitted verbatim as a column."""
    return (tags.get(key) or "").strip().lower()


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


def connect_roads(roads_layer: QgsVectorLayer, tolerance_m: float = 2.0, feedback=None) -> QgsVectorLayer:
    """Best-effort: weld road segments into one connected network.

    OSM ways are normally noded at junctions, but clipping to the study circle and
    small data gaps can leave near-but-not-touching segment ends. We:
      1. snap each road's END POINTS to the nearest road within ``tolerance_m``
         (interior vertices are left untouched, so road curves are not distorted),
      2. dissolve by road class so contiguous same-class runs merge,
      3. explode back to single-part LineStrings (the viewer renders LineString
         roads only; MultiLineString features would be skipped).
    On any failure the original layer is returned so the export never breaks.
    """
    if roads_layer is None or roads_layer.featureCount() == 0:
        return roads_layer
    try:
        import processing  # noqa: WPS433 (QGIS runtime import)

        if feedback:
            feedback(f"Connecting roads (snap {tolerance_m:g} m + dissolve)...")
        snapped = processing.run("native:snapgeometries", {
            "INPUT": roads_layer,
            "REFERENCE_LAYER": roads_layer,
            "TOLERANCE": float(tolerance_m),
            "BEHAVIOR": 5,  # move end points only, prefer closest point (no interior distortion)
            "OUTPUT": "TEMPORARY_OUTPUT",
        })["OUTPUT"]
        dissolved = processing.run("native:dissolve", {
            "INPUT": snapped,
            "FIELD": ["highway"],
            "OUTPUT": "TEMPORARY_OUTPUT",
        })["OUTPUT"]
        singles = processing.run("native:multiparttosingleparts", {
            "INPUT": dissolved,
            "OUTPUT": "TEMPORARY_OUTPUT",
        })["OUTPUT"]
        out = QgsVectorLayer(singles, "OSM Roads", "ogr") if isinstance(singles, str) else singles
        if out is not None and out.isValid() and out.featureCount() > 0:
            if feedback:
                feedback(f"Roads connected: {roads_layer.featureCount()} -> {out.featureCount()} segments.")
            return out
    except Exception as exc:  # never block the export on geometry cleanup
        if feedback:
            feedback(f"Road connect skipped ({exc}).")
    return roads_layer


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

    # Columns mirror raw OSM tags (no PlanX schema): the viewer maps building_levels
    # -> floors, building -> colour, highway -> hierarchy, etc. via the manifest.
    buildings_layer, b_pr = _make_layer(
        "OSM Buildings", "Polygon", epsg_dest,
        [("osm_id", QVariant.String), ("building", QVariant.String),
         ("building_levels", QVariant.Int), ("height", QVariant.Double), ("name", QVariant.String)],
    )
    roads_layer, r_pr = _make_layer(
        "OSM Roads", "LineString", epsg_dest,
        [("osm_id", QVariant.String), ("highway", QVariant.String),
         ("width", QVariant.Double), ("name", QVariant.String)],
    )
    # Dedicated cycleways (highway=cycleway) split off into their own bike-lane layer.
    bikelanes_layer, bl_pr = _make_layer(
        "OSM Bike lanes", "LineString", epsg_dest,
        [("osm_id", QVariant.String), ("highway", QVariant.String),
         ("width", QVariant.Double), ("name", QVariant.String)],
    )
    greens_layer, g_pr = _make_layer(
        "OSM Greens", "Polygon", epsg_dest,
        [("osm_id", QVariant.String), ("leisure", QVariant.String),
         ("landuse", QVariant.String), ("natural", QVariant.String), ("name", QVariant.String)],
    )
    trees_layer, t_pr = _make_layer(
        "OSM Trees", "Point", epsg_dest,
        [("osm_id", QVariant.String), ("natural", QVariant.String), ("height", QVariant.Double)],
    )
    # Waterways are clipped like roads; the memory provider accepts multi-part
    # clip results into a LineString layer (same as the roads layer above).
    waterlines_layer, w_pr = _make_layer(
        "OSM Waterlines", "LineString", epsg_dest,
        [("osm_id", QVariant.String), ("waterway", QVariant.String),
         ("width", QVariant.Double), ("name", QVariant.String)],
    )
    # Street furniture as points — one layer per viewer input (mybusstops,
    # mybenches, mylights, mytrashbins).
    busstops_layer, bs_pr = _make_layer(
        "OSM Bus stops", "Point", epsg_dest,
        [("osm_id", QVariant.String), ("highway", QVariant.String), ("name", QVariant.String)],
    )
    benches_layer, be_pr = _make_layer(
        "OSM Benches", "Point", epsg_dest, [("osm_id", QVariant.String), ("amenity", QVariant.String)],
    )
    lights_layer, li_pr = _make_layer(
        "OSM Street lights", "Point", epsg_dest, [("osm_id", QVariant.String), ("highway", QVariant.String)],
    )
    trashbins_layer, tb_pr = _make_layer(
        "OSM Trash bins", "Point", epsg_dest, [("osm_id", QVariant.String), ("amenity", QVariant.String)],
    )

    counts = {
        "buildings": 0, "roads": 0, "bikelanes": 0, "greens": 0, "trees": 0,
        "waterlines": 0, "busstops": 0, "benches": 0, "lights": 0,
        "trashbins": 0, "skipped": 0,
    }

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
            height_val = _parse_osm_number(tags.get("height")) or 6.0
            feat = QgsFeature()
            feat.setGeometry(g)
            feat.setAttributes([str(element.get("id", "")), "tree", round(height_val, 1)])
            t_pr.addFeatures([feat])
            counts["trees"] += 1
            continue

        if etype == "node":
            # Street furniture points: bus stops, benches, street lamps, bins.
            geom = _node_point(element)
            if not geom:
                counts["skipped"] += 1
                continue
            g = QgsGeometry(geom)
            if g.transform(to_utm) or not circle_utm.contains(g):
                counts["skipped"] += 1
                continue
            osm_id = str(element.get("id", ""))
            if tags.get("highway") == "bus_stop" or tags.get("public_transport") == "platform":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, _tag(tags, "highway") or "bus_stop", tags.get("name", "")])
                bs_pr.addFeatures([feat])
                counts["busstops"] += 1
            elif tags.get("amenity") == "bench":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, "bench"])
                be_pr.addFeatures([feat])
                counts["benches"] += 1
            elif tags.get("highway") == "street_lamp":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, "street_lamp"])
                li_pr.addFeatures([feat])
                counts["lights"] += 1
            elif tags.get("amenity") == "waste_basket":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, "waste_basket"])
                tb_pr.addFeatures([feat])
                counts["trashbins"] += 1
            else:
                counts["skipped"] += 1
            continue

        if etype in ("way", "relation") and tags.get("building"):
            base = _way_polygon(element)
            if not base:
                continue
            clipped = clip_to_circle(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            height_val = _parse_osm_number(tags.get("height"))
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")),
                _tag(tags, "building"),
                _building_levels(tags),
                round(height_val, 1) if height_val else None,
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
            highway = _tag(tags, "highway")
            width = _parse_osm_number(tags.get("width"))
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")), highway,
                round(width, 1) if width else None, tags.get("name", ""),
            ])
            # Dedicated cycle tracks go to the bike-lane layer; everything else
            # (incl. footways/paths, which the viewer keeps cars off) stays a road.
            if highway == "cycleway":
                bl_pr.addFeatures([feat])
                counts["bikelanes"] += 1
            else:
                r_pr.addFeatures([feat])
                counts["roads"] += 1
            continue

        if etype == "way" and tags.get("waterway"):
            base = _way_polyline(element)
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
                _tag(tags, "waterway"),
                round(_waterway_width(tags), 1),
                tags.get("name", ""),
            ])
            w_pr.addFeatures([feat])
            counts["waterlines"] += 1
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
            feat.setAttributes([
                str(element.get("id", "")),
                _tag(tags, "leisure"), _tag(tags, "landuse"), _tag(tags, "natural"),
                tags.get("name", ""),
            ])
            g_pr.addFeatures([feat])
            counts["greens"] += 1
            continue

        counts["skipped"] += 1

    for layer in (buildings_layer, roads_layer, bikelanes_layer, greens_layer, trees_layer,
                  waterlines_layer, busstops_layer, benches_layer, lights_layer, trashbins_layer):
        layer.updateExtents()

    return {
        "epsg": epsg_dest,
        "counts": counts,
        "buildings": buildings_layer,
        "roads": roads_layer,
        "bikelanes": bikelanes_layer,
        "greens": greens_layer,
        "trees": trees_layer,
        "waterlines": waterlines_layer,
        "busstops": busstops_layer,
        "benches": benches_layer,
        "lights": lights_layer,
        "trashbins": trashbins_layer,
    }
