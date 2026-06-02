# Publishing the GitHub Page

The repository is ready for a GitHub Pages site from the `/docs` folder.

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
- Website: the GitHub Pages URL created by GitHub.
- Topics: `qgis`, `openstreetmap`, `threejs`, `3d-city`, `urban-planning`, `geojson`, `procedural-generation`, `osm`, `gis`

## Documentation-only Rule

Documentation, GitHub Pages, issue templates, README media, and showcase scripts do not require a plugin version bump.

Only bump the plugin version when the packaged plugin behavior, metadata, or runtime code changes.
