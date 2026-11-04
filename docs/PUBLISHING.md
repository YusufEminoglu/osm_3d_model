# Publishing the GitHub Page

The repository can serve a GitHub Pages site from the `/docs` folder. Its only page, `docs/index.html`, forwards visitors to the reference manual hosted at <https://geophilo.com/osm_3d_model/manual.html>; the manual itself is maintained with the other geophilo.com pages, not in this repository.

## Enable Pages

1. Open the repository on GitHub.
2. Go to **Settings**.
3. Open **Pages**.
4. Set **Source** to the main branch.
5. Set the folder to `/docs`.
6. Save.

GitHub will publish `docs/index.html`.

## After Publishing

Update the repository description and website URL:

- Description: `One click from QGIS to an interactive OpenStreetMap 3D city.`
- Website: `https://geophilo.com/osm_3d_model/`, the homepage in `metadata.txt`.
- Topics: `qgis`, `openstreetmap`, `threejs`, `3d-city`, `urban-planning`, `geojson`, `procedural-generation`, `osm`, `gis`, `glb`

## Documentation-only Rule

Documentation, GitHub Pages, issue templates, README media, and showcase scripts do not require a plugin version bump.

Only bump the plugin version when the packaged plugin behavior, metadata, or runtime code changes.
