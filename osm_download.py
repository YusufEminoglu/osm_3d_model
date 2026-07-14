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
import re
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
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from . import PLUGIN_VERSION

# Public Overpass mirrors, tried in order. The main instance is frequently rate
# limited (HTTP 429) or slow; falling back to mirrors makes the one-button flow
# far more reliable. The first endpoint that answers with valid JSON wins.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = f"3D-OSM-Model-QGIS-Plugin/{PLUGIN_VERSION} (https://github.com/YusufEminoglu/osm_3d_model)"
DEFAULT_TIMEOUT_S = 60
MAX_RESPONSE_BYTES = 128 * 1024 * 1024

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
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def _write_cache(query: str, payload: dict) -> None:
    """Atomically cache a validated Overpass payload."""
    tmp_path = None
    try:
        cache_path = _cache_path(query)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=os.path.dirname(cache_path),
            prefix=".osm3d-", suffix=".tmp", delete=False,
        ) as handle:
            tmp_path = handle.name
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, cache_path)
        tmp_path = None
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _discard_cache(query: str) -> None:
    try:
        os.remove(_cache_path(query))
    except OSError:
        pass


def clear_cache():
    """Delete all cached Overpass responses. Returns (files_removed, bytes_freed)."""
    removed, freed = 0, 0
    try:
        for path in glob.glob(os.path.join(_cache_dir(), "*.json")):
            try:
                size = os.path.getsize(path)
                os.remove(path)
                removed += 1
                freed += size
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
def _overpass_query(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                    timeout_s: int = DEFAULT_TIMEOUT_S) -> str:
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    query_timeout = max(1, int(timeout_s))
    return f"""
[out:json][timeout:{query_timeout}];
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
  node["public_transport"="platform"]({bbox});
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
            payload_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(payload_bytes) > MAX_RESPONSE_BYTES:
                raise OsmDownloadError(
                    f"response exceeded the {MAX_RESPONSE_BYTES // (1024 * 1024)} MB safety limit"
                )
            payload = payload_bytes.decode("utf-8", errors="replace")
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


def _validate_overpass_payload(payload) -> dict:
    """Reject malformed or explicitly incomplete Overpass responses."""
    if not isinstance(payload, dict):
        raise OsmDownloadError("invalid JSON schema (expected an object)")
    remark = str(payload.get("remark") or "").strip()
    if remark:
        raise OsmDownloadError(f"Overpass reported an incomplete response: {remark}")
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise OsmDownloadError("invalid JSON schema (missing elements list)")
    if any(not isinstance(element, dict) for element in elements):
        raise OsmDownloadError("invalid JSON schema (non-object element)")
    return payload


def fetch_overpass(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                   timeout_s: int = DEFAULT_TIMEOUT_S, feedback=None,
                   use_cache: bool = True, cancel_check=None) -> dict:
    """Query Overpass, falling back across mirrors until one answers with JSON.

    When ``use_cache`` is set, a fresh on-disk response for the exact same query
    is reused (no network), and successful network responses are written back to
    the cache. This dodges the public API's frequent HTTP-429 rate limiting on
    repeated runs of the same area.
    """
    try:
        min_lat, min_lon, max_lat, max_lon = (
            float(min_lat), float(min_lon), float(max_lat), float(max_lon)
        )
    except (TypeError, ValueError) as exc:
        raise OsmDownloadError("Invalid Overpass bounding-box coordinates.") from exc
    bbox_values = (min_lat, min_lon, max_lat, max_lon)
    if not all(math.isfinite(value) for value in bbox_values):
        raise OsmDownloadError("Invalid Overpass bounding-box coordinates.")
    if not (-90 <= min_lat < max_lat <= 90 and -180 <= min_lon < max_lon <= 180):
        raise OsmDownloadError("Invalid or reversed Overpass bounding box.")
    timeout_s = max(1, int(timeout_s))
    if cancel_check and cancel_check():
        raise OsmDownloadError("OSM download cancelled.")
    query = _overpass_query(min_lat, min_lon, max_lat, max_lon, timeout_s)
    if use_cache:
        cached = _read_cache(query)
        if cached is not None:
            try:
                cached = _validate_overpass_payload(cached)
            except OsmDownloadError:
                _discard_cache(query)
            else:
                if feedback:
                    feedback("Using cached OSM (no download) ...")
                return cached
    last_error = None
    for index, endpoint in enumerate(OVERPASS_ENDPOINTS):
        if cancel_check and cancel_check():
            raise OsmDownloadError("OSM download cancelled.")
        host = urllib.parse.urlparse(endpoint).netloc or endpoint
        if feedback:
            prefix = "Querying" if index == 0 else f"Mirror {index} —"
            feedback(f"{prefix} {host} ...")
        try:
            payload = _validate_overpass_payload(_fetch_one(endpoint, query, timeout_s))
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
    text = str(value).split(";", 1)[0].strip().lstrip("~").strip()
    feet_match = re.fullmatch(
        r"([-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*(?:ft|feet|foot|['′])"
        r'(?:\s*([-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*(?:in|inches?|["″]))?',
        text,
        re.IGNORECASE,
    )
    if feet_match:
        feet = float(feet_match.group(1).replace(",", "."))
        inches = float((feet_match.group(2) or "0").replace(",", "."))
        return feet * 0.3048 + inches * 0.0254
    match = re.fullmatch(
        r"([-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*"
        r"(m|met(?:er|re)s?|cm|km|ft|feet|foot|in|inches?)?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or "").lower()
    if unit == "cm":
        return number / 100.0
    if unit == "km":
        return number * 1000.0
    if unit in {"ft", "feet", "foot"}:
        return number * 0.3048
    if unit.startswith("in"):
        return number * 0.0254
    return number


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
        base = _DEFAULT_FLOORS_BY_OSM.get((tags.get("building") or "").lower(), 3)
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


_CYCLEWAY_PRESENT_VALUES = {
    "lane", "track", "shared_lane", "share_busway", "opposite",
    "opposite_lane", "opposite_track",
}


def _cycleway_sides(tags: dict) -> list:
    """Return road-relative bike-lane sides mapped on a non-cycleway highway."""
    sides = []
    left = _tag(tags, "cycleway:left")
    right = _tag(tags, "cycleway:right")
    both = _tag(tags, "cycleway:both")
    if left in _CYCLEWAY_PRESENT_VALUES:
        sides.append("left")
    if right in _CYCLEWAY_PRESENT_VALUES:
        sides.append("right")
    if both in _CYCLEWAY_PRESENT_VALUES:
        sides.extend(("left", "right"))
    generic = _tag(tags, "cycleway")
    if not sides and generic in _CYCLEWAY_PRESENT_VALUES:
        sides.extend(("right",) if _tag(tags, "oneway") in {"yes", "1", "true"} else ("left", "right"))
    return list(dict.fromkeys(sides))


def _cycleway_width(tags: dict, side: str) -> float:
    for key in (f"cycleway:{side}:width", "cycleway:width"):
        width = _parse_osm_number(tags.get(key))
        if width is not None and width > 0:
            return max(0.8, min(5.0, width))
    return 1.8


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


class _SeededRng:
    """Tiny self-contained deterministic PRNG (64-bit LCG).

    Replaces ``random.Random`` for tree scatter so the module carries no
    dependency on the stdlib CSPRNG blacklist (Bandit B311) while keeping the
    "same seed -> identical city" guarantee. Not for cryptographic use.
    """

    __slots__ = ("_state",)

    def __init__(self, seed: int):
        self._state = (seed ^ 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF

    def _next(self) -> int:
        # Numerical-Recipes 64-bit LCG constants.
        self._state = (self._state * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        return self._state

    def uniform(self, lo: float, hi: float) -> float:
        frac = (self._next() >> 11) / float(1 << 53)  # [0, 1)
        return lo + (hi - lo) * frac


MAX_SCATTER_TREES = 500       # global cap across all polygons (perf budget)
MAX_SCATTER_PER_POLY = 130    # so one huge forest can't eat the whole budget


def _green_scatter_kind(tags: dict) -> str:
    """Return the tree-bearing green type for ``tags``, or '' if none should scatter."""
    for value in (_tag(tags, "landuse"), _tag(tags, "leisure"), _tag(tags, "natural")):
        if value in _TREE_SCATTER:
            return value
    return ""


def _prepared_engine(geom: QgsGeometry):
    """A prepared GEOS engine for fast repeated predicates against ``geom``.

    The caller must keep ``geom`` alive for as long as the engine is used (the
    engine references the underlying geometry, it does not copy it).
    """
    engine = QgsGeometry.createGeometryEngine(geom.constGet())
    engine.prepareGeometry()
    return engine


def _scatter_trees(geom_utm: QgsGeometry, kind: str, feature_sink: list, remaining: int) -> int:
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
    poly_engine = _prepared_engine(geom_utm)
    bbox = geom_utm.boundingBox()
    # Stable integer seed (NOT hash() of the kind string — Python randomises string
    # hashing per process, which would make every QGIS session scatter differently
    # and break the cached-re-run-is-identical guarantee).
    kind_code = sum((i + 1) * ord(c) for i, c in enumerate(kind))
    seed_parts = (
        int(round(bbox.xMinimum())) * 73856093,
        int(round(bbox.yMinimum())) * 19349663,
        int(round(area)) * 83492791,
        kind_code * 2654435761,
    )
    seed = seed_parts[0]
    for part in seed_parts[1:]:
        seed ^= part
    seed &= 0xFFFFFFFF
    rng = _SeededRng(seed)
    feats = []
    attempts = 0
    max_attempts = target * 8
    while len(feats) < target and attempts < max_attempts:
        attempts += 1
        px = rng.uniform(bbox.xMinimum(), bbox.xMaximum())
        py = rng.uniform(bbox.yMinimum(), bbox.yMaximum())
        pt = QgsGeometry.fromPointXY(QgsPointXY(px, py))
        if not poly_engine.contains(pt.constGet()):
            continue
        feat = QgsFeature()
        feat.setGeometry(pt)
        feat.setAttributes([f"scatter_{int(px)}_{int(py)}", "tree", round(rng.uniform(h_min, h_max), 1)])
        feats.append(feat)
    if feats:
        feature_sink.extend(feats)
    return len(feats)


def _tag(tags: dict, key: str) -> str:
    """Lower-cased OSM tag value, or '' when absent — emitted verbatim as a column."""
    return (tags.get(key) or "").strip().lower()


def _way_polygon(element) -> QgsGeometry | None:
    points = _member_points(element.get("geometry"))
    if len(points) < 3:
        return None
    if _point_key(points[0]) != _point_key(points[-1]):
        points.append(points[0])
    return _polygon_from_ring(points)


def _member_points(geometry) -> list:
    """Return finite coordinates for an Overpass relation-member geometry."""
    points = []
    for item in geometry or []:
        if not isinstance(item, dict) or "lon" not in item or "lat" not in item:
            continue
        try:
            lon, lat = float(item["lon"]), float(item["lat"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(lon) or not math.isfinite(lat):
            continue
        point = QgsPointXY(lon, lat)
        if not points or _point_key(points[-1]) != _point_key(point):
            points.append(point)
    return points


def _point_key(point: QgsPointXY) -> tuple:
    # Overpass repeats shared node coordinates, but JSON float round-tripping can
    # introduce insignificant noise. Sub-millimetre precision is ample here.
    return round(point.x(), 11), round(point.y(), 11)


def _stitch_member_rings(paths: list) -> list:
    """Join split/reversed relation member ways into closed coordinate rings."""
    pending = [list(path) for path in paths if len(path) >= 2]
    rings = []
    while pending:
        chain = pending.pop(0)
        while _point_key(chain[0]) != _point_key(chain[-1]):
            start, end = _point_key(chain[0]), _point_key(chain[-1])
            match_index = None
            joined = None
            for index, path in enumerate(pending):
                path_start, path_end = _point_key(path[0]), _point_key(path[-1])
                if end == path_start:
                    joined = chain + path[1:]
                elif end == path_end:
                    joined = chain + list(reversed(path[:-1]))
                elif start == path_end:
                    joined = path[:-1] + chain
                elif start == path_start:
                    joined = list(reversed(path[1:])) + chain
                else:
                    continue
                match_index = index
                break
            if match_index is None:
                break
            chain = joined
            pending.pop(match_index)
        if _point_key(chain[0]) == _point_key(chain[-1]):
            if len({_point_key(point) for point in chain[:-1]}) >= 3:
                rings.append(chain)
    return rings


def _polygon_from_ring(points: list) -> QgsGeometry | None:
    polygon = QgsGeometry.fromPolygonXY([points])
    if polygon is None or polygon.isEmpty() or polygon.area() <= 0:
        return None
    if not polygon.isGeosValid():
        polygon = polygon.makeValid()
    if polygon is None or polygon.isEmpty():
        return None
    if QgsWkbTypes.geometryType(polygon.wkbType()) != QgsWkbTypes.PolygonGeometry:
        converted = QgsGeometry(polygon)
        if not converted.convertGeometryCollectionToSubclass(QgsWkbTypes.PolygonGeometry):
            return None
        polygon = converted
    return polygon if not polygon.isEmpty() and polygon.area() > 0 else None


def _relation_multipolygon(element) -> QgsGeometry | None:
    """Assemble a (multi)polygon from a relation's outer/inner member ways.

    Overpass ``out geom`` returns each member way with its own ``geometry``. We
    union the outer rings and subtract the inner rings (holes). Returns None when
    no usable outer ring exists, so callers fall back to skipping the feature.
    """
    outer_paths, inner_paths = [], []
    for member in (element.get("members") or []):
        if member.get("type") != "way":
            continue
        role = (member.get("role") or "").lower()
        if role not in ("", "outer", "inner"):
            continue
        points = _member_points(member.get("geometry"))
        if len(points) < 2:
            continue
        (inner_paths if role == "inner" else outer_paths).append(points)

    outers = [polygon for polygon in
              (_polygon_from_ring(ring) for ring in _stitch_member_rings(outer_paths))
              if polygon is not None]
    if not outers:
        return None
    poly = QgsGeometry.unaryUnion(outers)
    inners = [polygon for polygon in
              (_polygon_from_ring(ring) for ring in _stitch_member_rings(inner_paths))
              if polygon is not None]
    if inners:
        holes = QgsGeometry.unaryUnion(inners)
        if holes is not None and not holes.isEmpty():
            poly = poly.difference(holes)
    if poly is None or poly.isEmpty() or poly.area() <= 0:
        return None
    if not poly.isGeosValid():
        poly = poly.makeValid()
    if poly is None or poly.isEmpty():
        return None
    if QgsWkbTypes.geometryType(poly.wkbType()) != QgsWkbTypes.PolygonGeometry:
        if not poly.convertGeometryCollectionToSubclass(QgsWkbTypes.PolygonGeometry):
            return None
    return poly


def _element_polygon(element) -> QgsGeometry | None:
    """Polygon for a building/green element: relation -> multipolygon, else ring."""
    if element.get("type") == "relation":
        return _relation_multipolygon(element)
    return _way_polygon(element)


def _way_polyline(element) -> QgsGeometry | None:
    points = _member_points(element.get("geometry"))
    if len(points) < 2:
        return None
    return QgsGeometry.fromPolylineXY(points)


def _node_point(element) -> QgsGeometry | None:
    if "lon" not in element or "lat" not in element:
        return None
    try:
        lon, lat = float(element["lon"]), float(element["lat"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lon) or not math.isfinite(lat):
        return None
    return QgsGeometry.fromPointXY(QgsPointXY(lon, lat))


def _make_layer(name: str, wkb_type: str, epsg_dest: int, fields_def):
    layer = QgsVectorLayer(f"{wkb_type}?crs=EPSG:{epsg_dest}", name, "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField(field_name, qvariant) for field_name, qvariant in fields_def])
    layer.updateFields()
    return layer, provider


def _add_features_batched(provider, features: list, label: str, chunk_size: int = 5000) -> None:
    """Add features with bounded Python/C++ crossings and verify provider success."""
    for offset in range(0, len(features), chunk_size):
        chunk = features[offset:offset + chunk_size]
        result = provider.addFeatures(chunk)
        if isinstance(result, tuple):
            success = bool(result[0])
            added = result[1] if len(result) > 1 else None
        else:
            success, added = bool(result), None
        if not success or (added is not None and len(added) != len(chunk)):
            raise OsmDownloadError(f"QGIS could not create all {label} features.")


def save_layer_to_geojson(layer: QgsVectorLayer, path) -> None:
    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GeoJSON"
    options.fileEncoding = "UTF-8"
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsProject.instance().transformContext(), options
    )
    if isinstance(result, tuple):
        error = result[0]
        message = str(result[1]) if len(result) > 1 else ""
    else:
        error, message = result, ""
    if error != QgsVectorFileWriter.NoError:
        try:
            os.remove(path)
        except OSError:
            pass
        detail = f": {message}" if message else ""
        raise OsmDownloadError(f"QGIS could not write {os.path.basename(path)}{detail}")
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise OsmDownloadError(f"QGIS did not create {os.path.basename(path)}.")


# --------------------------------------------------------------------------
# Main download + clip
# --------------------------------------------------------------------------
def download_osm_for_area(area_utm: QgsGeometry, epsg_dest: int, feedback=None,
                          use_cache: bool = True, payload: dict | None = None) -> dict:
    """Fetch OSM for the boundary's bbox, reproject to UTM, clip to the boundary.

    ``area_utm`` is the study-boundary polygon (circle, rounded rectangle,
    rectangle or exact polygon) in the EPSG:``epsg_dest`` metric CRS. Returns a
    dict of memory layers keyed by role plus feature counts.
    """
    project = QgsProject.instance()
    dst_crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg_dest)
    wgs_crs = QgsCoordinateReferenceSystem.fromEpsgId(4326)
    to_utm = QgsCoordinateTransform(wgs_crs, dst_crs, project)

    if payload is None:
        to_wgs = QgsCoordinateTransform(dst_crs, wgs_crs, project)
        wgs_rect = to_wgs.transformBoundingBox(area_utm.boundingBox())
        min_lon, min_lat = wgs_rect.xMinimum(), wgs_rect.yMinimum()
        max_lon, max_lat = wgs_rect.xMaximum(), wgs_rect.yMaximum()
        payload = fetch_overpass(
            min_lat, min_lon, max_lat, max_lon,
            feedback=feedback, use_cache=use_cache,
        )
    else:
        payload = _validate_overpass_payload(payload)
    elements = payload.get("elements") or []
    if not elements:
        # Overpass reports server-side problems (query timeout, memory limit) in a
        # "remark" field while still answering 200 with 0 elements — surface it.
        remark = str(payload.get("remark") or "").strip()
        hint = f" Overpass said: {remark}" if remark else ""
        raise OsmDownloadError(
            f"Overpass returned 0 elements for this area. Try a different or larger area.{hint}")

    # Columns mirror raw OSM tags (no PlanX schema): the viewer maps building_levels
    # -> floors, building -> colour, highway -> hierarchy, etc. via the manifest.
    buildings_layer, b_pr = _make_layer(
        "OSM Buildings", "Polygon", epsg_dest,
        [("osm_id", QVariant.String), ("building", QVariant.String),
         ("building_levels", QVariant.Int), ("height", QVariant.Double),
         ("roof_shape", QVariant.String), ("roof_height", QVariant.Double),
         ("name", QVariant.String)],
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
         ("width", QVariant.Double), ("name", QVariant.String),
         ("road_width", QVariant.Double), ("side", QVariant.String)],
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
    feature_batches = {
        "buildings": [], "roads": [], "bikelanes": [], "greens": [], "trees": [],
        "waterlines": [], "busstops": [], "benches": [], "lights": [], "trashbins": [],
    }

    # Prepared GEOS engine: the boundary is tested against every OSM element, and
    # preparing it once makes those thousands of intersects/contains calls cheap.
    area_engine = _prepared_engine(area_utm)

    def clip_to_area(geom_wgs: QgsGeometry, want_type):
        """Reproject to UTM and clip to the study boundary.

        ``want_type`` (QgsWkbTypes.PolygonGeometry / LineGeometry) guards against
        GEOS returning a GeometryCollection with point/line slivers when a feature
        only touches the boundary — only the wanted component is kept, so no
        invisible degenerate features reach the memory layers or the viewer.
        """
        g = QgsGeometry(geom_wgs)
        if g.transform(to_utm):
            return None
        if not area_engine.intersects(g.constGet()):
            return None
        clipped = QgsGeometry(area_engine.intersection(g.constGet()))
        if clipped.isEmpty() or not clipped.isGeosValid():
            # Fall back to the original geometry if the intersection is degenerate.
            return g if area_engine.contains(g.constGet()) else None
        if QgsWkbTypes.geometryType(clipped.wkbType()) != want_type:
            if not clipped.convertGeometryCollectionToSubclass(want_type) or clipped.isEmpty():
                return None
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
            if g.transform(to_utm) or not area_engine.contains(g.constGet()):
                counts["skipped"] += 1
                continue
            height_val = _parse_osm_number(tags.get("height")) or 6.0
            feat = QgsFeature()
            feat.setGeometry(g)
            feat.setAttributes([str(element.get("id", "")), "tree", round(height_val, 1)])
            feature_batches["trees"].append(feat)
            counts["trees"] += 1
            continue

        if etype == "node":
            # Street furniture points: bus stops, benches, street lamps, bins.
            geom = _node_point(element)
            if not geom:
                counts["skipped"] += 1
                continue
            g = QgsGeometry(geom)
            if g.transform(to_utm) or not area_engine.contains(g.constGet()):
                counts["skipped"] += 1
                continue
            osm_id = str(element.get("id", ""))
            if tags.get("highway") == "bus_stop" or tags.get("public_transport") == "platform":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, _tag(tags, "highway") or "bus_stop", tags.get("name", "")])
                feature_batches["busstops"].append(feat)
                counts["busstops"] += 1
            elif tags.get("amenity") == "bench":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, "bench"])
                feature_batches["benches"].append(feat)
                counts["benches"] += 1
            elif tags.get("highway") == "street_lamp":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, "street_lamp"])
                feature_batches["lights"].append(feat)
                counts["lights"] += 1
            elif tags.get("amenity") == "waste_basket":
                feat = QgsFeature()
                feat.setGeometry(g)
                feat.setAttributes([osm_id, "waste_basket"])
                feature_batches["trashbins"].append(feat)
                counts["trashbins"] += 1
            else:
                counts["skipped"] += 1
            continue

        if etype in ("way", "relation") and tags.get("building"):
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base, QgsWkbTypes.PolygonGeometry)
            if clipped is None:
                counts["skipped"] += 1
                continue
            height_val = _parse_osm_number(tags.get("height"))
            roof_height = _parse_osm_number(tags.get("roof:height"))
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")),
                _tag(tags, "building"),
                _building_levels(tags),
                round(height_val, 1) if height_val else None,
                _tag(tags, "roof:shape"),
                round(roof_height, 1) if roof_height else None,
                tags.get("name", ""),
            ])
            feature_batches["buildings"].append(feat)
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
            clipped = clip_to_area(base, QgsWkbTypes.PolygonGeometry)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([str(element.get("id", "")), "", "", "water", tags.get("name", "")])
            feature_batches["greens"].append(feat)
            counts["waterareas"] += 1
            continue

        # Parking lots: stored in the blocks layer as landuse='parking' so the
        # viewer draws them as paved asphalt-grey ground (it has a 'parking' block
        # style). A common, visible part of real cities that was previously absent.
        if etype in ("way", "relation") and tags.get("amenity") == "parking":
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base, QgsWkbTypes.PolygonGeometry)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([str(element.get("id", "")), "", "parking", "", tags.get("name", "")])
            feature_batches["greens"].append(feat)
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
            clipped = clip_to_area(base, QgsWkbTypes.PolygonGeometry)
            if clipped is None:
                counts["skipped"] += 1
                continue
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([str(element.get("id", "")), "", "pedestrian", "", tags.get("name", "")])
            feature_batches["greens"].append(feat)
            counts["plazas"] += 1
            continue

        if etype == "way" and tags.get("highway"):
            base = _way_polyline(element)
            if not base:
                continue
            clipped = clip_to_area(base, QgsWkbTypes.LineGeometry)
            if clipped is None:
                counts["skipped"] += 1
                continue
            highway = _tag(tags, "highway")
            width = _parse_osm_number(tags.get("width"))
            osm_id = str(element.get("id", ""))
            name = tags.get("name", "")
            # Dedicated tracks stay out of the motor-road layer. Bike lanes tagged
            # on an ordinary highway are duplicated into the bike layer with a
            # road-relative side so the viewer can offset them to the carriageway.
            if highway == "cycleway":
                feat = QgsFeature()
                feat.setGeometry(clipped)
                feat.setAttributes([
                    osm_id, highway, round(width, 1) if width else None, name,
                    None, "center",
                ])
                feature_batches["bikelanes"].append(feat)
                counts["bikelanes"] += 1
            else:
                feat = QgsFeature()
                feat.setGeometry(clipped)
                feat.setAttributes([
                    osm_id, highway, round(width, 1) if width else None, name,
                ])
                feature_batches["roads"].append(feat)
                counts["roads"] += 1
                for side in _cycleway_sides(tags):
                    bike_feat = QgsFeature()
                    bike_feat.setGeometry(QgsGeometry(clipped))
                    bike_feat.setAttributes([
                        osm_id, highway, round(_cycleway_width(tags, side), 1), name,
                        round(width, 1) if width else None, side,
                    ])
                    feature_batches["bikelanes"].append(bike_feat)
                    counts["bikelanes"] += 1
            continue

        if etype == "way" and tags.get("waterway"):
            base = _way_polyline(element)
            if not base:
                continue
            clipped = clip_to_area(base, QgsWkbTypes.LineGeometry)
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
            feature_batches["waterlines"].append(feat)
            counts["waterlines"] += 1
            continue

        if etype in ("way", "relation") and (tags.get("leisure") or tags.get("landuse") or tags.get("natural")):
            base = _element_polygon(element)
            if not base:
                continue
            clipped = clip_to_area(base, QgsWkbTypes.PolygonGeometry)
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
            feature_batches["greens"].append(feat)
            counts["greens"] += 1
            # Plant trees inside wooded greens (forest/wood/park/...) so they don't
            # read as bald flat patches. Globally capped; rendered via the normal
            # (instanced) tree layer, so no viewer change is needed.
            kind = _green_scatter_kind(tags)
            if kind:
                placed = _scatter_trees(
                    clipped, kind, feature_batches["trees"],
                    MAX_SCATTER_TREES - scattered_trees,
                )
                scattered_trees += placed
                counts["trees"] += placed
                counts["trees_scattered"] += placed
            continue

        counts["skipped"] += 1

    for provider, key, label in (
        (b_pr, "buildings", "building"), (r_pr, "roads", "road"),
        (bl_pr, "bikelanes", "bike-lane"), (g_pr, "greens", "ground-area"),
        (t_pr, "trees", "tree"), (w_pr, "waterlines", "waterway"),
        (bs_pr, "busstops", "bus-stop"), (be_pr, "benches", "bench"),
        (li_pr, "lights", "street-light"), (tb_pr, "trashbins", "trash-bin"),
    ):
        _add_features_batched(provider, feature_batches[key], label)

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
