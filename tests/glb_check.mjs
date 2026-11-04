/* Pins the "3D model (GLB)" export (web/src/glb_export.js): the static city
 * only, plain coloured materials, metres around the study-area centre with
 * glTF +X east / +Y up / -Z north, the georeference and OSM attribution in
 * the file, and a valid binary glTF 2.0. 1.9.1 had no 3D model export at all.
 *
 * The viewer resolves 'three' through an import map; here a module resolve
 * hook points it at the vendored three.js r160, and FileReader (which the
 * exporter uses to assemble the binary) is polyfilled from Blob.
 *
 *     node tests/glb_check.mjs
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { registerHooks } from 'node:module';

const vendor = new URL('../web/assets/vendor/three/', import.meta.url);
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'three') {
      return { url: new URL('build/three.module.js', vendor).href, shortCircuit: true };
    }
    if (specifier.startsWith('three/addons/')) {
      return { url: new URL(`examples/jsm/${specifier.slice('three/addons/'.length)}`, vendor).href, shortCircuit: true };
    }
    return nextResolve(specifier, context);
  }
});

if (typeof globalThis.FileReader === 'undefined') {
  globalThis.FileReader = class FileReader {
    _done(result) {
      this.result = result;
      this.onload?.({ target: this });
      this.onloadend?.({ target: this });
    }

    readAsArrayBuffer(blob) {
      blob.arrayBuffer().then((buffer) => this._done(buffer));
    }

    readAsDataURL(blob) {
      blob.arrayBuffer().then((buffer) => this._done(
        `data:${blob.type || 'application/octet-stream'};base64,${Buffer.from(buffer).toString('base64')}`));
    }
  };
}

const THREE = await import('three');
let glbModule;
try {
  glbModule = await import('../web/src/glb_export.js');
} catch (error) {
  assert.fail(`web/src/glb_export.js could not be loaded (${error.code || error.message}): 1.9.1 has no GLB export`);
}
const { STATIC_CITY_LAYERS, buildStaticCityScene, exportStaticCityGlb, readGlbJson, selectStaticCity } = glbModule;

// ---------------------------------------------------------------- vendored exporter
const exporterSource = await readFile(new URL('examples/jsm/exporters/GLTFExporter.js', vendor), 'utf8');
const threeSource = await readFile(new URL('build/three.module.js', vendor), 'utf8');
assert.match(threeSource, /const REVISION = '160';/);
assert.equal(THREE.REVISION, '160');
const exporterImports = [...exporterSource.matchAll(/from '([^']+)'/g)].map((m) => m[1]).sort();
assert.deepEqual(exporterImports, ['./../utils/TextureUtils.js', 'three'], 'GLTFExporter imports');
await readFile(new URL('examples/jsm/utils/TextureUtils.js', vendor), 'utf8');

// ---------------------------------------------------------------- a scene shaped like the viewer's
const GROUND_Y = 5; // the DEM height at the study-area centre
const scene = new THREE.Scene();
const world = new THREE.Group();
scene.add(world);
const sky = new THREE.Mesh(new THREE.SphereGeometry(500, 8, 8), new THREE.MeshBasicMaterial({ side: THREE.BackSide }));
sky.name = 'sky';
scene.add(sky, new THREE.AmbientLight(0xffffff, 0.6), new THREE.DirectionalLight(0xffffff, 1.2));
scene.add(new THREE.Points(new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute([0, 0, 0], 3)),
  new THREE.PointsMaterial()));
const measure = new THREE.Group();
measure.name = 'measureGroup';
measure.add(new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial()));
scene.add(measure);

const group = (name) => {
  const g = new THREE.Group();
  g.name = name;
  world.add(g);
  return g;
};
const standard = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, ...extra });
const hidden = () => new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0, depthWrite: false });

// Ground: a 100 x 100 m DEM rectangle clipped to the study area by an alpha
// mask in the viewer (the export keeps the triangles inside a 30 m disk).
const terrain = new THREE.Mesh(new THREE.PlaneGeometry(100, 100, 10, 10), standard('#e6dfd3', { transparent: true, alphaTest: 0.02 }));
terrain.name = 'terrain';
terrain.rotation.x = -Math.PI / 2;
terrain.position.y = GROUND_Y;
world.add(terrain);
const overlay = new THREE.Mesh(new THREE.PlaneGeometry(100, 100), standard('#ffffff', { transparent: true, depthWrite: false }));
overlay.name = 'terrain-overlay';
world.add(overlay);

const sides = group('terrain-sides');
const sideGeometry = new THREE.PlaneGeometry(10, 4);
sideGeometry.setAttribute('color', new THREE.Float32BufferAttribute(new Array(sideGeometry.attributes.position.count * 3).fill(0.7), 3));
sides.add(new THREE.Mesh(sideGeometry, new THREE.MeshStandardMaterial({ vertexColors: true })));

const islands = group('islands');
const park = new THREE.Mesh(new THREE.ShapeGeometry(new THREE.Shape([
  new THREE.Vector2(0, 0), new THREE.Vector2(12, 0), new THREE.Vector2(12, 8), new THREE.Vector2(0, 8)])), standard('#c2c5aa'));
park.rotation.x = Math.PI / 2;
park.position.set(5, GROUND_Y + 0.6, -10);
islands.add(park);

// Buildings: walls with a hidden cap (the roof is its own mesh), a dynamic
// facade patched with onBeforeCompile, a roof textured in the shader's way,
// and a building drawn by a ShaderMaterial.
const buildings = group('buildings');
const wallGeometry = new THREE.BoxGeometry(10, 12, 6).translate(0, 6, 0);
wallGeometry.groups.forEach((g) => { g.materialIndex = g.materialIndex === 2 ? 0 : 1; }); // +Y face = cap
const facade = standard('#c4d1db', { side: THREE.DoubleSide });
facade.onBeforeCompile = (shader) => { shader.fragmentShader = `// windows\n${shader.fragmentShader}`; };
facade.customProgramCacheKey = () => 'osm-dynamic-facade';
const buildingData = { osm_id: 'w101', building: 'apartments', name: 'Block A', height: 12, min_height: 0 };
const walls = new THREE.Mesh(wallGeometry, [hidden(), facade]);
walls.position.set(-20, GROUND_Y, 30); // scene x = -(east): 20 m east; z = 30 m north
walls.userData = buildingData;
buildings.add(walls);
const roofTexture = new THREE.Texture();
roofTexture.name = 'clay';
const roof = new THREE.Mesh(new THREE.ConeGeometry(3, 3, 4).translate(0, 1.5, 0), standard('#ffffff', { map: roofTexture }));
roof.position.set(-20, GROUND_Y + 12.04, 30);
roof.userData = buildingData;
buildings.add(roof);
const shaderBuilding = new THREE.Mesh(new THREE.BoxGeometry(6, 9, 6).translate(0, 4.5, 0), new THREE.ShaderMaterial({
  uniforms: { uColor: { value: new THREE.Color('#ebd4c0') } },
  vertexShader: 'void main() { gl_Position = vec4(position, 1.0); }',
  fragmentShader: 'uniform vec3 uColor; void main() { gl_FragColor = vec4(uColor, 1.0); }'
}));
shaderBuilding.position.set(15, GROUND_Y, -25);
shaderBuilding.userData = { osm_id: 'w102', building: 'yes' };
buildings.add(shaderBuilding);
const hiddenBuilding = new THREE.Mesh(new THREE.BoxGeometry(3, 3, 3), standard('#000000'));
hiddenBuilding.visible = false;
buildings.add(hiddenBuilding);

const roads = group('roads');
const road = new THREE.Mesh(new THREE.PlaneGeometry(60, 7).rotateX(-Math.PI / 2), standard('#7c5c43'));
road.position.y = GROUND_Y + 1.36;
roads.add(road);
roads.add(new THREE.Mesh(new THREE.PlaneGeometry(60, 7).rotateX(-Math.PI / 2), standard('#7c5c43', { transparent: true, opacity: 0 })));
roads.add(new THREE.Mesh(new THREE.PlaneGeometry(60, 0.2).rotateX(-Math.PI / 2),
  new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, depthWrite: false })));

const trees = group('trees');
const trunkGeometry = new THREE.CylinderGeometry(0.1, 0.18, 1, 6);
const crownGeometry = new THREE.IcosahedronGeometry(1, 0);
const trunks = new THREE.InstancedMesh(trunkGeometry, standard('#4a3a2a'), 3);
const crowns = new THREE.InstancedMesh(crownGeometry, standard('#5a7a3a'), 3);
const dummy = new THREE.Object3D();
[[-5, 5], [0, 8], [6, 2]].forEach(([x, z], i) => {
  dummy.position.set(x, GROUND_Y + 1, z);
  dummy.scale.set(1, 2, 1);
  dummy.updateMatrix();
  trunks.setMatrixAt(i, dummy.matrix);
  dummy.position.set(x, GROUND_Y + 3, z);
  dummy.scale.set(2, 2, 2);
  dummy.updateMatrix();
  crowns.setMatrixAt(i, dummy.matrix);
});
trees.add(trunks, crowns);

const furniture = group('furniture');
const lamp = new THREE.Group();
lamp.add(new THREE.Mesh(new THREE.CylinderGeometry(0.08, 0.12, 4, 8), standard('#333333', { metalness: 0.8, roughness: 0.2 })));
lamp.add(new THREE.PointLight(0xffcc88, 1.4, 24));
lamp.add(new THREE.Sprite(new THREE.SpriteMaterial({ color: 0xffcc88 })));
lamp.add(new THREE.Mesh(new THREE.ConeGeometry(2, 4), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.1, depthWrite: false })));
furniture.add(lamp);

const waterlines = group('waterlines');
for (const x of [-10, 10]) {
  const segment = new THREE.Mesh(new THREE.PlaneGeometry(20, 3).rotateX(-Math.PI / 2), standard('#0f5e9c', { side: THREE.DoubleSide }));
  segment.position.set(x, GROUND_Y + 0.58, -40);
  waterlines.add(segment);
}
const fences = group('fences');
fences.visible = false; // switched off in the viewer
fences.add(new THREE.Mesh(new THREE.BoxGeometry(10, 1, 0.1), standard('#999999')));

// Everything that moves, analysis overlays and helpers: never in the model.
for (const name of ['cars', 'bikes', 'pedestrians', 'wind-plumes', 'zoning', 'basemap']) {
  group(name).add(new THREE.Mesh(new THREE.BoxGeometry(2, 1, 4), standard('#ff0000')));
}
world.add(new THREE.Mesh(new THREE.PlaneGeometry(50, 50), standard('#ff00ff'))); // unnamed: shadow heatmap

// ---------------------------------------------------------------- the filter
const picked = selectStaticCity(world);
const pickedKeys = picked.map((entry) => entry.key).sort();
assert.deepEqual(pickedKeys, ['buildings', 'furniture', 'islands', 'roads', 'terrain', 'terrain-sides', 'trees', 'waterlines'],
  'static-city layers picked');
const meshesOf = (key) => picked.find((entry) => entry.key === key).meshes;
assert.deepEqual(meshesOf('buildings'), [walls, roof, shaderBuilding], 'buildings: walls, roof, shader building; not the hidden one');
assert.deepEqual(meshesOf('roads'), [road], 'roads: the drawn road only (no invisible road, no decal)');
assert.equal(meshesOf('furniture').length, 1, 'furniture: the lamp pole only (no light, sprite or glow)');
assert.deepEqual(meshesOf('trees'), [trunks, crowns]);
for (const name of ['cars', 'bikes', 'pedestrians', 'wind-plumes', 'zoning', 'basemap', 'terrain-overlay', 'sky', 'measureGroup', 'fences']) {
  assert.ok(!(name in STATIC_CITY_LAYERS) || name === 'fences', `${name} must not be a static-city layer`);
  assert.ok(!pickedKeys.includes(name), `${name} was exported`);
}

// ---------------------------------------------------------------- the scene that is written
const keepInside = (a, b, c) => Math.hypot((a.x + b.x + c.x) / 3, (a.z + b.z + c.z) / 3) <= 30;
const georeference = {
  crs: 'EPSG:32635', originEasting: 500000, originNorthing: 4250000, originHeight: GROUND_Y,
  units: 'metre', upAxis: 'Y'
};
const textureColor = (texture) => (texture === roofTexture ? new THREE.Color('#a8442a') : null);
const { scene: out, stats } = buildStaticCityScene({
  root: world,
  xSign: -1,
  originHeight: GROUND_Y,
  georeference,
  clipTriangles: (key) => (key === 'terrain' ? keepInside : null),
  textureColor
});
const model = out.children[0];
assert.equal(model.name, '3D OSM Model');
assert.deepEqual(model.userData, { ...georeference, attribution: '© OpenStreetMap contributors' });
assert.deepEqual(out.userData, model.userData);
const layerNames = model.children.map((layer) => layer.name).sort();
assert.deepEqual(layerNames, ['Buildings', 'Greens and water', 'Ground', 'Model base', 'Roads', 'Street furniture', 'Trees', 'Waterways']);
const layer = (name) => model.children.find((child) => child.name === name);

const buildingMeshes = layer('Buildings').children;
assert.deepEqual(buildingMeshes.map((mesh) => mesh.name).sort(), ['apartments Block A (OSM w101)', 'building (OSM w102)'],
  'one node per OSM building, walls and roof together');
const blockA = buildingMeshes.find((mesh) => mesh.name.startsWith('apartments'));
assert.equal(blockA.userData.osm_id, 'w101');
const colors = (mesh) => (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).map((m) => `#${m.color.getHexString()}`);
assert.deepEqual(colors(blockA).sort(), ['#a8442a', '#c4d1db'], 'facade colour and roof colour (texture averaged in)');
assert.equal(blockA.geometry.groups.length, 2);
const triangles = (mesh) => mesh.geometry.index.count / 3;
const coneTriangles = new THREE.ConeGeometry(3, 3, 4).index.count / 3;
assert.equal(triangles(blockA), 10 + coneTriangles, 'the hidden cap is not in the model (box 12 - 2 cap triangles + roof)');
const shaderMesh = buildingMeshes.find((mesh) => mesh.name.startsWith('building'));
assert.deepEqual(colors(shaderMesh), ['#ebd4c0'], 'a ShaderMaterial building keeps its colour');
out.traverse((object) => {
  if (!object.isMesh) return;
  for (const material of Array.isArray(object.material) ? object.material : [object.material]) {
    assert.equal(material.type, 'MeshStandardMaterial', `${object.name}: ${material.type}`);
    assert.equal(material.map, null, `${object.name}: a texture was kept`);
    assert.equal(material.onBeforeCompile.toString().includes('windows'), false, 'shader patch kept');
  }
});

// Metres around the centre: 20 m east -> +x 20, 30 m north -> z -30, ground -> y 0.
blockA.geometry.computeBoundingBox();
const box = blockA.geometry.boundingBox;
const near = (a, b, what) => assert.ok(Math.abs(a - b) < 1e-3, `${what}: ${a} != ${b}`);
near(box.min.x, 15, 'west wall at 15 m east');
near(box.max.x, 25, 'east wall at 25 m east');
near(box.max.z, -27, 'south wall at 27 m north');
near(box.min.z, -33, 'north wall at 33 m north');
near(box.min.y, 0, 'walls stand on the ground');
near(box.max.y, 12.04 + 3, 'roof top at 15.04 m');
// Outward normals survive the axis change (no mirror image).
const north = new THREE.Vector3();
const position = blockA.geometry.attributes.position;
const normal = blockA.geometry.attributes.normal;
let northFacing = 0;
for (let i = 0; i < position.count; i++) {
  north.fromBufferAttribute(normal, i);
  if (north.z < -0.99) {
    near(position.getZ(i), -33, 'a north-facing wall is the north wall');
    northFacing++;
  }
}
assert.ok(northFacing >= 4, 'north wall found');

const ground = layer('Ground').children[0];
assert.ok(triangles(ground) < 200 && triangles(ground) > 20, `ground clipped to the study area: ${triangles(ground)} of 200 triangles`);
const groundPosition = ground.geometry.attributes.position;
for (let i = 0; i < groundPosition.count; i++) {
  near(groundPosition.getY(i), 0, 'ground at y 0');
  assert.ok(Math.hypot(groundPosition.getX(i), groundPosition.getZ(i)) <= 30 + 10 * Math.SQRT2, 'ground vertex outside the study area');
}
assert.equal(layer('Trees').children.length, 1, 'trees merged into one mesh');
const treeTriangles = 3 * (trunkGeometry.index.count / 3 + crownGeometry.attributes.position.count / 3);
assert.equal(triangles(layer('Trees').children[0]), treeTriangles, 'every instance baked');
assert.equal(layer('Waterways').children.length, 1, 'waterway segments merged');
assert.ok(layer('Model base').children[0].geometry.getAttribute('color'), 'vertex colours kept for the model base');
assert.equal(stats.meshes, 9);
assert.equal(stats.layers.buildings.meshes, 2);

// ---------------------------------------------------------------- the GLB
const { glb, stats: written } = await exportStaticCityGlb({
  root: world,
  xSign: -1,
  originHeight: GROUND_Y,
  georeference,
  clipTriangles: (key) => (key === 'terrain' ? keepInside : null),
  textureColor,
  generator: '3D OSM Model test'
});
const view = new DataView(glb);
assert.equal(view.getUint32(0, true), 0x46546c67, 'magic "glTF"');
assert.equal(new TextDecoder().decode(new Uint8Array(glb, 0, 4)), 'glTF');
assert.equal(view.getUint32(4, true), 2, 'GLB version 2');
assert.equal(view.getUint32(8, true), glb.byteLength, 'GLB length');
assert.equal(written.bytes, glb.byteLength);
const json = readGlbJson(glb);
assert.ok(json, 'JSON chunk');
assert.equal(json.asset.version, '2.0');
assert.equal(json.asset.copyright, '© OpenStreetMap contributors');
assert.match(json.asset.generator, /^3D OSM Model test \(three\.js GLTFExporter r160\)$/);
assert.equal(json.scenes[0].extras.crs, 'EPSG:32635');
assert.equal(json.scenes[0].extras.originEasting, 500000);
assert.equal(json.scenes[0].extras.originNorthing, 4250000);
assert.equal(json.scenes[0].extras.originHeight, GROUND_Y);
assert.equal(json.scenes[0].extras.attribution, '© OpenStreetMap contributors');
const rootNode = json.nodes.find((node) => node.name === '3D OSM Model');
assert.equal(rootNode?.extras?.crs, 'EPSG:32635', 'georeference on the root node');
assert.equal(json.textures, undefined, 'no textures');
assert.equal(json.images, undefined, 'no images');
assert.equal(json.extensionsRequired, undefined, 'no required extensions');
// Plain PBR materials: a base colour factor (absent = the glTF default white,
// used only by the vertex-coloured model base) and no texture.
assert.ok(json.materials.every((m) => m.pbrMetallicRoughness && !m.pbrMetallicRoughness.baseColorTexture),
  'every material is a plain PBR material');
const factors = json.materials.map((m) => m.pbrMetallicRoughness.baseColorFactor || [1, 1, 1, 1]);
for (const hex of ['#c4d1db', '#a8442a', '#ebd4c0', '#7c5c43']) {
  const linear = new THREE.Color(hex);
  assert.ok(factors.some((f) => Math.abs(f[0] - linear.r) < 1e-4 && Math.abs(f[1] - linear.g) < 1e-4 && Math.abs(f[2] - linear.b) < 1e-4),
    `no material with base colour ${hex}`);
}
assert.equal(factors.filter((f) => f.slice(0, 3).every((v) => v === 1)).length, 1, 'only the model base is white (vertex colours)');
const nodeNames = json.nodes.map((node) => node.name);
for (const name of ['Buildings', 'Roads', 'Ground', 'Trees', 'Street furniture', 'Waterways', 'apartments Block A (OSM w101)']) {
  assert.ok(nodeNames.includes(name), `node ${name}`);
}
assert.ok(!nodeNames.some((name) => /car|pedestrian|bike|plume|zoning|sky/i.test(name || '')), `excluded nodes: ${nodeNames}`);
const blockNode = json.nodes.find((node) => node.name === 'apartments Block A (OSM w101)');
assert.equal(blockNode.extras.osm_id, 'w101');
const blockPrimitive = json.meshes[blockNode.mesh].primitives[0];
const accessor = json.accessors[blockPrimitive.attributes.POSITION];
assert.deepEqual(accessor.min.map((v) => Math.round(v * 100) / 100), [15, 0, -33]);
assert.deepEqual(accessor.max.map((v) => Math.round(v * 100) / 100), [25, 15.04, -27]);

// ---------------------------------------------------------------- wiring in the viewer
const app = await readFile(new URL('../web/src/app.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../web/src/index.html', import.meta.url), 'utf8');
const named = new Set([...app.matchAll(/(\w+)\.name = '([^']+)';/g)].map((m) => m[2]));
for (const key of Object.keys(STATIC_CITY_LAYERS)) {
  assert.ok(named.has(key), `app.js names no group '${key}' (the export would miss it)`);
}
for (const moving of ['cars', 'bikes', 'pedestrians', 'wind-plumes', 'zoning', 'basemap', 'terrain-overlay']) {
  assert.ok(named.has(moving) && !(moving in STATIC_CITY_LAYERS), `'${moving}' is named and left out`);
}
assert.match(app, /await import\('\.\/glb_export\.js'\)/, 'the viewer loads the exporter on demand');
assert.match(app, /timestampedFilename\(baseName, 'glb'\)/);
assert.match(app, /xSign: LOCAL_X_SIGN/);
assert.match(app, /originEasting: centerX,\s*originNorthing: centerY/);
assert.match(app, /crs: Number\.isFinite\(epsg\)/);
assert.match(html, /<option value="glb">3D model \(GLB\)/);

console.log(`glb_check: OK - ${stats.meshes} meshes, ${written.bytes} bytes, ${json.materials.length} materials`);
