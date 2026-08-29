---
name: repack
description: 重新打包 Python sidecar（PyInstaller）并启动 Electron 桌面端，让后端改动在桌面 UI 里真正生效。改完 services/api 又需要在桌面端验证时使用。
disable-model-invocation: true
---

桌面端开发态加载的是**冻结产物** `services/api/dist/inktable-sidecar`（路径写死在 `apps/desktop/electron/main.js:89`）。改完后端不重新打包，桌面端看到的还是旧代码。

日常改后端不需要走这个流程——直接 `uv run uvicorn app.main:app` 起来验证更快。只有需要在桌面 UI 里验证时才打包。

## 步骤

### 1. 先跑后端测试

打包很慢，别把一个测试就能发现的问题带进产物。

```bash
cd services/api
uv run pytest
```

### 2. 打包 sidecar

```bash
cd services/api
uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
```

`sidecar.spec` 手动处理了两个 PyInstaller 不会自动收集的原生依赖：`sqlite_vec/vec0.dylib` 与 jieba 词典。**`upx=False` 是必须的**（UPX 会破坏 dylib 签名）。如果你改动过 `sidecar.spec` 或新增了带原生扩展的依赖，特别留意这两项。

### 3. 验证产物没有 degraded

原生依赖遗漏的表现很隐蔽：**开发态完全正常，只有冻结产物会 `/health` 报 degraded**。所以必须验产物本身，不能只信开发态跑通了。

```bash
cd services/api
INKTABLE_TOKEN=smoke ./dist/inktable-sidecar &
# 从 stdout 拿到端口号后：
curl -s http://127.0.0.1:<端口>/health
```

`/health` 是唯一不需要 Bearer Token 的端点。确认返回不是 degraded 再往下走；报了 degraded 就去查 vec0.dylib 和 jieba 词典有没有被收进去。验完记得把这个进程停掉。

### 4. 装桌面依赖（若缺）

```bash
ls apps/desktop/node_modules >/dev/null 2>&1 || (cd apps/desktop && npm install)
```

### 5. 起桌面端

```bash
cd apps/desktop && npm start
```

Electron 主进程会自己拉起 sidecar：绑定 `127.0.0.1:0`，端口经 **stdout** 回传，token 经 **stdin** 传入，启动超时 15 秒。如果窗口起来但功能空转，先看主进程日志里 sidecar 是否启动成功。

## 出发布产物才需要

```bash
cd apps/desktop && npx electron-builder --mac --arm64
```

无代码签名（`electron-builder.yml` 里 `identity: null`、`hardenedRuntime: false`），首次打开需右键→打开。
