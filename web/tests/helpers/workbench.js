'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Execute the actual page and inline handlers with a small DOM surface and a real API client.
async function createWorkbench(client) {
  const elements = new Map();
  const errors = [];
  const listeners = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
      classList: { add() {}, remove() {}, toggle() {} },
      appendChild() {}, remove() {}, focus() {}, setAttribute() {}, addEventListener() {}
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    OrdoApi: { createClient: () => client },
    location: { hash: '#/home', origin: 'http://127.0.0.1:8790' },
    addEventListener: (name, fn) => listeners.set(name, fn),
    document: {
      getElementById: element, querySelectorAll: () => [], querySelector: () => null,
      createElement: () => element('created'), addEventListener() {},
      documentElement: { dataset: {}, style: {}, setAttribute() {} },
      body: { appendChild() {}, classList: { add() {}, remove() {} } }
    },
    localStorage: { getItem: () => null, setItem() {} },
    navigator: { clipboard: { writeText: async () => {} } },
    console: { ...console, error: (...args) => errors.push(args.map(String).join(' ')) },
    confirm: () => true, prompt: () => null, alert() {},
    setTimeout: (fn, ms) => { const timer = setTimeout(fn, ms); timer.unref(); return timer; },
    clearTimeout, URL, URLSearchParams, Headers, FormData, File, Blob, TextDecoder, AbortController
  });
  context.window = context;
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../app.js'), 'utf8'), context, { filename: 'app.js' });
  for (let i = 0; i < 500 && context.ordoState.bootstrapping; i++) await new Promise(resolve => setTimeout(resolve, 10));
  if (context.ordoState.bootstrapping || !context.ordoApi.connected) throw new Error('Workbench bootstrap failed: ' + context.ordoState.connectionError);
  return {
    context, element, errors,
    async page(route) { context.location.hash = '#/' + route; await context.render(); return element('body').innerHTML; },
    async inline(code) { return vm.runInContext(code, context); }
  };
}

module.exports = { createWorkbench };
