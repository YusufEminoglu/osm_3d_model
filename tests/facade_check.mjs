/* Pins the dynamic facade grid (web/src/facade_dynamic.js): every wall gets a
 * whole number of window bays that restarts at its corners, rows sit on the
 * floors, walls face out of the footprint (courtyards into the courtyard),
 * and the automatic facade follows the OSM building category with names from
 * the shared material catalogue.
 *
 *     node tests/facade_check.mjs
 */
import assert from 'node:assert/strict';
import * as THREE from '../web/assets/vendor/three/build/three.module.js';
import { prismGeometry, autoFacadeName, dynamicStyleOfToken } from '../web/src/facade_dynamic.js';
import { APARTMENT_TEXTURES, FACADES, FACADE_TEXTURE_GRID, catalogueName } from '../web/src/material_catalogue.js';

const rect = (w, d) => new THREE.Shape([new THREE.Vector2(0, 0), new THREE.Vector2(w, 0), new THREE.Vector2(w, d), new THREE.Vector2(0, d)]);
const walls = (geo) => {
  const g = geo.groups[1];
  const P = geo.attributes.position.array;
  const N = geo.attributes.normal.array;
  const U = geo.attributes.uv.array;
  const out = [];
  for (let i = g.start; i < g.start + g.count; i++) {
    out.push({ x: P[i * 3], z: P[i * 3 + 2], nx: N[i * 3], nz: N[i * 3 + 2], u: U[i * 2], v: U[i * 2 + 1] });
  }
  return out;
};
const values = (list, key) => [...new Set(list.map((p) => +p[key].toFixed(6)))].sort((a, b) => a - b);

// A 10 x 6 m block, 9 m high, 3 m bays and floors: 3 and 2 bays, 3 floors.
const box = walls(prismGeometry(rect(10, 6), 9, { bay: 3, floor: 3, z0: 0 }));
assert.deepEqual(values(box.filter((p) => Math.abs(p.nz) > 0.9), 'u'), [0, 3]);
assert.deepEqual(values(box.filter((p) => Math.abs(p.nx) > 0.9), 'u'), [0, 2]);
assert.deepEqual(values(box, 'v'), [0, 3]);

// Either winding: normals point out.
for (const shape of [rect(10, 6), new THREE.Shape(rect(10, 6).getPoints().reverse())]) {
  for (const p of walls(prismGeometry(shape, 3, { bay: 3, floor: 3 }))) {
    assert.ok(p.nx * (p.x - 5) + p.nz * (p.z - 3) > 0, 'wall faces into the building');
  }
}

// Courtyard walls face the courtyard; an upper volume continues the floors.
const court = rect(30, 30);
court.holes.push(new THREE.Path([new THREE.Vector2(10, 10), new THREE.Vector2(20, 10), new THREE.Vector2(20, 20), new THREE.Vector2(10, 20)]));
const cw = walls(prismGeometry(court, 6, { bay: 3, floor: 3, z0: 6 }));
for (const p of cw.filter((q) => q.x > 9 && q.x < 21 && q.z > 9 && q.z < 21)) {
  assert.ok(p.nx * (p.x - 15) + p.nz * (p.z - 15) < 0, 'courtyard wall faces away from the courtyard');
}
assert.deepEqual(values(cw, 'v'), [2, 4]);

// Automatic facades from OSM categories: dynamic styles by type, the photo
// apartment facades (or villas when low) for homes.
assert.equal(autoFacadeName('Office', 6, 1), 'Office');
assert.ok(['MixedUse', 'Modern'].includes(autoFacadeName('Commercial', 3, 2)));
assert.ok(['Civic', 'Heritage'].includes(autoFacadeName('Worship', 2, 3)));
assert.equal(autoFacadeName('Industrial', 2, 4), 'Industrial');
const homes = new Set();
for (let seed = 0; seed < 60; seed++) {
  const name = autoFacadeName('Residential', 5, seed);
  assert.ok(FACADES[name], name);
  homes.add(name);
}
assert.ok(APARTMENT_TEXTURES.every((name) => homes.has(name)), 'every apartment photo facade is reached');
for (const [name, look] of Object.entries(FACADES)) {
  assert.ok(dynamicStyleOfToken(look) || FACADE_TEXTURE_GRID[name], `${name}: neither dynamic nor a photo facade with a window grid`);
}

// Legacy names still load as the entry chosen for them.
assert.equal(catalogueName('facade', 'ResidentialC'), 'ApartmentTextureC');
assert.equal(catalogueName('facade', 'curtainwall'), 'Office');
assert.equal(catalogueName('roof', 'TurkishTile'), 'ClayTile');
assert.equal(catalogueName('surface', 'PlazaGranite'), 'GraniteSlab');
assert.equal(catalogueName('facade', 'NoSuchFacade'), null);

console.log('facade_check: ok');
