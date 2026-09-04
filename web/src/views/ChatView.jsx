import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useApp } from '../state/AppContext.jsx';
import { sx } from '../utils/style.js';

const initialSessions = [
  { id: 's-1', group: '今天', title: '如何为企业网站安装产品问答助手？', time: '10:24' },
  { id: 's-2', group: '今天', title: '产品问答助手支持哪些网站平台？', time: '09:58' },
  { id: 's-3', group: '今天', title: '如何自定义问答助手的欢迎语？', time: '09:12' },
  { id: 's-4', group: '昨天', title: '如何查看问答数据统计报表？', time: '16:45' },
  { id: 's-5', group: '昨天', title: '知识库更新后多久生效？', time: '14:23' },
  { id: 's-6', group: '更早', title: '如何配置敏感词过滤？', time: '05-18' },
  { id: 's-7', group: '更早', title: '问答助手支持多语言吗？', time: '05-17' }
];

const initialMessages = [
  { role: 'user', content: '如何为企业网站安装产品问答助手？', time: '10:24' },
  {
    role: 'assistant',
    content: '要为企业网站安装产品问答助手，请按以下步骤操作：\n\n1. 获取安装代码：在「产品问答助手」应用中创建助手，复制生成的安装代码（包含 JS 脚本）。[1]\n2. 添加到网站：将代码粘贴到网站所有页面的 </body> 标签前，确保脚本正确加载执行。[1][2]\n3. 验证与发布：刷新网站页面，确认助手已正常显示并可对话；完成测试后发布上线。[3]\n\n如需自定义外观或欢迎语，可在应用配置中设置后重新获取代码并更新到网站。',
    time: '10:25',
    kb: '产品文档库 v7'
  }
];

const citations = [
  { id: 1, title: '产品问答助手使用指南.pdf', page: 'P.12-13', quote: '在「产品问答助手」中创建助手后，进入「发布」页面，可获取安装代码。该代码为一段 JS 脚本，请复制并粘贴到网站所有页面的 </body> 标签前...' },
  { id: 2, title: 'Web 集成开发指南.pdf', page: 'P.25-26', quote: '将安装代码粘贴到网站所有页面的 </body> 标签前，确保脚本在页面加载时被执行，以保证问答助手组件能够正常初始化和渲染。' },
  { id: 3, title: '常见问题与排查手册.pdf', page: 'P.8-9', quote: '安装完成后，请刷新网站页面确认助手已正常显示并可对话。如未显示，请检查代码是否正确放置或是否存在脚本冲突。' }
];

function Answer({ text }) {
  const parts = String(text).split(/(\[\d+\])/g);
  return <div style={{ whiteSpace: 'pre-wrap' }}>{parts.map((part, index) => /^\[\d+\]$/.test(part)
    ? <span key={index} style={sx('color:#16a34a;background:#dcfce7;padding:1px 5px;border-radius:4px;font-weight:700;margin:0 2px')}>{part}</span>
    : part)}</div>;
}

export default function ChatView() {
  const app = useApp();
  const navigate = useNavigate();
  const [sessions, setSessions] = useState(initialSessions);
  const [activeSessionId, setActiveSessionId] = useState('s-1');
  const [messages, setMessages] = useState(initialMessages);
  const [userInput, setUserInput] = useState('');

  function newChat() {
    const item = { id: 's-' + Date.now(), group: '今天', title: '新会话', time: '刚刚' };
    setSessions(items => [item, ...items]);
    setActiveSessionId(item.id);
    setMessages([{ role: 'assistant', content: '您好！我是产品问答助手，已连接本地知识库【产品文档库 (Release v7)】。请随时提问！', time: '刚刚', kb: '产品文档库 v7' }]);
  }

  function sendMessage(event) {
    event?.preventDefault();
    const question = userInput.trim();
    if (!question) return;
    const now = new Date();
    const time = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
    setMessages(items => [...items, { role: 'user', content: question, time }]);
    setUserInput('');
    window.setTimeout(() => setMessages(items => [...items, {
      role: 'assistant',
      content: '基于【产品文档库 (Release v7)】分析，针对问题「' + question + '」：\n\n1. 核心流程已在本地向量数据库中完成高维索引构建。[1]\n2. 支持在企业工作空间内直接调用或通过 JS 嵌入代码挂载到外部网站。[2]\n3. 您可随时点击右上角「🔀 查看问答流程」深入诊断本次回答的 8 阶段执行日志与耗时。[3]',
      time,
      kb: '产品文档库 v7'
    }]), 350);
  }

  return <div style={sx('display:flex;flex-direction:column;gap:14px;height:calc(100vh - 110px)')}>
    <div style={sx('display:flex;justify-content:space-between;align-items:center')}>
      <div style={sx('display:flex;align-items:center;gap:12px')}>
        <h2 style={sx('margin:0;font-size:18px;font-weight:700;color:var(--ink-strong)')}>智能问答</h2>
        <select className="input sm" style={sx('height:32px;font-weight:600;font-size:13px;padding:0 10px;border-radius:6px')} defaultValue="product"><option value="product">📚 产品文档库</option><option>技术资料库</option><option>制度与内控库</option></select>
        <span className="badge ok" style={sx('font-size:11px;padding:2px 8px;border-radius:12px')}>v7 (当前最新) ●</span>
      </div>
      <div style={{ display: 'flex', gap: 8 }}><button className="btn sm" style={toolButton} onClick={() => navigate('/knowledge/datasets')}>📖 查看知识库</button><button className="btn sm" style={{ ...toolButton, color: 'var(--accent)', fontWeight: 600 }} onClick={() => navigate('/qaflow/parse')}>🔀 查看问答流程</button></div>
    </div>
    <div style={sx('flex:1;min-height:0;display:grid;grid-template-columns:240px 1fr 280px;gap:16px')}>
      <div className="card" style={columnCard}>
        <div className="card-head" style={sx('padding:12px;border-bottom:1px solid var(--line)')}><button className="btn primary" style={sx('width:100%;height:36px;background:#16a34a;color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer')} onClick={newChat}>+　新对话</button></div>
        <div className="card-body" style={sx('padding:8px;flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:2px')}>
          {['今天','昨天','更早'].map(group => <div key={group}><div className="muted" style={sx('font-size:11px;font-weight:700;padding:8px 8px 4px')}>{group}</div>{sessions.filter(item => item.group === group).map(session => <div key={session.id} style={{ padding: '8px 10px', borderRadius: 6, cursor: 'pointer', display: 'flex', justifyContent: 'space-between', background: activeSessionId === session.id ? 'var(--accent-soft)' : 'transparent', color: activeSessionId === session.id ? 'var(--accent)' : 'var(--ink)' }} onClick={() => setActiveSessionId(session.id)}><span style={sx('font-size:12.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1')}>{session.title}</span><span className="muted" style={{ fontSize: 10.5 }}>{session.time}</span></div>)}</div>)}
        </div>
      </div>
      <div className="card" style={columnCard}>
        <div className="card-body" style={sx('padding:20px;flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:20px')}>
          {messages.map((message, index) => message.role === 'user'
            ? <div key={index} style={sx('display:flex;justify-content:flex-end;align-items:flex-start;gap:10px')}><div style={sx('background:#f0fdf4;border:1px solid #bbf7d0;color:#166534;padding:12px 16px;border-radius:12px 12px 2px 12px;font-size:13.5px;max-width:80%;line-height:1.5')}><div>{message.content}</div><div style={sx('font-size:10.5px;text-align:right;margin-top:4px;color:#15803d')}>{message.time}</div></div><div style={avatarStyle}>👤</div></div>
            : <div key={index} style={sx('display:flex;gap:12px;align-items:flex-start')}><div style={botStyle}>🤖</div><div style={{ flex: 1, maxWidth: '88%' }}><div style={sx('background:var(--card-bg);border:1px solid var(--line);padding:16px 20px;border-radius:12px;font-size:13px;line-height:1.7;color:var(--ink-strong);box-shadow:0 1px 3px rgba(0,0,0,.03)')}><Answer text={message.content} /><div style={sx('font-size:11.5px;color:var(--ink-dim);margin-top:12px;padding-top:8px;border-top:1px solid var(--line-soft)')}>{message.time} · 基于 {message.kb || '产品文档库 v7'}</div></div><div style={sx('display:flex;gap:8px;margin-top:8px')}>{['📋 复制','⟳ 重新生成','👍 有帮助','👎 没帮助'].map(label => <button key={label} className="btn sm" style={actionButton} onClick={() => app.showToast(label === '📋 复制' ? '已复制回答内容' : label === '⟳ 重新生成' ? '正在重新检索与生成回答...' : '感谢您的反馈！', 'ok')}>{label}</button>)}</div></div></div>)}
        </div>
        <form onSubmit={sendMessage} style={sx('border-top:1px solid var(--line);background:var(--card-bg);padding:12px 16px;border-radius:0 0 8px 8px')}>
          <div style={sx('display:flex;align-items:center;gap:10px')}><button type="button" className="btn sm" style={sx('border:none;background:transparent;font-size:18px;color:var(--ink-dim)')} onClick={() => app.showToast('附件上传已就绪')}>📎</button><textarea value={userInput} onChange={event => setUserInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) sendMessage(event); }} placeholder="向产品文档库提问..." style={sx('flex:1;height:38px;resize:none;border:1px solid var(--line);border-radius:6px;padding:8px 12px;font-size:13px;font-family:inherit;outline:none;background:var(--inset);color:var(--ink)')} /><button className="btn primary" type="submit" style={sx('width:42px;height:38px;background:#16a34a;color:#fff;border:none;border-radius:6px;font-size:16px')}>➤</button></div>
          <div style={sx('font-size:11px;color:var(--ink-dim);margin-top:6px')}>回答仅使用当前知识库　ⓘ</div>
        </form>
      </div>
      <div style={sx('display:flex;flex-direction:column;gap:14px;overflow-y:auto')}>
        <div className="card" style={sideCard}><b style={sideTitle}>引用来源</b><div style={sx('display:flex;flex-direction:column;gap:10px')}>{citations.map(citation => <div key={citation.id} style={sx('border:1px solid var(--line);border-radius:6px;padding:10px;background:var(--inset);font-size:12px')}><div style={sx('display:flex;justify-content:space-between;align-items:center;margin-bottom:6px')}><div style={sx('display:flex;align-items:center;gap:6px')}><span style={citationNumber}>{citation.id}</span><span style={sx('font-weight:600;color:var(--ink-strong);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px')}>📄 {citation.title}</span></div><span className="muted" style={{ fontSize: 11 }}>{citation.page}</span></div><div className="muted" style={sx('font-size:11.5px;line-height:1.45')}><span style={{ color: 'var(--accent)' }}>⌄</span> {citation.quote}</div></div>)}</div></div>
        <div className="card" style={sideCard}><b style={sideTitle}>相关 Wiki</b>{['产品问答助手简介','问答助手配置项说明','安装与集成常见问题'].map(item => <div key={item} style={sx('display:flex;justify-content:space-between;align-items:center;padding:8px 4px;border-bottom:1px solid var(--line-soft);font-size:12.5px;cursor:pointer')} onClick={() => app.showToast('正在调取 Wiki 文档: ' + item, 'ok')}><span>📄 {item}</span><span className="muted">›</span></div>)}</div>
      </div>
    </div>
  </div>;
}

const toolButton = sx('background:var(--card-bg);border:1px solid var(--line);border-radius:6px;padding:5px 12px;font-size:12.5px;cursor:pointer');
const columnCard = sx('margin:0;display:flex;flex-direction:column;height:100%');
const actionButton = sx('font-size:11.5px;padding:3px 10px;background:var(--card-bg);border:1px solid var(--line);border-radius:6px');
const avatarStyle = sx('width:34px;height:34px;border-radius:50%;background:#e2e8f0;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0');
const botStyle = sx('width:36px;height:36px;border-radius:10px;background:var(--accent-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0');
const sideCard = sx('margin:0;padding:14px;background:var(--card-bg);border:1px solid var(--line);border-radius:8px');
const sideTitle = sx('font-size:13.5px;color:var(--ink-strong);margin-bottom:12px;display:block');
const citationNumber = sx('width:18px;height:18px;border-radius:4px;background:#16a34a;color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700');
