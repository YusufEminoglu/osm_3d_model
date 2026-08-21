# 3D OSM Model Documentation

This folder is the GitHub presentation layer for 3D OSM Model.

It is intentionally separate from plugin versioning. Documentation, showcase assets, and GitHub Pages polish can be improved without changing `metadata.txt`.

## Pages

- [GitHub Pages landing page](index.html)
- [Showcase playbook](SHOWCASE.md)
- [Architecture notes](ARCHITECTURE.md)
- [Publishing guide](PUBLISHING.md)

## Visual Assets

- [Hero scene](assets/github-hero.svg) — the study area rising into a city, recolouring through the ten looks while the sun crosses the sky
- [Export pipeline](assets/pipeline.svg) — study area → Overpass → GeoJSON → viewer
- [Curated looks](assets/looks-gallery.svg) — one street elevation cycling through all ten palettes
- [Walk mode](assets/walk-mode.svg) — the 1.85 m body, its 1.73 m eye height and its gait

All four are animated with SMIL only — no `<style>` block and no script, both of which
GitHub's README sanitiser strips — so they animate in the repository page, in the Pages
site and in any plain image viewer. They are regenerated, not hand-edited; keep the
numbers in them in step with `web/src/looks.js` and `web/src/walk.js`.
