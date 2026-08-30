// 主题视觉回归:直接加载 renderer/index.html(不起 sidecar),逐套主题截图。
//
// 没有 sidecar 时数据区呈空态/错误态,这正好——本脚本要验的是 chrome:
// 外壳与内容卡的层级、圆角三档、墨色阶梯、accent 的落点、以及七套主题的
// token 是否都真的解析成了颜色(未定义的 var() 会静默回落成透明/继承,
// 肉眼看不出但截图能看出)。
//
// 用法:npx electron scripts/theme-shots.js [输出目录]

const { app, BrowserWindow } = require('electron');
const path = require('node:path');
const fs = require('node:fs');

const THEMES = ['nebula', 'steel', 'coal', 'moss', 'silver', 'limestone', 'linen'];
const outDir = process.argv[2] || path.resolve(__dirname, '..', '..', '..', 'output', 'theme-shots');

app.commandLine.appendSwitch('disable-gpu');
app.disableHardwareAcceleration();

// 离屏窗口的首次 capture 常常是上一屏。丢一帧再取，并统一落盘。
async function shoot(win, file) {
  await win.webContents.capturePage();
  await new Promise((r) => setTimeout(r, 360));
  const img = await win.webContents.capturePage();
  fs.writeFileSync(file, img.toPNG());
  return file;
}

async function main() {
  fs.mkdirSync(outDir, { recursive: true });
  // 失败计数贯穿全程（抽屉状态核对 + token 解析核对），最后决定退出码。
  let bad = 0;
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    show: false,
    webPreferences: {
      // 渲染层要能跑起来:它会读 window.ordo.*。这里注入一个最小假桥,
      // 而不是加载真 preload —— 真 preload 要求主进程侧的 sidecar 句柄。
      preload: path.resolve(__dirname, 'theme-shots-preload.js'),
      // show:false 的窗口会被节流，capturePage 可能取到旧帧（实测：弹层
      // 已打开而截图仍是上一屏）。关掉节流，并在每次截图前丢弃一帧。
      backgroundThrottling: false,
    },
  });

  await win.loadFile(path.resolve(__dirname, '..', 'renderer', 'index.html'));
  // 等启动脚本跑完一轮(它有若干 await api(...) 会失败并落到空态)
  await new Promise((r) => setTimeout(r, 1500));

  for (const id of THEMES) {
    await win.webContents.executeJavaScript(
      `document.documentElement.setAttribute('data-theme', ${JSON.stringify(id)});`,
    );
    await new Promise((r) => setTimeout(r, 220));
    const file = await shoot(win, path.join(outDir, `${id}.png`));
    process.stdout.write(`wrote ${file}\n`);
  }

  // 问答栏占三分之一界面,但没有 sidecar 就只有空态。注入一条假回答,
  // 让问题气泡、正文、引用小圆号(cite-chip)和引用列表都进入画面 ——
  // 这些是改动最容易碰坏、又最不容易被空态截图发现的地方。
  for (const id of ['nebula', 'linen']) {
    await win.webContents.executeJavaScript(`(() => {
      document.documentElement.setAttribute('data-theme', ${JSON.stringify(id)});
      const rows = document.getElementById('answerRows');
      rows.innerHTML =
        '<div class="qa-q">墨洞项目的检索管线是什么顺序?</div>' +
        '<div class="ans"><div class="atext">检索链路固定为分层索引到混合召回' +
        '<span class="cite-run"><span class="cite-chip">1</span>' +
        '<span class="cite-chip">2</span></span>,再经 RRF 粗融合与 Child ' +
        'Rerank<span class="cite-run"><span class="cite-chip">3</span></span>。' +
        '证据压缩保留原文区间映射,不允许用无法回溯的自由摘要代替。' +
        '<span class="cite-run"><span class="cite-chip">4</span></span></div>' +
        '<div class="refs"><div class="refs-head">引用 · 2 条</div>' +
        '<div class="ref"><span class="ref-tag">C1</span>' +
        '<span class="ref-name">PLAN.md</span></div>' +
        '<div class="ref"><span class="ref-tag">C2</span>' +
        '<span class="ref-name">RETRIEVAL-PERF.md</span></div></div></div>';
    })()`);
    await new Promise((r) => setTimeout(r, 260));
    const file = await shoot(win, path.join(outDir, `qa-${id}.png`));
    process.stdout.write(`wrote ${file}\n`);
  }

  // 抽屉三态：两栏都开、收左栏、两栏都收。收起态最容易出的错是 0 宽列里
  // 的内容漏出来，或中间卡没有把让出的宽度接过去 —— 都只有截图能看出。
  //
  // 用 localStorage + reload 设定**绝对**状态，不用 toggleDrawer 累加：
  // 切换是相对操作，跨状态连续调用会把上一轮的结果带进来，截图和标签就对
  // 不上了（第一版正是这么错的）。顺带把持久化恢复这条路径也验了。
  const drawerStates = [
    ['both', { navOpen: true, qaOpen: true }],
    ['no-nav', { navOpen: false, qaOpen: true }],
    ['no-qa', { navOpen: true, qaOpen: false }],
    ['no-both', { navOpen: false, qaOpen: false }],
  ];
  for (const [name, state] of drawerStates) {
    await win.webContents.executeJavaScript(
      `localStorage.setItem('layoutCols', ${JSON.stringify(JSON.stringify(state))});`,
    );
    await win.webContents.reload();
    await new Promise((r) => setTimeout(r, 1800));
    // 核对 DOM 真的进入了目标状态，而不是只截了张图就当通过
    const actual = await win.webContents.executeJavaScript(`(() => {
      const cols = getComputedStyle(document.querySelector('.content-layout'))
        .gridTemplateColumns.split(' ').map((v) => Math.round(parseFloat(v)));
      const vis = (id) => getComputedStyle(document.getElementById(id)).visibility;
      return { cols, nav: vis('navPanel'), qa: vis('qaPanel') };
    })()`);
    const navZero = actual.cols[0] === 0 && actual.cols[1] === 0;
    const qaZero = actual.cols[3] === 0 && actual.cols[4] === 0;
    const ok = navZero === !state.navOpen && qaZero === !state.qaOpen
      && actual.nav === (state.navOpen ? 'visible' : 'hidden')
      && actual.qa === (state.qaOpen ? 'visible' : 'hidden');
    await shoot(win, path.join(outDir, `drawer-${name}.png`));
    process.stdout.write(
      `${ok ? 'ok' : 'FAIL'} drawer-${name}  cols=[${actual.cols}] `
      + `nav=${actual.nav} qa=${actual.qa}\n`,
    );
    if (!ok) bad += 1;
  }
  await win.webContents.executeJavaScript(`localStorage.removeItem('layoutCols');`);
  await win.webContents.reload();
  await new Promise((r) => setTimeout(r, 1800));

  // 设置页里的主题选择器:七张迷你卡自己就是七套配色的样本,必须亲眼看过。
  // 同时核对浮层的两条不变式:整块单一 panel 色 + 圆角真的画出来了。
  for (const id of ['nebula', 'silver']) {
    await win.webContents.executeJavaScript(`(async () => {
      document.documentElement.setAttribute('data-theme', ${JSON.stringify(id)});
      document.getElementById('gear').click();
      await new Promise((r) => setTimeout(r, 500));
    })()`);
    await new Promise((r) => setTimeout(r, 700));
    const file = await shoot(win, path.join(outDir, `settings-${id}.png`));
    process.stdout.write(`wrote ${file}\n`);

    const sheet = await win.webContents.executeJavaScript(`(() => {
      const root = document.getElementById('sheet');
      const box = document.querySelector('.sheet-box');
      const cs = getComputedStyle(box);
      const r = box.getBoundingClientRect();
      const bgOf = (sel) => {
        const el = box.querySelector(sel);
        return el ? getComputedStyle(el).backgroundColor : 'n/a';
      };
      return {
        // display:none 的子树 getComputedStyle 照样返回值，所以必须先证明
        // 弹层真的在渲染 —— 否则这条断言在弹层没打开时也会通过（实测踩过）。
        shown: root.classList.contains('show'),
        w: Math.round(r.width), h: Math.round(r.height),
        radius: cs.borderTopLeftRadius,
        bg: cs.backgroundColor,
        head: bgOf('.sheet-head'),
        foot: bgOf('.sheet-foot'),
        nav: bgOf('.set-nav'),
      };
    })()`);
    // 页头/页脚/侧导航都必须是透明（继承 panel），不能各自上底色 ——
    // 一个浮层里出现第二、第三种灰，就是"颜色不一致"的来源。
    const transparent = (v) => v === 'rgba(0, 0, 0, 0)' || v === 'transparent';
    const flat = transparent(sheet.head) && transparent(sheet.foot) && transparent(sheet.nav);
    const rounded = parseFloat(sheet.radius) >= 12;
    const rendered = sheet.shown && sheet.w > 300 && sheet.h > 200;
    const ok = flat && rounded && rendered;
    process.stdout.write(
      `${ok ? 'ok' : 'FAIL'} sheet-${id}  shown=${sheet.shown} ${sheet.w}x${sheet.h} `
      + `radius=${sheet.radius} bg=${sheet.bg} `
      + `head/foot/nav=${[sheet.head, sheet.foot, sheet.nav].join(' | ')}\n`,
    );
    if (!ok) bad += 1;
    await win.webContents.executeJavaScript(
      `document.getElementById('btnClose').click();`);
    await new Promise((r) => setTimeout(r, 320));
  }

  // 逐套主题核对关键 token 是否真的解析出颜色。未定义的自定义属性会解析成
  // 空串,而不是报错 —— 不查就会漏掉整套主题缺 token 的情况。
  const probe = await win.webContents.executeJavaScript(`(() => {
    const keys = ['--shell','--card-bg','--panel','--ink','--accent-raw','--hairline',
                  '--sem-danger','--sem-ok','--sem-warn','--ico-folder','--shadow-md'];
    const out = {};
    for (const t of ${JSON.stringify(THEMES)}) {
      document.documentElement.setAttribute('data-theme', t);
      const cs = getComputedStyle(document.documentElement);
      const missing = keys.filter((k) => !cs.getPropertyValue(k).trim());
      const body = getComputedStyle(document.body).backgroundColor;
      out[t] = { missing, bodyBg: body };
    }
    return out;
  })()`);

  for (const [t, r] of Object.entries(probe)) {
    if (r.missing.length) { bad += 1; process.stdout.write(`MISSING ${t}: ${r.missing.join(', ')}\n`); }
    else { process.stdout.write(`ok ${t}  body=${r.bodyBg}\n`); }
  }
  app.exit(bad ? 1 : 0);
}

app.whenReady().then(main).catch((err) => {
  process.stderr.write(String((err && err.stack) || err) + '\n');
  app.exit(1);
});
