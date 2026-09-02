# Ordo Web

Ordo Web 是统一产品服务提供的原生 HTML/CSS/JavaScript 工作台。它没有独立构建步骤，也不直连数据库；所有主数据和业务写操作通过同源 `/api/v1` 完成。

## 启动

从项目根目录启动统一服务：

```powershell
cd D:\AIApp\Ordo
npm run seed
npm start
```

打开 `http://127.0.0.1:8790/`。不要再使用独立静态服务器运行产品，因为写请求需要统一服务创建的 HttpOnly 会话和 CSRF 令牌。

## 页面范围

- 首页：真实组件健康、对象计数、任务、问答趋势和存储用量
- 知识库：知识库/数据集、上传/目录/归档/数据库登记、解析产物、块修订、Release、Wiki 与图谱
- 问答流程：七个外显阶段；结果融合作为多路召回内部 Trace
- AI 应用：固定 Release 的严格证据问答和网站助手发布
- 设置：持久设置、模型连接、备份恢复、审计和版本/API 信息
- 全局搜索和新对话知识库选择

当前不区分企业与个人，不提供账号登录、通知中心或 Workspace 切换。后端仍使用本地 `ws_local` 作为数据隔离边界。

## 检查

```powershell
npm test
node --check app.js
```
