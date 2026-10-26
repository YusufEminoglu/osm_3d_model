// @ts-check
// The material catalogue shared by PlanX 3D City and 3D OSM Model: one list
// of English names for facades, roofs and surfaces, the look behind each
// name, and the legacy names that still load (they map to the entry chosen
// for them). This file is kept identical in both plugins; a test checks it.
//
//   facades  - by building type. Most are dynamic: windows, balconies,
//              shopfronts and doors are drawn on each wall's bay/floor grid
//              ('Dynamic:<style>', see facade_dynamic.js). A few photo
//              textures are kept as their own entries (laid out on the same
//              grid).
//   roofs    - by material; the value names the texture recipe.
//   surfaces - one material list for roads, sidewalks and plazas, and block
//              grounds; `uses` says where each may be chosen.

/** Facade name -> look ('Dynamic:<style>' or an image under assets/). */
export const FACADES = {
  Apartment: 'Dynamic:Apartment',
  MixedUse: 'Dynamic:MixedUse',
  Modern: 'Dynamic:Modern',
  Office: 'Dynamic:Office',
  Heritage: 'Dynamic:Heritage',
  Brick: 'Dynamic:Brick',
  Villa: 'Dynamic:Villa',
  Industrial: 'Dynamic:Industrial',
  Civic: 'Dynamic:Civic',
  ApartmentTextureA: 'assets/facade_resid_a.webp',
  ApartmentTextureB: 'assets/facade_resid_b.webp',
  ApartmentTextureC: 'assets/facade_resid_c.webp',
  ApartmentTextureD: 'assets/facade_resid_d.webp',
  ApartmentTextureE: 'assets/facade_resid_e.webp',
  ApartmentTextureF: 'assets/facade_resid_f.webp',
  ModernTextureA: 'assets/facade3.webp',
  ModernTextureB: 'assets/facade4.webp'
};

/** Photo facades of homes: what residential buildings wear by default. */
export const APARTMENT_TEXTURES = ['ApartmentTextureA', 'ApartmentTextureB', 'ApartmentTextureC', 'ApartmentTextureD', 'ApartmentTextureE', 'ApartmentTextureF'];

/**
 * Window grid of each photo facade tile: window columns (bays) and rows
 * (floors), and where the cells start as a fraction of one cell, so a wall
 * of whole bays and floors shows whole windows.
 */
export const FACADE_TEXTURE_GRID = {
  ApartmentTextureA: { bays: 3, floors: 4, offset: [0, 0.442] },
  ApartmentTextureB: { bays: 3, floors: 4, offset: [0, 0.096] },
  ApartmentTextureC: { bays: 3, floors: 4, offset: [0, 0.442] },
  ApartmentTextureD: { bays: 3, floors: 4, offset: [0, 0.212] },
  ApartmentTextureE: { bays: 5, floors: 5, offset: [0.481, 0.481] },
  ApartmentTextureF: { bays: 3, floors: 5, offset: [0, 0.635] },
  ModernTextureA: { bays: 3, floors: 3, offset: [0.047, 0] },
  ModernTextureB: { bays: 3, floors: 3, offset: [0, 0] }
};

/** Window rows (floors) per tile of the photo apartment facades. */
export const FACADE_TEXTURE_FLOORS = Object.fromEntries(APARTMENT_TEXTURES.map((name) => [name, FACADE_TEXTURE_GRID[name].floors]));

/** Roof name -> texture recipe. */
export const ROOFS = {
  ClayTile: 'ClayTile',
  Slate: 'Slate',
  Shingle: 'Shingle',
  MetalSeam: 'MetalSeam',
  Copper: 'Copper',
  Lead: 'Lead',
  Gravel: 'Gravel',
  Green: 'Green',
  Solar: 'Solar'
};

export const ROOF_LABELS = {
  ClayTile: 'Clay tile',
  Slate: 'Slate',
  Shingle: 'Asphalt shingle',
  MetalSeam: 'Standing-seam metal',
  Copper: 'Copper with patina',
  Lead: 'Lead sheet',
  Gravel: 'Gravel deck',
  Green: 'Green roof',
  Solar: 'Solar panels'
};

/**
 * Surface name -> { look, uses }. look: 'surface:asphalt' and
 * 'surface:paver' are the generated asphalt and paver textures, an assets/
 * path an image, anything else a procedural texture recipe.
 * uses: road, walk (sidewalks and plazas), ground (block grounds).
 */
export const SURFACES = {
  Asphalt: { look: 'surface:asphalt', uses: ['road', 'walk'] },
  Cobblestone: { look: 'assets/pavement.webp', uses: ['road', 'walk'] },
  ConcretePaver: { look: 'surface:paver', uses: ['road', 'walk'] },
  GraniteSlab: { look: 'GraniteSlab', uses: ['walk'] },
  ConcreteSlab: { look: 'ConcreteSlab', uses: ['walk'] },
  GrassPaver: { look: 'GrassPaver', uses: ['walk'] },
  Grass: { look: 'Grass', uses: ['ground'] },
  Gravel: { look: 'Gravel', uses: ['ground'] },
  Sand: { look: 'Sand', uses: ['ground'] },
  Earth: { look: 'Earth', uses: ['ground'] },
  Water: { look: 'Water', uses: ['ground'] }
};

/**
 * Surfaces usable for one purpose, as { name: look }.
 * @param {'road' | 'walk' | 'ground'} use
 * @returns {Record<string, string>}
 */
export function surfacesFor(use) {
  /** @type {Record<string, string>} */
  const out = {};
  for (const [name, s] of Object.entries(SURFACES)) if (s.uses.includes(use)) out[name] = s.look;
  return out;
}

/** Legacy names (earlier versions, saved settings, QGIS attributes). */
export const LEGACY_NAMES = {
  facade: {
    DynamicApartment: 'Apartment', DynamicMixedUse: 'MixedUse', DynamicModern: 'Modern', DynamicOffice: 'Office',
    DynamicHeritage: 'Heritage', DynamicBrick: 'Brick',
    ResidentialA: 'ApartmentTextureA', ResidentialB: 'ApartmentTextureB', ResidentialC: 'ApartmentTextureC',
    ResidentialD: 'ApartmentTextureD', ResidentialE: 'ApartmentTextureE', ResidentialF: 'ApartmentTextureF',
    UrbanA: 'Apartment', UrbanB: 'Brick', UrbanC: 'ModernTextureA', UrbanD: 'ModernTextureB', UrbanE: 'MixedUse', UrbanF: 'Modern',
    CampusGlass: 'Office', CurtainWall: 'Office', BrutalistConcrete: 'Office', EcoTimber: 'Modern', NordicTimber: 'Modern',
    CivicStone: 'Heritage', HeritageStone: 'Heritage', DenseBrick: 'Brick', AnatolianBrick: 'Brick', IndustrialLoft: 'Brick',
    CoastalWhite: 'Apartment', MediterraneanStucco: 'Apartment'
  },
  roof: {
    RoofA: 'ClayTile', RoofB: 'Slate', RoofC: 'ClayTile', RoofD: 'Gravel', TurkishTile: 'ClayTile', CeramicLight: 'ClayTile',
    GermanTile: 'Slate', USShingle: 'Shingle', StandingSeam: 'MetalSeam', CopperPatina: 'Copper', LeadMansard: 'Lead',
    GravelBallast: 'Gravel', GreenRoof: 'Green', SolarRoof: 'Solar'
  },
  surface: {
    Plain: 'Asphalt', Cobble: 'Cobblestone', SharedStreet: 'ConcretePaver', CampusPaver: 'ConcretePaver', Grid: 'ConcretePaver',
    PlazaGranite: 'GraniteSlab', StoneA: 'GraniteSlab', StoneB: 'GraniteSlab', WarmStone: 'GraniteSlab', Tile: 'GraniteSlab',
    Concrete: 'ConcreteSlab', Permeable: 'GrassPaver', ParkGreen: 'Grass', CivicGravel: 'Gravel', CoastalSand: 'Sand',
    ResidentialBeige: 'Earth', SoftNoise: 'Earth', FineGrid: 'Earth'
  }
};

/**
 * The current name for `value` in `group` ('facade' | 'roof' | 'surface'):
 * the name itself (any letter case), or the name a legacy name maps to, or
 * null when it is neither.
 * @param {'facade' | 'roof' | 'surface'} group
 * @param {unknown} value
 * @param {Record<string, unknown>} [names] the names on offer (defaults to the whole group)
 */
export function catalogueName(group, value, names) {
  if (value === null || value === undefined) return null;
  const raw = String(value).trim().toLowerCase();
  if (!raw) return null;
  const offered = names || (group === 'facade' ? FACADES : group === 'roof' ? ROOFS : SURFACES);
  const keys = Object.keys(offered);
  const direct = keys.find((k) => k.toLowerCase() === raw);
  if (direct) return direct;
  const legacy = Object.entries(LEGACY_NAMES[group] || {}).find(([k]) => k.toLowerCase() === raw);
  return legacy && keys.includes(legacy[1]) ? legacy[1] : null;
}
