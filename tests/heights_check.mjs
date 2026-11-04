/* Pins how the viewer reads building heights (contract C3 of 1.10.0,
 * web/src/building_heights.js): walls from `min_height` to `height`, and a
 * `roof_height` above 0 replaces the viewer's default roof rise. In 1.9.1 the
 * viewer ignored min_height (a canopy on 3 m posts stood on the ground) and
 * read roof_height 0 as "flat roof" instead of "use the default".
 * Exports without the new attributes (the bundled sample city) keep the old
 * rule: levels x floor height, the default rise, walls on the ground.
 *
 *     node tests/heights_check.mjs
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

let heights;
try {
  heights = await import('../web/src/building_heights.js');
} catch (error) {
  assert.fail(`web/src/building_heights.js could not be loaded (${error.code || error.message}): `
    + '1.9.1 has no C3 height rules (min_height ignored, roof_height 0 read as a flat roof)');
}
const { roofRise, wallBase, wallTop } = heights;
const DEFAULT_RISE = 2.0; // the viewer's roofHeight setting
const FLOOR = 3.2; // the viewer's floorHeight setting

// The bundled sample city: no height, levels only.
const sample = { osm_id: 'b1', building: 'apartments', building_levels: 4, height: null };
assert.equal(wallTop(sample, 4, FLOOR), 12.8);
assert.equal(wallBase(sample, 12.8), 0);
assert.equal(roofRise(sample, DEFAULT_RISE), DEFAULT_RISE);

// C3 export: OSM height 18 m with a 3 m gabled roof -> walls 0..15, rise 3.
const gabled = { height: 15, roof_height: 3, min_height: 0, height_source: 'height', roof_shape: 'gabled' };
assert.equal(wallTop(gabled, 5, FLOOR), 15, 'height is the wall top, not levels x floor height');
assert.equal(wallBase(gabled, 15), 0);
assert.equal(roofRise(gabled, DEFAULT_RISE), 3);

// roof_height 0 = "the viewer's default", not a flat roof.
const plain = { height: 9, roof_height: 0, min_height: 0, height_source: 'levels' };
assert.equal(roofRise(plain, DEFAULT_RISE), DEFAULT_RISE);
assert.equal(roofRise({ height: 9 }, DEFAULT_RISE), DEFAULT_RISE, 'missing roof_height');
assert.equal(roofRise({ roof_height: '' }, DEFAULT_RISE), DEFAULT_RISE, 'empty roof_height');

// A roof on posts (building=roof, min_height 3, height 5): walls 3..5.
const canopy = { building: 'roof', height: 5, min_height: 3, roof_height: 0, height_source: 'height' };
assert.equal(wallTop(canopy, 1, FLOOR), 5);
assert.equal(wallBase(canopy, 5), 3);

// Bad data never sinks or inverts a building.
assert.equal(wallBase({ height: 5, min_height: 6 }, 5), 0, 'a base above the top is ignored');
assert.equal(wallBase({ height: 5, min_height: 4.8 }, 5), 0, 'a sliver of wall is ignored');
assert.equal(wallBase({ min_height: -2 }, 10), 0);
assert.equal(wallBase({ min_height: 'abc' }, 10), 0);
assert.equal(roofRise({ roof_height: 900 }, DEFAULT_RISE), heights.MAX_ROOF_RISE_M);
assert.equal(roofRise({ roof_height: -1 }, DEFAULT_RISE), DEFAULT_RISE);
assert.equal(wallTop({ height: 0 }, 2, FLOOR), 6.4, 'height 0 falls back to levels');
assert.equal(wallTop({ HEIGHT: '12,5' }, 2, FLOOR), 12.5, 'field names are case-insensitive, decimal comma');
assert.equal(wallTop({ planx_height: 30, height: 12 }, 2, FLOOR), 30, 'a PlanX height wins');
assert.equal(wallTop({}, 3, 99), 18, 'floor height clamped to 6 m');
assert.ok(Math.abs(wallTop({}, 3, 1) - 7.2) < 1e-9, 'floor height clamped to 2.4 m');

// Wiring in the viewer: the building layer uses these rules.
const app = await readFile(new URL('../web/src/app.js', import.meta.url), 'utf8');
assert.match(app, /import \{ roofRise, wallBase, wallTop \} from '\.\/building_heights\.js';/);
assert.match(app, /function buildingHeightFromProps\([^)]*\) \{\s*return wallTop\(/);
const layer = app.slice(app.indexOf('async function buildBuildingLayer('), app.indexOf('function buildZoningEnvelopesLayer('));
assert.match(layer, /const minHeight = wallBase\(props, height\);/, 'the building layer ignores min_height');
assert.match(layer, /const featureRoofHeight = roofRise\(props, fnStyle\.roofHeight\);/,
  'the building layer reads roof_height 0 as a flat roof');
assert.match(layer, /const wallBaseY = baseY \+ minHeight;\s*const extrude = volumeGeometry\(shape, wallBaseY, height - minHeight\);/,
  'the walls are not extruded from min_height to height');
assert.match(layer, /b\.position\.y = wallBaseY;/);
assert.match(layer, /minHeight > 0 && TIERED_MASSING\.has\(resolvedMassing\) \? 'extrude\+roof'/,
  'a raised building can still be split into podium/tower tiers from the ground');
assert.match(layer, /roofMeshFor\(finalTowerShape, finalTowerFootprint, roofBaseY, roofTowerHeight, roofShapeForMassing, featureRoofHeight\)/);
assert.doesNotMatch(layer, /parseNumberProp\(props, \['planx_roof_height', 'roof_height'\]/);

console.log('heights_check: OK - walls min_height..height, roof_height > 0 replaces the default rise');
