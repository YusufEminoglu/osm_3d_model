# -*- coding: utf-8 -*-
"""What one Overpass request came to, and how to say it to a person.

Ported from 02AgentOSM 1.6.0 ``core/transport.py``; the tiling parts are left
out, because this plugin asks for one area in one request.

Pure Python by design: ``osm_download`` owns the network stack and hands this
module the plain facts of one request (HTTP status, body, Qt's error number
and text), so every rule that decides a rest or a message is testable without
QGIS.

The status is read first and the body second, because QGIS reports every 4xx
or 5xx reply as a failed request while the status and the server's own error
page stay readable.  Reading only the request's error turned a 429 into an
immediate retry (3D OSM Model 1.9.1).  Only a 400 says the request itself is
wrong; a 404 or 405 from a moved address or a proxy is that server's failure.
An empty body with no status is the client giving up, which Qt 5 even
reports as success.

Messages use the plugin's own words only: Qt's error strings never reach the
person reading them.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: An answer larger than this is cut off while it arrives and never kept.
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
#: Where a person can report a fault in the request itself.
ISSUES_URL = "https://github.com/YusufEminoglu/osm_3d_model/issues"

# ---------------------------------------------------------------- classes
OK = "ok"
RATE = "rate"                      # HTTP 429
BLOCKED = "blocked"                # HTTP 403 / 406
BUSY = "busy"                      # 5xx, capacity page, duplicate query
CLIENT_TIMEOUT = "client_timeout"  # nothing usable arrived in time
NETWORK = "network"                # refused, unreachable, closed, proxy
TLS = "tls"                        # the secure connection failed
SERVER_TIMEOUT = "server_timeout"  # the server ran out of time on the query
SERVER_MEMORY = "server_memory"    # the server ran out of memory on the query
TOO_LARGE = "too_large"            # the answer passed MAX_RESPONSE_BYTES
BAD_QUERY = "bad_query"            # HTTP 400: the request itself is refused
HOST_ERROR = "host_error"          # any other status that is not data

#: Answers that say the area may be too heavy for one request.
HEAVY = frozenset({SERVER_TIMEOUT, SERVER_MEMORY, TOO_LARGE})
#: The heavy answers that prove it: the same request would fail the same way
#: on every server, so no other server is asked.  A server timeout alone does
#: not prove it: an overloaded server times out on a light area too.
DENSE = frozenset({SERVER_MEMORY, TOO_LARGE})

# ------------------------------------------------------------- categories
# What a download that ended without data is called; chosen from the classes
# of the answers.
BUSY_SERVERS = "busy"
RATE_LIMITED = "rate_limited"
REFUSED = "refused"
OFFLINE = "offline"
TIMED_OUT = "timed_out"
TOO_DENSE = "too_dense"
INVALID_REQUEST = "bad_query"
SERVER_ERROR = "server_error"

# QNetworkReply::NetworkError values this module reads.
_QT_CONNECTION_REFUSED = 1
_QT_REMOTE_HOST_CLOSED = 2
_QT_HOST_NOT_FOUND = 3
_QT_TIMEOUT = 4
_QT_OPERATION_CANCELED = 5
_QT_SSL_HANDSHAKE_FAILED = 6
_QT_PROXY_ERRORS = range(101, 200)

# ------------------------------------------------------------- time limits
#: Longest a transfer waits.  QGIS aborts any request at its own network
#: timeout (60 s by default) and reports it as an empty success, so waiting
#: longer than that can never receive anything.
MAX_TRANSFER_SECONDS = 55.0
_QGIS_TIMER_MARGIN_SECONDS = 2.0
#: An accepted query can sit up to 15 s in the server's queue before it runs.
QUEUE_SECONDS = 15.0
MIN_SERVER_TIMEOUT_SECONDS = 10
DEFAULT_QGIS_TIMEOUT_MS = 60_000


def transfer_seconds(qgis_timeout_ms: object) -> float:
    """T = min(55 s, QGIS network timeout - 2 s)."""
    try:
        qgis_seconds = float(qgis_timeout_ms) / 1000.0
    except (TypeError, ValueError):
        qgis_seconds = DEFAULT_QGIS_TIMEOUT_MS / 1000.0
    if not math.isfinite(qgis_seconds) or qgis_seconds <= 0:
        qgis_seconds = DEFAULT_QGIS_TIMEOUT_MS / 1000.0
    return max(1.0, min(MAX_TRANSFER_SECONDS, qgis_seconds - _QGIS_TIMER_MARGIN_SECONDS))


def server_timeout_seconds(transfer_s: float) -> int:
    """`[timeout:N]` with N = max(10, T - 15): what the client can wait out."""
    return max(MIN_SERVER_TIMEOUT_SECONDS, int(float(transfer_s) - QUEUE_SECONDS))


# ----------------------------------------------------------------- verdict
@dataclass(frozen=True)
class Verdict:
    """One request to one server.

    `detail` is a verb phrase that completes "<server> ...", for example
    "was busy (HTTP 502)", so messages can name each server and its answer.
    """

    kind: str
    detail: str
    payload: Optional[Dict[str, Any]] = None
    #: Only a dropped connection earns one more request to the same server.
    retry_same_host: bool = False

    @property
    def ok(self) -> bool:
        return self.kind == OK


def _seconds_text(seconds: float) -> str:
    whole = max(1, int(round(float(seconds))))
    return "1 second" if whole == 1 else f"{whole} seconds"


def _short(text: object, limit: int = 90) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _status_verdict(status: int, lowered_head: str) -> Optional[Verdict]:
    if status == 429:
        return Verdict(RATE, "asked this computer to slow down (HTTP 429)")
    if status in (403, 406):
        return Verdict(BLOCKED, f"refused this computer (HTTP {status})")
    if 500 <= status <= 599:
        if "capacity" in lowered_head:
            return Verdict(BUSY, f"was at capacity (HTTP {status})")
        return Verdict(BUSY, f"was busy (HTTP {status})")
    if status == 400:
        return Verdict(BAD_QUERY, "rejected the request as invalid (HTTP 400)")
    if status and not 200 <= status <= 299:
        # 404, 405, 410, 451 ... from a moved address or a proxy, or a
        # redirect that was not followed: this server failed, not the request.
        return Verdict(HOST_ERROR, f"answered with HTTP {status} instead of data")
    return None


_CLOSED = Verdict(NETWORK, "closed the connection", retry_same_host=True)
_NOT_FOUND = Verdict(NETWORK, "could not be found")
_REFUSED = Verdict(NETWORK, "refused the connection")
_THROUGH_PROXY = Verdict(NETWORK, "could not be reached through the proxy")
_SECURE = Verdict(TLS, "could not set up a secure connection")
_UNREACHABLE = Verdict(NETWORK, "could not be reached")


def _error_verdict(error_code: int, error_text: str, transfer_s: float) -> Verdict:
    """A request that ended without a usable HTTP status.

    Qt's error number decides; its text is consulted only when the number is
    unknown, and never repeated, because it means nothing to the reader.
    """
    timed_out = Verdict(CLIENT_TIMEOUT, f"did not answer within {_seconds_text(transfer_s)}")
    by_code = {
        _QT_SSL_HANDSHAKE_FAILED: _SECURE,
        _QT_TIMEOUT: timed_out,
        _QT_OPERATION_CANCELED: timed_out,
        _QT_REMOTE_HOST_CLOSED: _CLOSED,
        _QT_HOST_NOT_FOUND: _NOT_FOUND,
        _QT_CONNECTION_REFUSED: _REFUSED,
    }
    if error_code in by_code:
        return by_code[error_code]
    if error_code in _QT_PROXY_ERRORS:
        return _THROUGH_PROXY
    lowered = error_text.casefold()
    if any(word in lowered for word in ("ssl", "tls", "certificate", "handshake")):
        return _SECURE
    if "proxy" in lowered:
        return _THROUGH_PROXY
    if "timed out" in lowered or "timeout" in lowered or "canceled" in lowered:
        return timed_out
    if "closed" in lowered or "reset" in lowered:
        return _CLOSED
    if "not found" in lowered:
        return _NOT_FOUND
    if "refused" in lowered:
        return _REFUSED
    return _UNREACHABLE


def _dispatcher_verdict(lowered: str) -> Optional[Verdict]:
    """The dispatcher's refusals, before the query ever ran.

    Overpass writes them into an error page or, for JSON output, into the
    answer's remark field.  `request_read_and_idx::timeout` means no query
    slot came free: another server may have one.
    """
    if "rate_limited" in lowered or "too many requests" in lowered:
        return Verdict(RATE, "asked this computer to slow down")
    if "duplicate_query" in lowered:
        return Verdict(BUSY, "was still running the same query")
    if "request_read_and_idx::timeout" in lowered or "too busy" in lowered:
        return Verdict(BUSY, "was too busy to start the query")
    return None


def _page_verdict(lowered_head: str, body: bytes) -> Verdict:
    """A 2xx answer that is not JSON: an error page, or a cut-off answer."""
    refused = _dispatcher_verdict(lowered_head)
    if refused is not None:
        return refused
    if "capacity" in lowered_head:
        return Verdict(BUSY, "was at capacity")
    if "query timed out" in lowered_head:
        return Verdict(SERVER_TIMEOUT, "ran out of time on this query")
    if "out of memory" in lowered_head:
        return Verdict(SERVER_MEMORY, "ran out of memory on this query")
    if body.lstrip()[:1] in (b"{", b"["):
        return Verdict(NETWORK, "cut the answer off before it was complete")
    return Verdict(BUSY, "answered with an error page instead of data")


def _payload_verdict(document: Any) -> Verdict:
    if not isinstance(document, dict):
        return Verdict(BUSY, "sent an answer that is not OSM data")
    remark = str(document.get("remark") or "").strip()
    if remark:
        # An answer with a remark is incomplete, whatever it holds.
        lowered = remark.casefold()
        refused = _dispatcher_verdict(lowered)
        if refused is not None:
            return refused
        if "timed out" in lowered or "timeout" in lowered:
            return Verdict(SERVER_TIMEOUT, "ran out of time on this query")
        if "memory" in lowered:
            return Verdict(SERVER_MEMORY, "ran out of memory on this query")
        return Verdict(BUSY, f"reported a problem ({_short(remark, 70)})")
    elements = document.get("elements")
    if not isinstance(elements, list):
        return Verdict(BUSY, "sent an answer without OSM data")
    if any(not isinstance(item, dict) for item in elements):
        return Verdict(BUSY, "sent a damaged answer")
    return Verdict(OK, "delivered the data", document)


def classify(
    status: int = 0,
    body: bytes = b"",
    error_code: int = 0,
    error_text: str = "",
    oversize: bool = False,
    transfer_s: float = MAX_TRANSFER_SECONDS,
) -> Verdict:
    """Classify one finished request.

    `status` is the HTTP status (0 when no reply arrived), `body` the reply
    content, `error_code` Qt's network error number, `error_text` the
    request's error message (empty when the request reported success), and
    `oversize` is True when the transfer was stopped for passing
    MAX_RESPONSE_BYTES.
    """
    if oversize or len(body) > MAX_RESPONSE_BYTES:
        megabytes = MAX_RESPONSE_BYTES // (1024 * 1024)
        return Verdict(TOO_LARGE, f"sent more than {megabytes} MB")
    head = bytes(body[:2048]).decode("utf-8", "replace")
    lowered_head = head.casefold()
    try:
        status_code = int(status or 0)
    except (TypeError, ValueError):
        status_code = 0
    by_status = _status_verdict(status_code, lowered_head)
    if by_status is not None:
        return by_status
    try:
        code = int(error_code or 0)
    except (TypeError, ValueError):
        code = 0
    text = str(error_text or "").strip()
    if code or text:
        return _error_verdict(code, text, transfer_s)
    if not body:
        if status_code:
            return Verdict(NETWORK, "sent an empty answer")
        # Qt 5 and QGIS's own timer end a stalled transfer this way.
        return Verdict(CLIENT_TIMEOUT, f"did not answer within {_seconds_text(transfer_s)}")
    try:
        document = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _page_verdict(lowered_head, bytes(body))
    return _payload_verdict(document)


def cache_refusal(payload: Any) -> str:
    """Why an answer must not be kept or reused, or "" when it may be.

    An empty answer or one the server marked incomplete is used once and
    never kept, so the next run asks again instead of replaying it.
    """
    if not isinstance(payload, dict):
        return "it is not OSM data"
    if str(payload.get("remark") or "").strip():
        return "the server marked it as incomplete"
    elements = payload.get("elements")
    if not isinstance(elements, list) or not elements:
        return "it is empty"
    if any(not isinstance(item, dict) for item in elements):
        return "it is damaged"
    return ""


# ------------------------------------------------------------- wording
def duration_text(seconds: float) -> str:
    """'a moment', '45 seconds', 'a minute', '2 minutes', 'an hour'."""
    value = max(0.0, float(seconds))
    if value < 2:
        return "a moment"
    if 55 <= value <= 65:
        return "a minute"
    if value < 90:
        return _seconds_text(value)
    if value < 3300:
        return f"{int(math.ceil(value / 60.0))} minutes"
    hours = int(round(value / 3600.0))
    return "an hour" if hours <= 1 else f"{hours} hours"


def wait_text(seconds: float) -> str:
    """'about a minute', 'about 4 minutes', 'about an hour'."""
    value = max(0.0, float(seconds))
    if value <= 90:
        return "about a minute"
    if value < 3300:
        return f"about {int(math.ceil(value / 60.0))} minutes"
    hours = int(math.ceil(value / 3600.0))
    return "about an hour" if hours <= 1 else f"about {hours} hours"


def age_text(seconds: float) -> str:
    """How long ago something happened: 'less than a minute', '3 hours'."""
    value = max(0.0, float(seconds))
    if value < 60:
        return "less than a minute"
    if value < 3600:
        minutes = int(value // 60)
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    if value < 48 * 3600:
        hours = int(value // 3600)
        return "1 hour" if hours == 1 else f"{hours} hours"
    return f"{int(value // 86400)} days"


@dataclass(frozen=True)
class HostAnswer:
    """What one server last said, for the message that closes a download."""

    host: str
    kind: str = ""
    detail: str = ""
    asked: bool = True
    age_s: float = 0.0

    def said(self) -> str:
        """What became of the server, without its name: "was busy (HTTP 502)"."""
        if self.asked and self.detail:
            return self.detail
        if self.detail:
            return (
                f"not asked, because it {self.detail} "
                f"{duration_text(self.age_s)} ago"
            )
        return "not asked"


@dataclass(frozen=True)
class FailureReport:
    kind: str
    category: str
    summary: str
    message: str


def host_summary(answers: Iterable[HostAnswer]) -> str:
    """Each server and its answer: "a, b: did not answer within 55 seconds".

    Servers that answered alike share one entry; entries keep the order the
    servers were listed in and are separated by semicolons.  A server that was
    not asked because of an earlier answer keeps its own entry.
    """
    groups: Dict[Tuple[str, str], List[str]] = {}
    for answer in answers:
        said = answer.said()
        alone = answer.host if not answer.asked and answer.detail else ""
        groups.setdefault((alone, said), []).append(answer.host)
    return "; ".join(
        f"{', '.join(hosts)}: {said}" for (_alone, said), hosts in groups.items()
    )


def category_for(kinds: Sequence[str]) -> Tuple[str, str]:
    """(category, deciding class) for the classes the servers answered with."""
    present = [kind for kind in kinds if kind and kind != OK]
    if not present:
        return BUSY_SERVERS, ""
    if BAD_QUERY in present:
        return INVALID_REQUEST, BAD_QUERY
    if any(kind in DENSE for kind in present) and all(kind in HEAVY for kind in present):
        return TOO_DENSE, next(kind for kind in present if kind in DENSE)
    if all(kind in (NETWORK, TLS) for kind in present):
        return OFFLINE, present[0]
    if BLOCKED in present:
        return REFUSED, BLOCKED
    if RATE in present:
        return RATE_LIMITED, RATE
    if BUSY in present:
        return BUSY_SERVERS, BUSY
    for kind in (CLIENT_TIMEOUT, SERVER_TIMEOUT):
        if kind in present:
            return TIMED_OUT, kind
    if HOST_ERROR in present:
        return SERVER_ERROR, HOST_ERROR
    return BUSY_SERVERS, present[0]


_NOTHING = "Nothing was downloaded."

# What happened and what to do come first; the servers and their answers
# follow in parentheses, so a narrow window shows the advice.
_TEMPLATES = {
    BUSY_SERVERS: "The OpenStreetMap servers are busy. {nothing} Try again in {wait}.",
    RATE_LIMITED: (
        "The OpenStreetMap servers asked this computer to slow down. "
        "{nothing} Wait {wait} before trying again."
    ),
    REFUSED: (
        "The OpenStreetMap servers refused this computer. This usually means a "
        "daily usage limit was reached. {nothing} Try again in {wait}."
    ),
    OFFLINE: (
        "No OpenStreetMap server could be reached. {nothing} Check the internet "
        "connection and the proxy settings in QGIS (Settings > Options > "
        "Network), then try again."
    ),
    TIMED_OUT: (
        "The OpenStreetMap servers did not deliver this area in time. {nothing} "
        "Try again in {wait}, or choose a smaller area."
    ),
    TOO_DENSE: (
        "This area holds too much OpenStreetMap data for one download. "
        "{nothing} Choose a smaller area."
    ),
    INVALID_REQUEST: (
        "The OpenStreetMap server rejected the download request as invalid. "
        "{nothing} Please report this at {issues}."
    ),
    SERVER_ERROR: (
        "The OpenStreetMap servers answered with an error instead of data. "
        "{nothing} Try again in {wait}."
    ),
}


def failure_report(answers: Sequence[HostAnswer], wait_s: float = 0.0) -> FailureReport:
    """The complete sentence for a download that ended without any data.

    The category is decided by the servers asked during this download; when
    none could be asked because all were resting after earlier failures, by
    what they answered then.  The message is the advice, then every server
    and its answer in parentheses.
    """
    asked = [answer for answer in answers if answer.asked and answer.kind]
    relevant = asked or [answer for answer in answers if answer.kind]
    category, kind = category_for([answer.kind for answer in relevant])
    summary = host_summary(answers) or "no server was asked"
    advice = _TEMPLATES[category].format(
        nothing=_NOTHING, wait=wait_text(wait_s), issues=ISSUES_URL
    )
    return FailureReport(kind, category, summary, f"{advice} ({summary})")


#: Words the closing messages must never use: they mean nothing to the
#: person reading them.
JARGON = ("mirror", "pinned", "backend", "endpoint", "remark", "tile")
