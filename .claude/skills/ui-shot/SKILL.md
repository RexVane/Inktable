---
name: ui-shot
description: 用 tools/preview 对 Inktable 桌面 UI 做截图回归——正常态、三个边界态、深色模式计算样式。改动 renderer/index.html 或 workbench.css 后使用。
disable-model-invocation: true
---

`apps/desktop/tools/preview/` 用真实 renderer + mock 后端数据出截图，**不需要打包 sidecar、不需要真实资料库**。改 UI 后用它做视觉回归。

需要 `apps/desktop/node_modules` 存在（`npm install`）。

## 步骤

所有命令在 `apps/desktop/` 目录下执行。

### 1. 改动前先存一份对照

截图回归的前提是有 before。**先在改动前跑一遍**存到单独目录：

```bash
cd apps/desktop
npx electron tools/preview/main.js /tmp/inktable-ui-before
npx electron tools/preview/edge-states.js /tmp/inktable-ui-before-edge
```

### 2. 改动后再跑

```bash
cd apps/desktop
npx electron tools/preview/main.js /tmp/inktable-ui-after
npx electron tools/preview/edge-states.js /tmp/inktable-ui-after-edge
```

- `main.js` —— 各正常状态截图，输出目录默认 `/tmp/inktable-ui`
- `edge-states.js` —— 四个边界态：空库首启向导 / 模型未配置 / 低置信 hedge / sidecar 故障，默认 `/tmp/inktable-ui-after`

### 3. 深色模式计算样式检查

```bash
cd apps/desktop
npx electron tools/preview/check-style.js
```

注意：`check-style.js` 的路径是**硬编码**的，不接受输出目录参数。它检查深色模式下主按钮（`#askSubmit`）的计算样式，直接看它打印的结果。

### 4. 用 Read 工具看图并对比

把 before / after 的同名截图都读进来逐一比对。汇报时说清楚：

- 哪些画面变了，变成什么样
- **有没有非预期的变化**（改一处样式却影响了别的画面，是最常见的问题）
- 四个边界态是否都还正常——这些最容易在改动中被忽略

## 别忘了同步测试

`apps/desktop/tests/*.test.js` 是对 `renderer/index.html` 和 `electron/main.js` **源码做 regex 字面断言**，改引号或换变量名都会红灯。改完 UI 必须：

```bash
cd apps/desktop && npm test
```

红灯时**同步修正正则以匹配新源码**，不要放宽或删除断言。（PLAN §11.3 已把「改成行为/IPC 契约测试」列为待办，但尚未做。）
