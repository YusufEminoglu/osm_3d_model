// Building heights from the export's attributes (3D OSM Model 1.10.0, C3).
//
// The QGIS side writes, per building:
//   height      wall top above the ground in metres (OSM `height` minus the
//               roof rise for a non-flat roof; else levels x 3 m; else a
//               default for the building type);
//   roof_height roof rise in metres; 0 means "use the viewer's default";
//   min_height  base of the walls in metres (0 = on the ground);
//   height_source "height", "levels" or "default".
// The walls are extruded from min_height to height and a roof_height above 0
// replaces the viewer's default roof rise. Exports without these attributes
// (the bundled sample city, older runs) keep the old rule: levels times the
// floor height, the default roof rise, walls on the ground.

export const MIN_FLOOR_HEIGHT_M = 2.4;
export const MAX_FLOOR_HEIGHT_M = 6;
export const MAX_ROOF_RISE_M = 40;
/** A raised base must leave at least this much wall. */
export const MIN_WALL_M = 0.5;

const WALL_TOP_FIELDS = ['planx_height', 'height', 'building_height'];
const WALL_BASE_FIELDS = ['planx_min_height', 'min_height'];
const ROOF_RISE_FIELDS = ['planx_roof_height', 'roof_height'];

/** First non-empty value of `names` in `props`, matched case-insensitively. */
export function propValue(props, names) {
  if (!props) return null;
  const keys = {};
  for (const key of Object.keys(props)) keys[key.toLowerCase()] = key;
  for (const name of names) {
    const key = keys[name.toLowerCase()];
    if (key === undefined) continue;
    const value = props[key];
    if (value !== null && value !== undefined && String(value).trim() !== '') return value;
  }
  return null;
}

/** A finite number from the first usable field ("12,5" reads as 12.5), else null. */
export function numberProp(props, names) {
  const raw = propValue(props, names);
  if (raw === null) return null;
  const value = Number(String(raw).trim().replace(',', '.'));
  return Number.isFinite(value) ? value : null;
}

/** Wall top in metres: the explicit height, else levels times the floor height. */
export function wallTop(props, levels, floorHeight) {
  const explicit = numberProp(props, WALL_TOP_FIELDS);
  if (explicit !== null && explicit > 0) return explicit;
  const floor = Math.max(MIN_FLOOR_HEIGHT_M, Math.min(MAX_FLOOR_HEIGHT_M, Number(floorHeight) || 3));
  return Math.max(0, Number(levels) || 0) * floor;
}

/** Wall base in metres (0 = on the ground); a base at or above the wall top is ignored. */
export function wallBase(props, top) {
  const base = numberProp(props, WALL_BASE_FIELDS);
  if (base === null || !(base > 0) || !(top - base >= MIN_WALL_M)) return 0;
  return base;
}

/** Roof rise in metres: roof_height when above 0, else the viewer's default. */
export function roofRise(props, fallback) {
  const rise = numberProp(props, ROOF_RISE_FIELDS);
  if (rise !== null && rise > 0) return Math.min(rise, MAX_ROOF_RISE_M);
  return fallback;
}
