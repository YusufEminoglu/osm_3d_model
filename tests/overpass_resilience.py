"""How osm_3d_model treats the Overpass servers over time (P1-P3, P15).

The 1.9.1 version of this suite replaced the network function with a stub
and asserted the 1.2 s retry after a 429 as correct, so it could never see
that QGIS reports every 4xx/5xx as a failed request and that the status
branch never ran.  This one stubs nothing: each download goes through the
real ``osm_download`` code and QGIS's network stack to a fake server on
127.0.0.1, which answers with real HTTP statuses.  Only time is shifted:
the module's host board runs on clocks a check can advance, so a rest of
15 minutes is checked without waiting 15 minutes.

The rules checked, each against the 1.9.1 behaviour it replaces:

* the HTTP status is read even though QGIS reports an error;
* one request per server per click, no immediate retry;
* a server rests after a failure (429: 60 s, 5xx: 45 s, 404: 120 s), the
  rest doubles while it keeps failing, capped at 15 minutes, and a success
  clears it;
* the server that worked last is asked first;
* after a dropped connection the same server is asked once more only on
  Qt 6, 5 s later (Qt 5 re-sends by itself);
* the transfer waits T = min(55 s, QGIS timeout - 2 s) and the request
  claims [timeout:N] with N = max(10, T - 15).

Run from the folder that contains the plugin, with QGIS_CUSTOM_CONFIG_PATH
pointing at a throwaway folder (docs/TRAPS.md 4.6):

    python-qgis-ltr.bat -m osm_3d_model.tests.overpass_resilience    # QGIS 3.44
    python-qgis.bat     -m osm_3d_model.tests.overpass_resilience    # QGIS 4.2
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))

from qgis.core import QgsApplication, QgsNetworkAccessManager  # noqa: E402

from osm_3d_model.tests.test_overpass_rules_qgis import (  # noqa: E402
    QT6,
    Checks,
    FakeOverpass,
    ShiftClock,
    Worker,
    attempts,
    click,
    fresh,
)

QGIS_TIMEOUT_MS = 4000


def od():
    from osm_3d_model import osm_download

    return osm_download


def install_shifted_board():
    """Give the module a host board on clocks the checks can advance."""
    from osm_3d_model.net.hosts import HostBoard

    mono, wall = ShiftClock(time.monotonic), ShiftClock(time.time)
    od().BOARD = HostBoard(clock=mono, wall=wall)

    def advance(seconds: float) -> None:
        mono.advance(seconds)
        wall.advance(seconds)

    return advance


def rest(host: str) -> float:
    board = getattr(od(), "BOARD", None)
    return board.remaining(host) if board is not None and hasattr(board, "remaining") else 0.0


def only(servers: FakeOverpass, *names: str) -> None:
    od().OVERPASS_ENDPOINTS = tuple(servers.url(name) for name in names)


# --------------------------------------------------------------- checks
def check_status_and_one_request(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    """Through the 1.9 API, which both versions have."""
    a = servers.host("a")
    only(servers, "a")
    for mode, code, expected_rest in (("h429", 429, 60), ("h502", 502, 45), ("h404", 404, 120)):
        fresh(servers, a=mode)
        run = click(worker, servers, legacy=True)
        c.ok(f"HTTP {code} is read although QGIS reports the request as failed "
             "(1.9.1 showed Qt's 'server replied' text, the status branch never ran)",
             f"HTTP {code}" in run.message and "server replied" not in run.message,
             run.message[-110:])
        c.ok(f"HTTP {code}: one request, no retry on the same server (1.9.1: a second one 1.2 s later)",
             run.count("a") == 1, f"{run.count('a')} requests")
        c.ok(f"HTTP {code}: the server rests {expected_rest} s (1.9.1 kept no rest)",
             expected_rest - 5 <= rest(a) <= expected_rest, f"rests {rest(a):.1f} s")
    c.ok("the 1.2 s same-server retry is gone (MIRROR_RETRY_DELAY_S in 1.9.1)",
         not hasattr(od(), "MIRROR_RETRY_DELAY_S"))


def check_doubling(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a = servers.host("a")
    only(servers, "a")
    advance = install_shifted_board()
    fresh(servers, a="h429")
    rests, requests = [], []
    for _round in range(6):
        run = click(worker, servers)
        requests.append(run.count("a"))
        rests.append(round(rest(a)))
        advance(rest(a) + 1)
    c.ok("a server that keeps answering 429 rests 60, 120, 240, 480, 900, 900 s "
         "(doubling, capped at 15 minutes; 1.9.1 retried after 1.2 s every time)",
         rests == [60, 120, 240, 480, 900, 900] and requests == [1] * 6, f"rests={rests} requests={requests}")
    strikes = getattr(od().BOARD, "strikes", lambda host: -1)
    before = strikes(a)
    servers.set("a", "ok")
    run = click(worker, servers)
    c.ok("a success clears the six strikes and the rest (1.9.1 kept no strikes to clear)",
         before == 6 and run.payload is not None and rest(a) == 0.0 and strikes(a) == 0,
         f"strikes before={before} after={strikes(a)} rest={rest(a):.0f} payload={run.payload is not None}")
    servers.set("a", "h429")
    od().clear_cache()
    click(worker, servers)
    c.ok("after a success the next 429 rests the base 60 s again, not double",
         55 <= rest(a) <= 60, f"{rest(a):.0f} s")


def check_resting_server_skipped(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a = servers.host("a")
    only(servers, "a", "b")
    advance = install_shifted_board()
    fresh(servers, a="h502", b="ok")
    first = click(worker, servers)
    c.ok("502 then data: the busy server is asked once, the other delivers",
         first.payload is not None and first.count("a") == 1 and first.count("b") == 1,
         f"a={first.count('a')} b={first.count('b')}")
    servers.set("a", "ok")
    od().clear_cache()
    second = click(worker, servers)
    c.ok("inside the 45 s rest the busy server is skipped and said to rest "
         "(1.9.1 asked it first again)", second.count("a") == 0 and second.count("b") == 1
         and any(line.startswith(f"{a} is resting") for line in second.status),
         f"a={second.count('a')} b={second.count('b')} status={second.status[:1]}")
    advance(46)
    od().clear_cache()
    third = click(worker, servers)
    c.ok("after the rest, the server that worked last is asked first "
         "(1.9.1 always started with the first listed)", third.count("a") == 0 and third.count("b") == 1,
         f"a={third.count('a')} b={third.count('b')}")


def check_dropped_connection(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    a = servers.host("a")
    only(servers, "a")
    fresh(servers, a="drop")
    run = click(worker, servers)
    times = [hit["t"] for hit in run.hits]
    offsets = [round(t - times[0], 2) for t in times] if times else []
    if QT6:
        c.ok("dropped connection, Qt 6: the same server is asked once more, 5 s later, and no more "
             "(1.9.1: once more after 1.2 s)",
             len(times) == 2 and times[1] - times[0] >= 4.9, f"request times {offsets}")
    else:
        # Qt 5 re-sends a dropped POST up to three times within milliseconds,
        # as often as its connection channel still allows.
        c.ok("dropped connection, Qt 5: the plugin sends once and adds no re-ask; Qt's own "
             "re-sends follow at once (1.9.1: a second send 1.2 s later)",
             attempts(run, "a") == 1 and 1 <= len(times) <= 4, f"request times {offsets}")
    c.ok("dropped connection: the server rests 2 minutes and the message says it closed the "
         "connection", 110 <= rest(a) <= 120 and f"{a}: closed the connection" in run.message,
         f"rest {rest(a):.0f} s; {run.message[-80:]}")


def check_time_limits(c: Checks, worker: Worker, servers: FakeOverpass) -> None:
    module = od()
    a = servers.host("a")
    only(servers, "a")
    measured = {}
    transfer = getattr(module, "_transfer_seconds", None)
    for timeout_ms in (4000, 30000, 60000, 120000):
        QgsNetworkAccessManager.setTimeout(timeout_ms)
        measured[timeout_ms] = transfer() if transfer is not None else None
    QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)
    c.ok("the transfer waits T = min(55 s, QGIS timeout - 2 s) (1.9.1: 70 s, longer than QGIS "
         "waits, so QGIS cut every hang and called it an empty success)",
         measured == {4000: 2.0, 30000: 28.0, 60000: 55.0, 120000: 55.0}, str(measured))
    heads = {}
    for timeout_ms in (4000, 30000, 60000):
        QgsNetworkAccessManager.setTimeout(timeout_ms)
        fresh(servers, a="ok")
        run = click(worker, servers)
        heads[timeout_ms] = run.hits[0]["query"].split("\n", 1)[0] if run.hits else ""
    QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)
    c.ok("the request claims [timeout:N] with N = max(10, T - 15) and [maxsize:134217728] "
         "(1.9.1: [timeout:60], no maxsize)",
         heads == {4000: "[out:json][timeout:10][maxsize:134217728];",
                   30000: "[out:json][timeout:13][maxsize:134217728];",
                   60000: "[out:json][timeout:40][maxsize:134217728];"}, str(heads))
    fresh(servers, a="hang")
    run = click(worker, servers)
    c.ok("a hung server is given up after T = 2 s under a 4 s QGIS timeout, once "
         "(1.9.1: QGIS cut it at its own 4 s and the user read 'non-JSON response (0 bytes)')",
         run.count("a") == 1 and 1.5 <= run.elapsed <= 3.5
         and f"{a}: did not answer within 2 seconds" in run.message,
         f"{run.count('a')} requests in {run.elapsed:.1f} s; {run.message[-70:]}")


def check_network_stack(c: Checks) -> None:
    """Kept from 1.9.1: QGIS's stack honours its proxy, TLS and certificate settings."""
    source_path = os.path.join(PLUGIN_DIR, "osm_download.py")
    with open(source_path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Import):
            used.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            used.add(node.module)
    c.ok("downloads go through QGIS's network stack, never urllib (kept from 1.9.1)",
         "urlopen" not in used and "urllib.request" not in used
         and "QgsBlockingNetworkRequest" in used)


# ------------------------------------------------------------------ main
def run_all() -> bool:
    from osm_3d_model.tests._offline import isolate_download_cache, isolate_network, refused

    isolate_network()
    isolate_download_cache()
    print("=" * 62)
    print(" osm_3d_model - Overpass rules over time (real status handling)")
    print("=" * 62, flush=True)
    c = Checks()
    QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)
    servers = FakeOverpass()
    worker = Worker()
    module = od()
    original_board = getattr(module, "BOARD", None)
    started = time.monotonic()
    for step in (check_status_and_one_request, check_doubling, check_resting_server_skipped,
                 check_dropped_connection, check_time_limits):
        step_started = time.monotonic()
        try:
            step(c, worker, servers)
        except Exception as error:  # noqa: BLE001 - a crashed step is a failed step
            traceback.print_exc()
            c.ok(f"{step.__name__} ran to the end", False, f"{type(error).__name__}: {error}")
        finally:
            if original_board is not None:
                module.BOARD = original_board
        print(f"  ({step.__name__}: {time.monotonic() - step_started:.1f} s)", flush=True)
    check_network_stack(c)
    c.ok("no request left this computer (guard)", not refused(), str(refused()[:3]))
    print("-" * 62)
    print(f"  {c.passed}/{len(c.results)} passed in {time.monotonic() - started:.0f} s")
    return c.passed == len(c.results)


def main() -> None:
    profile = tempfile.TemporaryDirectory(prefix="osm_3d_model-overpass-")
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
