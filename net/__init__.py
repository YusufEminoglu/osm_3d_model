# -*- coding: utf-8 -*-
"""How the plugin talks to the public Overpass servers.

Pure Python, ported from 02AgentOSM 1.6.0 (``core/transport.py`` and
``core/mirrors.py``), which was proven against the 2026 Overpass rules:

* :mod:`.transport` reads what one request came to (HTTP status first, then
  the body) and words the message that closes a download that got nothing.
* :mod:`.hosts` remembers how each server is doing: a server that failed
  rests for a while that depends on what it answered, and each download asks
  every server at most once.

``osm_download`` owns the QGIS network stack and hands these modules the
plain facts of each request, so every rule here is testable without QGIS.
"""
