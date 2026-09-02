'use strict';

const path = require('node:path');
const { XMLParser } = require('fast-xml-parser');
const mammoth = require('mammoth');
const ExcelJS = require('exceljs');
const { Readable } = require('node:stream');
const { unzipSync } = require('fflate');
const { AppError, hash } = require('./core');

const ALLOWED_EXTENSIONS = new Set([
  '.pdf', '.docx', '.pptx', '.xlsx', '.csv', '.md', '.txt',
  '.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.webp', '.gif',
  '.zip', '.tar', '.tar.gz', '.tgz'
]);
const IMAGE_EXTENSIONS = new Set(['.jpg','.jpeg','.png','.bmp','.tif','.tiff','.webp','.gif']);
const ARCHIVE_EXTENSIONS = new Set(['.zip','.tar','.tar.gz','.tgz']);
const MIME_BY_EXT = {
  '.pdf':'application/pdf', '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.pptx':'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.csv':'text/csv',
  '.md':'text/markdown', '.txt':'text/plain', '.jpg':'image/jpeg', '.jpeg':'image/jpeg',
  '.png':'image/png', '.bmp':'image/bmp', '.tif':'image/tiff', '.tiff':'image/tiff',
  '.webp':'image/webp', '.gif':'image/gif', '.zip':'application/zip', '.tar':'application/x-tar',
  '.tar.gz':'application/gzip', '.tgz':'application/gzip'
};

function extensionOf(filename) {
  const lower = String(filename || '').toLowerCase();
  if (lower.endsWith('.tar.gz')) return '.tar.gz';
  return path.extname(lower);
}

function normalizeText(text) {
  return String(text || '').replace(/\r\n?/g, '\n').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '').normalize('NFC');
}

function estimateTokens(text) {
  const source = String(text || '');
  const chinese = (source.match(/[\u3400-\u9fff]/g) || []).length;
  const rest = source.replace(/[\u3400-\u9fff]/g, ' ').trim();
  const words = rest ? rest.split(/\s+/).length : 0;
  return Math.max(1, Math.ceil(chinese * 0.7 + words * 1.3));
}

function blocksFromText(text, locatorType = 'line', extra = {}) {
  const normalized = normalizeText(text);
  const lines = normalized.split('\n');
  const blocks = [];
  let buffer = [];
  let start = 1;
  const flush = end => {
    const content = buffer.join('\n').trim();
    if (content) blocks.push({
      type: /^#{1,6}\s/.test(content) ? 'heading' : /^```/.test(content) ? 'code' : 'paragraph',
      contentMd: content,
      contentText: content.replace(/^#{1,6}\s+/, ''),
      locator: { type: locatorType, start, end, ...extra },
      generatedBy: 'parser', confidence: 1, warnings: []
    });
    buffer = [];
  };
  lines.forEach((line, index) => {
    if (!line.trim()) { flush(index + 1); start = index + 2; }
    else { if (!buffer.length) start = index + 1; buffer.push(line); }
  });
  flush(lines.length);
  return blocks;
}

function markdownFromBlocks(title, blocks) {
  return `# ${title}\n\n${blocks.map(block => block.contentMd).join('\n\n')}`.trim() + '\n';
}

async function detectFile(buffer, filename) {
  const extension = extensionOf(filename);
  if (!ALLOWED_EXTENSIONS.has(extension)) {
    throw new AppError(415, 'UNSUPPORTED_FORMAT', `不支持的文件格式: ${extension || '未知'}`, { allowed: [...ALLOWED_EXTENSIONS] });
  }
  let detected = null;
  try {
    const { fileTypeFromBuffer } = await import('file-type');
    detected = await fileTypeFromBuffer(buffer);
  } catch {}
  const expected = MIME_BY_EXT[extension];
  const containerTypes = ['.docx','.pptx','.xlsx'];
  if (detected && !containerTypes.includes(extension)) {
    const compatible = detected.mime === expected || (extension === '.md' || extension === '.txt' || extension === '.csv') ||
      (extension === '.tar.gz' && detected.mime === 'application/gzip');
    if (!compatible) throw new AppError(422, 'MIME_MISMATCH', '扩展名与文件内容不一致，文件已拒绝', { extension, expected, detected: detected.mime });
  }
  if (containerTypes.includes(extension) && detected && !['application/zip','application/x-zip-compressed'].includes(detected.mime)) {
    throw new AppError(422, 'MIME_MISMATCH', 'OOXML 文件不是有效 ZIP 容器', { extension, detected: detected.mime });
  }
  return { extension, mimeType: detected?.mime || expected || 'application/octet-stream', detected };
}

async function parsePdf(buffer, filename) {
  const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs');
  const loading = pdfjs.getDocument({ data: new Uint8Array(buffer), isEvalSupported: false, useSystemFonts: true });
  let document;
  try { document = await loading.promise; } catch (error) {
    if (/password/i.test(error.name || '') || /password/i.test(error.message || '')) throw new AppError(422, 'NEEDS_PASSWORD', 'PDF 需要密码才能解析');
    throw new AppError(422, 'PARSE_FAILED', 'PDF 无法读取或已损坏');
  }
  const blocks = [];
  const warnings = [];
  for (let pageNumber = 1; pageNumber <= document.numPages; pageNumber += 1) {
    const page = await document.getPage(pageNumber);
    const content = await page.getTextContent();
    const strings = content.items.map(item => item.str).filter(Boolean);
    const text = normalizeText(strings.join(' ')).trim();
    if (text) {
      blocks.push({ type: 'paragraph', contentMd: `<!-- page:${pageNumber} -->\n${text}`, contentText: text,
        locator: { type: 'page', page: pageNumber }, generatedBy: 'pdfjs', confidence: 0.92, warnings: [] });
    } else {
      warnings.push({ code: 'VISUAL_PAGE_REVIEW_REQUIRED', page: pageNumber, message: '页面没有可靠文字层，需要 OCR/VLM Provider' });
    }
    page.cleanup();
  }
  const status = blocks.length ? (warnings.length ? 'review_required' : 'publishable') : 'review_required';
  return makeResult(filename, blocks, warnings, status, { pages: document.numPages, parser: 'pdfjs-lightweight' });
}

async function parseDocx(buffer, filename) {
  let result;
  try { result = await mammoth.convertToMarkdown({ buffer }); }
  catch { throw new AppError(422, 'PARSE_FAILED', 'DOCX 无法读取或已损坏'); }
  const text = normalizeText(result.value);
  const blocks = blocksFromText(text, 'paragraph');
  const warnings = result.messages.map(message => ({ code: 'DOCX_WARNING', message: message.message }));
  return makeResult(filename, blocks, warnings, warnings.length ? 'review_required' : 'publishable', { parser: 'mammoth' });
}

function findTextNodes(value, output = []) {
  if (Array.isArray(value)) value.forEach(item => findTextNodes(item, output));
  else if (value && typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      if ((key === 'a:t' || key === 't') && typeof child === 'string') output.push(child);
      else findTextNodes(child, output);
    }
  }
  return output;
}

async function parsePptx(buffer, filename) {
  let files;
  try { files = unzipSync(new Uint8Array(buffer)); }
  catch { throw new AppError(422, 'PARSE_FAILED', 'PPTX 无法读取或已损坏'); }
  const parser = new XMLParser({ ignoreAttributes: false, preserveOrder: false });
  const slides = Object.keys(files).filter(name => /^ppt\/slides\/slide\d+\.xml$/.test(name))
    .sort((a, b) => Number(a.match(/\d+/)[0]) - Number(b.match(/\d+/)[0]));
  const blocks = [];
  slides.forEach((name, index) => {
    const xml = Buffer.from(files[name]).toString('utf8');
    const text = normalizeText(findTextNodes(parser.parse(xml)).join('\n')).trim();
    if (text) blocks.push({ type: 'paragraph', contentMd: `## 幻灯片 ${index + 1}\n\n${text}`, contentText: text,
      locator: { type: 'slide', slide: index + 1 }, generatedBy: 'ooxml-lightweight', confidence: 0.9, warnings: [] });
  });
  const warnings = [{ code: 'VISUAL_ELEMENTS_NOT_INTERPRETED', message: '图形、SmartArt 与图片已保留在原件中，语义解释需要 VLM Provider' }];
  return makeResult(filename, blocks, warnings, blocks.length ? 'review_required' : 'review_required', { parser: 'ooxml-lightweight', slides: slides.length });
}

async function parseWorkbook(buffer, filename, extension) {
  const workbook = new ExcelJS.Workbook();
  try {
    if (extension === '.csv') await workbook.csv.read(Readable.from(buffer));
    else await workbook.xlsx.load(buffer);
  } catch { throw new AppError(422, 'PARSE_FAILED', `${extension === '.csv' ? 'CSV' : 'XLSX'} 无法读取或已损坏`); }
  const blocks = [];
  const sheets = [];
  for (const sheet of workbook.worksheets) {
    const rows = [];
    sheet.eachRow({ includeEmpty: true }, row => {
      const values = [];
      for (let column = 1; column <= Math.max(sheet.columnCount, row.cellCount); column += 1) {
        const cell = row.getCell(column);
        values.push(cell.text || (cell.value === null || cell.value === undefined ? '' : String(cell.value)));
      }
      rows.push(values);
    });
    const markdown = rows.map(row => row.map(value => {
      const text = String(value);
      return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
    }).join(',')).join('\n').trim();
    const text = rows.map(row => row.join(' | ')).join('\n').trim();
    if (text) blocks.push({ type: 'table', contentMd: `## Sheet: ${sheet.name}\n\n\`\`\`csv\n${markdown}\n\`\`\``, contentText: text,
      locator: { type: 'sheet', sheet: sheet.name, startRow: 1, endRow: rows.length }, generatedBy: 'exceljs', confidence: 0.95, warnings: [] });
    sheets.push({ name: sheet.name, rows: rows.length, columns: Math.max(0, ...rows.map(row => row.length)) });
  }
  return makeResult(filename, blocks, [], 'publishable', { parser: 'exceljs', sheets });
}

async function parseImage(buffer, filename, extension) {
  const warning = extension === '.gif'
    ? { code: 'GIF_FIRST_FRAME_ONLY', message: 'GIF 首版只处理首帧；当前保留原件等待 OCR/VLM Provider' }
    : { code: 'OCR_PROVIDER_REQUIRED', message: '图片已安全登记，需配置并验证 OCR/VLM Provider 后生成文本知识' };
  return makeResult(filename, [], [warning], 'review_required', { parser: 'image-metadata', contentHash: hash(buffer) });
}

function makeResult(filename, blocks, warnings, qualityStatus, metadata = {}) {
  const title = path.basename(filename);
  const normalizedBlocks = blocks.map((block, index) => ({
    ordinal: index + 1,
    type: block.type || 'paragraph',
    contentMd: normalizeText(block.contentMd || block.contentText),
    contentText: normalizeText(block.contentText || block.contentMd),
    locator: block.locator || { type: 'unknown' },
    tokenCount: estimateTokens(block.contentText || block.contentMd),
    generatedBy: block.generatedBy || 'parser',
    confidence: Number(block.confidence ?? 1),
    warnings: block.warnings || []
  }));
  const markdown = markdownFromBlocks(title, normalizedBlocks);
  return {
    schemaVersion: 1,
    title,
    markdown,
    document: { schemaVersion: 1, title, metadata, blocks: normalizedBlocks, warnings },
    quality: {
      schemaVersion: 1,
      status: qualityStatus,
      blockCount: normalizedBlocks.length,
      warningCount: warnings.length,
      warnings,
      publishable: qualityStatus === 'publishable' && normalizedBlocks.length > 0
    },
    blocks: normalizedBlocks,
    warnings,
    qualityStatus,
    metadata
  };
}

async function parseDocument(buffer, filename) {
  const detection = await detectFile(buffer, filename);
  const extension = detection.extension;
  if (ARCHIVE_EXTENSIONS.has(extension)) throw new AppError(422, 'ARCHIVE_REQUIRES_IMPORT', '压缩包必须通过安全导入接口展开');
  let result;
  if (extension === '.md' || extension === '.txt') {
    const text = normalizeText(buffer.toString('utf8'));
    if (text.includes('\uFFFD')) throw new AppError(422, 'ENCODING_INVALID', '文本编码无法可靠识别');
    result = makeResult(filename, blocksFromText(text), [], 'publishable', { parser: 'ordo-native-text' });
  } else if (extension === '.csv' || extension === '.xlsx') result = await parseWorkbook(buffer, filename, extension);
  else if (extension === '.docx') result = await parseDocx(buffer, filename);
  else if (extension === '.pptx') result = await parsePptx(buffer, filename);
  else if (extension === '.pdf') result = await parsePdf(buffer, filename);
  else if (IMAGE_EXTENSIONS.has(extension)) result = await parseImage(buffer, filename, extension);
  else throw new AppError(415, 'UNSUPPORTED_FORMAT', `没有可用解析器: ${extension}`);
  return { ...result, detection };
}

module.exports = {
  ALLOWED_EXTENSIONS, IMAGE_EXTENSIONS, ARCHIVE_EXTENSIONS, MIME_BY_EXT,
  extensionOf, normalizeText, estimateTokens, blocksFromText, detectFile, parseDocument
};
