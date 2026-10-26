// Dynamic facades: windows, frames, sills, shutters, balconies, shopfronts
// and entrances drawn in the fragment shader on a bay x floor grid that
// prismGeometry() puts on every wall (u = window bays along the wall, a
// whole number per wall restarting at each corner; v = floors from the
// ground). Only the wall material (plaster, render, brick, stone, concrete)
// is a texture. Each building varies its window proportions, balconies,
// doors and blinds from its seed; windows never cross a corner and sit on
// the real floors; at night a share of them is lit.
//
// Shared with the PlanX 3D City viewer. Technique after streets.gl
// (bay/floor coordinates per wall, MIT), OSM2World and CityEngine CGA (whole
// repeats per wall) and Mapbox's faux facades (windows in the fragment
// shader); no code taken from them.
//
// three is imported by path so node tests can load this module; the URL is
// the one the page's import map gives 'three', so there is one instance.
import * as THREE from '../assets/vendor/three/build/three.module.js';

/**
 * @typedef {{ material: 'plaster' | 'render' | 'brick' | 'stone' | 'concrete', winW: number, winH: number,
 *   sill: number, balcony: number, ground: 'residential' | 'shops' | 'lobby', tint: number, shutters?: boolean,
 *   ribbon?: boolean, frame: string, glass: string, accent: string, bay: number }} DynamicFacadeStyle
 * winW / winH / sill: window width, height and sill height as shares of a bay and a floor;
 * balcony: share of upper-floor bays with a balcony; tint: how much the building colour tints
 * the wall material; bay: preferred bay width (m).
 */

/** @type {Record<string, DynamicFacadeStyle>} */
export const DYNAMIC_FACADE_STYLES = {
  Apartment: { material: 'plaster', winW: 0.4, winH: 0.5, sill: 0.3, balcony: 0.45, ground: 'residential', tint: 1, frame: '#f2f0ea', glass: '#26323c', accent: '#d8d4cb', bay: 3.1 },
  MixedUse: { material: 'plaster', winW: 0.44, winH: 0.54, sill: 0.28, balcony: 0.2, ground: 'shops', tint: 1, frame: '#3a3e44', glass: '#25313b', accent: '#9a3b2c', bay: 3.2 },
  Modern: { material: 'render', winW: 0.66, winH: 0.58, sill: 0.24, balcony: 0.55, ground: 'shops', tint: 1, frame: '#2b2e33', glass: '#22303a', accent: '#c9ced3', bay: 3.4 },
  Office: { material: 'concrete', winW: 0.88, winH: 0.6, sill: 0.24, balcony: 0, ground: 'lobby', tint: 0.5, ribbon: true, frame: '#9aa3ad', glass: '#2c4152', accent: '#5b6570', bay: 3.0 },
  Heritage: { material: 'stone', winW: 0.34, winH: 0.6, sill: 0.22, balcony: 0.12, ground: 'shops', tint: 0.45, shutters: true, frame: '#ece7dc', glass: '#28333b', accent: '#3d5c49', bay: 3.0 },
  Brick: { material: 'brick', winW: 0.38, winH: 0.54, sill: 0.28, balcony: 0.15, ground: 'residential', tint: 0.25, frame: '#ece8df', glass: '#27323a', accent: '#efe9dd', bay: 3.0 }
};

export const FACADE_MODE_OPTIONS = ['Dynamic (auto)', 'Catalogue textures'];

/** Catalogue key of a dynamic style ('Apartment' -> 'DynamicApartment'). */
export function dynamicFacadeKey(style) {
  return `Dynamic${style}`;
}

/** The style behind a catalogue token 'Dynamic:<style>', or null. */
export function dynamicStyleOfToken(token) {
  const m = /^Dynamic:(\w+)$/.exec(String(token || ''));
  return m && DYNAMIC_FACADE_STYLES[m[1]] ? m[1] : null;
}

/**
 * Pick a dynamic style for a building from its function and height, varied
 * per building (seed).
 * @param {string} fn building function
 * @param {number} levels
 * @param {number} seed
 */
export function autoDynamicStyle(fn, levels, seed) {
  const f = String(fn || '').toLowerCase();
  const r = Math.abs(Math.sin(seed * 91.345 + 7.13) * 43758.5453) % 1;
  if (/office|ofis|admin|idari|bank|corporate|hotel|otel/.test(f)) return levels >= 4 ? 'Office' : 'MixedUse';
  if (/commerc|retail|ticaret|mixed|karma|shop|avm|market/.test(f)) return levels >= 9 ? 'Office' : (r < 0.7 ? 'MixedUse' : 'Modern');
  if (/civic|public|kamu|cultur|kultur|relig|worship|museum|school|educat|egitim|health|saglik|hospital/.test(f)) return r < 0.6 ? 'Heritage' : 'Brick';
  if (/indust|sanayi|warehouse|depo|factory|utility/.test(f)) return r < 0.5 ? 'Brick' : 'Office';
  if (levels >= 10) return r < 0.6 ? 'Modern' : 'Apartment';
  if (r < 0.5) return 'Apartment';
  if (r < 0.75) return 'Modern';
  if (r < 0.9) return 'Brick';
  return 'Heritage';
}

// Walls turn a corner where the outline bends by more than this (radians);
// gentler bends (curved walls, digitising noise) continue the same facade.
const WALL_CORNER_ANGLE = 0.3;

/**
 * Prism over a shape (x, z = shape x, y) from y = 0 to `depth`, like a
 * rotated ExtrudeGeometry, but with facade texture coordinates in building units on the
 * walls: u counts window bays along each wall, v counts floors from the
 * building's ground. Every wall (outline between two corners) gets a whole
 * number of bays of about frame.bay metres and restarts at its corner, so
 * windows are never cut by a corner and rows sit on the floors.
 * Groups: 0 = caps (u, v = x, z in metres, as ExtrudeGeometry), 1 = walls.
 * frame: { bay: metres per bay, floor: metres per floor, z0: height of
 * y = 0 above the building's ground }.
 */
export function prismGeometry(shape, depth, frame) {
  const { shape: outer, holes } = shape.extractPoints(1);
  const clean = (pts) => {
    const out = [];
    for (const p of pts) {
      const last = out[out.length - 1];
      if (!last || Math.hypot(p.x - last.x, p.y - last.y) > 1e-4) out.push(p);
    }
    if (out.length > 2 && Math.hypot(out[0].x - out[out.length - 1].x, out[0].y - out[out.length - 1].y) < 1e-4) out.pop();
    return out;
  };
  const contour = clean(outer);
  const holeRings = holes.map(clean).filter((h) => h.length >= 3);
  if (contour.length < 3) return null;
  const pos = [];
  const nor = [];
  const uv = [];

  // Caps: top faces up, bottom faces down.
  let tris;
  try {
    tris = THREE.ShapeUtils.triangulateShape(contour, holeRings);
  } catch {
    return null;
  }
  const all = contour.concat(...holeRings);
  for (const [y, up] of [[depth, 1], [0, -1]]) {
    for (const t of tris) {
      const [a, b, c] = t.map((i) => all[i]);
      // Normal y of (b - a) x (c - a) in (x, y, z) with z = shape y.
      const ny = (c.x - a.x) * (b.y - a.y) - (b.x - a.x) * (c.y - a.y);
      const order = (ny >= 0) === (up > 0) ? [a, b, c] : [a, c, b];
      for (const p of order) {
        pos.push(p.x, y, p.y);
        nor.push(0, up, 0);
        uv.push(p.x, p.y);
      }
    }
  }
  const capCount = pos.length / 3;

  const bay = Math.max(0.5, frame.bay || 3);
  const floorH = Math.max(1, frame.floor || 3);
  const v0 = (frame.z0 || 0) / floorH;
  const v1 = ((frame.z0 || 0) + depth) / floorH;
  const ringWalls = (ring, isHole) => {
    const n = ring.length;
    let area = 0;
    for (let i = 0; i < n; i++) {
      const a = ring[i];
      const b = ring[(i + 1) % n];
      area += a.x * b.y - b.x * a.y;
    }
    // Outward: right of the edge for a counter-clockwise outline, left
    // for a hole (the building is outside it).
    const outSign = (area >= 0 ? 1 : -1) * (isHole ? -1 : 1);
    const turn = (i) => {
      const p = ring[(i - 1 + n) % n];
      const c = ring[i];
      const q = ring[(i + 1) % n];
      const a1 = Math.atan2(c.y - p.y, c.x - p.x);
      const a2 = Math.atan2(q.y - c.y, q.x - c.x);
      return Math.abs(Math.atan2(Math.sin(a2 - a1), Math.cos(a2 - a1)));
    };
    const corners = [];
    for (let i = 0; i < n; i++) if (turn(i) > WALL_CORNER_ANGLE) corners.push(i);
    if (!corners.length) corners.push(0);
    for (let k = 0; k < corners.length; k++) {
      const start = corners[k];
      const end = corners[(k + 1) % corners.length];
      const edges = [];
      let len = 0;
      for (let i = start; ; i = (i + 1) % n) {
        const a = ring[i];
        const b = ring[(i + 1) % n];
        const l = Math.hypot(b.x - a.x, b.y - a.y);
        edges.push({ a, b, s: len, l });
        len += l;
        if ((i + 1) % n === end) break;
      }
      // Too short for a window: plain wall (u on a cell edge, between windows).
      const bays = Math.max(1, Math.round(len / bay));
      const scale = len < bay * 0.55 ? 0 : bays / len;
      for (const { a, b, s, l } of edges) {
        if (l < 1e-6) continue;
        const dx = (b.x - a.x) / l;
        const dz = (b.y - a.y) / l;
        const nx = dz * outSign;
        const nz = -dx * outSign;
        const ua = scale ? s * scale : 0.02;
        const ub = scale ? (s + l) * scale : 0.02;
        const A0 = [a.x, 0, a.y], B0 = [b.x, 0, b.y], B1 = [b.x, depth, b.y], A1 = [a.x, depth, a.y];
        const uvA0 = [ua, v0], uvB0 = [ub, v0], uvB1 = [ub, v1], uvA1 = [ua, v1];
        // Wind so the face normal points out (n): (B0 - A0) x (A1 - A0) = (dz, 0, -dx) * l * depth.
        const quad = outSign > 0
          ? [[A0, uvA0], [A1, uvA1], [B0, uvB0], [B0, uvB0], [A1, uvA1], [B1, uvB1]]
          : [[A0, uvA0], [B0, uvB0], [A1, uvA1], [B0, uvB0], [B1, uvB1], [A1, uvA1]];
        for (const [p, t] of quad) {
          pos.push(p[0], p[1], p[2]);
          nor.push(nx, 0, nz);
          uv.push(t[0], t[1]);
        }
      }
    }
  };
  ringWalls(contour, false);
  holeRings.forEach((h) => ringWalls(h, true));

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute('normal', new THREE.Float32BufferAttribute(nor, 3));
  geo.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  geo.addGroup(0, capCount, 0);
  geo.addGroup(capCount, pos.length / 3 - capCount, 1);
  return geo;
}


// --- wall materials ----------------------------------------------------------

let textureAnisotropy = 1;

// Tileable value noise on a size x size grid (wraps at the edges).
function valueNoise(size, cells, seed) {
  let s = seed >>> 0;
  const rand = () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
  const lattice = new Float32Array(cells * cells);
  for (let i = 0; i < lattice.length; i++) lattice[i] = rand();
  const out = new Float32Array(size * size);
  const k = cells / size;
  for (let y = 0; y < size; y++) {
    const gy = y * k;
    const y0 = Math.floor(gy);
    const fy = gy - y0;
    const sy = fy * fy * (3 - 2 * fy);
    const r0 = (y0 % cells) * cells;
    const r1 = ((y0 + 1) % cells) * cells;
    for (let x = 0; x < size; x++) {
      const gx = x * k;
      const x0 = Math.floor(gx);
      const fx = gx - x0;
      const sx = fx * fx * (3 - 2 * fx);
      const c0 = x0 % cells;
      const c1 = (x0 + 1) % cells;
      const a = lattice[r0 + c0] + (lattice[r0 + c1] - lattice[r0 + c0]) * sx;
      const b = lattice[r1 + c0] + (lattice[r1 + c1] - lattice[r1 + c0]) * sx;
      out[y * size + x] = a + (b - a) * sy;
    }
  }
  return out;
}

function fbm(size, octaves, seed) {
  const out = new Float32Array(size * size);
  let amp = 0.5;
  let total = 0;
  for (const cells of octaves) {
    const n = valueNoise(size, cells, seed + cells * 7919);
    for (let i = 0; i < out.length; i++) out[i] += n[i] * amp;
    total += amp;
    amp *= 0.55;
  }
  for (let i = 0; i < out.length; i++) out[i] /= total;
  return out;
}

function seededRandom(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

const MATERIAL_TILE_M = { plaster: 2.4, render: 2.4, brick: 1.2, stone: 2.4, concrete: 3.0 };

function paint(size, fill) {
  const c = document.createElement('canvas');
  c.width = size;
  c.height = size;
  const ctx = c.getContext('2d');
  const img = ctx.createImageData(size, size);
  fill(img.data);
  ctx.putImageData(img, 0, 0);
  const t = new THREE.CanvasTexture(c);
  t.wrapS = THREE.RepeatWrapping;
  t.wrapT = THREE.RepeatWrapping;
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = textureAnisotropy;
  return t;
}

const clamp255 = (v) => Math.max(0, Math.min(255, Math.round(v * 255)));

// Tileable wall material, near white for plaster/render/concrete (the
// building colour tints it) and in its own colours for brick and stone.
function materialTexture(kind) {
  const size = 512;
  const rand = seededRandom(kind.length * 977 + 13);
  const big = fbm(size, [4, 8, 16], 31);
  const fine = fbm(size, [64, 128], 47);
  const pxPerM = size / MATERIAL_TILE_M[kind];
  if (kind === 'brick') {
    const bw = 0.25 * pxPerM;
    const bh = 0.075 * pxPerM;
    const rows = Math.round(size / bh);
    const cols = Math.round(size / bw);
    const tones = Array.from({ length: rows * cols }, () => [0.55 + rand() * 0.2, 0.25 + rand() * 0.1, 0.18 + rand() * 0.08]);
    return paint(size, (px) => {
      for (let y = 0; y < size; y++) {
        const row = Math.floor(y / bh) % rows;
        const fy = y - Math.floor(y / bh) * bh;
        for (let x = 0; x < size; x++) {
          const xs = (x + (row % 2) * bw * 0.5) % size;
          const col = Math.floor(xs / bw) % cols;
          const fx = xs - Math.floor(xs / bw) * bw;
          const mortar = fx < 1.6 || fy < 1.6;
          const i = y * size + x;
          const n = (fine[i] - 0.5) * 0.12 + (big[i] - 0.5) * 0.08;
          const t = tones[row * cols + col];
          const rgb = mortar ? [0.72 + n, 0.69 + n, 0.64 + n] : [t[0] + n, t[1] + n, t[2] + n];
          px.set([clamp255(rgb[0]), clamp255(rgb[1]), clamp255(rgb[2]), 255], i * 4);
        }
      }
    });
  }
  if (kind === 'stone') {
    const bh = 0.36 * pxPerM;
    const rows = Math.round(size / bh);
    // Ashlar: courses of blocks of varying length.
    const joints = Array.from({ length: rows }, () => {
      const xs = [];
      let x = rand() * 40;
      while (x < size) {
        xs.push(x);
        x += (0.5 + rand() * 0.5) * pxPerM;
      }
      return xs;
    });
    const tones = Array.from({ length: rows * 16 }, () => 0.86 + rand() * 0.12);
    return paint(size, (px) => {
      for (let y = 0; y < size; y++) {
        const row = Math.floor(y / bh) % rows;
        const fy = y - Math.floor(y / bh) * bh;
        for (let x = 0; x < size; x++) {
          let k = 0;
          let edge = Infinity;
          joints[row].forEach((jx, j) => {
            edge = Math.min(edge, Math.abs(x - jx));
            if (x >= jx) k = j;
          });
          const joint = edge < 1.5 || fy < 1.5;
          const i = y * size + x;
          const n = (fine[i] - 0.5) * 0.1 + (big[i] - 0.5) * 0.1;
          const t = tones[(row * 16 + k) % tones.length];
          const rgb = joint ? [0.6, 0.57, 0.52] : [0.93 * t + n, 0.88 * t + n, 0.78 * t + n];
          px.set([clamp255(rgb[0]), clamp255(rgb[1]), clamp255(rgb[2]), 255], i * 4);
        }
      }
    });
  }
  const panel = kind === 'concrete' ? 1.5 * pxPerM : 0;
  const strength = kind === 'render' ? 0.05 : kind === 'concrete' ? 0.12 : 0.09;
  return paint(size, (px) => {
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const i = y * size + x;
        // Rain streaks: darker vertical runs from the big noise.
        const streak = (big[(x * 7) % size] - 0.5) * 0.06 * (y / size);
        let v = 0.9 + (big[i] - 0.5) * strength + (fine[i] - 0.5) * strength * 0.8 - streak;
        if (panel && ((x % panel) < 1.5 || (y % panel) < 1.5)) v *= 0.72;
        px.set([clamp255(v), clamp255(v * 0.995), clamp255(v * 0.985), 255], i * 4);
      }
    }
  });
}

const textureCache = new Map();
function materialFor(styleName) {
  const style = DYNAMIC_FACADE_STYLES[styleName] || DYNAMIC_FACADE_STYLES.Apartment;
  if (!textureCache.has(style.material)) textureCache.set(style.material, materialTexture(style.material));
  return textureCache.get(style.material);
}

const linear = (hex) => {
  const c = new THREE.Color(hex);
  return new THREE.Vector3(c.r, c.g, c.b);
};
const GROUND_KIND = { residential: 0, shops: 1, lobby: 2 };

// Declarations (fragment, after <common>).
export const DYNAMIC_FACADE_PARS = `
uniform vec4 uDynA;
uniform vec4 uDynB;
uniform vec2 uDynMat;
uniform vec3 uDynFrame;
uniform vec3 uDynGlass;
uniform vec3 uDynAccent;
uniform float uDynSeed;
uniform float uDynNight;
float planxDynWall = 1.0;
float planxDynGlass = 0.0;
vec3 planxDynLit = vec3(0.0);
float planxDynBox(vec2 p, vec2 c, vec2 halfSize) { vec2 d = abs(p - c) - halfSize; return max(d.x, d.y); }
float planxDynFill(float sd, float aa) { return smoothstep(aa, -aa, sd); }
// Hash without sin(): stable for the moderate inputs used here.
float planxDynHash(vec2 p) {
  vec3 q = fract(vec3(p.xyx) * 0.1031);
  q += dot(q, q.yzx + 33.33);
  return fract((q.x + q.y) * q.z);
}
// Thin bars across a coordinate (count per unit), anti-aliased, fading to
// their average coverage once they get finer than a pixel.
float planxDynBars(float x, float count, float width) {
  float t = x * count;
  float w = fwidth(t) + 1e-4;
  float d = abs(fract(t) - 0.5) - (0.5 - width);
  float bars = smoothstep(-w, w, d);
  return mix(bars, width * 2.0, smoothstep(0.35, 0.8, w));
}
`;

// Replaces <map_fragment>: the facade colour of this fragment from the
// bay/floor coordinates (vMapUv). The material colour (the building colour,
// already in diffuseColor) tints the wall only; also sets the glass mask and
// the window light for the night.
export const DYNAMIC_FACADE_FRAGMENT = `
vec2 fUv = vMapUv;
vec2 fCell = floor(fUv);
vec2 fLoc = fUv - fCell;
vec2 fW = fwidth(fUv);
float fAA = max(fW.x, fW.y) * 0.8 + 1e-4;
float bSeed = uDynSeed * 1.618 + 0.5;
float bH1 = planxDynHash(vec2(bSeed, 3.1));
float bH2 = planxDynHash(vec2(bSeed, 7.7));
float bH3 = planxDynHash(vec2(bSeed, 11.3));
float cH = planxDynHash(fCell * 1.37 + vec2(bSeed, bSeed * 0.71));
vec3 wallCol = texture2D(map, fUv * uDynMat).rgb;
bool fGround = fCell.y < 0.5;
float ribbon = uDynB.w;
// Window: centred in the bay, proportions varied per building.
float ww = clamp(uDynA.x + (bH1 - 0.5) * 0.12, 0.2, 0.94);
float wh = clamp(uDynA.y + (bH2 - 0.5) * 0.1, 0.3, 0.8);
float sill = uDynA.z;
float door = 0.0;
float shop = 0.0;
// Balconies on upper floors: whole bay columns (a stack per building).
float balcony = (!fGround && planxDynHash(vec2(fCell.x * 2.1 + 0.3, bSeed)) < uDynA.w) ? 1.0 : 0.0;
if (fGround) {
  if (uDynB.x > 1.5) { ww = 0.9; wh = 0.76; sill = 0.03; shop = 1.0; }
  else if (uDynB.x > 0.5) {
    door = step(0.82, planxDynHash(vec2(fCell.x * 1.7 + 5.0, bSeed)));
    ww = mix(0.84, 0.36, door); wh = mix(0.64, 0.72, door); sill = mix(0.07, 0.0, door); shop = 1.0;
  } else {
    door = step(0.8, planxDynHash(vec2(fCell.x * 1.3 + 9.0, bSeed)));
    ww = mix(ww, 0.34, door); wh = mix(wh - 0.06, 0.74, door); sill = mix(sill + 0.06, 0.0, door);
  }
} else if (balcony > 0.5) {
  // A balcony door: full height opening.
  sill = 0.04; wh = 0.72; ww = max(ww, 0.42);
}
vec2 wC = vec2(0.5, sill + wh * 0.5);
vec2 wHalf = vec2(ww, wh) * 0.5;
float opening = planxDynFill(planxDynBox(fLoc, wC, wHalf), fAA);
float frameW = mix(0.035, 0.02, ribbon);
float glassIn = planxDynFill(planxDynBox(fLoc, wC, wHalf - frameW), fAA);
// Mullion and transom on wide windows; shopfronts get slim bars.
float bars = 0.0;
if (ww > 0.5 || shop > 0.5) bars = planxDynFill(abs(fLoc.x - 0.5) - 0.01, fAA);
if (!fGround && wh > 0.5) bars = max(bars, planxDynFill(abs(fLoc.y - (sill + wh * 0.72)) - 0.009, fAA) * step(0.5, ww));
float glass = glassIn * (1.0 - bars);
float frame = opening * (1.0 - glass);
// Glass: sky reflection brighter towards the top, darker room behind,
// the head of the opening in shadow (recess).
float gy = clamp((fLoc.y - (wC.y - wHalf.y)) / max(0.01, wh), 0.0, 1.0);
vec3 glassCol = uDynGlass * (0.65 + 0.7 * cH) * (0.7 + 0.6 * gy);
glassCol *= 1.0 - 0.5 * smoothstep(0.8, 1.0, gy);
if (door > 0.5) glassCol = mix(uDynFrame * 0.35, glassCol, 0.35);
vec3 col = wallCol;
float wallMask = 1.0;
// Ribbon facades: spandrel panels between the window bands.
if (ribbon > 0.5 && !fGround) {
  float spandrel = 1.0 - planxDynFill(abs(fLoc.y - wC.y) - wHalf.y - 0.05, fAA);
  col = mix(col, uDynAccent * (0.9 + 0.2 * bH3), spandrel * 0.85);
  wallMask *= 1.0 - spandrel * 0.85;
}
// Reveal: shadow along the head and one jamb of the opening.
float reveal = planxDynFill(planxDynBox(fLoc, wC + vec2(0.012, 0.016), wHalf + 0.012), fAA) * (1.0 - opening);
col *= 1.0 - 0.35 * reveal;
// Sill or lintel band.
if (shop < 0.5 && door < 0.5) {
  float sillBand = planxDynFill(planxDynBox(fLoc, vec2(0.5, sill - 0.018), vec2(wHalf.x + 0.03, 0.016)), fAA);
  col = mix(col, uDynFrame * 0.92, sillBand);
  wallMask *= 1.0 - sillBand;
}
// Shutters beside the windows.
if (uDynB.z > 0.5 && !fGround && ww < 0.5) {
  float shW = ww * 0.5;
  float sh = planxDynFill(planxDynBox(vec2(abs(fLoc.x - 0.5), fLoc.y), vec2(wHalf.x + shW * 0.5 + 0.01, wC.y), vec2(shW * 0.5, wHalf.y)), fAA);
  float slats = 0.85 + 0.15 * planxDynBars(fLoc.y, 40.0, 0.25);
  col = mix(col, uDynAccent * slats, sh);
  wallMask *= 1.0 - sh;
}
col = mix(col, uDynFrame, frame);
col = mix(col, glassCol, glass);
wallMask *= 1.0 - opening;
// Shop sign band above the shopfronts.
if (shop > 0.5 && uDynB.x < 1.5) {
  float sign = planxDynFill(planxDynBox(fLoc, vec2(0.5, 0.86), vec2(0.46, 0.06)), fAA);
  vec3 signCol = mix(uDynAccent, vec3(0.12, 0.2, 0.32), step(0.5, planxDynHash(vec2(fCell.x + 13.0, bSeed * 3.0))));
  col = mix(col, signCol, sign * (1.0 - door));
  wallMask *= 1.0 - sign * (1.0 - door);
}
// Balcony: slab edge, railing and its shadow on the wall.
if (balcony > 0.5) {
  float slab = planxDynFill(planxDynBox(fLoc, vec2(0.5, 0.02), vec2(0.47, 0.02)), fAA);
  float rail = planxDynFill(planxDynBox(fLoc, vec2(0.5, 0.2), vec2(0.47, 0.16)), fAA);
  float barsV = planxDynBars(fLoc.x, 22.0, 0.12);
  float railTop = planxDynFill(abs(fLoc.y - 0.36) - 0.008, fAA) * planxDynFill(abs(fLoc.x - 0.5) - 0.47, fAA);
  float shadow = planxDynFill(planxDynBox(fLoc, vec2(0.5, 0.97), vec2(0.47, 0.03)), fAA);
  col *= 1.0 - 0.3 * shadow;
  col = mix(col, uDynFrame * 0.55, rail * barsV * 0.7 + railTop);
  col = mix(col, uDynFrame * 0.95, slab);
  wallMask *= 1.0 - max(slab, railTop);
  glass *= 1.0 - rail * barsV * 0.7;
}
// Floor line (string course) on stone and concrete.
if (uDynB.y < 0.6) {
  float course = planxDynFill(abs(fLoc.y) - 0.012, fAA) * (1.0 - opening);
  col *= 1.0 - 0.18 * course;
}
// Far away a bay is a few pixels: fade to the average to avoid shimmer.
float far = smoothstep(0.2, 0.5, max(fW.x, fW.y));
vec3 avg = mix(wallCol, uDynGlass * 0.9, ww * wh * 0.8);
col = mix(col, avg, far);
wallMask = mix(wallMask, 1.0 - ww * wh * 0.8, far);
glass *= 1.0 - far * 0.6;
// By day about a quarter of the windows show blinds or curtains; at night
// a share of them is lit, warm or cool.
float cH2 = planxDynHash(fCell * 2.11 + vec2(bSeed * 0.37, 4.0));
float blind = step(0.74, cH2) * glass;
vec3 cloth = mix(vec3(0.82, 0.8, 0.74), vec3(0.62, 0.46, 0.32), step(0.55, fract(cH2 * 5.17)));
col = mix(col, cloth * 0.6, blind * 0.65);
float litShare = 0.25 + 0.35 * bH3;
float lit = glass * step(1.0 - litShare, planxDynHash(fCell * 3.7 + vec2(bSeed, 9.0)));
vec3 litTint = mix(vec3(1.0, 0.78, 0.5), vec3(0.8, 0.88, 1.0), step(0.82, fract(cH2 * 17.0)));
planxDynLit = litTint * lit * (0.9 + 0.6 * fract(cH * 31.0)) * (1.0 - far * 0.4);
diffuseColor.rgb = col * mix(vec3(1.0), diffuseColor.rgb, wallMask * uDynB.y);
planxDynWall = wallMask;
planxDynGlass = glass * (1.0 - 0.7 * blind);
`;

/**
 * Wall material of one building with a dynamic facade. The geometry needs
 * prismGeometry's facade coordinates. color: the building colour (tints the
 * wall); seed: varies the building; night: windows lit.
 * @param {string} styleName key of DYNAMIC_FACADE_STYLES
 * @param {{ color: any, seed: number, night?: boolean, anisotropy?: number }} opts
 */
export function createDynamicFacadeMaterial(styleName, { color, seed, night = false, anisotropy = 1 }) {
  textureAnisotropy = anisotropy;
  const style = DYNAMIC_FACADE_STYLES[styleName] || DYNAMIC_FACADE_STYLES.Apartment;
  const tile = MATERIAL_TILE_M[style.material];
  const uniforms = {
    uDynA: { value: new THREE.Vector4(style.winW, style.winH, style.sill, style.balcony) },
    uDynB: { value: new THREE.Vector4(GROUND_KIND[style.ground] ?? 0, style.tint, style.shutters ? 1 : 0, style.ribbon ? 1 : 0) },
    // Material texture repeats per bay and per floor (bays ~3.1 m, floors ~3 m).
    uDynMat: { value: new THREE.Vector2(3.1 / tile, 3.0 / tile) },
    uDynFrame: { value: linear(style.frame) },
    uDynGlass: { value: linear(style.glass) },
    uDynAccent: { value: linear(style.accent) },
    uDynSeed: { value: Math.abs(seed) % 4096 },
    uDynNight: { value: night ? 1 : 0 }
  };
  const mat = new THREE.MeshStandardMaterial({ map: materialFor(styleName), color, roughness: 0.72, side: THREE.DoubleSide });
  mat.userData.dynFacade = { style: styleName, uniforms };
  mat.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', `#include <common>\n${DYNAMIC_FACADE_PARS}`)
      .replace('#include <map_fragment>', `#ifdef USE_MAP\n${DYNAMIC_FACADE_FRAGMENT}\n#endif`)
      // Glass is glossy, blinds and walls matt.
      .replace('#include <roughnessmap_fragment>', '#include <roughnessmap_fragment>\nroughnessFactor = mix(roughnessFactor, 0.12, planxDynGlass);')
      // At night only the lit windows glow (the hover highlight shows by day).
      .replace('#include <emissivemap_fragment>', '#include <emissivemap_fragment>\nif (uDynNight > 0.5) totalEmissiveRadiance = planxDynLit * 1.2;');
  };
  mat.customProgramCacheKey = () => 'osm-dynamic-facade';
  return mat;
}

/** Day/night switch of a dynamic facade material (no-op for others). */
export function setDynamicFacadeNight(mat, night) {
  const u = mat?.userData?.dynFacade?.uniforms;
  if (u) u.uDynNight.value = night ? 1 : 0;
}
