# Ordo 真实浏览器验收矩阵

- 验收日期：2026-09-03
- 服务：`http://127.0.0.1:8790/`
- 口径：20 个 hash route + 2 个 modal journey
- 页面判定：真实导航后等待 `domcontentloaded` 和异步渲染；`#pageTitle` 非空；页面正文不包含 `页面加载出错`/`TypeError`/`ReferenceError`；根文档和 `.page-scroll-container` 无意外横向溢出。
- 移动视口：`320×844`、`375×844`、`390×844`、`414×844`。
- 桌面视口：`1280×720`。
- 证据脚本：[`route-matrix.mjs`](./route-matrix.mjs)。脚本按 Browser Use 规则重新绑定标签，逐项导航并输出 JSON。

## 路由矩阵

| Route | 桌面 1280 | 移动 320 | 移动 375 | 移动 390 | 移动 414 | 最终可见证据 |
|---|---:|---:|---:|---:|---:|---|
| `#/home` | PASS | PASS | PASS | PASS | PASS | SQLite 正常；真实 dashboard 统计；无待处理事项 |
| `#/knowledge/config` | PASS | PASS | PASS | PASS | PASS | 真实知识库与向量后端状态 |
| `#/knowledge/datasets` | PASS | PASS | PASS | PASS | PASS | 数据集列表、发布状态、数据与目录树 |
| `#/knowledge/registry` | PASS | PASS | PASS | PASS | PASS | 真实在线空态；上传/目录/压缩包入口；未启用能力明确标识 |
| `#/knowledge/parsing` | PASS | PASS | PASS | PASS | PASS | 服务端 Task 已完成 100%；Artifact Markdown 预览 |
| `#/knowledge/index` | PASS | PASS | PASS | PASS | PASS | 18 个真实知识块、活动 Release v1、索引状态 |
| `#/qaflow/parse` | PASS | PASS | PASS | PASS | PASS | Query Trace 1/8；真实 Trace selector |
| `#/qaflow/embed` | PASS | PASS | PASS | PASS | PASS | Query Trace 2/8；local-hash-v1/128 维信息 |
| `#/qaflow/route` | PASS | PASS | PASS | PASS | PASS | Query Trace 3/8；检索路由输出 |
| `#/qaflow/recall` | PASS | PASS | PASS | PASS | PASS | Query Trace 4/8；多路召回输出 |
| `#/qaflow/fuse` | PASS | PASS | PASS | PASS | PASS | Query Trace 5/8；结果融合输出 |
| `#/qaflow/rerank` | PASS | PASS | PASS | PASS | PASS | Query Trace 6/8；重排输出 |
| `#/qaflow/prompt` | PASS | PASS | PASS | PASS | PASS | Query Trace 7/8；提示词构建输出 |
| `#/qaflow/answer` | PASS | PASS | PASS | PASS | PASS | Query Trace 8/8；sufficient evidence；citation 展示 |
| `#/apps/chat` | PASS | PASS | PASS | PASS | PASS | 真实会话列表、回答/引用空态或内容 |
| `#/apps/assistants` | PASS | PASS | PASS | PASS | PASS | 服务端无助手时显示诚实空态；先前 undefined.id 崩溃已修复 |
| `#/settings/general` | PASS | PASS | PASS | PASS | PASS | 真实设置回填；未支持项明确标注 |
| `#/settings/models` | PASS | PASS | PASS | PASS | PASS | 真实模型连接、可用状态和配置表单 |
| `#/settings/storage` | PASS | PASS | PASS | PASS | PASS | 真实 health/backup/storage 状态 |
| `#/settings/version` | PASS | PASS | PASS | PASS | PASS | `/version` 与 `/diagnostics` 真实字段 |

**结果：100/100 route × viewport PASS。**

## Modal journey 矩阵

| Journey | 桌面 1280 | 移动 320 | 打开证据 | 关闭证据 |
|---|---:|---:|---|---|
| 新对话 `#newChat` | PASS | PASS | `role=dialog`、`aria-modal=true`、`aria-label=对话框`、知识库选择、问题输入框；首焦点在 dialog 内；无横向溢出 | 点击 `data-close` 后 overlay 隐藏，焦点恢复 `#newChat` |
| 全局搜索 `#searchBtn` | PASS | PASS | `role=dialog`、`aria-modal=true`、`aria-label=全局搜索`、`#spotlightInput`、`#spotlightResults`；首焦点在搜索框；无横向溢出 | 点击 `data-close` 后 overlay 隐藏，焦点恢复 `#searchBtn` |

**结果：4/4 modal viewport journey PASS。**

> 说明：当前 IAB 版本的 locator/CUA 点击和原始 Escape/Space 通道在部分情况下只聚焦、不派发页面事件。`route-matrix.mjs` 记录了这一事实，并在 modal 入口使用页面中已渲染控件的 DOM click fallback；fallback 仍执行现有页面 onclick、Overlay 和焦点恢复逻辑，不调用 API 绕过 UI。该通道限制不等同于产品逻辑通过，需在支持原生键盘事件的浏览器环境补跑键盘黑盒矩阵。

## 本批发现与修复

1. `apps/assistants` 在真实在线空数据下曾因未定义条目读取 `.id` 崩溃；已加入助手条目、发布记录和数组字段防御性归一化，最终空态 PASS。
2. 320–414px 下发现数据集/解析/QA/模型等固定子网格撑宽页面；已加入 `minmax(0, …)`、`min-width:0`、移动单列和内部表格滚动规则，最终 5 个 viewport 全部无页面级溢出。
3. Overlay 已统一补齐 `role=dialog`、`aria-modal`、accessible name、同步首焦点、dialog 级 Escape/Tab 处理、关闭后的稳定 opener ID 恢复。
4. 搜索 modal 已补齐标准 `data-close` button，避免只能依赖 Escape 关闭。
5. 入口按钮保留显式 `onclick`，并统一 `data-close` 为 `type=button`。

## 尚未由本矩阵覆盖的场景

本矩阵只证明正常/空数据终态、页面级响应式布局和 modal 基本语义/关闭流程。以下目标仍未完成，不能据此宣称完整目标完成：

- 断网恢复、401、403、409、410 的真实浏览器注入与页面证据；
- failed/partial/paused/retry/cancelled Task 的完整浏览器状态矩阵；
- 支持原生真实键盘事件的环境下 Tab/Shift+Tab/Enter/Space/Escape 全链路证据，以及 axe/Lighthouse 报告；
- Secret 不进入普通 DOM、URL、日志和 toast 的全黑盒证据；
- 计划中的 RBAC/ACL、模型 capability probe、多模型 embedding 空间、OCR/VLM/页面级 PDF 路由、Golden 评测、SourceAdapter、Connector tool route、Graph projection/retrieval、Widget visitor isolation/SSE/quota/privacy 等高级后端能力。
