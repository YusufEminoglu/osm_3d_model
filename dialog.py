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
    QComboBox,
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

# Boundary-shape options (value, label). Values match builder.SHAPE_* and are
# persisted in QgsSettings. The model base extends 5 m beyond whichever shape,
# with softly rounded corners.
SHAPE_OPTIONS = (
    ("circle", "Inscribed circle — largest fit"),
    ("rounded", "Rounded rectangle"),
    ("extent", "Rectangle (extent)"),
    ("polygon", "Exact polygon / selection"),
)
_SHAPE_SHORT = {
    "circle": "inscribed circle",
    "rounded": "rounded rectangle",
    "extent": "rectangle",
    "polygon": "exact polygon",
}

# Easy colour themes for the 3D web output (value, label). Values match
# builder._THEMES and app.js COLOR_THEMES. These recolour the city content
# (buildings, roads, base, greens, roofs) — not the viewer's toolbar/panels.
THEME_OPTIONS = (
    ("Plugin Tones", "Plugin tones — salmon & grey"),
    ("Tinted Gray Teal", "Tinted gray + teal"),
    ("Teal & Salmon", "Teal + salmon"),
    ("Light Purple & Black", "Light purple + soft black"),
    ("Warm Sand & Slate", "Warm sand + slate"),
)
_THEME_TOOLTIP = (
    "Colour palette for the exported 3D city — buildings, roads, the base/island, "
    "greens and roofs. It does not change the viewer's toolbar or panels.\n"
    "Pick the look in QGIS; the browser viewer opens in that theme."
)
_SHAPE_TOOLTIP = (
    "How the study boundary is derived from your area:\n"
    "• Inscribed circle — the largest circle that fits inside it (classic look).\n"
    "• Rounded rectangle — its bounding box with rounded corners.\n"
    "• Rectangle (extent) — its bounding box.\n"
    "• Exact polygon / selection — the selected polygon as-is "
    "(falls back to the canvas rectangle).\n"
    "The model base extends 5 m beyond the boundary with softly rounded corners."
)


class Osm3dModelDialog(QDialog):
    runRequested = pyqtSignal(dict)
    reopenRequested = pyqtSignal()
    openFolderRequested = pyqtSignal()
    clearCacheRequested = pyqtSignal()

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
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_button.clicked.connect(self._emit_run)
        body.addWidget(self.run_button)

        self.reopen_button = QPushButton("Reopen last viewer")
        self.reopen_button.setObjectName("ghostButton")
        self.reopen_button.setCursor(Qt.CursorShape.PointingHandCursor)
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
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
            lay.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel("3D OSM Model")
        title.setObjectName("headerTitle")
        subtitle = QLabel(
            "Pick an area and a boundary shape, download OpenStreetMap, and open it as an "
            "interactive 3D city. The model sits on a base that extends 5 m beyond the boundary."
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

        shape_row = QHBoxLayout()
        shape_label = QLabel("Boundary shape")
        self.shape_combo = QComboBox()
        for value, label in SHAPE_OPTIONS:
            self.shape_combo.addItem(label, value)
        self.shape_combo.setToolTip(_SHAPE_TOOLTIP)
        self.shape_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.shape_combo.currentIndexChanged.connect(self._refresh_area_summary)
        shape_row.addWidget(shape_label)
        shape_row.addWidget(self.shape_combo, 1)
        lay.addLayout(shape_row)

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

        theme_row = QHBoxLayout()
        theme_label = QLabel("Web theme")
        self.theme_combo = QComboBox()
        for value, label in THEME_OPTIONS:
            self.theme_combo.addItem(label, value)
        self.theme_combo.setToolTip(_THEME_TOOLTIP)
        self.theme_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        theme_row.addWidget(theme_label)
        theme_row.addWidget(self.theme_combo, 1)
        lay.addLayout(theme_row)

        self.auto_open_check = QCheckBox("Open the 3D viewer automatically after export")
        self.auto_open_check.setChecked(True)
        lay.addWidget(self.auto_open_check)

        self.adv_toggle = QToolButton()
        self.adv_toggle.setObjectName("advToggle")
        self.adv_toggle.setText("Advanced")
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.adv_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.adv_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
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
        dem_hint = QLabel("Drapes the city over an elevation raster (clipped to the study boundary).")
        dem_hint.setWordWrap(True)
        dem_hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        adv.addWidget(dem_hint)

        bm_row = QHBoxLayout()
        bm_row.addWidget(QLabel("Basemap (optional)"))
        self.basemap_combo = QgsMapLayerComboBox()
        self.basemap_combo.setAllowEmptyLayer(True)
        self.basemap_combo.setCurrentIndex(0)
        bm_row.addWidget(self.basemap_combo, 1)
        adv.addLayout(bm_row)
        bm_hint = QLabel("Renders the chosen layer (raster, XYZ/satellite tiles, or vector) as a flat "
                         "underlay beneath the city. Style it live in the viewer's Basemap & Texture panel.")
        bm_hint.setWordWrap(True)
        bm_hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        adv.addWidget(bm_hint)

        self.clear_cache_button = QPushButton("Clear OSM download cache")
        self.clear_cache_button.setObjectName("ghostButton")
        self.clear_cache_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_cache_button.clicked.connect(self.clearCacheRequested.emit)
        adv.addWidget(self.clear_cache_button)
        cache_hint = QLabel("OSM responses are cached on disk for a week so re-running the same "
                            "area skips the download. Clear it to force a fresh fetch.")
        cache_hint.setWordWrap(True)
        cache_hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        adv.addWidget(cache_hint)

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
        open_viewer.setCursor(Qt.CursorShape.PointingHandCursor)
        open_viewer.clicked.connect(self.reopenRequested.emit)
        open_folder = QPushButton("Open data folder")
        open_folder.setObjectName("ghostButton")
        open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
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
        self.adv_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def _emit_run(self):
        params = {
            "source": "selection" if self.radio_selection.isChecked() else "canvas",
            "shape": self.shape_combo.currentData() or "circle",
            "theme": self.theme_combo.currentData() or "Plugin Tones",
            "max_ha": float(self.ha_spin.value()),
            "dem_layer": self.dem_combo.currentLayer(),
            "basemap_layer": self.basemap_combo.currentLayer(),
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
        saved_shape = str(s.value(f"{_SETTINGS_PREFIX}/shape", "circle"))
        shape_index = self.shape_combo.findData(saved_shape)
        if shape_index >= 0:
            self.shape_combo.setCurrentIndex(shape_index)
        saved_theme = str(s.value(f"{_SETTINGS_PREFIX}/theme", "Plugin Tones"))
        theme_index = self.theme_combo.findData(saved_theme)
        if theme_index >= 0:
            self.theme_combo.setCurrentIndex(theme_index)
        auto = s.value(f"{_SETTINGS_PREFIX}/auto_open", True)
        self.auto_open_check.setChecked(str(auto).lower() not in ("false", "0", "no"))

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(f"{_SETTINGS_PREFIX}/max_ha", float(self.ha_spin.value()))
        s.setValue(f"{_SETTINGS_PREFIX}/source",
                   "selection" if self.radio_selection.isChecked() else "canvas")
        s.setValue(f"{_SETTINGS_PREFIX}/shape", self.shape_combo.currentData() or "circle")
        s.setValue(f"{_SETTINGS_PREFIX}/theme", self.theme_combo.currentData() or "Plugin Tones")
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
        # Append the chosen boundary shape to a valid estimate (not to error text).
        if text and " ha" in text:
            shape = _SHAPE_SHORT.get(self.shape_combo.currentData(), "shape")
            text = f"{text}  ·  {shape} boundary"
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
        furniture = (counts.get("busstops", 0) + counts.get("benches", 0)
                     + counts.get("lights", 0) + counts.get("trashbins", 0))
        self.summary_counts.setText(
            f"<b>{counts.get('buildings', 0)}</b> buildings · "
            f"<b>{counts.get('roads', 0)}</b> roads · "
            f"<b>{counts.get('bikelanes', 0)}</b> bike lanes · "
            f"<b>{counts.get('greens', 0)}</b> greens · "
            f"<b>{counts.get('trees', 0)}</b> trees · "
            f"<b>{counts.get('waterlines', 0)}</b> waterways · "
            f"<b>{furniture}</b> furniture"
        )
        label = summary.get("shape_label", "Study area")
        area_ha = summary.get("area_ha", "?")
        if summary.get("radius_m"):
            line = f"{label}: r = {summary['radius_m']} m ({area_ha} ha)"
        elif summary.get("width_m"):
            line = f"{label}: {summary['width_m']} × {summary['depth_m']} m ({area_ha} ha)"
        else:
            line = f"{label} · {area_ha} ha"
        if url:
            line += f"  ·  {url}"
        self.summary_circle.setText(line)
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
        QComboBox {{
            padding: 4px 8px; border: 1px solid {CARD_BORDER};
            border-radius: 6px; background: #ffffff; min-width: 92px; color: {TEXT};
        }}
        QComboBox:hover {{ border-color: {ACCENT}; }}
        QComboBox::drop-down {{ border: 0; width: 18px; }}
        QComboBox QAbstractItemView {{
            background: #ffffff; color: {TEXT};
            selection-background-color: {ACCENT_SOFT}; selection-color: {ACCENT_DARK};
            border: 1px solid {CARD_BORDER}; outline: 0;
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
