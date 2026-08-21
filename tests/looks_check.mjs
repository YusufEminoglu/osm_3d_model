/* Guards the curated look catalogue against the one failure mode it actually
 * has: a typo. A look references colour themes, asset themes, texture keys and
 * roof shapes by name, and every consumer in app.js silently ignores a name it
 * does not recognise — so a misspelt 'StandingSeem' would not throw, it would
 * just quietly do nothing and the look would render wrong. This test reads the
 * real tables out of app.js and asserts every referenced name exists.
 *
 *     node tests/looks_check.mjs
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { LOOKS, LOOK_IDS, getLook, pickLookId, TOUR_SHOTS, TOUR_DURATION, tourStateAt } from '../web/src/looks.js';

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = await readFile(join(here, '..', 'web', 'src', 'app.js'), 'utf8');

/* Brace-matched slice of an object literal's body. The tables we read contain
 * no braces inside string literals, so a plain depth counter is enough. */
function objectBody(src, header) {
  const at = src.indexOf(header);
  assert.ok(at >= 0, `app.js no longer contains ${header} — update this test`);
  const start = src.indexOf('{', at);
  let depth = 0;
  for (let i = start; i < src.length; i += 1) {
    if (src[i] === '{') depth += 1;
    else if (src[i] === '}') {
      depth -= 1;
      if (depth === 0) return src.slice(start + 1, i);
    }
  }
  throw new Error(`unbalanced braces after ${header}`);
}

function topLevelKeys(body) {
  const keys = [];
  let depth = 0;
  for (const line of body.split('\n')) {
    if (depth === 0) {
      const match = /^\s*'?([A-Za-z0-9_ &+.-]+)'?\s*:/.exec(line);
      if (match) keys.push(match[1].trim());
    }
    for (const ch of line) {
      if (ch === '{' || ch === '[') depth += 1;
      else if (ch === '}' || ch === ']') depth -= 1;
    }
  }
  return keys;
}

const colorThemes = topLevelKeys(objectBody(appSrc, 'const COLOR_THEMES'));
const assetThemes = topLevelKeys(objectBody(appSrc, 'const assetThemePresets'));
const textureSetsBody = objectBody(appSrc, 'const textureSets');
const textures = {
  road: topLevelKeys(objectBody(textureSetsBody, 'road:')),
  pavement: topLevelKeys(objectBody(textureSetsBody, 'pavement:')),
  island: topLevelKeys(objectBody(textureSetsBody, 'island:')),
  roof: topLevelKeys(objectBody(textureSetsBody, 'roof:'))
};
const roofShapes = /const ROOF_SHAPE_OPTIONS = \[([^\]]+)\]/
  .exec(appSrc)[1]
  .split(',')
  .map((item) => item.trim().replace(/^'|'$/g, ''));
const buildingModes = /buildingMode: \['([^\]]+)'\]/
  .exec(appSrc)[1]
  .split("', '");

// Sanity: the extraction itself worked before we trust any assertion built on it.
assert.ok(colorThemes.length >= 8, `parsed too few colour themes: ${colorThemes.length}`);
assert.ok(assetThemes.length >= 4, `parsed too few asset themes: ${assetThemes.length}`);
assert.ok(textures.roof.includes('StandingSeam'), 'roof texture table did not parse');
assert.deepEqual(roofShapes, ['Flat', 'Pyramid', 'Hip', 'Gable', 'Shed']);

assert.equal(LOOKS.length, 10, 'the shuffle promises ten curated looks');
assert.equal(new Set(LOOK_IDS).size, LOOKS.length, 'look ids must be unique');
assert.equal(new Set(LOOKS.map((l) => l.name)).size, LOOKS.length, 'look names must be unique');

const seenThemes = new Set();
for (const look of LOOKS) {
  const where = `look "${look.id}"`;
  assert.match(look.id, /^[a-z0-9-]+$/, `${where}: id must be kebab-case`);
  assert.ok(look.name && look.blurb, `${where}: needs a name and a blurb`);
  assert.equal(look.swatch.length, 4, `${where}: swatch must be four colours`);
  look.swatch.forEach((hex) => assert.match(hex, /^#[0-9a-f]{6}$/, `${where}: bad swatch ${hex}`));

  assert.ok(colorThemes.includes(look.colorTheme), `${where}: unknown colorTheme ${look.colorTheme}`);
  seenThemes.add(look.colorTheme);

  const s = look.settings;
  assert.ok(assetThemes.includes(s.assetTheme), `${where}: unknown assetTheme ${s.assetTheme}`);
  assert.ok(textures.road.includes(s.roadStyle), `${where}: unknown roadStyle ${s.roadStyle}`);
  assert.ok(textures.pavement.includes(s.pavementStyle), `${where}: unknown pavementStyle ${s.pavementStyle}`);
  assert.ok(textures.island.includes(s.islandTexture), `${where}: unknown islandTexture ${s.islandTexture}`);
  assert.ok(textures.roof.includes(s.roofTexture), `${where}: unknown roofTexture ${s.roofTexture}`);
  assert.ok(roofShapes.includes(s.roofShape), `${where}: unknown roofShape ${s.roofShape}`);
  assert.ok(buildingModes.includes(s.buildingMode), `${where}: unknown buildingMode ${s.buildingMode}`);
  assert.ok(['Clear', 'Rain', 'Snow'].includes(s.weather), `${where}: unknown weather ${s.weather}`);
  assert.ok(['Stylized', 'Realistic'].includes(s.treeRenderMode), `${where}: unknown treeRenderMode`);

  // Ranges must match the dock sliders, otherwise applying a look would push a
  // control past its own min/max and the slider would misreport the state.
  assert.ok(s.timeOfDay >= 0 && s.timeOfDay <= 24, `${where}: timeOfDay out of range`);
  assert.ok(s.fogDensity >= 0.0001 && s.fogDensity <= 0.002, `${where}: fogDensity outside the dock slider range`);
  assert.ok(s.roofHeight >= 0.5 && s.roofHeight <= 6, `${where}: roofHeight outside the dock slider range`);
  assert.ok(s.buildingSetback >= 0 && s.buildingSetback <= 5, `${where}: buildingSetback outside the dock slider range`);
  assert.ok(s.ledgeProjection >= 0.05 && s.ledgeProjection <= 0.6, `${where}: ledgeProjection outside the dock slider range`);

  // SSAO is opt-in because it crushes a map-scale scene to near black; a
  // one-click look must never turn it on for the user.
  assert.equal(s.enableSSAO, false, `${where}: looks must not enable SSAO`);

  // A look never touches layer visibility: re-enabling heavy layers behind the
  // user's back costs frames and is not what "change the look" means.
  Object.keys(s).forEach((key) => {
    assert.ok(!/^show(Buildings|Roads|Trees|Cars|Pedestrians|Bikes|Furniture|Islands|Sidewalks|Waterlines|BikeLanes)$/.test(key),
      `${where}: looks must not toggle layer visibility (${key})`);
  });
}
assert.ok(seenThemes.size >= 8, 'the ten looks should span most of the colour themes, not repeat two');

// getLook / pickLookId
assert.equal(getLook('editorial-dusk').name, 'Editorial Dusk');
assert.equal(getLook('nope'), null);
assert.notEqual(pickLookId(LOOK_IDS[0], () => 0), LOOK_IDS[0], 'shuffle must never return the current look');
for (const id of LOOK_IDS) {
  for (const r of [0, 0.25, 0.5, 0.999999, 1]) {
    const picked = pickLookId(id, () => r);
    assert.ok(LOOK_IDS.includes(picked), `pickLookId returned ${picked}`);
    assert.notEqual(picked, id);
  }
}

// Tour choreography
assert.ok(TOUR_SHOTS.length >= 4, 'a tour needs several shots');
assert.ok(Math.abs(TOUR_DURATION - TOUR_SHOTS.reduce((a, s) => a + s.duration, 0)) < 1e-9);
assert.ok(TOUR_DURATION > 20 && TOUR_DURATION < 90, `tour length ${TOUR_DURATION}s is not a usable clip`);
for (let i = 1; i < TOUR_SHOTS.length; i += 1) {
  assert.ok(TOUR_SHOTS[i].azimuth > TOUR_SHOTS[i - 1].azimuth,
    'azimuths must increase monotonically so the camera never snaps back through 0');
}
const first = tourStateAt(0);
assert.equal(first.shotIndex, 0);
assert.equal(first.azimuth, TOUR_SHOTS[0].azimuth);
assert.equal(first.progress, 0);
const last = tourStateAt(TOUR_DURATION);
assert.equal(last.progress, 1);
assert.equal(Math.round(last.azimuth), TOUR_SHOTS[TOUR_SHOTS.length - 1].azimuth);
assert.deepEqual(tourStateAt(-5), tourStateAt(0), 'time before the start clamps to the first frame');
assert.deepEqual(tourStateAt(TOUR_DURATION + 99), last, 'time past the end clamps to the last frame');
// Continuity: no jump larger than a few degrees between consecutive samples.
let prev = tourStateAt(0);
for (let t = 0.05; t <= TOUR_DURATION; t += 0.05) {
  const now = tourStateAt(t);
  assert.ok(now.azimuth >= prev.azimuth - 1e-9, `azimuth went backwards at t=${t.toFixed(2)}`);
  assert.ok(now.azimuth - prev.azimuth < 4, `azimuth jumped at t=${t.toFixed(2)}`);
  assert.ok(Math.abs(now.radius - prev.radius) < 0.1, `radius jumped at t=${t.toFixed(2)}`);
  assert.ok(now.radius > 0 && now.height > 0, `camera fell through the ground at t=${t.toFixed(2)}`);
  assert.ok(now.fov >= 30 && now.fov <= 90, `fov out of range at t=${t.toFixed(2)}`);
  prev = now;
}

console.log(`looks_check: OK — ${LOOKS.length} looks, ${TOUR_SHOTS.length} tour shots, ${TOUR_DURATION}s`);
