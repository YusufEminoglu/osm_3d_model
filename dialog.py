# -*- coding: utf-8 -*-
"""The 3D OSM Model dialog.

A compact, salmon-tinted ("yavru agzi") cockpit: find a place or frame one on
the map, read the model boundary that gives (it is drawn on the map too), run
the one-button export, follow the download while it runs and stop it, and read
back what was built and what was not. The heavy lifting lives in
``builder.py`` and ``main_plugin.py``; this file is only the GUI and emits
signals for ``main_plugin.py`` to handle.

Colours: every text colour here reads at 4.5:1 or more on the surface it is
drawn on. Check boxes, radio buttons and the spin box get colour only, never a
background or border in the stylesheet (Qt 6 then draws their indicators
blank, TRAPS 3.7); where a QGIS UI theme styles the spin box, its own text and
field colours are left to agree with each other.
"""
from __future__ import annotations

import html
import os
import re
import time

from qgis.core import QgsMapLayerProxyModel, QgsSettings
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from qgis.PyQt.QtGui import QColor, QIcon, QPalette
from qgis.PyQt.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# QgsSettings key prefix for persisting the dialog's last-used options.
_SETTINGS_PREFIX = "osm_3d_model"

# Salmon-tinted grey identity, shared with the web viewer's --planx-accent tokens.
ACCENT = "#b8978a"        # decoration only: borders, the boundary fill on the map
ACCENT_DARK = "#8a675c"   # the Run button (white text 5.0:1), the header
ACCENT_DEEP = "#6e5047"   # accent text on the light surfaces (6.1:1 or more)
ACCENT_SOFT = "#d8bcb0"
PANEL_BG = "#f4e9e4"
CARD_BG = "#fffaf7"
CARD_BORDER = "#e6d6cf"
TEXT = "#4a3a34"
TEXT_MUTED = "#6b5750"
OK = "#3f6f54"
ERR = "#b91c1c"
WARN = "#8a4b00"
RUN_BG = ACCENT_DARK
RUN_BG_HOVER = ACCENT_DEEP
RUN_FG = "#ffffff"
RUN_BG_BUSY = "#e3d5cf"   # "Working…" stays readable: TEXT on it is 7.3:1

_ICON = os.path.join(os.path.dirname(__file__), "icons", "icon.png")
# "&&" renders as a literal ampersand; a single "&" would be eaten as a mnemonic.
_RUN_LABEL = "Create OSM layers && export 3D viewer"
_BUSY_LABEL = "Working…"
REOPEN_LABEL = "Reopen last viewer"
SAMPLE_LABEL = "Open sample city"

# Live OpenStreetMap tiles, mirrored in main_plugin.Osm3dModelPlugin.
_BASEMAP_TOOLTIP = (
    "Adds the live OpenStreetMap tile layer to your QGIS project so you can see "
    "and frame the study area before exporting.\n"
    "It is placed under your other layers and reused if it is already there.\n"
    "Tiles © OpenStreetMap contributors (ODbL)."
)

# Boundary-shape options (value, label). Values match builder.SHAPE_* and are
# persisted in QgsSettings. The model base extends 5 m beyond whichever shape,
# with softly rounded corners.
SHAPE_OPTIONS = (
    ("circle", "Inscribed circle — largest fit"),
    ("rounded", "Rounded rectangle"),
    ("extent", "Rectangle (extent)"),
    ("polygon", "Exact polygon / selection"),
)

# Easy colour themes for the native QGIS layers and 3D web output. Values match
# builder._THEMES and app.js COLOR_THEMES. These recolour the city content
# (buildings, roads, base, greens, roofs) — not the viewer's toolbar/panels.
THEME_OPTIONS = (
    ("Editorial Paper", "Editorial paper — warm & elegant"),
    ("Plugin Tones", "Plugin tones — salmon & grey"),
    ("Tinted Gray Teal", "Tinted gray + teal"),
    ("Teal & Salmon", "Teal + salmon"),
    ("Light Purple & Black", "Light purple + soft black"),
    ("Warm Sand & Slate", "Warm sand + slate"),
    ("Anime", "Anime — bright cel pastels"),
    ("Cartoon", "Cartoon — bold primaries"),
    ("Pixar", "Pixar — warm & friendly"),
    ("Futuristic City", "Futuristic — neon & glass"),
    ("Classic Era", "Classic era — vintage sepia"),
)
_DEFAULT_THEME = THEME_OPTIONS[0][0]
_THEME_TOOLTIP = (
    "Coordinated palette for both the native QGIS layers and exported 3D city — "
    "buildings, roads, the base/island, greens, water and roofs.\n"
    "Editorial Paper is the warm, legible default shared with OSM Quick 3D."
)
_SHAPE_TOOLTIP = (
    "How the study boundary is derived from your area:\n"
    "• Inscribed circle — the largest circle that fits inside it (classic look).\n"
    "• Rounded rectangle — its bounding box with rounded corners.\n"
    "• Rectangle (extent) — its bounding box.\n"
    "• Exact polygon / selection — the selected polygon as-is "
    "(the map view rectangle when the map view is the area).\n"
    "The model base extends 5 m beyond the boundary with softly rounded corners."
)
_READOUT_TOOLTIP = (
    "The boundary the export will use: its shape and size in hectares.\n"
    "It is drawn on the map while this dialog is open."
)
_PLACE_TOOLTIP = (
    "Type a town, a neighbourhood or an address and press Enter.\n"
    "Pick a result to centre the model on it.\n"
    "Place search: QGIS's Nominatim service. Names © OpenStreetMap contributors."
)

# A UTM code in a builder message reads as jargon; say which zone it is instead.
_UTM_CODE = re.compile(r"\bEPSG:(32[67])(\d\d)\b")


def plain_crs(text: str) -> str:
    """Replace a bare UTM EPSG code ("EPSG:32635") with "UTM zone 35N"."""
    def zone(match):
        hemisphere = "N" if match.group(1) == "326" else "S"
        return f"UTM zone {int(match.group(2))}{hemisphere}"

    return _UTM_CODE.sub(zone, str(text or ""))


def utm_zone_label(epsg) -> str:
    """'UTM zone 35N' for 32635; '' for anything that is not a UTM code."""
    try:
        code = int(epsg)
    except (TypeError, ValueError):
        return ""
    if 32601 <= code <= 32660:
        return f"UTM zone {code - 32600}N"
    if 32701 <= code <= 32760:
        return f"UTM zone {code - 32700}S"
    return ""


def age_text(seconds: float) -> str:
    """'3 hours' for 10,800 s: how old a saved download is, in words."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "less than a minute"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return "less than a minute"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


class Osm3dModelDialog(QDialog):
    runRequested = pyqtSignal(dict)
    cancelRequested = pyqtSignal()
    reopenRequested = pyqtSignal()
    openFolderRequested = pyqtSignal()
    clearCacheRequested = pyqtSignal()
    deleteRunsRequested = pyqtSignal()
    basemapRequested = pyqtSignal()
    freshRequested = pyqtSignal()
    placeSearchRequested = pyqtSignal(str)
    placeChosen = pyqtSignal(object)
    visibilityChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._cancelling = False
        self._ever_shown = False
        self._status_text = ""
        self._status_kind = "info"
        self._wait_started = None
        self._places = []
        # Set by the plugin so the dialog can describe the model boundary without
        # duplicating the geometry logic. Signature: probe(params: dict) -> str.
        self.area_probe = None
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._render_status)
        self.setWindowTitle("3D OSM Model")
        if os.path.exists(_ICON):
            self.setWindowIcon(QIcon(_ICON))
        self.setMinimumWidth(440)
        self.setMinimumHeight(340)
        self.setSizeGripEnabled(True)
        self._apply_palette()
        self._build_ui()
        self._restore_settings()
        self._fit_to_contents()

    def _apply_palette(self):
        """Pin the light colour roles the card design assumes.

        The stylesheet forces light backgrounds, but text, spin-box and check-box
        foregrounds come from the inherited application palette. Under QGIS 4/Qt 6
        that palette can be light-on-light, which made the "Open the 3D viewer
        automatically" label and the "Max study area" value unreadable.
        """
        pal = self.palette()
        roles = QPalette.ColorRole
        groups = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive)
        for group in groups:
            pal.setColor(group, roles.Window, QColor(PANEL_BG))
            pal.setColor(group, roles.WindowText, QColor(TEXT))
            pal.setColor(group, roles.Base, QColor("#ffffff"))
            pal.setColor(group, roles.AlternateBase, QColor(CARD_BG))
            pal.setColor(group, roles.Text, QColor(TEXT))
            pal.setColor(group, roles.Button, QColor(CARD_BG))
            pal.setColor(group, roles.ButtonText, QColor(TEXT))
            pal.setColor(group, roles.Highlight, QColor(ACCENT_SOFT))
            pal.setColor(group, roles.HighlightedText, QColor(TEXT))
            pal.setColor(group, roles.ToolTipBase, QColor(CARD_BG))
            pal.setColor(group, roles.ToolTipText, QColor(TEXT))
            pal.setColor(group, roles.PlaceholderText, QColor(TEXT_MUTED))
        disabled = QPalette.ColorGroup.Disabled
        for role in (roles.WindowText, roles.Text, roles.ButtonText):
            pal.setColor(disabled, role, QColor(TEXT_MUTED))
        pal.setColor(disabled, roles.Base, QColor("#f7efec"))
        pal.setColor(disabled, roles.Button, QColor("#f7efec"))
        self.setPalette(pal)

    # -- UI construction ----------------------------------------------------
    def _build_ui(self):
        self.setStyleSheet(self._stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header_widget = self._header()
        root.addWidget(self.header_widget)

        # The cards scroll; the footer with the status line, Run and Cancel does
        # not, so Run stays on screen on a short display with Advanced open.
        self.scroll = QScrollArea()
        self.scroll.setObjectName("bodyScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll, 1)

        self.body_widget = QWidget()
        self.body_widget.setObjectName("body")
        self.body_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        body = QVBoxLayout(self.body_widget)
        body.setContentsMargins(16, 14, 16, 16)
        body.setSpacing(12)
        self.scroll.setWidget(self.body_widget)

        body.addWidget(self._area_card())
        body.addWidget(self._options_card())
        self.summary_card = self._summary_card()
        self.summary_card.setVisible(False)
        body.addWidget(self.summary_card)
        body.addStretch(1)

        self.footer_widget = self._footer()
        root.addWidget(self.footer_widget)

        # Enter in the search box searches; it must never press another button.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

    def _header(self) -> QWidget:
        head = QFrame()
        head.setObjectName("header")
        lay = QHBoxLayout(head)
        lay.setContentsMargins(16, 12, 16, 12)
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
            "Search a place or frame it on the map, then open its OpenStreetMap buildings, "
            "streets, water and trees as an interactive 3D city."
        )
        subtitle.setObjectName("headerSub")
        subtitle.setWordWrap(True)
        text.addWidget(title)
        text.addWidget(subtitle)
        lay.addLayout(text, 1)
        return head

    def _footer(self) -> QWidget:
        foot = QFrame()
        foot.setObjectName("footer")
        foot.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(foot)
        lay.setContentsMargins(16, 10, 16, 12)
        lay.setSpacing(8)

        self.status = QLabel("")
        self.status.setObjectName("statusLine")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setVisible(False)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.run_button = QPushButton(_RUN_LABEL)
        self.run_button.setObjectName("runButton")
        self.run_button.setMinimumHeight(42)
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_button.clicked.connect(self._emit_run)
        row.addWidget(self.run_button, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setMinimumHeight(42)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setToolTip("Stop the download. Nothing is added to the project.")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setVisible(False)
        row.addWidget(self.cancel_button)
        lay.addLayout(row)

        self.reopen_button = QPushButton(SAMPLE_LABEL)
        self.reopen_button.setObjectName("ghostButton")
        self.reopen_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reopen_button.clicked.connect(self.reopenRequested.emit)
        lay.addWidget(self.reopen_button)
        self.set_has_last_run(False)
        return foot

    def _area_card(self) -> QWidget:
        card, lay = self._card("Study area")

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_label = QLabel("Find a place")
        search_label.setToolTip(_PLACE_TOOLTIP)
        search_row.addWidget(search_label)
        self.place_edit = QLineEdit()
        self.place_edit.setObjectName("placeEdit")
        self.place_edit.setPlaceholderText("e.g. Alsancak, Izmir")
        self.place_edit.setToolTip(_PLACE_TOOLTIP)
        self.place_edit.setClearButtonEnabled(True)
        self.place_edit.returnPressed.connect(self._search)
        self.place_button = QPushButton("Search")
        self.place_button.setObjectName("ghostButton")
        self.place_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.place_button.setToolTip(_PLACE_TOOLTIP)
        self.place_button.clicked.connect(self._search)
        search_row.addWidget(self.place_edit, 1)
        search_row.addWidget(self.place_button)
        lay.addLayout(search_row)

        self.place_list = QListWidget()
        self.place_list.setObjectName("placeResults")
        self.place_list.setVisible(False)
        self.place_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.place_list.itemClicked.connect(self._pick_place)
        self.place_list.itemActivated.connect(self._pick_place)
        lay.addWidget(self.place_list)

        self.place_note = QLabel("")
        self.place_note.setObjectName("hint")
        self.place_note.setWordWrap(True)
        self.place_note.setVisible(False)
        lay.addWidget(self.place_note)

        self.source_group = QButtonGroup(self)
        self.radio_canvas = QRadioButton("Current map view")
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
        self.area_readout.setToolTip(_READOUT_TOOLTIP)
        lay.addWidget(self.area_readout)

        self.basemap_button = QPushButton("Add OSM basemap to the map")
        self.basemap_button.setObjectName("ghostButton")
        self.basemap_button.setToolTip(_BASEMAP_TOOLTIP)
        self.basemap_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.basemap_button.clicked.connect(self.basemapRequested.emit)
        lay.addWidget(self.basemap_button)
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
        self.ha_spin.valueChanged.connect(self._refresh_area_summary)
        ha_row.addWidget(ha_label)
        ha_row.addStretch(1)
        ha_row.addWidget(self.ha_spin)
        lay.addLayout(ha_row)

        theme_row = QHBoxLayout()
        theme_label = QLabel("Map & web theme")
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
        self.adv_toggle.setAutoRaise(True)
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
        self.dem_combo.setFilters(
            getattr(getattr(QgsMapLayerProxyModel, "Filter", QgsMapLayerProxyModel), "RasterLayer")
        )
        self.dem_combo.setAllowEmptyLayer(True)
        self.dem_combo.setCurrentIndex(0)
        dem_row.addWidget(self.dem_combo, 1)
        adv.addLayout(dem_row)
        adv.addWidget(self._hint("Drapes the city over an elevation raster (clipped to the study boundary)."))

        bm_row = QHBoxLayout()
        bm_row.addWidget(QLabel("Basemap (optional)"))
        self.basemap_combo = QgsMapLayerComboBox()
        self.basemap_combo.setAllowEmptyLayer(True)
        self.basemap_combo.setCurrentIndex(0)
        bm_row.addWidget(self.basemap_combo, 1)
        adv.addLayout(bm_row)
        adv.addWidget(self._hint(
            "Renders the chosen layer (raster, XYZ/satellite tiles, or vector) as a flat "
            "underlay beneath the city. Style it live in the viewer's Basemap & Texture panel."))

        self.clear_cache_button = QPushButton("Clear OSM download cache")
        self.clear_cache_button.setObjectName("ghostButton")
        self.clear_cache_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_cache_button.clicked.connect(self.clearCacheRequested.emit)
        adv.addWidget(self.clear_cache_button)
        adv.addWidget(self._hint(
            "OpenStreetMap answers are kept on disk for a week, so building the same area "
            "again sends no request. Clear them to force a fresh download."))

        runs_row = QHBoxLayout()
        runs_row.setSpacing(8)
        self.runs_folder_button = QPushButton("Open data folder")
        self.runs_folder_button.setObjectName("ghostButton")
        self.runs_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.runs_folder_button.clicked.connect(self.openFolderRequested.emit)
        self.delete_runs_button = QPushButton("Delete old runs")
        self.delete_runs_button.setObjectName("ghostButton")
        self.delete_runs_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_runs_button.setToolTip("Deletes every run folder except the newest one, after you confirm.")
        self.delete_runs_button.clicked.connect(self.deleteRunsRequested.emit)
        runs_row.addWidget(self.runs_folder_button)
        runs_row.addWidget(self.delete_runs_button)
        adv.addLayout(runs_row)
        adv.addWidget(self._hint(
            "Every export is kept in its own folder under your QGIS profile, so a later "
            "run never overwrites it. Nothing is deleted unless you ask."))

        self.adv_container.setVisible(False)
        lay.addWidget(self.adv_container)
        return card

    def _summary_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("summaryCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        self.summary_title = QLabel("✓ Model ready")
        self.summary_title.setObjectName("summaryTitle")
        lay.addWidget(self.summary_title)

        self.summary_counts = QLabel("")
        self.summary_counts.setObjectName("summaryCounts")
        self.summary_counts.setWordWrap(True)
        lay.addWidget(self.summary_counts)

        self.summary_details = QLabel("")
        self.summary_details.setObjectName("summaryText")
        self.summary_details.setWordWrap(True)
        lay.addWidget(self.summary_details)

        fetch_row = QHBoxLayout()
        fetch_row.setSpacing(8)
        self.summary_fetch = QLabel("")
        self.summary_fetch.setObjectName("summaryText")
        self.summary_fetch.setWordWrap(True)
        self.fresh_button = QPushButton("Download fresh")
        self.fresh_button.setObjectName("ghostButton")
        self.fresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fresh_button.setToolTip("Builds the same area again from a new download.")
        self.fresh_button.clicked.connect(self._fresh)
        self.fresh_button.setVisible(False)
        fetch_row.addWidget(self.summary_fetch, 1)
        fetch_row.addWidget(self.fresh_button, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(fetch_row)

        self.summary_warnings = QLabel("")
        self.summary_warnings.setObjectName("summaryWarnings")
        self.summary_warnings.setTextFormat(Qt.TextFormat.RichText)
        self.summary_warnings.setWordWrap(True)
        self.summary_warnings.setVisible(False)
        lay.addWidget(self.summary_warnings)

        self.summary_circle = QLabel("")
        self.summary_circle.setObjectName("summaryText")
        self.summary_circle.setWordWrap(True)
        self.summary_circle.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.summary_circle)

        btn_row = QHBoxLayout()
        self.open_viewer_button = QPushButton("Open viewer")
        self.open_viewer_button.setObjectName("ghostButton")
        self.open_viewer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_viewer_button.clicked.connect(self.reopenRequested.emit)
        self.open_folder_button = QPushButton("Open data folder")
        self.open_folder_button.setObjectName("ghostButton")
        self.open_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_folder_button.clicked.connect(self.openFolderRequested.emit)
        btn_row.addWidget(self.open_viewer_button)
        btn_row.addWidget(self.open_folder_button)
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

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hint")
        label.setWordWrap(True)
        return label

    # -- Behaviour ----------------------------------------------------------
    def _toggle_advanced(self, checked: bool):
        self.adv_container.setVisible(checked)
        self.adv_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self._fit_to_contents()

    def _fit_to_contents(self):
        """Grow (or shrink) the window to its content, never past the screen.

        The cards scroll and the footer does not, so opening Advanced on a short
        screen scrolls the cards while Run stays in view.
        """
        layout = self.layout()
        if layout is None:
            return
        layout.activate()

        # Keep whatever width the user has settled on; only derive it before the
        # dialog is first shown (a raw QWidget starts at 640x480).
        if self._ever_shown:
            width = max(self.width(), self.minimumWidth())
        else:
            width = max(self.minimumWidth(), self.sizeHint().width())

        frame = 2 * self.scroll.frameWidth()
        inner = max(width - frame - 2, 100)
        body_layout = self.body_widget.layout()
        content = self.body_widget.sizeHint().height()
        if body_layout is not None and body_layout.hasHeightForWidth():
            content = max(content, body_layout.heightForWidth(inner))
        chrome = 0
        for band in (self.header_widget, self.footer_widget):
            if band.hasHeightForWidth():
                chrome += band.heightForWidth(width)
            else:
                chrome += band.sizeHint().height()
        desired = min(content + chrome + frame, self._available_height())
        self.resize(width, max(desired, self.minimumHeight()))

    def _available_height(self) -> int:
        """Usable screen height for this dialog, leaving room for window chrome."""
        screen = None
        get_screen = getattr(self, "screen", None)
        if callable(get_screen):
            screen = get_screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return 0
        return max(int(screen.availableGeometry().height()) - 90, 320)

    def select_basemap_layer(self, layer):
        """Point the advanced basemap combo at ``layer`` (used by Add OSM basemap)."""
        if layer is None:
            return
        try:
            self.basemap_combo.setLayer(layer)
        except (AttributeError, RuntimeError):
            return

    def area_params(self) -> dict:
        """What the model boundary depends on: the area source, the shape and the cap."""
        return {
            "source": "selection" if self.radio_selection.isChecked() else "canvas",
            "shape": self.shape_combo.currentData() or "circle",
            "max_ha": float(self.ha_spin.value()),
        }

    def run_params(self) -> dict:
        params = self.area_params()
        params.update({
            "theme": self.theme_combo.currentData() or _DEFAULT_THEME,
            "dem_layer": self.dem_combo.currentLayer(),
            "basemap_layer": self.basemap_combo.currentLayer(),
            "auto_open": self.auto_open_check.isChecked(),
        })
        return params

    def _emit_run(self):
        if self._busy:
            return
        params = self.run_params()
        self._save_settings()
        self.set_status("Starting…", busy=True)
        self.runRequested.emit(params)

    def _cancel(self):
        if not self._busy or self._cancelling:
            return
        self.cancel_button.setEnabled(False)
        self.set_status("Cancelling…", busy=True)
        self._cancelling = True
        self.cancelRequested.emit()

    def _fresh(self):
        if not self._busy:
            self.freshRequested.emit()

    # -- Place search -------------------------------------------------------
    def _search(self):
        if self._busy:
            return
        text = " ".join(self.place_edit.text().split())
        if not text:
            self._set_place_note("Type a place name first, for example Alsancak, Izmir.", error=True)
            return
        self.placeSearchRequested.emit(text)

    def set_place_searching(self, searching: bool):
        self.place_button.setEnabled(not searching and not self._busy)
        self.place_button.setText("Searching…" if searching else "Search")
        if searching:
            self.place_list.clear()
            self.place_list.setVisible(False)
            self._set_place_note("Searching…")

    def show_place_results(self, places, error_text: str = "", query: str = ""):
        """List up to five places to pick from, or say why there are none."""
        self.set_place_searching(False)
        self._places = list(places or [])[:5]
        self.place_list.clear()
        if error_text:
            self.place_list.setVisible(False)
            self._set_place_note(error_text, error=True)
            return
        if not self._places:
            self.place_list.setVisible(False)
            asked = f" for “{query}”" if query else ""
            self._set_place_note(
                f"No place found{asked}. Try a fuller name, for example Alsancak, Izmir.", error=True)
            return
        for place in self._places:
            item = QListWidgetItem(place.name)
            item.setToolTip(f"{place.name}\n{place.lat:.5f}, {place.lon:.5f}")
            self.place_list.addItem(item)
        rows = self.place_list.count()
        row_h = max(self.place_list.sizeHintForRow(0), self.fontMetrics().height() + 8)
        self.place_list.setFixedHeight(rows * row_h + 2 * self.place_list.frameWidth() + 4)
        self.place_list.setVisible(True)
        self._set_place_note("Pick a place to centre the model on it.")
        self._fit_to_contents()

    def _pick_place(self, item):
        row = self.place_list.row(item)
        if self._busy or not 0 <= row < len(self._places):
            return
        place = self._places[row]
        self.radio_canvas.setChecked(True)
        self._set_place_note(f"Centred on {place.name}.")
        self.placeChosen.emit(place)

    def set_place_note(self, text: str, error: bool = False):
        self._set_place_note(text, error)

    def _set_place_note(self, text: str, error: bool = False):
        self.place_note.setStyleSheet(f"color: {ERR if error else TEXT_MUTED};")
        self.place_note.setText(text)
        self.place_note.setVisible(bool(text))

    # -- Persistence (QgsSettings) -----------------------------------------
    def _restore_settings(self):
        s = QgsSettings()
        try:
            self.ha_spin.setValue(float(s.value(f"{_SETTINGS_PREFIX}/max_ha", self.ha_spin.value())))
        except (TypeError, ValueError):
            self.ha_spin.setValue(150.0)
        if str(s.value(f"{_SETTINGS_PREFIX}/source", "canvas")) == "selection":
            self.radio_selection.setChecked(True)
        else:
            self.radio_canvas.setChecked(True)
        saved_shape = str(s.value(f"{_SETTINGS_PREFIX}/shape", "circle"))
        shape_index = self.shape_combo.findData(saved_shape)
        if shape_index >= 0:
            self.shape_combo.setCurrentIndex(shape_index)
        saved_theme = str(s.value(f"{_SETTINGS_PREFIX}/theme", _DEFAULT_THEME))
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
        s.setValue(f"{_SETTINGS_PREFIX}/theme", self.theme_combo.currentData() or _DEFAULT_THEME)
        s.setValue(f"{_SETTINGS_PREFIX}/auto_open", self.auto_open_check.isChecked())

    # -- Window events -------------------------------------------------------
    def showEvent(self, event):  # noqa: N802 (Qt override)
        super().showEvent(event)
        first_show = not self._ever_shown
        self._ever_shown = True
        if first_show:
            self._fit_to_contents()
        self.visibilityChanged.emit(True)
        if not self._busy:
            self._refresh_area_summary()

    def hideEvent(self, event):  # noqa: N802 (Qt override)
        super().hideEvent(event)
        self.visibilityChanged.emit(False)

    def closeEvent(self, event):  # noqa: N802 (Qt override)
        # Closing (or Esc) only hides the dialog; a running export carries on and
        # the toolbar icon brings the dialog back with its live status.
        self._save_settings()
        super().closeEvent(event)

    def prepare_for_unload(self):
        """Stop the clocks and leave the busy state before QGIS tears the dialog down."""
        self._elapsed_timer.stop()
        self._set_busy(False)

    # -- Area readout --------------------------------------------------------
    def refresh_area_summary(self) -> None:
        """Public entry point: re-measure the model boundary and redraw the readout.

        Called by the plugin whenever the map is zoomed, panned or reprojected,
        so the figure never describes a view the user has already left.
        """
        self._refresh_area_summary()

    def _refresh_area_summary(self, *_):
        if self._busy or not callable(self.area_probe):
            return
        try:
            text = self.area_probe(self.area_params())
        except Exception as exc:  # never let a probe error break the dialog
            text = str(exc)
        self.set_area_summary(text)

    def set_area_summary(self, text: str):
        self.area_readout.setText(text or "—")

    # -- Status line ---------------------------------------------------------
    def set_status(self, text: str, error: bool = False, busy: bool = False,
                   neutral: bool = False):
        """Show one sentence in the footer.

        While busy the elapsed seconds since the run started follow the
        sentence and count up every second ("Asking overpass-api.de… 23 s").
        """
        was_busy = self._busy
        self._status_text = plain_crs(text)
        if error:
            self._status_kind = "error"
        elif busy or neutral:
            self._status_kind = "info"
        else:
            self._status_kind = "ok"
        self._set_busy(busy)
        if busy:
            if not was_busy or self._wait_started is None:
                self._wait_started = time.monotonic()
            if not self._elapsed_timer.isActive():
                self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()
            self._wait_started = None
        self._render_status()

    @pyqtSlot(str)
    def show_progress(self, text: str):
        """A status sentence from the download task (delivered on the UI thread)."""
        if self._busy and not self._cancelling and text:
            self.set_status(text, busy=True)

    def elapsed_seconds(self) -> int:
        if self._wait_started is None:
            return 0
        return int(time.monotonic() - self._wait_started)

    def _render_status(self):
        text = self._status_text
        if self._busy and self._wait_started is not None and text:
            seconds = self.elapsed_seconds()
            if text.endswith(("…", "...")):
                text = f"{text} {seconds} s"
            else:
                text = f"{text} ({seconds} s)"
        sheet = f"color: {({'error': ERR, 'ok': OK}).get(self._status_kind, TEXT)}; font-size: 12px;"
        if self.status.styleSheet() != sheet:
            self.status.setStyleSheet(sheet)
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def set_cancel_enabled(self, enabled: bool):
        self.cancel_button.setEnabled(bool(enabled) and self._busy)

    def set_has_last_run(self, has_run: bool):
        """'Reopen last viewer' once a run exists; 'Open sample city' before."""
        if has_run:
            self.reopen_button.setText(REOPEN_LABEL)
            self.reopen_button.setToolTip("Opens the newest run in the 3D viewer again.")
        else:
            self.reopen_button.setText(SAMPLE_LABEL)
            self.reopen_button.setToolTip(
                "Opens the sample city that ships with the plugin, so you can try the "
                "viewer before downloading anything.")

    def _set_busy(self, busy: bool):
        entering = bool(busy) and not self._busy
        self._busy = bool(busy)
        if entering or not self._busy:
            self._cancelling = False
        enabled = not self._busy
        for widget in (
            self.place_edit,
            self.place_button,
            self.place_list,
            self.radio_canvas,
            self.radio_selection,
            self.shape_combo,
            self.basemap_button,
            self.ha_spin,
            self.theme_combo,
            self.auto_open_check,
            self.adv_toggle,
            self.dem_combo,
            self.basemap_combo,
            self.clear_cache_button,
            self.runs_folder_button,
            self.delete_runs_button,
            self.reopen_button,
            self.open_viewer_button,
            self.open_folder_button,
            self.fresh_button,
        ):
            widget.setEnabled(enabled)
        self.run_button.setEnabled(enabled)
        self.run_button.setText(_BUSY_LABEL if self._busy else _RUN_LABEL)
        if entering:
            self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(self._busy)

    # -- Result --------------------------------------------------------------
    def show_summary(self, summary: dict, url: str = "", fetch=None, notes=()):
        """What was built, what was estimated, where the data came from, what failed.

        ``summary`` is builder.build_and_export's dict; ``fetch`` carries the
        download's ``from_cache``, ``saved_at`` (epoch seconds) and ``host``;
        ``notes`` are extra problems found after the build (the viewer could not
        start, for instance). Every warning is listed; none is summarised away.
        """
        summary = summary or {}
        fetch = fetch or {}
        counts = summary.get("counts", {}) or {}
        warnings = [str(w).strip() for w in (summary.get("warnings") or []) if str(w).strip()]
        problems = warnings + [str(n).strip() for n in (notes or []) if str(n).strip()]

        if problems:
            self.summary_title.setText(f"Model ready, with {_plural(len(problems), 'warning')}")
            self.summary_title.setStyleSheet(f"color: {WARN};")
        else:
            self.summary_title.setText("✓ Model ready")
            self.summary_title.setStyleSheet(f"color: {OK};")

        furniture = sum(int(counts.get(key, 0) or 0)
                        for key in ("busstops", "benches", "lights", "trashbins"))
        waterways = int(counts.get("waterlines", 0) or 0) + int(counts.get("waterareas", 0) or 0)
        buildings = int(summary.get("buildings", counts.get("buildings", 0)) or 0)
        self.summary_counts.setText(
            f"<b>{buildings}</b> buildings · "
            f"<b>{counts.get('roads', 0)}</b> roads · "
            f"<b>{counts.get('bikelanes', 0)}</b> bike lanes · "
            f"<b>{counts.get('greens', 0)}</b> greens · "
            f"<b>{waterways}</b> waterways · "
            f"<b>{furniture}</b> furniture"
        )

        details = []
        if summary.get("osm_trees") is not None or summary.get("planted_trees") is not None:
            osm_trees = int(summary.get("osm_trees") or 0)
            planted = int(summary.get("planted_trees") or 0)
            details.append(f"Trees: {osm_trees} from OSM + {planted} planted.")
        else:
            details.append(f"Trees: {int(counts.get('trees', 0) or 0)}.")
        if buildings and summary.get("estimated_heights") is not None:
            estimated = int(summary.get("estimated_heights") or 0)
            if estimated:
                details.append(f"{estimated} of {buildings} building heights are estimated "
                               "from the building type.")
            else:
                details.append(f"All {buildings} building heights come from OpenStreetMap tags.")
        self.summary_details.setText(" ".join(details))

        fetch_text = ""
        if fetch.get("from_cache"):
            saved_at = fetch.get("saved_at")
            if saved_at:
                fetch_text = (f"Built from a copy downloaded {age_text(time.time() - float(saved_at))} "
                              "ago; no request was sent.")
            else:
                fetch_text = "Built from a saved copy; no request was sent."
        elif fetch.get("host"):
            fetch_text = f"Downloaded just now from {fetch['host']}."
        self.summary_fetch.setText(fetch_text)
        self.summary_fetch.setVisible(bool(fetch_text))
        self.fresh_button.setVisible(bool(fetch.get("from_cache")))

        if problems:
            self.summary_warnings.setText(
                "<br>".join(f"• {html.escape(text)}" for text in problems))
        else:
            self.summary_warnings.setText("")
        self.summary_warnings.setVisible(bool(problems))

        label = summary.get("shape_label", "Study area")
        area_ha = summary.get("area_ha", "?")
        if summary.get("radius_m"):
            line = f"{label}: r = {summary['radius_m']} m ({area_ha} ha)"
        elif summary.get("width_m"):
            line = f"{label}: {summary['width_m']} × {summary['depth_m']} m ({area_ha} ha)"
        else:
            line = f"{label} · {area_ha} ha"
        if url:
            line += f"\nViewer: {url}"
        self.summary_circle.setText(line)
        zone = utm_zone_label(summary.get("epsg"))
        self.summary_circle.setToolTip(
            f"Measured in {zone} (EPSG:{summary.get('epsg')})." if zone else "")

        self.summary_card.setVisible(True)
        self._fit_to_contents()
        self._reveal_summary()
        # The card's geometry settles on the next pass of the event loop.
        QTimer.singleShot(0, self._reveal_summary)

    def _reveal_summary(self):
        """Scroll the summary card into view (vertically only)."""
        layout = self.body_widget.layout()
        if layout is not None:
            layout.activate()
        self.scroll.ensureWidgetVisible(self.summary_card, 0, 12)
        self.scroll.horizontalScrollBar().setValue(0)

    # -- Style --------------------------------------------------------------
    def _stylesheet(self) -> str:
        return f"""
        QDialog {{ background: {PANEL_BG}; }}
        #body {{ background: {PANEL_BG}; }}
        #bodyScroll {{ background: {PANEL_BG}; border: 0; }}
        #bodyScroll > QWidget > QWidget {{ background: {PANEL_BG}; }}
        QScrollBar:vertical {{
            background: transparent; width: 10px; margin: 2px 2px 2px 0;
        }}
        QScrollBar::handle:vertical {{
            background: {ACCENT_SOFT}; border-radius: 5px; min-height: 28px;
        }}
        QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QLabel {{ color: {TEXT}; background: transparent; }}
        #header {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 {ACCENT_DARK}, stop:1 {ACCENT_DEEP});
        }}
        #headerTitle {{ color: #ffffff; font-size: 19px; font-weight: 800; }}
        #headerSub {{ color: #ffffff; font-size: 11px; }}
        #footer {{ background: {CARD_BG}; border-top: 1px solid {CARD_BORDER}; }}
        #card, #summaryCard {{
            background: {CARD_BG};
            border: 1px solid {CARD_BORDER};
            border-radius: 10px;
        }}
        #summaryCard {{ border: 1px solid {ACCENT_SOFT}; background: #fff4ee; }}
        #cardTitle {{ font-weight: 700; color: {ACCENT_DEEP}; font-size: 12px;
            text-transform: uppercase; letter-spacing: 0.04em; }}
        #summaryTitle {{ font-weight: 800; color: {OK}; font-size: 14px; }}
        #summaryCounts {{ font-size: 13px; color: {TEXT}; }}
        #summaryText {{ font-size: 12px; color: {TEXT}; }}
        #summaryWarnings {{ font-size: 12px; color: {WARN}; }}
        #hint {{ color: {TEXT_MUTED}; font-size: 11px; }}
        #readout {{
            background: {PANEL_BG};
            border: 1px solid {CARD_BORDER};
            border-radius: 6px;
            padding: 6px 9px;
            color: {ACCENT_DEEP};
            font-weight: 600;
            font-size: 12px;
        }}
        /* Only the text colour: any background/border rule here would make Qt
           render the check/radio indicators through the stylesheet, and Qt 6
           then draws them as blank boxes. Their colours come from the palette. */
        QRadioButton {{ color: {TEXT}; padding: 2px 0; }}
        QCheckBox {{ color: {TEXT}; padding: 2px 0; }}
        QRadioButton:disabled, QCheckBox:disabled {{ color: {TEXT_MUTED}; }}
        /* The spin box gets no colour either: a QGIS UI theme that styles it
           (Night Mapping, Blend of Gray) sets its field and text colours as a
           pair, and the default theme reads the dialog palette. Styling its
           background here would hide its native up/down arrows. */
        QDoubleSpinBox {{ min-width: 108px; }}
        /* Text colour too: a stylesheet-polished widget keeps the application
           palette, which is light-on-dark when QGIS follows a dark desktop. */
        QLineEdit#placeEdit {{
            background: #ffffff; color: {TEXT}; border: 1px solid {CARD_BORDER};
            border-radius: 6px; padding: 5px 8px;
            selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT};
        }}
        QLineEdit#placeEdit:focus {{ border-color: {ACCENT_DARK}; }}
        QListWidget#placeResults {{
            background: #ffffff; color: {TEXT}; border: 1px solid {CARD_BORDER};
            border-radius: 6px; outline: 0;
        }}
        QListWidget#placeResults::item {{ padding: 4px 6px; color: {TEXT}; }}
        QListWidget#placeResults::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
        QListWidget#placeResults::item:hover {{ background: #f6e7e0; color: {TEXT}; }}
        QComboBox {{
            padding: 4px 8px; border: 1px solid {CARD_BORDER};
            border-radius: 6px; background: #ffffff; min-width: 92px; color: {TEXT};
        }}
        QComboBox:hover {{ border-color: {ACCENT}; }}
        QComboBox:disabled {{ background: #f7efec; color: {TEXT_MUTED}; }}
        QComboBox::drop-down {{ border: 0; width: 18px; }}
        QComboBox QAbstractItemView {{
            background: #ffffff; color: {TEXT};
            selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT};
            border: 1px solid {CARD_BORDER}; outline: 0;
        }}
        /* A transparent, borderless toggle on the card: a QGIS theme's tool-button
           fill (#535353 in Night Mapping) would otherwise sit behind its text. */
        #advToggle {{
            background: transparent; border: 0; color: {ACCENT_DEEP};
            font-weight: 700; padding: 2px;
        }}
        #advToggle:hover, #advToggle:checked {{ background: transparent; border: 0; color: {TEXT}; }}
        QPushButton#runButton {{
            background: {RUN_BG}; color: {RUN_FG}; border: 0; border-radius: 9px;
            font-size: 14px; font-weight: 800; padding: 0 12px;
        }}
        QPushButton#runButton:hover {{ background: {RUN_BG_HOVER}; color: {RUN_FG}; }}
        QPushButton#runButton:disabled {{ background: {RUN_BG_BUSY}; color: {TEXT}; }}
        QPushButton#cancelButton {{
            background: #ffffff; color: {ERR}; border: 1px solid {ERR};
            border-radius: 9px; font-size: 13px; font-weight: 700; padding: 0 14px;
        }}
        QPushButton#cancelButton:hover {{ background: #fdecea; }}
        QPushButton#cancelButton:disabled {{
            background: #f7efec; color: {TEXT_MUTED}; border-color: {CARD_BORDER};
        }}
        QPushButton#ghostButton {{
            background: transparent; color: {ACCENT_DEEP};
            border: 1px solid {ACCENT_SOFT}; border-radius: 7px; padding: 7px 10px;
            font-weight: 600;
        }}
        QPushButton#ghostButton:hover {{ background: #f6e7e0; border-color: {ACCENT}; }}
        QPushButton#ghostButton:disabled {{ color: {TEXT_MUTED}; border-color: {CARD_BORDER}; }}
        """
