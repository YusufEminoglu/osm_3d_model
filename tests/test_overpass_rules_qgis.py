"""QGIS suite: the Overpass client against fake servers on 127.0.0.1 (P1-P6).

Everything goes through the real QgsBlockingNetworkRequest and the real
``osm_download`` code; only the two servers are fake.  QGIS's network
timeout is set to 4 s, so the transfer waits T = 4 - 2 = 2 s and every
request claims ``[timeout:10]`` (the formula, not a patched constant).  Each
download runs on one long-lived worker thread, as on the QgsTask pool
thread the plugin uses.  No public host is contacted: ``isolate_network()``
runs first, and the fake servers bind 127.0.0.1 only.

Every check names the 1.9.1 behaviour it rules out.  Run from the folder
that contains the plugin, with QGIS_CUSTOM_CONFIG_PATH pointing at a
throwaway folder (docs/TRAPS.md 4.6):

    python-qgis-ltr.bat -m osm_3d_model.tests.test_overpass_rules_qgis   # QGIS 3.44
    python-qgis.bat     -m osm_3d_model.tests.test_overpass_rules_qgis   # QGIS 4.2
"""
from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from typing import Dict, List, NamedTuple, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))

from qgis.core import QgsApplication, QgsFeedback, QgsNetworkAccessManager, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QT_VERSION  # noqa: E402

QT6 = int(QT_VERSION) >= 0x060000
QGIS_TIMEOUT_MS = 4000
TRANSFER_S = 2
BBOX = (38.4200, 27.1400, 38.4290, 27.1515)   # min_lat, min_lon, max_lat, max_lon (Izmir)
FRESH = "2026-11-02T06:58:39Z"
MIB = 1024 * 1024
#: The oversize answer: twice the limit, so a cut at 128 MiB is unmistakable even
#: after loopback socket buffers swallowed a few MiB more.
OVERSIZE_TOTAL = 256 * MIB
#: Qt's own words; none of them may reach the person reading a message.
QT_WORDS = ("Error transferring", "server replied", "Connection closed", "Operation canceled",
            "Connection refused", "Socket operation timed out", "Expecting value", "non-JSON")
REMARK = 'runtime error: Query timed out in "query" at line 3 after 41 seconds.'


# ------------------------------------------------------------------ server
def _overpass_page(text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
        "\n<head>\n  <title>OSM3S Response</title>\n</head>\n<body>\n"
        f'<p><strong style="color:#FF0000">Error</strong>: {text} </p>\n</body>\n</html>\n'
    ).encode("utf-8")


ELEMENTS = [
    {"type": "node", "id": 301, "lat": 38.4245, "lon": 27.1457, "tags": {"natural": "tree"}},
    {"type": "way", "id": 101, "tags": {"building": "yes", "building:levels": "4"},
     "geometry": [{"lat": 38.4240, "lon": 27.1450}, {"lat": 38.4240, "lon": 27.1452},
                  {"lat": 38.4242, "lon": 27.1452}, {"lat": 38.4242, "lon": 27.1450},
                  {"lat": 38.4240, "lon": 27.1450}]},
]
STATUS_PAGES = {
    "h429": (429, "Too Many Requests", _overpass_page(
        "runtime error: open64: 0 Success /osm3s_osm_base Dispatcher_Client::"
        "request_read_and_idx::rate_limited. Please check /api/status for the quota of your IP address."),
        "text/html"),
    "h406": (406, "Not Acceptable", b"<html><body>Not Acceptable</body></html>", "text/html"),
    "h403": (403, "Forbidden", b"<html><body>Forbidden</body></html>", "text/html"),
    "h400": (400, "Bad Request", _overpass_page("line 2: parse error: Unknown type &quot;&quot;"),
             "text/html"),
    "h502": (502, "Bad Gateway", b"# 502 Gateway Timeout\n\nAll query workers are busy.\n", "text/plain"),
    "h504": (504, "Gateway Timeout", _overpass_page(
        "runtime error: open64: 0 Success /osm3s_osm_base Dispatcher_Client::"
        "request_read_and_idx::timeout. The server is probably too busy to handle your request."),
        "text/html"),
    "h404": (404, "Not Found", b"<html><head><title>404 Not Found</title></head></html>", "text/html"),
}


def overpass_doc(elements, remark: Optional[str] = None) -> bytes:
    """Output_JSON's byte layout: header, elements, optional remark."""
    text = (
        '{\n  "version": 0.6,\n  "generator": "Overpass API 0.7.62 fake",\n'
        f'  "osm3s": {{\n    "timestamp_osm_base": "{FRESH}",\n'
        '    "copyright": "The data included in this document is from www.openstreetmap.org. '
        'The data is made available under ODbL."\n  },\n'
        '  "elements": [\n\n' + ",\n".join(json.dumps(item) for item in elements) + "\n\n  ]"
    )
    if remark:
        text += ',\n"remark": ' + json.dumps(remark)
    return (text + "\n}\n").encode("utf-8")


class FakeOverpass:
    """Overpass servers on 127.0.0.1, each on its own port.

    `set(name, *modes)` scripts the answers: the n-th request gets the n-th
    mode and the last one repeats.  Modes: ok, empty, remark, hang, drop
    (read the request, close without answering), oversize (256 MiB, paced)
    and the status pages h429, h406, h403, h400, h502, h504, h404.
    """

    def __init__(self, names=("a", "b")) -> None:
        self.lock = threading.Lock()
        self.hits: List[Dict] = []
        self.modes: Dict[str, Dict] = {}
        self.ports: Dict[str, int] = {}
        for name in names:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(64)
            self.ports[name] = listener.getsockname()[1]
            threading.Thread(target=self._serve, args=(name, listener), daemon=True).start()
            self.set(name, "ok")

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.ports[name]}/api/interpreter"

    def host(self, name: str) -> str:
        return f"127.0.0.1:{self.ports[name]}"

    def set(self, name: str, *modes: str) -> None:
        with self.lock:
            self.modes[name] = {"seq": list(modes) or ["ok"], "seen": 0}

    def take_hits(self) -> List[Dict]:
        with self.lock:
            hits, self.hits = self.hits, []
        return hits

    def _serve(self, name, listener) -> None:
        while True:
            connection, _address = listener.accept()
            threading.Thread(target=self._handle, args=(name, connection), daemon=True).start()

    @staticmethod
    def _read(connection):
        data = b""
        connection.settimeout(30)
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, _sep, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            key, _colon, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        need = int(headers.get("content-length", "0") or 0)
        while len(body) < need:
            chunk = connection.recv(65536)
            if not chunk:
                break
            body += chunk
        method, path = (lines[0].split(" ") + ["", ""])[:2]
        return method, path, headers, body

    @staticmethod
    def _send(connection, status, reason, body, ctype="application/json") -> None:
        head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {ctype}",
                f"Content-Length: {len(body)}", "Connection: close"]
        connection.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)

    @staticmethod
    def _hold(connection, limit=120.0) -> None:
        start = time.monotonic()
        connection.settimeout(0.2)
        while time.monotonic() - start < limit:
            try:
                if connection.recv(4096) == b"":
                    return
            except socket.timeout:
                continue
            except OSError:
                return

    def _handle(self, name, connection) -> None:
        with contextlib.closing(connection):
            try:
                request = self._read(connection)
                if request is None:
                    return
                method, path, headers, body = request
                query = urllib.parse.parse_qs(body.decode("latin-1")).get("data", [""])[0]
                with self.lock:
                    state = self.modes[name]
                    mode = state["seq"][min(state["seen"], len(state["seq"]) - 1)]
                    state["seen"] += 1
                    record = {"host": name, "method": method, "path": path, "query": query,
                              "referer": headers.get("referer", ""), "ua": headers.get("user-agent", ""),
                              "accept": headers.get("accept", ""), "t": time.monotonic(), "mode": mode}
                    self.hits.append(record)
                self._answer(connection, mode, record)
            except OSError:
                return

    def _answer(self, connection, mode, record) -> None:
        if mode == "ok":
            self._send(connection, 200, "OK", overpass_doc(ELEMENTS))
        elif mode == "empty":
            self._send(connection, 200, "OK", overpass_doc([]))
        elif mode == "remark":
            self._send(connection, 200, "OK", overpass_doc(ELEMENTS[:1], remark=REMARK))
        elif mode == "hang":
            self._hold(connection)
        elif mode == "drop":
            return
        elif mode == "oversize":
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(OVERSIZE_TOTAL).encode() + b"\r\nConnection: close\r\n\r\n")
            chunk, sent = b" " * MIB, 0
            with contextlib.suppress(OSError):
                while sent < OVERSIZE_TOTAL:
                    connection.sendall(chunk)
                    sent += len(chunk)
                    with self.lock:
                        record["sent"] = sent
                    time.sleep(0.004)
        elif mode in STATUS_PAGES:
            status, reason, body, ctype = STATUS_PAGES[mode]
            self._send(connection, status, reason, body, ctype)
        else:
            self._send(connection, 500, "Fake", b"unknown mode", "text/plain")


# --------------------------------------------------------------- harness
class ShiftClock:
    """Real time plus an offset a test can advance: waits still pass, and a
    test jumps over a rest without sleeping through it."""

    def __init__(self, source) -> None:
        self.source = source
        self.offset = 0.0

    def __call__(self) -> float:
        return self.source() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += float(seconds)


class Worker:
    """One long-lived worker thread, like the task-pool thread a download runs on."""

    def __init__(self) -> None:
        self.jobs: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._loop, name="osm3d-test-worker", daemon=True).start()

    def _loop(self) -> None:
        while True:
            job, box = self.jobs.get()
            try:
                box["result"] = job()
            except BaseException as error:  # noqa: BLE001 - handed back to the check
                box["error"] = error
            box["done"] = time.monotonic()

    def call(self, job, limit: float = 120.0, on_tick=None) -> Dict:
        box: Dict = {}
        started = time.monotonic()
        self.jobs.put((job, box))
        while "done" not in box and time.monotonic() - started < limit:
            QgsApplication.processEvents()
            if on_tick is not None:
                on_tick(time.monotonic() - started)
            time.sleep(0.01)
        box["started"] = started
        return box


class Run(NamedTuple):
    payload: Optional[dict]
    outcome: object
    error: Optional[BaseException]
    status: List[str]
    elapsed: float
    hits: List[Dict]
    finished: bool
    done_at: Optional[float]       # time.monotonic() when the download returned

    @property
    def message(self) -> str:
        return str(self.error) if self.error is not None else ""

    def count(self, name: str) -> int:
        return len([hit for hit in self.hits if hit["host"] == name])


class Checks:
    def __init__(self) -> None:
        self.results: List[bool] = []

    def ok(self, name: str, condition, detail: str = "") -> bool:
        tag = "PASS" if condition else "FAIL"
        print(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""), flush=True)
        self.results.append(bool(condition))
        return bool(condition)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result)


def od():
    from osm_3d_model import osm_download

    return osm_download


def click(worker: Worker, servers: FakeOverpass, *, use_cache: bool = False, feedback=None,
          cancel_check=None, legacy: bool = False, limit: float = 120.0, on_tick=None) -> Run:
    """One download, as a click in the dialog makes it, on the worker thread.

    The 1.10 API when the module has it; the 1.9 `fetch_overpass` otherwise
    (or with `legacy`), so every check also runs against 1.9.1.
    """
    module = od()
    lines: List[str] = []
    servers.take_hits()

    def job():
        if not legacy and hasattr(module, "fetch_overpass_ex"):
            return module.fetch_overpass_ex(*BBOX, use_cache=use_cache, status=lines.append,
                                            feedback=feedback)
        kwargs = {"use_cache": use_cache, "feedback": lines.append}
        if cancel_check is not None:
            kwargs["cancel_check"] = cancel_check
        return module.fetch_overpass(*BBOX, **kwargs)

    box = worker.call(job, limit, on_tick)
    finished = "done" in box
    elapsed = box.get("done", time.monotonic()) - box["started"]
    result = box.get("result")
    outcome = result if hasattr(result, "payload") else None
    payload = result.payload if outcome is not None else result
    time.sleep(0.05)   # a re-sent request still in flight lands in this run's hits
    return Run(payload, outcome, box.get("error"), list(lines), elapsed, servers.take_hits(),
               finished, box.get("done"))


def fresh(servers: FakeOverpass, **modes) -> None:
    """A clean start for one check: no rests, no saved answers, new scripts."""
    from osm_3d_model.tests._offline import forget_servers

    module = od()
    forget_servers()
    module.clear_cache()
    for name, mode in modes.items():
        if isinstance(mode, (tuple, list)):
            servers.set(name, *mode)
        else:
            servers.set(name, mode)
    servers.take_hits()


def plain_message(text: str) -> List[str]:
    """What is wrong with a closing message: jargon or Qt's own words."""
    from osm_3d_model.net.transport import JARGON

    lowered = text.casefold()
    return [word for word in JARGON if word in lowered] + [word for word in QT_WORDS if word in text]


_ELAPSED = re.compile(r"\b\d+(?:\.\d+)?\s?s\b|elapsed", re.IGNORECASE)


def remaining(name_host: str) -> float:
    board = getattr(od(), "BOARD", None)
    return board.remaining(name_host) if board is not None else 0.0


def attempts(run: Run, name: str, gap: float = 0.5) -> int:
    """The plugin's own requests to one server: wire requests closer than
    `gap` belong to one send (Qt 5 re-sends a dropped POST by itself within
    milliseconds, as often as its connection channel still allows)."""
    times = sorted(hit["t"] for hit in run.hits if hit["host"] == name)
    if not times:
        return 0
    return 1 + sum(1 for earlier, later in zip(times, times[1:]) if later - earlier > gap)


def cache_files() -> List[str]:
    module = od()
    folder = module._cache_dir()
    return sorted(name for name in os.listdir(folder) if name.endswith(".json"))


# --------------------------------------------------------------- checks
def check_contract(c: Checks) -> None:
    """C1: the names the dialog relies on."""
    import inspect

    module = od()
    c.ok("C1: OVERPASS_ENDPOINTS is one server per operator, overpass-api.de then "
         "private.coffee (1.9.1 also asked the kumi.systems alias of private.coffee)",
         _DEFAULT_ENDPOINTS == ("https://overpass-api.de/api/interpreter",
                                "https://overpass.private.coffee/api/interpreter"),
         repr(_DEFAULT_ENDPOINTS))
    cancelled = getattr(module, "OsmDownloadCancelled", None)
    c.ok("C1: OsmDownloadCancelled is an OsmDownloadError saying 'Download cancelled.' (missing in 1.9.1)",
         cancelled is not None and issubclass(cancelled, module.OsmDownloadError)
         and str(cancelled()) == "Download cancelled.")
    outcome = getattr(module, "FetchOutcome", None)
    fields = list(getattr(outcome, "__dataclass_fields__", {}) or {})
    c.ok("C1: FetchOutcome carries payload, from_cache, saved_at, host (missing in 1.9.1)",
         fields == ["payload", "from_cache", "saved_at", "host"], str(fields))
    fetch = getattr(module, "fetch_overpass_ex", None)
    keyword_only = []
    if fetch is not None:
        keyword_only = [name for name, parameter in inspect.signature(fetch).parameters.items()
                        if parameter.kind == parameter.KEYWORD_ONLY]
    c.ok("C1: fetch_overpass_ex(min_lat, min_lon, max_lat, max_lon, *, use_cache, status, feedback) "
         "(missing in 1.9.1)", keyword_only == ["use_cache", "status", "feedback"], str(keyword_only))
    legacy = list(inspect.signature(module.fetch_overpass).parameters)
    c.ok("C1: fetch_overpass keeps its 1.9.1 signature (kept)", legacy == [
        "min_lat", "min_lon", "max_lat", "max_lon", "timeout_s", "feedback", "use_cache",
        "cancel_check"], str(legacy))


def check_rate_limit(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="h429", b="h429")
    run = click(worker, servers)
    c.ok("429 on both servers: one request each, no retry on the same server "
         "(1.9.1: two each, 1.2 s apart)", run.count("a") == 1 and run.count("b") == 1,
         f"a={run.count('a')} b={run.count('b')}")
    c.ok("429: each server rests 60 s (1.9.1 kept no rest)",
         55 <= remaining(a) <= 60 and 55 <= remaining(b) <= 60,
         f"a rests {remaining(a):.0f} s, b {remaining(b):.0f} s")
    c.ok("429: the message says the servers asked to slow down and when to try again "
         "(1.9.1: 'Wait a minute and retry, or pick a smaller area')",
         run.message.startswith("The OpenStreetMap servers asked this computer to slow down.")
         and "Wait about a minute before trying again." in run.message, run.message[:160])
    c.ok("429: the message names every server asked and what it said "
         "(1.9.1 kept the last three failures and dropped the first server)",
         f"{a}, {b}: asked this computer to slow down (HTTP 429)" in run.message, run.message[-120:])
    c.ok("429: no jargon and no raw Qt text in the message (1.9.1: 'Error transferring ... server replied')",
         not plain_message(run.message), str(plain_message(run.message)))
    c.ok("429: the status names the server, its answer, its rest and the next server",
         len(run.status) >= 2 and run.status[0] == f"Asking {a}…"
         and run.status[1] == (f"{a} asked this computer to slow down (HTTP 429), so it rests "
                               f"for a minute. Asking {b}…"), " | ".join(run.status))
    again = click(worker, servers)
    c.ok("429: a second click inside the rest sends nothing and fails at once "
         "(1.9.1 asked both servers twice again)", not again.hits and again.elapsed < 1.5,
         f"{len(again.hits)} requests in {again.elapsed:.1f} s")
    c.ok("429: the second click's message says why each server was not asked",
         f"{a}: not asked, because it asked this computer to slow down (HTTP 429)" in again.message
         and "Wait about a minute" in again.message, again.message[-200:])


def check_refused(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="h406", b="h406")
    run = click(worker, servers)
    c.ok("406 on both: one request each, each server rests an hour (1.9.1: two each, no rest)",
         run.count("a") == 1 and run.count("b") == 1 and remaining(a) > 3500 and remaining(b) > 3500,
         f"a={run.count('a')} b={run.count('b')} rest {remaining(a):.0f} s")
    c.ok("406: the message says the servers refused this computer and to try again in about an hour",
         run.message.startswith("The OpenStreetMap servers refused this computer.")
         and "Try again in about an hour." in run.message
         and f"{a}, {b}: refused this computer (HTTP 406)" in run.message, run.message[:220])
    fresh(servers, a="h403", b="ok")
    run = click(worker, servers)
    host = getattr(run.outcome, "host", None)
    c.ok("403 on the first server: the second delivers, the first rests an hour "
         "(1.9.1 asked the first twice)", run.payload is not None and run.count("a") == 1
         and run.count("b") == 1 and remaining(a) > 3500 and host == b,
         f"a={run.count('a')} b={run.count('b')} host={host!r} rest {remaining(a):.0f} s")
    c.ok("403: the status tells the person what happened before asking the next server",
         any(line == f"{a} refused this computer (HTTP 403), so it rests for an hour. Asking {b}…"
             for line in run.status), " | ".join(run.status))


def check_invalid(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="h400", b="ok")
    run = click(worker, servers)
    c.ok("400: the request is wrong on every server, so only one request is sent "
         "(1.9.1: two to each server)", run.count("a") == 1 and run.count("b") == 0,
         f"a={run.count('a')} b={run.count('b')}")
    c.ok("400: the message says the request was rejected, where to report it, and that the "
         "other server was not asked",
         run.message.startswith("The OpenStreetMap server rejected the download request as invalid.")
         and "github.com/YusufEminoglu/osm_3d_model/issues" in run.message
         and f"{a}: rejected the request as invalid (HTTP 400); {b}: not asked" in run.message,
         run.message[:240])


def check_busy(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="h502", b="ok")
    run = click(worker, servers)
    c.ok("502 on the first server: the second delivers; the first rests 45 s "
         "(1.9.1 asked the first twice)", run.payload is not None and run.count("a") == 1
         and run.count("b") == 1 and 40 <= remaining(a) <= 45,
         f"a={run.count('a')} b={run.count('b')} rest {remaining(a):.0f} s")
    saved = cache_files()
    again = click(worker, servers, use_cache=True)
    outcome = again.outcome
    age_ok = outcome is not None and outcome.from_cache and outcome.host == "" \
        and outcome.saved_at is not None and abs(time.time() - outcome.saved_at) < 30
    c.ok("cache: a good answer is kept and the next click sends nothing", len(saved) == 1
         and not again.hits and again.payload is not None, f"files={len(saved)} requests={len(again.hits)}")
    c.ok("cache: the hit reports its age through status and FetchOutcome.saved_at "
         "(1.9.1 said 'Using cached OSM' and had no outcome)",
         age_ok and again.status == ["Using the copy downloaded less than a minute ago; no request sent."],
         f"{again.status} saved_at={getattr(outcome, 'saved_at', None)}")
    fresh(servers, a="h504", b="h504")
    run = click(worker, servers)
    c.ok("504 on both: one request each, 45 s rests, 'busy' advice (1.9.1: two each)",
         run.count("a") == 1 and run.count("b") == 1 and remaining(b) > 40
         and run.message.startswith("The OpenStreetMap servers are busy.")
         and "Try again in about a minute." in run.message
         and f"{a}, {b}: was busy (HTTP 504)" in run.message, run.message[:200])


def check_hang(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="hang", b="hang")
    run = click(worker, servers)
    c.ok("hang on both: one request each, each cut at T = 2 s (1.9.1: QGIS cut each at its "
         "own 4 s timeout, twice per server)", run.count("a") == 1 and run.count("b") == 1
         and run.elapsed < 7, f"a={run.count('a')} b={run.count('b')} in {run.elapsed:.1f} s")
    c.ok("hang: each server rests 2 minutes (1.9.1 kept no rest)",
         110 <= remaining(a) <= 120 and 110 <= remaining(b) <= 120, f"{remaining(a):.0f} s")
    c.ok("hang: the message advises later or a smaller area and names the wait "
         "(1.9.1: 'non-JSON response (0 bytes): Expecting value')",
         run.message.startswith("The OpenStreetMap servers did not deliver this area in time.")
         and "Try again in about 2 minutes, or choose a smaller area." in run.message
         and f"{a}, {b}: did not answer within {TRANSFER_S} seconds" in run.message
         and not plain_message(run.message), run.message[:220])
    queries = [hit["query"] for hit in run.hits]
    c.ok("the request claims [timeout:10][maxsize:134217728] under a 4 s QGIS timeout "
         "(1.9.1: [timeout:60] and no maxsize)",
         queries and all(q.startswith("[out:json][timeout:10][maxsize:134217728];") for q in queries),
         queries[0][:60] if queries else "no request")


def check_dropped(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="drop", b="drop")
    run = click(worker, servers)
    reasks = [line for line in run.status if "closed the connection; asking it once more" in line]
    counts = f"wire a={run.count('a')} b={run.count('b')}, sends a={attempts(run, 'a')} b={attempts(run, 'b')}"
    if QT6:
        c.ok("dropped connection, Qt 6: each server is asked once more after 5 s and no more "
             "(1.9.1: once more after 1.2 s)",
             run.count("a") == 2 and run.count("b") == 2 and len(reasks) == 2 and run.elapsed >= 10,
             f"{counts}, re-asks={len(reasks)} in {run.elapsed:.1f} s")
        times = [hit["t"] for hit in run.hits if hit["host"] == "a"]
        c.ok("dropped connection, Qt 6: the second request to a server comes 5 s after the first",
             len(times) == 2 and times[1] - times[0] >= 4.9, f"{[round(t - times[0], 2) for t in times]}")
    else:
        c.ok("dropped connection, Qt 5: the plugin sends once per server and adds no re-ask; "
             "Qt's own re-sends follow within milliseconds (1.9.1 sent twice per server, 1.2 s apart)",
             attempts(run, "a") == 1 and attempts(run, "b") == 1 and not reasks
             and 1 <= run.count("a") <= 4 and 1 <= run.count("b") <= 4 and run.elapsed < 4,
             f"{counts}, plugin re-asks={len(reasks)} in {run.elapsed:.1f} s")
    c.ok("dropped connection: each server rests 2 minutes and the message points at the "
         "connection and the proxy (1.9.1: 'Connection closed ... pick a smaller area')",
         remaining(a) > 110 and run.message.startswith("No OpenStreetMap server could be reached.")
         and "proxy settings" in run.message
         and f"{a}, {b}: closed the connection" in run.message and not plain_message(run.message),
         run.message[:200])
    fresh(servers, a=("drop", "ok"), b="ok")
    run = click(worker, servers)
    host = getattr(run.outcome, "host", None)
    if QT6:
        c.ok("dropped once, Qt 6: the same server delivers on the re-ask 5 s later; "
             "the other server is not asked", run.payload is not None and run.count("a") == 2
             and run.count("b") == 0 and run.elapsed >= 4.9 and host in (a, None),
             f"a={run.count('a')} b={run.count('b')} in {run.elapsed:.1f} s host={host}")
    else:
        # Whether Qt 5 re-sends depends on its connection channel: a fresh
        # channel re-sends (the data comes from a), one whose re-sends were
        # spent on an earlier drop does not (the plugin moves on to b).
        c.ok("dropped once, Qt 5: the plugin waits for nothing and asks the first server once; "
             "the data arrives from Qt's re-send or from the next server",
             run.payload is not None and attempts(run, "a") == 1 and run.elapsed < 4
             and ((run.count("a") == 2 and run.count("b") == 0)
                  or (run.count("a") == 1 and run.count("b") == 1)),
             f"a={run.count('a')} b={run.count('b')} in {run.elapsed:.1f} s host={host}")


def check_offline(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    from osm_3d_model.tests._offline import CLOSED

    module = od()
    saved = module.OVERPASS_ENDPOINTS
    module.OVERPASS_ENDPOINTS = (f"{CLOSED}/api/interpreter", "http://127.0.0.1:1/api/interpreter")
    # Windows reports a refused loopback connection after about 2 s (it
    # retries the SYN), as long as this suite's T: give it T = 6 s here.
    QgsNetworkAccessManager.setTimeout(8000)
    try:
        fresh(servers)
        run = click(worker, servers)
    finally:
        module.OVERPASS_ENDPOINTS = saved
        QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)
    c.ok("offline (connection refused everywhere): the advice is the connection and the proxy, "
         "in plain words (1.9.1: 'Connection refused ... pick a smaller area')",
         run.message.startswith("No OpenStreetMap server could be reached.")
         and "Check the internet connection and the proxy settings in QGIS" in run.message
         and "127.0.0.1:9, 127.0.0.1:1: refused the connection" in run.message
         and not plain_message(run.message), run.message[:240])


def check_oversize(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="oversize", b="ok")
    run = click(worker, servers)
    sent = max((hit.get("sent", 0) for hit in run.hits if hit["host"] == "a"), default=0)
    c.ok("oversize: the answer is cut once it passes 128 MiB, never downloaded in full "
         "(1.9.1 read all of it first)", 128 * MIB <= sent <= 192 * MIB,
         f"server sent {sent / MIB:.0f} MiB of {OVERSIZE_TOTAL // MIB}")
    c.ok("oversize: the other server is not asked for the same too-big area, and the advice is a "
         "smaller area (1.9.1 asked every server)", run.count("a") == 1 and run.count("b") == 0
         and run.message.startswith("This area holds too much OpenStreetMap data for one download.")
         and "Choose a smaller area." in run.message
         and f"{a}: sent more than 128 MB; {b}: not asked" in run.message, run.message[:220])
    board = getattr(od(), "BOARD", None)
    last = board.last_answer(a) if board is not None else None
    c.ok("oversize: the answer is remembered as too big but costs the server no rest "
         "(1.9.1 remembered nothing)", last is not None and last[0] == "too_large"
         and remaining(a) == 0.0, f"last answer={last} rest={remaining(a):.0f} s")


def check_empty_and_remark(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="empty", b="ok")
    run = click(worker, servers, use_cache=True)
    c.ok("empty answer: used once (0 elements, from the first server) and never cached "
         "(1.9.1 kept it for a week)",
         run.payload is not None and run.payload.get("elements") == [] and run.count("a") == 1
         and cache_files() == [],
         f"a={run.count('a')} b={run.count('b')} cache={cache_files()} error={run.message[:80]!r}")
    again = click(worker, servers, use_cache=True)
    c.ok("empty answer: the next click asks again instead of replaying it "
         "(1.9.1 sent nothing and blamed the area)", again.count("a") == 1
         and not getattr(again.outcome, "from_cache", False), f"requests={len(again.hits)}")

    fresh(servers, a="remark", b="ok")
    run = click(worker, servers, use_cache=True)
    c.ok("remark on the first server: the second delivers; the first rests 30 s",
         run.payload is not None and not run.payload.get("remark") and run.count("a") == 1
         and run.count("b") == 1 and 25 <= remaining(a) <= 30,
         f"a={run.count('a')} b={run.count('b')} rest {remaining(a):.0f} s")
    kept = []
    for name in cache_files():
        with open(os.path.join(od()._cache_dir(), name), encoding="utf-8") as handle:
            kept.append(json.load(handle))
    c.ok("remark: the incomplete answer is never cached; the complete one is (kept)",
         len(kept) == 1 and not kept[0].get("remark"), f"{len(kept)} kept")
    fresh(servers, a="remark", b="remark")
    run = click(worker, servers, use_cache=True)
    c.ok("remark on both: no data is trusted, nothing cached, and the advice is later or a "
         "smaller area", run.error is not None and cache_files() == []
         and run.message.startswith("The OpenStreetMap servers did not deliver this area in time.")
         and f"{a}, {b}: ran out of time on this query" in run.message
         and not plain_message(run.message), run.message[:200])


def check_identity(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    module = od()
    fresh(servers, a="ok")
    run = click(worker, servers)
    referer = getattr(module, "REFERER", "https://github.com/YusufEminoglu/osm_3d_model")
    c.ok("every request carries the Referer https://github.com/YusufEminoglu/osm_3d_model "
         "(1.9.1 sent none)", run.hits and all(hit["referer"] == referer for hit in run.hits)
         and referer == "https://github.com/YusufEminoglu/osm_3d_model",
         run.hits[0]["referer"] if run.hits else "no request")
    ua = run.hits[0]["ua"] if run.hits else ""
    if QT6:
        c.ok("QGIS 4.2 appends the plugin to its User-Agent (1.9.1's header was replaced by QGIS)",
             "3D-OSM-Model-QGIS-Plugin/" in ua and "QGIS/" in ua, ua)
    else:
        c.ok("QGIS 3.44 cannot append a suffix; its stock User-Agent arrives and the Referer "
             "identifies the plugin (kept)", "QGIS/" in ua, ua)
    c.ok("the status ends with the server that delivered",
         run.status[-1:] == [f"{servers.host('a')} delivered the data."], " | ".join(run.status))
    elapsed = [line for line in run.status if _ELAPSED.search(line)]
    c.ok("status sentences carry no elapsed seconds; the dialog adds those (kept)", not elapsed, str(elapsed))


def check_sessions(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    from osm_3d_model.net.hosts import HostBoard

    module = od()
    a, b = servers.host("a"), servers.host("b")
    mono, wall = ShiftClock(time.monotonic), ShiftClock(time.time)
    original = getattr(module, "BOARD", None)
    module.BOARD = HostBoard(clock=mono, wall=wall)
    try:
        fresh(servers, a="h429", b="ok")
        click(worker, servers)
        settings = QgsSettings()
        stored = settings.value("osm_3d_model/hosts/board", "", type=str)
        last = settings.value("osm_3d_model/hosts/last_worked", "", type=str)
        board = json.loads(stored) if stored else {}
        rest_until = (board.get(a) or {}).get("rest_until", 0)
        c.ok("the rest and the last server that worked are kept in QgsSettings under "
             "osm_3d_model/hosts/ (1.9.1 kept nothing)",
             last == b and 55 <= rest_until - wall() <= 60, f"last_worked={last!r} rest_until-now="
             f"{rest_until - wall():.0f}" if rest_until else f"stored={stored[:80]!r}")
        module.BOARD = HostBoard(clock=mono, wall=wall)       # QGIS restarted
        servers.set("a", "ok")
        servers.set("b", "ok")
        module.clear_cache()
        run = click(worker, servers)
        c.ok("after a restart the resting server is still not asked and the one that worked "
             "is asked first (1.9.1 asked the 429 server again at once)",
             run.count("a") == 0 and run.count("b") == 1, f"a={run.count('a')} b={run.count('b')}")
        mono.advance(61)
        wall.advance(61)
        module.BOARD = HostBoard(clock=mono, wall=wall)       # restarted after the rest
        module.clear_cache()
        run = click(worker, servers)
        c.ok("after the rest has passed, the server that worked last is still asked first",
             run.count("a") == 0 and run.count("b") == 1, f"a={run.count('a')} b={run.count('b')}")
    finally:
        if original is not None:
            module.BOARD = original


def check_cancel(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    """Last: on 1.9.1 a cancel cannot stop a transfer, and its worker stays blocked."""
    module = od()
    a = servers.host("a")
    cancelled = getattr(module, "OsmDownloadCancelled", None) or module.OsmDownloadError
    QgsNetworkAccessManager.setTimeout(60000)        # T = 55 s: only Cancel can end it soon
    try:
        for label, legacy in (("QgsFeedback (C1)", False), ("the 1.9 cancel_check", True)):
            fresh(servers, a="hang", b="hang")
            feedback = QgsFeedback()
            stop = {"flag": False}
            marks: Dict[str, float] = {}

            def tick(seconds: float) -> None:
                if seconds > 0.6 and "cancel" not in marks:
                    marks["cancel"] = time.monotonic()
                    stop["flag"] = True
                    feedback.cancel()

            run = click(worker, servers, feedback=feedback, cancel_check=lambda: stop["flag"],
                        legacy=legacy, limit=12.0, on_tick=tick)
            took = (run.done_at - marks["cancel"]) if run.finished and "cancel" in marks else None
            c.ok(f"Cancel through {label} ends a hung transfer within 2 s with 'Download cancelled.' "
                 "(1.9.1 ran on for up to a minute)",
                 took is not None and took < 2.0 and isinstance(run.error, cancelled)
                 and str(run.error) == "Download cancelled.",
                 f"finished={run.finished} took={None if took is None else round(took, 2)} s "
                 f"error={run.message!r}")
            c.ok(f"Cancel through {label}: the server earns no rest and the other is not asked",
                 run.finished and run.count("b") == 0 and remaining(a) == 0.0,
                 f"b={run.count('b')} rest={remaining(a):.0f} s")
            if not run.finished:
                return      # the worker is still blocked in the transfer
    finally:
        QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)


def check_cancel_before_send(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    """A Cancel that lands before the request is sent: nothing may go out.

    QgsBlockingNetworkRequest ignores a feedback that is already cancelled,
    so a cancelled click used to send its POST anyway and wait for the server
    (release review of 1.10.0)."""
    module = od()
    cancelled = getattr(module, "OsmDownloadCancelled", None) or module.OsmDownloadError
    fresh(servers, a="hang", b="hang")
    feedback = QgsFeedback()
    feedback.cancel()
    started = time.monotonic()
    error = None
    try:
        module._post_once(servers.url("a"), b"data=%5Bout%3Ajson%5D%3B", 5.0, feedback)
    except Exception as caught:  # noqa: BLE001 - the check reads what was raised
        error = caught
    took = time.monotonic() - started
    time.sleep(0.3)                     # let a request that did go out reach the server
    hits = servers.take_hits()
    c.ok("A Cancel before the request is sent raises 'Download cancelled.' at once and "
         "sends nothing (it used to send the POST and wait for the server)",
         isinstance(error, cancelled) and took < 1.0 and not hits,
         f"error={error!r} took={took:.2f} s requests={len(hits)}")


_DEFAULT_ENDPOINTS = ()


# ------------------------------------------------------------------ main
def run_all() -> bool:
    global _DEFAULT_ENDPOINTS
    from osm_3d_model import osm_download

    _DEFAULT_ENDPOINTS = tuple(osm_download.OVERPASS_ENDPOINTS)
    from osm_3d_model.tests._offline import isolate_download_cache, isolate_network, refused

    isolate_network()
    isolate_download_cache()
    print("=" * 62)
    print(" osm_3d_model - Overpass rules against fake servers on 127.0.0.1")
    print("=" * 62, flush=True)
    c = Checks()
    QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)
    servers = FakeOverpass()
    worker = Worker()
    osm_download.OVERPASS_ENDPOINTS = (servers.url("a"), servers.url("b"))
    started = time.monotonic()
    steps = (check_contract, check_rate_limit, check_refused, check_invalid, check_busy,
             check_hang, check_dropped, check_offline, check_oversize, check_empty_and_remark,
             check_identity, check_sessions, check_cancel_before_send, check_cancel)
    for step in steps:
        step_started = time.monotonic()
        try:
            if step is check_contract:
                step(c)
            else:
                step(c, worker, servers)
        except Exception as error:  # noqa: BLE001 - a crashed step is a failed step
            traceback.print_exc()
            c.ok(f"{step.__name__} ran to the end", False, f"{type(error).__name__}: {error}")
        print(f"  ({step.__name__}: {time.monotonic() - step_started:.1f} s)", flush=True)
    c.ok("no request left this computer (guard)", not refused(), str(refused()[:3]))
    print("-" * 62)
    print(f"  {c.passed}/{len(c.results)} passed in {time.monotonic() - started:.0f} s")
    return c.passed == len(c.results)


def main() -> None:
    profile = tempfile.TemporaryDirectory(prefix="osm_3d_model-rules-")
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
    # TRAPS 4.6: exit from the frame that holds the application; a worker
    # still blocked in a 1.9.1 transfer cannot keep the process alive.
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
