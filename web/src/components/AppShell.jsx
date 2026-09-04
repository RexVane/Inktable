import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useApp } from '../state/AppContext.jsx';

const titles = {
  '/home': '首页总览',
  '/knowledge/registry': '数据登记',
  '/knowledge/datasets': '数据集',
  '/knowledge/parsing': '数据解析',
  '/knowledge/index': '构建知识索引',
  '/knowledge/slices': '切片管理',
  '/knowledge/config': '知识库管理',
  '/apps/chat': '问答检索',
  '/apps/assistants': '智能助手',
  '/settings/general': '通用设置',
  '/settings/models': '模型配置',
  '/settings/storage': '存储配置',
  '/settings/version': '版本信息'
};

function Icon({ children, size = 16 }) {
  return <span aria-hidden="true" style={{ width: size, textAlign: 'center', display: 'inline-block' }}>{children}</span>;
}

function Navigation() {
  const app = useApp();
  const navigate = useNavigate();
  return (
    <aside className={`sidebar${app.sidebarCollapsed ? ' collapsed' : ''}`}>
      <div className="brand">
        <div className="logo"><Icon size={20}>▤</Icon></div>
        <div className="brand-text"><div className="title">Ordo</div><div className="subtitle">Local Knowledge Engine</div></div>
      </div>
      <div className="rail-tools"><button className="icon-btn" title="折叠/展开" onClick={app.toggleSidebar}><Icon>☰</Icon></button></div>
      <button className="new-chat-btn" onClick={() => navigate('/apps/chat')}><span style={{ fontSize: 16 }}>+</span><span className="label">新对话</span></button>
      <nav className="nav">
        <NavLink to="/home" className={({ isActive }) => `nav-parent${isActive ? ' on' : ''}`}><Icon>⌂</Icon><span className="label">首页</span></NavLink>
        <div className={`nav-group${app.openRail === 'knowledge' ? ' is-open' : ''}`}>
          <button className="nav-parent" type="button" onClick={() => app.toggleRail('knowledge')}><Icon>▤</Icon><span className="label">知识库</span><span className="nav-caret">▾</span></button>
          <div className="nav-children">
            <NavLink to="/knowledge/registry" className={({ isActive }) => `nav-child${isActive ? ' on' : ''}`}>数据登记</NavLink>
            <NavLink to="/knowledge/datasets" className={({ isActive }) => `nav-child${isActive ? ' on' : ''}`}>数据集</NavLink>
            <NavLink to="/knowledge/parsing" className={({ isActive }) => `nav-child${isActive ? ' on' : ''}`}>数据解析</NavLink>
            <NavLink to="/knowledge/index" className={({ isActive }) => `nav-child${isActive ? ' on' : ''}`}>构建知识索引</NavLink>
            <NavLink to="/knowledge/config" className={({ isActive }) => `nav-child${isActive ? ' on' : ''}`}>知识库管理</NavLink>
          </div>
        </div>
        <NavLink to="/qaflow" className={({ isActive }) => `nav-parent${isActive ? ' on' : ''}`}><Icon>◷</Icon><span className="label">问答流程诊断</span></NavLink>
        <div className={`nav-group${app.openRail === 'apps' ? ' is-open' : ''}`}>
          <button className="nav-parent" type="button" onClick={() => app.toggleRail('apps')}><Icon>◔</Icon><span className="label">AI 应用</span><span className="nav-caret">▾</span></button>
          <div className="nav-children">
            <NavLink to="/apps/chat" className={({ isActive }) => `nav-child${isActive ? ' on' : ''}`}>智能问答</NavLink>
            <NavLink to="/apps/assistants" className={({ isActive }) => `nav-child${isActive ? ' on' : ''}`}>智能助手</NavLink>
          </div>
        </div>
        <NavLink to="/settings/general" className={({ isActive }) => `nav-parent${isActive ? ' on' : ''}`}><Icon>⚙</Icon><span className="label">系统设置</span></NavLink>
      </nav>
      <div className="sidebar-footer"><div className="footer-row"><span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: 'var(--accent)' }} /><span className="label">Ordo 企业版</span><span className="version mono">v1.8.0</span></div></div>
    </aside>
  );
}

export default function AppShell() {
  const app = useApp();
  const location = useLocation();
  const title = location.pathname.startsWith('/qaflow') ? '问答流程诊断' : (titles[location.pathname] || '工作台');
  document.title = `${title} · Ordo 本地知识工作台`;
  return (
    <div className="app-shell">
      <Navigation />
      <section className="main-column">
        <header className="topbar">
          <button className="workspace-switcher" type="button"><Icon>▣</Icon><b>{app.activeWorkspace}</b><span>⌄</span></button>
          <div className="breadcrumbs"><span>/</span><span>知识库</span><span>/</span><b>{title}</b></div>
          <div className="topbar-spacer" />
          <div className="topbar-actions">
            <button className="bell-btn" type="button" title="通知" onClick={() => app.showToast('暂无新通知')}><Icon size={18}>♧</Icon><span className="unread-dot" /></button>
            <div className="user-avatar" title="当前用户"><span>Ordo</span></div>
          </div>
        </header>
        <div className="page-scroll-container"><Outlet /></div>
      </section>
    </div>
  );
}
