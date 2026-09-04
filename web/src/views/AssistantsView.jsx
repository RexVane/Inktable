import { useState } from 'react';
import { useApp } from '../state/AppContext.jsx';
import { sx } from '../utils/style.js';

const initialAssistants = [
  { id: 'ast-1', name: '产品问答助手', status: 'published', statusText: '已发布', kb: '产品文档库', url: 'www.example.com', version: 'v1.2.3', desc: '面向网站访客的产品信息问答助手，提供产品功能、价格、使用场景等相关问题解答。', requestsToday: 46 },
  { id: 'ast-2', name: '技术支持助手', status: 'draft', statusText: '草稿', kb: '技术资料库', url: 'support.example.com', version: 'v0.9.1', desc: '内部研发与运维技术排查助手，精准定位异常日志与开发接口文档。', requestsToday: 28 },
  { id: 'ast-3', name: '销售资料助手', status: 'paused', statusText: '已停用', kb: '销售资料库', url: 'sales.example.com', version: 'v0.8.0', desc: '商业化解决方案与报价核算智能助手。', requestsToday: 12 }
];
const tabs = [['basic','基本设置'],['scope','知识范围'],['model','模型与提示词'],['web','网站接入'],['release','发布版本'],['usage','用量与会话']];
const initialQuestions = ['你们的产品主要有哪些功能？','产品如何收费？有没有试用？','支持哪些使用场景？'];
const visitors = [{ name: '访客_3821', unresolved: 2, time: '1 分钟前' }, { name: '访客_7308', unresolved: 0, time: '8 分钟前' }, { name: '访客_1954', unresolved: 1, time: '23 分钟前' }, { name: '访客_5129', unresolved: 0, time: '45 分钟前' }, { name: '访客_9072', unresolved: 1, time: '1 小时前' }];

export default function AssistantsView() {
  const app = useApp();
  const [assistants, setAssistants] = useState(initialAssistants);
  const [selectedId, setSelectedId] = useState('ast-1');
  const [activeTab, setActiveTab] = useState('basic');
  const [questions, setQuestions] = useState(initialQuestions);
  const selected = assistants.find(item => item.id === selectedId) || assistants[0];

  function updateSelected(patch) {
    setAssistants(items => items.map(item => item.id === selectedId ? { ...item, ...patch } : item));
  }
  function toggleStatus() {
    const published = selected.status === 'published';
    updateSelected({ status: published ? 'paused' : 'published', statusText: published ? '已停用' : '已发布' });
    app.showToast(published ? '助手服务已停用' : '助手服务已启用并上线', published ? 'info' : 'ok');
  }
  function addQuestion() {
    const value = window.prompt('请输入新增的建议问题：');
    if (value?.trim()) {
      setQuestions(items => [...items, value.trim()]);
      app.showToast('建议问题添加成功', 'ok');
    }
  }

  return <div style={sx('display:flex;flex-direction:column;gap:16px')}>
    <div style={sx('display:flex;justify-content:space-between;align-items:center')}><h2 style={sx('margin:0;font-size:18px;font-weight:700;color:var(--ink-strong)')}>智能助手</h2><button className="btn primary" style={greenButton} onClick={() => app.showToast('已唤起新建助手面板', 'ok')}>+ 新建助手</button></div>
    <div className="grid grid-4" style={{ gap: 14 }}>{[['🤖','助手总数','6'],['🚀','已发布','4'],['📈','今日请求','86'],['🛡️','成功率','96.2%']].map(metric => <div className="card" key={metric[1]} style={metricCard}><div style={metricIcon}>{metric[0]}</div><div><div className="muted" style={{ fontSize: 12 }}>{metric[1]}</div><b style={{ fontSize: 20, color: metric[1] === '成功率' ? '#16a34a' : 'var(--ink-strong)' }}>{metric[2]}</b></div></div>)}</div>
    <div style={sx('display:grid;grid-template-columns:320px 1fr;gap:16px')}>
      <div className="card" style={sx('margin:0;padding:12px;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;display:flex;flex-direction:column')}>
        <div style={sx('display:flex;gap:6px;margin-bottom:12px')}><input className="input sm" placeholder="🔍 搜索助手名称" style={{ flex: 1, height: 32, fontSize: 12 }} /><select className="input sm" style={{ width: 90, height: 32, fontSize: 12 }}><option>全部状态</option><option>已发布</option><option>草稿</option><option>已停用</option></select></div>
        <div style={sx('display:flex;flex-direction:column;gap:8px;flex:1')}>{assistants.map(item => <div key={item.id} style={{ padding: '10px 12px', borderRadius: 8, border: '1.5px solid ' + (selectedId === item.id ? '#16a34a' : 'var(--line)'), cursor: 'pointer', background: selectedId === item.id ? '#f0fdf4' : 'var(--card-bg)' }} onClick={() => setSelectedId(item.id)}><div style={sx('display:flex;align-items:center;gap:10px')}><div style={{ ...assistantIcon, background: item.status === 'published' ? '#dcfce7' : item.status === 'draft' ? '#dbeafe' : '#f1f5f9' }}>🤖</div><div style={{ flex: 1, minWidth: 0 }}><div style={sx('display:flex;justify-content:space-between;align-items:center')}><b style={sx('font-size:13px;color:var(--ink-strong);overflow:hidden;text-overflow:ellipsis;white-space:nowrap')}>{item.name}</b><span className={'badge ' + (item.status === 'published' ? 'ok' : item.status === 'draft' ? 'blue' : '')}>{item.statusText}</span></div><div className="muted" style={{ fontSize: 11 }}>绑定知识库: {item.kb}</div><div style={sx('display:flex;justify-content:space-between;margin-top:4px')}><span style={{ fontSize: 11 }}>{item.url}</span><span className="muted" style={{ fontSize: 11 }}>今日请求 <b>{item.requestsToday}</b></span></div></div></div></div>)}</div>
        <div className="muted" style={sx('display:flex;justify-content:space-between;padding:10px 4px 0;border-top:1px solid var(--line-soft);margin-top:10px;font-size:11.5px')}><span>共 3 项</span><span>‹　<b style={{ color: '#16a34a' }}>1</b>　›</span><span>10 条/页 ⌄</span></div>
      </div>
      <div className="card" style={sx('margin:0;padding:16px 20px;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;display:flex;flex-direction:column')}>
        <div style={sx('display:flex;justify-content:space-between;align-items:center;padding-bottom:14px;border-bottom:1px solid var(--line)')}>
          <div style={sx('display:flex;align-items:center;gap:12px')}><div style={{ ...assistantIcon, width: 42, height: 42, fontSize: 22, background: '#dcfce7' }}>🤖</div><div><div style={sx('display:flex;align-items:center;gap:8px')}><h3 style={{ margin: 0, fontSize: 16 }}>{selected.name}</h3><span className={'badge ' + (selected.status === 'published' ? 'ok' : 'blue')}>{selected.statusText}</span><select className="input sm" defaultValue={selected.version} style={{ height: 24, fontSize: 11.5 }}><option>{selected.version}</option><option>v1.2.2</option><option>v1.2.1</option></select></div><a href={'https://' + selected.url} target="_blank" rel="noreferrer" style={{ color: '#16a34a', fontSize: 12 }}>{selected.url} ↗</a></div></div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}><span style={{ fontSize: 11.5, color: '#16a34a' }}>● 网站接入健康 <span className="muted">(最后检测: 1 分钟前)</span></span><button className="btn sm" style={smallButton} onClick={() => app.showToast('已唤起实时预览')}>预览</button><button className="btn sm" style={smallButton} onClick={toggleStatus}>{selected.status === 'published' ? '停用' : '启用'}</button><button className="btn sm primary" style={greenButton} onClick={() => app.showToast('已成功发布新版本', 'ok')}>发布新版本</button></div>
        </div>
        <div style={sx('display:flex;gap:20px;border-bottom:1px solid var(--line);padding:10px 0 0')}>{tabs.map(([key,label]) => <button key={key} style={{ border: 'none', background: 'transparent', padding: '8px 4px', fontSize: 13, cursor: 'pointer', position: 'relative', color: activeTab === key ? '#16a34a' : 'var(--ink)', fontWeight: activeTab === key ? 700 : 400 }} onClick={() => setActiveTab(key)}>{label}{activeTab === key && <span style={sx('position:absolute;bottom:0;left:0;right:0;height:2px;background:#16a34a')} />}</button>)}</div>
        <AssistantTab tab={activeTab} selected={selected} updateSelected={updateSelected} questions={questions} setQuestions={setQuestions} addQuestion={addQuestion} app={app} />
      </div>
    </div>
  </div>;
}

function AssistantTab({ tab, selected, updateSelected, questions, setQuestions, addQuestion, app }) {
  if (tab === 'scope') return <TabBox><b>关联知识库与数据范围</b><p className="muted">此助手仅在指定知识库边界内进行多路召回与证据引用，禁止跨库越权检索。</p><div style={boundCard}><span style={{ fontSize: 20 }}>📚</span><div><b>{selected.kb}</b><div className="muted" style={{ fontSize: 11.5 }}>所属数据集: 产品文档全集 · 状态: 活跃已索引</div></div><span className="badge ok" style={{ marginLeft: 'auto' }}>✓ 已绑定</span></div></TabBox>;
  if (tab === 'model') return <TabBox><b>模型与系统提示词 (Prompt)</b><label style={labelStyle}>问答推理大模型</label><select className="input" style={inputStyle}><option>DeepSeek-V3 (本地/内网高并发推理)</option><option>Qwen-2.5-72B-Instruct (严格佐证与逻辑推理)</option><option>Qwen-2.5-14B-Chat (低时延极速响应)</option></select><label style={labelStyle}>系统提示词 (System Prompt)</label><textarea className="textarea" style={{ ...inputStyle, height: 100 }} defaultValue="你是一个严谨客观的企业知识库问答助手。请仅根据提供的参考文档切片内容回答用户问题。若参考资料中未提及相关事实，请直接明确告知无法根据现有资料回答，严禁模型自行编造或产生虚假事实幻觉。每条结论均须以 [n] 格式标注切片引用佐证来源。" /><button className="btn primary" style={{ ...greenButton, alignSelf: 'flex-start' }} onClick={() => app.showToast('模型与提示词已保存', 'ok')}>保存配置</button></TabBox>;
  if (tab === 'web') return <TabBox><b>企业网站挂载嵌入代码</b><p className="muted">复制下方脚本代码粘贴到您企业网站所有页面的 &lt;/body&gt; 标签前：</p><pre style={sx('background:#1e293b;color:#f8fafc;padding:14px;border-radius:6px;font-size:12px;overflow-x:auto')}>{'<script src="https://ordo.local/widget.js" data-assistant-id="' + selected.id + '" defer></script>'}</pre><button className="btn primary" style={{ ...greenButton, alignSelf: 'flex-start' }} onClick={() => app.showToast('✓ 嵌入代码已复制到剪贴板！', 'ok')}>📋 复制代码</button></TabBox>;
  if (tab === 'release') return <TabBox><b>发布版本历史</b><table style={tableStyle}><thead><tr><th>版本号</th><th>说明</th><th>时间</th><th>状态</th></tr></thead><tbody><tr><td className="mono"><b>v1.2.3</b></td><td>新增网站接入端点密钥轮换与转人工工单支持</td><td className="muted">2025-05-20 10:30</td><td><span className="badge ok">● 运行中</span></td></tr></tbody></table></TabBox>;
  if (tab === 'usage') return <TabBox><b>访客用量与工单转人工 (Handoffs)</b><div className="grid grid-3" style={{ gap: 12 }}>{[['本月问答请求','2,458 次'],['平均端到端耗时','1.42 秒'],['转人工请求率','3.8%']].map(item => <div key={item[0]} style={boundCard}><div><div className="muted">{item[0]}</div><b style={{ fontSize: 18 }}>{item[1]}</b></div></div>)}</div></TabBox>;
  return <div style={sx('display:grid;grid-template-columns:1fr 1.15fr;gap:24px;margin-top:16px')}>
    <div>
      <label style={labelStyle}>助手名称 <span className="muted">{selected.name.length}/50</span></label><input className="input" value={selected.name} onChange={event => updateSelected({ name: event.target.value })} style={inputStyle} />
      <label style={labelStyle}>助手描述 <span className="muted">{selected.desc.length}/200</span></label><textarea className="textarea" value={selected.desc} onChange={event => updateSelected({ desc: event.target.value })} style={{ ...inputStyle, height: 76 }} />
      <label style={labelStyle}>回答语气</label><select className="input" style={inputStyle}><option>专业、友好</option><option>严谨客观</option><option>亲和活泼</option></select>
      <label style={labelStyle}>欢迎语</label><textarea className="textarea" style={{ ...inputStyle, height: 60 }} defaultValue="您好！我是产品问答助手，有任何关于我们产品的问题，都可以随时问我。" />
      <label style={labelStyle}>建议问题</label><div style={sx('display:flex;flex-direction:column;gap:6px')}>{questions.map((question,index) => <div key={question} style={questionRow}><span className="muted">⋮⋮</span><span style={{ flex: 1 }}>{question}</span><button className="btn sm" onClick={() => setQuestions(items => items.filter((_,i) => i !== index))}>✕</button></div>)}<button className="btn sm" style={dashedButton} onClick={addQuestion}>+ 添加建议问题</button></div>
      <button className="btn primary" style={{ ...greenButton, marginTop: 16 }} onClick={() => app.showToast('基本设置保存成功', 'ok')}>保存基本设置</button>
    </div>
    <div style={sx('display:flex;flex-direction:column;gap:14px')}><TrendChart app={app} /><div className="card" style={innerCard}><div style={sx('display:flex;justify-content:space-between;margin-bottom:8px')}><b>最近访客会话</b><span className="muted">更多</span></div><table style={tableStyle}><thead><tr><th>访客</th><th>未解决问题</th><th>最后活跃时间</th></tr></thead><tbody>{visitors.map(item => <tr key={item.name} onClick={() => app.showToast('查看访客会话: ' + item.name)}><td><b>{item.name}</b></td><td style={{ color: item.unresolved ? '#ea580c' : 'var(--ink-dim)', textAlign: 'center' }}>{item.unresolved}</td><td className="muted" style={{ textAlign: 'right' }}>{item.time}</td></tr>)}</tbody></table></div></div>
  </div>;
}

function TabBox({ children }) { return <div style={sx('margin-top:16px;padding:18px;background:var(--inset);border:1px solid var(--line);border-radius:8px;display:flex;flex-direction:column;gap:12px')}>{children}</div>; }
function TrendChart({ app }) { return <div className="card" style={innerCard}><div style={sx('display:flex;justify-content:space-between;margin-bottom:10px')}><b>请求趋势 (近 7 天)</b><span className="muted" onClick={() => app.showToast('调取完整请求报表')}>更多</span></div><svg viewBox="0 0 360 120" style={{ width: '100%', height: 130 }}><line x1="20" y1="90" x2="340" y2="90" stroke="var(--line-soft)" /><polygon points="35,69.5 85,60.8 135,50.3 185,65.5 235,43.3 285,25.8 335,39.8 335,90 35,90" fill="rgba(22,163,74,.12)" /><polyline points="35,69.5 85,60.8 135,50.3 185,65.5 235,43.3 285,25.8 335,39.8" fill="none" stroke="#16a34a" strokeWidth="2" />{['05-14','05-15','05-16','05-17','05-18','05-19','05-20'].map((date,index) => <text key={date} x={35 + index * 50} y="105" fontSize="9" fill="var(--ink-dim)" textAnchor="middle">{date}</text>)}</svg></div>; }

const metricCard = sx('margin:0;padding:14px 18px;display:flex;align-items:center;gap:14px;background:var(--card-bg);border:1px solid var(--line);border-radius:8px');
const metricIcon = sx('width:40px;height:40px;border-radius:10px;background:#f0fdf4;color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:20px');
const assistantIcon = sx('width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px');
const greenButton = sx('background:#16a34a;color:#fff;height:36px;padding:0 16px;border-radius:6px;font-size:13px;font-weight:600;border:none;cursor:pointer');
const smallButton = sx('background:var(--card-bg);border:1px solid var(--line);border-radius:6px;padding:4px 10px');
const labelStyle = sx('font-size:12.5px;font-weight:600;color:var(--ink-strong);margin:12px 0 4px;display:flex;justify-content:space-between');
const inputStyle = sx('width:100%;height:34px;font-size:12.5px');
const boundCard = sx('display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--card-bg);border-radius:6px;border:1px solid var(--line)');
const questionRow = sx('display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--inset);border:1px solid var(--line);border-radius:6px;font-size:12px');
const dashedButton = sx('align-self:flex-start;background:transparent;border:1px dashed var(--line);color:#16a34a;font-size:12px;margin-top:4px');
const innerCard = sx('margin:0;padding:14px 16px;background:var(--card-bg);border:1px solid var(--line);border-radius:8px');
const tableStyle = sx('width:100%;font-size:11.5px;border-collapse:collapse');
