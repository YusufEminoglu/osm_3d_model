# -*- coding: utf-8 -*-
"""3D OSM Model - one-click OSM to interactive 3D city viewer for QGIS."""
from __future__ import annotations


def classFactory(iface):  # noqa: N802 (QGIS entry point name)
    from .main_plugin import Osm3dModelPlugin

    return Osm3dModelPlugin(iface)
