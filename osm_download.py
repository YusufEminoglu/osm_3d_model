# -*- coding: utf-8 -*-
"""OpenStreetMap download + clip for the 3D OSM Model plugin.

Fetches buildings, roads, greens and trees from the public Overpass servers
for the bounding box of a study boundary, reprojects to a metric UTM CRS, and
clips every feature to that boundary.

The download follows the 2026 Overpass rules (``net/``, ported from
02AgentOSM 1.6.0): the HTTP status is read even when QGIS reports an error; a
server that failed rests for a while that depends on its answer (429: a
minute, 403/406: an hour, 5xx: 45 s, no answer: 2 minutes), doubling while it
keeps failing, and the rests and the last server that worked are remembered
across QGIS sessions; each click asks every server at most once; the request
claims ``[timeout:N][maxsize:134217728]`` matched to QGIS's own network
timeout; an answer is cut off at once when Cancel is pressed or when it
passes 128 MiB; empty or incomplete answers are never cached; and a failed
download ends with one message that names every server and what it said.

Building heights follow contract C3 (docs/DEVELOPMENT_SET_v1.10.md):
``height`` is the wall top, ``roof_height`` the roof rise, ``min_height`` the
base of the walls and ``height_source`` says where the height came from.
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from qgis.PyQt.QtCore import QT_VERSION, QByteArray, Qt, QUrl, QVariant
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
    QgsBlockingNetworkRequest,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeedback,
    QgsField,
    QgsGeometry,
    QgsNetworkAccessManager,
    QgsNetworkRequestParameters,
    QgsPointXY,
    QgsProject,
    QgsSettings,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from . import PLUGIN_VERSION
from .net import hosts as _hosts
from .net import transport as _transport

_NO_NETWORK_ERROR = getattr(
    getattr(QgsBlockingNetworkRequest, "ErrorCode", QgsBlockingNetworkRequest),
    "NoError",
)
_NO_WRITER_ERROR = getattr(
    getattr(QgsVectorFileWriter, "WriterError", QgsVectorFileWriter),
    "NoError",
)
_POLYGON_GEOMETRY = getattr(
    getattr(QgsWkbTypes, "GeometryType", QgsWkbTypes),
    "PolygonGeometry",
)
_LINE_GEOMETRY = getattr(
    getattr(QgsWkbTypes, "GeometryType", QgsWkbTypes),
    "LineGeometry",
)

# The public Overpass servers, one per operator, asked in this order when no
# server has worked yet; after that the one that last worked goes first (net/
# hosts.py remembers it across QGIS sessions).  overpass-api.de leads: a
# click sends one light query, and overpass.private.coffee did not answer at
# all on 2026-11-02 and 2026-11-04, so leading with it made every first click
# wait 55 s before reaching overpass-api.de anyway.  When overpass-api.de is
# busy, private.coffee answers and is remembered, so the load still spreads.
# overpass.kumi.systems is not listed: it is an alias of private.coffee (same
# machine).  Read at call time: the test guard replaces this tuple.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
#: Sent as the Referer on every request.  QGIS replaces a plugin's own
#: User-Agent with its stock one but keeps this header, so the operators can
#: tell this plugin's traffic apart and know whom to contact.
REFERER = "https://github.com/YusufEminoglu/osm_3d_model"
#: Appended to QGIS's User-Agent where QGIS allows it (QGIS 4.2 and later).
USER_AGENT = f"3D-OSM-Model-QGIS-Plugin/{PLUGIN_VERSION} (+{REFERER})"
#: Kept for callers of the 1.9 `fetch_overpass(timeout_s=...)`: an upper bound
#: on the transfer, which otherwise follows QGIS's own network timeout.
DEFAULT_TIMEOUT_S = 60
MAX_RESPONSE_BYTES = _transport.MAX_RESPONSE_BYTES
#: Memory a request asks the server to reserve: 128 MiB instead of the 512 MiB
#: Overpass assumes when a query declares nothing.  A nearly full server
#: admits the small claim and sheds the default one.
OVERPASS_MAXSIZE_BYTES = 134_217_728

# Disk cache for Overpass answers, keyed by the exact selection.  Re-running
# the same area (or nudging a style option) sends nothing within the TTL.
CACHE_TTL_S = 7 * 24 * 3600  # one week
#: Folder of the answer cache; None means "<system temp>/osm_3d_model_cache".
#: Read at call time, so a test can point it at a folder of its own.
CACHE_DIR: Optional[str] = None
_CACHE_KEY_VERSION = "osm_3d_model/1.10"

#: QgsSettings group that keeps each server's rest and the last server that
#: worked across QGIS sessions (read at the start of a click, written after).
SETTINGS_GROUP = "osm_3d_model/hosts"
#: The board every download in this QGIS session shares (read at call time).
BOARD = _hosts.HostBoard()


class OsmDownloadError(RuntimeError):
    """A download or build step failed; ``str(error)`` is the final user text."""


class OsmDownloadCancelled(OsmDownloadError):
    """The user cancelled the download."""

    def __init__(self, message: str = "Download cancelled.") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class FetchOutcome:
    """What `fetch_overpass_ex` brought back."""

    payload: dict              # validated Overpass JSON
    from_cache: bool
    saved_at: Optional[float]  # epoch seconds the answer was downloaded
    host: str                  # server that answered; "" when from_cache


# --------------------------------------------------------------------------
# Overpass answer cache (on disk, keyed by the exact selection)
# --------------------------------------------------------------------------
def _cache_dir() -> str:
    path = CACHE_DIR or os.path.join(tempfile.gettempdir(), "osm_3d_model_cache")
    with contextlib.suppress(OSError):
        os.makedirs(path, exist_ok=True)
    return path


def _cache_path(key: str) -> str:
    # SHA-256 purely as a filename-safe digest of the key (not security).
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
    return os.path.join(_cache_dir(), f"{digest}.json")


def _discard_path(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def _read_cache(key: str) -> Optional[Tuple[dict, float]]:
    """(payload, saved_at) for a fresh, usable saved answer, else None.

    An answer saved by 1.9.1 or earlier may be empty: such a copy, an
    incomplete one, an unreadable one and one older than the TTL are deleted
    instead of being used.
    """
    path = _cache_path(key)
    try:
        if not os.path.isfile(path):
            return None
        saved_at = os.path.getmtime(path)
        if time.time() - saved_at > CACHE_TTL_S:
            _discard_path(path)
            return None
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        _discard_path(path)
        return None
    if _transport.cache_refusal(payload):
        _discard_path(path)
        return None
    return payload, saved_at


def _write_cache(key: str, payload: dict) -> bool:
    """Atomically keep a good answer; an empty or incomplete one never."""
    if _transport.cache_refusal(payload):
        return False
    tmp_path = None
    written = False
    try:
        cache_path = _cache_path(key)
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
        written = True
    except (OSError, TypeError, ValueError):
        written = False
    finally:
        if tmp_path:
            _discard_path(tmp_path)
    return written


def clear_cache():
    """Delete all cached Overpass answers. Returns (files_removed, bytes_freed)."""
    removed, freed = 0, 0
    with contextlib.suppress(OSError):
        for path in glob.glob(os.path.join(_cache_dir(), "*.json")):
            with contextlib.suppress(OSError):
                size = os.path.getsize(path)
                os.remove(path)
                removed += 1
                freed += size
    return removed, freed


# --------------------------------------------------------------------------
# Geo helpers
# --------------------------------------------------------------------------
def utm_epsg_for(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180.0) / 6.0) + 1)
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0 else 32700) + zone


# --------------------------------------------------------------------------
# Overpass query
# --------------------------------------------------------------------------
def _overpass_selection(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> str:
    """The statements that pick the data, without the settings header."""
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    return f"""
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


def _overpass_header(timeout_s: int) -> str:
    """`[timeout:N]` is what the server may spend; `[maxsize]` the memory it
    is asked to reserve.  Both are claims the server admits against."""
    return f"[out:json][timeout:{max(1, int(timeout_s))}][maxsize:{OVERPASS_MAXSIZE_BYTES}];"


def _overpass_query(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                    timeout_s: Optional[int] = None) -> str:
    """The whole request; `timeout_s` defaults to what QGIS's timeout allows."""
    if timeout_s is None:
        timeout_s = _transport.server_timeout_seconds(_transfer_seconds())
    return _overpass_header(timeout_s) + "\n" + _overpass_selection(min_lat, min_lon, max_lat, max_lon)


def _cache_key(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> str:
    # The settings header is left out: a changed QGIS timeout must not orphan
    # a saved answer for the same selection.
    return _CACHE_KEY_VERSION + "\n" + _overpass_selection(min_lat, min_lon, max_lat, max_lon)


# --------------------------------------------------------------------------
# One request to one server, through QGIS's own network stack
# --------------------------------------------------------------------------
def _network_enum(group: str, name: str):
    """Qt 5 exposes these enum members on QNetworkRequest, Qt 6 nests them."""
    enum = getattr(QNetworkRequest, group, None)
    return getattr(enum, name) if enum is not None else getattr(QNetworkRequest, name)


def _reply_status(reply) -> int:
    try:
        return int(reply.attribute(_network_enum("Attribute", "HttpStatusCodeAttribute")))
    except (TypeError, ValueError):
        return 0


def _enum_int(value) -> int:
    """An enum's number on Qt 5 (int enums) and Qt 6 (Python enums)."""
    with contextlib.suppress(TypeError, ValueError):
        return int(getattr(value, "value", value))
    return 0


_UA_ATTRIBUTE_PROBED = False
_UA_ATTRIBUTE = None


def _user_agent_attribute():
    """The request attribute QGIS appends to its User-Agent (QGIS 4.2+).

    Older QGIS has no such attribute and overwrites any User-Agent a plugin
    sets, so there the Referer is the only identification that arrives.
    """
    global _UA_ATTRIBUTE_PROBED, _UA_ATTRIBUTE
    if not _UA_ATTRIBUTE_PROBED:
        attribute = None
        with contextlib.suppress(Exception):
            raw = getattr(QgsNetworkRequestParameters, "AttributeUserAgentSuffix", None)
            if raw is None:
                enum = getattr(QgsNetworkRequestParameters, "RequestAttributes", None)
                raw = getattr(enum, "AttributeUserAgentSuffix", None)
            if raw is not None:
                attribute = QNetworkRequest.Attribute(int(getattr(raw, "value", raw)))
        _UA_ATTRIBUTE = attribute
        _UA_ATTRIBUTE_PROBED = True
    return _UA_ATTRIBUTE


def _identify(request: QNetworkRequest) -> None:
    """Name the plugin on a request: Referer always, User-Agent suffix where QGIS allows."""
    request.setRawHeader(b"Referer", REFERER.encode("ascii"))
    attribute = _user_agent_attribute()
    if attribute is not None:
        with contextlib.suppress(Exception):
            request.setAttribute(attribute, USER_AGENT)


def _transfer_seconds() -> float:
    """T = min(55 s, QGIS network timeout - 2 s), read from the user's QGIS."""
    timeout_ms = _transport.DEFAULT_QGIS_TIMEOUT_MS
    with contextlib.suppress(Exception):
        timeout_ms = QgsNetworkAccessManager.timeout()
    return _transport.transfer_seconds(timeout_ms)


def _form_body(query: str) -> bytes:
    # Standard form encoding: a literal "+" would reach the server as a space.
    return urlencode({"data": query}).encode("ascii")


def _host_name(url: str) -> str:
    return urlsplit(url).netloc or url


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _address_allowed(url: QUrl) -> bool:
    """HTTPS anywhere; plain HTTP only to this computer (local test servers)."""
    if not url.isValid() or not url.host():
        return False
    scheme = str(url.scheme()).lower()
    return scheme == "https" or (scheme == "http" and str(url.host()).lower() in _LOOPBACK_HOSTS)


def _cancelled(feedback) -> bool:
    return feedback is not None and bool(feedback.isCanceled())


def _raise_if_cancelled(feedback) -> None:
    if _cancelled(feedback):
        raise OsmDownloadCancelled()


def _post_once(url: str, body: bytes, transfer_s: float, feedback=None) -> _transport.Verdict:
    """Send one request to one server and classify what came back.

    The transfer runs under its own QgsFeedback, cancelled either by the
    caller's feedback (Cancel) or as soon as the answer passes 128 MiB, so an
    oversized answer is never downloaded in full.  The HTTP status is read
    even when QGIS reports the request as failed, which it does for every
    4xx and 5xx reply.  Raises OsmDownloadCancelled when the caller cancelled.
    """
    address = QUrl(url)
    if not _address_allowed(address):
        return _transport.Verdict(_transport.HOST_ERROR, "is not a valid secure address")
    request = QNetworkRequest(address)
    if hasattr(request, "setTransferTimeout"):
        request.setTransferTimeout(max(1000, int(round(transfer_s * 1000))))
    request.setHeader(
        _network_enum("KnownHeaders", "ContentTypeHeader"),
        "application/x-www-form-urlencoded",
    )
    request.setRawHeader(b"Accept", b"application/json")
    _identify(request)

    guard = QgsFeedback()
    oversize = []

    def watch(received, _total) -> None:
        if received > MAX_RESPONSE_BYTES and not oversize:
            oversize.append(int(received))
            guard.cancel()

    client = QgsBlockingNetworkRequest()
    # For a POST, QGIS reports the bytes received through uploadProgress;
    # both signals are watched, and a direct connection lets the check run on
    # the thread doing the transfer.
    for name in ("downloadProgress", "uploadProgress"):
        signal = getattr(client, name, None)
        if signal is not None:
            signal.connect(watch, Qt.ConnectionType.DirectConnection)
    canceled = getattr(feedback, "canceled", None) if feedback is not None else None
    linked = False
    if canceled is not None and hasattr(canceled, "connect"):
        canceled.connect(guard.cancel)
        linked = True
    try:
        # QgsBlockingNetworkRequest ignores a feedback that is already
        # cancelled and would still send the POST: a Cancel that came before
        # this point (the link above is now in place) stops it here.
        _raise_if_cancelled(feedback)
        code = client.post(request, QByteArray(body), True, guard)
    except Exception as error:  # noqa: BLE001 - one server's fault, not the download's
        _raise_if_cancelled(feedback)
        return _transport.Verdict(
            _transport.NETWORK, f"could not be reached (the request failed: {type(error).__name__})"
        )
    finally:
        if linked:
            with contextlib.suppress(TypeError, RuntimeError):
                canceled.disconnect(guard.cancel)
    _raise_if_cancelled(feedback)
    reply = client.reply()
    error_text = ""
    if code != _NO_NETWORK_ERROR:
        error_text = str(client.errorMessage() or "").strip()
    return _transport.classify(
        status=_reply_status(reply),
        body=b"" if oversize else bytes(reply.content()),
        error_code=_enum_int(reply.error()),
        error_text=error_text,
        oversize=bool(oversize),
        transfer_s=transfer_s,
    )


# --------------------------------------------------------------------------
# The servers' rests, kept across QGIS sessions
# --------------------------------------------------------------------------
_SETTINGS_ERRORS = (ValueError, TypeError, RuntimeError, OSError)


def _restore_board(board: _hosts.HostBoard) -> None:
    """Merge the rests and successes kept by earlier sessions into `board`."""
    with contextlib.suppress(*_SETTINGS_ERRORS):
        settings = QgsSettings()
        raw = settings.value(f"{SETTINGS_GROUP}/board", "", type=str)
        last = settings.value(f"{SETTINGS_GROUP}/last_worked", "", type=str)
        hosts = json.loads(raw) if raw else {}
        board.restore({"hosts": hosts if isinstance(hosts, dict) else {}, "last_worked": last or ""})


def _save_board(board: _hosts.HostBoard) -> None:
    """Keep the board's rests and the last server that worked in QgsSettings."""
    with contextlib.suppress(*_SETTINGS_ERRORS):
        snapshot = board.snapshot()
        settings = QgsSettings()
        settings.setValue(f"{SETTINGS_GROUP}/board", json.dumps(snapshot.get("hosts") or {}, sort_keys=True))
        settings.setValue(f"{SETTINGS_GROUP}/last_worked", str(snapshot.get("last_worked") or ""))
        settings.sync()


def forget_hosts() -> None:
    """Forget every server's rest and success, in this session and in QgsSettings.

    For test suites; a person never needs it, and the rests protect the
    servers, so the plugin offers no button for it.
    """
    BOARD.reset()
    with contextlib.suppress(*_SETTINGS_ERRORS):
        settings = QgsSettings()
        settings.remove(SETTINGS_GROUP)
        settings.sync()


# --------------------------------------------------------------------------
# A click: the saved copy, else the servers, one request each
# --------------------------------------------------------------------------
def _say(status, text: str) -> None:
    """Report one step; a fault in the status display never breaks the download."""
    if status is not None and text:
        with contextlib.suppress(Exception):
            status(text)


def _cancellable_wait(seconds: float, feedback) -> None:
    """Wait, checking for Cancel at short intervals."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        _raise_if_cancelled(feedback)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _reask_after_drop() -> bool:
    """Qt 5 already re-sends a POST three times after a dropped connection
    (measured on QGIS 3.44, Qt 5.15.13: four requests per send, with or
    without a `Connection: close` header); Qt 6 sends once, so only there
    does the plugin ask the same server once more."""
    return int(QT_VERSION) >= 0x060000


def _checked_bbox(min_lat, min_lon, max_lat, max_lon) -> Tuple[float, float, float, float]:
    try:
        values = (float(min_lat), float(min_lon), float(max_lat), float(max_lon))
    except (TypeError, ValueError) as exc:
        raise OsmDownloadError("Invalid Overpass bounding-box coordinates.") from exc
    if not all(math.isfinite(value) for value in values):
        raise OsmDownloadError("Invalid Overpass bounding-box coordinates.")
    south, west, north, east = values
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise OsmDownloadError("Invalid or reversed Overpass bounding box.")
    return values


def _failure_message(result: _hosts.HostResult, hosts, board: _hosts.HostBoard) -> str:
    """Every server and what it said: during this click, or else the last time."""
    heard = {}
    for host, verdict in result.answers:
        heard[host] = verdict
    answers = []
    for host in hosts:
        verdict = heard.get(host)
        if verdict is not None:
            answers.append(_transport.HostAnswer(host, verdict.kind, verdict.detail))
            continue
        last = board.last_answer(host)
        if last is not None and last[0] != _transport.OK:
            answers.append(_transport.HostAnswer(host, last[0], last[1], asked=False, age_s=last[2]))
        else:
            answers.append(_transport.HostAnswer(host, asked=False))
    wait_s = min((board.remaining(host) for host in hosts), default=0.0)
    return _transport.failure_report(answers, wait_s).message


def _fetch(min_lat, min_lon, max_lat, max_lon, use_cache: bool, status, feedback,
           transfer_cap: Optional[float] = None) -> FetchOutcome:
    south, west, north, east = _checked_bbox(min_lat, min_lon, max_lat, max_lon)
    _raise_if_cancelled(feedback)
    transfer_s = _transfer_seconds()
    if transfer_cap is not None:
        with contextlib.suppress(TypeError, ValueError):
            cap = float(transfer_cap)
            if math.isfinite(cap) and cap > 0:
                transfer_s = max(1.0, min(transfer_s, cap))
    query = _overpass_query(south, west, north, east, _transport.server_timeout_seconds(transfer_s))
    key = _cache_key(south, west, north, east)
    if use_cache:
        hit = _read_cache(key)
        if hit is not None:
            payload, saved_at = hit
            age = _transport.age_text(time.time() - saved_at)
            _say(status, f"Using the copy downloaded {age} ago; no request sent.")
            return FetchOutcome(payload, True, saved_at, "")

    board = BOARD
    _restore_board(board)
    urls = {}
    for endpoint in tuple(OVERPASS_ENDPOINTS):
        urls.setdefault(_host_name(str(endpoint)), str(endpoint))
    hosts = tuple(urls)
    if not hosts:
        raise OsmDownloadError("No OpenStreetMap server is configured, so nothing was downloaded.")
    body = _form_body(query)

    lead = []
    for host in hosts:
        if board.remaining(host) > 0:
            last = board.last_answer(host)
            why = f" after it {last[1]}" if last is not None and last[1] else ""
            lead.append(f"{host} is resting{why}, so it is not asked. ")
    pending = []
    announced = []

    def opening() -> str:
        text = "".join(lead) + "".join(pending)
        lead.clear()
        pending.clear()
        return text

    def attempt(host: str) -> _transport.Verdict:
        _say(status, f"{opening()}Asking {host}…")
        return _post_once(urls[host], body, transfer_s, feedback)

    def pause(seconds: float, host: str, why: str) -> None:
        if why == _hosts.RESTING:
            _say(status, f"{opening()}Every server is resting after an earlier answer; "
                         f"waiting until {host} accepts requests again.")
        elif why == _hosts.RESET:
            _say(status, f"{host} closed the connection; asking it once more in "
                         f"{_transport.duration_text(seconds)}.")
        elif why == _hosts.IN_USE and host not in announced:
            announced.append(host)
            _say(status, f"Waiting for {host}: another download is using it.")
        _cancellable_wait(seconds, feedback)

    def notify(host: str, verdict: _transport.Verdict) -> None:
        rest = board.remaining(host)
        if rest >= 1:
            pending.append(f"{host} {verdict.detail}, so it rests for "
                           f"{_transport.duration_text(rest)}. ")
        else:
            pending.append(f"{host} {verdict.detail}. ")

    try:
        result = _hosts.ask_hosts(hosts, attempt, board, pause, notify,
                                  reset_retry=_reask_after_drop())
    finally:
        _save_board(board)
    if result.kind == _transport.OK and isinstance(result.payload, dict):
        _write_cache(key, result.payload)
        _say(status, f"{result.host} delivered the data.")
        return FetchOutcome(result.payload, False, time.time(), result.host)
    raise OsmDownloadError(_failure_message(result, hosts, board))


def fetch_overpass_ex(min_lat, min_lon, max_lat, max_lon, *, use_cache=True,
                      status=None, feedback=None) -> FetchOutcome:
    """Download the OSM data for a bounding box, politely (contract C1).

    `use_cache` lets a saved copy younger than a week answer without a
    request; with False the servers are asked and a good answer replaces the
    saved copy.  `status(text)` is called on the calling thread with one short
    plain sentence per step.  `feedback` is a QgsFeedback: cancelling it
    aborts the transfer at once and raises OsmDownloadCancelled.  Any other
    failure raises OsmDownloadError whose text is the message for the user.
    """
    return _fetch(min_lat, min_lon, max_lat, max_lon, bool(use_cache), status, feedback)


class _CancelBridge:
    """A QgsFeedback that is cancelled as soon as `cancel_check()` turns true.

    The 1.9 API takes a polled `cancel_check`; a small watcher thread polls
    it every 0.1 s so a hung transfer is aborted at once, not when it ends.
    """

    def __init__(self, cancel_check: Optional[Callable[[], bool]], outer=None) -> None:
        self.feedback = QgsFeedback()
        self._check = cancel_check
        self._outer = outer
        self._stop = threading.Event()
        self._thread = None
        self._linked = False

    def _polled(self) -> bool:
        try:
            return bool(self._check())
        except (RuntimeError, TypeError, ValueError, AttributeError):
            return False

    def _watch(self) -> None:
        while not self._stop.wait(0.1):
            if self._polled():
                self.feedback.cancel()
                return

    def __enter__(self) -> QgsFeedback:
        if self._outer is not None:
            self._outer.canceled.connect(self.feedback.cancel)
            self._linked = True
            if self._outer.isCanceled():
                self.feedback.cancel()
        if self._check is not None:
            if self._polled():
                self.feedback.cancel()
            else:
                self._thread = threading.Thread(target=self._watch, name="osm3d-cancel", daemon=True)
                self._thread.start()
        return self.feedback

    def __exit__(self, *_exc) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
        if self._linked:
            with contextlib.suppress(TypeError, RuntimeError):
                self._outer.canceled.disconnect(self.feedback.cancel)
        return False


def fetch_overpass(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                   timeout_s: int = DEFAULT_TIMEOUT_S, feedback=None,
                   use_cache: bool = True, cancel_check=None) -> dict:
    """The 1.9 API: the payload of `fetch_overpass_ex`.

    `feedback` is a callable that receives the status sentences;
    `cancel_check` a callable that returns True once the user cancelled;
    `timeout_s` caps the transfer.
    """
    status = feedback if callable(feedback) else None
    outer = feedback if isinstance(feedback, QgsFeedback) else None
    if cancel_check is None and outer is None:
        return _fetch(min_lat, min_lon, max_lat, max_lon, bool(use_cache), status, None,
                      transfer_cap=timeout_s).payload
    with _CancelBridge(cancel_check, outer) as bridged:
        return _fetch(min_lat, min_lon, max_lat, max_lon, bool(use_cache), status, bridged,
                      transfer_cap=timeout_s).payload


def _validate_overpass_payload(payload) -> dict:
    """Reject malformed or explicitly incomplete Overpass answers."""
    if not isinstance(payload, dict):
        raise OsmDownloadError("The OpenStreetMap answer is not valid map data.")
    if str(payload.get("remark") or "").strip():
        raise OsmDownloadError("The OpenStreetMap server marked its answer as incomplete, "
                               "so nothing was built. Try again later, or choose a smaller area.")
    elements = payload.get("elements")
    if not isinstance(elements, list) or any(not isinstance(element, dict) for element in elements):
        raise OsmDownloadError("The OpenStreetMap answer is not valid map data.")
    return payload


EMPTY_AREA_MESSAGE = (
    "OpenStreetMap has no buildings, streets, green areas or trees in this area: "
    "the server sent an empty answer. Choose another area, or try again later if "
    "you expected map data here."
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

#: Metres per level when heights come from level counts.
FLOOR_HEIGHT_M = 3.0
#: Level counts are clamped to 1-200, so one absurd tag never sinks the export.
MAX_LEVELS = 200
_MAX_ROOF_LEVELS = 20
#: A wall top derived by taking the roof rise off OSM `height` never drops below this.
_MIN_WALL_TOP_M = 2.0
#: The roof rise assumed for an OSM `height` with a pitched roof but no roof
#: tags: min(3 m, 0.3 x height) (C3 amendment, 2026-11-02).
_DEFAULT_ROOF_RISE_M = 3.0
_DEFAULT_ROOF_SHARE = 0.3
_HEIGHT_RANGE_M = (1.0, 1000.0)
_BASE_RANGE_M = (0.0, 1000.0)
_ROOF_RANGE_M = (0.0, 100.0)


def _parse_osm_number(value):
    """Parse an OSM numeric tag ('12', '12.5', '12 m', '3;4') -> float or None.

    A value that does not parse to a finite number (``"9" * 400`` is infinite
    as a float) is treated as absent.
    """
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
        result = feet * 0.3048 + inches * 0.0254
        return result if math.isfinite(result) else None
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
        number /= 100.0
    elif unit == "km":
        number *= 1000.0
    elif unit in {"ft", "feet", "foot"}:
        number *= 0.3048
    elif unit.startswith("in"):
        number *= 0.0254
    return number if math.isfinite(number) else None


def _round_half_up(value: float) -> int:
    """2.5 -> 3 (Python's round() would give 2)."""
    return int(math.floor(float(value) + 0.5))


def _round1(value: float) -> float:
    """Metres to one decimal, half up."""
    return math.floor(float(value) * 10.0 + 0.5) / 10.0


def _level_count(tags: dict, keys, low: int, high: int):
    for key in keys:
        number = _parse_osm_number(tags.get(key))
        if number is not None:
            return max(low, min(high, _round_half_up(number)))
    return None


def _metres(tags: dict, key: str, bounds, allow_zero: bool):
    number = _parse_osm_number(tags.get(key))
    if number is None or number < 0 or (number == 0 and not allow_zero):
        return None
    return max(bounds[0], min(bounds[1], number))


def building_heights(tags: dict) -> dict:
    """The building attributes of contract C3, from OSM tags.

    * ``height``: wall top above ground in metres -- OSM ``height`` (which
      includes the roof) minus the roof rise unless the roof is flat, never
      below 2 m; else levels x 3 m; else the default for the building type.
    * ``roof_height``: roof rise -- ``roof:height``, else ``roof:levels`` x
      3 m; else, for an OSM ``height`` with a non-flat ``roof:shape``,
      min(3 m, 0.3 x height) (the master's C3 amendment: with 0 the viewer
      would put its default roof on top of the full height); else 0 (the
      viewer's default).  Flat or unknown roof shapes without roof tags keep
      0 and the full height as the wall top.
    * ``min_height``: base of the walls -- ``min_height``, else
      ``building:min_level`` x 3 m, else 0; kept below the wall top.
    * ``height_source``: "height", "levels" or "default".
    * ``building_levels``: the wall's level count (roof levels excluded).

    Levels are rounded half up and clamped to 1-200; metres are clamped to
    sane ranges; values that are not finite numbers count as absent.
    """
    levels = _level_count(tags, ("building:levels", "levels"), 1, MAX_LEVELS)
    roof_shape = _tag(tags, "roof:shape")
    tagged = _metres(tags, "height", _HEIGHT_RANGE_M, allow_zero=False)
    roof = _metres(tags, "roof:height", _ROOF_RANGE_M, allow_zero=True)
    if roof is None:
        roof_levels = _level_count(tags, ("roof:levels",), 0, _MAX_ROOF_LEVELS)
        if roof_levels is not None:
            roof = roof_levels * FLOOR_HEIGHT_M
    if roof is None:
        if tagged is not None and roof_shape not in ("", "flat"):
            roof = min(_DEFAULT_ROOF_RISE_M, _DEFAULT_ROOF_SHARE * tagged)
        else:
            roof = 0.0
    if tagged is not None:
        wall = tagged
        if roof > 0 and roof_shape != "flat":
            wall = max(_MIN_WALL_TOP_M, tagged - roof)
        source = "height"
        floors = levels if levels is not None else max(
            1, min(MAX_LEVELS, _round_half_up(wall / FLOOR_HEIGHT_M)))
    elif levels is not None:
        wall, source, floors = levels * FLOOR_HEIGHT_M, "levels", levels
    else:
        floors = _DEFAULT_FLOORS_BY_OSM.get(_tag(tags, "building"), 3)
        wall, source = floors * FLOOR_HEIGHT_M, "default"
    base = _metres(tags, "min_height", _BASE_RANGE_M, allow_zero=True)
    if base is None:
        min_level = _level_count(tags, ("building:min_level",), 0, MAX_LEVELS - 1)
        base = min_level * FLOOR_HEIGHT_M if min_level else 0.0
    if base >= wall:
        # A base at or above the wall top would extrude nothing: keep one
        # storey of wall above the tagged base.
        wall = base + FLOOR_HEIGHT_M
    return {
        "height": _round1(wall),
        "roof_height": _round1(roof),
        "min_height": _round1(base),
        "height_source": source,
        "building_levels": int(floors),
    }


def _building_levels(tags: dict) -> int:
    """Floor count for the export's OSM-native ``building_levels`` column."""
    return building_heights(tags)["building_levels"]


# Highways that are not there to drive on: never built, or gone.
_NOT_BUILT_HIGHWAYS = frozenset({"proposed", "abandoned", "razed", "demolished"})
# Ways underground: drawn on the surface they would be wrong.
_UNDERGROUND_TUNNELS = frozenset({"yes", "culvert"})


def _is_underground(tags: dict) -> bool:
    return _tag(tags, "tunnel") in _UNDERGROUND_TUNNELS


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
            return max(0.5, min(1000.0, n))
    return _WATERWAY_WIDTH.get(_waterway_class(tags), 3.0)


def _road_width(tags: dict):
    """A tagged carriageway width in metres (0-100], else None."""
    width = _parse_osm_number(tags.get("width"))
    if width is None or width <= 0:
        return None
    return _round1(min(100.0, width))


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


def _tree_height(tags: dict) -> float:
    """A tagged tree height in metres (0.5-120), else 6 m."""
    height = _parse_osm_number(tags.get("height"))
    if height is None or height <= 0:
        return 6.0
    return _round1(max(0.5, min(120.0, height)))


# Tree scatter: forests, woods and parks are flat green polygons in the viewer, and
# the OSM *tree nodes* are sparse, so wooded areas look bald. We scatter procedural
# tree points inside tree-bearing green polygons so parks and forests read as
# planted. Trees render as instanced meshes in the viewer, so a few hundred extra
# is cheap; the scatter is globally capped and deterministic per polygon (seeded by
# its footprint) so cached re-runs produce an identical city.  Every scattered
# tree carries source="planted"; trees mapped in OSM carry source="osm".
# kind -> (avg m² of polygon per scattered tree, min height m, max height m)
_TREE_SCATTER = {
    "forest": (75.0, 7.0, 13.0), "wood": (75.0, 7.0, 13.0),
    "orchard": (110.0, 4.0, 7.0),  # regular rows of fruit trees
    "park": (170.0, 5.0, 9.0), "garden": (210.0, 4.0, 7.0), "nature_reserve": (180.0, 5.0, 10.0),
    "village_green": (220.0, 4.0, 8.0), "recreation_ground": (230.0, 4.0, 8.0),
    "cemetery": (260.0, 5.0, 9.0), "scrub": (280.0, 2.5, 4.5), "allotments": (320.0, 3.0, 5.0),
    "grass": (400.0, 3.0, 6.0), "meadow": (440.0, 3.0, 6.0),
}
TREE_SOURCE_OSM = "osm"
TREE_SOURCE_PLANTED = "planted"


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
    number of tree features added to ``feature_sink``; each carries source="planted".
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
        feat.setAttributes([
            f"scatter_{int(px)}_{int(py)}", "tree", round(rng.uniform(h_min, h_max), 1),
            TREE_SOURCE_PLANTED,
        ])
        feats.append(feat)
    if feats:
        feature_sink.extend(feats)
    return len(feats)


def _tag(tags: dict, key: str) -> str:
    """Lower-cased OSM tag value, or '' when absent — emitted verbatim as a column."""
    return (tags.get(key) or "").strip().lower()


def _way_polygon(element) -> QgsGeometry | None:
    """A closed way's polygon; None for an open way.

    An open way is never closed here: joining its ends would invent an
    outline that OSM does not have.
    """
    points = _member_points(element.get("geometry"))
    if len(points) < 4 or _point_key(points[0]) != _point_key(points[-1]):
        return None
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
    if QgsWkbTypes.geometryType(polygon.wkbType()) != _POLYGON_GEOMETRY:
        converted = QgsGeometry(polygon)
        if not converted.convertGeometryCollectionToSubclass(_POLYGON_GEOMETRY):
            return None
        polygon = converted
    return polygon if not polygon.isEmpty() and polygon.area() > 0 else None


def _nested_area(rings: list) -> QgsGeometry | None:
    """One area from closed rings, holes decided by nesting (even/odd depth).

    A ring inside an even number of other rings is land, inside an odd number
    a hole -- what libosmium, osm2pgsql and OGR do -- so an island in a pond
    inside a park survives (subtracting every inner ring from the union of
    the outer rings erased it).  Ported from 02AgentOSM's ``_nested_area``.
    """
    polygons = [polygon for polygon in (_polygon_from_ring(ring) for ring in rings)
                if polygon is not None]
    if not polygons:
        return None
    polygons.sort(key=lambda item: -item.area())
    boxes = [polygon.boundingBox() for polygon in polygons]
    depths = []
    for index, polygon in enumerate(polygons):
        probe = polygon.pointOnSurface()
        depths.append(sum(
            1 for outer in range(index)
            if boxes[outer].contains(boxes[index]) and polygons[outer].contains(probe)
        ))
    result = None
    for level in sorted({depth - depth % 2 for depth in depths}):
        shells = [item for item, depth in zip(polygons, depths) if depth == level]
        holes = [item for item, depth in zip(polygons, depths) if depth == level + 1]
        part = QgsGeometry.unaryUnion(shells)
        if holes and part is not None and not part.isEmpty():
            part = part.difference(QgsGeometry.unaryUnion(holes))
        if part is None or part.isEmpty():
            continue
        result = part if result is None else QgsGeometry.unaryUnion([result, part])
    return result


def _relation_multipolygon(element) -> QgsGeometry | None:
    """Assemble a (multi)polygon from a relation's member ways.

    Overpass ``out geom`` returns each member way with its own ``geometry``.
    Split or reversed member ways are stitched into rings (outer and inner
    roles separately), and which ring is a hole is decided by nesting, so an
    island inside a hole is kept.  Returns None when no usable ring exists,
    so callers fall back to skipping the feature.
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

    poly = _nested_area(_stitch_member_rings(outer_paths) + _stitch_member_rings(inner_paths))
    if poly is None or poly.isEmpty() or poly.area() <= 0:
        return None
    if not poly.isGeosValid():
        poly = poly.makeValid()
    if poly is None or poly.isEmpty():
        return None
    if QgsWkbTypes.geometryType(poly.wkbType()) != _POLYGON_GEOMETRY:
        if not poly.convertGeometryCollectionToSubclass(_POLYGON_GEOMETRY):
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
    if error != _NO_WRITER_ERROR:
        _discard_path(path)
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
    rectangle or exact polygon) in the EPSG:``epsg_dest`` metric CRS.
    ``feedback`` is a callable that receives status sentences.  Returns a dict
    of memory layers keyed by role plus feature counts, including
    ``buildings``, ``estimated_heights`` (buildings whose height is the
    default for their type), ``osm_trees`` and ``planted_trees``.
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
        raise OsmDownloadError(EMPTY_AREA_MESSAGE)

    # Columns mirror raw OSM tags (no PlanX schema): the viewer maps building_levels
    # -> floors, building -> colour, highway -> hierarchy, etc. via the manifest.
    # Heights follow contract C3 (see building_heights).
    buildings_layer, b_pr = _make_layer(
        "OSM Buildings", "Polygon", epsg_dest,
        [("osm_id", QVariant.String), ("building", QVariant.String),
         ("building_levels", QVariant.Int), ("height", QVariant.Double),
         ("roof_shape", QVariant.String), ("roof_height", QVariant.Double),
         ("min_height", QVariant.Double), ("height_source", QVariant.String),
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
    # source: "osm" for a tree mapped in OSM, "planted" for one this plugin scattered.
    trees_layer, t_pr = _make_layer(
        "OSM Trees", "Point", epsg_dest,
        [("osm_id", QVariant.String), ("natural", QVariant.String), ("height", QVariant.Double),
         ("source", QVariant.String)],
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
        "buildings": 0, "estimated_heights": 0, "roads": 0, "bikelanes": 0, "greens": 0,
        "trees": 0, "osm_trees": 0, "planted_trees": 0, "trees_scattered": 0,
        "parking": 0, "plazas": 0, "waterlines": 0,
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
            feat = QgsFeature()
            feat.setGeometry(g)
            feat.setAttributes([str(element.get("id", "")), "tree", _tree_height(tags), TREE_SOURCE_OSM])
            feature_batches["trees"].append(feat)
            counts["trees"] += 1
            counts["osm_trees"] += 1
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
                counts["skipped"] += 1
                continue
            clipped = clip_to_area(base, _POLYGON_GEOMETRY)
            if clipped is None:
                counts["skipped"] += 1
                continue
            heights = building_heights(tags)
            feat = QgsFeature()
            feat.setGeometry(clipped)
            feat.setAttributes([
                str(element.get("id", "")),
                _tag(tags, "building"),
                heights["building_levels"],
                heights["height"],
                _tag(tags, "roof:shape"),
                heights["roof_height"],
                heights["min_height"],
                heights["height_source"],
                tags.get("name", ""),
            ])
            feature_batches["buildings"].append(feat)
            counts["buildings"] += 1
            if heights["height_source"] == "default":
                counts["estimated_heights"] += 1
            continue

        # Water areas (lakes, ponds, riverbanks, reservoirs, bays). Stored in the
        # greens/blocks layer normalised to natural='water'; the viewer's block
        # styling already maps a 'water' category to a blue water surface, so no
        # extra viewer layer is needed. Must run before the linear-waterway and
        # greens branches so a riverbank polygon is filled, not drawn as a ribbon.
        if etype in ("way", "relation") and _is_water_area(tags):
            base = _element_polygon(element)
            if not base:
                counts["skipped"] += 1
                continue
            clipped = clip_to_area(base, _POLYGON_GEOMETRY)
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
                counts["skipped"] += 1
                continue
            clipped = clip_to_area(base, _POLYGON_GEOMETRY)
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
                counts["skipped"] += 1
                continue
            clipped = clip_to_area(base, _POLYGON_GEOMETRY)
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
            highway = _tag(tags, "highway")
            # A road that was never built or is gone, and a road in a tunnel,
            # is not drawn as a surface road.
            if highway in _NOT_BUILT_HIGHWAYS or _is_underground(tags):
                counts["skipped"] += 1
                continue
            base = _way_polyline(element)
            if not base:
                continue
            clipped = clip_to_area(base, _LINE_GEOMETRY)
            if clipped is None:
                counts["skipped"] += 1
                continue
            width = _road_width(tags)
            osm_id = str(element.get("id", ""))
            name = tags.get("name", "")
            # Dedicated tracks stay out of the motor-road layer. Bike lanes tagged
            # on an ordinary highway are duplicated into the bike layer with a
            # road-relative side so the viewer can offset them to the carriageway.
            if highway == "cycleway":
                feat = QgsFeature()
                feat.setGeometry(clipped)
                feat.setAttributes([osm_id, highway, width, name, None, "center"])
                feature_batches["bikelanes"].append(feat)
                counts["bikelanes"] += 1
            else:
                feat = QgsFeature()
                feat.setGeometry(clipped)
                feat.setAttributes([osm_id, highway, width, name])
                feature_batches["roads"].append(feat)
                counts["roads"] += 1
                for side in _cycleway_sides(tags):
                    bike_feat = QgsFeature()
                    bike_feat.setGeometry(QgsGeometry(clipped))
                    bike_feat.setAttributes([
                        osm_id, highway, round(_cycleway_width(tags, side), 1), name,
                        width, side,
                    ])
                    feature_batches["bikelanes"].append(bike_feat)
                    counts["bikelanes"] += 1
            continue

        if etype == "way" and tags.get("waterway"):
            # A stream in a culvert or tunnel runs underground.
            if _is_underground(tags):
                counts["skipped"] += 1
                continue
            base = _way_polyline(element)
            if not base:
                continue
            clipped = clip_to_area(base, _LINE_GEOMETRY)
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
                counts["skipped"] += 1
                continue
            clipped = clip_to_area(base, _POLYGON_GEOMETRY)
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
            # (instanced) tree layer with source="planted".
            kind = _green_scatter_kind(tags)
            if kind:
                placed = _scatter_trees(
                    clipped, kind, feature_batches["trees"],
                    MAX_SCATTER_TREES - scattered_trees,
                )
                scattered_trees += placed
                counts["trees"] += placed
                counts["planted_trees"] += placed
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
