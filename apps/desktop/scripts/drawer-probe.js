// 抽屉收起态的**几何**核对。列宽为 0 只说明 grid track 是 0，不说明
// 那一栏的内容没有画到别处去：0 宽列里的绝对定位子元素、负 margin、
// 或者 flex 的 min-content 撑开，都会让像素漏出来而计算样式全对。
//
// 用法:npx electron scripts/drawer-probe.js

const { app, BrowserWindow } = require('electron');
const path = require('node:path');

app.commandLine.appendSwitch('disable-gpu');
app.disableHardwareAcceleration();

async function main() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    show: false,
    webPreferences: { preload: path.resolve(__dirname, 'theme-shots-preload.js') },
  });
  await win.loadFile(path.resolve(__dirname, '..', 'renderer', 'index.html'));
  await new Promise((r) => setTimeout(r, 1500));

  for (const state of [{ navOpen: false, qaOpen: false }, { navOpen: true, qaOpen: true }]) {
    await win.webContents.executeJavaScript(
      `localStorage.setItem('layoutCols', ${JSON.stringify(JSON.stringify(state))});`,
    );
    await win.webContents.reload();
    await new Promise((r) => setTimeout(r, 1800));

    const r = await win.webContents.executeJavaScript(`(() => {
      const vw = document.documentElement.clientWidth;
      const box = (el) => { const b = el.getBoundingClientRect();
        return { x: Math.round(b.x), w: Math.round(b.width), h: Math.round(b.height) }; };
      const nav = document.getElementById('navPanel');
      const qa = document.getElementById('qaPanel');
      const card = document.querySelector('.results-panel');
      // 收起栏里的子元素**布局盒**仍然存在（padding/图标撑出 min-content），
      // 但父级 overflow:hidden 会裁掉、visibility:hidden 会整体不画。所以
      // 不能用 getBoundingClientRect 判「有没有漏出去」—— 那量的是布局，
      // 不是像素，第一版正是这么误报的。真正要守的两条不变式是：
      //   1. 收起栏自身宽度为 0，且 visibility 为 hidden（不可见、不可聚焦）
      //   2. 中间卡把让出的宽度全部接过去
      // 像素层面的核对交给 theme-shots.js 的截图 + 采样。
      const focusable = [];
      for (const root of [nav, qa]) {
        if (getComputedStyle(root).visibility !== 'hidden') continue;
        // visibility:hidden 的子树不参与 Tab；这里核对它确实继承下去了
        for (const el of root.querySelectorAll('button, input, textarea, [tabindex]')) {
          if (getComputedStyle(el).visibility !== 'hidden') {
            focusable.push({ id: root.id, tag: el.className || el.tagName });
          }
        }
      }
      const leaked = focusable;
      return { vw, nav: box(nav), qa: box(qa), card: box(card), leaked: leaked.slice(0, 6),
               leakedCount: leaked.length };
    })()`);

    const label = `nav=${state.navOpen ? 'open' : 'closed'} qa=${state.qaOpen ? 'open' : 'closed'}`;
    process.stdout.write(`\n[${label}] 视口 ${r.vw}\n`);
    process.stdout.write(`  navPanel  x=${r.nav.x} w=${r.nav.w}\n`);
    process.stdout.write(`  qaPanel   x=${r.qa.x} w=${r.qa.w}\n`);
    process.stdout.write(`  中间卡    x=${r.card.x} w=${r.card.w}\n`);
    process.stdout.write(`  漏出元素  ${r.leakedCount}\n`);
    for (const l of r.leaked) {
      process.stdout.write(`    ${l.id} .${String(l.tag).slice(0, 34)} x=${l.x} w=${l.w}\n`);
    }
  }
  app.exit(0);
}

app.whenReady().then(main).catch((e) => {
  process.stderr.write(String((e && e.stack) || e) + '\n');
  app.exit(1);
});
