const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const renderer = fs.readFileSync(
  path.resolve(__dirname, '..', 'renderer', 'index.html'), 'utf8');

function sourceBetween(start, end) {
  const from = renderer.indexOf(start);
  const to = renderer.indexOf(end, from);
  assert.notEqual(from, -1, `missing ${start}`);
  assert.notEqual(to, -1, `missing ${end}`);
  return renderer.slice(from, to);
}

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.className = '';
    this.clientWidth = tag === 'div' ? 640 : 0;
    this._innerHTML = '';
    this._controls = new Map();
    this.classList = {
      add: (...names) => {
        const values = new Set(this.className.split(/\s+/).filter(Boolean));
        names.forEach((name) => values.add(name));
        this.className = [...values].join(' ');
      },
      remove: (...names) => {
        const removed = new Set(names);
        this.className = this.className.split(/\s+/)
          .filter((name) => name && !removed.has(name)).join(' ');
      },
    };
  }

  set innerHTML(value) {
    this._innerHTML = value;
    this.children = [];
    if (value.includes('pdf-pos')) {
      const pos = new FakeElement('span');
      pos.className = 'pdf-pos';
      pos.textContent = '';
      this._controls.set('.pdf-pos', pos);
      for (const id of ['pdfPrev', 'pdfNext', 'pdfZoomOut', 'pdfZoomIn']) {
        this._controls.set(`#${id}`, new FakeElement('button'));
      }
    }
  }

  get innerHTML() { return this._innerHTML; }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }

  querySelector(selector) {
    if (this._controls.has(selector)) return this._controls.get(selector);
    const page = selector.match(/^\.pdf-page\[data-page="(\d+)"\]$/);
    if (page) return this.find((item) =>
      item.className.split(/\s+/).includes('pdf-page') &&
      String(item.dataset.page) === page[1]);
    return null;
  }

  find(predicate) {
    for (const child of this.children) {
      if (predicate(child)) return child;
      const nested = child.find(predicate);
      if (nested) return nested;
    }
    return null;
  }

  scrollIntoView() {}
  getContext() { return {}; }
}

class FakeObserver {
  constructor(callback) { this.callback = callback; this.observed = []; this.disconnected = false; }
  observe(element) { this.observed.push(element); }
  disconnect() { this.disconnected = true; }
}

test('PDF skeletons are real page containers and the target page renders a canvas', async () => {
  const body = new FakeElement('div');
  const evidence = new FakeElement('div');
  const document = {
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => (id === 'evidence' ? evidence : null),
  };
  let renderCalls = 0;
  const page = {
    pageNumber: 1,
    getViewport: ({ scale }) => ({ width: 400 * scale, height: 600 * scale }),
    render: () => {
      renderCalls += 1;
      return { promise: Promise.resolve(), cancel() {} };
    },
  };
  const doc = { numPages: 1, getPage: async () => page };
  const session = { observer: null, renderTasks: new Set() };
  const context = {
    document,
    IntersectionObserver: FakeObserver,
    originalViewToken: 1,
    highlightPdfPage() {},
  };
  const build = sourceBetween('function buildPdfView(', 'function highlightPdfPage(');
  vm.runInNewContext(`${build}\nbuildPdfView({}, doc, body, {}, 1, session);`,
    { ...context, body, doc, session });

  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  const holder = body.find((item) => String(item.dataset.page) === '1');
  assert.ok(holder, 'page holder should exist');
  assert.match(holder.className, /\bpdf-page\b/);
  assert.doesNotMatch(holder.className, /\bpdf-page-ph\b/);
  assert.equal(holder.children[0].tagName, 'CANVAS');
  assert.equal(renderCalls, 1);
  assert.equal(session.renderTasks.size, 0);
  assert.equal(session.observer.observed.length, 1);
});

test('PDF.js receives the ordodoc URL instead of a full in-memory Uint8Array', () => {
  const openPdf = sourceBetween('function openPdfOriginal(', 'function buildPdfView(');
  assert.match(openPdf, /url: ordodocUrl\(fileId, name\)/);
  assert.doesNotMatch(openPdf, /fetchOriginalBytes|new Uint8Array\(buf\)|data:/);
});
