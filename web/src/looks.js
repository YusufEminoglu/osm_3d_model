/* 3D OSM Model — curated viewer "looks".
 *
 * A look is one hand-tuned combination of an existing colour theme plus the
 * texture, massing and atmosphere settings that make that palette read the way
 * it was designed to. The toolbar's shuffle button picks one of these at random
 * — never a random *setting*, always a whole composition that was checked by
 * eye. Layer visibility is deliberately NOT part of a look: turning the user's
 * trees or pedestrians back on behind their back would cost frames and trust.
 *
 * No look enables SSAO. It is opt-in for a reason — on a map-scale scene it
 * darkens the whole render to near black, which is not something a one-click
 * button may inflict on the user. See tests/looks_check.mjs, which enforces it.
 *
 * This module is pure data plus two tiny helpers so it can be unit-tested in
 * plain Node (tests/looks_check.mjs) without pulling in three.js. Every value
 * below must exist in app.js — COLOR_THEMES, assetThemePresets, textureSets and
 * ROOF_SHAPE_OPTIONS — and the test asserts exactly that.
 */

export const LOOKS = [
  {
    id: 'editorial-dusk',
    name: 'Editorial Dusk',
    blurb: 'Warm paper palette, low late-afternoon sun, soft civic massing.',
    swatch: ['#ebd4c0', '#c2c5aa', '#7c5c43', '#fdfbf7'],
    colorTheme: 'Editorial Paper',
    settings: {
      assetTheme: 'Civic Heritage',
      roadStyle: 'Asphalt',
      pavementStyle: 'WarmStone',
      islandTexture: 'SoftNoise',
      roofTexture: 'RoofA',
      roofShape: 'Hip',
      buildingMode: 'Extruded + roof',
      roofHeight: 2.2,
      showLedges: true,
      showStorefronts: true,
      buildingSetback: 0.4,
      ledgeProjection: 0.18,
      treeRenderMode: 'Realistic',
      timeOfDay: 17.4,
      weather: 'Clear',
      fogDensity: 0.00028,
      enableBloom: true,
      enableSSAO: false
    }
  },
  {
    id: 'nordic-snowfall',
    name: 'Nordic Snowfall',
    blurb: 'Cool grey-teal city under falling snow and a flat winter light.',
    swatch: ['#b9c7c5', '#dde6e3', '#36433f', '#e6efec'],
    colorTheme: 'Tinted Gray Teal',
    settings: {
      assetTheme: 'Modern Urban',
      roadStyle: 'Asphalt',
      pavementStyle: 'Concrete',
      islandTexture: 'FineGrid',
      roofTexture: 'StandingSeam',
      roofShape: 'Shed',
      buildingMode: 'Extruded + roof',
      roofHeight: 1.4,
      showLedges: true,
      showStorefronts: false,
      buildingSetback: 0.2,
      ledgeProjection: 0.12,
      treeRenderMode: 'Stylized',
      timeOfDay: 10.2,
      weather: 'Snow',
      fogDensity: 0.0009,
      enableBloom: false,
      enableSSAO: false
    }
  },
  {
    id: 'harbor-morning',
    name: 'Harbor Morning',
    blurb: 'Teal streets and salmon facades in clean early side-light.',
    swatch: ['#e3c3b5', '#5e9e7e', '#2f4a46', '#dfeae7'],
    colorTheme: 'Teal & Salmon',
    settings: {
      assetTheme: 'Coastal Light',
      roadStyle: 'SharedStreet',
      pavementStyle: 'CampusPaver',
      islandTexture: 'CoastalSand',
      roofTexture: 'CeramicLight',
      roofShape: 'Gable',
      buildingMode: 'Extruded + roof',
      roofHeight: 2.0,
      showLedges: false,
      showStorefronts: true,
      buildingSetback: 0.0,
      ledgeProjection: 0.15,
      treeRenderMode: 'Realistic',
      timeOfDay: 8.4,
      weather: 'Clear',
      fogDensity: 0.00045,
      enableBloom: true,
      enableSSAO: false
    }
  },
  {
    id: 'golden-mediterranean',
    name: 'Golden Mediterranean',
    blurb: 'Terracotta and stucco at golden hour — the postcard shot.',
    swatch: ['#f0d9bf', '#7bb069', '#6f655c', '#f4ead6'],
    colorTheme: 'Pixar',
    settings: {
      assetTheme: 'Mediterranean',
      roadStyle: 'Cobblestone',
      pavementStyle: 'StoneA',
      islandTexture: 'SoftNoise',
      roofTexture: 'TurkishTile',
      roofShape: 'Gable',
      buildingMode: 'Extruded + roof',
      roofHeight: 2.6,
      showLedges: true,
      showStorefronts: true,
      buildingSetback: 0.0,
      ledgeProjection: 0.22,
      treeRenderMode: 'Realistic',
      timeOfDay: 18.2,
      weather: 'Clear',
      fogDensity: 0.0004,
      enableBloom: true,
      enableSSAO: false
    }
  },
  {
    id: 'neon-rain',
    name: 'Neon Rain',
    blurb: 'Night glass towers, wet asphalt and bloom-lit rain.',
    swatch: ['#9fc7d6', '#2f9e7e', '#1a1d24', '#2b3038'],
    colorTheme: 'Futuristic City',
    settings: {
      assetTheme: 'Dense Urban',
      roadStyle: 'Asphalt',
      pavementStyle: 'Grid',
      islandTexture: 'FineGrid',
      roofTexture: 'SolarRoof',
      roofShape: 'Flat',
      buildingMode: 'Extruded + roof',
      roofHeight: 0.8,
      showLedges: true,
      showStorefronts: true,
      buildingSetback: 0.0,
      ledgeProjection: 0.1,
      treeRenderMode: 'Stylized',
      timeOfDay: 21.6,
      weather: 'Rain',
      fogDensity: 0.0011,
      enableBloom: true,
      enableSSAO: false
    }
  },
  {
    id: 'anime-noon',
    name: 'Anime Noon',
    blurb: 'Bright cel pastels, hard noon shadows, no haze.',
    swatch: ['#bfe3f0', '#b6f2c6', '#7d8a96', '#e9f1e4'],
    colorTheme: 'Anime',
    settings: {
      assetTheme: 'Coastal Light',
      roadStyle: 'Plain',
      pavementStyle: 'Permeable',
      islandTexture: 'ParkGreen',
      roofTexture: 'CeramicLight',
      roofShape: 'Pyramid',
      buildingMode: 'Extruded + roof',
      roofHeight: 2.4,
      showLedges: false,
      showStorefronts: true,
      buildingSetback: 0.0,
      ledgeProjection: 0.15,
      treeRenderMode: 'Stylized',
      timeOfDay: 12.6,
      weather: 'Clear',
      fogDensity: 0.00012,
      enableBloom: false,
      enableSSAO: false
    }
  },
  {
    id: 'violet-dawn',
    name: 'Violet Dawn',
    blurb: 'Lilac blocks against soft black roads in first light and mist.',
    swatch: ['#d2c9e4', '#8a9e6e', '#2a2a30', '#efecf6'],
    colorTheme: 'Light Purple & Black',
    settings: {
      assetTheme: 'Modern Urban',
      roadStyle: 'Asphalt',
      pavementStyle: 'StoneB',
      islandTexture: 'SoftNoise',
      roofTexture: 'USShingle',
      roofShape: 'Shed',
      buildingMode: 'Extruded + roof',
      roofHeight: 1.6,
      showLedges: true,
      showStorefronts: false,
      buildingSetback: 0.6,
      ledgeProjection: 0.14,
      treeRenderMode: 'Stylized',
      timeOfDay: 7.1,
      weather: 'Clear',
      fogDensity: 0.0006,
      enableBloom: true,
      enableSSAO: false
    }
  },
  {
    id: 'desert-noon',
    name: 'Desert Noon',
    blurb: 'Sand and slate, dry air, deep contact shadows.',
    swatch: ['#e3d6b6', '#8aa05e', '#46413a', '#efe7d4'],
    colorTheme: 'Warm Sand & Slate',
    settings: {
      assetTheme: 'Modern Turkish',
      roadStyle: 'Asphalt',
      pavementStyle: 'Cobble',
      islandTexture: 'CivicGravel',
      roofTexture: 'GermanTile',
      roofShape: 'Gable',
      buildingMode: 'Extruded + roof',
      roofHeight: 2.0,
      showLedges: true,
      showStorefronts: true,
      buildingSetback: 0.3,
      ledgeProjection: 0.2,
      treeRenderMode: 'Realistic',
      timeOfDay: 12.0,
      weather: 'Clear',
      fogDensity: 0.00018,
      enableBloom: false,
      enableSSAO: false
    }
  },
  {
    id: 'cartoon-playground',
    name: 'Cartoon Playground',
    blurb: 'Bold primaries under dark pyramid roofs, mid-afternoon light.',
    swatch: ['#f2b8b0', '#5fbf57', '#4a4540', '#fbe7c6'],
    colorTheme: 'Cartoon',
    settings: {
      assetTheme: 'Modern Urban',
      roadStyle: 'Plain',
      pavementStyle: 'CampusPaver',
      islandTexture: 'ParkGreen',
      roofTexture: 'USShingle',
      roofShape: 'Pyramid',
      buildingMode: 'Extruded + roof',
      roofHeight: 3.0,
      showLedges: false,
      showStorefronts: true,
      buildingSetback: 0.0,
      ledgeProjection: 0.25,
      treeRenderMode: 'Stylized',
      timeOfDay: 14.2,
      weather: 'Clear',
      fogDensity: 0.0002,
      enableBloom: true,
      enableSSAO: false
    }
  },
  {
    id: 'vintage-postcard',
    name: 'Vintage Postcard',
    blurb: 'Sepia stock, cobbled streets, heavy cornices, late sun.',
    swatch: ['#cdb89a', '#b9c79a', '#6b6052', '#e4d6b8'],
    colorTheme: 'Classic Era',
    settings: {
      assetTheme: 'Civic Heritage',
      roadStyle: 'Cobblestone',
      pavementStyle: 'StoneB',
      islandTexture: 'ResidentialBeige',
      roofTexture: 'RoofC',
      roofShape: 'Hip',
      buildingMode: 'Extruded + roof',
      roofHeight: 2.4,
      showLedges: true,
      showStorefronts: true,
      buildingSetback: 0.0,
      ledgeProjection: 0.3,
      treeRenderMode: 'Realistic',
      timeOfDay: 16.2,
      weather: 'Clear',
      fogDensity: 0.0006,
      enableBloom: true,
      enableSSAO: false
    }
  }
];

export const LOOK_IDS = LOOKS.map((look) => look.id);

export function getLook(id) {
  return LOOKS.find((look) => look.id === id) || null;
}

/* Pick a look at random, never the one already applied — a shuffle that can
 * hand back the current look reads as a broken button. With a single look in
 * the catalogue it returns that look rather than nothing. */
export function pickLookId(currentId, rand = Math.random) {
  const pool = LOOKS.filter((look) => look.id !== currentId);
  const list = pool.length ? pool : LOOKS;
  const index = Math.min(list.length - 1, Math.max(0, Math.floor(rand() * list.length)));
  return list[index].id;
}

/* ------------------------------------------------------------------ Tour ---
 * Cinematic tour shot list. Each shot is resolved against the model's own
 * extent at run time, so the same choreography works for a 4 ha courtyard and
 * a 300 ha district:
 *
 *   radius  — orbit distance as a multiple of the model radius
 *   height  — camera height as a multiple of the model radius
 *   azimuth — absolute bearing in degrees; the runner unwraps these so the
 *             camera always sweeps the short, continuous way round instead of
 *             snapping back through 0.
 *   target  — [x, z] offset of the look-at point, as a fraction of the radius.
 *
 * Times are seconds *at the start* of the shot; the runner interpolates every
 * field with a smoothstep so there is no visible keyframe pop.
 */
export const TOUR_SHOTS = [
  { name: 'Establishing', duration: 7.0, azimuth: 215, radius: 1.95, height: 1.15, fov: 52, timeOfDay: 8.4, target: [0, 0] },
  { name: 'District sweep', duration: 7.0, azimuth: 305, radius: 1.30, height: 0.62, fov: 54, timeOfDay: 11.4, target: [0.10, -0.08] },
  { name: 'Descent', duration: 6.5, azimuth: 385, radius: 0.72, height: 0.24, fov: 48, timeOfDay: 14.6, target: [-0.06, 0.10] },
  { name: 'Street level', duration: 6.5, azimuth: 455, radius: 0.38, height: 0.055, fov: 64, timeOfDay: 17.2, target: [0.04, 0.02] },
  { name: 'Golden rise', duration: 8.0, azimuth: 540, radius: 1.70, height: 0.95, fov: 50, timeOfDay: 18.7, target: [0, 0] }
];

export const TOUR_DURATION = TOUR_SHOTS.reduce((sum, shot) => sum + shot.duration, 0);

/* Resolve the tour to a camera state at elapsed time `t` (seconds).
 * Returns unit-less factors; the caller scales them by the model radius and
 * adds the ground height. Kept here — and free of three.js — so the easing and
 * the shot arithmetic are unit-testable. */
export function tourStateAt(t, shots = TOUR_SHOTS) {
  if (!shots.length) return null;
  const total = shots.reduce((sum, shot) => sum + shot.duration, 0);
  const clamped = Math.max(0, Math.min(t, total));
  let index = 0;
  let start = 0;
  while (index < shots.length - 1 && clamped >= start + shots[index].duration) {
    start += shots[index].duration;
    index += 1;
  }
  const shot = shots[index];
  const next = shots[index + 1] || shot;
  const span = shot.duration || 1;
  const u = Math.max(0, Math.min(1, (clamped - start) / span));
  const e = u * u * (3 - 2 * u); // smoothstep — zero velocity at every cut
  const mix = (a, b) => a + (b - a) * e;
  return {
    shotIndex: index,
    shotName: shot.name,
    progress: total > 0 ? clamped / total : 1,
    elapsed: clamped,
    total,
    azimuth: mix(shot.azimuth, next.azimuth),
    radius: mix(shot.radius, next.radius),
    height: mix(shot.height, next.height),
    fov: mix(shot.fov, next.fov),
    timeOfDay: mix(shot.timeOfDay, next.timeOfDay),
    targetX: mix(shot.target[0], next.target[0]),
    targetZ: mix(shot.target[1], next.target[1])
  };
}
