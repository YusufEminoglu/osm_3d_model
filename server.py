# -*- coding: utf-8 -*-
"""Tiny threaded local HTTP server for the bundled 3D viewer."""
from __future__ import annotations

import functools
import http.server
import mimetypes
import socket
import socketserver
import threading
from pathlib import Path


class QuietCorsHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


class ReusableTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Osm3dServer:
    def __init__(self, web_root: str, host: str = "127.0.0.1", start_port: int = 8120, end_port: int = 8139):
        self.web_root = Path(web_root)
        self.host = host
        self.start_port = start_port
        self.end_port = end_port
        self.port = None
        self._httpd = None
        self._thread = None

        mimetypes.add_type("application/json", ".geojson")
        mimetypes.add_type("image/tiff", ".tif")
        mimetypes.add_type("application/javascript", ".js")

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    @property
    def url(self) -> str:
        if self.port is None:
            return ""
        return f"http://{self.host}:{self.port}/src/"

    def start(self) -> str:
        if self.is_running:
            return self.url

        handler = functools.partial(QuietCorsHandler, directory=str(self.web_root))
        last_error = None
        for port in range(self.start_port, self.end_port + 1):
            if port_is_open(self.host, port):
                last_error = OSError(f"Port {port} is already in use")
                continue
            try:
                self._httpd = ReusableTcpServer((self.host, port), handler)
                self.port = port
                break
            except OSError as exc:
                last_error = exc

        if self._httpd is None:
            raise OSError(f"No free local port in {self.start_port}-{self.end_port}: {last_error}")

        self._thread = threading.Thread(target=self._httpd.serve_forever, name="Osm3dServer", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._httpd is None:
            return
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
