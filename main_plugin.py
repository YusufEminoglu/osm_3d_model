# -*- coding: utf-8 -*-
"""3D OSM Model - QGIS plugin entry point."""
from __future__ import annotations

import os
import webbrowser

from qgis.core import QgsDistanceArea, QgsGeometry, QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QApplication, QMessageBox

from .builder import build_and_export
from .server import Osm3dServer


class Osm3dModelPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.web_root = os.path.join(self.plugin_dir, "web")
        self.action = None
        self.dialog = None
        self.server = Osm3dServer(self.web_root)

    # -- QGIS lifecycle -----------------------------------------------------
    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, "icons", "icon.png"))
        self.action = QAction(icon, "3D OSM Model", self.iface.mainWindow())
        self.action.setStatusTip("Download OSM for an area and open it as a 3D city")
        self.action.triggered.connect(self.show_dialog)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&3D OSM Model", self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("&3D OSM Model", self.action)
            self.iface.removeToolBarIcon(self.action)
        self.server.stop()
        if self.dialog:
            self.dialog.close()
            self.dialog = None

    def show_dialog(self):
        if self.dialog is None:
            from .dialog import Osm3dModelDialog

            self.dialog = Osm3dModelDialog(self.iface.mainWindow())
            self.dialog.runRequested.connect(self.run_export)
            self.dialog.reopenRequested.connect(self.reopen_viewer)
            self.dialog.openFolderRequested.connect(self._open_data_folder)
            self.dialog.clearCacheRequested.connect(self._clear_osm_cache)
            self.dialog.area_probe = self._area_summary
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    # -- Geometry resolution ------------------------------------------------
    def _resolve_area(self, source: str):
        """Return (QgsGeometry, QgsCoordinateReferenceSystem) for the chosen source."""
        if source == "selection":
            layer = self.iface.activeLayer()
            if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != QgsWkbTypes.PolygonGeometry:
                raise ValueError("Select a polygon layer as the active layer, then select feature(s).")
            feats = [f.geometry() for f in layer.selectedFeatures() if f.hasGeometry() and not f.geometry().isEmpty()]
            if not feats:
                raise ValueError("No selected polygon features in the active layer.")
            combined = QgsGeometry.collectGeometry(feats) if len(feats) > 1 else QgsGeometry(feats[0])
            if combined.isEmpty():
                raise ValueError("Selected features have no usable geometry.")
            return combined, layer.crs()

        # Default: current map canvas extent.
        canvas = self.iface.mapCanvas()
        rect = canvas.extent()
        if rect.isEmpty():
            raise ValueError("The map view is empty. Zoom to your area first.")
        return QgsGeometry.fromRect(rect), canvas.mapSettings().destinationCrs()

    # -- Main action --------------------------------------------------------
    def run_export(self, params: dict):
        def feedback(msg: str):
            if self.dialog:
                self.dialog.set_status(msg, busy=True)
            QApplication.processEvents()

        try:
            geom, crs = self._resolve_area(params.get("source", "canvas"))
        except Exception as exc:
            self._fail(str(exc))
            return

        if self.dialog:
            self.dialog.set_status("Computing study area...", busy=True)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            summary = build_and_export(
                geom, crs, self.web_root,
                dem_layer=params.get("dem_layer"),
                basemap_layer=params.get("basemap_layer"),
                max_ha=float(params.get("max_ha", 300.0)),
                add_to_project=True,
                shape=params.get("shape", "circle"),
                theme=params.get("theme", "Plugin Tones"),
                feedback=feedback,
            )
            url = self.server.start()
            if params.get("auto_open", True):
                webbrowser.open(url)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self._fail(str(exc))
            return
        QApplication.restoreOverrideCursor()

        counts = summary.get("counts", {})
        furniture = (counts.get("busstops", 0) + counts.get("benches", 0)
                     + counts.get("lights", 0) + counts.get("trashbins", 0))
        waterways = counts.get("waterlines", 0) + counts.get("waterareas", 0)
        # Shape-aware size: circle reports a radius, rectangle/polygon a footprint.
        if summary.get("radius_m"):
            dim = f"r={summary['radius_m']} m"
        elif summary.get("width_m"):
            dim = f"{summary['width_m']}×{summary['depth_m']} m"
        else:
            dim = summary.get("shape_label", "study area")
        msg = (
            f"Done. {counts.get('buildings', 0)} buildings, {counts.get('roads', 0)} roads, "
            f"{counts.get('bikelanes', 0)} bike lanes, {counts.get('greens', 0)} greens, "
            f"{counts.get('trees', 0)} trees, {waterways} waterways, "
            f"{furniture} furniture | "
            f"{dim} ({summary.get('area_ha')} ha). Viewer: {url}"
        )
        if self.dialog:
            self.dialog.set_status(msg, busy=False)
            self.dialog.show_summary(summary, url)
        self.iface.messageBar().pushSuccess("3D OSM Model", msg)

    def reopen_viewer(self):
        try:
            url = self.server.url or self.server.start()
            webbrowser.open(url)
            if self.dialog:
                self.dialog.set_status(f"Viewer opened: {url}")
        except Exception as exc:
            self._fail(str(exc))

    def _area_summary(self, source: str) -> str:
        """Short, human-readable estimate of the chosen study area for the dialog."""
        try:
            geom, crs = self._resolve_area(source)
        except Exception as exc:
            return str(exc)
        da = QgsDistanceArea()
        da.setSourceCrs(crs, QgsProject.instance().transformContext())
        da.setEllipsoid("WGS84")
        try:
            area_ha = da.measureArea(geom) / 10000.0
        except Exception:
            area_ha = 0.0
        if source == "selection":
            layer = self.iface.activeLayer()
            count = len(layer.selectedFeatures()) if isinstance(layer, QgsVectorLayer) else 0
            return f"{count} selected feature(s) · ≈ {area_ha:,.0f} ha in extent"
        return f"Canvas extent · ≈ {area_ha:,.0f} ha"

    def _open_data_folder(self):
        folder = os.path.join(self.web_root, "data", "yerlesim")
        if not os.path.isdir(folder):
            folder = os.path.join(self.web_root, "data")
        try:
            os.startfile(folder)  # noqa: B606 - intended Windows shell open
        except Exception:
            webbrowser.open("file://" + folder.replace("\\", "/"))

    def _clear_osm_cache(self):
        from .osm_download import clear_cache

        removed, freed = clear_cache()
        mb = freed / (1024 * 1024)
        msg = (f"Cleared {removed} cached OSM file(s), freed {mb:.1f} MB."
               if removed else "OSM cache was already empty.")
        if self.dialog:
            self.dialog.set_status(msg)
        self.iface.messageBar().pushInfo("3D OSM Model", msg)

    def _fail(self, text: str):
        if self.dialog:
            self.dialog.set_status(text, error=True, busy=False)
        QMessageBox(QMessageBox.Icon.Critical, "3D OSM Model", text,
                    QMessageBox.StandardButton.Ok, self.iface.mainWindow()).exec()
