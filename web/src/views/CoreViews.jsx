import { useMemo, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { useApp } from '../state/AppContext.jsx';
import { sx } from '../utils/style.js';

const metricCards = [
  ['📚', '数据集总数', '3 个', 'var(--accent-soft)', 'var(--accent)'],
  ['📄', '文档资产', '2,783 篇', 'var(--blue-soft)', 'var(--blue)'],
  ['⚡', '解析队列', '38 项处理中', 'var(--warn-soft)', 'var(--warn)'],
  ['🥞', '活跃索引版本', 'v7 (8,610 向量)', 'var(--purple-soft)', 'var(--purple)']
];

export function HomeView() {
  const navigate = useNavigate();
  const quick = [
    ['📂 数据集管理', '管理企业各部门知识集合、维护多层目录树与原始资产。', '/knowledge/datasets'],
    ['⚡ 数据解析流水线', '路由分流、OCR 与版面分析、双栏版面高保真比对与清洗。', '/knowledge/parsing'],
    ['🧩 构建知识索引', '知识切块、1536 维向量化、HNSW 图索引与一致性 DAG 视图。', '/knowledge/index']
  ];
  return <div>
    <div className="page-head"><div><h1>工作台总览</h1><p>Ordo 企业级本地知识引擎运行状态与快速导航</p></div></div>
    <div className="grid grid-4" style={{ marginBottom: 24 }}>
      {metricCards.map(([icon, label, value, bg, color]) => <div className="card" key={label}><div className="card-body" style={sx('padding:18px 20px;display:flex;align-items:center;gap:16px')}><div style={{ width: 44, height: 44, borderRadius: 10, background: bg, color, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 22 }}>{icon}</div><div><div className="muted" style={{ fontSize: 12.5 }}>{label}</div><b style={{ fontSize: 22, color: 'var(--ink-strong)' }}>{value}</b></div></div></div>)}
    </div>
    <div className="grid grid-3">
      {quick.map(([title, text, route]) => <div className="card" key={route} style={{ cursor: 'pointer' }} onClick={() => navigate(route)}><div className="card-body" style={{ padding: 22 }}><h3 style={sx('font-size:16px;margin-bottom:8px;color:var(--ink-strong)')}>{title}</h3><p className="muted" style={sx('font-size:13px;line-height:1.5')}>{text}</p></div></div>)}
    </div>
  </div>;
}

export function ConfigView() {
  const app = useApp();
  const [chunkSize, setChunkSize] = useState(512);
  const [chunkOverlap, setChunkOverlap] = useState(50);
  return <div>
    <div className="page-head"><div><h1>知识库管理</h1><p>一体化知识库管理工作台：管理已有知识库、分块切片策略、向量模型与持久化存储</p></div><div className="page-actions"><button className="btn primary" style={sx('background:var(--accent);color:#fff;height:36px;padding:0 18px;border-radius:6px;font-size:13.5px')} onClick={() => app.showToast('配置已持久化保存！', 'ok')}>保存全局配置</button></div></div>
    <div className="grid grid-2" style={sx('min-height:calc(100vh - 175px);align-items:stretch;gap:20px')}>
      <div className="card" style={sx('margin:0;display:flex;flex-direction:column;height:100%;padding:22px 26px')}>
        <h2 style={sx('font-size:16px;font-weight:700;color:var(--ink-strong);margin-bottom:18px;border-bottom:1px solid var(--line);padding-bottom:12px')}>分块切片策略 (Chunking)</h2>
        <Field label="分块大小 (Chunk Size in Tokens)" hint="推荐值 300 ~ 800 Tokens，平衡段落语义完整度与模型检索精度"><input className="input" value={chunkSize} onChange={e => setChunkSize(Number(e.target.value))} style={fieldStyle} /></Field>
        <Field label="重叠长度 (Chunk Overlap in Tokens)" hint="相邻知识块之间保留的重叠上下文，防止截断边界关键语义"><input className="input" value={chunkOverlap} onChange={e => setChunkOverlap(Number(e.target.value))} style={fieldStyle} /></Field>
        <Field label="语义分段符优先序"><input className="input" defaultValue={'\\n\\n, \\n, 。, ；, ；'} style={fieldStyle} /></Field>
      </div>
      <div className="card" style={sx('margin:0;display:flex;flex-direction:column;height:100%;padding:22px 26px')}>
        <h2 style={sx('font-size:16px;font-weight:700;color:var(--ink-strong);margin-bottom:18px;border-bottom:1px solid var(--line);padding-bottom:12px')}>向量模型与存储引擎</h2>
        <Field label="主力向量模型 (Embedding Model)"><select className="select" style={fieldStyle}><option>text-embedding-3-small (1536 维 - 默认推荐)</option><option>bge-m3 (1024 维 - 本地稠密+稀疏多功能)</option><option>text-embedding-3-large (3072 维 - 极限精度)</option></select></Field>
        <Field label="底层向量数据库 (Vector DB Engine)"><select className="select" style={fieldStyle}><option>SQLite-VSS (本地嵌入式轻量图索引 - 推荐)</option><option>Milvus 2.4+ (分布式云原生向量集群)</option><option>Qdrant (Rust 高速单机/集群引擎)</option></select></Field>
        <Field label="距离度量算法 (Metric)"><div style={sx('display:flex;gap:16px;font-size:13px')}><label><input type="radio" name="metric" defaultChecked /> 余弦相似度 (Cosine)</label><label><input type="radio" name="metric" /> 点积 (IP / Inner Product)</label><label><input type="radio" name="metric" /> 欧氏距离 (L2)</label></div></Field>
      </div>
    </div>
  </div>;
}

const fieldStyle = sx('height:36px;font-size:13.5px;padding:0 12px;border-radius:6px;background:var(--card-bg);border:1px solid var(--line);color:var(--ink);width:100%');
function Field({ label, hint, children }) {
  return <div className="form-group" style={{ marginBottom: 18 }}><label className="muted" style={sx('font-size:13px;margin-bottom:6px;display:block')}>{label}</label>{children}{hint && <span className="muted" style={sx('font-size:12px;margin-top:4px;display:block')}>{hint}</span>}</div>;
}

const datasetItems = [{ id: 'ds-1', name: 'Test', count: 1284 }, { id: 'ds-2', name: 'Ordo 示例知识库', count: 0 }];
const folders = [
  { name: 'Test', level: 0, count: 1284, open: true }, { name: '01 快速入门', level: 1, count: 128 },
  { name: '02 安装部署', level: 1, count: 162 }, { name: '03 功能说明', level: 1, count: 512, open: true },
  { name: '3.1 用户管理', level: 2, count: 68 }, { name: '3.4 报表与分析', level: 2, count: 72, icon: '📄' }
];

export function DatasetsView() {
  const app = useApp();
  const navigate = useNavigate();
  const [active, setActive] = useState(datasetItems[0]);
  const [selectedFolder, setSelectedFolder] = useState('3.4 报表与分析');
  return <div>
    <div className="page-head"><div><h1>数据集</h1><p>统一组织数据集、层级目录树与文档资产</p></div><div className="page-actions"><button className="btn primary" style={primaryButton} onClick={() => app.showToast('已打开新建数据集面板')}>+ 新建数据集</button></div></div>
    <div className="dataset-layout-root" style={sx('min-height:calc(100vh - 175px);align-items:stretch;display:grid;grid-template-columns:260px 1fr;gap:18px')}>
      <div className="dataset-left-card" style={sx('display:flex;flex-direction:column;height:100%;background:var(--card-bg);border:1px solid var(--line);border-radius:var(--radius-card);overflow:hidden')}>
        <div className="dataset-left-header" style={sx('padding:14px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;font-weight:600')}><span>数据集 ({datasetItems.length})</span><span style={{ cursor: 'pointer', fontSize: 18 }} onClick={() => app.showToast('已打开新建数据集面板')}>+</span></div>
        <div style={{ flex: 1, overflowY: 'auto' }}>{datasetItems.map(ds => <div key={ds.id} className={`dataset-list-item${ds.id === active.id ? ' active' : ''}`} style={sx('padding:14px 18px;border-bottom:1px solid var(--line-soft);cursor:pointer;display:flex;justify-content:space-between;align-items:center;transition:background .15s ease')} onClick={() => setActive(ds)}><div style={{ minWidth: 0 }}><b style={sx('display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13.5px;color:var(--ink-strong)')}>{ds.name}</b><div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{ds.count} 文件</div></div><span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: ds.id === active.id ? 'var(--accent)' : 'transparent' }} /></div>)}</div>
      </div>
      <div className="dataset-main-card" style={sx('display:flex;flex-direction:column;height:100%;background:var(--card-bg);border:1px solid var(--line);border-radius:var(--radius-card);padding:18px 22px')}>
        <div style={sx('display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--line)')}><div style={{ display: 'flex', gap: 12 }}>{['📁 新建文件夹', '⛛ 筛选', '↻ 刷新'].map(label => <button key={label} className="btn sm" style={smallButton} onClick={() => app.showToast(`${label.slice(2)}已就绪`)}>{label}</button>)}</div><span className="muted" style={{ fontSize: 12 }}>点击左侧文件查看详细信息</span></div>
        <div style={sx('display:grid;grid-template-columns:240px 1fr;gap:20px;flex:1;min-height:480px')}>
          <div style={sx('border-right:1px solid var(--line-soft);padding-right:16px;display:flex;flex-direction:column')}><div style={sx('display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;font-weight:600;font-size:13px;color:var(--ink-strong)')}><span>目录树</span><span className="muted" onClick={() => app.showToast('目录树已更新')}>↻</span></div><div style={sx('display:flex;flex-direction:column;gap:4px;font-size:13px')}>{folders.map(item => <div key={item.name} style={{ paddingLeft: item.level * 16, paddingTop: 6, paddingBottom: 6, paddingRight: 8, borderRadius: 4, cursor: 'pointer', display: 'flex', justifyContent: 'space-between', background: selectedFolder === item.name ? 'var(--accent-soft)' : 'transparent', color: selectedFolder === item.name ? 'var(--accent)' : 'var(--ink)' }} onClick={() => setSelectedFolder(item.name)}><div style={sx('display:flex;align-items:center;gap:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap')}><span style={{ fontSize: 10 }}>{item.open ? '∨' : '›'}</span><span>{item.icon || '📁'}</span><span>{item.name}</span></div><span className="muted" style={{ fontSize: 11 }}>{item.count}</span></div>)}</div></div>
          <div style={sx('display:flex;flex-direction:column;justify-content:center;align-items:center;padding:40px 20px;text-align:center')}><div style={{ fontSize: 48, marginBottom: 14 }}>📁</div><h3 style={sx('font-size:15px;color:var(--ink-strong);margin-bottom:6px')}>该文件夹暂无文件</h3><p className="muted" style={{ fontSize: 13, marginBottom: 20 }}>可在「数据登记」中导入原始资料并分配至此路径</p><button className="btn primary" style={primaryButton} onClick={() => navigate('/knowledge/registry')}>前往数据登记导入文档</button></div>
        </div>
        <div className="muted" style={sx('margin-top:auto;padding-top:14px;border-top:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;font-size:12.5px')}><span>共 {active.count} 条文件</span><div style={{ display: 'flex', gap: 6 }}>{['<', '1', '2', '3', '>'].map((x, i) => <button className="btn sm" key={`${x}-${i}`} style={i === 1 ? sx('padding:2px 8px;border-radius:4px;background:var(--accent);color:#fff;font-weight:700') : sx('padding:2px 8px;border:1px solid var(--line);border-radius:4px')}>{x}</button>)}</div></div>
      </div>
    </div>
  </div>;
}

const primaryButton = sx('background:var(--accent);color:#fff;height:36px;padding:0 18px;border-radius:6px;font-size:13.5px');
const smallButton = sx('border:1px solid var(--line);background:var(--card-bg);height:32px;padding:0 12px;border-radius:6px;font-size:13px');

const sources = [{ name: '企业知识资料库', icon: '🏢', count: 128 }, { name: 'WebDAV 挂载盘', icon: '☁️', count: 36 }, { name: 'PostgreSQL 业务库', icon: '🗄️', count: 12 }, { name: '本机导入资料', icon: '💻', count: 78 }];
const files = [{ name: '用户手册 产品A.pdf', icon: '📄', size: '12.4 MB' }, { name: '技术规范手册 v2.pdf', icon: '📄', size: '8.7 MB' }, { name: '企业安全制度规范.docx', icon: '📝', size: '3.2 MB' }, { name: '产品架构设计与API规范.md', icon: '📜', size: '480 KB' }, { name: '2024Q3季度研发复盘.pptx', icon: '📊', size: '24.1 MB' }];

export function RegistryView() {
  const app = useApp();
  const [activeSource, setActiveSource] = useState(sources[0].name);
  const [currentFile, setCurrentFile] = useState(files[0]);
  return <div>
    <div className="page-head"><div><h1>数据登记</h1><p>统一组织接入企业本地文件、WebDAV 共享与外部业务数据源</p></div><div className="page-actions"><button className="btn" style={smallButton} onClick={() => app.showToast('正在同步外部数据源...')}>↻ 批量同步</button><button className="btn primary" style={primaryButton} onClick={() => app.showToast('已接入新数据源', 'ok')}>+ 接入新数据源</button></div></div>
    <div className="dataset-layout-root" style={sx('min-height:calc(100vh - 175px);align-items:stretch;display:grid;grid-template-columns:240px 1fr 280px;gap:16px')}>
      <Panel title="数据源目录"><div style={sx('padding:10px;display:flex;flex-direction:column;gap:4px')}>{sources.map(ds => <div key={ds.name} className={`dataset-list-item${activeSource === ds.name ? ' active' : ''}`} style={{ padding: '10px 12px', borderRadius: 6, cursor: 'pointer', display: 'flex', justifyContent: 'space-between', background: activeSource === ds.name ? 'var(--accent-soft)' : 'transparent', color: activeSource === ds.name ? 'var(--accent)' : 'var(--ink)' }} onClick={() => setActiveSource(ds.name)}><span style={{ fontSize: 13 }}>{ds.icon} {ds.name}</span><span className="muted" style={{ fontSize: 11.5 }}>{ds.count}</span></div>)}</div></Panel>
      <div className="card" style={sx('margin:0;display:flex;flex-direction:column;height:100%')}><div className="card-head" style={panelHead}><div><b>已发现文件</b> <span className="muted" style={{ fontSize: 12.5 }}>(共 {files.length} 篇文档)</span></div><button className="btn sm" style={smallButton} onClick={() => app.showToast('所选文件已分配至数据集', 'ok')}>📥 导入至数据集</button></div><div className="card-body" style={{ padding: 0, overflowY: 'auto' }}><table className="table" style={sx('width:100%;font-size:13px;border-collapse:collapse')}><thead><tr style={sx('background:var(--inset);border-bottom:1px solid var(--line);text-align:left')}><th style={{ padding: '10px 16px', width: 30 }}><input type="checkbox" defaultChecked /></th><th style={{ padding: '10px 16px' }}>文档名称</th><th style={{ padding: '10px 16px' }}>大小</th><th style={{ padding: '10px 16px' }}>登记状态</th></tr></thead><tbody>{files.map(file => <tr key={file.name} style={{ borderBottom: '1px solid var(--line-soft)', cursor: 'pointer', background: currentFile.name === file.name ? 'var(--accent-soft)' : 'transparent' }} onClick={() => setCurrentFile(file)}><td style={{ padding: '10px 16px' }}><input type="checkbox" defaultChecked /></td><td style={sx('padding:10px 16px;font-weight:600;color:var(--ink-strong)')}>{file.icon} {file.name}</td><td style={sx('padding:10px 16px;color:var(--ink-dim)')}>{file.size}</td><td style={{ padding: '10px 16px' }}><span className="badge ok">✓ 已登记</span></td></tr>)}</tbody></table></div></div>
      <Panel title="数据源属性"><div style={sx('padding:16px;display:flex;flex-direction:column;gap:14px;font-size:12.5px')}><Info label="选中文件"><b style={{ fontSize: 14 }}>{currentFile.name}</b></Info><Info label="物理存储路径"><code className="mono" style={sx('font-size:11px;background:var(--inset);padding:4px 8px;border-radius:4px;display:block;word-break:break-all')}>D:/DataStore/Docs/{currentFile.name}</code></Info><Info label="字符编码检测"><span className="badge ok">UTF-8 (置信度 100%)</span></Info><Info label="自动清洗规则">去除页眉页脚、OCR 乱码过滤、去重保护</Info><button className="btn primary" style={{ ...primaryButton, marginTop: 'auto' }} onClick={() => app.showToast('已下发至Station 3数据解析队列', 'ok')}>🚀 立即发送至数据解析流水线</button></div></Panel>
    </div>
  </div>;
}

const panelHead = sx('padding:12px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center');
function Panel({ title, children }) { return <div className="card" style={sx('margin:0;display:flex;flex-direction:column;height:100%')}><div className="card-head" style={panelHead}><span style={sx('font-weight:700;font-size:13px;color:var(--ink-strong)')}>{title}</span></div>{children}</div>; }
function Info({ label, children }) { return <div><div className="muted" style={{ marginBottom: 3 }}>{label}</div>{children}</div>; }

export function ParsingView() {
  const app = useApp();
  const stages = [['🧭', '检测与路由', '10,852 / 10,852', 'var(--accent-soft)'], ['📄', '解析', '10,814 / 10,852', 'var(--blue-soft)'], ['✨', '清理', '10,814 / 10,814', 'var(--purple-soft)'], ['📦', 'Markdown / JSON', '8,652 块就绪', 'var(--warn-soft)']];
  return <div>
    <div style={sx('display:flex;align-items:center;justify-content:space-between;margin-bottom:16px')}><div style={{ display: 'flex', gap: 18 }}><span className="muted">知识库</span><div className="page-size-selector">产品文档库 ⌄</div><span className="muted">运行</span><div className="page-size-selector">默认解析运行 ⌄</div></div><div style={{ display: 'flex', gap: 10 }}>{['▶ 开始解析', '⏸ 暂停', '↻ 重试失败'].map((label, index) => <button key={label} className={`btn${index === 0 ? ' primary' : ''}`} style={index === 0 ? primaryButton : smallButton} onClick={() => app.showToast(label)}>{label}</button>)}</div></div>
    <div className="parsing-pipeline-row" style={sx('display:grid;grid-template-columns:1fr auto 1fr auto 1fr auto 1fr;gap:12px;align-items:center;margin-bottom:18px')}>{stages.flatMap((stage, index) => { const card = <div className="parsing-node-card" key={stage[1]} style={sx('background:var(--card-bg);border:1px solid var(--line);border-radius:8px;padding:12px 14px;display:flex;align-items:center;gap:12px')}><div style={{ width: 38, height: 38, borderRadius: 8, background: stage[3], display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 18 }}>{stage[0]}</div><div style={{ flex: 1 }}><b style={{ fontSize: 13 }}>{stage[1]}</b><div className="muted" style={{ fontSize: 11.5 }}>{stage[2]}</div></div><span style={{ width: 20, height: 20, borderRadius: '50%', background: index === 3 ? 'var(--warn)' : 'var(--accent)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>{index === 3 ? '!' : '✓'}</span></div>; return index < 3 ? [card, <span key={`arrow-${index}`}>➔</span>] : [card]; })}</div>
    <div className="parsing-three-columns" style={sx('display:grid;grid-template-columns:240px 1fr 300px;gap:16px;align-items:stretch;min-height:calc(100vh - 280px)')}>
      <Panel title="任务队列"><div style={{ padding: 10 }}><Task active name="用户手册 产品A.pdf" note="第 45 页 / 共 128 页" /><Task name="技术规范手册 v2.pdf" note="排队中 (第 12 项)" /></div></Panel>
      <div className="card" style={sx('margin:0;display:flex;flex-direction:column;height:100%')}><div className="card-head" style={panelHead}><b style={{ fontSize: 13 }}>文档预览 (用户手册 产品A.pdf)</b><span className="muted">&lt; 45 / 128 &gt;　- 90% +</span></div><div className="card-body" style={sx('padding:16px;display:flex;gap:14px;flex:1;background:var(--inset);overflow:hidden')}><div style={sx('width:48px;display:flex;flex-direction:column;gap:8px')}>{[1,2,3,4,5,6].map(p => <div key={p} style={{ height: 36, borderRadius: 4, border: `1px solid ${p === 4 ? 'var(--accent)' : 'var(--line)'}`, background: 'var(--card-bg)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11 }}>{p}</div>)}</div><div style={sx('flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden')}><div style={sx('width:100%;max-width:760px;aspect-ratio:16/9;background:#fff;color:#1e293b;border-radius:6px;box-shadow:0 6px 24px rgba(0,0,0,.12);padding:24px 32px;display:flex;flex-direction:column;justify-content:space-between')}><div><h2 style={{ fontSize: 18, color: '#0f172a' }}>3.2 产品功能</h2><p style={{ fontSize: 12.5, color: '#64748b' }}>产品提供以下核心功能模块，支持用户快速从原始数据导入到分析决策的全流程管理。</p><div style={sx('display:grid;grid-template-columns:1fr 1fr;gap:10px')}><Feature title="数据接入" items={['支持多种数据源接入','自动化清洗与校验']} green /><Feature title="数据清洗" items={['智能提取关键段落','高质量切块切片']} /></div></div><div style={sx('font-size:11px;color:#94a3b8;display:flex;justify-content:space-between')}><span>用户手册 产品A.pdf</span><span>第 45 页 / 共 128 页</span></div></div></div></div></div>
      <Panel title="页面信息 (第 45 页)"><div style={sx('padding:14px;display:flex;flex-direction:column;gap:12px;font-size:12px')}><Row label="路由决策"><span className="mono" style={{ color: 'var(--accent)' }}>PDF 纯文本流, 文本密度 78% &gt;</span></Row><Row label="引擎选择"><span className="badge ok">pypdf</span></Row><Row label="文字质量"><b style={{ color: 'var(--accent)' }}>96%</b></Row><div style={sx('margin-top:10px;border-top:1px solid var(--line);padding-top:10px')}><b>内容对比</b><div style={sx('background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:10px;font-size:11.5px;line-height:1.5;margin-top:6px')}><b>解析后 (Markdown)</b><p style={{ color: 'var(--accent)', margin: 0 }}>3.2 产品功能<br />产品提供以下核心功能模块，支持用户快速从原始数据接入到分析决策的全流程管理。</p></div></div></div></Panel>
    </div>
  </div>;
}

function Task({ name, note, active }) { return <div style={{ padding: '10px 12px', background: active ? 'var(--accent-soft)' : 'var(--card-bg)', border: `1px solid ${active ? 'var(--accent)' : 'var(--line)'}`, borderRadius: 6, marginBottom: 8 }}><div style={{ fontSize: 13, fontWeight: 600 }}>{name}</div><div className="muted" style={{ fontSize: 11.5 }}>{note}</div></div>; }
function Feature({ title, items, green }) { return <div style={{ padding: 10, background: green ? '#f0fdf4' : '#eff6ff', border: `1px solid ${green ? '#bbf7d0' : '#bfdbfe'}`, borderRadius: 6 }}><b style={{ color: green ? '#166534' : '#1e40af', fontSize: 12 }}>{title}</b><ul style={{ fontSize: 11, color: green ? '#15803d' : '#1d4ed8', marginTop: 4, paddingLeft: 14 }}>{items.map(x => <li key={x}>{x}</li>)}</ul></div>; }
function Row({ label, children }) { return <div style={{ display: 'flex', justifyContent: 'space-between' }}><span className="muted">{label}</span>{children}</div>; }

const qaStages = ['问题解析', '问题向量化', '检索路由', '多路召回', '结果融合', '重排', '构建提示词', '回答生成'];
const stageKeys = ['parse', 'embed', 'route', 'recall', 'fuse', 'rerank', 'prompt', 'answer'];
export function QAFlowView() {
  const app = useApp();
  const { stage } = useParams();
  const initial = Math.max(0, stageKeys.indexOf(stage));
  const [current, setCurrent] = useState(initial + 1);
  const label = qaStages[current - 1];
  return <div><div className="page-head"><div><h1>问答流程诊断中枢</h1><p>端到端观测并调优从用户提问到回答生成的 8 大核心阶段</p></div></div><div className="index-stepper-track" style={{ marginBottom: 24 }}><div className="index-stepper-indicator" style={{ width: '12.5%', left: `${(current - 1) * 12.5}%` }} /><div style={sx('display:grid;grid-template-columns:repeat(8,1fr);width:100%;align-items:center')}>{qaStages.map((name, index) => <div key={name} style={{ textAlign: 'center', cursor: 'pointer', padding: '8px 0', color: current === index + 1 ? 'var(--accent)' : 'var(--ink-dim)', fontWeight: current === index + 1 ? 700 : 400, fontSize: 13 }} onClick={() => setCurrent(index + 1)}>{name}</div>)}</div></div><div className="card" style={sx('min-height:calc(100vh - 240px);display:flex;flex-direction:column;padding:26px 30px')}><div style={sx('display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;border-bottom:1px solid var(--line);padding-bottom:14px')}><div><h2 style={{ fontSize: 16 }}>当前阶段诊断：第 {current} 站 · {label}</h2><p className="muted">实时追踪本阶段的入参、路由策略决策与处理延迟</p></div><button className="btn primary" style={primaryButton} onClick={() => app.showToast('已下发回放重放测试', 'ok')}>▶ 重新执行本阶段测试</button></div><div style={sx('display:grid;grid-template-columns:1fr 1fr;gap:20px;flex:1')}><CodeBox title="阶段输入数据 (Input Payload)">{`{\n  "query": "什么是产品A的核心功能？",\n  "stage": "${label}",\n  "traceId": "trace_20260904_qa_01",\n  "timestamp": "2026-09-04T01:00:00Z"\n}`}</CodeBox><CodeBox title="阶段执行指标与产物 (Output)" accent>{`{\n  "status": "success",\n  "latencyMs": 14.8,\n  "confidenceScore": 0.962,\n  "artifacts": "8_chunks_recalled"\n}`}</CodeBox></div></div></div>;
}
function CodeBox({ title, children, accent }) { return <div style={sx('background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:18px')}><b style={{ fontSize: 13.5, color: accent ? 'var(--accent)' : 'var(--ink-strong)', display: 'block', marginBottom: 8 }}>{title}</b><pre className="mono" style={sx('font-size:12px;color:var(--ink);line-height:1.5')}>{children}</pre></div>; }

const settingTabs = [{ id: 'general', label: '通用偏好' }, { id: 'models', label: 'AI 模型' }, { id: 'storage', label: '存储管理' }, { id: 'version', label: '版本信息' }];
export function SettingsView() {
  const app = useApp();
  const location = useLocation();
  const routeTab = location.pathname.split('/').pop();
  const [activeTab, setActiveTab] = useState(settingTabs.some(x => x.id === routeTab) ? routeTab : 'general');
  return <div><div className="page-head"><div><h1>系统设置</h1><p>个性化偏好、本地 AI 模型接口与底层持久化存储管理</p></div><div className="page-actions"><button className="btn primary" style={primaryButton} onClick={() => app.showToast('设置已安全持久化！', 'ok')}>保存设置</button></div></div><div style={sx('min-height:calc(100vh - 175px);display:grid;grid-template-columns:220px 1fr;gap:20px')}><div className="card" style={sx('margin:0;display:flex;flex-direction:column;height:100%;padding:10px')}>{settingTabs.map(tab => <div key={tab.id} className={`dataset-list-item${activeTab === tab.id ? ' active' : ''}`} style={{ padding: '12px 16px', borderRadius: 6, cursor: 'pointer', fontSize: 13.5, fontWeight: 600, background: activeTab === tab.id ? 'var(--accent-soft)' : 'transparent', color: activeTab === tab.id ? 'var(--accent)' : 'var(--ink)' }} onClick={() => setActiveTab(tab.id)}>{tab.label}</div>)}</div><div className="card" style={sx('margin:0;display:flex;flex-direction:column;height:100%;padding:24px 30px')}><SettingsPane tab={activeTab} app={app} /></div></div></div>;
}

function SettingsPane({ tab, app }) {
  const heading = style => <h2 style={sx('font-size:16px;font-weight:700;color:var(--ink-strong);margin-bottom:18px;border-bottom:1px solid var(--line);padding-bottom:10px')}>{style}</h2>;
  if (tab === 'general') return <div>{heading('界面与主题偏好')}<Field label="色彩主题模式"><div style={{ display: 'flex', gap: 16 }}>{['浅色','深色','跟随系统'].map(value => <label key={value}><input type="radio" name="theme" checked={app.theme === value} onChange={() => app.setTheme(value)} /> {value}{value === '浅色' ? ' (Silver)' : value === '深色' ? ' (Nebula)' : ''}</label>)}</div></Field></div>;
  if (tab === 'models') return <div>{heading('大模型接口与密钥')}<Field label="模型服务提供商"><input className="input" defaultValue="OpenAI Compatible (本地 Ollama / vLLM)" style={fieldStyle} /></Field><Field label="Base URL"><input className="input" defaultValue="http://127.0.0.1:11434/v1" style={fieldStyle} /></Field></div>;
  if (tab === 'storage') return <div>{heading('本地数据与缓存路径')}<Field label="SQLite 数据库文件"><code className="mono" style={sx('background:var(--inset);padding:8px 12px;border-radius:6px;display:block')}>D:/AIApp/Ordo/data/ordo.db (38.4 MB)</code></Field><button className="btn" style={smallButton} onClick={() => app.showToast('已清理临时解析缓存')}>🧹 清理临时缓存</button></div>;
  return <div>{heading('产品与运行环境')}<div style={sx('font-size:13.5px;line-height:1.8')}><div>产品名称: <b>Ordo 本地知识引擎工作台</b></div><div>系统版本: <span className="mono" style={{ color: 'var(--accent)', fontWeight: 700 }}>v1.8.0-enterprise</span></div><div>前端架构: <span className="badge ok">React 18 + Vite 5 + React Router 6</span></div><div>后端服务: <span className="badge ok">Node.js 24 + Fastify + SQLite</span></div></div></div>;
}
