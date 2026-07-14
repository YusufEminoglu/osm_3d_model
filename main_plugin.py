# -*- coding: utf-8 -*-
"""3D OSM Model - QGIS plugin entry point."""
from __future__ import annotations

import os
import webbrowser

from qgis.core import (
    QgsApplication,
    QgsDistanceArea,
    QgsGeometry,
    QgsProject,
    QgsTask,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QEventLoop, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QApplication, QMessageBox

from .builder import build_and_export, prepare_study_area
from .server import Osm3dServer


# PyQt5 exposes this flag directly on QEventLoop, while PyQt6 nests it under
# ProcessEventsFlag.  QGIS 3 and QGIS 4 therefore need different enum paths.
_EXCLUDE_USER_INPUT_EVENTS = getattr(
    QEventLoop, "ProcessEventsFlag", QEventLoop
).ExcludeUserInputEvents


class _OverpassFetchTask(QgsTask):
    """Network-only task; QGIS geometry and layer work stays on the UI thread."""

    def __init__(self, bbox, use_cache, callback):
        super().__init__("3D OSM Model: download OpenStreetMap", QgsTask.CanCancel)
        self.bbox = tuple(float(value) for value in bbox)
        self.use_cache = bool(use_cache)
        self.callback = callback
        self.payload = None
        self.error_text = ""

    def run(self):
        from .osm_download import fetch_overpass

        if self.isCanceled():
            self.error_text = "OSM download cancelled."
            return False
        try:
            self.payload = fetch_overpass(
                *self.bbox,
                use_cache=self.use_cache,
                cancel_check=self.isCanceled,
            )
            return not self.isCanceled()
        except Exception as exc:
            self.error_text = str(exc)
            return False

    def finished(self, result):
        callback, self.callback = self.callback, None
        if callback is not None:
            callback(self, bool(result))


class Osm3dModelPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.web_root = os.path.join(self.plugin_dir, "web")
        self.action = None
        self.dialog = None
        self.server = Osm3dServer(self.web_root)
        self._running = False
        self._unloaded = False
        self._task = None
        self._pending_run = None

    # -- QGIS lifecycle -----------------------------------------------------
    def initGui(self):
        if self._unloaded or self.action is not None:
            return
        icon = QIcon(os.path.join(self.plugin_dir, "icons", "icon.png"))
        self.action = QAction(icon, "3D OSM Model", self.iface.mainWindow())
        self.action.setStatusTip("Download OSM for an area and open it as a 3D city")
        self.action.triggered.connect(self.show_dialog)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&3D OSM Model", self.action)

    def unload(self):
        if self._unloaded:
            return
        self._unloaded = True
        self._running = False

        task, self._task = self._task, None
        self._pending_run = None
        if task is not None:
            task.cancel()

        action, self.action = self.action, None
        dialog, self.dialog = self.dialog, None
        server, self.server = self.server, None
        iface, self.iface = self.iface, None

        if action is not None:
            try:
                action.triggered.disconnect(self.show_dialog)
            except (RuntimeError, TypeError):
                pass
            if iface is not None:
                iface.removePluginMenu("&3D OSM Model", action)
                iface.removeToolBarIcon(action)
            action.deleteLater()

        if dialog is not None:
            for signal, slot in (
                (dialog.runRequested, self.run_export),
                (dialog.reopenRequested, self.reopen_viewer),
                (dialog.openFolderRequested, self._open_data_folder),
                (dialog.clearCacheRequested, self._clear_osm_cache),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            dialog.area_probe = None
            dialog.prepare_for_unload()
            dialog.close()
            dialog.deleteLater()

        if server is not None:
            server.stop()

    def show_dialog(self):
        if self._unloaded or self._running or self.iface is None:
            return
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
        if self._unloaded or self._running:
            return
        self._running = True
        cursor_set = False

        try:
            geom, crs = self._resolve_area(params.get("source", "canvas"))
            if self._unloaded:
                return
            if self.dialog is not None:
                self.dialog.set_status("Computing study area...", busy=True)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            cursor_set = True
            QApplication.processEvents(_EXCLUDE_USER_INPUT_EVENTS)

            prepared = prepare_study_area(
                geom, crs,
                max_ha=float(params.get("max_ha", 300.0)),
                shape=params.get("shape", "circle"),
            )
            if self._unloaded:
                return
            self._pending_run = (dict(params), prepared)
            task = _OverpassFetchTask(
                prepared["wgs_bbox"], params.get("use_cache", True),
                self._on_osm_fetched,
            )
            self._task = task
            if self.dialog is not None:
                self.dialog.set_status(
                    "Downloading OpenStreetMap data in the background...", busy=True,
                )
            QgsApplication.taskManager().addTask(task)
        except Exception as exc:
            task, self._task = self._task, None
            self._pending_run = None
            if task is not None:
                task.cancel()
            if not self._unloaded:
                self._fail(str(exc))
            self._running = False
        finally:
            if cursor_set:
                QApplication.restoreOverrideCursor()

    def _on_osm_fetched(self, task: _OverpassFetchTask, success: bool):
        """Continue on the QGIS UI thread after the background HTTP request."""
        if task is not self._task:
            return
        self._task = None
        pending, self._pending_run = self._pending_run, None
        if self._unloaded:
            self._running = False
            return
        if not success or task.payload is None:
            self._running = False
            self._fail(task.error_text or "The OpenStreetMap download was cancelled.")
            return
        if pending is None:
            self._running = False
            self._fail("The prepared export state was lost.")
            return
        params, prepared = pending
        self._finish_export(params, prepared, task.payload)

    def _feedback(self, msg: str):
        if self._unloaded:
            return
        if self.dialog is not None:
            self.dialog.set_status(msg, busy=True)
        QApplication.processEvents(_EXCLUDE_USER_INPUT_EVENTS)

    def _finish_export(self, params: dict, prepared: dict, osm_payload: dict):
        """Parse, clip and publish the downloaded model on the QGIS UI thread."""
        cursor_set = False
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            cursor_set = True
            summary = build_and_export(
                None, None, self.web_root,
                dem_layer=params.get("dem_layer"),
                basemap_layer=params.get("basemap_layer"),
                max_ha=float(params.get("max_ha", 300.0)),
                add_to_project=True,
                shape=params.get("shape", "circle"),
                theme=params.get("theme", "Plugin Tones"),
                use_cache=params.get("use_cache", True),
                feedback=self._feedback,
                prepared=prepared,
                osm_payload=osm_payload,
            )
            if self._unloaded or self.server is None:
                return
            url = self.server.start()
            if params.get("auto_open", True):
                webbrowser.open(url)
            if self._unloaded:
                return

            counts = summary.get("counts", {})
            furniture = sum(
                counts.get(key, 0)
                for key in ("busstops", "benches", "lights", "trashbins")
            )
            waterways = counts.get("waterlines", 0) + counts.get("waterareas", 0)
            if summary.get("radius_m"):
                dim = f"r={summary['radius_m']} m"
            elif summary.get("width_m"):
                dim = f"{summary['width_m']}×{summary['depth_m']} m"
            else:
                dim = summary.get("shape_label", "study area")
            msg = (
                f"Done. {counts.get('buildings', 0)} buildings, "
                f"{counts.get('roads', 0)} roads, "
                f"{counts.get('bikelanes', 0)} bike lanes, "
                f"{counts.get('greens', 0)} greens, "
                f"{counts.get('trees', 0)} trees, {waterways} waterways, "
                f"{furniture} furniture | "
                f"{dim} ({summary.get('area_ha')} ha). Viewer: {url}"
            )
            if self.dialog is not None:
                self.dialog.set_status(msg, busy=False)
                self.dialog.show_summary(summary, url)
            if self.iface is not None:
                self.iface.messageBar().pushSuccess("3D OSM Model", msg)
        except Exception as exc:
            if not self._unloaded:
                self._fail(str(exc))
        finally:
            if cursor_set:
                QApplication.restoreOverrideCursor()
            self._running = False

    def reopen_viewer(self):
        if self._unloaded or self._running or self.server is None:
            return
        try:
            url = self.server.url or self.server.start()
            webbrowser.open(url)
            if self.dialog:
                self.dialog.set_status(f"Viewer opened: {url}")
        except Exception as exc:
            self._fail(str(exc))

    def _area_summary(self, source: str) -> str:
        """Short, human-readable estimate of the chosen study area for the dialog."""
        if self._unloaded or self.iface is None:
            return "Plugin is unloading."
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
        if self._unloaded or self._running:
            return
        folder = os.path.join(self.web_root, "data", "yerlesim")
        if not os.path.isdir(folder):
            folder = os.path.join(self.web_root, "data")
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(folder)):
            webbrowser.open("file://" + folder.replace("\\", "/"))

    def _clear_osm_cache(self):
        if self._unloaded or self._running:
            return
        from .osm_download import clear_cache

        removed, freed = clear_cache()
        mb = freed / (1024 * 1024)
        msg = (f"Cleared {removed} cached OSM file(s), freed {mb:.1f} MB."
               if removed else "OSM cache was already empty.")
        if self.dialog:
            self.dialog.set_status(msg)
        if self.iface is not None:
            self.iface.messageBar().pushInfo("3D OSM Model", msg)

    def _fail(self, text: str):
        if self._unloaded:
            return
        if self.dialog is not None:
            self.dialog.set_status(text, error=True, busy=False)
        parent = self.iface.mainWindow() if self.iface is not None else None
        QMessageBox(QMessageBox.Icon.Critical, "3D OSM Model", text,
                    QMessageBox.StandardButton.Ok, parent).exec()
