# Third-party notices

3D OSM Model ships the following libraries for a fully offline viewer.

## Three.js r160

Source: <https://github.com/mrdoob/three.js>

The viewer ships three.js r160 in `web/assets/vendor/three/`: the core build
and the addons it uses from `examples/jsm/`, among them the GLTFLoader that
reads custom models and the GLTFExporter that writes the GLB export. The
addons are part of three.js and come under the same MIT licence, reproduced
below.

The MIT License

Copyright © 2010-2023 Three.js Authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## OpenStreetMap data

3D OSM Model downloads and displays OpenStreetMap data. OpenStreetMap data is
© OpenStreetMap contributors and is available under the Open Database License
1.0 (ODbL).

- Attribution: [OpenStreetMap](https://www.openstreetmap.org/copyright)
- License: [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)

When you retain or publish downloaded OSM layers, maps, screenshots, exports,
or derived databases, keep the attribution and comply with the ODbL as
applicable.

## OpenStreetMap tile service

The optional standard basemap uses the OpenStreetMap tile service. Keep the
visible attribution and follow the [tile usage policy](https://operations.openstreetmap.org/policies/tiles/).

## Overpass API

OSM downloads use public Overpass API servers. The endpoints are network
services and are not bundled with this plugin:

- <https://overpass-api.de/api/interpreter>
- <https://overpass.private.coffee/api/interpreter>

overpass-api.de is asked first, because overpass.private.coffee did not
answer at all when this release was tested; once a server has delivered, the
one that delivered last is asked first, also after QGIS restarts.

The plugin follows the operators' 2026 rules. Every request carries the
`Referer` `https://github.com/YusufEminoglu/osm_3d_model`, because QGIS
replaces a plugin's own User-Agent. Each run asks each server once, and again
only after a dropped connection. A server that answers 429 rests for a
minute, one that refuses with 403 or 406 for an hour, a busy one (5xx) for 45
seconds and one that does not answer for two minutes; the shorter rests
double while the server keeps failing, up to 15 minutes. Queries are bounded
in time and memory (`[maxsize:134217728]`), and only complete answers are
stored on this computer, for a week.

## Place search (Nominatim)

The place search uses QGIS's own geocoder, `QgsNominatimGeocoder`, which
sends the text you type to the Nominatim service of QGIS at
<https://nominatim.qgis.org/search>. Nominatim results are OpenStreetMap
data, © OpenStreetMap contributors, under the ODbL.

## geotiff.js

Source: <https://github.com/geotiffjs/geotiff.js>

The MIT License (MIT)

Copyright (c) 2015 EOX IT Services GmbH

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
