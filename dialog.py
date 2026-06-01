# -*- coding: utf-8 -*-
"""Single-button dialog for the 3D OSM Model plugin.

A compact, salmon-tinted ("yavru agzi") cockpit: pick an area, optionally add a
DEM, run the one-button export, and read back a live area estimate plus a
last-run summary. The heavy lifting lives in ``builder.py``; this file is only
the GUI and emits :pyattr:`runRequested` / :pyattr:`reopenRequested` /
:pyattr:`openFolderRequested` for ``main_plugin.py`` to handle.
"""
from __future__ import annotations

import os

from qgis.core import QgsMapLayerProxyModel, QgsSettings
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# QgsSettings key prefix for persisting the dialog's last-used options.
_SETTINGS_PREFIX = "osm_3d_model"

# Salmon-tinted grey identity, shared with the web viewer's --planx-accent tokens.
ACCENT = "#b8978a"
ACCENT_DARK = "#8a675c"
ACCENT_SOFT = "#d8bcb0"
PANEL_BG = "#f4e9e4"
CARD_BG = "#fffaf7"
CARD_BORDER = "#e6d6cf"
TEXT = "#4a3a34"
TEXT_MUTED = "#6b5750"
OK = "#3f6f54"
ERR = "#b91c1c"

_ICON = os.path.join(os.path.dirname(__file__), "icons", "icon_main.svg")
_RUN_LABEL = "Create OSM layers & export 3D viewer"


class Osm3dModelDialog(QDialog):
    runRequested = pyqtSignal(dict)
    reopenRequested = pyqtSignal()
    openFolderRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Set by the plugin so the dialog can show a live area estimate without
        # duplicating the geometry-resolution logic. Signature: probe(source) -> str.
        self.area_probe = None
        self.setWindowTitle("3D OSM Model")
        if os.path.exists(_ICON):
            self.setWindowIcon(QIcon(_ICON))
        self.setMinimumWidth(440)
        self._build_ui()
        self._restore_settings()

    # -- UI construction ----------------------------------------------------
    def _build_ui(self):
        self.setStyleSheet(self._stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._header())

        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 16)
        body.setSpacing(12)
        root.addLayout(body)

        body.addWidget(self._area_card())
        body.addWidget(self._options_card())

        self.run_button = QPushButton(_RUN_LABEL)
        self.run_button.setObjectName("runButton")
        self.run_button.setMinimumHeight(42)
        self.run_button.setCursor(Qt.PointingHandCursor)
        self.run_button.clicked.connect(self._emit_run)
        body.addWidget(self.run_button)

        self.reopen_button = QPushButton("Reopen last viewer")
        self.reopen_button.setObjectName("ghostButton")
        self.reopen_button.setCursor(Qt.PointingHandCursor)
        self.reopen_button.clicked.connect(self.reopenRequested.emit)
        body.addWidget(self.reopen_button)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        body.addWidget(self.status)

        self.summary_card = self._summary_card()
        self.summary_card.setVisible(False)
        body.addWidget(self.summary_card)

        footer = QLabel("PlanX 3D City engine · Dokuz Eylül University — City & Regional Planning")
        footer.setWordWrap(True)
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet("color: #9b837a; font-size: 10px; padding-top: 4px;")
        body.addWidget(footer)

    def _header(self) -> QWidget:
        head = QFrame()
        head.setObjectName("header")
        lay = QHBoxLayout(head)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        if os.path.exists(_ICON):
            icon = QLabel()
            icon.setPixmap(QIcon(_ICON).pixmap(40, 40))
            lay.addWidget(icon, 0, Qt.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel("3D OSM Model")
        title.setObjectName("headerTitle")
        subtitle = QLabel(
            "Pick an area, download OpenStreetMap, and open it as an interactive 3D city. "
            "The largest circle that fits your selection becomes the study area."
        )
        subtitle.setObjectName("headerSub")
        subtitle.setWordWrap(True)
        text.addWidget(title)
        text.addWidget(subtitle)
        lay.addLayout(text, 1)
        return head

    def _area_card(self) -> QWidget:
        card, lay = self._card("Study area")

        self.source_group = QButtonGroup(self)
        self.radio_canvas = QRadioButton("Current map view (canvas extent)")
        self.radio_selection = QRadioButton("Selected polygon feature(s) in the active layer")
        self.radio_canvas.setChecked(True)
        self.source_group.addButton(self.radio_canvas)
        self.source_group.addButton(self.radio_selection)
        self.radio_canvas.toggled.connect(self._refresh_area_summary)
        lay.addWidget(self.radio_canvas)
        lay.addWidget(self.radio_selection)

        self.area_readout = QLabel("—")
        self.area_readout.setObjectName("readout")
        self.area_readout.setWordWrap(True)
        lay.addWidget(self.area_readout)
        return card

    def _options_card(self) -> QWidget:
        card, lay = self._card("Options")

        ha_row = QHBoxLayout()
        ha_label = QLabel("Max study area")
        self.ha_spin = QDoubleSpinBox()
        self.ha_spin.setRange(1.0, 300.0)
        self.ha_spin.setSingleStep(10.0)
        self.ha_spin.setValue(150.0)
        self.ha_spin.setSuffix(" ha")
        ha_row.addWidget(ha_label)
        ha_row.addStretch(1)
        ha_row.addWidget(self.ha_spin)
        lay.addLayout(ha_row)

        self.auto_open_check = QCheckBox("Open the 3D viewer automatically after export")
        self.auto_open_check.setChecked(True)
        lay.addWidget(self.auto_open_check)

        self.adv_toggle = QToolButton()
        self.adv_toggle.setObjectName("advToggle")
        self.adv_toggle.setText("Advanced")
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.adv_toggle.setArrowType(Qt.RightArrow)
        self.adv_toggle.setCursor(Qt.PointingHandCursor)
        self.adv_toggle.toggled.connect(self._toggle_advanced)
        lay.addWidget(self.adv_toggle)

        self.adv_container = QWidget()
        adv = QVBoxLayout(self.adv_container)
        adv.setContentsMargins(0, 4, 0, 0)
        adv.setSpacing(8)
        dem_row = QHBoxLayout()
        dem_row.addWidget(QLabel("DEM (optional)"))
        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.dem_combo.setAllowEmptyLayer(True)
        self.dem_combo.setCurrentIndex(0)
        dem_row.addWidget(self.dem_combo, 1)
        adv.addLayout(dem_row)
        dem_hint = QLabel("Drapes the city over an elevation raster (clipped to the study circle).")
        dem_hint.setWordWrap(True)
        dem_hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        adv.addWidget(dem_hint)
        self.adv_container.setVisible(False)
        lay.addWidget(self.adv_container)
        return card

    def _summary_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("summaryCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        self.summary_title = QLabel("✓ Export ready")
        self.summary_title.setObjectName("summaryTitle")
        lay.addWidget(self.summary_title)

        self.summary_counts = QLabel("")
        self.summary_counts.setObjectName("summaryCounts")
        self.summary_counts.setWordWrap(True)
        lay.addWidget(self.summary_counts)

        self.summary_circle = QLabel("")
        self.summary_circle.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        lay.addWidget(self.summary_circle)

        btn_row = QHBoxLayout()
        open_viewer = QPushButton("Open viewer")
        open_viewer.setObjectName("ghostButton")
        open_viewer.setCursor(Qt.PointingHandCursor)
        open_viewer.clicked.connect(self.reopenRequested.emit)
        open_folder = QPushButton("Open data folder")
        open_folder.setObjectName("ghostButton")
        open_folder.setCursor(Qt.PointingHandCursor)
        open_folder.clicked.connect(self.openFolderRequested.emit)
        btn_row.addWidget(open_viewer)
        btn_row.addWidget(open_folder)
        lay.addLayout(btn_row)
        return card

    def _card(self, title: str):
        """Return (frame, content_layout) for a titled card."""
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        lay.addWidget(heading)
        return card, lay

    # -- Behaviour ----------------------------------------------------------
    def _toggle_advanced(self, checked: bool):
        self.adv_container.setVisible(checked)
        self.adv_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _emit_run(self):
        params = {
            "source": "selection" if self.radio_selection.isChecked() else "canvas",
            "max_ha": float(self.ha_spin.value()),
            "dem_layer": self.dem_combo.currentLayer(),
            "auto_open": self.auto_open_check.isChecked(),
        }
        self._save_settings()
        self.runRequested.emit(params)

    # -- Persistence (QgsSettings) -----------------------------------------
    def _restore_settings(self):
        s = QgsSettings()
        try:
            self.ha_spin.setValue(float(s.value(f"{_SETTINGS_PREFIX}/max_ha", self.ha_spin.value())))
        except (TypeError, ValueError):
            pass
        if str(s.value(f"{_SETTINGS_PREFIX}/source", "canvas")) == "selection":
            self.radio_selection.setChecked(True)
        else:
            self.radio_canvas.setChecked(True)
        auto = s.value(f"{_SETTINGS_PREFIX}/auto_open", True)
        self.auto_open_check.setChecked(str(auto).lower() not in ("false", "0", "no"))

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(f"{_SETTINGS_PREFIX}/max_ha", float(self.ha_spin.value()))
        s.setValue(f"{_SETTINGS_PREFIX}/source",
                   "selection" if self.radio_selection.isChecked() else "canvas")
        s.setValue(f"{_SETTINGS_PREFIX}/auto_open", self.auto_open_check.isChecked())

    def showEvent(self, event):  # noqa: N802 (Qt override)
        super().showEvent(event)
        self._refresh_area_summary()

    def _refresh_area_summary(self, *_):
        if not callable(self.area_probe):
            return
        source = "selection" if self.radio_selection.isChecked() else "canvas"
        try:
            text = self.area_probe(source)
        except Exception as exc:  # never let a probe error break the dialog
            text = str(exc)
        self.set_area_summary(text)

    def set_area_summary(self, text: str):
        self.area_readout.setText(text or "—")

    def set_status(self, text: str, error: bool = False, busy: bool = False):
        color = ERR if error else OK
        self.status.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.status.setText(text)
        self.run_button.setEnabled(not busy)
        self.run_button.setText("Working…" if busy else _RUN_LABEL)

    def show_summary(self, summary: dict, url: str = ""):
        counts = summary.get("counts", {}) or {}
        self.summary_counts.setText(
            f"<b>{counts.get('buildings', 0)}</b> buildings · "
            f"<b>{counts.get('roads', 0)}</b> roads · "
            f"<b>{counts.get('greens', 0)}</b> greens · "
            f"<b>{counts.get('trees', 0)}</b> trees"
        )
        circle = f"Study circle r = {summary.get('radius_m', '?')} m ({summary.get('area_ha', '?')} ha)"
        if url:
            circle += f"  ·  {url}"
        self.summary_circle.setText(circle)
        self.summary_card.setVisible(True)

    # -- Style --------------------------------------------------------------
    def _stylesheet(self) -> str:
        return f"""
        QDialog {{ background: {PANEL_BG}; }}
        QLabel {{ color: {TEXT}; }}
        #header {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 {ACCENT}, stop:1 {ACCENT_DARK});
        }}
        #headerTitle {{ color: #ffffff; font-size: 19px; font-weight: 800; }}
        #headerSub {{ color: rgba(255,255,255,0.9); font-size: 11px; }}
        #card, #summaryCard {{
            background: {CARD_BG};
            border: 1px solid {CARD_BORDER};
            border-radius: 10px;
        }}
        #summaryCard {{ border: 1px solid {ACCENT_SOFT}; background: #fff4ee; }}
        #cardTitle {{ font-weight: 700; color: {ACCENT_DARK}; font-size: 12px;
            text-transform: uppercase; letter-spacing: 0.04em; }}
        #summaryTitle {{ font-weight: 800; color: {OK}; font-size: 14px; }}
        #summaryCounts {{ font-size: 13px; color: {TEXT}; }}
        #readout {{
            background: {PANEL_BG};
            border: 1px solid {CARD_BORDER};
            border-radius: 6px;
            padding: 6px 9px;
            color: {ACCENT_DARK};
            font-weight: 600;
            font-size: 12px;
        }}
        QRadioButton {{ color: {TEXT}; padding: 2px 0; }}
        QDoubleSpinBox {{
            padding: 4px 6px; border: 1px solid {CARD_BORDER};
            border-radius: 6px; background: #ffffff; min-width: 92px;
        }}
        #advToggle {{ border: 0; color: {ACCENT_DARK}; font-weight: 700; padding: 2px; }}
        #advToggle:hover {{ color: {ACCENT}; }}
        QPushButton#runButton {{
            background: {ACCENT}; color: #ffffff; border: 0; border-radius: 9px;
            font-size: 14px; font-weight: 800;
        }}
        QPushButton#runButton:hover {{ background: {ACCENT_DARK}; }}
        QPushButton#runButton:disabled {{ background: #cbb8b0; color: #efe6e2; }}
        QPushButton#ghostButton {{
            background: transparent; color: {ACCENT_DARK};
            border: 1px solid {ACCENT_SOFT}; border-radius: 7px; padding: 7px 10px;
            font-weight: 600;
        }}
        QPushButton#ghostButton:hover {{ background: #f6e7e0; border-color: {ACCENT}; }}
        """
