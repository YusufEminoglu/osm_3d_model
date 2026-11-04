"""The real 3D OSM Model dialog and plugin against fake servers on 127.0.0.1.

Run under a QGIS Python, from the directory that contains the plugin folder,
with QGIS_CUSTOM_CONFIG_PATH set to a throwaway directory (TRAPS 4.6; pf
verify does both):

    python-qgis-ltr.bat -m osm_3d_model.tests.test_dialog_qgis   # QGIS 3.44
    python-qgis.bat     -m osm_3d_model.tests.test_dialog_qgis   # QGIS 4.2

Offscreen: no window reaches the desktop. No request leaves this computer:
tests._offline guards every host before anything else runs, the Overpass and
Nominatim hosts are fake servers on 127.0.0.1, and every browser or
file-manager open is only recorded.

Covers items 2 and 3 of DEVELOPMENT_SET_v1.10 and the dialog side of items 4,
5 and 8 (P4, P7, P8, P9, P12, P13, P14). Each check's docstring names what
1.9.1 did instead; every check fails on 1.9.1 for that reason.

Where a contract of the set (C1 download, C2 runs and build) is missing from
the code under test -- 1.9.1, or a tree whose other half is not merged yet --
the suite supplies a stand-in and says so on an [INFO] line: the dialog side
is then still tested, and the end-to-end check reports the missing contract.
1.9.1's own download refuses plain-HTTP addresses, so its stand-in posts to
the fake server the way 1.9.1 does: one blocking request that nothing can
interrupt.
"""
from __future__ import annotations

import contextlib
import dataclasses
import faulthandler
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SYSTEM_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
if os.path.isdir(_SYSTEM_FONTS):
    # Offscreen Qt has no fonts of its own (TRAPS 4.3: blank text on 3.44, boxes
    # on 4.2). Given the system's, it draws real glyphs, so the layout measures
    # as it does on screen -- still without a window on the desktop.
    os.environ.setdefault("QT_QPA_FONTDIR", _SYSTEM_FONTS)
if not os.environ.get("QGIS_CUSTOM_CONFIG_PATH"):
    # Never the user's own profile (TRAPS 4.6).
    os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp(prefix="osm3d-dialog-profile-")

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsTask,
)

ALSANCAK = (27.1428, 38.4361)          # lon, lat: a dense neighbourhood of Izmir
WORK_AREA_H = 728                      # a 1366x768 laptop minus the taskbar
VIEW_HINT = "Zoom in to a neighbourhood, or search a place."
CANCELLED = "Download cancelled."
ERR_COLOR = "#b91c1c"
FORBIDDEN_WORDS = ("canvas extent", "overpass mirror", "planx 3d city engine")
STANDINS = []                          # contract pieces this run supplies itself


class CheckFailed(AssertionError):
    pass


def check(condition, message):
    if not condition:
        raise CheckFailed(message)


def fail_if(problems):
    if problems:
        raise CheckFailed("; ".join(problems))


def pump(seconds=0.05):
    app = QgsApplication.instance()
    end = time.monotonic() + seconds
    while True:
        app.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.005)


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        pump(0.02)
    return bool(predicate())


@contextlib.contextmanager
def patched(owner, name, value):
    """Set ``owner.name`` for the block; restore it (or remove it) afterwards."""
    missing = object()
    old = owner.__dict__.get(name, missing) if isinstance(owner, type) else getattr(owner, name, missing)
    setattr(owner, name, value)
    try:
        yield value
    finally:
        if old is missing:
            with contextlib.suppress(AttributeError):
                delattr(owner, name)
        else:
            setattr(owner, name, old)


def _enum(owner, scope, name):
    holder = getattr(owner, scope, None)
    return getattr(holder, name) if holder is not None and hasattr(holder, name) else getattr(owner, name)


def task_done(task) -> bool:
    if task is None:
        return True
    try:
        return task.status() in (_enum(QgsTask, "TaskStatus", "Complete"),
                                 _enum(QgsTask, "TaskStatus", "Terminated"))
    except RuntimeError:  # QGIS already deleted the finished task
        return True


def distance_m(lon1, lat1, lon2, lat2) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def first_hectares(text: str) -> Optional[float]:
    match = re.search(r"([\d,]+(?:\.\d+)?)\s*ha\b", text or "")
    return float(match.group(1).replace(",", "")) if match else None


# ---------------------------------------------------------------------------
# Fake servers (127.0.0.1 only)
# ---------------------------------------------------------------------------
def synth_town(min_lat, min_lon, max_lat, max_lon):
    """A small synthetic town in the middle of the bbox; the open sea at 0°N 0°E.

    After the v1.10 UX audit's fake Overpass server.
    """
    if min_lat <= 0 <= max_lat and min_lon <= 0 <= max_lon:
        return []
    els = []
    nid = [1000]

    def nxt():
        nid[0] += 1
        return nid[0]

    dlat, dlon = max_lat - min_lat, max_lon - min_lon

    def pt(fy, fx):
        return {"lat": min_lat + dlat * fy, "lon": min_lon + dlon * fx}

    def rect(fy0, fx0, fy1, fx1):
        return [pt(fy0, fx0), pt(fy0, fx1), pt(fy1, fx1), pt(fy1, fx0), pt(fy0, fx0)]

    types = ["apartments", "house", "commercial", "retail", "school", "yes", "office", "residential"]
    n = 6
    for i in range(n):
        for j in range(n):
            fy0, fx0 = 0.25 + 0.5 * i / n, 0.25 + 0.5 * j / n
            size = 0.5 / n * 0.6
            tags = {"building": types[(i * n + j) % len(types)]}
            if (i + j) % 3 == 0:
                tags["building:levels"] = str(2 + (i * j) % 7)
            els.append({"type": "way", "id": nxt(), "tags": tags,
                        "geometry": rect(fy0, fx0, fy0 + size, fx0 + size)})
    for k in range(n + 1):
        f = 0.25 + 0.5 * k / n - 0.5 / n * 0.2
        els.append({"type": "way", "id": nxt(), "tags": {"highway": "residential", "name": f"Test Street {k}"},
                    "geometry": [pt(f, 0.2), pt(f, 0.8)]})
        els.append({"type": "way", "id": nxt(), "tags": {"highway": "service"},
                    "geometry": [pt(0.2, f), pt(0.8, f)]})
    els.append({"type": "way", "id": nxt(), "tags": {"leisure": "park", "name": "Test Park"},
                "geometry": rect(0.45, 0.45, 0.55, 0.56)})
    for t in range(12):
        els.append({"type": "node", "id": nxt(), "tags": {"natural": "tree"},
                    **pt(0.3 + 0.4 * ((t * 37) % 100) / 100, 0.3 + 0.4 * ((t * 61) % 100) / 100)})
    for tag in ({"amenity": "bench"}, {"highway": "street_lamp"}):
        els.append({"type": "node", "id": nxt(), "tags": dict(tag), **pt(0.5, 0.42)})
    return els


class _Quiet(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return None

    def _send(self, status, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        with contextlib.suppress(OSError):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)


class _OverpassHandler(_Quiet):
    def do_POST(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        query = parse_qs(body.decode("utf-8", "replace")).get("data", [""])[0]
        srv = self.server
        with srv.lock:
            srv.requests.append({"t": time.monotonic(), "query": query})
            mode, release = srv.mode, srv.release
        if mode == "hang":
            release.wait(120)
            self.close_connection = True
            return
        match = re.search(r"\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)", query)
        elements = synth_town(*(float(v) for v in match.groups())) if match else []
        self._send(200, json.dumps({
            "version": 0.6, "generator": "osm_3d_model test fake",
            "osm3s": {"timestamp_osm_base": "2026-11-02T08:00:00Z"}, "elements": elements}))


class FakeOverpass:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OverpassHandler)
        self.httpd.daemon_threads = True
        self.httpd.lock = threading.Lock()
        self.httpd.requests = []
        self.httpd.mode = "ok"
        self.httpd.release = threading.Event()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/interpreter"
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="fake-overpass").start()

    @property
    def requests(self):
        with self.httpd.lock:
            return list(self.httpd.requests)

    @property
    def released(self) -> bool:
        return self.httpd.release.is_set()

    def reset(self, mode):
        self.release_all()
        with self.httpd.lock:
            self.httpd.requests = []
            self.httpd.mode = mode
            self.httpd.release = threading.Event()

    def release_all(self):
        with self.httpd.lock:
            self.httpd.release.set()


PLACES = (
    ("Alsancak, Konak, İzmir, Türkiye", 38.43700, 27.14300, (38.4280, 38.4470, 27.1300, 27.1550)),
    ("Alsancak Garı, Konak, İzmir, Türkiye", 38.43901, 27.14801, None),
    ("Alsancak Mahallesi, Kayseri, Türkiye", 38.73010, 35.48720, (38.7250, 38.7350, 35.4800, 35.4950)),
    ("Alsancak, Girne, Kıbrıs", 35.34480, 33.19010, None),
    ("Alsancak Stadyumu, İzmir, Türkiye", 38.43520, 27.14010, None),
    ("Alsancak Sokağı, Ankara, Türkiye", 39.91990, 32.85410, None),
    ("Alsancak Caddesi, Bursa, Türkiye", 40.18260, 29.06650, None),
)


class _NominatimHandler(_Quiet):
    def do_GET(self):  # noqa: N802
        srv = self.server
        with srv.lock:
            srv.requests.append(self.path)
        time.sleep(srv.delay_s)
        query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
        rows = []
        if "alsancak" in query.lower():
            for index, (name, lat, lon, bbox) in enumerate(PLACES):
                row = {"place_id": index + 1, "licence": "Data © OpenStreetMap contributors, ODbL 1.0",
                       "osm_type": "relation", "osm_id": 1000 + index, "lat": f"{lat}", "lon": f"{lon}",
                       "display_name": name, "class": "place", "type": "suburb", "importance": 0.5}
                if bbox:
                    row["boundingbox"] = [f"{v}" for v in bbox]
                rows.append(row)
        self._send(200, json.dumps(rows))


class FakeNominatim:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NominatimHandler)
        self.httpd.daemon_threads = True
        self.httpd.lock = threading.Lock()
        self.httpd.requests = []
        self.httpd.delay_s = 1.0   # long enough to show the UI thread never waits for it
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/search"
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="fake-nominatim").start()

    @property
    def requests(self):
        with self.httpd.lock:
            return list(self.httpd.requests)


# ---------------------------------------------------------------------------
# Contract stand-ins (only where the code under test lacks the contract)
# ---------------------------------------------------------------------------
def _standin_post(endpoint, query, feedback):
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QByteArray, QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    request = QNetworkRequest(QUrl(endpoint))
    request.setHeader(_enum(QNetworkRequest, "KnownHeaders", "ContentTypeHeader"),
                      "application/x-www-form-urlencoded")
    client = QgsBlockingNetworkRequest()
    code = client.post(request, QByteArray(("data=" + quote(query)).encode("ascii")), False, feedback)
    ok = code == _enum(QgsBlockingNetworkRequest, "ErrorCode", "NoError")
    return ok, client


def install_contract_standins():
    """Supply C1/C2 pieces the code under test does not have, and list them."""
    from osm_3d_model import builder, osm_download, server

    if not hasattr(osm_download, "fetch_overpass_ex"):
        error = getattr(osm_download, "OsmDownloadError", RuntimeError)

        class OsmDownloadCancelled(error):
            def __init__(self, message=CANCELLED):
                super().__init__(message)

        @dataclasses.dataclass(frozen=True)
        class FetchOutcome:
            payload: dict
            from_cache: bool
            saved_at: Optional[float]
            host: str

        def fetch_overpass_ex(min_lat, min_lon, max_lat, max_lon, *, use_cache=True,
                              status=None, feedback=None):
            endpoint = osm_download.OVERPASS_ENDPOINTS[0]
            host = urlsplit(endpoint).netloc or endpoint
            if status is not None:
                status(f"Asking {host}…")
            query = f"[out:json];(nwr({min_lat},{min_lon},{max_lat},{max_lon}););out geom;"
            ok, client = _standin_post(endpoint, query, feedback)
            if feedback is not None and feedback.isCanceled():
                raise OsmDownloadCancelled()
            if not ok:
                raise error(f"{host} did not answer ({client.errorMessage()}).")
            payload = json.loads(bytes(client.reply().content()).decode("utf-8"))
            return FetchOutcome(payload, False, time.time(), host)

        def fetch_overpass(min_lat, min_lon, max_lat, max_lon, timeout_s=60, feedback=None,
                           use_cache=True, cancel_check=None):
            # As 1.9.1 does: one blocking request, which nothing interrupts.
            if cancel_check is not None and cancel_check():
                raise error("OSM download cancelled.")
            return fetch_overpass_ex(min_lat, min_lon, max_lat, max_lon, use_cache=use_cache).payload

        osm_download.OsmDownloadCancelled = OsmDownloadCancelled
        osm_download.FetchOutcome = FetchOutcome
        osm_download.fetch_overpass_ex = fetch_overpass_ex
        osm_download.fetch_overpass = fetch_overpass
        STANDINS.append("C1 osm_download.fetch_overpass_ex")

    def runs_root():
        root = Path(QgsApplication.qgisSettingsDirPath()) / "osm_3d_model" / "runs"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def new_run_dir(label=""):
        base = time.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
        for index in range(1, 1000):
            candidate = runs_root() / (base if index == 1 else f"{base}-{index}")
            if not candidate.exists():
                candidate.mkdir()
                return candidate
        raise OSError("no free run folder name")

    def latest_run_dir():
        found = [p for p in runs_root().iterdir() if (p / "data" / "planx_manifest.json").is_file()]
        return max(found, key=lambda p: ((p / "data" / "planx_manifest.json").stat().st_mtime, p.name)) \
            if found else None

    def delete_runs_except(keep):
        import shutil

        removed = 0
        for child in runs_root().iterdir():
            if child.is_dir() and (keep is None or child.resolve() != Path(keep).resolve()):
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed

    for name, function in (("runs_root", runs_root), ("new_run_dir", new_run_dir),
                           ("latest_run_dir", latest_run_dir), ("delete_runs_except", delete_runs_except)):
        if not hasattr(builder, name):
            setattr(builder, name, function)
            STANDINS.append(f"C2 builder.{name}")
    if not hasattr(server, "ServerUnavailable"):
        server.ServerUnavailable = type("ServerUnavailable", (RuntimeError,), {})
        STANDINS.append("C2 server.ServerUnavailable")
    if not hasattr(server.Osm3dServer, "set_data_dir"):
        def set_data_dir(self, path):
            self.data_dir_for_test = path

        server.Osm3dServer.set_data_dir = set_data_dir
        STANDINS.append("C2 Osm3dServer.set_data_dir")


def make_run(label="test") -> Path:
    """A finished run folder (valid manifest), as the builder leaves one."""
    from osm_3d_model import builder

    run = Path(builder.new_run_dir(label))
    manifest = run / "data" / "planx_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"schema": "planx-3d-city-manifest/v1", "plugin": "osm_3d_model"}),
                        encoding="utf-8")
    return run


def clear_runs():
    import shutil

    from osm_3d_model import builder

    root = Path(builder.runs_root())
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def file_url(path) -> str:
    from qgis.PyQt.QtCore import QUrl

    return QUrl.fromLocalFile(str(path)).toString()


# ---------------------------------------------------------------------------
# A QGIS-shaped window and recorders
# ---------------------------------------------------------------------------
class MessageBar:
    def __init__(self):
        self.notes = []

    def _push(self, level, *args):
        title, text = (str(args[0]), str(args[1])) if len(args) > 1 else ("", str(args[0]) if args else "")
        self.notes.append((level, title, text))

    def pushSuccess(self, *args, **_kw):  # noqa: N802 - QGIS naming
        self._push("success", *args)

    def pushInfo(self, *args, **_kw):  # noqa: N802
        self._push("info", *args)

    def pushWarning(self, *args, **_kw):  # noqa: N802
        self._push("warning", *args)

    def pushCritical(self, *args, **_kw):  # noqa: N802
        self._push("critical", *args)

    def pushMessage(self, *args, **_kw):  # noqa: N802
        self._push("message", *args)


class Iface:
    def __init__(self, window, canvas, bar):
        self._window = window
        self._canvas = canvas
        self._bar = bar
        self.active_layer = None
        self.toolbar_icons = []
        self.menu_entries = []

    def mainWindow(self):  # noqa: N802
        return self._window

    def mapCanvas(self):  # noqa: N802
        return self._canvas

    def messageBar(self):  # noqa: N802
        return self._bar

    def activeLayer(self):  # noqa: N802
        return self.active_layer

    def addToolBarIcon(self, action):  # noqa: N802
        self.toolbar_icons.append(action)

    def removeToolBarIcon(self, action):  # noqa: N802
        if action in self.toolbar_icons:
            self.toolbar_icons.remove(action)

    def addPluginToMenu(self, menu, action):  # noqa: N802
        self.menu_entries.append((menu, action))

    def removePluginMenu(self, menu, action):  # noqa: N802
        self.menu_entries = [e for e in self.menu_entries if e[1] is not action]


class BoxRecorder:
    """Every QMessageBox the plugin raises is recorded and answered, never shown."""

    def __init__(self):
        self.shown = []
        self.answer = None

    def install(self):
        from qgis.PyQt.QtWidgets import QMessageBox

        recorder = self
        self.answer = QMessageBox.StandardButton.Yes

        def exec_(box, *_args, **_kwargs):
            kind = "critical" if box.icon() == QMessageBox.Icon.Critical else "box"
            recorder.shown.append((kind, box.text()))
            return 0

        def static(kind):
            def show(_parent, _title, text, *_args, **_kwargs):
                recorder.shown.append((kind, str(text)))
                return recorder.answer if kind == "question" else QMessageBox.StandardButton.Ok
            return staticmethod(show)

        QMessageBox.exec = exec_
        QMessageBox.exec_ = exec_
        for kind in ("critical", "warning", "information", "question"):
            setattr(QMessageBox, kind, static(kind))

    def critical(self):
        return [text for kind, text in self.shown if kind == "critical"]

    def modal(self):
        return [entry for entry in self.shown if entry[0] != "question"]


def unclipped(widget, top) -> bool:
    """True when no scroll area between ``widget`` and ``top`` cuts it off."""
    from qgis.PyQt.QtCore import QPoint
    from qgis.PyQt.QtWidgets import QAbstractScrollArea

    parent = widget.parentWidget()
    while parent is not None and parent is not top:
        area = parent.parentWidget()
        if isinstance(area, QAbstractScrollArea) and area.viewport() is parent:
            corner = widget.mapTo(parent, QPoint(0, 0))
            if not (0 <= corner.y() and corner.y() + widget.height() <= parent.height()):
                return False
        parent = parent.parentWidget()
    return True


class Rig:
    def __init__(self):
        from qgis.gui import QgsLayerTreeMapCanvasBridge, QgsMapCanvas
        from qgis.PyQt.QtWidgets import QMainWindow

        self.window = QMainWindow()
        self.window.resize(1200, 760)
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.bridge = QgsLayerTreeMapCanvasBridge(QgsProject.instance().layerTreeRoot(), self.canvas)
        self.window.show()
        self.bar = MessageBar()
        self.iface = Iface(self.window, self.canvas, self.bar)
        self.boxes = BoxRecorder()
        self.boxes.install()
        self.overpass_fake = FakeOverpass()
        self.nominatim = FakeNominatim()
        self.plugins = []
        self.cache_dir = tempfile.mkdtemp(prefix="osm3d-dialog-cache-")
        pump(0.2)

    # -- plugin --------------------------------------------------------------
    def new_plugin(self):
        import osm_3d_model

        settings = QgsSettings()
        for key, value in (("max_ha", 150.0), ("shape", "circle"), ("source", "canvas"),
                           ("auto_open", True), ("theme", "Editorial Paper")):
            settings.setValue(f"osm_3d_model/{key}", value)
        plugin = osm_3d_model.classFactory(self.iface)
        plugin.initGui()
        if plugin.server is not None:
            plugin.server.start_port, plugin.server.end_port = 8170, 8189  # 127.0.0.1, off 8120
        self.plugins.append(plugin)
        return plugin

    def open_dialog(self):
        plugin = self.new_plugin()
        plugin.action.trigger()
        pump(0.4)
        return plugin, plugin.dialog

    def set_view(self, lon, lat, width_m, height_m):
        to_map = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                        self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
        centre = to_map.transform(QgsPointXY(lon, lat))
        self.canvas.setExtent(QgsRectangle(centre.x() - width_m / 2, centre.y() - height_m / 2,
                                           centre.x() + width_m / 2, centre.y() + height_m / 2))
        self.canvas.refresh()
        pump(0.1)

    def view_centre_wgs(self):
        to_wgs = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(),
                                        QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
        point = to_wgs.transform(self.canvas.extent().center())
        return point.x(), point.y()

    def bands(self):
        from qgis.gui import QgsRubberBand

        return [item for item in self.canvas.scene().items() if isinstance(item, QgsRubberBand)]

    def overpass(self, mode="ok"):
        from osm_3d_model import osm_download

        self.overpass_fake.reset(mode)
        osm_download.OVERPASS_ENDPOINTS = (self.overpass_fake.url,)
        if hasattr(osm_download, "CACHE_DIR"):
            osm_download.CACHE_DIR = self.cache_dir
        forget = getattr(osm_download, "forget_hosts", None)
        if callable(forget):
            forget()
        return self.overpass_fake

    def summary_text(self, dialog) -> str:
        from qgis.PyQt.QtWidgets import QLabel

        texts = [label.text() for label in dialog.summary_card.findChildren(QLabel) if label.text()]
        text = re.sub(r"<br\s*/?>", "\n", "\n".join(texts))
        return re.sub(r"<[^>]+>", "", text)

    # -- between checks --------------------------------------------------------
    def stop_runs(self):
        for plugin in self.plugins:
            task = getattr(plugin, "_task", None)
            if task is not None:
                with contextlib.suppress(RuntimeError):
                    task.cancel()
        self.overpass_fake.release_all()
        wait_for(lambda: all(not getattr(p, "_running", False) for p in self.plugins), 20)

    def finish(self):
        from osm_3d_model import osm_download

        self.stop_runs()
        for plugin in self.plugins:
            with contextlib.suppress(Exception):
                plugin.unload()
        self.plugins = []
        wait_for(lambda: QgsApplication.taskManager().countActiveTasks() == 0, 15)
        QgsProject.instance().clear()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.iface.active_layer = None
        self.boxes.shown.clear()
        self.bar.notes.clear()
        osm_download.OVERPASS_ENDPOINTS = ("http://127.0.0.1:9/api/interpreter",)
        pump(0.2)


def start_hanging_run(rig):
    plugin, dialog = rig.open_dialog()
    rig.set_view(*ALSANCAK, 1400, 800)
    overpass = rig.overpass("hang")
    pump(0.4)
    dialog.run_button.click()
    check(wait_for(lambda: overpass.requests, 10),
          f"the download never reached the fake server (status {dialog.status.text()!r})")
    pump(0.3)
    return plugin, dialog, overpass


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_status_moves_while_waiting(rig):
    """P4: while a server hangs, the status names it and counts the seconds.

    1.9.1 showed "Downloading OpenStreetMap data in the background..." for the
    whole wait (180 s with every host hanging): the line never changed.
    """
    plugin, dialog, _overpass = start_hanging_run(rig)
    first = dialog.status.text()
    seen = [first]
    end = time.monotonic() + 5.0
    while time.monotonic() < end:
        pump(0.1)
        if dialog.status.text() != seen[-1]:
            seen.append(dialog.status.text())
    problems = []
    if len(seen) < 2:
        problems.append(f"the status stayed {first!r} for 5 s while the server hung")
    if not any("127.0.0.1" in text for text in seen):
        problems.append(f"no status named the server being asked: {seen[-3:]}")
    if not any(re.search(r"\b\d+ s\b", text) for text in seen):
        problems.append(f"no status counted the seconds: {seen[-3:]}")
    fail_if(problems)
    return f"{len(seen)} status lines in 5 s, last {seen[-1]!r}"


def check_cancel_within_two_seconds(rig):
    """P4: Cancel stops a hanging download within 2 s and ends neutrally.

    1.9.1 had no Cancel in the dialog; cancelling the QGIS task did nothing
    until the request ended 52-58 s later, in a red Critical box.
    """
    plugin, dialog, overpass = start_hanging_run(rig)
    task = getattr(plugin, "_task", None)
    button = getattr(dialog, "cancel_button", None)
    has_button = button is not None and button.isVisible() and button.isEnabled()
    clicked = time.monotonic()
    if has_button:
        button.click()
    else:
        for active in QgsApplication.taskManager().activeTasks():
            active.cancel()
    ended = wait_for(lambda: not plugin._running and task_done(task), 2.0)
    elapsed = time.monotonic() - clicked
    still_hanging = not overpass.released
    rig.overpass_fake.release_all()   # 1.9.1 only ends once the server lets go
    wait_for(lambda: not plugin._running, 20)
    status = dialog.status.text()
    problems = []
    if not has_button:
        problems.append("the dialog offers no Cancel button during the download")
    if not ended:
        problems.append(f"the download was still running {elapsed:.1f} s after Cancel")
    elif not still_hanging:
        problems.append("the run ended only because the server let go")
    if status != CANCELLED:
        problems.append(f"the status reads {status!r}, not {CANCELLED!r}")
    if ERR_COLOR in dialog.status.styleSheet():
        problems.append("the cancelled run is shown in the error colour")
    if rig.boxes.critical():
        problems.append(f"a Critical box appeared: {rig.boxes.critical()[0][:90]!r}")
    fail_if(problems)
    # QGIS's own task manager (the status-bar progress) cancels through C++.
    plugin.action.trigger()
    overpass = rig.overpass("hang")
    pump(0.3)
    dialog.run_button.click()
    check(wait_for(lambda: overpass.requests, 10), "the second download never reached the fake server")
    pump(0.3)
    task = plugin._task
    clicked = time.monotonic()
    QgsApplication.taskManager().cancelAll()
    ended_qgis = wait_for(lambda: not plugin._running and task_done(task), 2.0)
    elapsed_qgis = time.monotonic() - clicked
    check(ended_qgis and not overpass.released,
          f"cancelling in QGIS's task manager left the download running for {elapsed_qgis:.1f} s")
    check(dialog.status.text() == CANCELLED and not rig.boxes.critical(),
          f"after a task-manager cancel the dialog reads {dialog.status.text()!r}")
    return (f"ended {elapsed:.2f} s after Cancel and {elapsed_qgis:.2f} s after a task-manager "
            f"cancel, each with {status!r}, no box")


def check_close_hides_and_run_continues(rig):
    """P4: Esc and the close button hide the dialog while the download carries on.

    1.9.1 ignored Esc and close during a run: the dialog stayed in front.
    """
    from qgis.PyQt.QtCore import QEvent, Qt
    from qgis.PyQt.QtGui import QKeyEvent

    plugin, dialog, overpass = start_hanging_run(rig)
    QgsApplication.sendEvent(dialog, QKeyEvent(_enum(QEvent, "Type", "KeyPress"), Qt.Key.Key_Escape,
                                               Qt.KeyboardModifier.NoModifier))
    pump(0.3)
    problems = []
    if dialog.isVisible():
        problems.append("Esc did not hide the dialog during a run")
    if not (plugin._running and plugin._task is not None):
        problems.append("hiding the dialog stopped the run")
    plugin.action.trigger()
    pump(0.3)
    dialog.close()
    pump(0.3)
    if dialog.isVisible():
        problems.append("the close button did not hide the dialog during a run")
    if not (plugin._running and not overpass.released):
        problems.append("closing the dialog stopped the run")
    fail_if(problems)
    return "Esc and close hid the dialog; the download was still waiting on the server"


def check_toolbar_shows_dialog_during_run(rig):
    """P4: the toolbar icon brings the dialog back during a run, with the live status.

    1.9.1's show_dialog returned early while a run was going: a hidden dialog
    stayed hidden until the run ended.
    """
    plugin, dialog, _overpass = start_hanging_run(rig)
    dialog.hide()
    pump(0.2)
    plugin.action.trigger()
    pump(0.3)
    check(dialog.isVisible(), "the toolbar icon did not show the dialog during a run")
    before = dialog.status.text()
    pump(1.4)
    after = dialog.status.text()
    check(after != before and "127.0.0.1" in after,
          f"the dialog came back without a live status: {before!r} -> {after!r}")
    return f"shown during the run with {after!r}"


def check_failure_in_dialog_not_a_box(rig):
    """P4/P5: a failed download is told in the dialog word for word, with a short
    message-bar note, and no modal box.

    1.9.1 put every failure in a red Critical box over QGIS and the message bar
    said nothing.
    """
    from osm_3d_model import osm_download

    message = ("Every OpenStreetMap server was busy: overpass.private.coffee answered HTTP 429 "
               "and rests for a minute; overpass-api.de did not answer within 55 s. "
               "Try again in a few minutes.")
    error = osm_download.OsmDownloadError

    def failing(*_args, **_kwargs):
        raise error(message)

    plugin, dialog = rig.open_dialog()
    rig.set_view(*ALSANCAK, 1400, 800)
    rig.overpass("ok")
    with patched(osm_download, "fetch_overpass_ex", failing), patched(osm_download, "fetch_overpass", failing):
        dialog.run_button.click()
        wait_for(lambda: not plugin._running, 10)
    status = dialog.status.text()
    notes = [note for note in rig.bar.notes if note[0] in ("warning", "critical")]
    problems = []
    if status != message:
        problems.append(f"the dialog says {status!r} instead of the download's own message")
    if rig.boxes.modal():
        problems.append(f"a modal box appeared over QGIS: {rig.boxes.modal()[0][1][:80]!r}")
    if not notes:
        problems.append("the QGIS message bar got no note")
    elif len(notes[-1][2]) > 160:
        problems.append(f"the message-bar note is not short ({len(notes[-1][2])} characters)")
    if not dialog.run_button.isEnabled():
        problems.append("Run stayed disabled after the failure")
    fail_if(problems)
    return f"dialog kept the message; message bar: {notes[-1][2]!r}"


def check_place_search(rig):
    """P7: search a place with QGIS's Nominatim geocoder off the UI thread, pick one
    of at most five results, and the model boundary is centred on it.

    1.9.1 had no place search: on an empty project the only way to an area was
    Add OSM basemap, which shows the whole world.
    """
    try:
        from osm_3d_model import place_search
    except ImportError:
        place_search = None
    check(place_search is not None and hasattr(place_search, "GEOCODER_ENDPOINT"),
          "there is no place search (place_search.GEOCODER_ENDPOINT)")
    plugin, dialog = rig.open_dialog()
    check(hasattr(dialog, "place_edit") and hasattr(dialog, "place_list"),
          "the dialog has no place search box")
    nominatim = rig.nominatim
    with patched(place_search, "GEOCODER_ENDPOINT", nominatim.url):
        dialog.place_edit.setText("Alsancak")
        asked = time.monotonic()
        dialog.place_button.click()
        returned = time.monotonic() - asked
        check(returned < 0.5, f"the search held the UI thread for {returned:.1f} s")
        check(wait_for(lambda: dialog.place_list.count() > 0, 15),
              f"no place was listed: {dialog.place_note.text()!r}")
        listed = dialog.place_list.count()
        name, lat, lon, _bbox = PLACES[0]
        dialog.place_list.itemClicked.emit(dialog.place_list.item(0))
        pump(0.8)
        centre_lon, centre_lat = rig.view_centre_wgs()
        bands = [band for band in rig.bands() if band.isVisible()]
        band_off = None
        if bands:
            centroid = bands[0].asGeometry().centroid().asPoint()
            to_wgs = QgsCoordinateTransform(rig.canvas.mapSettings().destinationCrs(),
                                            QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
            point = to_wgs.transform(centroid)
            band_off = distance_m(point.x(), point.y(), lon, lat)
        readout = dialog.area_readout.text()
        # On an empty project QGIS zooms to a first layer's extent (the world, for
        # tiles); Add OSM basemap must leave the picked place framed.
        dialog.basemap_button.click()
        pump(0.8)
        basemap_lon, basemap_lat = rig.view_centre_wgs()
        basemap_off = distance_m(basemap_lon, basemap_lat, lon, lat)
        # A name with no match says so.
        dialog.place_edit.setText("Nowhere at all")
        dialog.place_button.click()
        wait_for(lambda: "No place found" in dialog.place_note.text(), 15)
        no_match = dialog.place_note.text()
    problems = []
    if listed != 5:
        problems.append(f"{listed} places listed; the geocoder sent 7 and at most 5 are offered")
    if not nominatim.requests or "q=Alsancak" not in nominatim.requests[0]:
        problems.append(f"the fake geocoder was not asked: {nominatim.requests}")
    off = distance_m(centre_lon, centre_lat, lon, lat)
    if off > 25:
        problems.append(f"the map is centred {off:.0f} m from {name}")
    if band_off is None:
        problems.append("no model boundary is drawn after picking the place")
    elif band_off > 25:
        problems.append(f"the model boundary is centred {band_off:.0f} m from the place")
    if not readout.startswith("Model boundary") or first_hectares(readout) is None:
        problems.append(f"the readout does not give the model boundary: {readout!r}")
    if basemap_off > 25:
        problems.append(f"Add OSM basemap moved the map {basemap_off / 1000:,.0f} km away from the place")
    if "No place found" not in no_match:
        problems.append(f"a search with no match says {no_match!r}")
    fail_if(problems)
    return (f"5 of 7 places offered, search returned in {returned * 1000:.0f} ms, "
            f"map centred {off:.1f} m and boundary {band_off:.1f} m from {name}")


def check_readout_is_the_model_boundary(rig):
    """P7: the readout gives the model boundary (shape and hectares the build will
    use), within 1 %, not the map view.

    1.9.1 measured the whole map view: about 77 ha for a view whose model is a
    30.6 ha circle.
    """
    from osm_3d_model import builder

    plugin, dialog = rig.open_dialog()
    problems = []
    report = []
    for width, height, shape in ((1400, 800, "circle"), (2600, 1500, "circle"), (1400, 800, "extent")):
        dialog.shape_combo.setCurrentIndex(dialog.shape_combo.findData(shape))
        dialog.ha_spin.setValue(150.0)
        rig.set_view(*ALSANCAK, width, height)
        pump(0.8)
        text = dialog.area_readout.text()
        shown = first_hectares(text)
        prepared = builder.prepare_study_area(
            QgsGeometry.fromRect(rig.canvas.extent()), rig.canvas.mapSettings().destinationCrs(),
            max_ha=150.0, shape=shape)
        expected = prepared["clip_utm"].area() / 10000.0
        report.append(f"{shape} {width}x{height}: {shown} ha vs {expected:.1f} ha")
        if shown is None or abs(shown - expected) > 0.01 * expected:
            problems.append(f"{shape}, {width}x{height} m view: the readout says {text!r}, "
                            f"the model boundary is {expected:.1f} ha")
    fail_if(problems)
    return "; ".join(report)


def check_world_view_refused(rig):
    """P7: a map view wider than 50 km is refused with a hint, not built as open sea.

    1.9.1 built a 150 ha circle in the sea at 3°E 0°N from the world view that
    Add OSM basemap leaves on an empty project.
    """
    plugin, dialog = rig.open_dialog()
    overpass = rig.overpass("ok")
    rig.canvas.setExtent(QgsRectangle(-87794965, -44927335, 87794965, 44927335))
    pump(0.8)
    readout = dialog.area_readout.text()
    dialog.run_button.click()
    pump(1.5)
    started = bool(overpass.requests) or getattr(plugin, "_task", None) is not None or plugin._running
    status = dialog.status.text()
    wait_for(lambda: not plugin._running, 20)
    # Web Mercator metres are stretched by 1/cos(latitude): ask for true widths.
    stretch = 1.0 / math.cos(math.radians(ALSANCAK[1]))
    rig.set_view(*ALSANCAK, 60000 * stretch, 40000 * stretch)
    pump(0.8)
    wide = dialog.area_readout.text()
    rig.set_view(*ALSANCAK, 30000 * stretch, 20000 * stretch)
    pump(0.8)
    city = dialog.area_readout.text()
    problems = []
    if started:
        problems.append("Run on a world view started a download")
    if VIEW_HINT not in status:
        problems.append(f"Run gave no hint: {status!r}")
    if VIEW_HINT not in readout:
        problems.append(f"the readout for the world view gives no hint: {readout!r}")
    if VIEW_HINT not in wide:
        problems.append(f"a 60 km view is not refused: {wide!r}")
    if VIEW_HINT in city:
        problems.append(f"a 30 km view is refused: {city!r}")
    if rig.boxes.modal():
        problems.append("a modal box appeared")
    fail_if(problems)
    return f"world view refused: {status!r}"


def check_outline_follows_view_and_unload_removes_it(rig):
    """P7: the model boundary is drawn on the map while the dialog is open, follows
    the view after a short pause, hides on close and is removed on unload.

    1.9.1 drew nothing on the map: the user saw the boundary only after the build.
    """
    from osm_3d_model import builder

    plugin, dialog = rig.open_dialog()
    rig.set_view(*ALSANCAK, 1400, 800)
    pump(0.8)
    bands = [band for band in rig.bands() if band.isVisible()]
    check(bands, "no model boundary is drawn on the map while the dialog is open")
    band = bands[0]
    first = band.asGeometry().boundingBox()
    calls = []
    real_prepare = builder.prepare_study_area

    def counting(*args, **kwargs):
        calls.append(time.monotonic())
        return real_prepare(*args, **kwargs)

    with patched(builder, "prepare_study_area", counting):
        for step in range(6):   # six quick pans
            rig.set_view(ALSANCAK[0] + 0.002 * step, ALSANCAK[1], 1400, 800)
        pump(0.8)
    moved = band.asGeometry().boundingBox()
    problems = []
    if abs(moved.center().x() - first.center().x()) < 500:
        problems.append("the boundary did not follow the map")
    if len(calls) > 2:
        problems.append(f"six quick pans measured the boundary {len(calls)} times")
    dialog.close()
    pump(0.3)
    if any(item.isVisible() for item in rig.bands()):
        problems.append("the boundary stayed on the map after the dialog closed")
    plugin.action.trigger()
    pump(0.6)
    again = [item for item in rig.bands() if item.isVisible()]
    if not again:
        problems.append("the boundary did not come back with the dialog")
    plugin.unload()
    rig.plugins.remove(plugin)
    pump(0.3)
    if any(item.scene() is not None for item in again) or rig.bands():
        problems.append("unload left the boundary on the map")
    fail_if(problems)
    return f"drawn, followed six pans with {len(calls)} measurement(s), gone on close, removed on unload"


def check_run_visible_at_1366x728(rig):
    """P12: with Advanced open on a 1366x728 work area, Run stays on screen.

    1.9.1 kept Run inside the scrolling content: below the fold with Advanced open.
    """
    from qgis.PyQt.QtCore import QPoint

    plugin, dialog = rig.open_dialog()
    dialog.adv_toggle.setChecked(True)
    pump(0.2)
    limit = WORK_AREA_H - 90   # the dialog's own allowance for window chrome
    dialog._available_height = lambda: limit
    dialog.resize(dialog.minimumWidth(), dialog.height())
    dialog._fit_to_contents()
    pump(0.3)
    dialog.scroll.verticalScrollBar().setValue(0)
    pump(0.2)
    run = dialog.run_button
    top = run.mapTo(dialog, QPoint(0, 0)).y()
    scrolls = dialog.scroll.verticalScrollBar().maximum() > 0
    problems = []
    if dialog.height() > limit:
        problems.append(f"the dialog is {dialog.height()} px tall, over the {limit} px available")
    if not (0 <= top and top + run.height() <= dialog.height() and unclipped(run, dialog) and run.isVisible()):
        problems.append(f"Run is at y={top}..{top + run.height()} in a {dialog.height()} px dialog, "
                        "out of sight in the scrolling content")
    if not scrolls:
        problems.append("the content does not scroll, so the check proves nothing")
    fail_if(problems)
    return f"Run at y={top}..{top + run.height()} of {dialog.height()} px; the cards scroll"


def _collect_texts(dialog):
    from qgis.PyQt.QtWidgets import QComboBox, QWidget

    texts = [dialog.windowTitle()]
    for widget in dialog.findChildren(QWidget):
        for name in ("text", "toolTip", "placeholderText", "title"):
            getter = getattr(widget, name, None)
            if callable(getter):
                with contextlib.suppress(Exception):
                    value = getter()
                    if isinstance(value, str) and value:
                        texts.append(value)
        if isinstance(widget, QComboBox):
            texts.extend(widget.itemText(i) for i in range(widget.count()))
    return texts


FAKE_SUMMARY = {
    "epsg": 32635, "shape": "circle", "shape_label": "Inscribed circle", "theme": "Editorial Paper",
    "radius_m": 312.3, "width_m": None, "depth_m": None, "area_ha": 30.6,
    "counts": {"buildings": 64, "roads": 16, "bikelanes": 3, "greens": 1, "trees": 64,
               "waterlines": 2, "waterareas": 0, "busstops": 3, "benches": 3, "lights": 3, "trashbins": 3},
    "has_dem": False, "warnings": [
        "The DEM was skipped: it does not overlap the area.",
        "The basemap was skipped: no tile arrived within 20 s.",
    ],
    "buildings": 64, "estimated_heights": 41, "osm_trees": 1, "planted_trees": 63,
}


def check_plain_words(rig):
    """P14: plain words: "map view", not "canvas extent"; no "Overpass mirrors",
    no bare EPSG code in the status or summary, no "PlanX 3D City engine".

    1.9.1 said "Current map view (canvas extent)" and "PlanX 3D City engine",
    and its status line showed "EPSG:32635".
    """
    plugin, dialog = rig.open_dialog()
    dialog.adv_toggle.setChecked(True)
    pump(0.2)
    texts = _collect_texts(dialog)
    joined = "\n".join(texts).lower()
    problems = [f"the dialog says {word!r}" for word in FORBIDDEN_WORDS if word in joined]
    if "map view" not in joined:
        problems.append("the dialog never says 'map view'")
    dialog.set_status("Study area: Inscribed circle r = 312 m (30.6 ha) +5 m base, EPSG:32635.", busy=True)
    status = dialog.status.text()
    dialog.set_status("", busy=False)
    if "EPSG:32635" in status:
        problems.append(f"the status shows a bare EPSG code: {status!r}")
    summary = dict(FAKE_SUMMARY, run_dir=str(make_run("words")), gpkg_path="layers.gpkg")
    dialog.show_summary(summary, "http://127.0.0.1:8170/src/")
    pump(0.2)
    shown = rig.summary_text(dialog)
    if re.search(r"EPSG:\d+", shown):
        problems.append(f"the summary shows a bare EPSG code: {shown!r}")
    fail_if(problems)
    return f"{len(texts)} texts checked; status reads {status!r}"


def _stub_run(rig, outcome, summary, after_build=None):
    """Patches for a run whose download and build are stand-ins with fixed answers."""
    from osm_3d_model import builder, main_plugin, osm_download

    calls = []

    def fetch_ex(*args, **kwargs):
        calls.append({"bbox": tuple(round(float(v), 6) for v in args[:4]), **kwargs})
        status = kwargs.get("status")
        if status is not None:
            status("Using the copy downloaded 3 hours ago; no request sent."
                   if outcome.from_cache else "Asking 127.0.0.1…")
        return outcome

    def fetch_191(*args, **kwargs):
        calls.append({"bbox": tuple(round(float(v), 6) for v in args[:4]), **kwargs})
        return outcome.payload

    def build(*_args, **kwargs):
        feedback = kwargs.get("feedback")
        if feedback is not None:
            feedback("Study area: Inscribed circle r = 312 m (30.6 ha) +5 m base, EPSG:32635.")
        if after_build is not None:
            after_build()
        return dict(summary, run_dir=str(make_run("stub")))

    stack = contextlib.ExitStack()
    stack.enter_context(patched(osm_download, "fetch_overpass_ex", fetch_ex))
    stack.enter_context(patched(osm_download, "fetch_overpass", fetch_191))
    stack.enter_context(patched(builder, "build_and_export", build))
    if hasattr(main_plugin, "build_and_export"):
        stack.enter_context(patched(main_plugin, "build_and_export", build))
    return stack, calls


def _outcome(from_cache, age_s=0.0):
    from osm_3d_model import osm_download

    payload = {"version": 0.6, "elements": [{"type": "node", "id": 1, "lat": 38.43, "lon": 27.14,
                                             "tags": {"natural": "tree"}}]}
    return osm_download.FetchOutcome(payload=payload, from_cache=from_cache,
                                     saved_at=time.time() - age_s, host="" if from_cache else "127.0.0.1")


def check_summary_tells_the_whole_result(rig):
    """P8/P9: the summary lists every warning, says how old a saved copy is and
    offers Download fresh, splits OSM from planted trees, counts estimated heights.

    1.9.1 showed "✓ Export ready" with counts only: a skipped DEM flashed by
    in 0.2 s, a cache-served run never said so, and 63 planted trees counted as
    OSM trees.
    """
    plugin, dialog = rig.open_dialog()
    rig.set_view(*ALSANCAK, 1400, 800)
    rig.overpass("ok")
    stack, _calls = _stub_run(rig, _outcome(True, 3 * 3600 + 120), FAKE_SUMMARY)
    with stack:
        dialog.run_button.click()
        wait_for(lambda: not plugin._running, 15)
        pump(0.3)
    text = rig.summary_text(dialog)
    fresh = getattr(dialog, "fresh_button", None)
    problems = [f"warning not listed: {w!r}" for w in FAKE_SUMMARY["warnings"] if w not in text]
    if not (dialog.summary_card.isVisible() and unclipped(dialog.summary_title, dialog)):
        problems.append("the summary is not scrolled into view after the run")
    if "3 hours" not in text or "no request" not in text:
        problems.append("the summary does not say the data is a 3-hour-old saved copy")
    if fresh is None or not fresh.isVisible():
        problems.append("no Download fresh button for the saved copy")
    if "1 from OSM + 63 planted" not in text:
        problems.append("trees are not given as '1 from OSM + 63 planted'")
    if "41 of 64 building heights are estimated from the building type" not in text:
        problems.append("the estimated building heights are not counted")
    if "warning" not in dialog.summary_title.text().lower():
        problems.append(f"the title reads {dialog.summary_title.text()!r} despite two warnings")
    if rig.boxes.modal():
        problems.append("a modal box appeared")
    fail_if(problems)
    return " | ".join(line for line in text.splitlines()[:4])


def check_download_fresh(rig):
    """P9: Download fresh builds the same area again with use_cache False.

    1.9.1 had no way past a saved copy except clearing the whole cache.
    """
    plugin, dialog = rig.open_dialog()
    rig.set_view(*ALSANCAK, 1400, 800)
    rig.overpass("ok")
    stack, calls = _stub_run(rig, _outcome(True, 3600), FAKE_SUMMARY)
    with stack:
        dialog.run_button.click()
        wait_for(lambda: not plugin._running, 15)
        fresh = getattr(dialog, "fresh_button", None)
        check(fresh is not None and fresh.isVisible(), "there is no Download fresh button")
        rig.set_view(ALSANCAK[0] + 0.05, ALSANCAK[1], 1400, 800)   # the map has moved on
        pump(0.4)
        fresh.click()
        wait_for(lambda: len(calls) >= 2 and not plugin._running, 15)
    check(len(calls) >= 2, "Download fresh started no download")
    check(calls[-1].get("use_cache") is False,
          f"Download fresh asked with use_cache={calls[-1].get('use_cache')!r}")
    check(calls[-1]["bbox"] == calls[0]["bbox"], "Download fresh built a different area")
    return "second download asked with use_cache=False for the same area"


def check_viewer_unavailable_is_a_note(rig):
    """P13: when no local port is free after a good build, the summary says so;
    no error box.

    1.9.1 raised a Critical box after a successful build when all 20 ports were busy.
    """
    from osm_3d_model import server as server_module

    plugin, dialog = rig.open_dialog()
    rig.set_view(*ALSANCAK, 1400, 800)
    rig.overpass("ok")
    unavailable = getattr(server_module, "ServerUnavailable", OSError)
    message = ("The 3D viewer could not start: every local port it can use (8170-8189) is taken "
               "by another program.")

    def refuse():
        raise unavailable(message)

    plugin.server.start = refuse
    stack, _calls = _stub_run(rig, _outcome(False), dict(FAKE_SUMMARY, warnings=[]))
    with stack:
        dialog.run_button.click()
        wait_for(lambda: not plugin._running, 15)
        pump(0.3)
    text = rig.summary_text(dialog)
    problems = []
    if rig.boxes.modal():
        problems.append(f"an error box appeared: {rig.boxes.modal()[0][1][:80]!r}")
    if not dialog.summary_card.isVisible():
        problems.append("the summary of the good build is not shown")
    if message not in text:
        problems.append("the summary does not say the viewer could not start")
    elif "The model was saved." not in text:
        problems.append("the note does not say the model was saved")
    fail_if(problems)
    return "the busy ports are a note in the summary, which says the model was saved"


def check_sample_city_then_last_run(rig):
    """P8: before the first run the button reads "Open sample city" and serves the
    bundled sample; once a run exists, "Reopen last viewer" serves that run.

    1.9.1 always read "Reopen last viewer", even with nothing to reopen, and
    served whatever the plugin folder held.
    """
    from osm_3d_model import builder

    clear_runs()
    plugin, dialog = rig.open_dialog()
    served = []
    real = plugin.server.set_data_dir

    def record(path):
        served.append(path)
        return real(path)

    plugin.server.set_data_dir = record
    label = dialog.reopen_button.text()
    dialog.reopen_button.click()
    pump(0.3)
    sample_served = list(served)
    run = make_run("reopen")
    plugin2, dialog2 = rig.open_dialog()
    served2 = []
    real2 = plugin2.server.set_data_dir

    def record2(path):
        served2.append(path)
        return real2(path)

    plugin2.server.set_data_dir = record2
    label2 = dialog2.reopen_button.text()
    dialog2.reopen_button.click()
    pump(0.3)
    latest = builder.latest_run_dir()
    problems = []
    if label != "Open sample city":
        problems.append(f"before the first run the button reads {label!r}")
    if sample_served[-1:] != [None]:
        problems.append(f"the sample city was not served (data folder {sample_served})")
    if label2 != "Reopen last viewer":
        problems.append(f"with a run the button reads {label2!r}")
    if not served2 or served2[-1] is None or Path(served2[-1]).resolve() != Path(run).resolve():
        problems.append(f"Reopen last viewer did not serve the last run {run.name} ({served2})")
    if latest is None or Path(latest).resolve() != Path(run).resolve():
        problems.append("builder.latest_run_dir() is not the run just made")
    fail_if(problems)
    return f"{label!r} served the sample; {label2!r} served {run.name}"


def check_open_data_folder(rig):
    """P8: Open data folder opens the newest run, or the runs folder before any run.

    1.9.1 opened web/data/layers inside the plugin's own install folder.
    """
    from osm_3d_model import builder
    from osm_3d_model.tests import _offline

    clear_runs()
    plugin, dialog = rig.open_dialog()
    dialog.open_folder_button.click()
    pump(0.2)
    first = _offline.opened()[-1:] or [""]
    make_run("older")
    time.sleep(1.05)
    newest = make_run("newest")
    dialog.open_folder_button.click()
    pump(0.2)
    second = _offline.opened()[-1:] or [""]
    problems = []
    if first[0] != file_url(builder.runs_root()):
        problems.append(f"with no run it opened {first[0]!r}, not the runs folder")
    if second[0] != file_url(newest):
        problems.append(f"with runs it opened {second[0]!r}, not the newest run")
    fail_if(problems)
    return f"opened {Path(urlsplit(second[0]).path).name}"


def check_delete_old_runs(rig):
    """P8: Delete old runs (Advanced) asks first, then keeps only the newest run.

    1.9.1 offered no such choice: each run overwrote the last one in place.
    """
    from osm_3d_model import builder

    clear_runs()
    runs = []
    for label in ("one", "two", "three"):
        runs.append(make_run(label))
        time.sleep(1.05)
    plugin, dialog = rig.open_dialog()
    button = getattr(dialog, "delete_runs_button", None)
    check(button is not None, "there is no Delete old runs button")
    root = Path(builder.runs_root())
    from qgis.PyQt.QtWidgets import QMessageBox

    rig.boxes.answer = QMessageBox.StandardButton.No
    button.click()
    pump(0.2)
    after_no = sorted(p.name for p in root.iterdir() if p.is_dir())
    rig.boxes.answer = QMessageBox.StandardButton.Yes
    button.click()
    pump(0.3)
    after_yes = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    questions = [text for kind, text in rig.boxes.shown if kind == "question"]
    problems = []
    if len(questions) != 2:
        problems.append(f"{len(questions)} confirmations asked for two clicks")
    if len(after_no) != 3:
        problems.append(f"answering No still deleted runs: {after_no}")
    if after_yes != [runs[-1].name]:
        problems.append(f"after Yes the runs folder holds {after_yes}, not just {runs[-1].name}")
    if "2" not in dialog.status.text():
        problems.append(f"the status does not say how many were deleted: {dialog.status.text()!r}")
    fail_if(problems)
    return f"asked twice; kept {after_yes}; {dialog.status.text()!r}"


def check_real_build_end_to_end(rig):
    """Items 1-5 together, on the real download (C1), runs and build (C2) and server:
    Run builds a run folder, serves it, and the summary names the server, then the
    cache age on a second run, the trees and the heights.

    1.9.1 has none of the contracts: no run folder, no host, no cache age.
    """
    from osm_3d_model import builder

    missing = [name for name in STANDINS]
    check(not missing, f"the real contracts are not all there yet: {missing}")
    clear_runs()
    plugin, dialog = rig.open_dialog()
    rig.set_view(*ALSANCAK, 1400, 800)
    overpass = rig.overpass("ok")
    pump(0.6)
    statuses = []
    real_set_status = dialog.set_status

    def recording_set_status(text, *args, **kwargs):
        statuses.append(str(text))
        return real_set_status(text, *args, **kwargs)

    dialog.set_status = recording_set_status   # every sentence, however briefly shown
    dialog.run_button.click()
    finished = wait_for(lambda: not plugin._running, 90)
    pump(0.3)
    check(finished, f"the build did not finish in 90 s: {dialog.status.text()!r}")
    first_text = rig.summary_text(dialog)
    latest = builder.latest_run_dir()
    data_dir = getattr(plugin.server, "data_dir", None)
    manifest_ok = False
    if plugin.server.url:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with contextlib.suppress(OSError, ValueError):
            with opener.open(plugin.server.url.replace("/src/", "/data/planx_manifest.json"), timeout=5) as resp:
                manifest_ok = json.loads(resp.read().decode("utf-8")).get("schema", "").startswith("planx")
    requests_first = len(overpass.requests)
    # QGIS zooms an empty project to its first layers; frame the same area again.
    rig.set_view(*ALSANCAK, 1400, 800)
    pump(0.6)
    dialog.run_button.click()
    wait_for(lambda: not plugin._running, 60)
    pump(0.3)
    second_text = rig.summary_text(dialog)
    fresh = getattr(dialog, "fresh_button", None)
    problems = []
    if rig.boxes.modal():
        problems.append(f"a box appeared: {rig.boxes.modal()[0][1][:80]!r}")
    if not any("127.0.0.1" in s for s in statuses):
        problems.append(f"no status named the server: {statuses[:4]}")
    if "Downloaded just now from 127.0.0.1" not in first_text:
        problems.append(f"the summary does not name the server: {first_text!r}")
    if latest is None or not (Path(latest) / "data" / "planx_manifest.json").is_file():
        problems.append("no finished run folder")
    elif data_dir is None or Path(data_dir).resolve() != Path(latest).resolve():
        problems.append(f"the viewer serves {data_dir}, not the run {latest}")
    if not manifest_ok:
        problems.append("the viewer server did not answer with the run's manifest")
    if " from OSM + " not in first_text or "building heights" not in first_text:
        problems.append("trees or heights are missing from the summary")
    if len(overpass.requests) != requests_first:
        problems.append("the second run asked the server again instead of using the saved copy")
    if "Built from a copy downloaded" not in second_text or fresh is None or not fresh.isVisible():
        problems.append(f"the cache-served run does not say so: {second_text!r}")
    fail_if(problems)
    return f"run {Path(latest).name} served; second run from the saved copy"


def _measure(dialog, widget, role, inside_scroll=True):
    """(ratio, background, ink) for a widget's text on the dialog's own pixels.

    The background is what the dialog really paints behind the widget; the ink
    is the colour the polished palette gives the widget's text (QGIS's offscreen
    platform draws no glyphs, so text pixels cannot be sampled on every runtime).
    """
    from qgis.PyQt.QtCore import QPoint, QRect
    from qgis.PyQt.QtGui import QColor, QImage, QPalette

    if inside_scroll:
        dialog.scroll.ensureWidgetVisible(widget)
    pump(0.15)
    pixmap = dialog.grab()
    scale = pixmap.devicePixelRatio()
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB32)
    corner = widget.mapTo(dialog, QPoint(0, 0))
    rect = QRect(int((corner.x() + 4) * scale), int((corner.y() + 4) * scale),
                 int(max(1, widget.width() - 8) * scale), int(max(1, widget.height() - 8) * scale))
    rect = rect.intersected(QRect(0, 0, image.width(), image.height()))
    ink = widget.palette().color(QPalette.ColorGroup.Active, role)
    ink_rgb = (ink.red(), ink.green(), ink.blue())
    counts = Counter()
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            value = image.pixel(x, y)
            counts[((value >> 16) & 255, (value >> 8) & 255, value & 255)] += 1
    total = sum(counts.values())
    if not total:
        return 0.0, "#000000", ink.name()

    def near_ink(colour):
        return sum((a - b) ** 2 for a, b in zip(colour, ink_rgb)) <= 40 ** 2

    background = {c: n for c, n in counts.items() if not near_ink(c)}
    if sum(background.values()) < 0.4 * total:
        background = dict(counts)   # the "background" is the ink itself: no contrast at all
    covered, worst, worst_colour = 0, None, None
    pool = sum(background.values())
    for colour, number in sorted(background.items(), key=lambda item: -item[1]):
        ratio = _ratio(colour, ink_rgb)
        if worst is None or ratio < worst:
            worst, worst_colour = ratio, colour
        covered += number
        if covered >= 0.5 * pool:
            break
    return worst, "#%02x%02x%02x" % worst_colour, QColor(*ink_rgb).name()


def _luminance(rgb):
    channels = []
    for value in rgb:
        value /= 255.0
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _ratio(first, second):
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _dark_system_palette(base):
    """The palette QGIS 4 inherits from a Windows desktop in dark mode."""
    from qgis.PyQt.QtGui import QColor, QPalette

    roles = QPalette.ColorRole
    palette = QPalette(base)
    for role, colour in ((roles.Window, "#2d2d2d"), (roles.WindowText, "#ffffff"), (roles.Base, "#1e1e1e"),
                         (roles.AlternateBase, "#262626"), (roles.Text, "#ffffff"), (roles.Button, "#3a3a3a"),
                         (roles.ButtonText, "#ffffff"), (roles.PlaceholderText, "#a0a0a0")):
        palette.setColor(role, QColor(colour))
    return palette


def check_contrast_under_three_themes(rig):
    """P12: Run, the spin-box value and the Advanced toggle (and the place search
    box) read at 4.5:1 or more under the default theme, Night Mapping and Blend
    of Gray, and under the dark palette QGIS 4 takes from a dark desktop.

    1.9.1: Run was white on #b8978a (2.68:1) everywhere; under Night Mapping the
    spin-box value was about 1.5:1 and the Advanced toggle 1.53:1.
    """
    from qgis.PyQt.QtGui import QPalette
    from qgis.PyQt.QtWidgets import QApplication

    from osm_3d_model.dialog import Osm3dModelDialog

    roles = QPalette.ColorRole
    saved = QApplication.palette()
    problems = []
    report = []
    try:
        for theme, dark in (("default", False), ("Night Mapping", False), ("Blend of Gray", False),
                            ("default", True)):
            label = "dark desktop" if dark else theme
            check(theme == "default" or theme in QgsApplication.uiThemes(), f"QGIS ships no {theme} theme")
            QApplication.setPalette(saved)
            QgsApplication.setUITheme(theme)
            if dark:
                QApplication.setPalette(_dark_system_palette(saved))
            pump(0.2)
            dialog = Osm3dModelDialog(rig.window)
            dialog.resize(460, 1000)
            dialog.show()
            dialog.adv_toggle.setChecked(True)
            pump(0.3)
            try:
                targets = [("Run", dialog.run_button, roles.ButtonText,
                            dialog.scroll.isAncestorOf(dialog.run_button)),
                           ("spin box", dialog.ha_spin.lineEdit(), roles.Text, True),
                           ("Advanced", dialog.adv_toggle, roles.ButtonText, True)]
                if hasattr(dialog, "place_edit"):
                    dialog.place_edit.setText("Alsancak")
                    targets.append(("place box", dialog.place_edit, roles.Text, True))
                for name, widget, role, in_scroll in targets:
                    ratio, background, ink = _measure(dialog, widget, role, in_scroll)
                    report.append(f"{label} {name} {ink} on {background} {ratio:.2f}:1")
                    if ratio < 4.5:
                        problems.append(f"{label}: {name} {ink} on {background} is {ratio:.2f}:1")
            finally:
                dialog.close()
                dialog.deleteLater()
                pump(0.1)
    finally:
        QApplication.setPalette(saved)
        QgsApplication.setUITheme("default")
        pump(0.2)
    fail_if(problems)
    return ", ".join(report)


CHECKS = (
    check_plain_words,
    check_readout_is_the_model_boundary,
    check_world_view_refused,
    check_outline_follows_view_and_unload_removes_it,
    check_run_visible_at_1366x728,
    check_status_moves_while_waiting,
    check_cancel_within_two_seconds,
    check_close_hides_and_run_continues,
    check_toolbar_shows_dialog_during_run,
    check_failure_in_dialog_not_a_box,
    check_place_search,
    check_summary_tells_the_whole_result,
    check_download_fresh,
    check_viewer_unavailable_is_a_note,
    check_sample_city_then_last_run,
    check_open_data_folder,
    check_delete_old_runs,
    check_real_build_end_to_end,
    check_contrast_under_three_themes,
)


def run_checks() -> bool:
    from osm_3d_model.tests import _offline

    print("=" * 70)
    print(" osm_3d_model - dialog, place search and results (fake servers, offscreen)")
    print("=" * 70)
    started = time.monotonic()
    install_contract_standins()
    if STANDINS:
        print(f"  [INFO] contract stand-ins in use: {', '.join(STANDINS)}")
    rig = Rig()
    passed = 0
    for function in CHECKS:
        name = function.__name__[len("check_"):].replace("_", " ")
        try:
            detail = function(rig)
            passed += 1
            print(f"  [PASS] {name} - {detail}")
        except CheckFailed as exc:
            print(f"  [FAIL] {name} - {exc}")
        except Exception as exc:  # report the check as failed and carry on
            print(f"  [FAIL] {name} - {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=4)
        finally:
            with contextlib.suppress(Exception):
                rig.finish()
    refused = _offline.refused()
    if refused:
        print(f"  [FAIL] public requests were attempted: {refused[:3]}")
    print("-" * 70)
    total = len(CHECKS)
    ok = passed == total and not refused
    print(f"  {passed}/{total} checks passed in {time.monotonic() - started:.0f} s - {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    faulthandler.dump_traceback_later(900, exit=True)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="backslashreplace")
    app = QgsApplication([], True)
    app.initQgis()
    # Before anything else: no public host, no browser, no file manager.
    from osm_3d_model.tests._offline import isolate_network

    isolate_network()
    from qgis.PyQt.QtGui import QFont

    app.setFont(QFont("Segoe UI", 9))   # QGIS's own font on Windows; a fallback elsewhere
    try:
        ok = run_checks()
    except Exception:  # the suite itself broke: say so and fail
        traceback.print_exc()
        ok = False
    sys.stdout.flush()
    sys.stderr.flush()
    # Exit from the frame that holds the QgsApplication (TRAPS 4.6).
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
