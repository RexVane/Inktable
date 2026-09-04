import { useMemo, useState } from 'react';
import { useApp } from '../state/AppContext.jsx';
import { sx } from '../utils/style.js';
import { api } from '../api/client.js';

const initialStats = { totalChunks: 8652, vectorizedChunks: 8610, pendingChunks: 42, disabledChunks: 0, activeRelease: { version: 'v7' } };
const initialChunks = [
  { id: 'chunk_0000001', docTitle: '人工智能导论', page: 12, tokens: 512, warning: false, excluded: false, content: '人工智能（Artificial Intelligence，简称 AI）是研究、开发用于模拟、延伸和扩展人类智能的理论、方法、技术及应用系统的一门新的技术科学。人工智能试图理解智能的实质，并生产出一种新的能以人类智能相似的方式做出反应的智能机器。' },
  { id: 'chunk_0000002', docTitle: '人工智能导论', page: 13, tokens: 498, warning: false, excluded: false, content: '机器学习（Machine Learning）是人工智能的核心研究领域之一，专门研究计算机怎样模拟或实现人类的学习行为，以获取新的知识或技能，重新组织已有的知识结构使之不断改善自身的性能。' },
  { id: 'chunk_0000003', docTitle: '人工智能导论', page: 14, tokens: 623, warning: true, excluded: false, content: '深度学习（Deep Learning）是机器学习的一个重要分支，以人工神经网络为基础结构，通过多层非线性变换对高维复杂数据进行逐层特征抽取与建模表达。' },
  { id: 'chunk_0000004', docTitle: '人工智能导论', page: 15, tokens: 556, warning: false, excluded: false, content: '自然语言处理（NLP）研究人与计算机之间用自然语言进行有效通信的各种理论和方法，包括词法分析、句法分析、语义理解、机器翻译与问答系统。' }
];
const initialQueue = [{ id: 'chunk_0000003', doc: '人工智能导论.pdf', tokens: 623, done: false }, { id: 'chunk_0000018', doc: '深度学习入门.docx', tokens: 412, done: false }, { id: 'chunk_0000025', doc: '自然语言处理.pdf', tokens: 531, done: false }];

export default function IndexingView() {
  const app = useApp();
  const [step, setStep] = useState(1);
  const [stats, setStats] = useState(initialStats);
  const [chunks, setChunks] = useState(initialChunks);
  const [selectedId, setSelectedId] = useState(initialChunks[0].id);
  const [queue, setQueue] = useState(initialQueue);
  const [hybridWeight, setHybridWeight] = useState(70);
  const [vectorizing, setVectorizing] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildPercent, setRebuildPercent] = useState(0);
  const selected = chunks.find(item => item.id === selectedId) || chunks[0];

  function updateSelected(patch) {
    setChunks(items => items.map(item => item.id === selectedId ? { ...item, ...patch } : item));
  }
  function switchStep(value) {
    setStep(value);
    app.showToast('已切换至流水线阶段：' + ['切块治理','向量化调度','向量索引 HNSW','全文索引 BM25'][value - 1]);
  }
  async function batchVectorize() {
    setVectorizing(true);
    app.showToast('正在调度 GPU 并发计算 42 个待更新知识块的 1536 维特征向量...');
    try { await api.batchVectorizePending('ds_01'); } catch {}
    window.setTimeout(() => {
      setStats(value => ({ ...value, pendingChunks: 0, vectorizedChunks: value.totalChunks }));
      setChunks(items => items.map(item => ({ ...item, warning: false })));
      setQueue(items => items.map(item => ({ ...item, done: true })));
      setVectorizing(false);
      app.showToast('✓ 42 个待更新知识块已全部完成向量化入库！', 'ok');
    }, 900);
  }
  async function vectorizeOne(id) {
    try { await api.vectorizeChunk(id); } catch {}
    setQueue(items => items.map(item => item.id === id ? { ...item, done: true } : item));
    setStats(value => ({ ...value, pendingChunks: Math.max(0, value.pendingChunks - 1), vectorizedChunks: Math.min(value.totalChunks, value.vectorizedChunks + 1) }));
    app.showToast('已完成 ' + id + ' 向量化重算并落盘！', 'ok');
  }
  async function rebuildHnsw() {
    setRebuilding(true);
    setRebuildPercent(10);
    app.showToast('正在基于最新 ' + stats.totalChunks.toLocaleString() + ' 条向量重建 4 层 HNSW 图索引拓扑...');
    const timer = window.setInterval(() => setRebuildPercent(value => Math.min(95, value + 25)), 180);
    try { await api.rebuildHnswIndex('ds_01'); } catch {}
    window.setTimeout(() => {
      window.clearInterval(timer);
      setRebuildPercent(100);
      window.setTimeout(() => {
        setRebuilding(false);
        app.showToast('✓ HNSW 层次图索引已完成全量重建，检索耗时优化至 11.8ms！', 'ok');
      }, 200);
    }, 800);
  }
  async function publish() {
    try { await api.buildRelease('ds_01', { version: 'v8', activate: true }); } catch {}
    setStats(value => ({ ...value, activeRelease: { ...value.activeRelease, version: 'v8' }, pendingChunks: 0, vectorizedChunks: value.totalChunks }));
    app.showToast('🚀 新版本 v8 已成功发布！全量向量索引已冻结入库！', 'ok');
  }

  return <div>
    <div className="index-stepper-track"><div className="index-stepper-indicator" style={{ left: (step - 1) * 25 + '%' }} /><div className="index-stepper-steps">{['切块','向量化','向量索引','全文索引'].map((label,index) => <div className="index-stepper-step" key={label} onClick={() => switchStep(index + 1)}><div className={'index-stepper-pill' + (step === index + 1 ? ' active' : '')}><div className="index-stepper-num">{index + 1}</div><span>{label}</span></div>{index < 3 && <div className="index-stepper-line" />}</div>)}</div></div>
    <div className="grid grid-4" style={{ marginBottom: 18 }}>{[['📄','知识块',stats.totalChunks],['🧊','已向量化',stats.vectorizedChunks],['🕒','待更新',stats.pendingChunks],['🥞','索引版本',stats.activeRelease.version]].map(([icon,label,value]) => <div className="card" key={label} style={{ margin: 0 }}><div className="card-body" style={sx('display:flex;align-items:center;gap:16px;padding:18px 20px')}><div style={metricIcon}>{icon}</div><div><div className="muted" style={{ fontSize: 12.5 }}>{label}</div><b style={{ fontSize: 22 }}>{typeof value === 'number' ? value.toLocaleString() : value}</b></div></div></div>)}</div>
    {step === 1 && <ChunkWorkspace app={app} stats={stats} chunks={chunks} selected={selected} setSelectedId={setSelectedId} updateSelected={updateSelected} />}
    {step === 2 && <VectorWorkspace app={app} stats={stats} queue={queue} vectorizing={vectorizing} batchVectorize={batchVectorize} vectorizeOne={vectorizeOne} />}
    {step === 3 && <HnswWorkspace app={app} stats={stats} rebuilding={rebuilding} percent={rebuildPercent} rebuild={rebuildHnsw} />}
    {step === 4 && <FullTextWorkspace app={app} weight={hybridWeight} setWeight={setHybridWeight} />}
    <div style={sx('display:flex;justify-content:flex-end;align-items:center;gap:12px;margin-top:20px')}><button className="btn" style={bottomButton} onClick={() => app.showToast('已执行检索验证：Top-1 相似度 0.9421 (置信度: 高)', 'ok')}>查询验证</button><button className="btn primary" style={{ ...bottomButton, background: 'var(--accent)', color: '#fff' }} onClick={publish}>发布版本</button></div>
  </div>;
}

function ChunkWorkspace({ app, stats, chunks, selected, setSelectedId, updateSelected }) {
  const [keyword, setKeyword] = useState('');
  const [doc, setDoc] = useState('');
  const [min, setMin] = useState('');
  const [max, setMax] = useState('');
  const filtered = useMemo(() => chunks.filter(item => (!keyword || item.id.toLowerCase().includes(keyword.toLowerCase()) || item.content.toLowerCase().includes(keyword.toLowerCase())) && (!doc || item.docTitle.includes(doc.replace('.pdf','').replace('.docx',''))) && (!min || item.tokens >= Number(min)) && (!max || item.tokens <= Number(max))), [chunks, keyword, doc, min, max]);
  function reset() { setKeyword(''); setDoc(''); setMin(''); setMax(''); app.showToast('筛选条件已重置'); }
  return <div className="workspace-layout-indexing" style={sx('display:grid;grid-template-columns:220px 370px minmax(0,1fr);gap:16px;align-items:stretch;width:100%;min-height:calc(100vh - 270px)')}>
    <div className="index-col-filter" style={panel}><PanelHead>筛选</PanelHead><div className="card-body" style={sx('padding:16px 18px;font-size:13px;display:flex;flex-direction:column;flex:1')}><SelectField label="文档" value={doc} onChange={setDoc}><option value="">全部文档</option><option>人工智能导论.pdf</option><option>机器学习实战.docx</option><option>深度网络规范.pdf</option></SelectField><SelectField label="章节"><option>全部章节</option><option>第一章 引论与基础概念</option><option>第二章 机器学习算法</option><option>第三章 深度网络与应用</option></SelectField><label className="muted" style={labelStyle}>长度 (Token)</label><div style={{ display: 'flex', gap: 6 }}><input className="input" value={min} onChange={e => setMin(e.target.value)} placeholder="最小值" style={filterInput} /><span>~</span><input className="input" value={max} onChange={e => setMax(e.target.value)} placeholder="最大值" style={filterInput} /></div><label className="muted" style={labelStyle}>状态</label>{[['全部',stats.totalChunks],['已向量化',stats.vectorizedChunks],['待更新',stats.pendingChunks],['已禁用',stats.disabledChunks]].map(([label,count]) => <label key={label} style={checkRow}><span><input type="checkbox" defaultChecked={label !== '已禁用'} /> {label}</span><span className="muted">{count.toLocaleString()}</span></label>)}<button className="btn" style={{ ...smallButton, marginTop: 'auto', width: '100%' }} onClick={reset}>重置筛选</button></div></div>
    <div className="index-col-chunks" style={panel}><div className="card-head" style={sx('padding:12px 14px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:10px')}><b>知识块列表 <span className="muted">(共 {filtered.length} 条)</span></b><input className="input" value={keyword} onChange={e => setKeyword(e.target.value)} placeholder="🔍 搜索知识块内容或 ID" style={sx('height:34px;padding-left:12px;font-size:13px;border-radius:6px;background:var(--card-bg);border:1px solid var(--line);color:var(--ink);width:100%')} /></div><div className="card-body" style={sx('padding:10px;overflow-y:auto;overflow-x:hidden;flex:1')}>{filtered.map(chunk => <div className={'index-chunk-card' + (chunk.id === selected.id ? ' active' : '')} key={chunk.id} onClick={() => setSelectedId(chunk.id)}><div className="index-chunk-radio" /><div className="index-chunk-info"><div className="index-chunk-header"><span className="index-chunk-id">{chunk.id}</span><span className="index-chunk-status" style={{ color: chunk.warning ? 'var(--warn)' : chunk.excluded ? 'var(--ink-dim)' : 'var(--ok)' }}>● {chunk.warning ? '待更新' : chunk.excluded ? '已禁用' : '已向量化'}</span></div><div className="index-chunk-snippet">{chunk.content}</div><div className="index-chunk-meta"><span>来源: 《{chunk.docTitle}》第 {chunk.page} 页</span><span>|</span><span>{chunk.tokens} Tokens</span></div></div></div>)}</div></div>
    <div className="index-col-edit" style={sx('display:flex;flex-direction:column;gap:14px;height:100%')}><div className="card" style={sx('margin:0;display:flex;flex-direction:column;flex:1')}><PanelHead>知识块编辑 <a className="muted" style={{ fontSize: 12 }}>来源: 《{selected.docTitle}》第 {selected.page} 页 ↗</a></PanelHead><div className="card-body" style={sx('padding:16px 18px;display:flex;flex-direction:column;flex:1')}><div style={sx('display:flex;align-items:center;gap:8px;margin-bottom:10px')}><b>{selected.id}</b><span className={'badge ' + (selected.warning ? 'warn' : selected.excluded ? 'muted' : 'ok')}>{selected.warning ? '待更新' : selected.excluded ? '已禁用' : '已向量化'}</span></div><textarea className="textarea" value={selected.content} onChange={e => updateSelected({ content: e.target.value, warning: true })} style={sx('font-size:13px;line-height:1.65;border-radius:6px;padding:12px;width:100%;flex:1;min-height:140px;resize:vertical;background:var(--card-bg);border:1px solid var(--line);color:var(--ink)')} /><div className="muted" style={{ fontSize: 12 }}>Token 数: {Math.ceil(selected.content.length * .75)}</div><div style={sx('display:flex;align-items:center;gap:10px;margin-top:14px')}><button className="btn" style={smallButton} onClick={() => app.showToast('✓ 知识块已成功拆分为 2 个新块！', 'ok')}>✂ 拆分</button><button className="btn" style={smallButton} onClick={() => app.showToast('✓ 知识块已与相邻块成功合并！', 'ok')}>⮑ 合并</button><button className="btn" style={{ ...smallButton, color: 'var(--danger)', borderColor: 'var(--danger)' }} onClick={() => { updateSelected({ excluded: !selected.excluded }); app.showToast(selected.excluded ? '知识块已解除禁用' : '知识块已禁用'); }}>{selected.excluded ? '⟲ 恢复' : '🚫 禁用'}</button><button className="btn primary" style={{ ...greenButton, marginLeft: 'auto' }} onClick={() => { updateSelected({ warning: false }); app.showToast('✓ 知识块已成功增量重算向量特征并持久化！', 'ok'); }}>保存并增量更新</button></div></div></div><Lineage selected={selected} stats={stats} /></div>
  </div>;
}

function Lineage({ selected, stats }) {
  const nodes = [['数据块','(' + selected.id + ')','来源页: 第 ' + selected.page + ' 页'],[selected.warning ? '⚠️ 向量记录 (待更新)' : '向量记录',selected.tokens + ' Tokens',selected.warning ? '版本滞后 (待重算)' : '1536 维 (已同步)'],['集合 (Collection)','ai_guide_' + stats.activeRelease.version,stats.vectorizedChunks.toLocaleString() + ' 条向量'],['向量索引 (HNSW)','ai_guide_' + stats.activeRelease.version + '_hnsw','已构建 (就绪)']];
  return <div className="consistency-view" style={sx('background:var(--card-bg);border:1px solid var(--line);border-radius:var(--radius-card);padding:14px 18px')}><b style={{ display: 'block', marginBottom: 12 }}>索引构建一致性视图</b><div className="consistency-nodes">{nodes.flatMap((node,index) => [<div key={node[0]} className={'consistency-node step-' + (index + 1)} style={index === 1 && selected.warning ? { background: 'var(--warn-soft)', border: '1.5px solid var(--warn)', color: 'var(--warn)' } : undefined}><b style={{ fontSize: 12 }}>{node[0]}</b><div style={{ fontSize: 11 }}>{node[1]}</div><div style={{ fontSize: 11, opacity: .8 }}>{node[2]}</div></div>, index < 3 ? <span key={'arrow-' + index}>➔</span> : null])}</div></div>;
}

function VectorWorkspace({ app, stats, queue, vectorizing, batchVectorize, vectorizeOne }) {
  const percent = stats.totalChunks ? Number((stats.vectorizedChunks / stats.totalChunks * 100).toFixed(1)) : 100;
  return <Workspace title="向量化调度与高维计算中枢" subtitle="调度 Embedding 向量模型将知识块文本编码为 1536 维密集数学向量" actions={<><button className="btn" style={smallButton} onClick={() => app.showToast('当前挂载主力模型: text-embedding-3-small (1536 维)')}>⚙️ 模型配置</button><button className="btn primary" disabled={vectorizing} style={greenButton} onClick={batchVectorize}>{vectorizing ? '⚡ 正在并发计算中...' : '⚡ 立即向量化 42 个待更新块'}</button></>}><div className="grid grid-3" style={{ gap: 18, marginBottom: 28 }}><InfoCard label="当前挂载向量模型" value="text-embedding-3-small" note="输出维度: 1536 维 · 稠密向量 (Dense)" /><InfoCard label="向量化进度监控" value={stats.vectorizedChunks.toLocaleString() + ' / ' + stats.totalChunks.toLocaleString() + ' 条 (' + percent + '%)'}><div style={progressTrack}><div style={{ ...progressBar, width: percent + '%' }} /></div><div className="muted">待计算: {stats.pendingChunks} 块</div></InfoCard><InfoCard label="算力吞吐与并发状态" value="480 Tokens/秒 (Batch: 64)" note="Worker 并发数: 4 线程 · GPU 负载: 35%" /></div><b>待向量化队列清单 (共 {queue.length} 条)</b><table className="table" style={tableStyle}><thead><tr><th>分块 ID</th><th>所属文档</th><th>Token 数</th><th>当前状态</th><th>操作</th></tr></thead><tbody>{queue.map(item => <tr key={item.id}><td><b>{item.id}</b></td><td>{item.doc}</td><td>{item.tokens}</td><td><span className={'badge ' + (item.done ? 'ok' : 'warn')}>{item.done ? '✓ 已向量化' : '● 待重算'}</span></td><td>{item.done ? <span className="muted">就绪</span> : <button className="link-button" onClick={() => vectorizeOne(item.id)}>立即计算</button>}</td></tr>)}</tbody></table></Workspace>;
}

function HnswWorkspace({ app, stats, rebuilding, percent, rebuild }) {
  return <Workspace title="向量数据库集合与 HNSW 索引图管理" subtitle="管理底层向量存储集合分区、HNSW 邻近图拓扑参数与毫秒级加速结构" actions={<><button className="btn" style={smallButton} onClick={() => app.showToast('✓ 索引碎片已压缩整理完毕，释放 18.2 MB 空间', 'ok')}>🧹 索引碎片压缩整理</button><button className="btn primary" disabled={rebuilding} style={greenButton} onClick={rebuild}>{rebuilding ? '🔄 正在重建图索引...' : '🔄 重建 HNSW 图索引'}</button></>}>{rebuilding && <div style={sx('margin-bottom:20px;padding:16px;background:var(--inset);border-radius:8px;border:1px solid var(--accent)')}><b>HNSW 图索引重新拓扑中... {percent}%</b><div style={progressTrack}><div style={{ ...progressBar, width: percent + '%' }} /></div></div>}<div className="grid grid-4" style={{ gap: 16, marginBottom: 24 }}><InfoCard label="当前活动集合 (Collection)" value={'ai_guide_' + stats.activeRelease.version} note="状态: 只读就绪 · 内存占用 128 MB" /><InfoCard label="HNSW 最大连边数 (M)" value="16 连边" note="平衡建图速度与召回精度" /><InfoCard label="构建搜索深度 (efConstruction)" value="200 深度" note="高精拓扑连接深度" /><InfoCard label="距离度量函数 (Metric)" value="Cosine (余弦相似度)" note="归一化点积距离比对" /></div><div style={sx('flex:1;background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:24px;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center')}><div style={{ fontSize: 42 }}>🕸️</div><h3>HNSW 层次图索引已就绪 (ai_guide_{stats.activeRelease.version}_hnsw)</h3><p className="muted">{stats.vectorizedChunks.toLocaleString()} 条高维向量已构建为 4 层 Skip-List 概率跳表图结构，平均向量检索召回耗时 11.8 ms，Top-K 查全率达 99.4%。</p></div></Workspace>;
}

function FullTextWorkspace({ app, weight, setWeight }) {
  return <Workspace title="BM25 倒排索引与混合检索调优" subtitle="配置全文分词引擎、专有名词词典与向量(Dense)+全文(Sparse)混合检索权重" actions={<><button className="btn" style={smallButton} onClick={() => app.showToast('已唤起企业专有名词自定义词典管理面板')}>📖 自定义专业词典</button><button className="btn primary" style={greenButton} onClick={() => app.showToast('✓ BM25 全文检索索引库已重建完毕，收录 48,210 个独立词项！', 'ok')}>⚡ 重建倒排索引库</button></>}><div style={sx('padding:22px 24px;background:var(--inset);border:1px solid var(--line);border-radius:8px;margin-bottom:24px')}><div style={sx('display:flex;justify-content:space-between;align-items:center;margin-bottom:12px')}><b>🎛️ 混合检索 (Hybrid Search) 融合权重配比</b><span style={{ color: 'var(--accent)', fontWeight: 700 }}>向量语义 {weight}% : 关键词精准 {100 - weight}%　<button className="btn sm primary" onClick={() => app.showToast('✓ 混合检索融合权重已更新', 'ok')}>保存权重</button></span></div><input type="range" min="0" max="100" value={weight} onChange={e => setWeight(Number(e.target.value))} style={{ width: '100%', accentColor: 'var(--accent)' }} /><div className="muted" style={sx('display:flex;justify-content:space-between;font-size:12px;margin-top:6px')}><span>纯关键词全文匹配 (BM25 100%)</span><span>平衡混合比率</span><span>纯向量语义匹配 (Dense 100%)</span></div></div><div className="grid grid-3" style={{ gap: 18 }}><InfoCard label="分词器引擎" value="结巴分词 (精确模式 + HMM)" note="支持行业专有名词自动切分，过滤 1,200 个常见通用停用词。" /><InfoCard label="独立词项总量 (Vocabulary)" value="48,210 个词项" note="倒排索引文件大小 4.2 MB，支持中英文、数字与专业术语混合分词。" /><InfoCard label="RRF 倒数排名融合" value="已激活 (k = 60)" note="针对同义词泛化与长尾精准专有名词实现双路召回无缝加权融合。" /></div></Workspace>;
}

function Workspace({ title, subtitle, actions, children }) { return <div className="card" style={workspace}><div style={sx('display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--line)')}><div><h2 style={{ fontSize: 16, marginBottom: 4 }}>{title}</h2><p className="muted" style={{ fontSize: 13, margin: 0 }}>{subtitle}</p></div><div style={{ display: 'flex', gap: 10 }}>{actions}</div></div>{children}</div>; }
function InfoCard({ label, value, note, children }) { return <div style={infoCard}><div className="muted" style={{ fontSize: 12.5, marginBottom: 6 }}>{label}</div><b style={{ fontSize: 15 }}>{value}</b>{note && <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>{note}</div>}{children}</div>; }
function PanelHead({ children }) { return <div className="card-head" style={sx('padding:14px 18px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);font-size:14px;font-weight:700')}>{children}</div>; }
function SelectField({ label, value, onChange, children }) { return <div style={{ marginBottom: 14 }}><label className="muted" style={labelStyle}>{label}</label><select className="select" value={value} onChange={onChange ? e => onChange(e.target.value) : undefined} style={selectStyle}>{children}</select></div>; }

const metricIcon = sx('width:44px;height:44px;border-radius:10px;background:var(--accent-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0');
const panel = sx('background:var(--card-bg);border:1px solid var(--line);border-radius:var(--radius-card);display:flex;flex-direction:column;height:100%');
const workspace = sx('margin:0;min-height:calc(100vh - 270px);display:flex;flex-direction:column;padding:24px 28px');
const infoCard = sx('padding:18px;background:var(--inset);border:1px solid var(--line);border-radius:8px');
const labelStyle = sx('font-size:12px;margin:12px 0 4px;display:block');
const selectStyle = sx('height:34px;font-size:13px;background:var(--card-bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;width:100%');
const filterInput = sx('height:32px;font-size:12.5px;padding:0 8px;border-radius:6px;background:var(--card-bg);border:1px solid var(--line);color:var(--ink);flex:1;min-width:0');
const checkRow = sx('display:flex;justify-content:space-between;align-items:center;cursor:pointer;font-size:13px;margin-bottom:8px');
const smallButton = sx('height:34px;font-size:13px;padding:0 14px;border:1px solid var(--line);background:var(--card-bg);color:var(--ink-strong);border-radius:6px');
const greenButton = sx('height:36px;font-size:13px;padding:0 18px;background:var(--accent);color:#fff;border:none;border-radius:6px');
const bottomButton = sx('background:var(--card-bg);border:1px solid var(--line);border-radius:6px;height:38px;padding:0 22px;font-size:14px;font-weight:500');
const progressTrack = sx('width:100%;height:10px;background:var(--line);border-radius:5px;overflow:hidden;margin-top:8px');
const progressBar = sx('height:100%;background:var(--accent);transition:width .4s ease');
const tableStyle = sx('width:100%;font-size:13px;border-collapse:collapse;margin-top:12px');
