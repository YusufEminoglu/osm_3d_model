const encoder = new TextEncoder();

/** OpenStreetMap's required attribution (ODbL); every published export carries it. */
export const OSM_ATTRIBUTION = '© OpenStreetMap contributors';

/**
 * Size and colours of the attribution stamped on raster exports: dark text on
 * a near-white box in the bottom-right corner, 2.2 % of the shorter image
 * side (12-48 px), so it stays legible from a phone screenshot to a 4K
 * render and on any scene behind it.
 */
export function attributionStyle(width, height) {
  const fontPx = Math.round(Math.min(48, Math.max(12, Math.min(width, height) * 0.022)));
  return {
    fontPx,
    padX: Math.round(fontPx * 0.5),
    padY: Math.round(fontPx * 0.32),
    margin: Math.round(fontPx * 0.6),
    font: `600 ${fontPx}px system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif`,
    textColor: '#1f2328',
    boxColor: 'rgba(255, 255, 255, 0.86)',
    boxAlpha: 0.86
  };
}

/**
 * Stamp the OSM attribution onto a 2D canvas context of `width` x `height`.
 * Returns the box it drew ({ x, y, width, height, fontPx }).
 */
export function drawAttribution(ctx, width, height, text = OSM_ATTRIBUTION) {
  const style = attributionStyle(width, height);
  ctx.save();
  ctx.font = style.font;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  const textWidth = Math.ceil(ctx.measureText(text).width);
  const boxWidth = Math.min(width, textWidth + style.padX * 2);
  const boxHeight = style.fontPx + style.padY * 2;
  const x = Math.max(0, width - style.margin - boxWidth);
  const y = Math.max(0, height - style.margin - boxHeight);
  ctx.fillStyle = style.boxColor;
  ctx.fillRect(x, y, boxWidth, boxHeight);
  ctx.fillStyle = style.textColor;
  ctx.fillText(text, x + style.padX, y + boxHeight / 2, Math.max(1, boxWidth - style.padX * 2));
  ctx.restore();
  return { x, y, width: boxWidth, height: boxHeight, fontPx: style.fontPx };
}

export function sanitizeExportBaseName(value) {
  const cleaned = String(value || 'osm_3d_model')
    .trim()
    .replace(/\.[a-z0-9]{1,5}$/i, '')
    .replace(/[^a-z0-9_-]+/gi, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 80);
  return cleaned || 'osm_3d_model';
}

export function timestampedFilename(baseName, extension, date = new Date()) {
  const stamp = date.toISOString().replace(/[:.]/g, '-');
  const ext = String(extension || 'png').replace(/^\.+/, '').toLowerCase();
  return `${sanitizeExportBaseName(baseName)}_${stamp}.${ext}`;
}

export function escapeMarkup(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[char]);
}

export function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error('Could not read the rendered image.'));
    reader.onload = () => resolve(String(reader.result));
    reader.readAsDataURL(blob);
  });
}

export function buildSvgSnapshot(dataUrl, width, height, title = '3D OSM Model') {
  const safeTitle = escapeMarkup(title);
  const safeDataUrl = escapeMarkup(dataUrl);
  return `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${safeTitle}">
  <title>${safeTitle}</title>
  <desc>3D OSM Model scene snapshot. Map data © OpenStreetMap contributors.</desc>
  <image width="${width}" height="${height}" href="${safeDataUrl}" xlink:href="${safeDataUrl}"/>
</svg>`;
}

export function buildHtmlSnapshot(dataUrl, width, height, title = '3D OSM Model') {
  const safeTitle = escapeMarkup(title);
  const safeDataUrl = escapeMarkup(dataUrl);
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>${safeTitle}</title>
  <style>
    html,body{height:100%;margin:0;background:#171b20;color:#f8fafc;font:14px system-ui,sans-serif}
    main{min-height:100%;display:grid;place-items:center;padding:24px;box-sizing:border-box}
    figure{margin:0;max-width:100%;text-align:right}
    img{display:block;width:min(${width}px,100%);height:auto;max-height:calc(100vh - 72px);object-fit:contain;box-shadow:0 20px 55px #0008}
    figcaption{margin-top:8px;color:#cbd5e1;font-size:12px}
  </style>
</head>
<body><main><figure><img src="${safeDataUrl}" width="${width}" height="${height}" alt="${safeTitle}"><figcaption>Map data © OpenStreetMap contributors · 3D OSM Model</figcaption></figure></main></body>
</html>`;
}

function concatBytes(parts) {
  const size = parts.reduce((sum, part) => sum + part.length, 0);
  const output = new Uint8Array(size);
  let offset = 0;
  parts.forEach((part) => {
    output.set(part, offset);
    offset += part.length;
  });
  return output;
}

function pdfString(value) {
  return String(value ?? '')
    .normalize('NFKD')
    .replace(/[^\x20-\x7e]/g, '?')
    .replace(/([\\()])/g, '\\$1');
}

// A PDF string in the WinAnsi encoding of the standard Helvetica font: the
// copyright sign is byte 0xA9 (octal 251); other non-ASCII characters fall
// back to ASCII, as in pdfString.
function pdfWinAnsiString(value) {
  return String(value ?? '')
    .split('©')
    .map((part) => pdfString(part))
    .join('\\251');
}

export async function buildPdfFromJpeg(jpegBlob, imageWidth, imageHeight, title = '3D OSM Model',
  attribution = `Map data ${OSM_ATTRIBUTION}`) {
  const jpeg = new Uint8Array(await jpegBlob.arrayBuffer());
  if (jpeg.length < 4 || jpeg[0] !== 0xff || jpeg[1] !== 0xd8) {
    throw new Error('PDF export requires a valid JPEG render.');
  }

  const landscape = imageWidth >= imageHeight;
  const pageWidth = landscape ? 841.89 : 595.28;
  const pageHeight = landscape ? 595.28 : 841.89;
  const margin = 24;
  // A caption band under the image holds the OSM attribution as real text.
  const caption = 18;
  const scale = Math.min(
    (pageWidth - margin * 2) / imageWidth,
    (pageHeight - margin * 2 - caption) / imageHeight
  );
  const drawWidth = imageWidth * scale;
  const drawHeight = imageHeight * scale;
  const drawX = (pageWidth - drawWidth) / 2;
  const drawY = margin + caption + (pageHeight - margin * 2 - caption - drawHeight) / 2;
  const captionY = drawY - 13;
  const content = `q\n${drawWidth.toFixed(3)} 0 0 ${drawHeight.toFixed(3)} ${drawX.toFixed(3)} ${drawY.toFixed(3)} cm\n/Im0 Do\nQ\n`
    + `BT\n/F1 9 Tf\n0.12 0.14 0.16 rg\n${drawX.toFixed(3)} ${captionY.toFixed(3)} Td\n(${pdfWinAnsiString(attribution)}) Tj\nET\n`;
  const created = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');

  const objects = [
    [encoder.encode('1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n')],
    [encoder.encode('2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n')],
    [encoder.encode(`3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pageWidth.toFixed(2)} ${pageHeight.toFixed(2)}] /Resources << /XObject << /Im0 4 0 R >> /Font << /F1 7 0 R >> >> /Contents 5 0 R >>\nendobj\n`)],
    [
      encoder.encode(`4 0 obj\n<< /Type /XObject /Subtype /Image /Width ${imageWidth} /Height ${imageHeight} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpeg.length} >>\nstream\n`),
      jpeg,
      encoder.encode('\nendstream\nendobj\n')
    ],
    [encoder.encode(`5 0 obj\n<< /Length ${encoder.encode(content).length} >>\nstream\n${content}endstream\nendobj\n`)],
    [encoder.encode(`6 0 obj\n<< /Title (${pdfString(title)}) /Creator (3D OSM Model) /CreationDate (D:${created}) >>\nendobj\n`)],
    [encoder.encode('7 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\nendobj\n')]
  ];

  const parts = [encoder.encode('%PDF-1.4\n%OSM3D\n')];
  const offsets = [0];
  let byteLength = parts[0].length;
  objects.forEach((objectParts) => {
    offsets.push(byteLength);
    objectParts.forEach((part) => {
      parts.push(part);
      byteLength += part.length;
    });
  });
  const xrefOffset = byteLength;
  const xrefRows = offsets.slice(1).map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  parts.push(encoder.encode(`xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${xrefRows}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`));
  return new Blob([concatBytes(parts)], { type: 'application/pdf' });
}
