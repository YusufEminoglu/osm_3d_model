# -*- coding: utf-8 -*-
"""OpenStreetMap download + clip for the 3D OSM Model plugin.

Fetches buildings, roads, greens and trees from the public Overpass API for the
bounding box of a study boundary, reprojects to a metric UTM CRS, and clips every
feature to that boundary. Building floor count defaults to 3 when OSM has no level
data, matching the viewer's expectation.
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import random
import tempfile
import time
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
USER_AGENT = "3D-OSM-Model-QGIS-Plugin/0.17.0 (https://github.com/YusufEminoglu/osm_3d_model)"
DEFAULT_TIMEOUT_S = 60

# Disk cache for Overpass responses. The public API is frequently rate-limited
# (HTTP 429); caching the exact-query JSON means re-running on the same area (or
# nudging a layer/shape option) doesn't hit the network again within the TTL.
CACHE_TTL_S = 7 * 24 * 3600  # one week


class OsmDownloadError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Overpass response cache (on disk, keyed by the exact query)
# --------------------------------------------------------------------------
def _cache_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "osm_3d_model_cache")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _cache_path(query: str) -> str:
    # SHA-256 purely as a filename-safe digest of the query (not security).
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:40]
    return os.path.join(_cache_dir(), f"{digest}.json")


def _read_cache(query: str):
    """Return cached JSON for this exact query if present and fresh, else None."""
    path = _cache_path(query)
    try:
        if not os.path.isfile(path):
            return None
        if time.time() - os.path.getmtime(path) > CACHE_TTL_S:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_cache(query: str, payload: dict) -> None:
    try:
        with open(_cache_path(query), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except (OSError, TypeError, ValueError):
        pass


def clear_cache():
    """Delete all cached Overpass responses. Returns (files_removed, bytes_freed)."""
    removed, freed = 0, 0
    try:
        for path in glob.glob(os.path.join(_cache_dir(), "*.json")):
            try:
                freed += os.path.getsize(path)
                os.remove(path)
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed, freed


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
  way["waterway"~"river|stream|canal|drain|ditch|riverbank"]({bbox});
  way["leisure"~"park|garden|playground|pitch|nature_reserve|common|dog_park|golf_course"]({bbox});
  relation["leisure"~"park|garden|playground|pitch|nature_reserve|common|dog_park|golf_course"]({bbox});
  way["landuse"~"forest|grass|meadow|recreation_ground|cemetery|reservoir|basin|village_green|orchard|vineyard|farmland|allotments|greenfield"]({bbox});
  relation["landuse"~"forest|grass|meadow|recreation_ground|cemetery|reservoir|basin|village_green|orchard|vineyard|farmland|allotments|greenfield"]({bbox});
  way["natural"~"wood|scrub|water|grassland|heath"]({bbox});
  relation["natural"~"wood|scrub|water|grassland|heath"]({bbox});
  way["amenity"~"parking|marketplace"]({bbox});
  relation["amenity"~"parking|marketplace"]({bbox});
  way["place"="square"]({bbox});
  relation["place"="square"]({bbox});
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
                   timeout_s: int = DEFAULT_TIMEOUT_S, feedback=None,
                   use_cache: bool = True) -> dict:
    """Query Overpass, falling back across mirrors until one answers with JSON.

    When ``use_cache`` is set, a fresh on-disk response for the exact same query
    is reused (no network), and successful network responses are written back to
    the cache. This dodges the public API's frequent HTTP-429 rate limiting on
    repeated runs of the same area.
    """
    query = _overpass_query(min_lat, min_lon, max_lat, max_lon)
    if use_cache:
        cached = _read_cache(query)
        if cached is not None:
            if feedback:
                feedback("Using cached OSM (no download) ...")
            return cached
    last_error = None
    for index, endpoint in enumerate(OVERPASS_ENDPOINTS):
        host = urllib.parse.urlparse(endpoint).netloc or endpoint
        if feedback:
            prefix = "Querying" if index == 0 else f"Mirror {index} —"
            feedback(f"{prefix} {host} ...")
        try:
            payload = _fetch_one(endpoint, query, timeout_s)
            if use_cache:
                _write_cache(query, payload)
            return payload
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


def _is_water_area(tags: dict) -> bool:
    """True for an OSM polygon that should render as open water (a filled surface).

    Covers lakes, ponds, basins and bays (``natural=water``), the older
    ``waterway=riverbank`` river polygons, and ``landuse=reservoir|basin``. These
    are filled blue water surfaces, unlike the linear ``waterway`` rivers/streams
    drawn as ribbons by the waterlines layer.
    """
    if tags.get("natural") == "water":
        return True
    if tags.get("waterway") == "riverbank":
        return True
    if tags.get("landuse") in ("reservoir", "basin"):
        return True
    return bool((tags.get("water") or "").strip())


def _is_paved_area(tags: dict) -> bool:
    """True for an OSM polygon that should render as a paved public square/plaza.

    A pedestrian or footway *area* (``area=yes``), a town/market square
    (``place=square``) or a marketplace. A plain ``highway=pedestrian`` way with no
    ``area=yes`` is a pedestrian *street* and stays a road, not a plaza.
    """
    if tags.get("highway") in ("pedestrian", "footway", "living_street") and tags.get("area") == "yes":
        return True
    if tags.get("place") == "square":
        return True
    return tags.get("amenity") == "marketplace"


# Tree scatter: forests, woods and parks are flat green polygons in the viewer, and
# the OSM *tree nodes* are sparse, so wooded areas look bald. We scatter procedural
# tree points inside tree-bearing green polygons so parks and forests read as
# planted. Trees render as instanced meshes in the viewer, so a few hundred extra
# is cheap; the scatter is globally capped and deterministic per polygon (seeded by
# its footprint) so cached re-runs produce an identical city.
# kind -> (avg m² of polygon per scattered tree, min height m, max height m)
_TREE_SCATTER = {
    "forest": (75.0, 7.0, 13.0), "wood": (75.0, 7.0, 13.0),
    "orchard": (110.0, 4.0, 7.0),  # regular rows of fruit trees
    "park": (170.0, 5.0, 9.0), "garden": (210.0, 4.0, 7.0), "nature_reserve": (180.0, 5.0, 10.0),
    "village_green": (220.0, 4.0, 8.0), "recreation_ground": (230.0, 4.0, 8.0),
    "cemetery": (260.0, 5.0, 9.0), "scrub": (280.0, 2.5, 4.5), "allotments": (320.0, 3.0, 5.0),
    "grass": (400.0, 3.0, 6.0), "meadow": (440.0, 3.0, 6.0),
}
MAX_SCATTER_TREES = 500       # global cap across all polygons (perf budget)
MAX_SCATTER_PER_POLY = 130    # so one huge forest can't eat the whole budget


def _green_scatter_kind(tags: dict) -> str:
    """Return the tree-bearing green type for ``tags``, or '' if none should scatter."""
    for value in (_tag(tags, "landuse"), _tag(tags, "leisure"), _tag(tags, "natural")):
        if value in _TREE_SCATTER:
            return value
    return ""


def _scatter_trees(geom_utm: QgsGeometry, kind: str, provider, remaining: int) -> int:
    """Scatter up to ``remaining`` trees inside ``geom_utm`` (a green polygon, UTM m).

    Rejection-samples points within the polygon's bounding box and keeps those that
    fall inside it. Deterministic (RNG seeded by the polygon footprint). Returns the
    number of tree features added to ``provider``.
    """
    cfg = _TREE_SCATTER.get(kind)
    if not cfg or remaining <= 0:
        return 0
    spacing_area, h_min, h_max = cfg
    area = geom_utm.area()
    if area < spacing_area * 2.0:  # too small to read as wooded
        return 0
    target = int(min(MAX_SCATTER_PER_POLY, remaining, area / spacing_area))
    if target <= 0:
        return 0
    bbox = geom_utm.boundingBox()
    # Stable integer seed (NOT hash() of the kind string — Python randomises string
    # hashing per process, which would make every QGIS session scatter differently
    # and break the cached-re-run-is-identical guarantee).
    kind_code = sum((i + 1) * ord(c) for i, c in enumerate(kind))
    seed = ((int(round(bbox.xMinimum())) * 73856093)
            ^ (int(round(bbox.yMinimum())) * 19349663)
            ^ (int(round(area)) * 83492791)
            ^ (kind_code * 2654435761)) & 0xFFFFFFFF
    rng = random.Random(seed)
    feats = []
    attempts = 0
    max_attempts = target * 8
    while len(feats) < target and attempts < max_attempts:
        attempts += 1
        px = rng.uniform(bbox.xMinimum(), bbox.xMaximum())
        py = rng.uniform(bbox.yMinimum(), bbox.yMaximum())
        pt = QgsGeometry.fromPointXY(QgsPointXY(px, py))
        if not geom_utm.contains(pt):
            continue
        feat = QgsFeature()
        feat.setGeometry(pt)
        feat.setAttributes([f"scatter_{int(px)}_{int(py)}", "tree", round(rng.uniform(h_min, h_max), 1)])
        feats.append(feat)
    if feats:
        provider.addFeatures(feats)
    return len(feats)


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


def _ring_from_geometry(geometry) -> QgsGeometry | None:
    """Build a closed-ring polygon from an Overpass member/way ``geometry`` list."""
    pts = [QgsPointXY(pt["lon"], pt["lat"])
           for pt in (geometry or []) if "lon" in pt and "lat" in pt]
    if len(pts) < 3:
        return None
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    ring = QgsGeometry.fromPolygonXY([pts])
    return ring if ring and not ring.isEmpty() else None


def _relation_multipolygon(element) -> QgsGeometry | None:
    """Assemble a (multi)polygon from a relation's outer/inner member ways.

    Overpass ``out geom`` returns each member way with its own ``geometry``. We
    union the outer rings and subtract the inner rings (holes). Returns None when
    no usable outer ring exists, so callers fall back to skipping the feature.
    """
    outers, inners = [], []
    for member in (element.get("members") or []):
        if member.get("type") != "way":
            continue
        ring = _ring_from_geometry(member.get("geometry"))
        if ring is None:
            continue
        (inners if (member.get("role") or "").lower() == "inner" else outers).append(ring)
    if not outers:
        return None
    poly = outers[0]
    for extra in outers[1:]:
        merged = poly.combine(extra)
        if merged and not merged.isEmpty():
            poly = merged
    for hole in inners:
        cut = poly.difference(hole)
        if cut and not cut.isEmpty():
            poly = cut
    if poly is None or poly.isEmpty():
        return None
    return poly


def _element_polygon(element) -> QgsGeometry | None:
    """Polygon for a building/green element: relation -> multipolygon, else ring."""
    if element.get("type") == "relation":
        return _relation_multipolygon(element)
    return _way_polygon(element)


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
def download_osm_for_area(area_utm: QgsGeometry, epsg_dest: int, feedback=None,
                          use_cache: bool = True) -> dict:
    """Fetch OSM for the boundary's bbox, reproject to UTM, clip to the boundary.

    ``area_utm`` is the study-boundary polygon (circle, rounded rectangle,
    rectangle or exact polygon) in the EPSG:``epsg_dest`` metric CRS. Returns a
    dict of memory layers keyed by role plus feature counts.
    """
    project = QgsProject.instance()
    dst_crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest)
    wgs_crs = QgsCoordinateReferenceSystem.fromEpsgId(4326)
    to_wgs = QgsCoordinateTransform(dst_crs, wgs_crs, project)
    to_utm = QgsCoordinateTransform(wgs_crs, dst_crs, project)

    wgs_rect = to_wgs.transformBoundingBox(area_utm.boundingBox())
    min_lon, min_lat = wgs_rect.xMinimum(), wgs_rect.yMinimum()
    max_lon, max_lat = wgs_rect.xMaximum(), wgs_rect.yMaximum()

    payload = fetch_overpass(min_lat, min_lon, max_lat, max_lon, feedback=feedback, use_cache=use_cache)
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
        "trees_scattered": 0, "parking": 0, "plazas": 0, "waterlines": 0,
        "waterareas": 0, "busstops": 0, "benches": 0, "lights": 0,
        "trashbins": 0, "skipped": 0,
    }

    def clip_to_area(geom_wgs: QgsGeometry):
        g = QgsGeometry(geom_wgs)
        if g.transform(to_utm):
            return None
        if not g.intersects(area_utm):
            return None
        clipped = g.intersection(area_utm)
        if clipped.isEmpty() or not clipped.isGeosValid():
            # Fall back to the original geometry if the intersection is degenerate.
            return g if g.within(area_utm) else None
        return clipped

    scattered_trees = 0  # running total of procedurally-scattered park/forest trees
    for element in elements:
        etype = element.get("type")
        tags = element.get("tags") or {}

        if etype == "node" and tags.get("natural") == "tree":
            geom = _node_point(element)
            if not geom:
                continue
            g = QgsGeometry(geom)
            if g.transform(to_utm) or not area_utm.contains(g):
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
            if g.transform(to_utm) or not area_utm.contains(g):
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
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
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

        # Water areas (lakes, ponds, riverbanks, reservoirs, bays). Stored in the
        # greens/blocks layer normalised to natural='water'; the viewer's block
        # styling already maps a 'water' category to a blue water surface, so no
        # extra viewer layer is needed. Must run before the linear-waterway and
        # greens branches so a riverbank polygon is filled, not drawn as a ribbon.
        if etype in ("way", "relation") and _is_water_area(tags):
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([str(element.get("id", "")), "", "", "water", tags.get("name", "")])
            g_pr.addFeatures([feat])
            counts["waterareas"] += 1
            continue

        # Parking lots: stored in the blocks layer as landuse='parking' so the
        # viewer draws them as paved asphalt-grey ground (it has a 'parking' block
        # style). A common, visible part of real cities that was previously absent.
        if etype in ("way", "relation") and tags.get("amenity") == "parking":
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([str(element.get("id", "")), "", "parking", "", tags.get("name", "")])
            g_pr.addFeatures([feat])
            counts["parking"] += 1
            continue

        # Paved public squares/plazas (pedestrian or footway areas, town squares,
        # marketplaces). Stored as landuse='pedestrian' so the viewer paves them.
        # Must run before the highway branch, else a pedestrian *area* is drawn as a
        # ring-shaped road tracing its outline instead of a filled square.
        if etype in ("way", "relation") and _is_paved_area(tags):
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([str(element.get("id", "")), "", "pedestrian", "", tags.get("name", "")])
            g_pr.addFeatures([feat])
            counts["plazas"] += 1
            continue

        if etype == "way" and tags.get("highway"):
            base = _way_polyline(element)
            if not base:
                continue
            clipped = clip_to_area(base)
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
            clipped = clip_to_area(base)
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

        if etype in ("way", "relation") and (tags.get("leisure") or tags.get("landuse") or tags.get("natural")):
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base)
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
            # Plant trees inside wooded greens (forest/wood/park/...) so they don't
            # read as bald flat patches. Globally capped; rendered via the normal
            # (instanced) tree layer, so no viewer change is needed.
            kind = _green_scatter_kind(tags)
            if kind:
                placed = _scatter_trees(clipped, kind, t_pr, MAX_SCATTER_TREES - scattered_trees)
                scattered_trees += placed
                counts["trees"] += placed
                counts["trees_scattered"] += placed
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
