import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import * as exportUtils from '../web/src/export_utils.js';

const {
  buildHtmlSnapshot,
  buildPdfFromJpeg,
  buildSvgSnapshot,
  escapeMarkup,
  sanitizeExportBaseName,
  timestampedFilename
} = exportUtils;

assert.equal(sanitizeExportBaseName('../../My unsafe <scene>.png'), 'My_unsafe_scene');
assert.equal(sanitizeExportBaseName('   '), 'osm_3d_model');
assert.equal(
  timestampedFilename('city view', 'JPG', new Date('2026-07-11T12:34:56.789Z')),
  'city_view_2026-07-11T12-34-56-789Z.jpg'
);
assert.equal(escapeMarkup('<script>"x" & y</script>'), '&lt;script&gt;&quot;x&quot; &amp; y&lt;/script&gt;');

const hostileTitle = '<img src=x onerror=alert(1)>';
const dataUrl = 'data:image/png;base64,iVBORw0KGgo=';
const svg = buildSvgSnapshot(dataUrl, 1920, 1080, hostileTitle);
assert.match(svg, /viewBox="0 0 1920 1080"/);
assert.ok(!svg.includes(hostileTitle));
assert.match(svg, /&lt;img src=x onerror=alert\(1\)&gt;/);

const html = buildHtmlSnapshot(dataUrl, 1920, 1080, hostileTitle);
assert.match(html, /<!doctype html>/i);
assert.ok(!html.includes(hostileTitle));
assert.match(html, /OpenStreetMap contributors/);

// The PDF builder embeds JPEG bytes without decoding them. A minimal marker
// sequence is sufficient to verify object offsets, xref and trailer assembly.
const jpeg = new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], { type: 'image/jpeg' });
const pdf = await buildPdfFromJpeg(jpeg, 1920, 1080, 'City (north)');
assert.equal(pdf.type, 'application/pdf');
const bytes = new Uint8Array(await pdf.arrayBuffer());
const text = new TextDecoder('latin1').decode(bytes);
assert.ok(text.startsWith('%PDF-1.4'));
assert.match(text, /\/Subtype \/Image/);
assert.ok(text.includes('/Title (City \\(north\\))'));
assert.ok(text.endsWith('%%EOF\n'));

const xrefOffset = Number(text.match(/startxref\n(\d+)\n%%EOF/)?.[1]);
assert.equal(text.slice(xrefOffset, xrefOffset + 4), 'xref');
const size = Number(text.match(/trailer\n<< \/Size (\d+) /)?.[1]);
assert.ok(size >= 7, 'trailer /Size');
const rows = text.match(new RegExp(`xref\\n0 ${size}\\n0000000000 65535 f \\n((?:\\d{10} 00000 n \\n){${size - 1}})`))?.[1];
assert.ok(rows, 'xref rows match the trailer size');
rows.trimEnd().split('\n').forEach((row, index) => {
  const offset = Number(row.slice(0, 10));
  assert.equal(text.slice(offset, offset + 7), `${index + 1} 0 obj`);
});

// ---------------------------------------------------------------- OSM attribution
// ODbL: every published raster carries "© OpenStreetMap contributors". 1.9.1
// drew it on none of PNG, JPEG, PDF or video (SVG and HTML had it as text).
assert.equal(typeof exportUtils.drawAttribution, 'function',
  'export_utils.js has no drawAttribution(): 1.9.1 PNG/JPEG/PDF/video exports carry no OSM attribution');
assert.equal(exportUtils.OSM_ATTRIBUTION, '© OpenStreetMap contributors');

// The PDF page shows the attribution as real (searchable) text under the image,
// in the WinAnsi-encoded Helvetica of every PDF reader (\251 is the © sign).
assert.match(text, /\/Font << \/F1 (\d+) 0 R >>/, 'the PDF page has no font for the attribution');
const fontObject = Number(text.match(/\/Font << \/F1 (\d+) 0 R >>/)[1]);
assert.match(text, new RegExp(`${fontObject} 0 obj\\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>`));
assert.ok(text.includes('(Map data \\251 OpenStreetMap contributors) Tj'), 'the PDF page has no attribution text');
const imageBox = text.match(/q\n([\d.]+) 0 0 ([\d.]+) ([\d.]+) ([\d.]+) cm\n\/Im0 Do/);
const captionAt = text.match(/BT\n\/F1 9 Tf\n[\d. ]+ rg\n([\d.]+) ([\d.]+) Td/);
assert.ok(imageBox && captionAt, 'image and caption positions');
assert.ok(Number(captionAt[2]) < Number(imageBox[4]) && Number(captionAt[2]) > 12,
  'the caption sits on the page, below the image');

// The stamp on PNG/JPEG/WebP/video frames: a fake 2D context records the calls.
function fakeContext() {
  const calls = [];
  const state = {};
  return {
    calls,
    state,
    save() { calls.push(['save']); },
    restore() { calls.push(['restore']); },
    measureText(value) { return { width: String(value).length * Number(/(\d+)px/.exec(state.font)[1]) * 0.56 }; },
    fillRect(x, y, w, h) { calls.push(['fillRect', x, y, w, h, state.fillStyle]); },
    fillText(value, x, y, maxWidth) { calls.push(['fillText', value, x, y, maxWidth, state.fillStyle, state.font]); },
    set font(value) { state.font = value; },
    get font() { return state.font; },
    set fillStyle(value) { state.fillStyle = value; },
    get fillStyle() { return state.fillStyle; },
    set textAlign(value) { state.textAlign = value; },
    set textBaseline(value) { state.textBaseline = value; }
  };
}

function luminance(hex) {
  const channel = (value) => {
    const c = value / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

for (const [width, height, minFont] of [[320, 240, 12], [1920, 1080, 23], [3840, 2160, 47]]) {
  const ctx = fakeContext();
  const box = exportUtils.drawAttribution(ctx, width, height);
  const label = ctx.calls.find((call) => call[0] === 'fillText');
  const rect = ctx.calls.find((call) => call[0] === 'fillRect');
  assert.ok(label && rect, `${width}x${height}: no stamp drawn`);
  assert.equal(label[1], '© OpenStreetMap contributors');
  assert.ok(box.fontPx >= minFont, `${width}x${height}: ${box.fontPx}px text is too small`);
  assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.width <= width && box.y + box.height <= height,
    `${width}x${height}: the stamp leaves the image`);
  const style = exportUtils.attributionStyle(width, height);
  assert.ok(Math.abs(box.x + box.width - (width - style.margin)) < 1 && Math.abs(box.y + box.height - (height - style.margin)) < 1
    && box.y > height / 2, `${width}x${height}: the stamp is not in the bottom-right corner`);
  assert.ok(label[4] >= String(label[1]).length * box.fontPx * 0.5, 'the text is not squeezed');
  assert.equal(ctx.calls[0][0], 'save');
  assert.equal(ctx.calls.at(-1)[0], 'restore');
  // Legible on any scene: dark text over a >= 0.8 white box, even on black.
  assert.ok(style.boxAlpha >= 0.8 && rect[5] === style.boxColor && label[5] === style.textColor);
  const boxOnBlack = Math.round(255 * style.boxAlpha).toString(16).padStart(2, '0');
  const contrast = (luminance(`#${boxOnBlack.repeat(3)}`) + 0.05) / (luminance(style.textColor) + 0.05);
  assert.ok(contrast >= 7, `stamp contrast ${contrast.toFixed(1)}:1 on a black scene`);
}

const viewerHtml = await readFile(new URL('../web/src/index.html', import.meta.url), 'utf8');
const viewerApp = await readFile(new URL('../web/src/app.js', import.meta.url), 'utf8');
const ids = [...viewerHtml.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(ids.length, new Set(ids).size, 'Viewer HTML must not contain duplicate element IDs.');
[
  'export-toggle', 'export-dock', 'export-format', 'export-size-preset',
  'export-width', 'export-height', 'export-quality', 'export-filename',
  'export-scene', 'export-clipboard', 'record-format', 'record-fps',
  'record-bitrate', 'record-start', 'record-stop'
].forEach((id) => assert.ok(ids.includes(id), `Missing Export Studio control: ${id}`));
assert.ok(!viewerHtml.includes('btn-screenshot'));
assert.match(viewerApp, /initExportStudio\(\)/);
assert.match(viewerApp, /captureStream\(fps\)/);
assert.match(viewerApp, /enableSSAO:\s*false/);
assert.doesNotMatch(
  viewerApp,
  /_lastCameraMove|enableSSAO\s*&&\s*!_camMoving/,
  'SSAO must not switch when pointer or camera activity stops.'
);

// The viewer stamps what it publishes: every still capture (PNG, JPEG, WebP,
// and the JPEG inside the PDF/HTML, the PNG inside the SVG and the clipboard)
// is encoded from the attributed copy, and the recorder films the attributed
// copy that the render loop refreshes after every frame.
const body = (name) => {
  const start = viewerApp.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `app.js has no ${name}()`);
  const next = viewerApp.indexOf('\nfunction ', start + 10);
  const nextAsync = viewerApp.indexOf('\nasync function ', start + 10);
  const end = [next, nextAsync].filter((i) => i > 0).reduce((a, b) => Math.min(a, b), viewerApp.length);
  return viewerApp.slice(start, end);
};
assert.match(body('attributedFrame'), /drawAttribution\(context, width, height\)/);
assert.match(body('captureSceneImage'), /renderSceneFrame\(\);\s*const frame = attributedFrame\(width, height\);\s*const blob = await canvasToBlob\(mimeType, quality, frame\)/,
  'still exports are not encoded from the attributed frame');
assert.match(body('compositeRecordingFrame'), /drawAttribution\(recordContext,/);
assert.match(body('startSceneRecording'), /recorderStream = recordCanvas\.captureStream\(fps\)/,
  'the recorder films the bare WebGL canvas, without the attribution');
assert.doesNotMatch(body('startSceneRecording'), /renderer\.domElement\.captureStream\(fps\)/);
assert.match(body('animate'), /labelRenderer\.render\(scene, camera\);\s*\/\/[^\n]*\n\s*if \(recordContext\) compositeRecordingFrame\(\);/,
  'the render loop does not refresh the recorded frame');
// declared before the render loop starts (no temporal dead zone at startup)
assert.ok(viewerApp.indexOf('let recordContext = null;') < viewerApp.indexOf('\nanimate();'));
assert.ok(viewerHtml.includes('<option value="glb">'), 'the Export Studio has no 3D model (GLB) option');

console.log('Export utility checks passed.');
