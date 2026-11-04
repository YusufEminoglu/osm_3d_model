# -*- coding: utf-8 -*-
"""Tiny threaded local HTTP server for the bundled 3D viewer.

The viewer (``web/src``, ``web/assets``) is served from the plugin folder; the
URL ``/data/...`` is served from the folder chosen with
:meth:`Osm3dServer.set_data_dir` -- a run folder written by the builder, or the
bundled sample city (``web/data``) when none is set.

The server only listens on 127.0.0.1 and only answers requests addressed to
``127.0.0.1:<port>`` or ``localhost:<port>``: a web page that rebinds its own
domain name to 127.0.0.1 (DNS rebinding) sends its own name in the Host header
and is refused, so it cannot read the user's exported basemap or DEM.
Folders are never listed.
"""
from __future__ import annotations

import functools
import http.server
import mimetypes
import os
import posixpath
import socket
import socketserver
import threading
import urllib.parse
from http import HTTPStatus
from pathlib import Path
from typing import Optional

DATA_PREFIX = "data"
#: Where a request that resolves outside the served folder is sent: a name no
#: run or viewer file has, so the answer is a plain 404.
_REFUSED_NAME = ".refused-path"
_LOCAL_NAMES = ("127.0.0.1", "localhost")


class ServerUnavailable(RuntimeError):
    """The viewer server could not start; ``str(e)`` is the text for the user."""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serve the viewer quietly without exposing local exports cross-origin."""

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # Every viewer asset is same-origin.  Wildcard CORS allowed arbitrary web
        # pages to probe the local port and read exported basemaps/GeoJSON.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    # -- Host check (DNS rebinding) ------------------------------------------
    def _host_allowed(self) -> bool:
        host = str(self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        return host in {f"{name}:{port}" for name in _LOCAL_NAMES}

    def _refuse_foreign_host(self) -> bool:
        if self._host_allowed():
            return False
        self.send_error(HTTPStatus.FORBIDDEN, "This server only answers requests addressed to itself.")
        return True

    def do_GET(self):
        if self._refuse_foreign_host():
            return
        super().do_GET()

    def do_HEAD(self):
        if self._refuse_foreign_host():
            return
        super().do_HEAD()

    # -- paths ---------------------------------------------------------------
    def translate_path(self, path):
        """``/data/...`` -> the chosen data folder; everything else -> ``web/``."""
        clean = path.split("?", 1)[0].split("#", 1)[0]
        try:
            decoded = urllib.parse.unquote(clean, errors="surrogatepass")
        except UnicodeDecodeError:
            decoded = urllib.parse.unquote(clean)
        words = [word for word in posixpath.normpath(decoded).split("/") if word]
        owner = getattr(self.server, "owner", None)
        if words and words[0] == DATA_PREFIX and owner is not None:
            self.directory = str(owner.data_root)
        elif owner is not None:
            self.directory = str(owner.web_root)
        translated = super().translate_path(path)
        # A link or junction inside the served folder must not lead out of it.
        try:
            base = os.path.realpath(self.directory)
            real = os.path.realpath(translated)
            inside = os.path.commonpath([base, real]) == base
        except (OSError, ValueError):  # a NUL in the path, another drive
            inside = False
        return translated if inside else os.path.join(self.directory, _REFUSED_NAME)

    def send_head(self):
        try:
            return super().send_head()
        except ValueError:  # an embedded NUL byte that slipped through
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return None

    def list_directory(self, path):
        """Folders are never listed."""
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        return None


class ReusableTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    owner = None


class Osm3dServer:
    def __init__(self, web_root: str, host: str = "127.0.0.1", start_port: int = 8120, end_port: int = 8139):
        self.web_root = Path(web_root)
        self.host = host
        self.start_port = start_port
        self.end_port = end_port
        self.port = None
        self._httpd = None
        self._thread = None
        self._data_dir: Optional[Path] = None

        mimetypes.add_type("application/json", ".geojson")
        mimetypes.add_type("image/tiff", ".tif")
        mimetypes.add_type("image/webp", ".webp")
        mimetypes.add_type("application/javascript", ".js")
        mimetypes.add_type("model/gltf-binary", ".glb")

    # -- data folder ---------------------------------------------------------
    def set_data_dir(self, path: Optional[Path]) -> None:
        """Serve ``/data/...`` from ``path/data/...``; ``None`` -> the bundled sample.

        Takes effect for the next request, also while the server is running.
        """
        self._data_dir = Path(path) if path else None

    @property
    def data_dir(self) -> Optional[Path]:
        """The run folder ``/data`` is served from, or None for the bundled sample."""
        return self._data_dir

    @property
    def data_root(self) -> Path:
        """The folder whose ``data`` subfolder answers ``/data/...``."""
        return self._data_dir if self._data_dir is not None else self.web_root

    # -- lifecycle -----------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return all((
            self._httpd is not None,
            self._thread is not None,
            self._thread.is_alive() if self._thread is not None else False,
        ))

    @property
    def url(self) -> str:
        if self.port is None:
            return ""
        return f"http://{self.host}:{self.port}/src/"

    def start(self) -> str:
        """Start (or keep) the server and return the viewer URL.

        Raises :class:`ServerUnavailable` when every port in the range is busy.
        """
        if self.is_running:
            return self.url
        if self._httpd is not None:
            self.stop()

        handler = functools.partial(QuietHandler, directory=str(self.web_root))
        for port in range(self.start_port, self.end_port + 1):
            if port_is_open(self.host, port):
                continue
            try:
                httpd = ReusableTcpServer((self.host, port), handler)
            except OSError:
                continue
            httpd.owner = self
            self._httpd = httpd
            self.port = port
            break

        if self._httpd is None:
            raise ServerUnavailable(
                f"The 3D viewer could not start: every local port it can use "
                f"({self.start_port}-{self.end_port}) is taken by another program. "
                f"Close that program or restart QGIS, then open the viewer again."
            )

        self._thread = threading.Thread(target=self._httpd.serve_forever, name="Osm3dServer", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._httpd is None:
            return
        if self._thread and self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._httpd = None
        self.port = None


def port_is_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0
