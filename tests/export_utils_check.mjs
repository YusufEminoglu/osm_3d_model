import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  buildHtmlSnapshot,
  buildPdfFromJpeg,
  buildSvgSnapshot,
  escapeMarkup,
  sanitizeExportBaseName,
  timestampedFilename
} from '../web/src/export_utils.js';

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
const rows = text.match(/xref\n0 7\n0000000000 65535 f \n((?:\d{10} 00000 n \n){6})/)?.[1];
assert.ok(rows);
rows.trimEnd().split('\n').forEach((row, index) => {
  const offset = Number(row.slice(0, 10));
  assert.equal(text.slice(offset, offset + 7), `${index + 1} 0 obj`);
});

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

console.log('Export utility checks passed.');
