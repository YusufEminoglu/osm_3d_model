// "3D model (GLB)" in the Export Studio: the static city as one binary glTF
// for Blender, SketchUp, Rhino or a 3D-printing slicer.
//
// What goes in: buildings with their roofs, roads, sidewalks and paths, the
// ground and model base, greens and water, trees, street furniture and fences
// -- the world groups named in STATIC_CITY_LAYERS. What stays out: the sky,
// lights, helpers and analysis overlays, and everything that moves (cars,
// bikes, pedestrians), because those groups are not in the list.
//
// Every material becomes a plain MeshStandardMaterial with its base colour
// (texture colours averaged in), so a viewer that knows nothing of the
// viewer's shaders still shows coloured buildings. Geometry is baked into
// metres around the study-area centre: glTF +X east, +Y up, -Z north
// (Blender turns that into +X east, +Y north, +Z up). The scene and the root
// node carry the georeference (CRS, origin easting/northing/height) and the
// OpenStreetMap attribution as glTF extras; asset.copyright carries the
// attribution too.
//
// Written with the three.js GLTFExporter (r160, MIT), vendored under
// web/assets/vendor/three/examples/jsm/exporters.
import * as THREE from 'three';
import { GLTFExporter } from 'three/addons/exporters/GLTFExporter.js';

export const GLB_ATTRIBUTION = '© OpenStreetMap contributors';

/** World group name -> node name in the GLB. Only these groups are exported. */
export const STATIC_CITY_LAYERS = Object.freeze({
  terrain: 'Ground',
  'terrain-sides': 'Model base',
  islands: 'Greens and water',
  hardscape: 'Hardscape',
  parcels: 'Parcels',
  buildings: 'Buildings',
  roads: 'Roads',
  sidewalks: 'Sidewalks',
  'pedestrian-paths': 'Paths',
  crosswalks: 'Crosswalks',
  'bike-lanes': 'Bike lanes',
  waterlines: 'Waterways',
  trees: 'Trees',
  furniture: 'Street furniture',
  fences: 'Fences'
});

// One node per OSM building (selectable in Blender); every other layer is
// merged into one mesh with a material per colour.
const PER_FEATURE_LAYERS = new Set(['buildings']);

/** False for materials the viewer does not actually draw (hidden caps, decals, glows). */
export function materialIsDrawn(material) {
  if (!material || material.visible === false || material.colorWrite === false) return false;
  if (material.transparent && !(Number(material.opacity ?? 1) > 0.01)) return false;
  if (material.transparent && material.depthWrite === false) return false;
  return true;
}

/** Meshes the export can write: a mesh or instanced mesh with positions and a drawn material. */
export function isExportableMesh(object) {
  if (!object || !(object.isMesh || object.isInstancedMesh) || object.isSkinnedMesh) return false;
  if (!object.geometry?.getAttribute?.('position')) return false;
  if (object.isInstancedMesh && !(object.count > 0)) return false;
  const materials = Array.isArray(object.material) ? object.material : [object.material];
  return materials.some(materialIsDrawn);
}

/**
 * The static city under `root` (the viewer's `world` group):
 * [{ key, label, meshes }] for each visible group named in STATIC_CITY_LAYERS.
 */
export function selectStaticCity(root) {
  const picked = [];
  for (const child of root?.children || []) {
    const label = STATIC_CITY_LAYERS[child.name];
    if (!label || !child.visible) continue;
    const meshes = [];
    child.traverseVisible((object) => {
      if (isExportableMesh(object)) meshes.push(object);
    });
    if (meshes.length) picked.push({ key: child.name, label, meshes });
  }
  return picked;
}

const textureColorCache = new WeakMap();

/** Average colour of a texture image (browser only; null when it cannot be read). */
export function averageTextureColor(texture) {
  if (!texture) return null;
  if (textureColorCache.has(texture)) return textureColorCache.get(texture);
  let result = null;
  try {
    const image = texture.image;
    if (image && typeof document !== 'undefined') {
      const canvas = document.createElement('canvas');
      canvas.width = 8;
      canvas.height = 8;
      const context = canvas.getContext('2d', { willReadFrequently: true });
      context.drawImage(image, 0, 0, 8, 8);
      const data = context.getImageData(0, 0, 8, 8).data;
      let r = 0;
      let g = 0;
      let b = 0;
      let weight = 0;
      for (let i = 0; i < data.length; i += 4) {
        const alpha = data[i + 3] / 255;
        r += data[i] * alpha;
        g += data[i + 1] * alpha;
        b += data[i + 2] * alpha;
        weight += alpha;
      }
      if (weight > 0) {
        const space = texture.colorSpace === THREE.SRGBColorSpace ? THREE.SRGBColorSpace : THREE.LinearSRGBColorSpace;
        result = new THREE.Color().setRGB(r / weight / 255, g / weight / 255, b / weight / 255, space);
      }
    }
  } catch (error) {
    result = null; // a texture the canvas may not read (tainted, not decoded yet)
  }
  textureColorCache.set(texture, result);
  return result;
}

/** The colour a material shows: its colour (or colour uniform) times its texture's average. */
export function baseColorOf(material, textureColor = averageTextureColor) {
  const color = new THREE.Color(1, 1, 1);
  if (material?.color?.isColor) {
    color.copy(material.color);
  } else {
    const uniforms = material?.uniforms || {};
    for (const name of ['color', 'diffuse', 'uColor', 'baseColor', 'uBaseColor']) {
      const value = uniforms[name]?.value;
      if (value?.isColor) {
        color.copy(value);
        break;
      }
      if (value?.isVector3 || value?.isVector4) {
        color.setRGB(value.x, value.y, value.z);
        break;
      }
    }
  }
  if (material?.map && textureColor) {
    const average = textureColor(material.map);
    if (average?.isColor) color.multiply(average);
  }
  return color;
}

/** A plain MeshStandardMaterial for `material` (shared between equal results). */
export function plainMaterial(material, cache, textureColor = averageTextureColor, tint = null) {
  const color = baseColorOf(material, textureColor);
  if (tint?.isColor) color.multiply(tint);
  const opacity = material?.transparent ? Math.max(0, Math.min(1, Number(material.opacity ?? 1))) : 1;
  const doubleSided = material?.side === THREE.DoubleSide || material?.side === THREE.BackSide;
  const vertexColors = Boolean(material?.vertexColors);
  const roughness = Number.isFinite(material?.roughness) ? material.roughness : 0.9;
  const metalness = Number.isFinite(material?.metalness) ? material.metalness : 0;
  const hex = color.getHexString(THREE.SRGBColorSpace);
  const key = [hex, opacity.toFixed(2), doubleSided, vertexColors, roughness.toFixed(2), metalness.toFixed(2)].join('|');
  if (cache.has(key)) return cache.get(key);
  const plain = new THREE.MeshStandardMaterial({
    color,
    roughness,
    metalness,
    vertexColors,
    side: doubleSided ? THREE.DoubleSide : THREE.FrontSide,
    transparent: opacity < 0.999,
    opacity
  });
  plain.name = `#${hex}${opacity < 0.999 ? ` ${Math.round(opacity * 100)}%` : ''}`;
  cache.set(key, plain);
  return plain;
}

/** Scene -> glTF axes: x' = xSign * x (east), y' = y - originHeight (up), z' = -z (south). */
export function exportFrame(xSign = -1, originHeight = 0) {
  return new THREE.Matrix4()
    .makeScale(xSign, 1, -1)
    .premultiply(new THREE.Matrix4().makeTranslation(0, -originHeight, 0));
}

class MeshAccumulator {
  constructor(name, userData = {}) {
    this.name = name;
    this.userData = userData;
    this.parts = new Map();
    this.missingNormals = false;
    this.triangles = 0;
  }

  part(material) {
    let part = this.parts.get(material);
    if (!part) {
      part = { positions: [], normals: [], colors: [], indices: [], vertices: 0 };
      this.parts.set(material, part);
    }
    return part;
  }

  hasGeometry() {
    for (const part of this.parts.values()) if (part.indices.length) return true;
    return false;
  }

  toMesh() {
    const parts = [...this.parts.entries()].filter(([, part]) => part.indices.length);
    const vertexCount = parts.reduce((sum, [, part]) => sum + part.vertices, 0);
    const indexCount = parts.reduce((sum, [, part]) => sum + part.indices.length, 0);
    const colored = parts.some(([material]) => material.vertexColors);
    const positions = new Float32Array(vertexCount * 3);
    const normals = new Float32Array(vertexCount * 3);
    const colors = colored ? new Float32Array(vertexCount * 3).fill(1) : null;
    const indices = vertexCount > 65535 ? new Uint32Array(indexCount) : new Uint16Array(indexCount);
    const geometry = new THREE.BufferGeometry();
    const materials = [];
    let vertexOffset = 0;
    let indexOffset = 0;
    parts.forEach(([material, part], materialIndex) => {
      positions.set(part.positions, vertexOffset * 3);
      normals.set(part.normals, vertexOffset * 3);
      if (colors && material.vertexColors) colors.set(part.colors, vertexOffset * 3);
      for (let i = 0; i < part.indices.length; i++) indices[indexOffset + i] = part.indices[i] + vertexOffset;
      geometry.addGroup(indexOffset, part.indices.length, materialIndex);
      materials.push(material);
      vertexOffset += part.vertices;
      indexOffset += part.indices.length;
    });
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
    if (colors) geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geometry.setIndex(new THREE.BufferAttribute(indices, 1));
    if (this.missingNormals) geometry.computeVertexNormals();
    if (materials.length === 1) geometry.clearGroups();
    const mesh = new THREE.Mesh(geometry, materials.length === 1 ? materials[0] : materials);
    mesh.name = this.name;
    mesh.userData = { ...this.userData };
    return mesh;
  }
}

const _sceneMatrix = new THREE.Matrix4();
const _instanceMatrix = new THREE.Matrix4();
const _exportMatrix = new THREE.Matrix4();
const _normalMatrix = new THREE.Matrix3();
const _point = new THREE.Vector3();
const _normal = new THREE.Vector3();
const _a = new THREE.Vector3();
const _b = new THREE.Vector3();
const _c = new THREE.Vector3();
const _tint = new THREE.Color();

function vertexIndex(index, i) {
  return index ? index.getX(i) : i;
}

/** Bake one mesh (every instance, every drawn material group) into `target`. */
function addMesh(target, mesh, frame, keepTriangle, materialFor) {
  const geometry = mesh.geometry;
  const position = geometry.getAttribute('position');
  const normal = geometry.getAttribute('normal');
  const color = geometry.getAttribute('color');
  const index = geometry.index;
  const total = index ? index.count : position.count;
  const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
  const groups = Array.isArray(mesh.material) && geometry.groups.length
    ? geometry.groups
    : [{ start: 0, count: total, materialIndex: 0 }];
  if (!normal) target.missingNormals = true;
  const instances = mesh.isInstancedMesh ? mesh.count : 1;
  for (let instance = 0; instance < instances; instance++) {
    _sceneMatrix.copy(mesh.matrixWorld);
    if (mesh.isInstancedMesh) {
      mesh.getMatrixAt(instance, _instanceMatrix);
      _sceneMatrix.multiply(_instanceMatrix);
    }
    _exportMatrix.multiplyMatrices(frame, _sceneMatrix);
    _normalMatrix.getNormalMatrix(_exportMatrix);
    const flip = _exportMatrix.determinant() < 0;
    const tint = mesh.isInstancedMesh && mesh.instanceColor ? _tint.fromBufferAttribute(mesh.instanceColor, instance) : null;
    for (const group of groups) {
      const source = materials[group.materialIndex ?? 0];
      if (!materialIsDrawn(source)) continue;
      const material = materialFor(source, tint);
      const part = target.part(material);
      const remap = new Map();
      const end = Math.min(total, group.start + group.count);
      for (let i = group.start; i + 2 < end; i += 3) {
        const ia = vertexIndex(index, i);
        const ib = vertexIndex(index, i + 1);
        const ic = vertexIndex(index, i + 2);
        if (keepTriangle) {
          _a.fromBufferAttribute(position, ia).applyMatrix4(_sceneMatrix);
          _b.fromBufferAttribute(position, ib).applyMatrix4(_sceneMatrix);
          _c.fromBufferAttribute(position, ic).applyMatrix4(_sceneMatrix);
          if (!keepTriangle(_a, _b, _c)) continue;
        }
        const corners = flip ? [ia, ic, ib] : [ia, ib, ic];
        for (const vi of corners) {
          let out = remap.get(vi);
          if (out === undefined) {
            out = part.vertices++;
            remap.set(vi, out);
            _point.fromBufferAttribute(position, vi).applyMatrix4(_exportMatrix);
            part.positions.push(_point.x, _point.y, _point.z);
            if (normal) {
              _normal.fromBufferAttribute(normal, vi).applyMatrix3(_normalMatrix).normalize();
              part.normals.push(_normal.x, _normal.y, _normal.z);
            } else {
              part.normals.push(0, 1, 0);
            }
            if (material.vertexColors) {
              if (color && color.itemSize >= 3) part.colors.push(color.getX(vi), color.getY(vi), color.getZ(vi));
              else part.colors.push(1, 1, 1);
            }
          }
          part.indices.push(out);
        }
        target.triangles++;
      }
    }
  }
}

function featureKey(mesh, fallbackIds) {
  const data = mesh.userData || {};
  const id = data.osm_id ?? data.osmId ?? data.id;
  if (id !== undefined && id !== null && String(id).trim() !== '') return `osm:${id}`;
  if (!fallbackIds.has(data)) fallbackIds.set(data, `part:${fallbackIds.size + 1}`);
  return fallbackIds.get(data);
}

function featureName(mesh, key) {
  const data = mesh.userData || {};
  const kind = data.building && data.building !== 'yes' ? String(data.building) : 'building';
  const name = data.name ? ` ${String(data.name).slice(0, 60)}` : '';
  return `${kind}${name} (${key.replace(/^osm:/, 'OSM ')})`;
}

/**
 * Build the static-city scene for the GLB (no I/O; used by exportStaticCityGlb
 * and by the node check).
 */
export function buildStaticCityScene({
  root,
  xSign = -1,
  originHeight = 0,
  georeference = {},
  clipTriangles = null,
  textureColor = averageTextureColor
}) {
  root?.updateMatrixWorld?.(true);
  const frame = exportFrame(xSign, originHeight);
  const materialCache = new Map();
  const materialFor = (material, tint) => plainMaterial(material, materialCache, textureColor, tint);
  const extras = { ...georeference, attribution: georeference.attribution || GLB_ATTRIBUTION };
  const scene = new THREE.Scene();
  scene.name = '3D OSM Model';
  scene.userData = extras;
  const model = new THREE.Group();
  model.name = '3D OSM Model';
  model.userData = extras;
  scene.add(model);
  const stats = { layers: {}, meshes: 0, triangles: 0, materials: 0 };
  for (const { key, label, meshes } of selectStaticCity(root)) {
    const keep = clipTriangles ? clipTriangles(key) : null;
    const accumulators = new Map();
    const fallbackIds = new Map();
    for (const mesh of meshes) {
      const groupKey = PER_FEATURE_LAYERS.has(key) ? featureKey(mesh, fallbackIds) : key;
      let target = accumulators.get(groupKey);
      if (!target) {
        const data = mesh.userData || {};
        const userData = PER_FEATURE_LAYERS.has(key)
          ? Object.fromEntries(['osm_id', 'name', 'building', 'height', 'min_height', 'roof_height', 'building_levels']
            .filter((field) => data[field] !== undefined && data[field] !== null && data[field] !== '')
            .map((field) => [field, data[field]]))
          : {};
        target = new MeshAccumulator(PER_FEATURE_LAYERS.has(key) ? featureName(mesh, groupKey) : label, userData);
        accumulators.set(groupKey, target);
      }
      addMesh(target, mesh, frame, keep, materialFor);
    }
    const layer = new THREE.Group();
    layer.name = label;
    let layerMeshes = 0;
    let layerTriangles = 0;
    for (const target of accumulators.values()) {
      if (!target.hasGeometry()) continue;
      layer.add(target.toMesh());
      layerMeshes++;
      layerTriangles += target.triangles;
    }
    if (!layerMeshes) continue;
    model.add(layer);
    stats.layers[key] = { label, meshes: layerMeshes, triangles: layerTriangles };
    stats.meshes += layerMeshes;
    stats.triangles += layerTriangles;
  }
  stats.materials = materialCache.size;
  return { scene, stats };
}

/**
 * Export the static city under `root` as a binary glTF (GLB).
 * Returns { glb: ArrayBuffer, stats }.
 */
export async function exportStaticCityGlb(options) {
  const { scene, stats } = buildStaticCityScene(options);
  if (!stats.meshes) {
    throw new Error('Nothing to export yet: the scene has no visible buildings, roads or ground.');
  }
  const extras = scene.userData;
  const exporter = new GLTFExporter();
  exporter.register((writer) => ({
    afterParse() {
      writer.json.asset.copyright = extras.attribution;
      writer.json.asset.generator = `${options.generator || '3D OSM Model'} (three.js GLTFExporter r${THREE.REVISION})`;
      writer.json.asset.extras = extras;
    }
  }));
  const glb = await exporter.parseAsync(scene, { binary: true, onlyVisible: true });
  scene.traverse((object) => object.geometry?.dispose?.());
  stats.bytes = glb.byteLength;
  return { glb, stats };
}

/** The JSON chunk of a GLB, or null when the bytes are not a valid GLB 2.0 file. */
export function readGlbJson(buffer) {
  const bytes = buffer instanceof ArrayBuffer ? buffer : buffer?.buffer;
  if (!bytes || bytes.byteLength < 20) return null;
  const view = new DataView(bytes, buffer.byteOffset || 0, buffer.byteLength);
  if (view.getUint32(0, true) !== 0x46546c67 || view.getUint32(4, true) !== 2) return null;
  if (view.getUint32(8, true) !== view.byteLength) return null;
  const length = view.getUint32(12, true);
  if (view.getUint32(16, true) !== 0x4e4f534a) return null;
  const text = new TextDecoder().decode(new Uint8Array(bytes, (buffer.byteOffset || 0) + 20, length));
  return JSON.parse(text);
}
