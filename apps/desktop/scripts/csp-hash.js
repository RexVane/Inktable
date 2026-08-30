// 算出 renderer/index.html 里每个内联 <script> 的 CSP sha256，并与 meta 里
// 已登记的值比对。改过内联脚本就必须跑一次 —— 否则 CSP 静默拦掉整个脚本块。
//
// 用法：node scripts/csp-hash.js [--write]
//   --write 直接把 meta 里的 script-src 哈希替换成实测值。
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const target = path.join(__dirname, '..', 'renderer', 'index.html');
const html = fs.readFileSync(target, 'utf8');

const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)];
if (!blocks.length) {
  console.error('没找到内联 script');
  process.exit(1);
}

// HTML 解析器会把输入流里的 CRLF / CR 统一成 LF（HTML 标准的 newline
// normalization），CSP 校验的是**规范化之后**的脚本文本。所以这里必须同样
// 规范化 —— 否则文件被某个编辑器存成 CRLF 时会算出一串对不上的哈希。
const normalize = (s) => s.replace(/\r\n?/g, '\n');

const actual = blocks.map((m) => {
  const text = normalize(m[1]);
  const digest = crypto.createHash('sha256').update(text, 'utf8').digest('base64');
  return { hash: `sha256-${digest}`, chars: text.length };
});

const declared = [...html.matchAll(/'(sha256-[A-Za-z0-9+/=]+)'/g)].map((m) => m[1]);

console.log('实测：');
actual.forEach((a, i) => {
  const ok = declared.includes(a.hash);
  console.log(`  #${i + 1} ${a.hash}  (${a.chars} 字符)  ${ok ? 'CSP 已登记' : '** 未登记 **'}`);
});
console.log('meta 里登记的：');
declared.forEach((d) => {
  const ok = actual.some((a) => a.hash === d);
  console.log(`  ${d}  ${ok ? 'OK' : '** 已过期 **'}`);
});

const stale = declared.filter((d) => !actual.some((a) => a.hash === d));
const missing = actual.filter((a) => !declared.includes(a.hash));

if (!stale.length && !missing.length) {
  console.log('\n一致，无需改动。');
  process.exit(0);
}

if (!process.argv.includes('--write')) {
  console.log('\n不一致。加 --write 就地修正。');
  process.exit(1);
}

// 按出现顺序把 script-src 里的哈希整体换成实测值
let out = html;
const csp = out.match(/script-src [^;]*;/);
if (!csp) {
  console.error('meta 里找不到 script-src');
  process.exit(1);
}
const replaced = `script-src 'self' ${actual.map((a) => `'${a.hash}'`).join(' ')};`;
out = out.replace(csp[0], replaced);
fs.writeFileSync(target, out);
console.log(`\n已写入：${replaced}`);
