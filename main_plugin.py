# -*- coding: utf-8 -*-
"""3D OSM Model - QGIS plugin entry point."""
from __future__ import annotations

import contextlib
import math
import os
import time
import webbrowser
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeedback,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsTask,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QEventLoop, Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction, QApplication, QMessageBox

from . import builder, place_search
from . import server as server_module
from .server import Osm3dServer

# PyQt5 exposes this flag directly on QEventLoop, while PyQt6 nests it under
# ProcessEventsFlag.  QGIS 3 and QGIS 4 therefore need different enum paths.
_EXCLUDE_USER_INPUT_EVENTS = getattr(
    QEventLoop, "ProcessEventsFlag", QEventLoop
).ExcludeUserInputEvents
_POLYGON_GEOMETRY = getattr(getattr(QgsWkbTypes, "GeometryType", QgsWkbTypes), "PolygonGeometry")

TITLE = "3D OSM Model"
#: A map view wider than this is not a neighbourhood: Run refuses it instead
#: of building open sea out of a world view.
VIEW_LIMIT_M = 50_000.0
VIEW_HINT = "Zoom in to a neighbourhood, or search a place."
CANCELLED_TEXT = "Download cancelled."
#: The map outline follows the view this long after the last pan or zoom.
_PREVIEW_DELAY_MS = 250
#: A cancelled download that has not ended after this long is let go.
_CANCEL_GRACE_MS = 5000


def _view_size_m(rect: QgsRectangle, crs: QgsCoordinateReferenceSystem):
    """The larger of the map view's width and height in metres, or None.

    Measured on the ellipsoid in short pieces along the view's centre lines, so
    a view spanning the antimeridian or the whole world still measures as huge
    (one long geodesic would take the short way round and read as small).
    """
    if crs is None or not crs.isValid() or rect is None or rect.isEmpty():
        return None
    area = QgsDistanceArea()
    area.setSourceCrs(crs, QgsProject.instance().transformContext())
    area.setEllipsoid("WGS84")
    pieces = 24
    cx, cy = rect.center().x(), rect.center().y()
    dx, dy = rect.width() / pieces, rect.height() / pieces
    width = height = 0.0
    try:
        for i in range(pieces):
            x0 = rect.xMinimum() + i * dx
            y0 = rect.yMinimum() + i * dy
            width += area.measureLine(QgsPointXY(x0, cy), QgsPointXY(x0 + dx, cy))
            height += area.measureLine(QgsPointXY(cx, y0), QgsPointXY(cx, y0 + dy))
    except Exception:  # a view the transform cannot follow is no neighbourhood
        return None
    size = max(width, height)
    if not math.isfinite(size) or size <= 0:
        return None
    return size


def _first_sentence(text: str, limit: int = 140) -> str:
    """A short message-bar note: the first sentence of a longer explanation."""
    text = " ".join(str(text or "").split())
    for end in (". ", "? ", "! "):
        cut = text.find(end)
        if 0 < cut < limit:
            return text[:cut + 1]
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class _OverpassFetchTask(QgsTask):
    """Network-only task; QGIS geometry and layer work stays on the UI thread.

    Each step of the download is reported through ``statusChanged`` (emitted on
    the worker thread, delivered queued on the UI thread). Cancelling the task
    cancels its QgsFeedback, which aborts the transfer at once.
    """

    statusChanged = pyqtSignal(str)

    def __init__(self, bbox, use_cache, callback):
        can_cancel = getattr(getattr(QgsTask, "Flag", QgsTask), "CanCancel")
        super().__init__("3D OSM Model: download OpenStreetMap", can_cancel)
        self.bbox = tuple(float(value) for value in bbox)
        self.use_cache = bool(use_cache)
        self.callback = callback
        self.feedback = QgsFeedback()
        self.outcome = None
        self.error_text = ""
        self.cancelled = False

    def cancel(self):
        self.feedback.cancel()
        super().cancel()

    def _status(self, text):
        self.statusChanged.emit(str(text))

    def run(self):
        from . import osm_download

        if self.isCanceled() or self.feedback.isCanceled():
            self.cancelled = True
            self.error_text = CANCELLED_TEXT
            return False
        cancelled_error = getattr(osm_download, "OsmDownloadCancelled", None)
        try:
            self.outcome = osm_download.fetch_overpass_ex(
                *self.bbox,
                use_cache=self.use_cache,
                status=self._status,
                feedback=self.feedback,
            )
        except Exception as exc:
            if cancelled_error is not None and isinstance(exc, cancelled_error):
                self.cancelled = True
                self.error_text = str(exc) or CANCELLED_TEXT
            elif self.feedback.isCanceled() or self.isCanceled():
                self.cancelled = True
                self.error_text = CANCELLED_TEXT
            else:
                self.error_text = str(exc)
            return False
        if self.isCanceled():
            self.cancelled = True
            self.error_text = CANCELLED_TEXT
            return False
        return True

    def finished(self, result):
        callback, self.callback = self.callback, None
        if callback is not None:
            callback(self, bool(result))


class Osm3dModelPlugin:
    # Live OpenStreetMap tiles, added with one click so the study area can be
    # framed on an empty project. Braces are percent-encoded for the WMS/XYZ URI.
    OSM_BASEMAP_NAME = "OpenStreetMap"
    OSM_BASEMAP_URI = (
        "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
        "&zmax=19&zmin=0&crs=EPSG3857"
    )

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.web_root = os.path.join(self.plugin_dir, "web")
        self.action = None
        self.dialog = None
        self.server = Osm3dServer(self.web_root)
        self._running = False
        self._unloaded = False
        self._canvas_probe_connected = False
        self._task = None
        self._place_task = None
        self._retired_tasks = []
        self._pending_run = None
        self._last_request = None
        self._run_started = 0.0
        self._run_prepared = None
        self._band = None
        self._preview_timer = None

    # -- QGIS lifecycle -----------------------------------------------------
    def initGui(self):
        if self._unloaded or self.action is not None:
            return
        icon = QIcon(os.path.join(self.plugin_dir, "icons", "icon.png"))
        self.action = QAction(icon, TITLE, self.iface.mainWindow())
        self.action.setStatusTip("Download OSM for an area and open it as a 3D city")
        self.action.triggered.connect(self.show_dialog)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&3D OSM Model", self.action)

    def unload(self):
        if self._unloaded:
            return
        self._unloaded = True
        self._running = False

        for task in (self._task, self._place_task):
            if task is not None:
                self._retired_tasks.append(task)  # alive until QGIS lets go of it
                with contextlib.suppress(RuntimeError):
                    task.cancel()
        self._task = None
        self._place_task = None
        self._pending_run = None
        self._run_prepared = None

        timer, self._preview_timer = self._preview_timer, None
        if timer is not None:
            timer.stop()
            with contextlib.suppress(RuntimeError, TypeError):
                timer.timeout.disconnect(self._refresh_preview)
        self._disconnect_canvas_probe()
        self._remove_band()

        action, self.action = self.action, None
        dialog, self.dialog = self.dialog, None
        server, self.server = self.server, None
        iface, self.iface = self.iface, None

        if action is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                action.triggered.disconnect(self.show_dialog)
            if iface is not None:
                iface.removePluginMenu("&3D OSM Model", action)
                iface.removeToolBarIcon(action)
            action.deleteLater()

        if dialog is not None:
            for signal, slot in self._dialog_connections(dialog):
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect(slot)
            dialog.area_probe = None
            dialog.prepare_for_unload()
            dialog.close()
            dialog.deleteLater()

        if server is not None:
            server.stop()

    def _dialog_connections(self, dialog):
        return (
            (dialog.runRequested, self.run_export),
            (dialog.cancelRequested, self._cancel_run),
            (dialog.reopenRequested, self.reopen_viewer),
            (dialog.openFolderRequested, self._open_data_folder),
            (dialog.clearCacheRequested, self._clear_osm_cache),
            (dialog.deleteRunsRequested, self._delete_old_runs),
            (dialog.basemapRequested, self.add_osm_basemap),
            (dialog.freshRequested, self._download_fresh),
            (dialog.placeSearchRequested, self._search_place),
            (dialog.placeChosen, self._zoom_to_place),
            (dialog.visibilityChanged, self._on_dialog_visibility),
        )

    def show_dialog(self):
        """Show the dialog, during a run too: it carries the run's live status."""
        if self._unloaded or self.iface is None:
            return
        if self.dialog is None:
            from .dialog import Osm3dModelDialog

            self.dialog = Osm3dModelDialog(self.iface.mainWindow())
            for signal, slot in self._dialog_connections(self.dialog):
                signal.connect(slot)
            self.dialog.area_probe = self._area_summary
            self._refresh_last_run()
            self._connect_canvas_probe()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    # -- Geometry resolution ------------------------------------------------
    def _resolve_area(self, source: str):
        """Return (QgsGeometry, QgsCoordinateReferenceSystem) for the chosen source."""
        if source == "selection":
            layer = self.iface.activeLayer()
            if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != _POLYGON_GEOMETRY:
                raise ValueError("Select a polygon layer as the active layer, then select feature(s).")
            feats = [f.geometry() for f in layer.selectedFeatures() if f.hasGeometry() and not f.geometry().isEmpty()]
            if not feats:
                raise ValueError("No selected polygon features in the active layer.")
            combined = QgsGeometry.collectGeometry(feats) if len(feats) > 1 else QgsGeometry(feats[0])
            if combined.isEmpty():
                raise ValueError("Selected features have no usable geometry.")
            return combined, layer.crs()

        # Default: the current map view.
        canvas = self.iface.mapCanvas()
        rect = canvas.extent()
        if rect.isEmpty():
            raise ValueError("The map view is empty. Search a place, or zoom the map to your area first.")
        crs = canvas.mapSettings().destinationCrs()
        size = _view_size_m(rect, crs)
        if size is None:
            raise ValueError(f"The map view is too large to measure. {VIEW_HINT}")
        if size > VIEW_LIMIT_M:
            raise ValueError(
                f"The map view is about {size / 1000.0:,.0f} km across, far larger than a "
                f"3D model can show. {VIEW_HINT}"
            )
        return QgsGeometry.fromRect(rect), crs

    def _prepare(self, params: dict, area=None) -> dict:
        geom, crs = area if area is not None else self._resolve_area(params.get("source", "canvas"))
        return builder.prepare_study_area(
            geom, crs,
            max_ha=float(params.get("max_ha", 150.0)),
            shape=params.get("shape", "circle"),
        )

    # -- Main action --------------------------------------------------------
    def run_export(self, params: dict, area=None):
        """Start a run: measure the boundary here, download on a worker thread."""
        if self._unloaded or self._running:
            return
        self._running = True
        self._run_started = time.monotonic()
        params = dict(params or {})
        try:
            if area is None:
                area = self._resolve_area(params.get("source", "canvas"))
            geom, crs = area
            self._dialog_status("Measuring the study area…", busy=True)
            QApplication.processEvents(_EXCLUDE_USER_INPUT_EVENTS)
            prepared = self._prepare(params, (geom, crs))
            if self._unloaded:
                return
            self._run_prepared = prepared
            self._show_boundary(prepared)
            self._pending_run = (params, prepared)
            self._last_request = (dict(params), QgsGeometry(geom), QgsCoordinateReferenceSystem(crs))
            task = _OverpassFetchTask(
                prepared["wgs_bbox"], params.get("use_cache", True), self._on_osm_fetched,
            )
            self._task = task
            if self.dialog is not None:
                task.statusChanged.connect(self.dialog.show_progress, Qt.ConnectionType.QueuedConnection)
                self.dialog.set_cancel_enabled(True)
            self._dialog_status("Starting the OpenStreetMap download…", busy=True)
            QgsApplication.taskManager().addTask(task)
        except Exception as exc:
            task, self._task = self._task, None
            self._pending_run = None
            if task is not None:
                task.cancel()
            self._running = False
            if not self._unloaded:
                self._fail(str(exc), bar=False)

    def _download_fresh(self):
        """Build the last run's area again, ignoring the saved download."""
        if self._unloaded or self._running or self._last_request is None:
            return
        params, geom, crs = self._last_request
        params = dict(params)
        params["use_cache"] = False
        if self.dialog is not None:
            # The layers the user picks now, not the ones of the last run.
            params["dem_layer"] = self.dialog.dem_combo.currentLayer()
            params["basemap_layer"] = self.dialog.basemap_combo.currentLayer()
            self.dialog.set_status("Starting a fresh download…", busy=True)
        self.run_export(params, area=(QgsGeometry(geom), QgsCoordinateReferenceSystem(crs)))

    def _cancel_run(self):
        """Cancel button: stop the download; the task reports back within 2 s."""
        task = self._task
        if task is None:
            if self._running:
                return  # the build itself runs on this thread and cannot be stopped
            if self.dialog is not None:
                self.dialog.set_status(CANCELLED_TEXT, neutral=True)
            return
        if self.dialog is not None:
            self.dialog.set_cancel_enabled(False)
        task.cancel()
        QTimer.singleShot(_CANCEL_GRACE_MS, lambda: self._abandon_task(task))

    def _abandon_task(self, task):
        """A cancelled download still running after the grace period is let go."""
        if self._unloaded or task is not self._task:
            return
        self._task = None
        self._pending_run = None
        self._retired_tasks.append(task)  # keep it alive until it really ends
        # its late status lines must not land in the next run's status
        with contextlib.suppress(TypeError, RuntimeError):
            task.statusChanged.disconnect()
        self._running = False
        self._show_cancelled(CANCELLED_TEXT)

    def _on_osm_fetched(self, task: _OverpassFetchTask, success: bool):
        """Continue on the QGIS UI thread after the background HTTP request."""
        if task in self._retired_tasks:
            self._retired_tasks.remove(task)
        if task is not self._task:
            return
        self._task = None
        pending, self._pending_run = self._pending_run, None
        if self._unloaded:
            self._running = False
            return
        # Cancel pressed just as the answer arrived still means cancel.
        cancelled_late = False
        with contextlib.suppress(RuntimeError):
            cancelled_late = bool(task.isCanceled())
        if not success or task.outcome is None or cancelled_late:
            self._running = False
            if task.cancelled or cancelled_late:
                self._show_cancelled(task.error_text or CANCELLED_TEXT)
            else:
                self._fail(task.error_text or "The OpenStreetMap download stopped without an answer.")
            return
        if pending is None:
            self._running = False
            self._fail("The prepared export state was lost.")
            return
        params, prepared = pending
        self._finish_export(params, prepared, task.outcome)

    def _feedback(self, msg: str):
        if self._unloaded:
            return
        if self.dialog is not None:
            self.dialog.set_status(msg, busy=True)
        QApplication.processEvents(_EXCLUDE_USER_INPUT_EVENTS)

    def _finish_export(self, params: dict, prepared: dict, outcome):
        """Parse, clip and publish the downloaded model on the QGIS UI thread."""
        cursor_set = False
        try:
            if self.dialog is not None:
                self.dialog.set_cancel_enabled(False)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            cursor_set = True
            summary = builder.build_and_export(
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
                osm_payload=outcome.payload,
            )
            if self._unloaded or self.server is None:
                return
            summary = summary or {}
            notes = []
            url = ""
            run_dir = summary.get("run_dir")
            try:
                url = self._serve(Path(run_dir) if run_dir else None)
            except self._server_errors() as exc:
                # The server's text is neutral about the build, which did succeed.
                notes.append(f"{str(exc).strip()} The model was saved.")
            if url and params.get("auto_open", True):
                webbrowser.open(url)
            if self._unloaded:
                return
            self._report_success(summary, url, outcome, notes)
        except Exception as exc:
            if not self._unloaded:
                self._fail(str(exc))
        finally:
            if cursor_set:
                QApplication.restoreOverrideCursor()
            self._running = False
            if not self._unloaded:
                self._refresh_last_run()

    def _report_success(self, summary: dict, url: str, outcome, notes):
        counts = summary.get("counts", {}) or {}
        buildings = int(summary.get("buildings", counts.get("buildings", 0)) or 0)
        warnings = [w for w in (summary.get("warnings") or []) if str(w).strip()]
        problems = len(warnings) + len(notes)
        seconds = max(0, int(round(time.monotonic() - self._run_started)))
        fetch = {
            "from_cache": bool(getattr(outcome, "from_cache", False)),
            "saved_at": getattr(outcome, "saved_at", None),
            "host": getattr(outcome, "host", ""),
        }
        if self.dialog is not None:
            self.dialog.show_summary(summary, url, fetch=fetch, notes=notes)
            if problems:
                done = (f"Model ready in {seconds} s, with {problems} "
                        f"warning{'' if problems == 1 else 's'}; the summary lists "
                        f"{'it' if problems == 1 else 'them'}.")
            else:
                done = f"Model ready in {seconds} s."
            self.dialog.set_status(done, busy=False)
        if self.iface is not None:
            area = summary.get("area_ha", "?")
            if problems:
                self.iface.messageBar().pushWarning(
                    TITLE, f"3D model ready, with {problems} warning{'' if problems == 1 else 's'}; "
                    "the 3D OSM Model dialog lists them.")
            else:
                text = f"3D model ready: {buildings} buildings on {area} ha."
                if url:
                    text += f" Viewer: {url}"
                self.iface.messageBar().pushSuccess(TITLE, text)

    # -- Viewer and runs ------------------------------------------------------
    @staticmethod
    def _server_errors():
        unavailable = getattr(server_module, "ServerUnavailable", None)
        return (unavailable, OSError) if isinstance(unavailable, type) else (OSError,)

    def _serve(self, run_dir):
        """Serve ``run_dir`` (None: the bundled sample city) and return the viewer URL."""
        set_data_dir = getattr(self.server, "set_data_dir", None)
        if callable(set_data_dir):
            set_data_dir(Path(run_dir) if run_dir else None)
        return self.server.start()

    @staticmethod
    def _latest_run():
        latest = getattr(builder, "latest_run_dir", None)
        if not callable(latest):
            return None
        try:
            return latest()
        except Exception:  # no profile folder means no run to offer
            return None

    def _refresh_last_run(self):
        if self.dialog is not None:
            self.dialog.set_has_last_run(self._latest_run() is not None)

    def reopen_viewer(self):
        """Serve the newest run again, or the sample city before the first run."""
        if self._unloaded or self._running or self.server is None:
            return
        latest = self._latest_run()
        try:
            url = self._serve(latest)
        except self._server_errors() as exc:
            self._fail(str(exc))
            return
        webbrowser.open(url)
        if self.dialog:
            what = "the last run" if latest is not None else "the sample city"
            self.dialog.set_status(f"Opened {what} in your browser: {url}")

    def _open_data_folder(self):
        """Open the newest run's folder, or the folder that holds every run."""
        if self._unloaded or self._running:
            return
        try:
            folder = self._latest_run()
            if folder is None:
                folder = builder.runs_root()
        except Exception as exc:  # no profile folder: say so instead of raising into Qt
            self._fail(str(exc))
            return
        folder = str(folder)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(folder)):
            webbrowser.open("file://" + folder.replace("\\", "/"))

    @staticmethod
    def _older_run_count(keep) -> int:
        try:
            root = Path(builder.runs_root())
            keep_path = Path(keep).resolve() if keep is not None else None
            return sum(1 for child in root.iterdir()
                       if child.is_dir() and not child.name.startswith(".")
                       and child.resolve() != keep_path)
        except Exception:  # an unreadable runs folder holds nothing we can delete
            return 0

    def _delete_old_runs(self):
        """Delete every run folder except the newest, after the user confirms."""
        if self._unloaded or self._running:
            return
        latest = self._latest_run()
        older = self._older_run_count(latest)
        if older <= 0:
            if self.dialog is not None:
                self.dialog.set_status("There are no older runs to delete.", neutral=True)
            return
        kept = f" ({Path(latest).name})" if latest is not None else ""
        parent = self.dialog if self.dialog is not None else (
            self.iface.mainWindow() if self.iface is not None else None)
        answer = QMessageBox.question(
            parent, "Delete old runs",
            f"Delete {older} older run folder{'' if older == 1 else 's'}? "
            f"The newest run{kept} is kept. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            if self.dialog is not None:
                self.dialog.set_status("Nothing was deleted.", neutral=True)
            return
        try:
            removed = int(builder.delete_runs_except(latest) or 0)
        except Exception as exc:  # say what went wrong instead of raising into Qt
            self._fail(f"Could not delete the old runs: {exc}")
            return
        if self.dialog is not None:
            text = (f"Deleted {removed} old run folder{'' if removed == 1 else 's'}; "
                    "the newest run is kept.")
            left = self._older_run_count(latest)
            if left > 0:
                text += (f" {left} could not be deleted: a layer in the project or another "
                         f"program still uses {'it' if left == 1 else 'them'}.")
            self.dialog.set_status(text)
        self._refresh_last_run()

    # -- OSM basemap --------------------------------------------------------
    def add_osm_basemap(self):
        """Add (or reveal) the live OpenStreetMap tile layer in the project.

        It goes to the bottom of the layer tree so it sits under everything else,
        and a second click reuses the existing layer instead of stacking copies.
        When no basemap underlay has been chosen yet, it is also offered to the
        3D export as the ground texture. On an empty project QGIS zooms to a new
        first layer's extent, the whole world for tiles; a place the user has
        already framed is put back.
        """
        if self._unloaded or self._running:
            return
        project = QgsProject.instance()
        root = project.layerTreeRoot()

        for layer in project.mapLayers().values():
            if (
                isinstance(layer, QgsRasterLayer)
                and layer.name() == self.OSM_BASEMAP_NAME
                and "tile.openstreetmap.org" in layer.source()
            ):
                node = root.findLayer(layer.id())
                if node is not None:
                    node.setItemVisibilityChecked(True)
                self._basemap_added(layer, "OSM basemap is already on the map.")
                return

        layer = QgsRasterLayer(self.OSM_BASEMAP_URI, self.OSM_BASEMAP_NAME, "wms")
        if not layer.isValid():
            self._fail("Could not load the OSM basemap (no network, or tiles unavailable).")
            return

        framing = None
        if not project.mapLayers() and self.iface is not None:
            canvas = self.iface.mapCanvas()
            view = canvas.extent()
            if not view.isEmpty():
                framing = (QgsRectangle(view),
                           QgsCoordinateReferenceSystem(canvas.mapSettings().destinationCrs()))

        layer.setAttribution("© OpenStreetMap contributors")
        layer.setAttributionUrl("https://www.openstreetmap.org/copyright")
        project.addMapLayer(layer, False)
        root.addLayer(layer)  # appended == bottom of the tree, under other layers
        if framing is not None:
            self._restore_framing(framing)
            # QGIS sets up the view for a first layer a moment later.
            QTimer.singleShot(150, lambda: self._restore_framing(framing))
        self._basemap_added(
            layer, "Added the OpenStreetMap basemap. Tiles © OpenStreetMap contributors.",
        )

    def _restore_framing(self, framing):
        if self._unloaded or self.iface is None:
            return
        rect, crs = framing
        canvas = self.iface.mapCanvas()
        target = canvas.mapSettings().destinationCrs()
        try:
            if crs.isValid() and target.isValid() and crs != target:
                rect = QgsCoordinateTransform(crs, target, QgsProject.instance()).transformBoundingBox(rect)
        except Exception:  # keep QGIS's own view rather than raise into Qt
            return
        canvas.setExtent(rect)
        canvas.refresh()

    def _basemap_added(self, layer, message: str):
        if self.dialog is not None:
            # Only fill an empty choice, never override a basemap the user picked.
            if self.dialog.basemap_combo.currentLayer() is None:
                self.dialog.select_basemap_layer(layer)
                message += " It will also underlay the 3D model."
            self.dialog.set_status(message)
        if self.iface is not None:
            self.iface.messageBar().pushInfo(TITLE, message)

    # -- Place search -------------------------------------------------------
    def _search_place(self, text: str):
        if self._unloaded or self.dialog is None:
            return
        previous, self._place_task = self._place_task, None
        if previous is not None:
            self._retired_tasks.append(previous)
            previous.cancel()
        task = place_search.PlaceSearchTask(text, self._on_places_found)
        self._place_task = task
        self.dialog.set_place_searching(True)
        QgsApplication.taskManager().addTask(task)

    def _on_places_found(self, task):
        if task in self._retired_tasks:
            self._retired_tasks.remove(task)
        if task is not self._place_task:
            return
        self._place_task = None
        if self._unloaded or self.dialog is None:
            return
        self.dialog.show_place_results(task.places, task.error_text, query=task.text)

    def _zoom_to_place(self, place):
        """Centre the map on ``place``, wide enough for the largest model boundary."""
        if self._unloaded or self.iface is None or place is None:
            return
        canvas = self.iface.mapCanvas()
        max_ha = float(self.dialog.ha_spin.value()) if self.dialog is not None else 150.0
        # Half the view: the radius of a max-size circle, plus a margin around it.
        half = math.sqrt(max_ha * 10000.0 / math.pi) * 1.15
        lat = max(-85.0, min(85.0, float(place.lat)))
        lon = float(place.lon)
        dlat = half / 111_320.0
        dlon = half / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        target = canvas.mapSettings().destinationCrs()
        if not target.isValid():
            target = QgsCoordinateReferenceSystem("EPSG:3857")
            canvas.setDestinationCrs(target)
        rect = QgsRectangle(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        try:
            if target != wgs84:
                rect = QgsCoordinateTransform(wgs84, target, QgsProject.instance()).transformBoundingBox(rect)
        except Exception as exc:  # an exotic project CRS that cannot show the place
            if self.dialog is not None:
                self.dialog.set_place_note(f"Could not show {place.name} in this project's CRS: {exc}",
                                           error=True)
            return
        canvas.setExtent(rect)
        canvas.refresh()
        if self.dialog is not None:
            note = f"Centred on {place.name}."
            if not QgsProject.instance().mapLayers():
                note += " Add the OSM basemap to see the streets around it."
            self.dialog.set_place_note(note)
        self._refresh_preview()

    # -- Area readout and boundary outline -----------------------------------
    def _area_summary(self, params: dict) -> str:
        """The model boundary the current settings give, drawn on the map, in words."""
        if self._unloaded or self.iface is None:
            return "Plugin is unloading."
        try:
            prepared = self._prepare(params)
        except Exception as exc:
            self._show_boundary(None)
            return str(exc)
        self._show_boundary(prepared)
        return self._describe_boundary(prepared, params)

    def _describe_boundary(self, prepared: dict, params: dict) -> str:
        info = prepared.get("area_info", {}) or {}
        area = float(info.get("area_ha") or 0.0)
        shape = info.get("shape", "circle")
        if info.get("radius_m"):
            dims = f"inscribed circle, r = {float(info['radius_m']):,.0f} m"
        elif shape == "polygon":
            dims = "exact polygon"
        else:
            label = str(info.get("shape_label", "rectangle")).lower()
            dims = f"{label}, {float(info.get('width_m') or 0):,.0f} × {float(info.get('depth_m') or 0):,.0f} m"
        text = f"Model boundary: {dims} · {area:,.1f} ha"
        cap = float(params.get("max_ha", 150.0))
        if cap > 0 and area >= cap * 0.995:
            text += f" (the {cap:,.0f} ha limit; the rest of the area is left out)"
        if params.get("source") == "selection" and self.iface is not None:
            layer = self.iface.activeLayer()
            count = len(layer.selectedFeatures()) if isinstance(layer, QgsVectorLayer) else 0
            text += f" · from {count} selected feature{'' if count == 1 else 's'}"
        return text

    def _ensure_band(self, canvas):
        if self._band is None:
            band = QgsRubberBand(canvas, _POLYGON_GEOMETRY)
            stroke = QColor("#6e5047")
            fill = QColor("#b8978a")
            fill.setAlpha(50)
            band.setStrokeColor(stroke)
            band.setFillColor(fill)
            band.setWidth(2)
            self._band = band
        return self._band

    def _show_boundary(self, prepared):
        """Draw the model boundary on the map while the dialog is open."""
        if self.iface is None:
            return
        visible = self.dialog is not None and self.dialog.isVisible()
        if prepared is None or not visible:
            if self._band is not None:
                self._band.hide()
            return
        try:
            crs = QgsCoordinateReferenceSystem.fromEpsgId(int(prepared["epsg"]))
            band = self._ensure_band(self.iface.mapCanvas())
            band.setToGeometry(QgsGeometry(prepared["clip_utm"]), crs)
            band.show()
        except Exception as exc:  # an outline must never break the dialog
            warn_lvl = getattr(getattr(Qgis, "MessageLevel", Qgis), "Warning")
            QgsMessageLog.logMessage(f"Boundary outline failed: {exc}", TITLE, warn_lvl)

    def _remove_band(self):
        band, self._band = self._band, None
        if band is None:
            return
        with contextlib.suppress(RuntimeError):
            band.reset(_POLYGON_GEOMETRY)
            scene = band.scene()
            if scene is not None:
                scene.removeItem(band)

    def _on_dialog_visibility(self, visible: bool):
        """The outline is on the map only while the dialog is open."""
        if self._unloaded:
            return
        if not visible:
            self._remove_band()
        elif self._running and self._run_prepared is not None:
            self._show_boundary(self._run_prepared)  # the boundary being built

    def _connect_canvas_probe(self) -> None:
        """Keep the readout and the outline in step with the map (debounced).

        extentsChanged covers zoom and pan, destinationCrsChanged a project CRS
        switch, selectionChanged a new selection for the "selected features" area.
        """
        if self._canvas_probe_connected or self.iface is None:
            return
        canvas = self.iface.mapCanvas()
        if canvas is None:
            return
        if self._preview_timer is None:
            self._preview_timer = QTimer()
            self._preview_timer.setSingleShot(True)
            self._preview_timer.setInterval(_PREVIEW_DELAY_MS)
            self._preview_timer.timeout.connect(self._refresh_preview)
        for signal_name in ("extentsChanged", "destinationCrsChanged", "selectionChanged"):
            signal = getattr(canvas, signal_name, None)
            if signal is None:
                continue
            with contextlib.suppress(RuntimeError, TypeError):
                signal.connect(self._on_canvas_changed)
        self._canvas_probe_connected = True

    def _disconnect_canvas_probe(self) -> None:
        if not self._canvas_probe_connected or self.iface is None:
            self._canvas_probe_connected = False
            return
        canvas = self.iface.mapCanvas()
        if canvas is not None:
            for signal_name in ("extentsChanged", "destinationCrsChanged", "selectionChanged"):
                signal = getattr(canvas, signal_name, None)
                if signal is None:
                    continue
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect(self._on_canvas_changed)
        self._canvas_probe_connected = False

    def _on_canvas_changed(self, *_args) -> None:
        """Restart the debounce; never let a canvas signal raise into Qt."""
        if self._unloaded or self.dialog is None or self._preview_timer is None:
            return
        try:
            if self.dialog.isVisible():
                self._preview_timer.start()
        except RuntimeError:
            # The dialog's C++ object is gone; drop the connection.
            self._disconnect_canvas_probe()

    def _refresh_preview(self) -> None:
        if self._unloaded or self._running or self.dialog is None:
            return
        try:
            if not self.dialog.isVisible():
                return
            self.dialog.refresh_area_summary()
        except RuntimeError:
            self._disconnect_canvas_probe()
        except Exception as exc:  # a readout must never break the canvas
            warn_lvl = getattr(getattr(Qgis, "MessageLevel", Qgis), "Warning")
            QgsMessageLog.logMessage(
                f"Area readout refresh failed: {exc}", TITLE, warn_lvl
            )

    # -- Small helpers ---------------------------------------------------------
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
            self.iface.messageBar().pushInfo(TITLE, msg)

    def _dialog_status(self, text: str, busy: bool = False):
        if self.dialog is not None:
            self.dialog.set_status(text, busy=busy)

    def _show_cancelled(self, text: str):
        """A cancelled download is a neutral outcome, not an error."""
        if self.dialog is not None:
            self.dialog.set_status(text or CANCELLED_TEXT, neutral=True)
            if not self.dialog.isVisible() and self.iface is not None:
                self.iface.messageBar().pushInfo(TITLE, text or CANCELLED_TEXT)

    def _fail(self, text: str, bar: bool = True):
        """Say what went wrong in the dialog, plus a short note in the message bar.

        No modal box: the dialog keeps the whole sentence, and the run can be
        started again straight away.
        """
        if self._unloaded:
            return
        if self.dialog is not None:
            self.dialog.set_status(text, error=True, busy=False)
        if bar or self.dialog is None or not self.dialog.isVisible():
            if self.iface is not None:
                self.iface.messageBar().pushWarning(TITLE, _first_sentence(text))
