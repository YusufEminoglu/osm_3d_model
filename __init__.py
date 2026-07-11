# -*- coding: utf-8 -*-
"""3D OSM Model - one-click OSM to interactive 3D city viewer for QGIS."""
from __future__ import annotations

from pathlib import Path


def _metadata_version() -> str:
    """Read the plugin version from QGIS' canonical metadata file."""
    try:
        for line in Path(__file__).with_name("metadata.txt").read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("version="):
                version = line.partition("=")[2].strip()
                if version:
                    return version
    except OSError:
        pass
    return "0.0.0"


PLUGIN_VERSION = _metadata_version()


def classFactory(iface):  # noqa: N802 (QGIS entry point name)
    from .main_plugin import Osm3dModelPlugin

    return Osm3dModelPlugin(iface)
