"""Keep a QGIS test suite off every public host, and off the user's desktop.

Every QGIS suite calls :func:`isolate_network` before it runs anything.  It
points each host the plugin asks at a closed port on this computer -- the
Overpass hosts (``osm_download.OVERPASS_ENDPOINTS``, read at call time), the
OpenStreetMap tiles of "Add OSM basemap" and the place search -- and then
guards QGIS's network access itself: a request for any host other than this
computer is turned into a request for that closed port, recorded, and
reported with a ``[FAIL]`` line (pf verify fails a suite on such a line,
docs/TRAPS.md 4.4).  A host the plugin gains later therefore cannot reach the
internet from a test either.  A suite then points the hosts it exercises at
its own fake servers on 127.0.0.1.

It also stops a suite from opening the user's browser or file manager:
``webbrowser.open`` and ``QDesktopServices.openUrl`` only record what they
were asked to open (:func:`opened`); and it points the Overpass answer cache
at a new empty folder (:func:`isolate_download_cache`).

Ported from 02AgentOSM 1.6.0, whose verify run once sent real requests to a
public service because its suites redirected only some of the hosts.
"""
from __future__ import annotations

import threading
from typing import List
from urllib.parse import urlsplit

#: Nothing listens on the discard port: a request there is refused locally.
CLOSED = "http://127.0.0.1:9"
#: "Add OSM basemap" in a suite: tiles from the closed port, never from
#: tile.openstreetmap.org.
CLOSED_TILES_URI = (
    "type=xyz&url=http://127.0.0.1:9/%7Bz%7D/%7Bx%7D/%7By%7D.png"
    "&zmax=19&zmin=0&crs=EPSG3857"
)
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_GUARDED_SCHEMES = frozenset({"http", "https", "ftp", "ws", "wss"})
_LOCK = threading.Lock()
_REFUSED: List[str] = []
_OPENED: List[str] = []
_GUARD: List[str] = []


def refused() -> List[str]:
    """Every public URL the guard has refused in this process, in order."""
    with _LOCK:
        return list(_REFUSED)


def opened() -> List[str]:
    """Every URL or folder a suite asked the browser or file manager to open."""
    with _LOCK:
        return list(_OPENED)


def _guard(request) -> None:
    """QGIS request preprocessor: no request leaves this computer."""
    from qgis.PyQt.QtCore import QUrl

    url = request.url()
    host = str(url.host()).lower()
    if str(url.scheme()).lower() not in _GUARDED_SCHEMES or host in _LOCAL_HOSTS:
        return
    text = str(url.toString())
    with _LOCK:
        _REFUSED.append(text)
    request.setUrl(QUrl(f"{CLOSED}/refused-by-the-offline-guard"))
    print(f"  [FAIL] offline guard refused a request for {host}: {text[:160]}", flush=True)


def _closed(url: str) -> str:
    """The same path and query, on the closed local port."""
    parts = urlsplit(url)
    return CLOSED + parts.path + (f"?{parts.query}" if parts.query else "")


def _record_open(target, *_args, **_kwargs) -> bool:
    text = target.toString() if hasattr(target, "toString") else str(target)
    with _LOCK:
        _OPENED.append(str(text))
    return True


def block_desktop() -> None:
    """Record, instead of performing, every browser and file-manager open."""
    import webbrowser

    from qgis.PyQt.QtGui import QDesktopServices

    webbrowser.open = _record_open
    webbrowser.open_new = _record_open
    webbrowser.open_new_tab = _record_open
    QDesktopServices.openUrl = staticmethod(_record_open)


def isolate_network(guard_qgis: bool = True) -> None:
    """Point every plugin host at a closed local port and guard the rest.

    `guard_qgis` installs the request guard, which covers every request this
    QGIS makes; a suite passes False when it runs inside a QGIS session it
    did not start, whose own network must keep working.  Calling it again is
    harmless: the guard is installed once.
    """
    from qgis.core import QgsNetworkAccessManager

    from osm_3d_model import main_plugin, osm_download

    osm_download.OVERPASS_ENDPOINTS = (f"{CLOSED}/api/interpreter",)
    main_plugin.Osm3dModelPlugin.OSM_BASEMAP_URI = CLOSED_TILES_URI
    try:
        from osm_3d_model import place_search
    except ImportError:
        place_search = None
    if place_search is not None and hasattr(place_search, "GEOCODER_ENDPOINT"):
        place_search.GEOCODER_ENDPOINT = _closed(place_search.GEOCODER_ENDPOINT)
    # Never read or write the user's real Overpass answer cache.
    isolate_download_cache()
    block_desktop()
    if not guard_qgis:
        return
    with _LOCK:
        if not _GUARD:
            _GUARD.append(QgsNetworkAccessManager.setRequestPreprocessor(_guard))


# ---------------------------------------------------------------------------
# Appended by O3-NET (1.10.0): the Overpass answer cache and the servers' rests
# ---------------------------------------------------------------------------
def isolate_download_cache() -> str:
    """Point the Overpass answer cache at a new empty folder; return its path.

    Without it a suite reads and writes the user's real cache in the system
    temp folder: a fake server's answer could later be served for a real
    area.  The folder is created fresh on every call.
    """
    import tempfile

    from osm_3d_model import osm_download

    folder = tempfile.mkdtemp(prefix="osm_3d_model-cache-")
    osm_download.CACHE_DIR = folder
    return folder


def forget_servers() -> None:
    """Forget every Overpass server's rest and success, in memory and in
    QgsSettings (needs QGIS_CUSTOM_CONFIG_PATH set to a throwaway folder,
    docs/TRAPS.md 4.6), so one check's rests cannot decide the next one."""
    from osm_3d_model import osm_download

    forget = getattr(osm_download, "forget_hosts", None)
    if forget is not None:
        forget()
