/* 原文查看器:文本类格式(md / txt / csv)的契约。
 *
 * 这三种格式占真实库里已登记文档的绝大多数(md 1144、txt 539、csv 15),
 * 而它们原先没有原版式渲染,详情页只能看展平的提取文本。这里钉住三件事:
 * 分派认得这些扩展名、解码顺序与后端一致、CSV 按 RFC 4180 而不是 split(',')。
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const desktopRoot = path.resolve(__dirname, '..');
const renderer = fs.readFileSync(
  path.join(desktopRoot, 'renderer', 'index.html'), 'utf8');
const css = fs.readFileSync(
  path.join(desktopRoot, 'renderer', 'workbench.css'), 'utf8');

function fn(name, signature) {
  const re = new RegExp(`function ${name}\\(${signature}\\) \\{[\\s\\S]*?\\n\\}`);
  const match = renderer.match(re);
  assert.ok(match, `renderer must define ${name}()`);
  return match[0];
}

function evaluate(source, extra = {}) {
  const context = { TextDecoder, Uint8Array, ...extra };
  vm.runInNewContext(source, context);
  return context;
}

test('viewer dispatch claims the text formats it can now render', () => {
  const { viewerKindOf } = evaluate(fn('viewerKindOf', 'nameOrExt'));
  // 扩展名与整个文件名两种入参都要认(调用点传的是 file.ext || file.name)
  assert.equal(viewerKindOf('md'), 'md');
  assert.equal(viewerKindOf('README.md'), 'md');
  assert.equal(viewerKindOf('NOTES.MARKDOWN'), 'md');
  assert.equal(viewerKindOf('a.txt'), 'text');
  assert.equal(viewerKindOf('server.log'), 'text');
  assert.equal(viewerKindOf('table.csv'), 'csv');
  // 既有两种不能被回归掉
  assert.equal(viewerKindOf('paper.pdf'), 'pdf');
  assert.equal(viewerKindOf('report.docx'), 'docx');
  // 没有原版式渲染的仍须返回 null —— 否则详情页会给出一个打不开的"原文"档
  for (const name of ['page.html', 'book.epub', 'sheet.xlsx', 'deck.pptx', '']) {
    assert.equal(viewerKindOf(name), null, `${name} 不该被认作可原版式渲染`);
  }
  // 多点文件名只看最后一段
  assert.equal(viewerKindOf('archive.tar.md'), 'md');
});

test('byte decoding matches the backend parser order, including GBK', () => {
  const { decodeBytes } = evaluate(fn('decodeBytes', 'buf'));
  const bytes = (...v) => new Uint8Array(v).buffer;

  // UTF-8 正常路径
  assert.equal(decodeBytes(new TextEncoder().encode('中文 abc').buffer), '中文 abc');

  // BOM 必须剥掉,否则首字符变成不可见的 U+FEFF
  const withBom = new Uint8Array([0xEF, 0xBB, 0xBF,
    ...new TextEncoder().encode('标题')]);
  assert.equal(decodeBytes(withBom.buffer), '标题');
  assert.ok(!decodeBytes(withBom.buffer).startsWith('﻿'));

  // GBK:Windows 上中文 txt 大量是这个编码。这两个字节不是合法 UTF-8,
  // 严格模式下会落到 gb18030 —— "中"的 GBK 编码是 D6 D0。
  assert.equal(decodeBytes(bytes(0xD6, 0xD0)), '中');

  // UTF-16 LE / BE BOM
  assert.equal(decodeBytes(bytes(0xFF, 0xFE, 0x2D, 0x4E)), '中');
  assert.equal(decodeBytes(bytes(0xFE, 0xFF, 0x4E, 0x2D)), '中');

  // 全都解不通时不许抛,回落 replace(坏字节 → U+FFFD)
  assert.doesNotThrow(() => decodeBytes(bytes(0xFF, 0xFF, 0xFF)));
});

test('CSV parsing follows RFC 4180 instead of splitting on commas', () => {
  const { parseCsv } = evaluate(fn('parseCsv', 'text'));
  // vm 里造出的数组原型属于另一个 realm,deepStrictEqual 会因此判不等 ——
  // 比结构而不比原型。
  const rows = (text) => JSON.parse(JSON.stringify(parseCsv(text)));

  // 引号内的逗号不是分隔符 —— split(',') 会在这里把单元格拆散
  assert.deepEqual(rows('a,"b,c",d'), [['a', 'b,c', 'd']]);
  // "" 是一个字面引号
  assert.deepEqual(rows('"say ""hi"""'), [['say "hi"']]);
  // 引号内的换行属于单元格,不是新行
  assert.deepEqual(rows('"line1\nline2",x'), [['line1\nline2', 'x']]);
  // CRLF 与多行
  assert.deepEqual(rows('h1,h2\r\n1,2\r\n'), [['h1', 'h2'], ['1', '2']]);
  // 空单元格保留位置
  assert.deepEqual(rows('a,,c'), [['a', '', 'c']]);
});

test('markdown rendering sanitizes before it reaches the DOM', () => {
  const source = fn('openMarkdownOriginal', 'fileId, name, jump, body, token');
  // md 是用户文件内容,可以内嵌 <script> 或 on* 属性。消毒不是可选步骤,
  // 且必须发生在赋值给 innerHTML 之前。
  assert.match(source, /DOMPurify\.sanitize\(/);
  const sanitizeAt = source.indexOf('DOMPurify.sanitize(');
  const assignAt = source.indexOf('holder.innerHTML');
  assert.ok(sanitizeAt !== -1 && assignAt !== -1);
  assert.ok(sanitizeAt < assignAt, '必须先消毒再进 DOM');
  // 绝不能把 marked 的原始输出直接塞进 DOM
  assert.doesNotMatch(source, /innerHTML\s*=\s*raw\b/);
  for (const tag of ['script', 'iframe', 'object', 'embed', 'form']) {
    assert.match(source, new RegExp(`'${tag}'`), `FORBID_TAGS 应含 ${tag}`);
  }
  assert.match(source, /ALLOW_DATA_ATTR:\s*false/);
});

test('PDF page placeholders carry the class pageWrap() looks them up by', () => {
  // 真实缺陷:占位只给了 `pdf-page-ph`，而 pageWrap() 按
  // `.pdf-page[data-page]` 找容器。类名不匹配 → renderPage 拿到 null 就
  // `return null`，于是**一页都画不出来、也不报任何错**。
  // 参数表不写死：它已经因为新增 session 参数变过一次，而这条测试要钉的是
  // 类名，不是函数签名。
  const build = renderer.match(
    /function buildPdfView\([^)]*\) \{[\s\S]*?\n\}/)[0];
  const phClass = build.match(/ph\.className = '([^']+)'/);
  assert.ok(phClass, 'buildPdfView 必须给占位设置 className');
  const tokens = phClass[1].split(/\s+/);
  assert.ok(tokens.includes('pdf-page'),
    '占位必须带 pdf-page —— pageWrap() 用它定位，缺了就静默渲染不出来');
  assert.ok(tokens.includes('pdf-page-ph'), '占位仍需骨架态类以撑出滚动条');

  // pageWrap 的选择器与上面的类名必须对得上
  assert.match(build, /pages\.querySelector\('\.pdf-page\[data-page="'/);
  // 画完要摘掉骨架态，否则 min-height 会给矮页面留一截空白
  assert.match(build, /classList\.remove\('pdf-page-ph'\)/);
});

test('PDF.js loads as a module with a resolvable specifier', () => {
  const loader = renderer.match(/function loadPdfJs\(\) \{[\s\S]*?\n\}/)[0];
  // 裸的 'vendor/...' 不是合法 module specifier，动态 import 会直接抛
  // "Failed to resolve module specifier" —— PDF 于是一个都打不开。
  assert.doesNotMatch(loader, /import\('vendor\//);
  assert.match(loader, /new URL\('\.\/vendor\/pdfjs\/pdf\.min\.mjs', document\.baseURI\)/);
  // worker 同样必须是 file://：自定义协议不被当作合法 module 来源
  assert.match(loader,
    /workerSrc\s*=\s*\n?\s*new URL\('\.\/vendor\/pdfjs\/pdf\.worker\.min\.mjs', document\.baseURI\)/);
  assert.doesNotMatch(loader, /workerSrc\s*=\s*'ordodoc:/);
  // 加载失败不缓存，否则一次失败之后永远打不开
  assert.match(loader, /pdfjsLibPromise = null/);
});

test('vendored renderers load from disk, never from the network', () => {
  // CSP 是 script-src 'self' + 两个内联哈希:任何 CDN 都会被拦。
  // 这条同时防止有人"顺手"改成外链而在开发机上恰好能用。
  for (const rel of ['vendor/marked/marked.umd.js',
    'vendor/dompurify/purify.min.js']) {
    assert.match(renderer, new RegExp(`ensureScript\\('${rel.replace(/[/.]/g, '\\$&')}'\\)`));
    assert.ok(fs.existsSync(path.join(desktopRoot, 'renderer', rel)),
      `${rel} 必须随应用分发`);
  }
  const policy = renderer.match(
    /http-equiv="Content-Security-Policy" content="([^"]+)"/)[1];
  assert.doesNotMatch(policy, /script-src[^;]*https?:/);
});

test('text-format views reuse theme tokens instead of hardcoded colors', () => {
  for (const cls of ['.md-view', '.text-view', '.csv-view']) {
    assert.ok(css.includes(cls), `workbench.css 应定义 ${cls}`);
  }
  // 取三块规则的合并文本,确认没有写死的十六进制/rgb 颜色 ——
  // 写死会在七套主题里至少一套上不可读。
  const blocks = css.match(/^\.(md-view|text-view|csv-view|csv-more)[^{]*\{[^}]*\}/gm) || [];
  assert.ok(blocks.length >= 3);
  for (const block of blocks) {
    assert.doesNotMatch(block, /:\s*#[0-9a-fA-F]{3,8}\b/, `写死颜色: ${block.slice(0, 60)}`);
    assert.doesNotMatch(block, /:\s*rgba?\(/, `写死颜色: ${block.slice(0, 60)}`);
  }
});
