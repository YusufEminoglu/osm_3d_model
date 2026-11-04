"""QGIS suite: is the model faithful to the OSM data? (P10, contract C3)

A synthetic Overpass answer around Izmir goes through the real
``download_osm_for_area`` (no network: the answer is handed in), and every
check compares the layers with what OSM says.  Areas of the multipolygons
are checked against an independent UTM projection (Krueger series, not
PROJ) and the shoelace formula.

Each check names the 1.9.1 behaviour it rules out; the few marked "kept"
guard behaviour 1.9.1 already had.  Run from the folder that contains the
plugin, with QGIS_CUSTOM_CONFIG_PATH pointing at a throwaway folder:

    python-qgis-ltr.bat -m osm_3d_model.tests.test_osm_fidelity_qgis   # QGIS 3.44
    python-qgis.bat     -m osm_3d_model.tests.test_osm_fidelity_qgis   # QGIS 4.2
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
import traceback
from typing import Dict, List

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)

LAT0, LON0 = 38.4245, 27.1457      # model centre (Izmir, UTM zone 35N)
EPSG = 32635
RADIUS_M = 250.0
_M_PER_DEG_LAT = 111_000.0          # only to PLACE points; areas are re-projected exactly
_M_PER_DEG_LON = 111_000.0 * math.cos(math.radians(LAT0))


# ------------------------------------------------- independent geometry
def utm_forward(lat: float, lon: float, zone: int = 35):
    """WGS84 -> UTM (Krueger series, n^3 terms): independent of PROJ."""
    a, f, k0 = 6378137.0, 1 / 298.257223563, 0.9996
    n = f / (2 - f)
    big_a = a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64)
    alpha = (0.0, n / 2 - 2 * n ** 2 / 3 + 5 * n ** 3 / 16,
             13 * n ** 2 / 48 - 3 * n ** 3 / 5, 61 * n ** 3 / 240)
    e = math.sqrt(f * (2 - f))
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    phi, lam = math.radians(lat), math.radians(lon) - lon0
    t = math.sinh(math.atanh(math.sin(phi)) - e * math.atanh(e * math.sin(phi)))
    xi_p = math.atan2(t, math.cos(lam))
    eta_p = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    xi = xi_p + sum(alpha[j] * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p) for j in (1, 2, 3))
    eta = eta_p + sum(alpha[j] * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p) for j in (1, 2, 3))
    return 500000.0 + k0 * big_a * eta, k0 * big_a * xi


def ring_area(ring) -> float:
    """Shoelace area in m² of a closed lat/lon ring, projected independently."""
    points = [utm_forward(p["lat"], p["lon"]) for p in ring[:-1]]
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def ll(dx: float, dy: float) -> Dict[str, float]:
    return {"lat": round(LAT0 + dy / _M_PER_DEG_LAT, 9), "lon": round(LON0 + dx / _M_PER_DEG_LON, 9)}


def rect(cx: float, cy: float, w: float, h: float) -> List[Dict[str, float]]:
    """A closed counter-clockwise ring, w x h metres centred cx, cy metres from the centre."""
    ring = [ll(cx - w / 2, cy - h / 2), ll(cx + w / 2, cy - h / 2),
            ll(cx + w / 2, cy + h / 2), ll(cx - w / 2, cy + h / 2)]
    return ring + [ring[0]]


def way(wid: int, ring, tags: dict) -> dict:
    return {"type": "way", "id": wid, "geometry": ring, "tags": tags}


def line(wid: int, x0: float, x1: float, y: float, tags: dict) -> dict:
    return {"type": "way", "id": wid, "geometry": [ll(x0, y), ll(x1, y)], "tags": tags}


def node(nid: int, dx: float, dy: float, tags: dict) -> dict:
    point = ll(dx, dy)
    return {"type": "node", "id": nid, "lat": point["lat"], "lon": point["lon"], "tags": tags}


# ------------------------------------------------------------- fixture
COURT_OUTER = rect(60, -60, 40, 40)
COURT_INNER = rect(60, -60, 16, 16)
PARK_OUTER = rect(-80, -90, 120, 120)
PARK_HOLE = rect(-80, -90, 60, 60)
PARK_ISLAND = rect(-80, -90, 20, 20)

# osm id -> (tags, expected C3 attributes: height, roof_height, min_height, source, levels)
BUILDINGS = {
    101: ({"building": "apartments", "building:levels": "5", "roof:levels": "1",
           "roof:shape": "gabled", "roof:height": "3", "height": "18", "name": "Çarşı Apartmanı"},
          (15.0, 3.0, 0.0, "height", 5)),
    102: ({"building": "yes", "height": "45 ft"}, (13.7, 0.0, 0.0, "height", 5)),
    103: ({"building": "house", "building:levels": "2;3"}, (6.0, 0.0, 0.0, "levels", 2)),
    104: ({"building": "roof", "min_height": "3", "height": "5"}, (5.0, 0.0, 3.0, "height", 2)),
    105: ({"building": "yes", "roof:shape": "gabled", "height": "10"}, (7.0, 3.0, 0.0, "height", 2)),
    106: ({"building": "yes", "roof:shape": "hipped", "height": "6"}, (4.2, 1.8, 0.0, "height", 1)),
    108: ({"building": "yes", "height": "12,5 m", "building:levels": "4"}, (12.5, 0.0, 0.0, "height", 4)),
    109: ({"building": "yes", "building:levels": "2.5"}, (9.0, 0.0, 0.0, "levels", 3)),
    110: ({"building": "yes"}, (9.0, 0.0, 0.0, "default", 3)),
    111: ({"building": "yes", "roof:shape": "gabled", "height": "10", "roof:levels": "1"},
          (7.0, 3.0, 0.0, "height", 2)),
    112: ({"building": "yes", "roof:shape": "gabled", "height": "4", "roof:height": "3"},
          (2.0, 3.0, 0.0, "height", 1)),
    113: ({"building": "yes", "building:min_level": "2", "building:levels": "6"},
          (18.0, 0.0, 6.0, "levels", 6)),
    114: ({"building": "yes", "roof:shape": "flat", "height": "12"}, (12.0, 0.0, 0.0, "height", 4)),
    115: ({"building": "yes", "height": "15"}, (15.0, 0.0, 0.0, "height", 5)),
    116: ({"building": "yes", "building:levels": "4.5"}, (15.0, 0.0, 0.0, "levels", 5)),
    117: ({"building": "house"}, (6.0, 0.0, 0.0, "default", 2)),
}
_SPOTS = [(-180 + 40 * (i % 9), 40 + 30 * (i // 9)) for i in range(len(BUILDINGS))]

ABSURD = {
    150: ({"building": "yes", "building:levels": "3000000000"}, (600.0, "levels", 200)),
    151: ({"building": "yes", "building:levels": "9" * 400}, (9.0, "default", 3)),
    152: ({"building": "yes", "height": "99999"}, (1000.0, "height", 200)),
    153: ({"building": "yes", "height": "-5"}, (9.0, "default", 3)),
    154: ({"building": "yes", "building:levels": "1000"}, (600.0, "levels", 200)),
    155: ({"building": "roof", "building:levels": "0"}, (3.0, "levels", 1)),
}


def main_payload() -> dict:
    elements = []
    for (wid, (tags, _expected)), (cx, cy) in zip(BUILDINGS.items(), _SPOTS):
        elements.append(way(wid, rect(cx, cy, 12, 10), tags))
    open_ring = rect(150, 150, 10, 10)[:-2]          # three nodes, not closed
    elements.append(way(107, open_ring, {"building": "yes"}))
    # Courtyard: the outer ring split into two open ways, one reversed, and a hole.
    outer = COURT_OUTER[:-1]
    elements.append({"type": "relation", "id": 201, "tags": {"type": "multipolygon", "building": "yes"},
                     "members": [
                         {"type": "way", "ref": 9001, "role": "outer", "geometry": outer[0:3]},
                         {"type": "way", "ref": 9002, "role": "outer",
                          "geometry": [outer[2], outer[3], outer[0]][::-1]},
                         {"type": "way", "ref": 9003, "role": "inner", "geometry": COURT_INNER}]})
    # Park with a pond (a hole) that holds an island.
    elements.append({"type": "relation", "id": 202,
                     "tags": {"type": "multipolygon", "leisure": "park", "name": "Kültürpark"},
                     "members": [
                         {"type": "way", "ref": 9101, "role": "outer", "geometry": PARK_OUTER},
                         {"type": "way", "ref": 9102, "role": "inner", "geometry": PARK_HOLE},
                         {"type": "way", "ref": 9103, "role": "outer", "geometry": PARK_ISLAND}]})
    elements += [
        node(301, 10, 10, {"natural": "tree", "height": "8"}),
        node(302, 20, 12, {"natural": "tree", "height": "9" * 400}),
        node(306, 400, 10, {"natural": "tree"}),                         # outside the circle
        line(401, -400, 400, 5, {"highway": "residential", "name": "Atatürk Caddesi", "width": "7"}),
        line(402, -100, 100, -170, {"highway": "proposed", "proposed": "secondary"}),
        line(403, -100, 100, -180, {"highway": "residential", "tunnel": "yes", "layer": "-1"}),
        line(404, -100, 100, -190, {"highway": "abandoned"}),
        line(405, -100, 100, -200, {"highway": "razed"}),
        line(406, -100, 100, -210, {"highway": "demolished"}),
        line(407, -100, 100, -160, {"highway": "service", "tunnel": "culvert"}),
        line(408, 100, 200, -20, {"highway": "service", "tunnel": "building_passage"}),
        line(409, 100, 200, -40, {"highway": "residential", "bridge": "yes", "layer": "1"}),
        line(410, 100, 200, -60, {"highway": "cycleway", "tunnel": "yes"}),
        line(411, 100, 200, -80, {"highway": "width_test", "width": "9" * 400}),
        line(501, -200, 200, 120, {"waterway": "stream"}),
        line(502, -200, 200, 130, {"waterway": "stream", "tunnel": "culvert"}),
    ]
    return {"version": 0.6, "generator": "osm_3d_model fidelity fixture", "elements": elements}


def absurd_payload() -> dict:
    elements = [way(wid, rect(cx, cy, 12, 10), tags)
                for (wid, (tags, _expected)), (cx, cy) in zip(BUILDINGS.items(), _SPOTS)
                if wid in (101, 102, 103)]
    for index, (wid, (tags, _expected)) in enumerate(ABSURD.items()):
        elements.append(way(wid, rect(-150 + 40 * index, 160, 12, 10), tags))
    return {"version": 0.6, "generator": "osm_3d_model fidelity fixture", "elements": elements}


# --------------------------------------------------------------- checks
class Checks:
    def __init__(self) -> None:
        self.results: List[bool] = []

    def ok(self, name: str, condition, detail: str = "") -> bool:
        tag = "PASS" if condition else "FAIL"
        print(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""), flush=True)
        self.results.append(bool(condition))
        return bool(condition)


def study_area() -> QgsGeometry:
    to_utm = QgsCoordinateTransform(QgsCoordinateReferenceSystem.fromEpsgId(4326),
                                    QgsCoordinateReferenceSystem.fromEpsgId(EPSG), QgsProject.instance())
    centre = to_utm.transform(QgsPointXY(LON0, LAT0))
    return QgsGeometry.fromPointXY(centre).buffer(RADIUS_M, 64)


def features(layer) -> Dict[str, object]:
    return {str(feature["osm_id"]): feature for feature in layer.getFeatures()}


def value(feature, name: str):
    names = [field.name() for field in feature.fields()]
    return feature[name] if name in names else "<no such column>"


def check_heights(c: Checks, osm: dict) -> None:
    buildings = features(osm["buildings"])
    columns = [field.name() for field in osm["buildings"].fields()]
    c.ok("C3 columns: height, roof_height, min_height, height_source (1.9.1 had no min_height "
         "or height_source)", all(name in columns for name in
                                  ("height", "roof_height", "min_height", "height_source")), str(columns))
    reasons = {
        101: "1.9.1 kept the full 18 m as wall height and added roof:levels to the levels",
        102: "a height in feet", 103: "'2;3' takes the first value",
        104: "1.9.1 ignored min_height", 105: "C3 amendment: pitched roof, no roof tags",
        106: "C3 amendment: 0.3 x height below 10 m", 108: "comma decimal",
        109: "2.5 levels round half up to 3 (1.9.1: 2)", 110: "the default for building=yes",
        111: "roof:levels x 3 m taken off the height", 112: "never below 2 m",
        113: "building:min_level x 3 m is the base", 114: "a flat roof keeps the full height",
        115: "an unknown roof shape keeps the full height", 116: "4.5 levels -> 5 (1.9.1: 4)",
        117: "the default for building=house",
    }
    for wid, (tags, expected) in BUILDINGS.items():
        feature = buildings.get(str(wid))
        got = None if feature is None else (
            value(feature, "height"), value(feature, "roof_height"), value(feature, "min_height"),
            value(feature, "height_source"), value(feature, "building_levels"))
        c.ok(f"building {wid}: height/roof/base/source/levels = {expected} ({reasons[wid]})",
             got == expected, f"got {got}")
    estimated = sum(1 for feature in osm["buildings"].getFeatures()
                    if value(feature, "height_source") == "default")
    counts = osm.get("counts", {})
    c.ok("counts: buildings and estimated_heights (buildings at the default for their type) "
         "(1.9.1 had no estimated_heights)",
         counts.get("buildings") == osm["buildings"].featureCount()
         and counts.get("estimated_heights") == estimated and estimated >= 2,
         f"buildings={counts.get('buildings')} estimated={counts.get('estimated_heights')} "
         f"(layer: {osm['buildings'].featureCount()}, {estimated})")


def check_geometry(c: Checks, osm: dict) -> None:
    buildings = features(osm["buildings"])
    greens = features(osm["greens"])
    c.ok("an open building way is skipped, not closed into an invented polygon "
         "(1.9.1 closed it)", "107" not in buildings, "present" if "107" in buildings else "absent")
    want = ring_area(COURT_OUTER) - ring_area(COURT_INNER)
    got = buildings["201"].geometry().area() if "201" in buildings else None
    c.ok("courtyard relation: split and reversed outer ways joined, the inner ring is a hole (kept)",
         got is not None and abs(got - want) < 0.01 * want, f"{got} vs independent {want:.1f} m²")
    want = ring_area(PARK_OUTER) - ring_area(PARK_HOLE) + ring_area(PARK_ISLAND)
    lost = ring_area(PARK_OUTER) - ring_area(PARK_HOLE)
    got = greens["202"].geometry().area() if "202" in greens else None
    c.ok("a multipolygon island inside a hole is kept (1.9.1 subtracted it with the hole)",
         got is not None and abs(got - want) < 0.01 * want,
         f"{got if got is None else round(got, 1)} vs independent {want:.1f} m² (island lost: {lost:.1f})")


def check_ways(c: Checks, osm: dict) -> None:
    roads = features(osm["roads"])
    bikes = features(osm["bikelanes"])
    water = features(osm["waterlines"])
    gone = [wid for wid in ("402", "404", "405", "406") if wid in roads]
    c.ok("highway=proposed/abandoned/razed/demolished is not drawn as a road "
         "(1.9.1 drew them)", not gone, f"drawn: {gone}")
    tunnels = [wid for wid in ("403", "407") if wid in roads] + (["410"] if "410" in bikes else [])
    c.ok("tunnel=yes/culvert is not drawn as a surface road or bike lane (1.9.1 drew them)",
         not tunnels, f"drawn: {tunnels}")
    c.ok("a building passage, a bridge and an ordinary street stay roads (kept)",
         all(wid in roads for wid in ("401", "408", "409")), str(sorted(roads)))
    c.ok("a stream in a culvert is not drawn on the surface; an open stream is",
         "501" in water and "502" not in water, str(sorted(water)))
    width = value(roads["411"], "width") if "411" in roads else "missing"
    c.ok("an absurd width tag is dropped, not written as infinity (1.9.1 wrote inf)",
         width is None or (isinstance(width, float) and math.isfinite(width)) or str(width) == "NULL",
         str(width))


def check_trees(c: Checks, osm: dict) -> None:
    layer = osm["trees"]
    columns = [field.name() for field in layer.fields()]
    rows = list(layer.getFeatures())
    sources = [value(feature, "source") for feature in rows]
    osm_ids = sorted(str(feature["osm_id"]) for feature in rows if value(feature, "source") == "osm")
    planted = [feature for feature in rows if value(feature, "source") == "planted"]
    c.ok("trees carry source: 'osm' for mapped trees, 'planted' for the plugin's own "
         "(1.9.1 called all of them OSM trees)", "source" in columns and osm_ids == ["301", "302"]
         and planted and all(str(f["osm_id"]).startswith("scatter_") for f in planted)
         and set(sources) == {"osm", "planted"}, f"columns={columns} osm={osm_ids} planted={len(planted)}")
    heights = {str(f["osm_id"]): f["height"] for f in rows if value(f, "source") == "osm"}
    c.ok("a tree height tag that is not a finite number falls back to 6 m (1.9.1 wrote inf)",
         heights.get("301") == 8.0 and heights.get("302") == 6.0, str(heights))
    counts = osm.get("counts", {})
    c.ok("counts: osm_trees and planted_trees, and trees is their sum (1.9.1 had neither)",
         counts.get("osm_trees") == 2 and counts.get("planted_trees") == len(planted)
         and counts.get("trees") == 2 + len(planted),
         f"osm_trees={counts.get('osm_trees')} planted_trees={counts.get('planted_trees')} "
         f"trees={counts.get('trees')}")


def check_empty(c: Checks, module, area) -> None:
    """An empty answer must stop the build with plain words, not build nothing."""
    try:
        module.download_osm_for_area(area, EPSG, payload={"version": 0.6, "elements": []})
        message = ""
    except module.OsmDownloadError as error:
        message = str(error)
    c.ok("an empty answer stops the build and says the server sent nothing, without "
         "jargon (1.9.1: 'Overpass returned 0 elements for this area')",
         message.startswith("OpenStreetMap has no buildings, streets, green areas or trees in "
                            "this area") and "empty answer" in message and "Overpass" not in message,
         message[:120] or "no error: an empty model was built")


def check_absurd(c: Checks, module, area) -> None:
    try:
        osm = module.download_osm_for_area(area, EPSG, payload=absurd_payload())
        error = ""
    except Exception as exc:  # noqa: BLE001 - the check reports it
        osm, error = None, f"{type(exc).__name__}: {exc}"
    c.ok("absurd level and height tags never sink the export (1.9.1 failed the whole model)",
         osm is not None and osm["buildings"].featureCount() == 3 + len(ABSURD), error or
         f"{osm['buildings'].featureCount()} buildings")
    if osm is None:
        return
    buildings = features(osm["buildings"])
    for wid, (tags, expected) in ABSURD.items():
        feature = buildings.get(str(wid))
        got = None if feature is None else (
            value(feature, "height"), value(feature, "height_source"), value(feature, "building_levels"))
        shown = {key: (text if len(text) < 20 else text[:8] + "…") for key, text in tags.items()}
        c.ok(f"building {wid} {shown}: clamped to {expected} (levels 1-200, heights 1-1000 m)",
             got == expected, f"got {got}")
    with tempfile.TemporaryDirectory(prefix="osm3d-fidelity-") as folder:
        path = os.path.join(folder, "mybuildings.geojson")
        module.save_layer_to_geojson(osm["buildings"], path)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()

        def refuse(constant):
            raise ValueError(constant)

        try:
            document = json.loads(text, parse_constant=refuse)
            props = [feature["properties"] for feature in document["features"]]
            exported = all(isinstance(p.get("height"), (int, float)) and p.get("height_source")
                           in ("height", "levels", "default") and "min_height" in p
                           and "roof_height" in p for p in props)
        except (ValueError, KeyError, TypeError) as exc:
            exported = False
            props = [str(exc)]
    c.ok("the GeoJSON the viewer reads is strict JSON with finite C3 heights on every building",
         exported, f"{len(props)} buildings")


# ------------------------------------------------------------------ main
def run_all() -> bool:
    from osm_3d_model.tests._offline import isolate_network, refused

    isolate_network()
    from osm_3d_model import osm_download

    print("=" * 62)
    print(" osm_3d_model - model fidelity (P10, contract C3)")
    print("=" * 62, flush=True)
    c = Checks()
    started = time.monotonic()
    area = study_area()
    osm = None
    try:
        osm = osm_download.download_osm_for_area(area, EPSG, payload=main_payload())
    except Exception as error:  # noqa: BLE001 - reported as a failure
        traceback.print_exc()
        c.ok("the fixture builds", False, f"{type(error).__name__}: {error}")
    if osm is not None:
        for step in (check_heights, check_geometry, check_ways, check_trees):
            try:
                step(c, osm)
            except Exception as error:  # noqa: BLE001 - a crashed step is a failed step
                traceback.print_exc()
                c.ok(f"{step.__name__} ran to the end", False, f"{type(error).__name__}: {error}")
    for step in (check_absurd, check_empty):
        try:
            step(c, osm_download, area)
        except Exception as error:  # noqa: BLE001 - a crashed step is a failed step
            traceback.print_exc()
            c.ok(f"{step.__name__} ran to the end", False, f"{type(error).__name__}: {error}")
    c.ok("no request left this computer (guard)", not refused(), str(refused()[:3]))
    passed = sum(1 for result in c.results if result)
    print("-" * 62)
    print(f"  {passed}/{len(c.results)} passed in {time.monotonic() - started:.1f} s")
    return passed == len(c.results)


def main() -> None:
    profile = tempfile.TemporaryDirectory(prefix="osm_3d_model-fidelity-")
    app = QgsApplication([], False, profile.name, "external")
    app.initQgis()
    ok = False
    try:
        ok = run_all()
    except Exception as error:  # noqa: BLE001 - a crash is a failed suite
        traceback.print_exc()
        print(f"  [FAIL] {type(error).__name__}: {error}")
    sys.stdout.flush()
    sys.stderr.flush()
    # TRAPS 4.6: exit from the frame that holds the application.
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
