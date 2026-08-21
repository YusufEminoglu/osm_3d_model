"""Overpass mirror/retry behaviour for osm_3d_model.

The download path fails in the field, not on a developer's machine, and every one
of its failure modes is quiet: a retry loop that never retries looks identical to
one that does until the day a mirror is flaky, and a loop that retries a
permanent failure just triples the time before the user sees an error. None of
that is visible from a run that happens to succeed.

So the mirror loop is exercised against a stubbed transport: no network, no
Overpass, just the decision-making.

Run from the directory that *contains* the plugin folder:

    python-qgis-ltr.bat -m osm_3d_model.tests.overpass_resilience    # QGIS 3 LTR
    python-qgis.bat     -m osm_3d_model.tests.overpass_resilience    # QGIS 4
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))

from qgis.core import QgsApplication  # noqa: E402

from osm_3d_model import osm_download  # noqa: E402

BBOX = (39.10, 27.10, 39.11, 27.11)
OK_PAYLOAD = {"elements": [{"type": "node", "id": 1, "lat": 39.105, "lon": 27.105}]}


def _ok(name, condition, detail=""):
    tag = "PASS" if condition else "FAIL"
    print(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""))
    return bool(condition)


class _Transport:
    """Stands in for _fetch_one, recording every attempt it is asked to make."""

    def __init__(self, script):
        self.script = list(script)   # list of (payload, detail, transient)
        self.calls = []

    def __call__(self, endpoint, query, timeout_s):
        self.calls.append(endpoint)
        if self.script:
            return self.script.pop(0)
        return None, "exhausted", False


def _run(script, **kwargs):
    transport = _Transport(script)
    original = osm_download._fetch_one
    osm_download._fetch_one = transport
    original_delay = osm_download.MIRROR_RETRY_DELAY_S
    osm_download.MIRROR_RETRY_DELAY_S = 0.0          # keep the suite fast
    try:
        payload, error = None, None
        try:
            payload = osm_download.fetch_overpass(*BBOX, use_cache=False, **kwargs)
        except osm_download.OsmDownloadError as exc:
            error = exc
        return transport, payload, error
    finally:
        osm_download._fetch_one = original
        osm_download.MIRROR_RETRY_DELAY_S = original_delay


def run_all():
    results = []
    mirrors = list(osm_download.OVERPASS_ENDPOINTS)
    results.append(_ok("at least two mirrors are configured", len(mirrors) >= 2, f"{len(mirrors)}"))

    # A first-try success must not touch a second mirror.
    t, payload, err = _run([(OK_PAYLOAD, "", False)])
    results.append(_ok("first mirror answering ends the search",
                       payload is not None and err is None and t.calls == [mirrors[0]],
                       f"calls={len(t.calls)}"))

    # A transient failure is retried once on the SAME mirror before moving on.
    t, payload, err = _run([(None, "HTTP 504", True), (OK_PAYLOAD, "", False)])
    results.append(_ok("a transient failure retries the same mirror once",
                       payload is not None and t.calls == [mirrors[0], mirrors[0]],
                       f"calls={t.calls!r}"))

    # A permanent failure must NOT be retried; it moves straight to the next mirror.
    t, payload, err = _run([(None, "HTTP 400", False), (OK_PAYLOAD, "", False)])
    results.append(_ok("a permanent failure skips to the next mirror",
                       payload is not None and t.calls == [mirrors[0], mirrors[1]],
                       f"calls={t.calls!r}"))

    # A mirror gets at most two attempts, so three mirrors cost at most six calls.
    t, payload, err = _run([(None, "HTTP 503", True)] * 12)
    results.append(_ok("every mirror is tried at most twice",
                       err is not None and len(t.calls) == 2 * len(mirrors),
                       f"calls={len(t.calls)}"))

    # The error names the mirrors that failed, so a user can report something useful.
    named = err is not None and any(
        host.split("/")[2] in str(err) for host in mirrors[-3:]
    )
    results.append(_ok("the final error names the mirrors it tried", named, str(err)[:90]))

    # A rate limit is transient: it must be retried, not abandoned.
    t, payload, err = _run([(None, "rate-limited (HTTP 429)", True), (OK_PAYLOAD, "", False)])
    results.append(_ok("a rate limit is retried rather than abandoned",
                       payload is not None and len(t.calls) == 2))

    # A well-formed HTTP response carrying an Overpass 'remark' is still a failure,
    # and it is permanent - retrying the same mirror would return the same remark.
    t, payload, err = _run([
        ({"remark": "runtime error: Query timed out", "elements": []}, "", False),
        (OK_PAYLOAD, "", False),
    ])
    results.append(_ok("an Overpass remark fails over instead of being trusted",
                       payload is not None and t.calls == [mirrors[0], mirrors[1]],
                       f"calls={t.calls!r}"))

    # Cancellation is honoured before any request is made.
    t, payload, err = _run([(OK_PAYLOAD, "", False)], cancel_check=lambda: True)
    results.append(_ok("cancelling stops before the first request",
                       err is not None and not t.calls, f"calls={len(t.calls)}"))

    # _fetch_one itself rejects a non-HTTPS endpoint without any network use, and
    # reports it as permanent so it is not retried.
    payload, detail, transient = osm_download._fetch_one("ftp://example.invalid/x", "q", 5)
    results.append(_ok("a non-HTTPS endpoint is rejected as permanent",
                       payload is None and transient is False, detail))

    # The plugin must not drift back to urllib, which bypasses QGIS's proxy, TLS
    # and certificate settings - the cause of the SSL: WRONG_VERSION_NUMBER
    # reports. Parsed rather than grepped, so prose in a comment or a docstring
    # that merely mentions the old call cannot fail (or pass) this.
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
    results.append(_ok("downloads go through QGIS's network stack",
                       "urlopen" not in used
                       and "urllib.request" not in used
                       and "QgsBlockingNetworkRequest" in used,
                       "urlopen still called" if "urlopen" in used else ""))

    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} passed")
    return passed == len(results)


def main():
    app = QgsApplication.instance()
    owns_app = app is None
    profile = None
    if owns_app:
        profile = tempfile.TemporaryDirectory(prefix="osm_3d_model-overpass-")
        app = QgsApplication([], True, profile.name, "external")
        app.initQgis()
    try:
        return run_all()
    finally:
        if owns_app:
            app.exitQgis()
            profile.cleanup()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
else:
    main()
