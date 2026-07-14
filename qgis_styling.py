# -*- coding: utf-8 -*-
"""Native QGIS styling for the OSM layers created by the plugin.

The web viewer remains the primary 3D output, but the downloaded memory layers
should also arrive as a legible planning map.  The renderer structure follows
the companion OSM Quick 3D plugin: semantic building/ground categories, road
hierarchy with real-world widths, and a restrained Editorial Paper palette.
"""
from __future__ import annotations

import contextlib

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsProperty,
    QgsRendererCategory,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsUnitTypes,
)


EDITORIAL_PALETTE = {
    "background": "#fdfbf7",
    "base": "#e6dfd3",
    "base_outline": "#b9aa96",
    "roads_major": "#7c5c43",
    "roads_minor": "#eadecc",
    "greens": "#c2c5aa",
    "water": "#9ab8c2",
    "trees": "#6b705c",
    "buildings": {
        "residential": "#ebd4c0",
        "commercial": "#c4d1db",
        "industrial": "#dbcfb8",
        "civic": "#cfdcd5",
        "worship": "#e8dfeb",
        "other": "#dcd8d3",
    },
}

_FALLBACK_BUILDINGS = {
    "residential": "#d8c3b1",
    "commercial": "#b7c2d0",
    "industrial": "#c6b9a4",
    "civic": "#cdd6d2",
    "worship": "#d8cfe2",
    "other": "#cac5bf",
}

BUILDING_CLASS_EXPR = (
    "CASE"
    " WHEN lower(\"building\") IN ('apartments','residential','house','detached',"
    "'terrace','dormitory','bungalow','semidetached_house','hut','cabin','farmhouse','villa') THEN 'residential'"
    " WHEN lower(\"building\") IN ('commercial','retail','office','supermarket',"
    "'kiosk','hotel') THEN 'commercial'"
    " WHEN lower(\"building\") IN ('industrial','warehouse','manufacture','hangar',"
    "'factory') THEN 'industrial'"
    " WHEN lower(\"building\") IN ('school','university','college','kindergarten',"
    "'hospital','clinic','public','civic','government','townhall') THEN 'civic'"
    " WHEN lower(\"building\") IN ('church','mosque','temple','synagogue',"
    "'cathedral','chapel') THEN 'worship'"
    " ELSE 'other' END"
)

ROAD_CLASS_EXPR = (
    "CASE"
    " WHEN lower(\"highway\") IN ('motorway','trunk','motorway_link','trunk_link') THEN 'major'"
    " WHEN lower(\"highway\") IN ('primary','primary_link') THEN 'primary'"
    " WHEN lower(\"highway\") IN ('secondary','secondary_link') THEN 'secondary'"
    " WHEN lower(\"highway\") IN ('tertiary','tertiary_link') THEN 'tertiary'"
    " WHEN lower(\"highway\") IN ('residential','unclassified','living_street','road') THEN 'residential'"
    " WHEN lower(\"highway\") IN ('service','track') THEN 'service'"
    " WHEN lower(\"highway\") IN ('footway','path','pedestrian','steps','corridor','bridleway') THEN 'foot'"
    " ELSE 'other' END"
)

ROAD_WIDTH_M_EXPR = (
    "coalesce(to_real(\"width\"), CASE"
    " WHEN lower(\"highway\") IN ('motorway','trunk','motorway_link','trunk_link') THEN 24"
    " WHEN lower(\"highway\") IN ('primary','primary_link') THEN 18"
    " WHEN lower(\"highway\") IN ('secondary','secondary_link') THEN 14"
    " WHEN lower(\"highway\") IN ('tertiary','tertiary_link') THEN 11"
    " WHEN lower(\"highway\") IN ('residential','unclassified','living_street','road') THEN 7"
    " WHEN lower(\"highway\") IN ('service','track') THEN 4"
    " WHEN lower(\"highway\") IN ('footway','path','pedestrian','steps','corridor','bridleway') THEN 2.5"
    " ELSE 5 END)"
)
ROAD_RENDER_WIDTH_EXPR = (
    f"CASE WHEN ({ROAD_WIDTH_M_EXPR}) < 1.5 THEN 1.5 "
    f"WHEN ({ROAD_WIDTH_M_EXPR}) > 30 THEN 30 ELSE ({ROAD_WIDTH_M_EXPR}) END"
)

GREEN_CLASS_EXPR = (
    "CASE"
    " WHEN lower(\"natural\") = 'water' THEN 'water'"
    " WHEN lower(\"landuse\") = 'parking' THEN 'parking'"
    " WHEN lower(\"landuse\") = 'pedestrian' THEN 'pedestrian'"
    " WHEN lower(\"leisure\") IN ('park','garden') OR lower(\"landuse\") IN "
    "('grass','recreation_ground','meadow','village_green','orchard','vineyard',"
    "'farmland','allotments','greenfield') THEN 'park'"
    " WHEN lower(\"landuse\") = 'forest' OR lower(\"natural\") IN "
    "('wood','scrub','grassland','heath') THEN 'forest'"
    " WHEN lower(\"leisure\") IN ('pitch','playground','dog_park','golf_course') THEN 'pitch'"
    " WHEN lower(\"landuse\") = 'cemetery' THEN 'cemetery'"
    " ELSE 'green' END"
)


def _mix_hex(first: str, second: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, float(ratio)))
    one = first.lstrip("#")
    two = second.lstrip("#")
    if len(one) != 6 or len(two) != 6:
        return first
    values = [
        round(
            int(one[index:index + 2], 16) * (1.0 - ratio) +
            int(two[index:index + 2], 16) * ratio
        )
        for index in (0, 2, 4)
    ]
    return "#" + "".join(f"{value:02x}" for value in values)


def palette_for(theme_name: str, web_palette: dict | None = None) -> dict:
    """Return a complete native-map palette for a web theme."""
    if theme_name == "Editorial Paper":
        return EDITORIAL_PALETTE
    web = web_palette or {}
    base = web.get("islandColor", "#e6dfd3")
    road = web.get("roadColor", "#7c5c43")
    green = web.get("parkColor", "#c2c5aa")
    return {
        "background": web.get("terrainOutsideColor", "#fdfbf7"),
        "base": base,
        "base_outline": _mix_hex(base, "#55483d", 0.35),
        "roads_major": road,
        "roads_minor": _mix_hex(road, base, 0.72),
        "greens": green,
        "water": web.get("waterColor", "#9ab8c2"),
        "trees": _mix_hex(green, "#314334", 0.45),
        "buildings": _FALLBACK_BUILDINGS,
    }


def _render_map_units():
    try:
        return QgsUnitTypes.RenderUnit.RenderMapUnits
    except AttributeError:
        return QgsUnitTypes.RenderMapUnits


def _fill(color_hex: str, outline: str, outline_width: float = 0.16):
    return QgsFillSymbol.createSimple({
        "color": color_hex,
        "outline_color": outline,
        "outline_width": str(outline_width),
        "joinstyle": "round",
        "style": "solid",
    })


def _line(color_hex: str, width: float, width_expression: str | None = None,
          dashed: bool = False):
    props = {
        "color": color_hex,
        "width": str(width),
        "capstyle": "round",
        "joinstyle": "round",
        "line_style": "dash" if dashed else "solid",
    }
    symbol = QgsLineSymbol.createSimple(props)
    with contextlib.suppress(Exception):
        symbol.setWidthUnit(_render_map_units())
    if width_expression:
        key = getattr(QgsSymbolLayer, "PropertyStrokeWidth", None)
        if key is None:
            key = getattr(QgsSymbolLayer, "PropertyWidth", None)
        if key is not None:
            with contextlib.suppress(Exception):
                symbol.symbolLayer(0).setDataDefinedProperty(
                    key, QgsProperty.fromExpression(width_expression))
    return symbol


def _marker(color_hex: str, size: float, outline: str = "#5f574d", name: str = "circle"):
    return QgsMarkerSymbol.createSimple({
        "name": name,
        "color": color_hex,
        "size": str(size),
        "outline_color": outline,
        "outline_width": "0.2",
    })


def _categorized(expression: str, symbols: dict):
    categories = [
        QgsRendererCategory(value, symbol, value.replace("_", " ").title())
        for value, symbol in symbols.items()
    ]
    return QgsCategorizedSymbolRenderer(expression, categories)


def _style_buildings(layer, palette: dict) -> None:
    colors = palette["buildings"]
    symbols = {
        key: _fill(color, _mix_hex(color, "#67584d", 0.42), 0.18)
        for key, color in colors.items()
    }
    layer.setRenderer(_categorized(BUILDING_CLASS_EXPR, symbols))


def _style_roads(layer, palette: dict) -> None:
    major = palette["roads_major"]
    minor = palette["roads_minor"]
    colors = {
        "major": major,
        "primary": _mix_hex(major, "#b68d68", 0.22),
        "secondary": _mix_hex(major, minor, 0.42),
        "tertiary": _mix_hex(major, minor, 0.68),
        "residential": minor,
        "service": _mix_hex(minor, palette["base"], 0.35),
        "foot": _mix_hex(major, palette["base"], 0.58),
        "other": minor,
    }
    symbols = {
        key: _line(color, 5.0, ROAD_RENDER_WIDTH_EXPR, dashed=(key == "foot"))
        for key, color in colors.items()
    }
    layer.setRenderer(_categorized(ROAD_CLASS_EXPR, symbols))


def _style_greens(layer, palette: dict) -> None:
    green = palette["greens"]
    water = palette["water"]
    colors = {
        "water": water,
        "parking": _mix_hex(palette["base"], "#a9adb0", 0.48),
        "pedestrian": _mix_hex(palette["base"], "#ffffff", 0.28),
        "park": green,
        "forest": _mix_hex(green, "#52624a", 0.30),
        "pitch": _mix_hex(green, "#d8dfb9", 0.24),
        "cemetery": _mix_hex(green, "#8d9285", 0.30),
        "green": green,
    }
    symbols = {
        key: _fill(color, _mix_hex(color, "#596356", 0.34), 0.12)
        for key, color in colors.items()
    }
    layer.setRenderer(_categorized(GREEN_CLASS_EXPR, symbols))


def style_export_layers(roi_layer, osm: dict, theme_name: str,
                        web_palette: dict | None = None) -> None:
    """Apply coordinated native renderers to a completed export in-place."""
    palette = palette_for(theme_name, web_palette)

    if roi_layer is not None:
        roi_layer.setRenderer(QgsSingleSymbolRenderer(
            _fill(palette["base"], palette["base_outline"], 0.28)))

    buildings = osm.get("buildings")
    if buildings is not None:
        _style_buildings(buildings, palette)

    roads = osm.get("roads")
    if roads is not None:
        _style_roads(roads, palette)

    greens = osm.get("greens")
    if greens is not None:
        _style_greens(greens, palette)

    water = osm.get("waterlines")
    if water is not None:
        width = 'greatest(1.0, least(12.0, coalesce(to_real("width"), 3.0)))'
        water.setRenderer(QgsSingleSymbolRenderer(
            _line(palette["water"], 3.0, width)))

    bike = osm.get("bikelanes")
    if bike is not None:
        width = 'greatest(0.8, least(5.0, coalesce(to_real("width"), 1.8)))'
        bike.setRenderer(QgsSingleSymbolRenderer(
            _line("#769b72", 1.8, width)))

    trees = osm.get("trees")
    if trees is not None:
        trees.setRenderer(QgsSingleSymbolRenderer(
            _marker(palette["trees"], 2.2, "#4c5945")))

    point_styles = {
        "busstops": ("#b98258", 2.6, "square"),
        "benches": ("#9a7657", 2.0, "rectangle"),
        "lights": ("#d1b35b", 1.9, "circle"),
        "trashbins": ("#718276", 1.8, "diamond"),
    }
    for key, (color, size, marker_name) in point_styles.items():
        layer = osm.get(key)
        if layer is not None:
            layer.setRenderer(QgsSingleSymbolRenderer(
                _marker(color, size, name=marker_name)))

    for layer in [roi_layer, *(osm.get(key) for key in (
        "buildings", "roads", "greens", "waterlines", "bikelanes", "trees",
        "busstops", "benches", "lights", "trashbins",
    ))]:
        if layer is None:
            continue
        layer.setCustomProperty("osm_3d_model/theme", theme_name)
        layer.triggerRepaint()
