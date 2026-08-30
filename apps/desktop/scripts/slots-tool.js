// 读/改 llm.enc(Electron safeStorage / Windows DPAPI 加密)。密钥不打印。
//
//   npx electron scripts/slots-tool.js show
//   npx electron scripts/slots-tool.js clear library embedding
//
// 必须在应用没跑的时候用 —— 运行中的实例内存里有一份,之后保存会盖回去。
const fs = require('fs');
const path = require('path');
const { app, safeStorage } = require('electron');

const argv = process.argv.slice(2).filter((a) => !a.startsWith('--'));
const cmd = argv[0] || 'show';
const targets = argv.slice(1);

// 直接跑脚本时 Electron 不读 package.json,userData 会落到默认的 "Electron"。
// 开发态应用(electron .)用的是 package.json 的 name,这里必须对齐,
// 否则读的是另一个目录、看起来"没有配置"。
app.setName(require(path.join(__dirname, '..', 'package.json')).name);
app.setPath('userData', path.join(app.getPath('appData'), app.getName()));

app.whenReady().then(() => {
  const file = path.join(app.getPath('userData'), 'llm.enc');
  console.log('userData =', app.getPath('userData'));
  console.log('llm.enc  =', file, fs.existsSync(file) ? '(存在)' : '(不存在)');
  if (!safeStorage.isEncryptionAvailable()) {
    console.error('safeStorage 不可用,无法读写');
    return app.exit(1);
  }
  if (!fs.existsSync(file)) { console.log('没有配置文件,无需处理'); return app.exit(0); }

  let parsed;
  try {
    parsed = JSON.parse(safeStorage.decryptString(fs.readFileSync(file)));
  } catch (e) {
    console.error('解密失败:', e.message);
    return app.exit(1);
  }
  const slots = (parsed && parsed.slots) || {};

  const dump = (label) => {
    console.log(`\n--- ${label} ---`);
    for (const name of ['qa', 'library', 'embedding']) {
      const c = slots[name];
      console.log(`  ${name.padEnd(10)}`, c
        ? `provider=${c.provider} endpoint=${c.endpoint} model=${c.model} has_key=${!!c.api_key}`
        : '(未配置)');
    }
  };
  dump('当前');

  if (cmd === 'show') return app.exit(0);
  if (cmd !== 'clear') { console.error('未知命令:', cmd); return app.exit(2); }

  for (const t of targets) {
    if (!['qa', 'library', 'embedding'].includes(t)) {
      console.error('未知槽位:', t); return app.exit(2);
    }
    delete slots[t];
  }
  dump('清除后');

  if (!slots.qa && !slots.library && !slots.embedding) {
    fs.unlinkSync(file);
    console.log('\n三个槽位全空,已删除 llm.enc(与主进程 saveLLMConfig 同语义)');
  } else {
    fs.writeFileSync(file, safeStorage.encryptString(JSON.stringify({ slots })));
    console.log('\n已写回 llm.enc');
  }
  app.exit(0);
});
