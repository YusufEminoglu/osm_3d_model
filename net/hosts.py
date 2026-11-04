# -*- coding: utf-8 -*-
"""Memory of how each Overpass server is doing, and the rules for asking them.

Ported from 02AgentOSM 1.6.0 ``core/mirrors.py``; the tiling and request
budget parts are left out, because this plugin asks for one area per click.

Pure Python and thread-safe: downloads run on worker threads and share one
board, so a server that just refused one download rests for the next one too.

* A failed server rests for a time that depends on what it answered, and the
  rest doubles while it keeps failing (capped at the larger of its base and
  15 minutes).  A success clears its record.
* Servers are offered in this order: not resting, most recent success first,
  then the order the caller listed them in.
* One request at a time per server, at least one second apart.
* :meth:`HostBoard.snapshot` and :meth:`HostBoard.restore` carry the rests
  ("rest until" in wall-clock seconds), the strikes, the last answers and the
  last success across QGIS sessions; ``osm_download`` keeps them in QgsSettings.

:func:`ask_hosts` asks the servers for one download under those rules: each
server at most once, the same one once more only after it dropped the
connection (and only when the caller allows it), and a wait for a resting
server only when the wait is short.

Time comes from injected clocks, so tests advance them instead of sleeping.
Nothing here sleeps: waiting goes through the caller's (cancellable) pause.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .transport import (
    BAD_QUERY,
    BLOCKED,
    BUSY,
    CLIENT_TIMEOUT,
    DENSE,
    HOST_ERROR,
    NETWORK,
    OK,
    RATE,
    SERVER_TIMEOUT,
    TLS,
    Verdict,
)

#: Seconds a server rests after its first failure of each class.  429 and
#: 403/406 are the operators' explicit "stop" signals: their rule is at least
#: 30 s, and repeated 429s get an application banned.  An immediate 5xx is a
#: refusal at the door; a hang is an accepted query that never came back; a
#: 404 or 405 is an address that moved or a proxy in the way.  An answer over
#: 128 MiB, or out of memory, says only that the area is too big; it costs
#: the server nothing.
COOLDOWN_SECONDS: Dict[str, float] = {
    RATE: 60.0,
    BLOCKED: 3600.0,
    BUSY: 45.0,
    CLIENT_TIMEOUT: 120.0,
    NETWORK: 120.0,
    TLS: 3600.0,
    HOST_ERROR: 120.0,
}
#: Rests of a fixed length that are not strikes.  A query that ran out of
#: server time may mean an overloaded server or a dense area: the server
#: rests briefly, but the rest never grows.
FLAT_REST_SECONDS: Dict[str, float] = {
    SERVER_TIMEOUT: 30.0,
}
COOLDOWN_CAP_SECONDS = 15 * 60.0
#: No restored rest is longer than the longest rest any answer can earn, so a
#: wall clock set back by hand cannot lock a server out for days.
MAX_RESTORED_REST_SECONDS = max(max(COOLDOWN_SECONDS.values()), COOLDOWN_CAP_SECONDS)
#: Gap between the end of one request to a server and the start of the next.
SPACING_SECONDS = 1.0
#: How soon to ask again while another download holds the server.
BUSY_POLL_SECONDS = 0.25
#: Longest wait for a resting server before the download is given up.
MAX_REST_WAIT_SECONDS = 30.0
#: A dropped connection earns one more request to the same server, this much
#: later (only where the caller allows it: Qt 5 already re-sends by itself).
RESET_RETRY_DELAY_SECONDS = 5.0

# Why `try_acquire` or `ask_hosts` asks the caller to pause.
SPACING = "spacing"
IN_USE = "in_use"
RESTING = "resting"
RESET = "reset"

#: How a download ended when no server delivered it.
LOST = "lost"


def cooldown_seconds(kind: str, strikes: int) -> float:
    """Rest after `strikes` failures in a row, the last one of class `kind`."""
    base = COOLDOWN_SECONDS.get(kind, 0.0)
    if base <= 0 or strikes < 1:
        return 0.0
    doubling = 2.0 ** min(int(strikes) - 1, 16)
    return min(max(base, COOLDOWN_CAP_SECONDS), base * doubling)


def _number(value: object) -> Optional[float]:
    """A finite float from a stored value, or None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass
class _HostState:
    until: float = 0.0
    strikes: int = 0
    last_ok: Optional[float] = None
    last_end: Optional[float] = None
    in_use: bool = False
    kind: str = ""
    detail: str = ""
    answered_at: Optional[float] = None


class HostBoard:
    """Rests, ordering and pacing for a set of servers, behind one lock.

    `clock` measures intervals (monotonic seconds); `wall` is the calendar
    time (seconds since the epoch) used only to carry rests across sessions.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        spacing_s: float = SPACING_SECONDS,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._spacing = max(0.0, float(spacing_s))
        self._lock = threading.Lock()
        self._hosts: Dict[str, _HostState] = {}

    def _state(self, host: str) -> _HostState:
        state = self._hosts.get(host)
        if state is None:
            state = self._hosts[host] = _HostState()
        return state

    # -------------------------------------------------------------- health
    def remaining(self, host: str) -> float:
        """Seconds the server still rests; 0.0 when it may be asked."""
        with self._lock:
            return max(0.0, self._state(host).until - self._clock())

    def order(self, hosts: Sequence[str]) -> Tuple[str, ...]:
        """The servers that may be asked now, best first.

        `hosts` is the caller's order; a server with a success comes before
        one without, the most recent success first.
        """
        with self._lock:
            now = self._clock()
            ready = []
            for index, host in enumerate(dict.fromkeys(hosts)):
                state = self._state(host)
                if state.until > now:
                    continue
                recent = -state.last_ok if state.last_ok is not None else math.inf
                ready.append((recent, index, host))
            return tuple(host for _recent, _index, host in sorted(ready))

    def strike(self, host: str, kind: str, detail: str = "") -> float:
        """Record a failed answer; return the rest it earned (0 for none).

        Classes without a rest (an answer too big or out of memory, an
        invalid request) are remembered for messages but leave the server's
        health alone: the server answered properly.  A server timeout rests
        the server for its fixed time and is not counted as a strike.
        """
        with self._lock:
            now = self._clock()
            state = self._state(host)
            state.kind, state.detail, state.answered_at = kind, detail, now
            if kind in FLAT_REST_SECONDS:
                rest = FLAT_REST_SECONDS[kind]
                state.until = max(state.until, now + rest)
                return rest
            if kind not in COOLDOWN_SECONDS:
                return 0.0
            state.strikes += 1
            rest = cooldown_seconds(kind, state.strikes)
            state.until = max(state.until, now + rest)
            return rest

    def success(self, host: str) -> None:
        with self._lock:
            now = self._clock()
            state = self._state(host)
            state.until = 0.0
            state.strikes = 0
            state.last_ok = now
            state.kind, state.detail, state.answered_at = OK, "", now

    def last_answer(self, host: str) -> Optional[Tuple[str, str, float]]:
        """(class, detail, seconds ago) of the server's last answer, if any."""
        with self._lock:
            state = self._hosts.get(host)
            if state is None or state.answered_at is None:
                return None
            return state.kind, state.detail, max(0.0, self._clock() - state.answered_at)

    def strikes(self, host: str) -> int:
        with self._lock:
            return self._state(host).strikes

    def last_worked(self) -> str:
        """The server with the most recent success, or ""."""
        with self._lock:
            return self._last_worked_locked()

    def _last_worked_locked(self) -> str:
        best, best_at = "", -math.inf
        for host, state in self._hosts.items():
            if state.last_ok is not None and state.last_ok > best_at:
                best, best_at = host, state.last_ok
        return best

    # ---------------------------------------------------------- sessions
    def snapshot(self) -> Dict[str, object]:
        """Everything worth keeping across sessions, in wall-clock seconds.

        ``{"hosts": {host: {"rest_until", "strikes", "kind", "detail",
        "answered_at", "last_ok"}}, "last_worked": host}``; keys a server
        has nothing for are left out.
        """
        with self._lock:
            now, wall = self._clock(), self._wall()

            def to_wall(moment: float) -> float:
                return round(wall - (now - moment), 3)

            hosts: Dict[str, Dict[str, object]] = {}
            for host, state in self._hosts.items():
                entry: Dict[str, object] = {}
                if state.until > now:
                    entry["rest_until"] = round(wall + (state.until - now), 3)
                if state.strikes:
                    entry["strikes"] = int(state.strikes)
                if state.answered_at is not None and state.kind:
                    entry["kind"] = state.kind
                    entry["detail"] = state.detail
                    entry["answered_at"] = to_wall(state.answered_at)
                if state.last_ok is not None:
                    entry["last_ok"] = to_wall(state.last_ok)
                if entry:
                    hosts[host] = entry
            return {"hosts": hosts, "last_worked": self._last_worked_locked()}

    def restore(self, data: object) -> None:
        """Merge a :meth:`snapshot`, typically from an earlier session.

        A rest is kept if it is longer than the one on the board; the newer
        last answer (and its strikes) wins; the later success wins.  Broken
        entries are skipped, so stored garbage can never stop a download.
        """
        if not isinstance(data, dict):
            return
        hosts = data.get("hosts")
        if not isinstance(hosts, dict):
            hosts = {}
        with self._lock:
            now, wall = self._clock(), self._wall()

            def to_clock(moment: float) -> float:
                return now - max(0.0, wall - moment)

            for host, entry in hosts.items():
                if not isinstance(host, str) or not host or not isinstance(entry, dict):
                    continue
                state = self._state(host)
                rest_until = _number(entry.get("rest_until"))
                if rest_until is not None:
                    rest = min(rest_until - wall, MAX_RESTORED_REST_SECONDS)
                    if rest > 0:
                        state.until = max(state.until, now + rest)
                answered = _number(entry.get("answered_at"))
                if answered is not None:
                    moment = to_clock(answered)
                    if state.answered_at is None or moment > state.answered_at:
                        state.answered_at = moment
                        state.kind = str(entry.get("kind") or "")
                        state.detail = str(entry.get("detail") or "")
                        strikes = _number(entry.get("strikes"))
                        state.strikes = max(0, min(64, int(strikes))) if strikes else 0
                last_ok = _number(entry.get("last_ok"))
                if last_ok is not None:
                    moment = to_clock(last_ok)
                    if state.last_ok is None or moment > state.last_ok:
                        state.last_ok = moment
            last = data.get("last_worked")
            if isinstance(last, str) and last:
                state = self._state(last)
                if state.last_ok is None:
                    # It worked at some time not recorded: after every server
                    # with a known success, before every server without one.
                    known = [s.last_ok for s in self._hosts.values() if s.last_ok is not None]
                    state.last_ok = (min(known) if known else now) - 1.0

    # -------------------------------------------------------------- pacing
    def try_acquire(self, host: str) -> Tuple[float, str]:
        """Take the server's single request slot.

        Returns (0.0, "") when the slot is now held by the caller, who must
        call `release` afterwards; otherwise the seconds to wait before
        asking again and why: SPACING (the gap after the previous request)
        or IN_USE (another download is using the server).
        """
        with self._lock:
            state = self._state(host)
            if state.in_use:
                return BUSY_POLL_SECONDS, IN_USE
            if state.last_end is not None:
                gap = state.last_end + self._spacing - self._clock()
                if gap > 0:
                    return gap, SPACING
            state.in_use = True
            return 0.0, ""

    def release(self, host: str) -> None:
        with self._lock:
            state = self._state(host)
            state.in_use = False
            state.last_end = self._clock()

    def reset(self) -> None:
        """Forget every server (a test, or a fresh start)."""
        with self._lock:
            self._hosts.clear()


@dataclass(frozen=True)
class HostResult:
    """How the servers answered one download.

    `kind` is OK, a class from DENSE, BAD_QUERY or LOST; `answers` holds
    every (server, verdict) heard, in the order asked.
    """

    kind: str
    payload: Optional[dict] = None
    host: str = ""
    detail: str = ""
    answers: Tuple[Tuple[str, Verdict], ...] = ()


def ask_hosts(
    hosts: Sequence[str],
    attempt: Callable[[str], Verdict],
    board: HostBoard,
    pause: Callable[[float, str, str], None],
    notify: Optional[Callable[[str, Verdict], None]] = None,
    max_rest_wait_s: float = MAX_REST_WAIT_SECONDS,
    reset_delay_s: float = RESET_RETRY_DELAY_SECONDS,
    reset_retry: bool = True,
) -> HostResult:
    """Ask the servers for one download, politely.

    * At most one request per server; the same server is asked once more
      only after it dropped the connection, `reset_delay_s` later, and only
      when `reset_retry` is true.
    * Servers are taken in the board's order.  When every server left is
      resting, the download waits for the first one only if it is back
      within `max_rest_wait_s`; otherwise no other server is asked.
    * An answer that proves the area too heavy (out of memory, over the size
      limit) or an invalid request ends the download at once: the same
      request would fail the same way on every server.

    `attempt(host)` sends one request and returns its verdict; `pause(seconds,
    host, why)` waits (and raises to cancel); `notify(host, verdict)` is told
    when a failed server is left for the next one, after its rest is set.
    """
    pinned = tuple(dict.fromkeys(hosts))
    tried: List[str] = []
    answers: List[Tuple[str, Verdict]] = []

    def send(host: str) -> Verdict:
        while True:
            gap, why = board.try_acquire(host)
            if gap <= 0:
                break
            pause(gap, host, why)
        try:
            return attempt(host)
        finally:
            board.release(host)

    while True:
        ready = [host for host in board.order(pinned) if host not in tried]
        if not ready:
            untried = [host for host in pinned if host not in tried]
            if not untried:
                return HostResult(LOST, answers=tuple(answers))
            soonest = min(untried, key=board.remaining)
            rest = board.remaining(soonest)
            if rest > max_rest_wait_s:
                return HostResult(LOST, answers=tuple(answers))
            pause(rest, soonest, RESTING)
            continue
        host = ready[0]
        tried.append(host)
        verdict = send(host)
        if verdict.retry_same_host and reset_retry:
            pause(reset_delay_s, host, RESET)
            verdict = send(host)
        answers.append((host, verdict))
        if verdict.ok:
            board.success(host)
            return HostResult(OK, verdict.payload, host, verdict.detail, tuple(answers))
        board.strike(host, verdict.kind, verdict.detail)
        if verdict.kind in DENSE or verdict.kind == BAD_QUERY:
            return HostResult(verdict.kind, None, host, verdict.detail, tuple(answers))
        if notify is not None and any(item not in tried for item in pinned):
            notify(host, verdict)
