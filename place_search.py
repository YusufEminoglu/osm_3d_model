# -*- coding: utf-8 -*-
"""Find a place by name with QGIS's own Nominatim geocoder, off the UI thread.

``QgsNominatimGeocoder`` (QGIS 3.18+) makes a blocking request and keeps QGIS's
Nominatim service to one request per second by itself, so the search runs in a
``QgsTask``. Place names come from OpenStreetMap (© OpenStreetMap contributors,
ODbL).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlsplit

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsFeedback,
    QgsGeocoderContext,
    QgsNominatimGeocoder,
    QgsProject,
    QgsTask,
)

#: The endpoint given to QgsNominatimGeocoder: QGIS's own Nominatim service,
#: which is also the geocoder's built-in default on QGIS 3.44 and 4.2 (probed
#: 2026-11-02). Read at call time, so a test can point it at a fake server on
#: 127.0.0.1.
GEOCODER_ENDPOINT = "https://nominatim.qgis.org/search"
#: At most this many places are offered to pick from.
MAX_RESULTS = 5
#: Shown next to the search box.
ATTRIBUTION = "Place search: QGIS's Nominatim service. Names © OpenStreetMap contributors."


class PlaceSearchError(RuntimeError):
    """``str(error)`` is the sentence the dialog shows."""


@dataclass(frozen=True)
class Place:
    """One search result, in WGS 84."""

    name: str
    lon: float
    lat: float
    #: (west, south, east, north) when the service gave a bounding box.
    bbox: Optional[Tuple[float, float, float, float]] = None
    kind: str = ""


def _service_host() -> str:
    return urlsplit(GEOCODER_ENDPOINT).netloc or GEOCODER_ENDPOINT


def _to_wgs84(result):
    """The result's point and viewport in WGS 84 (Nominatim already answers in it)."""
    geometry = result.geometry()
    point = geometry.asPoint() if not geometry.isMultipart() else geometry.centroid().asPoint()
    viewport = result.viewport()
    crs = result.crs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if crs.isValid() and crs != wgs84:
        transform = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
        point = transform.transform(point)
        if not viewport.isNull():
            viewport = transform.transformBoundingBox(viewport)
    bbox = None
    if not viewport.isNull() and not viewport.isEmpty():
        bbox = (viewport.xMinimum(), viewport.yMinimum(), viewport.xMaximum(), viewport.yMaximum())
    return point, bbox


def search_places(text: str, feedback: Optional[QgsFeedback] = None) -> List[Place]:
    """Return up to MAX_RESULTS places for ``text``. Blocking: call it off the UI thread."""
    query = " ".join(str(text or "").split())
    if not query:
        raise PlaceSearchError("Type a place name first, for example Alsancak, Izmir.")
    geocoder = QgsNominatimGeocoder("", GEOCODER_ENDPOINT)
    context = QgsGeocoderContext(QgsCoordinateTransformContext())
    results = geocoder.geocodeString(query, context, feedback)
    if feedback is not None and feedback.isCanceled():
        return []
    places: List[Place] = []
    for result in results:
        if not result.isValid():
            reason = str(result.error() or "no answer").strip().rstrip(".")
            raise PlaceSearchError(
                f"The place search could not reach {_service_host()} ({reason}). "
                "Check the connection, or zoom the map to your area instead."
            )
        attributes = result.additionalAttributes() or {}
        point, bbox = _to_wgs84(result)
        kind = " ".join(str(attributes.get(key, "")) for key in ("class", "type")).strip()
        places.append(Place(str(result.identifier()), float(point.x()), float(point.y()), bbox, kind))
        if len(places) >= MAX_RESULTS:
            break
    return places


class PlaceSearchTask(QgsTask):
    """Runs search_places() on a worker thread and reports back on the UI thread."""

    def __init__(self, text: str, callback: Callable[["PlaceSearchTask"], None]):
        can_cancel = getattr(getattr(QgsTask, "Flag", QgsTask), "CanCancel")
        super().__init__("3D OSM Model: find a place", can_cancel)
        self.text = str(text or "")
        self.callback = callback
        self.feedback = QgsFeedback()
        self.places: List[Place] = []
        self.error_text = ""

    def cancel(self):
        self.feedback.cancel()
        super().cancel()

    def run(self):
        try:
            self.places = search_places(self.text, self.feedback)
        except PlaceSearchError as exc:
            self.error_text = str(exc)
            return False
        except Exception as exc:  # a search must never take the plugin down
            self.error_text = f"The place search failed: {exc}"
            return False
        return not self.isCanceled()

    def finished(self, result):
        callback, self.callback = self.callback, None
        if callback is not None:
            callback(self)
