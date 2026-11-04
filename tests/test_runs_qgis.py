"""Runs, viewer server and partial results (1.10.0 items 4, 5 and 8).

What the user gets from these checks, each of which fails on 1.9.1 for the
reason it names:

* every build is a *run* in its own folder under the QGIS profile; two runs
  never overwrite each other, a file left open from an earlier run never
  breaks a new one, and the plugin's own ``web/data`` is never written;
* the project layers load from the run's GeoPackage, so they come back with
  their features after the project is saved and opened again;
* the viewer server serves ``/data`` from the chosen run, refuses requests for
  any other host name (DNS rebinding), never lists a folder, and says so in
  plain words when every port is busy;
* a DEM or basemap problem is a plain sentence in the summary; with the tile
  server unreachable the build still finishes in seconds after a handful of
  tile requests (1.9.1: 65 s and 605 requests), and no blank basemap is
  published.

Run from the folder that contains the plugin, with QGIS_CUSTOM_CONFIG_PATH set
to a throwaway folder (docs/TRAPS.md 4.6):

    python-qgis-ltr.bat -m osm_3d_model.tests.test_runs_qgis    # QGIS 3 LTR
    python-qgis.bat     -m osm_3d_model.tests.test_runs_qgis    # QGIS 4
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
sys.dont_write_bytecode = True

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsNetworkAccessManager,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QTimer  # noqa: E402

CENTRE_LAT, CENTRE_LON = 38.4245, 27.1457  # Izmir, UTM zone 35N
EPSG = 32635
RESULTS = []
STATE = {}


def check(name):
    """Register a check; its return value is the PASS note, an exception is the FAIL."""
    def wrap(func):
        def run():
            started = time.monotonic()
            try:
                note = func()
                RESULTS.append((name, True, note or ""))
            except Exception as error:  # noqa: BLE001 - one check, one verdict
                traceback.print_exc()
                RESULTS.append((name, False, f"{type(error).__name__}: {error}"))
            print(f"  ({name}: {time.monotonic() - started:.1f} s)", flush=True)
        run.check_name = name
        return run
    return wrap


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _offset(dx, dy):
    """(lat, lon) of a point dx m east and dy m north of the centre."""
    lat = CENTRE_LAT + dy / 111320.0
    lon = CENTRE_LON + dx / (111320.0 * math.cos(math.radians(CENTRE_LAT)))
    return lat, lon


def _ring(x0, y0, w, h):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]
    return [dict(zip(("lat", "lon"), _offset(x, y))) for x, y in pts]


def overpass_payload():
    """A small Overpass "out geom" answer: 4 buildings, a road, a park, trees, a bench."""
    elements = []
    for index, (x, y, tags) in enumerate((
        (-50, -30, {"building": "apartments", "building:levels": "5"}),
        (10, -30, {"building": "house", "building:levels": "2"}),
        (-50, 20, {"building": "commercial", "height": "12"}),
        (10, 20, {"building": "yes"}),
    )):
        elements.append({"type": "way", "id": 100 + index, "geometry": _ring(x, y, 24, 14), "tags": tags})
    road = [dict(zip(("lat", "lon"), _offset(x, 0))) for x in (-120, -40, 40, 120)]
    elements.append({"type": "way", "id": 300, "geometry": road,
                     "tags": {"highway": "residential", "name": "Test Street"}})
    elements.append({"type": "way", "id": 400, "geometry": _ring(40, 50, 40, 30),
                     "tags": {"leisure": "park", "name": "Test Park"}})
    for index, (x, y) in enumerate(((-20, 60), (-10, 62))):
        lat, lon = _offset(x, y)
        elements.append({"type": "node", "id": 500 + index, "lat": lat, "lon": lon,
                         "tags": {"natural": "tree"}})
    lat, lon = _offset(0, 8)
    elements.append({"type": "node", "id": 600, "lat": lat, "lon": lon, "tags": {"amenity": "bench"}})
    return {"version": 0.6, "generator": "osm_3d_model test_runs_qgis", "elements": elements}


def centre_utm():
    transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem.fromEpsgId(4326),
                                       QgsCoordinateReferenceSystem.fromEpsgId(EPSG), QgsProject.instance())
    point = transform.transform(QgsPointXY(CENTRE_LON, CENTRE_LAT))
    return point.x(), point.y()


def prepared_area():
    from osm_3d_model import builder

    cx, cy = centre_utm()
    # The O3-CODE audit area: an inscribed circle of r = 250 m (+5 m base).
    rect = QgsRectangle(cx - 250, cy - 250, cx + 250, cy + 250)
    return builder.prepare_study_area(QgsGeometry.fromRect(rect),
                                      QgsCoordinateReferenceSystem.fromEpsgId(EPSG), 300, "circle")


def build(**kwargs):
    """build_and_export with the fixture; ``web_root`` is a scratch folder (1.9.1 wrote there)."""
    from osm_3d_model import builder

    messages = []
    args = dict(dem_layer=None, basemap_layer=None, add_to_project=False,
                feedback=messages.append, prepared=prepared_area(), osm_payload=overpass_payload())
    args.update(kwargs)
    started = time.monotonic()
    summary = builder.build_and_export(None, None, str(STATE["web_root"]), **args)
    return summary, round(time.monotonic() - started, 2), messages


def run_folder(summary) -> Path:
    """Where a build put its files: the run folder, or the scratch web folder on 1.9.1."""
    run_dir = summary.get("run_dir")
    return Path(run_dir) if run_dir else STATE["web_root"]


def tree_digest(folder: Path) -> dict:
    out = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(folder))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def make_dem(path: Path, x0: float, y0: float, size=50, pixel=8.0):
    import numpy
    from osgeo import gdal, osr

    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(path), size, size, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, pixel, 0.0, y0, 0.0, -pixel))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(EPSG)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.WriteArray(numpy.fromfunction(lambda r, c: 40.0 + r * 0.2 + c * 0.1, (size, size)))
    band.SetNoDataValue(-9999)
    ds.FlushCache()
    ds = None
    layer = QgsRasterLayer(str(path), path.stem, "gdal")
    require(layer.isValid(), f"test DEM {path} did not load")
    return layer


def http_get(port, path, host=None):
    """GET from the local viewer server with an explicit Host header (no proxy)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        conn.endheaders()
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
@check("runs live under the QGIS profile (runs_root, new_run_dir)")
def check_runs_root():
    from osm_3d_model import builder

    require(hasattr(builder, "runs_root"), "builder has no runs_root(): 1.9.1 writes every build "
                                           "into the plugin's own web/data folder")
    root = builder.runs_root()
    profile = Path(QgsApplication.qgisSettingsDirPath()).resolve()
    require(root.resolve() == profile / "osm_3d_model" / "runs", f"runs_root() is {root}, not under {profile}")
    require(root.is_dir(), "runs_root() was not created")
    first = builder.new_run_dir("İzmir Konak")
    second = builder.new_run_dir("İzmir Konak")
    require(first != second and first.is_dir() and second.is_dir(), "two new_run_dir() calls gave one folder")
    require(re.fullmatch(r"\d{8}-\d{6}-Izmir-Konak", first.name), f"unexpected run name {first.name}")
    require(first.parent == root, "new_run_dir() is not inside runs_root()")
    for folder in (first, second):
        folder.rmdir()
    return f"{root} (profile {profile.name}); new_run_dir -> {first.name}, {second.name}"


@check("a build is a run: own folder, viewer files, GeoPackage, summary keys")
def check_first_run():
    web_before = tree_digest(PLUGIN_DIR / "web" / "data")
    summary, elapsed, _messages = build(add_to_project=True)
    STATE["run_a"] = summary
    old_keys = {"epsg", "shape", "shape_label", "theme", "radius_m", "width_m", "depth_m",
                "area_ha", "counts", "has_dem"}
    new_keys = {"run_dir", "gpkg_path", "warnings", "buildings", "estimated_heights",
                "osm_trees", "planted_trees"}
    require(old_keys <= set(summary), f"1.9.1 keys missing: {sorted(old_keys - set(summary))}")
    require(new_keys <= set(summary), f"summary has no {sorted(new_keys - set(summary))}: 1.9.1 "
                                      f"writes into the given web folder and keeps no run")
    scratch = STATE["web_root"]
    require(not (scratch / "data").exists(), f"the build wrote into the web folder it was given ({scratch})")
    require(tree_digest(PLUGIN_DIR / "web" / "data") == web_before, "the plugin's bundled web/data changed")
    run_dir = Path(summary["run_dir"])
    from osm_3d_model import builder

    require(run_dir.parent.resolve() == builder.runs_root().resolve(), f"{run_dir} is not under runs_root()")
    for rel in ("data/planx_manifest.json", "data/layers/mybuildings.geojson", "data/layers/roi.geojson",
                "layers.gpkg"):
        require((run_dir / rel).is_file(), f"run is missing {rel}")
    require(Path(summary["gpkg_path"]) == run_dir / "layers.gpkg", f"gpkg_path is {summary['gpkg_path']}")
    from osgeo import ogr

    ds = ogr.Open(summary["gpkg_path"])
    tables = sorted(ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount()))
    ds = None
    expected = sorted(table for _role, table, _label in builder.PROJECT_LAYERS)
    require(tables == expected, f"GeoPackage tables {tables} != {expected}")
    counts = summary["counts"]
    for key in ("buildings", "estimated_heights", "osm_trees", "planted_trees"):
        require(summary[key] == int(counts.get(key, 0) or 0), f"summary[{key!r}] != counts[{key!r}]")
    require(summary["buildings"] == 4, f"expected 4 buildings, got {summary['buildings']}")
    require(summary["warnings"] == [], f"unexpected warnings {summary['warnings']}")
    return f"{run_dir.name} in {elapsed}s; GeoPackage tables: {len(tables)}; web folders untouched"


@check("a second run gets its own folder; files held open from run 1 do not break it")
def check_second_run_with_open_files():
    first = STATE.get("run_a")
    require(first is not None, "no first run to compare with")
    run_a = run_folder(first)
    held = [open(run_a / "data" / "layers" / "mybuildings.geojson", "rb"),  # noqa: SIM115
            open(run_a / "data" / "planx_manifest.json", "rb")]  # noqa: SIM115
    if (run_a / "layers.gpkg").is_file():
        held.append(open(run_a / "layers.gpkg", "rb"))  # noqa: SIM115
    before = tree_digest(run_a)
    try:
        summary, elapsed, _messages = build(add_to_project=True)
    finally:
        for handle in held:
            handle.close()
    STATE["run_b"] = summary
    run_b = run_folder(summary)
    require(run_b != run_a, f"both runs wrote into {run_a}")
    require(tree_digest(run_a) == before, "run 1's files changed when run 2 was built")
    return f"run 2 {run_b.name} in {elapsed}s with {len(held)} run-1 files open; run 1 unchanged"


@check("project layers come back with their features after saving and reopening")
def check_project_round_trip():
    run_b = STATE.get("run_b")
    require(run_b is not None, "no second run")
    project = QgsProject.instance()
    names = sorted(layer.name() for layer in project.mapLayers().values())
    require("OSM Buildings" in names and "OSM Study Area" in names, f"project layers: {names}")
    path = STATE["tmp"] / "round_trip.qgz"
    require(project.write(str(path)), f"QgsProject.write({path}) failed")
    project.clear()
    require(project.read(str(path)), f"QgsProject.read({path}) failed")
    by_name = {layer.name(): layer for layer in project.mapLayers().values()}
    report = []
    gpkg = Path(run_b.get("gpkg_path") or "").resolve() if run_b.get("gpkg_path") else None
    for name in ("OSM Buildings", "OSM Roads", "OSM Greens & Water", "OSM Trees", "OSM Benches", "OSM Study Area"):
        layer = by_name.get(name)
        require(layer is not None, f"{name} is missing after reopening; layers: {sorted(by_name)}")
        require(layer.isValid(), f"{name} is not valid after reopening")
        count = layer.featureCount()
        require(count > 0, f"{name} has {count} features after reopening ({layer.providerType()} layer: "
                           f"1.9.1 keeps the layers in memory)")
        source = Path(layer.source().split("|", 1)[0]).resolve()
        require(gpkg is not None and source == gpkg, f"{name} reads {source}, not the run's GeoPackage")
        report.append(f"{name}={count}")
    renderer = by_name["OSM Buildings"].renderer()
    require(renderer is not None and renderer.type() == "categorizedSymbol",
            f"OSM Buildings lost its styling: {renderer.type() if renderer else None}")
    return ", ".join(report) + "; buildings keep the categorized style"


@check("latest_run_dir() is the newest complete run")
def check_latest_run():
    from osm_3d_model import builder

    require(hasattr(builder, "latest_run_dir"), "builder has no latest_run_dir()")
    run_b = run_folder(STATE["run_b"])
    require(builder.latest_run_dir() == run_b, f"latest_run_dir() = {builder.latest_run_dir()}, expected {run_b}")
    broken = builder.new_run_dir("broken")
    (broken / "data").mkdir()
    (broken / "data" / "planx_manifest.json").write_text("{not json", encoding="utf-8")
    empty = builder.new_run_dir("empty")
    require(builder.latest_run_dir() == run_b, "a newer folder without a valid manifest was offered")
    STATE["junk"] = [broken, empty]
    return f"{run_b.name}; newer folders without a valid manifest are skipped"


@check("tiles unreachable: done within 25 s, few tile requests, a warning and no blank basemap")
def check_basemap_unreachable():
    """The O3-CODE audit area (r = 250 m): 1.9.1 took 65 s and 605 tile requests here.

    The event-loop ticks are reported, not claimed as a 1.9.1 failure: 1.9.1's
    synchronous render also spun a local loop while it waited; what changes is
    that the wait is bounded and starts with a small probe.
    """
    from osm_3d_model.tests import _offline

    layer = QgsRasterLayer(_offline.CLOSED_TILES_URI, "OpenStreetMap", "wms")
    require(layer.isValid(), "the closed-port XYZ layer did not load")
    tiles = []

    def count_tiles(request):
        url = request.url()
        if str(url.host()) == "127.0.0.1" and url.port() == 9:
            tiles.append(str(url.toString()))

    preprocessor = QgsNetworkAccessManager.setRequestPreprocessor(count_tiles)
    ticks = [0]
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    timer.start(100)
    try:
        summary, elapsed, messages = build(basemap_layer=layer)
    finally:
        timer.stop()
        QgsNetworkAccessManager.removeRequestPreprocessor(preprocessor)
    STATE["run_c"] = summary
    run_c = run_folder(summary)
    png = run_c / "data" / "basemap" / "basemap.png"
    manifest = json.loads((run_c / "data" / "planx_manifest.json").read_text(encoding="utf-8"))
    warnings = summary.get("warnings")
    detail = f"{elapsed}s, {len(tiles)} tile requests, {ticks[0]} event-loop ticks"
    problems = []
    if elapsed > 25.0:
        problems.append(f"the build took {elapsed}s")
    if len(tiles) >= 100:
        problems.append(f"{len(tiles)} tile requests")
    if png.exists():
        problems.append(f"a {png.stat().st_size}-byte basemap PNG was published")
    if manifest.get("basemap") is not None:
        problems.append("the manifest points at a basemap")
    if not (isinstance(warnings, list) and any(w.startswith("The basemap was skipped") for w in warnings)):
        problems.append(f"no basemap sentence in summary['warnings'] ({warnings!r}; feedback: {messages[-1:]})")
    if ticks[0] < 5:
        problems.append("QGIS handled no events while the basemap rendered")
    require(not problems, "; ".join(problems) + f" - {detail}")
    return f"{detail}; warning: {next(w for w in warnings if w.startswith('The basemap'))}"


def png_tile(pattern: bool) -> bytes:
    """A 256 px PNG tile: three colours, or one flat colour."""
    from qgis.PyQt.QtCore import QBuffer, QIODevice
    from qgis.PyQt.QtGui import QColor, QImage, QPainter

    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(QColor("#4a7fb5"))
    if pattern:
        painter = QPainter(image)
        painter.fillRect(0, 0, 128, 128, QColor("#e8d9a8"))
        painter.fillRect(128, 128, 128, 128, QColor("#3b5e2b"))
        painter.end()
    buffer = QBuffer()
    buffer.open(getattr(getattr(QIODevice, "OpenModeFlag", QIODevice), "WriteOnly"))
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class TileServer:
    """Serves one PNG for every /z/x/y.png request, on 127.0.0.1 only."""

    def __init__(self, tile: bytes):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(tile)))
                self.end_headers()
                self.wfile.write(tile)

            def log_message(self, *args):
                return None

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def layer(self, name):
        port = self.httpd.server_address[1]
        uri = f"type=xyz&url=http://127.0.0.1:{port}/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=19&zmin=0&crs=EPSG3857"
        layer = QgsRasterLayer(uri, name, "wms")
        require(layer.isValid(), f"tile layer {name} did not load")
        return layer

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@check("a basemap that draws something is published; a one-colour result is not")
def check_basemap_published_or_refused():
    good, flat = TileServer(png_tile(True)), TileServer(png_tile(False))
    try:
        summary, elapsed, _messages = build(basemap_layer=good.layer("tiles"))
        run = run_folder(summary)
        png = run / "data" / "basemap" / "basemap.png"
        manifest = json.loads((run / "data" / "planx_manifest.json").read_text(encoding="utf-8"))
        require(png.is_file() and png.stat().st_size > 1000, f"no basemap PNG for reachable tiles ({elapsed}s)")
        require((manifest.get("basemap") or {}).get("image") == "data/basemap/basemap.png",
                f"manifest basemap: {manifest.get('basemap')}")
        require(not summary.get("warnings"), f"warnings for a good basemap: {summary.get('warnings')}")
        good_note = f"reachable tiles: {png.stat().st_size} bytes in {elapsed}s"
        summary, elapsed, _messages = build(basemap_layer=flat.layer("flat tiles"))
        run = run_folder(summary)
        png = run / "data" / "basemap" / "basemap.png"
        warnings = summary.get("warnings")
        require(not png.exists(), f"a one-colour basemap PNG was published ({png.stat().st_size if png.exists() else 0}"
                                  f" bytes, warnings {warnings!r})")
        require(isinstance(warnings, list) and any("blank" in w for w in warnings), f"warnings: {warnings!r}")
        return f"{good_note}; one colour refused in {elapsed}s: {warnings[0]}"
    finally:
        good.close()
        flat.close()


@check("a DEM that does not overlap the area is a plain warning, not a fake DEM")
def check_dem_outside():
    cx, cy = centre_utm()
    dem = make_dem(STATE["tmp"] / "dem_far.tif", cx + 100000, cy + 100000)
    summary, elapsed, _messages = build(dem_layer=dem)
    STATE["run_d"] = summary
    run_d = run_folder(summary)
    warnings = summary.get("warnings")
    require(summary.get("has_dem") is False, f"has_dem is {summary.get('has_dem')} for a DEM 100 km away")
    require(not (run_d / "data" / "dem" / "mydem.tif").exists(), "a DEM file was published for a DEM 100 km away")
    require(warnings == ["The DEM was skipped: it does not overlap the area."],
            f"summary['warnings'] is {warnings!r}")
    return f"{elapsed}s: {warnings[0]}"


@check("an overlapping DEM is clipped into the run folder")
def check_dem_inside():
    if not STATE.get("processing"):
        return "SKIP: QGIS Processing is not available in this runtime"
    cx, cy = centre_utm()
    dem = make_dem(STATE["tmp"] / "dem_here.tif", cx - 200, cy + 200)
    summary, elapsed, _messages = build(dem_layer=dem)
    STATE["run_e"] = summary
    run_e = run_folder(summary)
    require(summary.get("has_dem") is True, f"has_dem is {summary.get('has_dem')}: {summary.get('warnings')}")
    require(summary.get("run_dir") and (run_e / "data" / "dem" / "mydem.tif").is_file(),
            f"no data/dem/mydem.tif in a run folder ({run_e})")
    require(summary.get("warnings") == [], f"warnings: {summary.get('warnings')}")
    return f"{elapsed}s: {run_e.name}/data/dem/mydem.tif"


def _server():
    from osm_3d_model.server import Osm3dServer

    server = Osm3dServer(str(PLUGIN_DIR / "web"), start_port=8170, end_port=8199)
    server.start()
    return server


@check("the server serves /data from the chosen run, or the bundled sample")
def check_server_data_dir():
    server = _server()
    try:
        require(hasattr(server, "set_data_dir"), "Osm3dServer has no set_data_dir(): 1.9.1 always serves "
                                                 "the plugin's own web/data")
        run_b = run_folder(STATE["run_b"])
        server.set_data_dir(run_b)
        status, body = http_get(server.port, "/data/planx_manifest.json")
        require(status == 200 and body == (run_b / "data" / "planx_manifest.json").read_bytes(),
                f"/data/planx_manifest.json -> {status}, not the run's manifest")
        status, body = http_get(server.port, "/data/layers/mybuildings.geojson")
        require(status == 200 and body == (run_b / "data" / "layers" / "mybuildings.geojson").read_bytes(),
                f"/data/layers/mybuildings.geojson -> {status}, not the run's file")
        status, _ = http_get(server.port, "/src/index.html")
        require(status == 200, f"/src/index.html -> {status} while serving a run")
        escapes = {path: http_get(server.port, path)[0]
                   for path in ("/data/../layers.gpkg", "/data/%2e%2e/layers.gpkg",
                                "/data/..%2flayers.gpkg", "/data/../../metadata.txt")}
        require(all(code == 404 for code in escapes.values()), f"path traversal out of /data: {escapes}")
        server.set_data_dir(None)
        status, body = http_get(server.port, "/data/planx_manifest.json")
        sample = (PLUGIN_DIR / "web" / "data" / "planx_manifest.json").read_bytes()
        require(status == 200 and body == sample, f"set_data_dir(None) -> {status}, not the bundled sample")
        return f"run {run_b.name} and the bundled sample both served on port {server.port}"
    finally:
        server.stop()


@check("the server refuses requests for any other host name (DNS rebinding)")
def check_server_host():
    server = _server()
    try:
        port = server.port
        results = {host: http_get(port, "/data/planx_manifest.json", host=host)[0]
                   for host in ("attacker.example", f"attacker.example:{port}", "127.0.0.1",
                                f"192.168.1.10:{port}", "")}
        require(all(code in (403, 421) for code in results.values()), f"foreign Host answers: {results}")
        ok = {host: http_get(port, "/src/index.html", host=host)[0]
              for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}")}
        require(all(code == 200 for code in ok.values()), f"own Host answers: {ok}")
        address = server._httpd.server_address
        require(address[0] == "127.0.0.1", f"bound to {address}")
        return f"refused {results}; served {ok}; bound to {address[0]}"
    finally:
        server.stop()


@check("the server never lists a folder")
def check_server_listing():
    server = _server()
    try:
        answers = {path: http_get(server.port, path) for path in ("/", "/data/", "/src/assets/", "/data/layers/")}
        listed = {path: status for path, (status, body) in answers.items()
                  if status == 200 or b"Directory listing" in body}
        require(not listed, f"folders listed: {listed}")
        return ", ".join(f"{path} -> {status}" for path, (status, _body) in answers.items())
    finally:
        server.stop()


def hold_ports(count=3, first=8150, last=8400):
    """Listen on ``count`` consecutive 127.0.0.1 ports; return (start port, sockets)."""
    for start in range(first, last, count):
        held = []
        for port in range(start, start + count):
            sock = socket.socket()
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            try:
                sock.bind(("127.0.0.1", port))
                sock.listen(1)
            except OSError:
                sock.close()
                break
            held.append(sock)
        if len(held) == count:
            return start, held
        for sock in held:
            sock.close()
    raise AssertionError(f"could not hold {count} consecutive local ports for the test")


@check("every port busy: ServerUnavailable with a plain sentence")
def check_server_ports_busy():
    from osm_3d_model import server as server_module

    start, held = hold_ports()
    try:
        server = server_module.Osm3dServer(str(PLUGIN_DIR / "web"), start_port=start, end_port=start + 2)
        unavailable = getattr(server_module, "ServerUnavailable", None)
        require(unavailable is not None, "server.py has no ServerUnavailable")
        try:
            server.start()
        except unavailable as error:
            text = str(error)
            require(text and "Errno" not in text and "OSError" not in text and len(text) < 300,
                    f"not a plain sentence: {text!r}")
            return text
        except OSError as error:
            raise AssertionError(f"raised {type(error).__name__} ({error}), not ServerUnavailable") from error
        server.stop()
        raise AssertionError("start() succeeded with every port busy")
    finally:
        for sock in held:
            sock.close()


@check("delete_runs_except keeps the chosen run and removes the others")
def check_delete_runs():
    from osm_3d_model import builder

    require(hasattr(builder, "delete_runs_except"), "builder has no delete_runs_except()")
    keep = run_folder(STATE["run_b"])
    others = [path for path in builder.runs_root().iterdir() if path.is_dir() and path != keep]
    removed = builder.delete_runs_except(keep)
    left = sorted(path.name for path in builder.runs_root().iterdir())
    require(keep.is_dir() and (keep / "layers.gpkg").is_file(), "the kept run was touched")
    require(removed == len(others) and left == [keep.name],
            f"removed {removed} of {len(others)} other runs; left: {left}")
    require(builder.latest_run_dir() == keep, "latest_run_dir() changed")
    return f"removed {removed}, kept {keep.name}"


def _make_junction(link: Path, target: Path) -> bool:
    """A Windows directory junction (no admin rights needed); False elsewhere."""
    try:
        import _winapi
    except ImportError:
        return False
    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError:
        return False
    return link.exists()


@check("links and junctions never lead out of a run (the server and Delete old runs)")
def check_links_stay_inside():
    from osm_3d_model import builder

    outside = STATE["tmp"] / "precious"
    outside.mkdir(exist_ok=True)
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    keep = run_folder(STATE["run_b"])
    in_data = keep / "data" / "linked"
    in_runs = builder.runs_root() / "20990101-000000-junction"
    if not (_make_junction(in_data, outside) and _make_junction(in_runs, outside)):
        return "skipped: directory junctions are not available here"
    server = _server()
    try:
        server.set_data_dir(keep)
        status, _ = http_get(server.port, "/data/linked/precious.txt")
        require(status == 404, f"/data/linked/precious.txt -> {status}: the server followed a "
                               "junction out of the run")
        try:
            status, _ = http_get(server.port, "/data/planx%00.json")
        except (OSError, http.client.HTTPException) as error:
            status = f"connection dropped ({type(error).__name__})"
        require(status == 404, f"a NUL byte in the path -> {status}, not 404")
    finally:
        server.stop()
        if in_data.exists():
            os.rmdir(in_data)  # removes the junction only, never its target
    try:
        removed = builder.delete_runs_except(keep)
        require((outside / "precious.txt").is_file(), "Delete old runs reached through a junction")
        leftovers = [path.name for path in builder.runs_root().iterdir() if path.name.startswith(".deleting-")]
        require(in_runs.exists() and not leftovers and removed == 0,
                f"the junction was renamed or counted as a run (removed={removed}, leftovers={leftovers})")
    finally:
        if in_runs.exists() or in_runs.is_symlink():
            os.rmdir(in_runs)
    return "junctions refused by the server and left alone by Delete old runs"


CHECKS = (
    check_runs_root,
    check_first_run,
    check_second_run_with_open_files,
    check_project_round_trip,
    check_latest_run,
    check_basemap_unreachable,
    check_basemap_published_or_refused,
    check_dem_outside,
    check_dem_inside,
    check_server_data_dir,
    check_server_host,
    check_server_listing,
    check_server_ports_busy,
    check_delete_runs,
    check_links_stay_inside,
)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _profile_is_throwaway(profile_root: Path) -> bool:
    """runs_root() follows the QGIS profile: it must be this suite's or the runner's temp one.

    QGIS_CUSTOM_CONFIG_PATH (set by pf verify) wins over the profile folder
    given to QgsApplication; either is fine, the user's real profile is not.
    """
    settings_dir = Path(QgsApplication.qgisSettingsDirPath())
    real = Path(os.environ.get("APPDATA", "~")).expanduser() / "QGIS"
    if _inside(settings_dir, real):
        return False
    allowed = [profile_root]
    if os.environ.get("QGIS_CUSTOM_CONFIG_PATH"):
        allowed.append(Path(os.environ["QGIS_CUSTOM_CONFIG_PATH"]))
    return any(_inside(settings_dir, root) for root in allowed)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="o3runs-"))
    profile_root = tmp / "profile"
    profile_root.mkdir()
    app = QgsApplication([], True, str(profile_root), "external")
    app.initQgis()
    print("=" * 66)
    print(f" osm_3d_model - runs, viewer server, partial results ({Qgis.version()})")
    print("=" * 66, flush=True)
    from osm_3d_model.tests._offline import isolate_network

    # Zero public requests: every plugin host points at a closed local port.
    isolate_network()
    STATE["tmp"] = tmp
    STATE["web_root"] = tmp / "web"
    STATE["web_root"].mkdir()
    plugins_path = os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    if plugins_path not in sys.path:
        sys.path.append(plugins_path)
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        STATE["processing"] = True
    except Exception as error:  # noqa: BLE001 - optional for one check
        print(f"  (Processing unavailable: {error})")
    if not _profile_is_throwaway(profile_root):
        RESULTS.append(("throwaway profile", False,
                        f"QGIS uses {QgsApplication.qgisSettingsDirPath()}, not {profile_root}: refusing to "
                        f"write runs into a real profile"))
    else:
        for step in CHECKS:
            step()
    QgsProject.instance().clear()
    app.processEvents()
    if _profile_is_throwaway(profile_root):
        runs = Path(QgsApplication.qgisSettingsDirPath()) / "osm_3d_model"
        shutil.rmtree(runs, ignore_errors=True)
    passed = sum(1 for _name, ok, _note in RESULTS if ok)
    for name, ok, note in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {note}")
    print("-" * 66)
    print(f"  {passed}/{len(RESULTS)} passed")
    ok = bool(RESULTS) and passed == len(RESULTS)
    shutil.rmtree(tmp, ignore_errors=True)
    sys.stdout.flush()
    sys.stderr.flush()
    # docs/TRAPS.md 4.6: exit from the frame that holds the application.
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
