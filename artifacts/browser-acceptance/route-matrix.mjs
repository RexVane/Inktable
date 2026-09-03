/**
 * Ordo 真实浏览器路由验收矩阵。
 *
 * 用法（在 Browser Use node_repl 会话中）：
 *   const result = await runRouteMatrix(browser, tab);
 *   nodeRepl.write(JSON.stringify(result));
 *
 * `browser` 和 `tab` 由调用方按 Browser Use 的 tabs.list()/tabs.get() 规则绑定。
 * 脚本不调用 API 绕过页面；每个条目都导航真实 hash 页面并检查最终 DOM。
 */

export const BASE_URL = 'http://127.0.0.1:8790/';
export const ROUTES = [
  'home',
  'knowledge/config',
  'knowledge/datasets',
  'knowledge/registry',
  'knowledge/parsing',
  'knowledge/index',
  'qaflow/parse',
  'qaflow/embed',
  'qaflow/route',
  'qaflow/recall',
  'qaflow/fuse',
  'qaflow/rerank',
  'qaflow/prompt',
  'qaflow/answer',
  'apps/chat',
  'apps/assistants',
  'settings/general',
  'settings/models',
  'settings/storage',
  'settings/version'
];
export const VIEWPORTS = [
  { name: 'desktop-1280x720', width: 1280, height: 720, waitMs: 1300 },
  { name: 'mobile-320x844', width: 320, height: 844, waitMs: 1500 },
  { name: 'mobile-375x844', width: 375, height: 844, waitMs: 1500 },
  { name: 'mobile-390x844', width: 390, height: 844, waitMs: 1500 },
  { name: 'mobile-414x844', width: 414, height: 844, waitMs: 2200 }
];

export async function inspectRoute(tab, route, viewport) {
  await tab.goto(`${BASE_URL}?acceptance=repro-${viewport.name}-${route}#/${route}`);
  await tab.playwright.waitForLoadState({ state: 'domcontentloaded' });
  await tab.playwright.waitForTimeout(viewport.waitMs);
  const state = await tab.playwright.evaluate(() => {
    const root = document.documentElement;
    const body = document.getElementById('body');
    const page = document.querySelector('.page-scroll-container');
    const text = (body?.innerText || '').replace(/\s+/g, ' ').trim();
    const title = (document.getElementById('pageTitle')?.innerText || '').trim();
    return {
      hash: location.hash,
      title,
      evidence: text.slice(0, 120),
      empty: /暂无|尚未创建|没有活动/.test(text),
      error: /页面加载出错|TypeError|ReferenceError/.test(text),
      rootOverflow: root.scrollWidth > root.clientWidth + 1,
      pageOverflow: Boolean(page && page.scrollWidth > page.clientWidth + 1),
      rootWidth: root.clientWidth,
      rootScrollWidth: root.scrollWidth,
      pageWidth: page?.clientWidth ?? null,
      pageScrollWidth: page?.scrollWidth ?? null
    };
  });
  return {
    route,
    viewport: viewport.name,
    status: state.hash === `#/${route}` && Boolean(state.title) && !state.error && !state.rootOverflow && !state.pageOverflow ? 'PASS' : 'FAIL',
    ...state
  };
}

export async function runRouteMatrix(browser, tab) {
  const rows = [];
  for (const viewport of VIEWPORTS) {
    await tab.setViewportSize({ width: viewport.width, height: viewport.height });
    for (const route of ROUTES) rows.push(await inspectRoute(tab, route, viewport));
  }
  return {
    generatedAt: new Date().toISOString(),
    baseUrl: BASE_URL,
    routeCount: ROUTES.length,
    viewportCount: VIEWPORTS.length,
    passCount: rows.filter(row => row.status === 'PASS').length,
    failCount: rows.filter(row => row.status === 'FAIL').length,
    rows
  };
}

export const MODAL_JOURNEYS = [
  { journey: 'new-chat', triggerId: 'newChat', triggerLabel: '＋ 新对话' },
  { journey: 'global-search', triggerId: 'searchBtn', triggerLabel: '全局搜索 (Ctrl+K)' }
];

/**
 * IAB 当前版本的 locator/CUA 点击有时只聚焦、不派发 click；因此这里先执行
 * 可观察的真实页面控件点击，失败时使用页面已渲染控件的 DOM click 作为明确标注的
 * fallback。fallback 不是 API 绕过，仍由页面现有 onclick 和 overlay 逻辑完成。
 */
export async function runModalJourneys(browser, tab) {
  const rows = [];
  for (const viewport of [VIEWPORTS[0], VIEWPORTS[1]]) {
    await tab.setViewportSize({ width: viewport.width, height: viewport.height });
    await tab.goto(`${BASE_URL}?acceptance=repro-modal-${viewport.name}#/home`);
    await tab.playwright.waitForLoadState({ state: 'domcontentloaded' });
    await tab.playwright.waitForTimeout(1700);
    for (const modal of MODAL_JOURNEYS) {
      const trigger = tab.playwright.locator(`#${modal.triggerId}`);
      if (await trigger.count() !== 1) {
        rows.push({ journey: modal.journey, viewport: viewport.name, status: 'FAIL', reason: 'trigger-not-unique' });
        continue;
      }
      let interaction = 'locator-click';
      await tab.playwright.evaluate(id => document.getElementById(id)?.focus(), modal.triggerId);
      try {
        await trigger.click();
      } catch {
        interaction = 'page-dom-click-fallback';
        await tab.playwright.evaluate(id => document.getElementById(id)?.click(), modal.triggerId);
      }
      await tab.playwright.waitForTimeout(650);
      const opened = await tab.playwright.evaluate(() => {
        const overlay = document.getElementById('overlay');
        const dialog = overlay?.querySelector('[role="dialog"]');
        return {
          opened: Boolean(dialog && !overlay.hidden),
          role: dialog?.getAttribute('role') ?? null,
          modal: dialog?.getAttribute('aria-modal') ?? null,
          label: dialog?.getAttribute('aria-label') ?? null,
          labelledby: dialog?.getAttribute('aria-labelledby') ?? null,
          focusInside: Boolean(dialog?.contains(document.activeElement)),
          active: document.activeElement?.id || document.activeElement?.tagName || '',
          overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
        };
      });
      await tab.playwright.evaluate(() => document.querySelector('[role="dialog"] [data-close]')?.click());
      await tab.playwright.waitForTimeout(350);
      const closed = await tab.playwright.evaluate(id => ({
        closed: document.getElementById('overlay')?.hidden === true,
        active: document.activeElement?.id || document.activeElement?.tagName || '',
        focusReturn: document.activeElement?.id === id
      }), modal.triggerId);
      rows.push({
        journey: modal.journey,
        viewport: viewport.name,
        status: opened.opened && opened.role === 'dialog' && opened.modal === 'true' && opened.focusInside && !opened.overflow && closed.closed && closed.focusReturn ? 'PASS' : 'FAIL',
        opened,
        closed,
        interaction
      });
    }
  }
  return { generatedAt: new Date().toISOString(), rows };
}
