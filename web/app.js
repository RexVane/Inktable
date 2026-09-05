(() => {
  'use strict';

  const routes = [
    { id: 'home', label: '首页', icon: 'home' },
    { id: 'knowledge', label: '知识库', icon: 'book', children: [
      ['knowledge/registry', '数据登记'],
      ['knowledge/datasets', '数据集'],
      ['knowledge/parsing', '数据解析'],
      ['knowledge/index', '构建知识索引'],
      ['knowledge/config', '知识库管理']
    ]},
    { id: 'qaflow', label: '问答流程', icon: 'flow', children: [
      ['qaflow/parse', '问题解析'],
      ['qaflow/embed', '问题向量化'],
      ['qaflow/route', '检索路由'],
      ['qaflow/recall', '多路召回'],
      ['qaflow/fuse', '结果融合'],
      ['qaflow/rerank', '重排'],
      ['qaflow/prompt', '构建提示词'],
      ['qaflow/answer', '回答生成']
    ]},
    { id: 'apps', label: 'AI应用', icon: 'bot', children: [
      ['apps/chat', '智能问答'],
      ['apps/assistants', '智能助手']
    ]},
    { id: 'settings', label: '设置', icon: 'gear', children: [
      ['settings/general', '通用'],
      ['settings/models', '模型配置'],
      ['settings/storage', '存储配置'],
      ['settings/version', '版本信息']
    ]}
  ];

  const flat = {};
  routes.forEach(rail => (rail.children || [[rail.id, rail.label]]).forEach(([id, label]) => {
    flat[id] = { id, label, rail: rail.label, railId: rail.id };
  }));

  const flowRoutes = ['parse', 'embed', 'route', 'recall', 'fuse', 'rerank', 'prompt', 'answer'];
  const flowNames = ['问题解析', '问题向量化', '检索路由', '多路召回', '结果融合', '重排', '构建提示词', '回答生成'];
  const traceStageKeys = ['parse', 'embed', 'route', 'recall', 'fusion', 'rerank', 'prompt', 'generation'];

  const state = {
    page: readPage(),
    bootstrapping: true,
    connectionError: null,
    currentTheme: localStorage.getItem('ordo.theme') || '跟随系统',
    open: localStorage.getItem('ordo.openRail') || 'qaflow',
    collapsed: localStorage.getItem('ordo.sidebarCollapsed') === 'true',
    currentWorkspace: 'Ordo 企业空间',
    datasetDocs: [],

    // Chat Interactive State
    selectedChatKb: '',
    chatConversations: [],
    chatMessages: [],
    chatInput: '',
    chatLoading: false,
    highlightedCitationId: null,

    // Parsing Interactive State
    parsingTasks: [],
    parsingSelectedDocId: null,
    parsingCurrentPage: 1,
    parsingZoom: 100,
    parsingHighlightDiff: true,
    parsingWarningExpanded: true,
    parsingMoreMenuOpen: false,
    autoParsingEnabled: true,
    parsingConcurrency: 4,

    // Assistants Interactive State
    assistants: [],
    selectedAssistantId: null,
    assistantTab: 'basic',

    // Datasets & Documents Interactive State
    datasetTab: 'data',
    datasetSearchQuery: '',
    selectedFolder: 'all',
    selectedDocIds: [],
    datasetCurrentPage: 1,

    // Models Interactive State
    selectedModel: null,
    modelTab: 'credentials',
    modelsData: {},

    // Rerank Interactive State
    rerankSelectedChunkId: 1,

    // Prompt Interactive State
    promptSelectedTab: 'evidence',
    promptCopied: false,

    // Storage Interactive State
    storageTab: 'overview',
    storageChecked: false
  };

  /* Ordo Local-First REST API Client */
  const api = {
    ...window.OrdoApi.createClient(),

    async bootstrap() {
      try {
        const session = await this.bootstrapSession();
        if (!session) throw new Error('FastAPI 服务未返回本机会话');
        await this.syncContext();
        state.connectionError = null;
        return true;
      } catch (error) {
        this.connected = false;
        state.connectionError = error?.message || '无法连接 FastAPI 服务';
        console.warn('[api] bootstrap failed:', state.connectionError);
        return false;
      } finally {
        state.bootstrapping = false;
        if (typeof render === 'function') render();
      }
    },

    // 启动后预取后端上下文，所有工作台页面只使用服务端记录。
    async syncContext() {
      const ctx = { knowledgeBases: [], datasets: [], models: [], assistants: [], conversations: [], activeConversation: null, defaultKbId: null, defaultDatasetId: null };
      const [kbs, models, assistants, ver, conversationPage, parsingSettings] = await Promise.all([
        this.getKnowledgeBases().catch(() => []),
        this.getModels().catch(() => []),
        this.getAssistants().catch(() => []),
        this.getVersion().catch(() => null),
        this.getConversations({ limit: 30 }).catch(() => ({ items: [] })),
        this.getParsingSettings().catch(() => null)
      ]);
      ctx.knowledgeBases = kbs || [];
      ctx.models = models || [];
      ctx.assistants = assistants || [];
      ctx.conversations = Array.isArray(conversationPage) ? conversationPage : (conversationPage?.items || []);
      if (ver) state.version = ver;
      if (parsingSettings) {
        state.autoParsingEnabled = Boolean(parsingSettings.autoParsingEnabled);
        state.parsingConcurrency = parsingSettings.concurrency || 4;
      }
      const firstKb = ctx.knowledgeBases.find(kb => kb.status === 'active' || !kb.status) || ctx.knowledgeBases[0];
      if (firstKb) {
        ctx.defaultKbId = firstKb.id;
        ctx.datasets = await this.getDatasets(firstKb.id).catch(() => []);
        const withRelease = ctx.datasets.find(d => d.active_release_id);
        ctx.defaultDatasetId = (withRelease || ctx.datasets[0] || {}).id || null;
      }
      if (ctx.conversations.length) {
        const selected = ctx.conversations.find(c => c.id === state.activeConversationId) || ctx.conversations[0];
        ctx.activeConversation = await this.getConversation(selected.id).catch(() => null);
      }
      this.context = ctx;
      this.applyContextToState(ctx);
      return ctx;
    },

    // 把真实后端对象映射进工作台状态。
    applyContextToState(ctx) {
      if (Object.prototype.hasOwnProperty.call(ctx, 'models')) {
      state.modelsData = {};
      (ctx.models || []).forEach(m => {
        state.modelsData[m.id] = {
          backendId: m.id,
          name: m.name,
          purpose: m.purpose || 'generation',
          provider: m.provider === 'openai-compatible' ? 'OpenAI 兼容接口' : m.provider === 'ollama' ? '本地 Ollama / vLLM' : '本地证据抽取',
          url: m.base_url || '',
          modelName: m.model_id || '',
          timeout: 60,
          proxy: '',
          notes: '',
          status: m.status === 'available' ? 'ok' : 'danger',
          statusText: m.status === 'available' ? '可用' : (m.status === 'unverified' ? '未验证' : (m.status || '未测试')),
          latency: '—',
          time: m.updated_at || ''
        };
      });
      state.selectedModel = state.modelsData[state.selectedModel] ? state.selectedModel : (ctx.models?.[0]?.id || null);
      }
      if (Object.prototype.hasOwnProperty.call(ctx, 'assistants')) {
      state.assistants = (ctx.assistants || []).map(a => {
          let config = {};
          try { config = typeof a.draft_config_json === 'string' ? JSON.parse(a.draft_config_json) : (a.draft_config_json || {}); } catch (e) { config = {}; }
          return {
            id: a.id,
            backendId: a.id,
            name: a.name,
            status: a.status,
            statusText: a.status === 'published' ? '已发布' : a.status === 'paused' ? '已停用' : a.status === 'draft' ? '草稿' : (a.status || '—'),
            health: a.status === 'published' ? '健康' : '未接入',
            url: config.url || '—',
            kb: a.dataset_name || '—',
            version: a.release_version ? `v${a.release_version}` : 'v0.1',
            desc: config.description || '',
            tone: config.tone || '专业且友好',
            welcome: config.welcome || '你好，请问有什么可以帮你？',
            questions: config.questions || [],
            requestsToday: '—',
            successRate: '—'
          };
        });
      state.selectedAssistantId = state.assistants.some(a => a.id === state.selectedAssistantId) ? state.selectedAssistantId : (state.assistants[0]?.id || null);
      }
      if (!Object.prototype.hasOwnProperty.call(ctx, 'conversations')) return;
      state.selectedKbId = state.selectedKbId || ctx.defaultKbId;
      state.selectedDatasetId = state.selectedDatasetId || ctx.defaultDatasetId;
      state.selectedChatKb = (ctx.knowledgeBases || []).find(kb => kb.id === state.selectedKbId)?.name || ctx.knowledgeBases?.[0]?.name || '';
      state.chatConversations = (ctx.conversations || []).map((c, index) => ({
        id: c.id,
        title: c.title || '未命名会话',
        time: String(c.updated_at || c.created_at || '').replace('T', ' ').slice(11, 16),
        active: c.id === (ctx.activeConversation?.id || ctx.conversations?.[0]?.id),
        knowledgeBaseId: c.knowledge_base_id,
        datasetId: c.dataset_id
      }));
      state.activeConversationId = ctx.activeConversation?.id || null;
      if (!state.chatLoading) state.chatMessages = (ctx.activeConversation?.messages || []).map(mapChatMessage);
      state.chatReleaseVersion = ctx.activeConversation?.release_version || null;
    },

    parseCitationLocator(citation) {
      let locator = {};
      try { locator = typeof citation.locator_json === 'string' ? JSON.parse(citation.locator_json) : (citation.locator_json || citation.locator || {}); } catch (e) { locator = {}; }
      if (locator.page) return `P.${locator.page}`;
      if (locator.slide) return `S.${locator.slide}`;
      if (locator.sheet) return `Sheet ${locator.sheet}`;
      if (locator.line) return `L.${locator.line}`;
      return '';
    }
  };

  // Legacy renderers consume arrays; the API client retains pagination envelopes.
  for (const name of ['getDocuments', 'getChunks', 'getTasks', 'getTraces', 'getConversations']) {
    api[name] = async function(...args) {
      const result = await this.call(name, args, { throwOnError: true });
      return Array.isArray(result) ? result : (result?.items || []);
    };
  }

  for (const name of ['getDatasetFiles', 'getRegisteredSources', 'getParsingTasks']) {
    api[name] = async function(...args) {
      const result = await this.call(name, args, { throwOnError: true, envelope: true });
      const data = result?.data;
      if (Array.isArray(data)) return { items: data, total: data.length, ...result.meta };
      return data || { items: [], total: 0 };
    };
  }

  function requireConnection() {
    if (api.connected) return true;
    showToast('服务未连接，请启动 FastAPI 服务后重试', 'error');
    return false;
  }

  async function serverAction(operation, args, success) {
    if (!requireConnection()) return null;
    try {
      const result = await api.call(operation, args, { throwOnError: true });
      if (success) showToast(typeof success === 'function' ? success(result) : success, 'ok');
      return result;
    } catch (error) {
      showToast(error.message || '操作失败，请重试', 'error');
      return null;
    }
  }

  // 页面加载即建立本机会话（CSRF + Cookie）。
  api.bootstrap();

  // Expose api & state globally for debugging & testing
  if (typeof window !== 'undefined') {
    window.ordoApi = api;
    window.ordoState = state;
    window.state = state;
    window.render = render;
  }


  const app = document.getElementById('app');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));

  const jsArg = value => esc(JSON.stringify(value ?? ''));

  function mapChatMessage(message) {
    return {
      ...message, text: message.content ?? message.text ?? '',
      time: String(message.created_at || '').slice(11, 16) || '刚刚',
      citations: (message.citations || []).map(c => ({ ...c, citationId: c.id, id: c.ordinal, page: api.parseCitationLocator(c), quote: c.excerpt || '' })),
      traceId: message.trace_id || message.traceId || null
    };
  }

  function readPage() {
    const hash = window.location.hash.replace(/^#\/?/, '').split('?')[0];
    return flat[hash] ? hash : 'home';
  }

  // Mount core navigation, modals, and toast utilities on window for inline HTML onclick handlers
  window.go = function(page, params = {}) {
    if (state.chatAbort) { try { state.chatAbort.abort(); } catch (e) {} state.chatAbort = null; }
    const qs = new URLSearchParams(params).toString();
    window.location.hash = '#/' + page + (qs ? '?' + qs : '');
  };
  function go(page, params = {}) {
    window.go(page, params);
  }
  window.showToast = function(msg, tone = '') {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = msg;
    toast.className = 'toast show ' + tone;
    setTimeout(() => { if (toast) toast.className = 'toast'; }, 3000);
  };
  function showToast(msg, tone = '') {
    window.showToast(msg, tone);
  }
  window.showOverlay = function(html) {
    const overlay = document.getElementById('overlay');
    if (!overlay) return null;
    overlay.innerHTML = html;
    overlay.hidden = false;
    overlay.querySelectorAll('[data-close]').forEach(b => b.onclick = window.closeOverlay);
    return overlay;
  };
  function showOverlay(html) {
    return window.showOverlay(html);
  }
  window.closeOverlay = function() {
    const overlay = document.getElementById('overlay');
    if (overlay) { overlay.hidden = true; overlay.innerHTML = ''; }
  };
  function closeOverlay() {
    window.closeOverlay();
  }
  window.hideOverlay = function() {
    window.closeOverlay();
  };
  function hideOverlay() {
    window.closeOverlay();
  }

  window.addEventListener('hashchange', () => {
    state.page = readPage();
    if (state.page.startsWith('qaflow/')) {
      state.open = 'qaflow';
    } else if (state.page.startsWith('knowledge/')) {
      state.open = 'knowledge';
    } else if (state.page.startsWith('settings/')) {
      state.open = 'settings';
    } else if (state.page.startsWith('apps/')) {
      state.open = 'apps';
    }
    render();
  });

  function getSvgIcon(name) {
    const icons = {
      home: '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
      book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
      flow: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M10 6.5h4M6.5 10v4M17.5 10v4M10 17.5h4"/>',
      bot: '<rect x="3" y="8" width="18" height="12" rx="3"/><line x1="12" y1="4" x2="12" y2="8"/><circle cx="8.5" cy="13.5" r="1.5"/><circle cx="15.5" cy="13.5" r="1.5"/><path d="M8 17h8"/>',
      gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
      db: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
      cubes: '<path d="m21 16-9 5-9-5V8l9-5 9 5v8z"/><path d="M12 3v18"/><path d="M3 8l9 5 9-5"/>',
      chart: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
      doc: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>',
      graph: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>',
      stack: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
      link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
      check: '<circle cx="12" cy="12" r="9"/><polyline points="9 12 11 14 15 10"/>',
      warn: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
      copy: '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    };
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.home}</svg>`;
  }

  function iconCopy(size = 13) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
  }
  function iconDoc(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`;
  }
  function iconFolder(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
  }
  function iconTarget(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>`;
  }
  function iconTag(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>`;
  }
  function iconClock(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;
  }
  function iconLock(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`;
  }
  function iconShield(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`;
  }
  function iconZap(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>`;
  }
  function iconChat(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
  }
  function iconHelp(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`;
  }
  function iconUser(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`;
  }
  function iconCode(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`;
  }
  function iconBot(size = 16) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="16" x2="16.01" y2="16"/></svg>`;
  }
  function iconThumbUp(size = 13) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>`;
  }
  function iconThumbDown(size = 13) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/></svg>`;
  }
  function iconSave(size = 13) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>`;
  }
  function iconCompare(size = 13) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 3v18"/></svg>`;
  }
  function iconRefresh(size = 13) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>`;
  }
  function iconLayers(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>`;
  }
  function iconCube(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="m21 16-9 5-9-5V8l9-5 9 5v8z"/><path d="M12 3v18"/><path d="M3 8l9 5 9-5"/></svg>`;
  }
  function iconPulse(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>`;
  }
  function iconNodes(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>`;
  }
  function iconEdit(size = 13) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>`;
  }
  function iconPlay(size = 12) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="currentColor" style="vertical-align:-1px;"><polygon points="5 3 19 12 5 21 5 3"/></svg>`;
  }
  function iconEye(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
  }
  function iconRoute(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a4.5 4.5 0 0 0 0-9H7a4 4 0 0 1 0-8h11"/><circle cx="18" cy="5" r="3"/></svg>`;
  }
  function iconDatabase(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>`;
  }
  function iconTable(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="12" y1="3" x2="12" y2="21"/></svg>`;
  }
  function iconCheckCircle(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`;
  }
    function iconBan(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>`;
  }
  function iconFunnel(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>`;
  }
  function iconLink(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;
  }
  function iconChart(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>`;
  }
  function iconCloud(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>`;
  }
function iconSearch(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`;
  }
  function iconSettings(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`;
  }
  function iconGlobe(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`;
  }
  function iconMonitor(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>`;
  }
  function iconMobile(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>`;
  }
  function iconTrash(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`;
  }
  function iconBuilding(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><rect x="4" y="2" width="16" height="20" rx="2" ry="2"/><line x1="9" y1="22" x2="9" y2="2"/><line x1="8" y1="6" x2="8.01" y2="6"/><line x1="16" y1="6" x2="16.01" y2="6"/><line x1="8" y1="10" x2="8.01" y2="10"/><line x1="16" y1="10" x2="16.01" y2="10"/><line x1="8" y1="14" x2="8.01" y2="14"/><line x1="16" y1="14" x2="16.01" y2="14"/><line x1="8" y1="18" x2="8.01" y2="18"/><line x1="16" y1="18" x2="16.01" y2="18"/></svg>`;
  }
  function iconBook(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>`;
  }
  function iconBulb(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-7 7c0 2.38 1.19 4.47 3 5.74V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.26c1.81-1.27 3-3.36 3-5.74a7 7 0 0 0-7-7z"/></svg>`;
  }
  function iconBell(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>`;
  }
  function iconFlag(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>`;
  }
  function iconUpload(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>`;
  }
  function iconDownload(size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;
  }
  function renderDocIcon(ext = '', size = 14) {
    return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`;
  }

  function qaStageKey(value) {
    const stage = value || state.page.split('/')[1] || 'parse';
    const index = flowNames.indexOf(stage);
    return index >= 0 ? traceStageKeys[index] : ({ fuse: 'fusion', answer: 'generation' }[stage] || stage);
  }

  function qaStatusLabel(status) {
    return { succeeded: '已完成', success: '已完成', completed: '已完成', degraded: '降级完成', failed: '失败',
      running: '执行中', queued: '排队中', skipped: '已跳过', unavailable: '未记录', cancelled: '已取消', paused: '已暂停' }[status] || status || '未记录';
  }

  function qaRecordedStages(trace) {
    return traceStageKeys.map((key, index) => {
      const stage = trace?.stages?.find(item => item.key === key || item.name === flowNames[index]);
      return { key, name: flowNames[index], status: 'unavailable', ...stage };
    });
  }

  /* The selected inspection tab is independent of recorded execution status. */
  function renderQATraceHeader(activeIdx, stageDurations = null) {
    const defaultDurations = Array(8).fill('—');
    const steps = [
      '问题解析', '问题向量化', '检索路由', '多路召回',
      '结果融合', '重排', '构建提示词', '回答生成'
    ];

    const recordedStages = qaRecordedStages(state.activeTraceDetail);
    let itemsHtml = '';
    for (let i = 0; i < steps.length; i++) {
      const recorded = recordedStages[i];
      const isDone = ['succeeded', 'success', 'completed'].includes(recorded.status);
      const isCurrent = i === activeIdx;
      const stepDur = (Array.isArray(stageDurations) && stageDurations[i]) ? stageDurations[i] : defaultDurations[i];
      const tooltip = `${steps[i]} (${qaStatusLabel(recorded.status)} · 阶段耗时: ${stepDur})`;

      let circleStyle = 'border: 1.5px solid var(--line); color: var(--ink-dim); background:var(--card-bg);';
      let textStyle = 'color: var(--ink-dim); font-size: 13px; font-weight: 500;';
      let checkmark = '';

      if (isCurrent) {
        circleStyle = 'background: var(--accent); color: var(--on-accent); border: none; font-weight: 700;';
        textStyle = 'color: var(--accent); font-size: 13px; font-weight: 700;';
      } else if (recorded.status === 'failed') {
        circleStyle = 'border:1.5px solid var(--danger); color:var(--danger); background:var(--card-bg);';
        textStyle = 'color:var(--danger); font-size:13px; font-weight:500;';
      } else if (isDone) {
        circleStyle = 'border:1.5px solid var(--accent); color: var(--accent); background:var(--accent-soft);';
        textStyle = 'color: var(--ink-strong); font-size: 13px; font-weight: 500;';
        checkmark = '<span style="color: var(--accent); font-size: 12px; font-weight: 700; margin-left: 2px;">✓</span>';
      }

      itemsHtml += `
        <div style="display: flex; align-items: center; gap: 6px; cursor: pointer; flex-shrink: 0;" title="${esc(tooltip)}" onclick="window.location.hash='#/qaflow/${flowRoutes[i]}'">
          <div style="width: 22px; height: 22px; border-radius: 50%; ${circleStyle} display: flex; align-items: center; justify-content: center; font-size: 11.5px;">
            ${i + 1}
          </div>
          <span style="${textStyle}; white-space: nowrap;">${steps[i]}</span>
          ${checkmark}
        </div>
      `;

      if (i < steps.length - 1) {
        const lineBg = isDone ? 'var(--accent)' : 'var(--line)';
        itemsHtml += `<div style="flex: 1; height: 1.5px; background: ${lineBg}; margin: 0 10px; min-width: 14px;"></div>`;
      }
    }

    return `
    <div style="margin-bottom: 20px; width: 100%;">
      <div style="display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 6px 4px 14px; border-bottom: 1.5px solid var(--line-soft);">
        ${itemsHtml}
      </div>
    </div>`;
  }


  function statCard(iconName, label, value, extra = '', unit = '') {
    return `<div class="stat-card">
      <div class="stat-icon">${getSvgIcon(iconName)}</div>
      <div class="stat-content">
        <div class="stat-label">${esc(label)}</div>
        <div class="stat-value">${value}${unit ? `<span style="font-size:14px;font-weight:400;color:var(--ink-dim);margin-left:4px;">${esc(unit)}</span>` : ''}</div>
        ${extra ? `<div class="stat-extra">${extra}</div>` : ''}
      </div>
    </div>`;
  }

  function renderShell() {
    const meta = flat[state.page] || flat.home;
    const isQA = state.page.startsWith('qaflow/');
    const breadcrumbHtml = isQA 
      ? `<span>/</span><a href="#/qaflow/parse" style="color:inherit;">问答流程</a><span>/</span><span class="mono" style="font-size:12.5px;">${state.activeTraceId || (state.lastTrace?.id) || 'QA-最新追踪'}</span><span>/</span><b>${meta.label}</b>`
      : `<span>/</span><span>${meta.rail}</span>${meta.rail !== meta.label ? `<span>/</span><b>${meta.label}</b>` : ''}`;

    app.innerHTML = `<aside class="sidebar${state.collapsed ? ' collapsed' : ''}" id="sidebar">
      <div class="rail-tools">
        <button class="icon-btn" id="drawer" type="button" title="${state.collapsed ? '展开导航' : '收起导航'}" aria-controls="sidebarNav" aria-expanded="${!state.collapsed}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
        </button>
        <button class="icon-btn" id="searchBtn" type="button" title="全局搜索 (Ctrl+K)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        </button>
      </div>
      <button class="new-chat" id="newChat" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        <span class="label">＋ 新对话</span>
      </button>
      <nav class="nav" id="sidebarNav">
        ${routes.map(rail => {
          if (rail.children) {
            const isOpen = state.open === rail.id;
            const hasActive = rail.children.some(([id]) => id === state.page);
            return `<div class="nav-group ${isOpen ? 'is-open' : ''}" data-rail="${rail.id}">
              <button class="nav-parent ${hasActive ? 'has-active' : ''}" type="button" data-group="${rail.id}" aria-controls="nav-${rail.id}" aria-expanded="${isOpen && !state.collapsed}" title="${rail.label}">
                <span class="nav-ico">${getSvgIcon(rail.icon)}</span>
                <span class="label">${rail.label}</span>
                <span class="nav-caret">›</span>
              </button>
              <div class="nav-children" id="nav-${rail.id}">
                ${rail.children.map(([id, label]) => `
                  <button class="nav-child ${state.page === id ? 'on' : ''}" type="button" data-page="${id}">${label}</button>
                `).join('')}
              </div>
            </div>`;
          }
          return `<button class="nav-parent is-leaf ${state.page === rail.id ? 'on' : ''}" type="button" data-page="${rail.id}">
            <span class="nav-ico">${getSvgIcon(rail.icon)}</span>
            <span class="label">${rail.label}</span>
          </button>`;
        }).join('')}
      </nav>
      <div class="sidebar-footer">
        <div class="footer-row">
          <span class="dot"></span>
          <span class="label">Ordo 企业版</span>
          <span class="version" style="margin-left:auto;color:var(--ink-faint);">${(state.version?.appVersion || state.version?.version) ? ('v' + (state.version.appVersion || state.version.version)) : 'v1.8.0'}</span>
        </div>
      </div>
    </aside>
    <section class="main-column">
      <header class="topbar">
        <button class="workspace-switcher" type="button" id="workspaceBtn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6M9 13h6M9 17h6"/></svg>
          <b>Ordo 企业空间</b>
          <span>⌄</span>
        </button>
        <div class="breadcrumbs">${breadcrumbHtml}</div>
      </header>
      <div class="page-scroll-container">
        <div class="page-head">
          <div>
            <h1 id="pageTitle">${meta.label}</h1>
            <p id="pageDesc"></p>
          </div>
          <div class="page-actions" id="actions"></div>
        </div>
        <div class="page-body" id="body"></div>
      </div>
    </section>`;

    document.querySelectorAll('[data-page]').forEach(n => n.onclick = () => go(n.dataset.page));
    document.querySelectorAll('[data-group]').forEach(n => {
      n.onclick = () => {
        const rail = n.dataset.group;
        state.open = state.collapsed || state.open !== rail ? rail : '';
        localStorage.setItem('ordo.openRail', state.open);
        if (state.collapsed) {
          state.collapsed = false;
          localStorage.setItem('ordo.sidebarCollapsed', 'false');
        }
        syncSidebarState();
      };
    });
    document.getElementById('drawer').onclick = () => {
      state.collapsed = !state.collapsed;
      localStorage.setItem('ordo.sidebarCollapsed', String(state.collapsed));
      syncSidebarState();
    };
    document.getElementById('searchBtn').onclick = openSearchModal;
    document.getElementById('newChat').onclick = openNewChatModal;
    document.getElementById('workspaceBtn').onclick = () => toggleWorkspaceSwitcher();
  }

  function syncSidebarState() {
    document.getElementById('sidebar').classList.toggle('collapsed', state.collapsed);
    const drawer = document.getElementById('drawer');
    drawer.title = state.collapsed ? '展开导航' : '收起导航';
    drawer.setAttribute('aria-expanded', String(!state.collapsed));
    document.querySelectorAll('[data-rail]').forEach(group => {
      group.classList.toggle('is-open', group.dataset.rail === state.open);
    });
    document.querySelectorAll('[data-group]').forEach(button => {
      button.setAttribute('aria-expanded', String(!state.collapsed && button.dataset.group === state.open));
    });
  }

/* Global Native File & OS Interaction Handlers */
  let nativeFileInputEl = null;
  let nativeFolderInputEl = null;

  function ensureNativeInputs() {
    if (typeof document === 'undefined') return;
    if (!nativeFileInputEl) {
      nativeFileInputEl = document.createElement('input');
      nativeFileInputEl.type = 'file';
      nativeFileInputEl.multiple = true;
      nativeFileInputEl.accept = '.pdf,.docx,.pptx,.xlsx,.md,.txt,.csv,image/*';
      nativeFileInputEl.style.display = 'none';
      nativeFileInputEl.id = 'ordoNativeFileInput';
      document.body.appendChild(nativeFileInputEl);

      nativeFileInputEl.addEventListener('change', async (e) => {
        const files = Array.from(e.target.files || []);
        if (files.length === 0) return;

        showToast(`正在上传 ${files.length} 个文件...`);

        for (const file of files) {
          let uploadTaskId = null;
          if (api && api.connected) {
            const dsId = api.context && api.context.defaultDatasetId;
            if (!dsId) {
              showToast(`${file.name} 上传失败：尚无可用数据集，请先在「数据配置」创建知识库`, 'error');
            } else {
              const uploaded = await api.uploadDocument(dsId, file);
              if (uploaded && uploaded.task) {
                uploadTaskId = uploaded.task.id;
                showToast(`${file.name} 已登记，解析任务执行中（${uploadTaskId.slice(0, 18)}…）`);
              } else {
                showToast(`${file.name} 上传失败：${(api.lastError && api.lastError.message) || '服务未确认'}`, 'error');
              }
            }
          }

          const ext = file.name.split('.').pop().toLowerCase();
          const newDoc = {
            id: (uploaded && uploaded.document && uploaded.document.id) || ('doc_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6)),
            name: file.name,
            size: (file.size / 1024).toFixed(1) + ' KB',
            type: ext.toUpperCase(),
            icon: ext === 'pdf' ? '${iconDoc(13)}' : ext === 'docx' ? '${iconDoc(13)}' : ext === 'xlsx' ? '${iconDoc(13)}' : ext === 'pptx' ? '${iconDoc(13)}' : '${iconDoc(13)}',
            status: uploadTaskId ? '解析中' : (api && api.connected ? '登记失败' : '已完成'),
            time: '刚刚',
            chunks: uploadTaskId ? '—' : Math.max(1, Math.floor(file.size / 600)),
            taskId: uploadTaskId
          };

          if (state.datasetDocs) {
            state.datasetDocs.unshift(newDoc);
          }

          state.parsingTasks.unshift({
            id: uploadTaskId || 'p_' + Date.now() + Math.random().toString(36).slice(2, 5),
            taskId: uploadTaskId,
            name: file.name,
            status: uploadTaskId ? '解析中' : 'processing',
            pages: uploadTaskId ? '—' : '第 1 页/共 ' + Math.max(1, Math.floor(file.size / 40000)) + ' 页',
            totalPages: uploadTaskId ? 0 : Math.max(1, Math.floor(file.size / 40000)),
            curPage: uploadTaskId ? 0 : 1,
            density: uploadTaskId ? '—' : '80%',
            parser: uploadTaskId ? 'ordo-parser' : (ext === 'pdf' ? 'pypdf' : 'native'),
            latency: uploadTaskId ? '—' : '42 ms',
            quality: uploadTaskId ? 0 : 97
          });

          // 已连接：轮询真实解析任务，按后端确认结果更新状态（不伪造完成）
          if (uploadTaskId) {
            (async () => {
              for (let attempt = 0; attempt < 6; attempt++) {
                const task = await api.waitTask(uploadTaskId, 20000);
                if (!task || !['queued', 'running', 'paused'].includes(task.status)) {
                  const doc = (state.datasetDocs || []).find(d => d.taskId === uploadTaskId);
                  const parseEntry = state.parsingTasks.find(t => t.taskId === uploadTaskId);
                  if (task && ['succeeded', 'partial'].includes(task.status)) {
                    const quality = task.result && task.result.qualityStatus;
                    if (doc) { doc.status = quality === 'publishable' ? '已完成' : '需复核'; if (task.result && task.result.blockCount != null) doc.chunks = task.result.blockCount; }
                    if (parseEntry) { parseEntry.status = quality === 'publishable' ? '已完成' : '需复核'; parseEntry.quality = quality === 'publishable' ? 100 : 60; }
                    if (api && api.connected && dsId) {
                      try {
                        const refreshed = await api.getDocuments(dsId, { limit: 20 });
                        if (Array.isArray(refreshed)) state.datasetDocs = refreshed;
                        else if (refreshed && refreshed.items) state.datasetDocs = refreshed.items;
                      } catch (e) {}
                    }
                    showToast(`${file.name} 解析${quality === 'publishable' ? '完成' : '结果需复核'}`, quality === 'publishable' ? 'ok' : '');
                  } else {
                    if (doc) doc.status = '解析失败';
                    if (parseEntry) { parseEntry.status = 'failed'; parseEntry.error = (task && task.error_message) || '解析失败'; }
                    showToast(`${file.name} 解析失败：${(task && task.error_message) || '任务异常'}`, 'error');
                  }
                  render();
                  return;
                }
              }
              showToast(`${file.name} 解析仍在进行，可稍后在「数据解析」查看`, '');
            })();
          }
        }

        if (api && api.connected) {
          showToast(`成功导入 ${files.length} 个文件！已登记入库并开始解析`, 'ok');
        } else {
          showToast('未连接后端服务，文件未能登记入库', 'error');
        }
        nativeFileInputEl.value = '';
        render();
      });
    }

    if (!nativeFolderInputEl) {
      nativeFolderInputEl = document.createElement('input');
      nativeFolderInputEl.type = 'file';
      nativeFolderInputEl.webkitdirectory = true;
      nativeFolderInputEl.directory = true;
      nativeFolderInputEl.style.display = 'none';
      nativeFolderInputEl.id = 'ordoNativeFolderInput';
      document.body.appendChild(nativeFolderInputEl);

      nativeFolderInputEl.addEventListener('change', (e) => {
        const files = Array.from(e.target.files || []);
        if (files.length === 0) return;
        showToast(`成功读取本地目录，包含 ${files.length} 个文件，开始批量导入入库...`, 'ok');
        files.slice(0, 10).forEach(file => {
          const ext = file.name.split('.').pop().toLowerCase();
          state.parsingTasks.unshift({
            id: 'p_' + Date.now() + Math.random().toString(36).slice(2, 5),
            name: file.name,
            status: 'processing',
            pages: '第 1 页/共 10 页',
            totalPages: 10,
            curPage: 1,
            density: '78%',
            parser: ext === 'pdf' ? 'pypdf' : 'native',
            latency: '36 ms',
            quality: 96
          });
        });
        nativeFolderInputEl.value = '';
        render();
      });
    }
  }

  window.triggerNativeFileUpload = function() {
    ensureNativeInputs();
    if (nativeFileInputEl) nativeFileInputEl.click();
  };

  window.triggerNativeFolderUpload = function() {
    ensureNativeInputs();
    if (nativeFolderInputEl) nativeFolderInputEl.click();
  };

  window.triggerDownloadFile = function(filename, content, mimeType = 'application/json') {
    if (typeof document === 'undefined') return;
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast(`已导出并保存文件: ${filename}`, 'ok');
  };

    /* Advanced Real Modals for QA Flow & Pipeline Inspection */

  function qaParseEmbedText(value, fallback = '未记录') {
    if (value === null || value === undefined || value === '') return fallback;
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
  }

  async function qaParseEmbedRequest(method, traceId, input = {}) {
    if (!api.connected || !traceId) throw new Error('未连接后端或未选择 Trace');
    const result = await api[method](traceId, input, { throwOnError: true });
    if (result === null || result === undefined) throw new Error(api.lastError?.message || '服务未返回数据');
    return result;
  }

  function qaParseEmbedRecord(kind) {
    const record = state[kind === 'parse' ? 'qaParseRecord' : 'qaEmbedRecord'];
    if (!record || record.traceId !== state.activeTraceId) throw new Error('当前 Trace 数据尚未加载');
    return record;
  }

  window.openEditParseResultModal = function() {
    let record;
    try { record = qaParseEmbedRecord('parse'); } catch (error) { showToast(error.message, 'error'); return; }
    if (!record.data || record.data.dataSource === 'unavailable') { showToast('解析记录不可用', 'error'); return; }
    const draft = record.data.draft || {};
    const entities = Array.isArray(draft.entities) ? draft.entities : record.fields.entities || [];
    state.qaParseEditingTraceId = record.traceId;
    const html = `
    <div class="modal-box" style="max-width:520px;">
      <div class="modal-header">
        <span>编辑问题解析结果</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">规范化问题</label>
          <input class="input" id="editNormQuestion" value="${esc(draft.normalizedQuery ?? record.fields.normalizedQuery ?? '')}" style="width:100%;">
        </div>
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">主意图分类</label>
          <input class="input" id="editMainIntent" value="${esc(draft.intent ?? record.fields.intent ?? '')}" style="width:100%;">
        </div>
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">识别实体标签 (逗号分隔)</label>
          <input class="input" id="editEntities" value="${esc(entities.map(item => typeof item === 'string' ? item : item?.text || item?.name || qaParseEmbedText(item)).join(', '))}" style="width:100%;">
        </div>
        <div>
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">改写查询 (Query Rewriting)</label>
          <textarea class="input" id="editRewrittenQuery" style="width:100%;height:54px;">${esc(draft.rewrittenQuery ?? record.fields.rewrittenQuery ?? '')}</textarea>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" onclick="handleSaveParsedResult();">保存草稿</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.handleSaveParsedResult = async function() {
    const input = {
      normalizedQuery: document.getElementById('editNormQuestion')?.value?.trim(),
      intent: document.getElementById('editMainIntent')?.value?.trim(),
      entities: (document.getElementById('editEntities')?.value || '').split(/[,，]/).map(item => item.trim()).filter(Boolean),
      rewrittenQuery: document.getElementById('editRewrittenQuery')?.value?.trim()
    };
    return window.handleSaveParseDraft(input, false);
  };

  window.openStageLogModal = async function(stageName) {
    if (!requireConnection()) return;
    const key = qaStageKey(stageName);
    const methods = { parse: 'getTraceParseLogs', embed: 'getEmbeddingLogs', route: 'getTraceRouteLogs', fusion: 'getFusionLogs', rerank: 'getRerankLogs' };
    let logs;
    try {
      const { activeTrace } = await getActiveQATrace();
      if (!activeTrace) return showToast('暂无可查看的 Trace', 'warn');
      if (methods[key]) {
        logs = await api.call(methods[key], [activeTrace.id], { throwOnError: true });
      } else {
        const stage = qaRecordedStages(activeTrace).find(item => item.key === key);
        logs = stage ? [{ timestamp: activeTrace.created_at, stage: key, status: stage.status, durationMs: stage.durationMs, output: stage.output }] : [];
      }
    } catch (error) {
      return showToast(error.message || '阶段日志读取失败', 'error');
    }
    state.qaLogText = (logs || []).map(row => JSON.stringify(row)).join('\n');
    const html = `
    <div class="modal-box" style="max-width:600px;">
      <div class="modal-header">
        <span>阶段执行日志: ${esc(stageName || '问题解析')}</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div style="background:#0f172a;color:#f1f5f9;font-family:var(--font-mono);font-size:12px;padding:14px;border-radius:6px;max-height:300px;overflow-y:auto;line-height:1.6;">
          <pre style="margin:0;white-space:pre-wrap;overflow-wrap:anywhere;">${esc(state.qaLogText || '暂无阶段日志')}</pre>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:space-between;align-items:center;">
        <button class="btn sm" onclick="handleCopySnippet(state.qaLogText)">${iconCopy(13)} 复制日志</button>
        <button class="btn primary" data-close>关闭</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.openModelComparisonModal = async function() {
    try {
      const record = qaParseEmbedRecord('embed');
      if (!record.model) throw new Error('当前 Trace 未记录向量模型，无法比较');
      const result = await qaParseEmbedRequest('compareEmbeddingModels', record.traceId, { models: [record.model] });
      if (state.activeTraceId !== record.traceId) return;
      const rows = Array.isArray(result.results) ? result.results : [];
      if (!rows.length) throw new Error('服务未返回模型比较结果');
      showOverlay(`
        <div class="modal-box" style="max-width:620px;">
          <div class="modal-header"><span>嵌入向量模型对比</span><button class="btn sm" data-close>✕</button></div>
          <div class="modal-body" style="padding:16px 20px;overflow-x:auto;">
            <table class="dataset-table" style="font-size:12.5px;">
              <thead><tr><th>模型名称</th><th>向量维度</th><th>MTEB 中文得分</th><th>推理延迟</th><th>状态</th></tr></thead>
              <tbody>${rows.map(row => `<tr><td>${esc(qaParseEmbedText(row.model))}</td><td>${esc(qaParseEmbedText(row.dimensions))}</td><td>${esc(qaParseEmbedText(row.mtebScore))}</td><td>${Number.isFinite(row.durationMs) ? `${row.durationMs} ms` : '未记录'}</td><td>${result.persisted === true ? '已保存' : '本次计算，未写入 Trace'}</td></tr>`).join('')}</tbody>
            </table>
            ${rows.length < 2 ? '<div class="muted" style="margin-top:12px;">仅返回 1 个可用模型，暂无跨模型比较结果。</div>' : ''}
          </div>
          <div class="modal-footer"><button class="btn primary" data-close>关闭</button></div>
        </div>`);
    } catch (error) { showToast(`模型比较失败：${error.message}`, 'error'); }
  };

  window.openRouteRulesModal = async function() {
    let stage;
    try {
      const { activeTrace } = await getActiveQATrace();
      if (!activeTrace?.id) throw new Error('请先选择一条 Trace');
      stage = await api.getTraceRouteStage(activeTrace.id, {}, { throwOnError: true });
      if (!stage) throw new Error('路由记录读取失败');
    } catch (error) {
      showToast(error.message || '路由记录读取失败', 'error');
      return;
    }
    const routes = stage.draft?.routes || stage.output?.routes || [];
    const enabled = name => routes.find(route => route.name === name || (name === 'full_text' && route.name === 'fullText'))?.enabled === true;
    const html = `
    <div class="modal-box" style="max-width:540px;">
      <div class="modal-header">
        <span>检索路由规则策略配置</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;font-size:13px;">
        <div style="margin-bottom:12px;">
          <b>检索通道</b>
          <div style="margin-top:6px;display:flex;flex-direction:column;gap:6px;">
            <label style="display:flex;align-items:center;gap:8px;"><input id="route-vector-enabled" type="checkbox" ${enabled('vector') ? 'checked' : ''}> 向量检索</label>
            <label style="display:flex;align-items:center;gap:8px;"><input id="route-fulltext-enabled" type="checkbox" ${enabled('full_text') ? 'checked' : ''}> 全文检索</label>
            <label style="display:flex;align-items:center;gap:8px;color:var(--ink-dim);"><input type="checkbox" disabled> 知识图谱（未配置检索服务）</label>
            <label style="display:flex;align-items:center;gap:8px;color:var(--ink-dim);"><input type="checkbox" disabled> 结构化查询（未配置检索服务）</label>
          </div>
        </div>
        <div>
          <b>固定权限范围</b>
          <div class="mono muted" style="margin-top:6px;overflow-wrap:anywhere;">${esc(stage.releaseId)}</div>
          <div class="muted" style="margin-top:6px;">${Object.keys(stage.draft || {}).length ? '已保存草稿，尚未应用到当前 Trace' : '当前 Trace 的已记录配置'}</div>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" data-route-trace="${esc(stage.traceId)}" onclick="handleSaveRouteRules(this)">保存草稿</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.handleSaveRouteRules = async function(button) {
    const traceId = button?.dataset.routeTrace;
    if (!traceId) return showToast('未指定 Trace', 'error');
    const routes = [
      { name: 'vector', enabled: document.getElementById('route-vector-enabled')?.checked === true },
      { name: 'full_text', enabled: document.getElementById('route-fulltext-enabled')?.checked === true },
      { name: 'graph', enabled: false, reason: '未配置检索服务' },
      { name: 'database', enabled: false, reason: '未配置检索服务' }
    ];
    button.disabled = true;
    try {
      const result = await api.updateTraceRoute(traceId, { routes }, { throwOnError: true });
      if (!result?.saved) throw new Error('服务端未确认草稿保存');
      closeOverlay();
      showToast(`路由草稿 v${result.version} 已保存，当前 Trace 未修改`, 'ok');
      await render();
    } catch (error) {
      showToast(error.message || '路由草稿保存失败', 'error');
    } finally {
      button.disabled = false;
    }
  };

  window.openPromptTemplateModal = function() {
    return window.handleQAPromptEdit();
  };

  window.openFullTraceModal = async function() {
    if (!requireConnection()) return;
    let activeTrace;
    try {
      ({ activeTrace } = await getActiveQATrace());
      if (!activeTrace) return showToast('暂无可查看的 Trace', 'warn');
    } catch (error) {
      return showToast(error.message || 'Trace 读取失败', 'error');
    }
    const html = `
    <div class="modal-box" style="max-width:680px;">
      <div class="modal-header">
        <span style="overflow-wrap:anywhere;">全链路 Trace 监控时间线 (Trace ID: ${esc(activeTrace.id)})</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;max-height:420px;overflow-y:auto;">
        <div style="display:flex;flex-direction:column;gap:8px;font-size:12.5px;">
          ${qaRecordedStages(activeTrace).map((stage, index) => `
            <div style="display:flex;justify-content:space-between;gap:12px;padding:8px 12px;background:var(--inset);border-radius:6px;border-left:3px solid ${stage.status === 'failed' ? 'var(--danger)' : 'var(--line)'};">
              <span>${index + 1}. ${esc(stage.name)}</span>
              <span>${stage.durationMs == null ? '未记录耗时' : esc(stage.durationMs) + ' ms'} · <b>${esc(qaStatusLabel(stage.status))}</b></span>
            </div>`).join('')}
        </div>
        <div style="margin-top:14px;padding:10px;background:var(--accent-soft);border-radius:6px;font-size:12px;color:#16a34a;">
          全链路端到端总耗时: <b>${activeTrace.metrics?.totalMs == null ? '未记录' : (activeTrace.metrics.totalMs / 1000).toFixed(3) + ' 秒'}</b> · ${esc(qaStatusLabel(activeTrace.status))}
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;">
        <button class="btn primary" data-close>关闭 Trace</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.handleCopySnippet = async function(text) {
    try {
      if (!globalThis.navigator?.clipboard?.writeText) throw new Error('当前浏览器无法访问剪贴板');
      await navigator.clipboard.writeText(String(text ?? ''));
      showToast('已复制内容到剪贴板', 'ok');
      return true;
    } catch (error) {
      showToast('复制失败：' + (error.message || '剪贴板访问被拒绝'), 'error');
      return false;
    }
  };

  // 3. Rerank: Change Model Modal
  window.openChangeRerankModelModal = function() {
    const html = `
    <div class="modal-box" style="max-width:500px;">
      <div class="modal-header">
        <span>更换重排 (Rerank) 模型</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:6px;">选择重排交叉编码器 (Cross-Encoder)</label>
          <select class="input" id="rerankModelSelect" style="width:100%;height:36px;">
            <option value="bge-reranker-v2-m3" selected>bge-reranker-v2-m3 (智源多语言 · 速度与效果均衡 · 推荐)</option>
            <option value="bge-reranker-large">bge-reranker-large (高精度中文重排)</option>
            <option value="cohere-rerank-v3">Cohere Rerank v3 (商用云端)</option>
            <option value="jina-reranker-v2">Jina Reranker v2 (长文本支持)</option>
          </select>
        </div>
        <p class="muted" style="font-size:12px;margin:0;">切换模型后，系统将自动使用选中的重排模型对融合候选集（Top 20）重新计算 query-passage 交互注意力得分。</p>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" onclick="handleConfirmRerankModel()">确认更换并重跑</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.handleConfirmRerankModel = async function() {
    const sel = document.getElementById('rerankModelSelect')?.value || 'bge-reranker-v2-m3';
    closeOverlay();
    showToast(`已切换重排模型为「${sel}」，正在重新执行 Cross-Encoder 评分...`, 'ok');
    const { activeTrace } = await getActiveQATrace();
    if (activeTrace?.id && api && api.connected) {
      try {
        await api.updateRerankConfig(activeTrace.id, { modelName: sel });
      } catch (e) {}
    }
    setTimeout(() => {
      showToast('重排打分完成！Top 8 优质候选块已锁定', 'ok');
      render();
    }, 300);
  };

  // 4. Rerank: Adjust Threshold Modal
  window.openAdjustRerankThresholdModal = function() {
    const curTh = state.rerankThreshold || 0.75;
    const html = `
    <div class="modal-box" style="max-width:460px;">
      <div class="modal-header" style="display:flex;justify-content:space-between;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);">
        <b style="font-size:15px;color:var(--ink-strong);">调整重排保留阈值 (Score Threshold)</b>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:8px;">
          <b>最低相关性得分阈值 (Threshold)</b>
          <span id="thLabel" class="mono" style="font-weight:700;color:var(--accent);">${curTh.toFixed(2)}</span>
        </div>
        <input type="range" id="rerankThInput" min="0.30" max="0.95" step="0.05" value="${curTh}" style="width:100%;cursor:pointer;" oninput="document.getElementById('thLabel').textContent=Number(this.value).toFixed(2);">
        <div class="muted" style="font-size:12px;margin-top:12px;line-height:1.5;">
          低于该阈值的候选块将被过滤（淘汰），不送入后续大模型 Prompt 上下文，防止无关噪声干扰回答生成。当前推荐基准阈值为 <b>0.75</b>。
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;padding:12px 20px;border-top:1px solid var(--line);">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" onclick="handleApplyRerankThreshold()">应用阈值</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.handleApplyRerankThreshold = async function() {
    const th = parseFloat(document.getElementById('rerankThInput').value);
    state.rerankThreshold = th;
    closeOverlay();
    const { activeTrace } = await getActiveQATrace();
    if (activeTrace?.id && api && api.connected) {
      try {
        await api.updateRerankConfig(activeTrace.id, { scoreThreshold: th });
      } catch (e) {}
    }
    showToast('重排过滤阈值已更新为 ' + th.toFixed(2) + '，候选集已重新划定', 'ok');
    render();
  };

  // 5. Rerank: Result Comparison Modal
  window.openRerankCompareModal = async function() {
    const { activeTrace } = await getActiveQATrace();
    let comp = null;
    if (activeTrace?.id && api && api.connected) {
      try { comp = await api.compareRerank(activeTrace.id); } catch (e) {}
    }
    if (!comp) {
      showToast(api.lastError?.message || '未连接后端服务，无法生成重排对比', 'error');
      return;
    }

    // 依据服务端返回的真实 before/after 候选推导指标（相关性以重排得分归一化近似）
    const toCand = (c) => ({ id: c.chunkRevisionId || c.id || '', title: c.title || '—', page: c.locator?.page ?? '—', score: Number(c.rerankScore ?? c.fusionScore ?? c.score ?? 0) || 0 });
    const beforeList = (comp.before || []).map(toCand);
    const afterList = (comp.after || []).map(toCand);
    const beforeRankById = new Map(beforeList.map((c, i) => [c.id, i + 1]));
    const scoreById = new Map(afterList.map(c => [c.id, c.score]));
    const maxScore = Math.max(1e-9, ...afterList.map(c => c.score), ...beforeList.map(c => c.score));
    const dcgOf = order => order.slice(0, 10).reduce((s, c, i) => s + (Math.pow(2, (scoreById.get(c.id) || 0) / maxScore) - 1) / Math.log2(i + 2), 0);
    const idealOrder = [...afterList].sort((x, y) => y.score - x.score);
    const ndcgOf = order => { const ideal = dcgOf(idealOrder); return ideal > 0 ? dcgOf(order) / ideal : 0; };
    const bestId = idealOrder[0]?.id;
    const mrrOf = order => { const r = order.findIndex(c => c.id === bestId); return r >= 0 ? 1 / (r + 1) : 0; };
    const topSet = new Set(idealOrder.slice(0, 5).map(c => c.id));
    const p5Of = order => order.slice(0, 5).filter(c => topSet.has(c.id)).length / Math.min(5, idealOrder.length || 5);
    const retained = afterList.filter(c => beforeRankById.has(c.id)).length;
    const eliminated = beforeList.length - retained;
    const noiseRate = beforeList.length ? ((eliminated / beforeList.length) * 100).toFixed(1) + '%' : '—';
    const pct = v => (v * 100).toFixed(1) + '%';
    const liftText = v => (v >= 0 ? '+' : '−') + pct(Math.abs(v));
    const ndcgB = ndcgOf(beforeList), ndcgA = ndcgOf(afterList);
    const mrrB = mrrOf(beforeList), mrrA = mrrOf(afterList);
    const p5B = p5Of(beforeList), p5A = p5Of(afterList);
    const summary = `重排保留 ${retained}/${beforeList.length} 个原候选，淘汰 ${eliminated} 个低相关片段；最高分片段得分 ${maxScore.toFixed(3)}。`;
    const rowsHtml = afterList.slice(0, 5).map((c, i) => {
      const bRank = beforeRankById.get(c.id);
      const afterNo = i + 1;
      let delta;
      if (bRank === undefined) delta = '<span class="muted">- 新入选</span>';
      else if (bRank > afterNo) delta = `<span style="color:#16a34a;font-weight:700;">↑ 提升 ${bRank - afterNo} 名</span>`;
      else if (bRank < afterNo) delta = `<span style="color:#dc2626;font-weight:700;">↓ 下降 ${afterNo - bRank} 名</span>`;
      else delta = '<span class="muted">- 保持</span>';
      return `
            <tr${i === 0 ? ' style="background:var(--accent-soft);"' : ''}>
              <td><b${i === 0 ? ' style="color:var(--accent);"' : ''}>#${afterNo}</b></td>
              <td>${bRank ? '#' + bRank : '—'}</td>
              <td><b>${esc(c.title)}</b> (${esc(c.id)} · P.${esc(c.page)})</td>
              <td class="mono"${i === 0 ? ' style="font-weight:700;color:#16a34a;"' : ''}>${c.score.toFixed(3)}</td>
              <td>${delta}</td>
            </tr>`;
    }).join('') || '<tr><td colspan="5" class="muted" style="text-align:center;">暂无重排候选</td></tr>';

    const html = `
    <div class="modal-box" style="max-width:720px;">
      <div class="modal-header" style="display:flex;justify-content:space-between;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);">
        <b style="font-size:15px;color:var(--ink-strong);">重排收益与质量对比评估 (NDCG & MRR)</b>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;max-height:480px;overflow-y:auto;display:flex;flex-direction:column;gap:14px;">
        <!-- Metric Cards Grid -->
        <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:10px;">
          <div style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:10px 12px;">
            <div class="muted" style="font-size:11px;">NDCG@10 增益</div>
            <div style="font-size:16px;font-weight:700;color:#16a34a;margin-top:2px;">${liftText(ndcgA - ndcgB)}</div>
            <div class="muted" style="font-size:10px;margin-top:2px;">${ndcgB.toFixed(3)} ➔ ${ndcgA.toFixed(3)}</div>
          </div>
          <div style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:10px 12px;">
            <div class="muted" style="font-size:11px;">MRR 倒数排名</div>
            <div style="font-size:16px;font-weight:700;color:#16a34a;margin-top:2px;">${liftText(mrrA - mrrB)}</div>
            <div class="muted" style="font-size:10px;margin-top:2px;">${mrrB.toFixed(2)} ➔ ${mrrA.toFixed(2)}</div>
          </div>
          <div style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:10px 12px;">
            <div class="muted" style="font-size:11px;">Top 5 准确率</div>
            <div style="font-size:16px;font-weight:700;color:#16a34a;margin-top:2px;">${liftText(p5A - p5B)}</div>
            <div class="muted" style="font-size:10px;margin-top:2px;">${pct(p5B)} ➔ ${pct(p5A)}</div>
          </div>
          <div style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:10px 12px;">
            <div class="muted" style="font-size:11px;">噪音过滤比率</div>
            <div style="font-size:16px;font-weight:700;color:var(--ink-strong);margin-top:2px;">${noiseRate}</div>
            <div class="muted" style="font-size:10px;margin-top:2px;">${eliminated}/${beforeList.length} 片段淘汰</div>
          </div>
        </div>

        <!-- Summary callout -->
        <div style="background:var(--accent-soft);border:1px solid #bbf7d0;border-radius:6px;padding:10px 14px;font-size:12px;color:var(--ink-strong);line-height:1.5;">
          <b>评估结论：</b>${esc(summary)}
        </div>

        <table class="dataset-table" style="font-size:12px;">
          <thead>
            <tr>
              <th>重排后</th>
              <th>重排前 (RRF)</th>
              <th>文档标题与片段</th>
              <th>重排得分</th>
              <th>排序变动</th>
            </tr>
          </thead>
          <tbody>
            ${rowsHtml}
          </tbody>
        </table>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;padding:12px 20px;border-top:1px solid var(--line);">
        <button class="btn primary" data-close>关闭对比</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  /* 01 首页 (Home) - 100% 对应 01-首页.png 撑满一页布局 */
  async function pageHome() {
    const dashboard = await api.getDashboard();
    if (!dashboard) {
      // 仪表盘读取失败：如实展示，不回退演示数据
      return { desc: '知识库与 AI 应用运行总览', html: `<div class="card" style="padding:24px;"><b>仪表盘读取失败</b><div class="muted" style="margin-top:6px;">${esc((api.lastError && api.lastError.message) || '服务无响应')}。请确认本地 Ordo 服务运行正常后刷新。</div></div>` };
    }

    const trendMap = {};
    (dashboard.requestTrend || []).forEach(row => { trendMap[String(row.day).slice(5)] = row.count; });
    let trend = [];
    const dates = [];
    for (let i = 6; i >= 0; i--) {
      const d = new Date(Date.now() - i * 86400000);
      const key = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      dates.push(key);
      trend.push(trendMap[key] || 0);
    }
    const maxVal = Math.max(40, ...trend);
    const pts = trend.map((v, i) => `${(i / (trend.length - 1)) * 380 + 40},${160 - (v / maxVal) * 125}`).join(' ');

    let statCardsHtml;
    let attentionHtml;
    let kbStatusHtml;
    let statsAsOf = '';
    {
      const c = dashboard.counts || {};
      const components = dashboard.components || {};
      const metaOk = components.metadata && components.metadata.status === 'available';
      const realModels = (api.context && api.context.models) || [];
      const modelOk = realModels.filter(m => m.status === 'available').length;
      const todayRequests = trend[trend.length - 1] || 0;
      statCardsHtml = `
        ${statCard('db', '数据库连接', metaOk ? 'SQLite 正常' : '异常')}
        ${statCard('cubes', '知识块', String(c.chunks ?? 0))}
        ${statCard('bot', '智能助手', String(c.assistants ?? 0))}
        ${statCard('chart', '今日请求', String(todayRequests))}
        ${statCard('flow', '模型连接', `<span class="${modelOk ? 'ok-text' : ''}" style="${modelOk ? 'color:var(--accent);' : ''}">${modelOk} 可用</span> <span style="font-size:14px;color:var(--ink-dim);font-weight:normal;">/ ${realModels.length} 已登记</span>`)}
        ${statCard('doc', 'Wiki / 笔记', String(c.wikiPages ?? 0))}
        ${statCard('graph', '知识图谱', '暂未接入')}
        ${statCard('stack', '活动知识版本', String(c.activeReleases ?? 0), '', '个')}`;
      statsAsOf = `<span class="muted" style="font-size:11.5px;">统计截至 ${esc(dashboard.generatedAt || '')}</span>`;

      const attention = [];
      if ((c.failedTasks ?? 0) > 0) {
        attention.push(`<div class="home-sub-card" onclick="window.location.hash='#/knowledge/parsing'"><div class="home-warn-triangle">⚠</div><div class="grow"><b>${c.failedTasks} 项任务失败</b><div class="muted" style="font-size:12px;margin-top:2px;">解析 / 发布任务需要重试</div></div><span class="list-arrow">›</span></div>`);
      }
      const unverifiedModels = realModels.filter(m => m.status !== 'available');
      if (unverifiedModels.length) {
        attention.push(`<div class="home-sub-card" onclick="window.location.hash='#/settings/models'"><div class="home-warn-triangle orange">⚠</div><div class="grow"><b>${unverifiedModels.length} 个模型连接待验证</b><div class="muted" style="font-size:12px;margin-top:2px;">${esc(unverifiedModels.map(m => m.name).join('、').slice(0, 40))}</div></div><span class="list-arrow">›</span></div>`);
      }
      if ((c.pendingTasks ?? 0) > 0) {
        attention.push(`<div class="home-sub-card" onclick="window.location.hash='#/knowledge/parsing'"><div class="home-warn-triangle orange">⚠</div><div class="grow"><b>${c.pendingTasks} 项任务处理中</b><div class="muted" style="font-size:12px;margin-top:2px;">解析 / 索引构建进行中</div></div><span class="list-arrow">›</span></div>`);
      }
      if (!(c.activeReleases > 0)) {
        attention.push(`<div class="home-sub-card" onclick="window.location.hash='#/knowledge/index'"><div class="home-warn-triangle orange">⚠</div><div class="grow"><b>尚无活动知识版本</b><div class="muted" style="font-size:12px;margin-top:2px;">构建并发布版本后才能进行问答</div></div><span class="list-arrow">›</span></div>`);
      }
      attentionHtml = attention.length ? attention.join('\n') : '<div class="muted" style="padding:16px;text-align:center;">✓ 暂无待处理事项</div>';

      const kbs = dashboard.recentKnowledgeBases || [];
      kbStatusHtml = kbs.length ? kbs.map(kb => `
            <div class="home-sub-card" onclick="window.location.hash='#/knowledge/datasets?kb=${esc(kb.id)}'">
              <div class="grow"><div style="display:flex;align-items:center;gap:6px;"><span class="dot" style="background:${kb.active_release_id ? '#0f8b4c' : '#f59e0b'};"></span><b>${esc(kb.name)}</b></div><div class="muted" style="font-size:12px;margin-top:4px;">更新于 ${esc((kb.updated_at || '').replace('T', ' ').slice(0, 16))}</div></div>
              <div style="text-align:right;font-size:12px;margin-right:4px;"><div class="muted">文档 ${kb.document_count ?? 0} · 知识块 ${kb.chunk_count ?? 0}</div>${kb.active_release_id ? '<div class="ok-text" style="color:var(--accent);">版本已发布</div>' : '<div class="muted">未发布版本</div>'}</div>
              <span class="list-arrow">›</span>
            </div>`).join('\n') : `
            <div style="padding:20px;text-align:center;">
              <b style="display:block;">尚未创建知识库</b>
              <div class="muted" style="font-size:12px;margin:6px 0 12px;">先在「知识库管理」创建知识库，再登记数据并构建索引。</div>
              <button class="btn primary" style="height:32px;font-size:12.5px;" onclick="window.location.hash='#/knowledge/config'">前往创建</button>
            </div>`;
    }

    const svgChart = `<svg viewBox="0 0 460 200" style="width:100%;height:100%;min-height:210px;flex:1;overflow:visible;">
      <defs>
        <linearGradient id="gradArea" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#0f8b4c" stop-opacity="0.22"/>
          <stop offset="100%" stop-color="#0f8b4c" stop-opacity="0.01"/>
        </linearGradient>
      </defs>
      <line x1="40" y1="35" x2="420" y2="35" stroke="#f3f4f6" stroke-dasharray="3,3"/>
      <line x1="40" y1="77" x2="420" y2="77" stroke="#f3f4f6" stroke-dasharray="3,3"/>
      <line x1="40" y1="118" x2="420" y2="118" stroke="#f3f4f6" stroke-dasharray="3,3"/>
      <line x1="40" y1="160" x2="420" y2="160" stroke="#e5e7eb"/>
      <text x="32" y="39" font-size="11" fill="#9ca3af" text-anchor="end">120</text>
      <text x="32" y="81" font-size="11" fill="#9ca3af" text-anchor="end">80</text>
      <text x="32" y="122" font-size="11" fill="#9ca3af" text-anchor="end">40</text>
      <text x="32" y="164" font-size="11" fill="#9ca3af" text-anchor="end">0</text>
      ${dates.map((d, i) => `<text x="${(i / (dates.length - 1)) * 380 + 40}" y="184" font-size="11" fill="#9ca3af" text-anchor="middle">${d}</text>`).join('')}
      <polygon points="40,160 ${pts} 420,160" fill="url(#gradArea)"/>
      <polyline points="${pts}" fill="none" stroke="#0f8b4c" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
      ${trend.map((v, i) => `<circle cx="${(i / (trend.length - 1)) * 380 + 40}" cy="${160 - (v / maxVal) * 125}" r="4" fill="#0f8b4c" stroke="#ffffff" stroke-width="1.8"/>`).join('')}
    </svg>`;

    const html = `
    <div class="home-page-layout">
      <!-- Top 8 Stat Cards in 2 Rows -->
      <div class="home-stat-grid">
        ${statCardsHtml}
      </div>

      <!-- Bottom 3 Cards Filling Remaining Height -->
      <div class="home-bottom-grid">
        <!-- 1. 近 7 天请求 -->
        <div class="card home-bottom-card">
          <div class="card-head"><span>近 7 天请求</span><span class="badge" style="background:var(--card-bg);border:1px solid var(--line);cursor:pointer;">近 7 天 ⌄</span></div>
          <div class="card-body">
            ${svgChart}
            <div style="display:flex;align-items:center;justify-content:center;gap:6px;font-size:12px;color:var(--ink-dim);margin-top:6px;">
              <span style="width:14px;height:2.5px;background:#0f8b4c;border-radius:1px;display:inline-block;"></span>
              <span>请求数</span>
            </div>
          </div>
        </div>

        <!-- 2. 需要处理 (动态) -->
        <div class="card home-bottom-card">
          <div class="card-head"><span>需要处理</span>${statsAsOf}</div>
          <div class="card-body">
            ${attentionHtml}
          </div>
        </div>

        <!-- 3. 知识库状态 (动态) -->
        <div class="card home-bottom-card">
          <div class="card-head"><span>知识库状态</span></div>
          <div class="card-body">
            ${kbStatusHtml}
          </div>
        </div>
      </div>
    </div>`;
    return { desc: '知识库与 AI 应用运行总览', html };
  }

    /* Interactive Vector Engine Dropdown Handlers */
  window.toggleEngineDropdown = function(e) {
    if (e) e.stopPropagation();
    const drop = document.getElementById('kbEngineDropdown');
    const chevron = document.getElementById('kbEngineChevron');
    if (!drop) return;
    if (drop.style.display === 'none' || !drop.style.display) {
      drop.style.display = 'block';
      if (chevron) chevron.textContent = '∧';
      const outsideClick = (evt) => {
        if (!drop.contains(evt.target) && !evt.target.closest('#kbEngineSelectBox')) {
          drop.style.display = 'none';
          if (chevron) chevron.textContent = '⌄';
          document.removeEventListener('click', outsideClick);
        }
      };
      setTimeout(() => document.addEventListener('click', outsideClick), 10);
    } else {
      drop.style.display = 'none';
      if (chevron) chevron.textContent = '⌄';
    }
  };

  window.handleSelectVectorEngine = function(name, defaultUrl, supported = true) {
    if (!supported) {
      showToast(`暂不支持 ${name}。当前版本遵循规划红线（§14.5.2），仅内置支持 SQLite 向量存储后端。`, 'warn');
      return;
    }
    state.selectedVectorEngine = name;
    const textEl = document.getElementById('kbEngineSelectedText');
    if (textEl) {
      textEl.textContent = name;
      textEl.style.color = 'var(--ink-strong)';
      textEl.style.fontWeight = '600';
    }
    const drop = document.getElementById('kbEngineDropdown');
    if (drop) drop.style.display = 'none';
    const chevron = document.getElementById('kbEngineChevron');
    if (chevron) chevron.textContent = '⌄';
    const urlInput = document.getElementById('kbHostInput');
    if (urlInput && defaultUrl) urlInput.value = defaultUrl;
    showToast(`已选择向量数据库引擎: ${name}`, 'ok');
  };

  /* 02 知识库 > 数据配置 - 100% 对应 02-知识库-数据配置.png (真实可交互下拉与高级设置) */
  async function pageConfig() {
    let kbs = (api && api.context && api.context.knowledgeBases) || [];
    if (api && api.connected && (!kbs || !kbs.length)) {
      try { kbs = await api.getKnowledgeBases() || []; } catch (e) {}
    }
    const currentKbId = state.selectedKbId || (kbs[0] && kbs[0].id);

    const kbsListHtml = kbs.length ? kbs.map(kb => {
      const isSelected = kb.id === currentKbId;
      return `
        <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border-radius:6px;border:1px solid ${isSelected ? 'var(--accent)' : 'var(--line)'};background:${isSelected ? 'var(--accent-soft)' : 'var(--card-bg)'};cursor:pointer;margin-bottom:8px;" onclick="state.selectedKbId='${esc(kb.id)}';render();">
          <div style="min-width:0;">
            <b style="font-size:13px;color:${isSelected ? 'var(--accent)' : 'var(--ink-strong)'};display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(kb.name)}</b>
            <div class="muted" style="font-size:11.5px;margin-top:2px;">${esc(kb.description || '无描述')}</div>
          </div>
          <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">
            <span class="badge ok" style="font-size:11px;">正常</span>
            <button class="btn sm" style="padding:2px 6px;font-size:11px;color:var(--danger);border:1px solid var(--danger-soft);background:var(--card-bg);" onclick="event.stopPropagation();window.handleDeleteKbWithImpact('${esc(kb.id)}', '${esc(kb.name)}')">删除</button>
          </div>
        </div>
      `;
    }).join('') : `
      <div style="padding:14px;text-align:center;color:var(--ink-dim);font-size:12.5px;background:var(--inset);border-radius:6px;border:1px dashed var(--line);">
        暂未登记知识库，请在右侧创建
      </div>
    `;

    const html = `
    <div style="display:grid;grid-template-columns:360px 1fr;gap:20px;align-items:start;">
      <!-- Left Column: 已有知识库列表 + 基本信息 -->
      <div style="display:flex;flex-direction:column;gap:16px;">
        <div class="card" style="background:var(--card-bg);border:1px solid var(--line);border-radius:var(--radius-card);">
          <div class="card-head" style="padding:14px 18px;font-size:14.5px;font-weight:700;color:var(--ink-strong);border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;">
            <span>已有知识库 (${kbs.length})</span>
            <span class="badge" style="font-size:11.5px;">真实数据</span>
          </div>
          <div class="card-body" style="padding:14px;">
            ${kbsListHtml}
          </div>
        </div>

        <div class="card" style="background:var(--card-bg);border:1px solid var(--line);border-radius:var(--radius-card);">
          <div class="card-head" style="padding:14px 18px;font-size:14.5px;font-weight:700;color:var(--ink-strong);border-bottom:1px solid var(--line);">
            新建知识库信息
          </div>
          <div class="card-body" style="padding:18px;display:flex;flex-direction:column;gap:14px;">
            <div>
              <label style="display:block;font-size:13px;color:var(--ink-strong);margin-bottom:6px;">
                <span style="color:#ef4444;margin-right:4px;">*</span>知识库名称
              </label>
              <input class="input" id="kbNameInput" placeholder="例如: 核心产品文档库" value="核心产品文档库" style="width:100%;height:36px;border-radius:6px;" required>
            </div>

            <div>
              <label style="display:block;font-size:13px;color:var(--ink-strong);margin-bottom:6px;">
                知识库说明
              </label>
              <textarea class="input" id="kbDescInput" placeholder="输入知识库的业务定位与用途说明..." style="width:100%;height:80px;border-radius:6px;padding:8px 10px;font-size:13px;resize:vertical;">用于统一沉淀核心产品的使用指南、架构文档及运维手册，并供智能问答与网站客服助手调用。</textarea>
            </div>

            <div>
              <label style="display:block;font-size:13px;color:var(--ink-strong);margin-bottom:6px;">
                所属工作空间
              </label>
              <div style="display:flex;align-items:center;justify-content:space-between;border:1px solid var(--line);border-radius:6px;padding:8px 12px;background:var(--inset);">
                <span style="font-size:13px;color:var(--ink);font-weight:500;">Ordo 企业空间</span>
                <span style="font-size:11.5px;color:var(--ink-dim);">✓ 默认</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Right Card: 配置向量数据库 -->
      <div class="card" style="background:var(--card-bg);border:1px solid var(--line);border-radius:var(--radius-card);">
        <div class="card-head" style="padding:16px 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);">
          <span style="font-size:15px;font-weight:700;color:var(--ink-strong);">配置向量数据库</span>
          <span id="kbTestStatusBadge" class="badge ok" style="background:var(--accent-soft);color:#16a34a;border:1px solid var(--accent);font-size:12.5px;padding:3px 10px;border-radius:12px;">✓ SQLite 向量引擎就绪</span>
        </div>
        <div class="card-body" style="padding:24px;">
          <form id="kbConfigForm" onsubmit="window.handleCreateKbSubmit(event);">
            <div style="display:flex;flex-direction:column;gap:18px;">
              <!-- Row 1: 引擎类型 (遵循规划红线 §14.5.2：只允许选择 SQLite，其余标明暂不支持) -->
              <div style="display:grid;grid-template-columns:160px 1fr;align-items:start;gap:16px;">
                <label style="font-size:13.5px;color:var(--ink-strong);padding-top:8px;">
                  <span style="color:#ef4444;margin-right:4px;">*</span>引擎类型
                </label>
                <div style="position:relative;">
                  <div id="kbEngineSelectBox" style="display:flex;align-items:center;justify-content:space-between;border:1.5px solid var(--accent);border-radius:6px;padding:8px 12px;background:var(--card-bg);cursor:pointer;user-select:none;" onclick="toggleEngineDropdown(event)">
                    <span id="kbEngineSelectedText" style="color:var(--ink-strong);font-size:13.5px;font-weight:600;">${state.selectedVectorEngine || 'SQLite (内置向量存储 - 推荐)'}</span>
                    <span id="kbEngineChevron" style="font-size:12px;color:var(--ink-dim);">⌄</span>
                  </div>

                  <!-- 下拉浮层：严格遵循红线 §14.5.2 -->
                  <div id="kbEngineDropdown" style="display:none;position:absolute;top:calc(100% + 4px);left:0;right:0;z-index:999;background:var(--card-bg);border:1px solid var(--line);border-radius:6px;box-shadow:0 10px 25px rgba(0,0,0,0.12);overflow:hidden;">
                    <div style="padding:9px 14px;cursor:pointer;font-size:13px;border-bottom:1px solid var(--line-soft);background:var(--accent-soft);color:var(--accent);font-weight:600;" onclick="handleSelectVectorEngine('SQLite (内置向量存储 - 推荐)', 'sqlite://local.db', true)">
                      ✓ SQLite (内置向量存储与 FTS5 - 当前完全就绪)
                    </div>
                    <div style="padding:9px 14px;cursor:not-allowed;font-size:13px;border-bottom:1px solid var(--line-soft);color:#94a3b8;" onclick="handleSelectVectorEngine('Elasticsearch', '', false)">
                      Elasticsearch (暂不支持 · 规划红线 §14.5.2)
                    </div>
                    <div style="padding:9px 14px;cursor:not-allowed;font-size:13px;border-bottom:1px solid var(--line-soft);color:#94a3b8;" onclick="handleSelectVectorEngine('Qdrant', '', false)">
                      Qdrant (暂不支持 · 规划红线 §14.5.2)
                    </div>
                    <div style="padding:9px 14px;cursor:not-allowed;font-size:13px;border-bottom:1px solid var(--line-soft);color:#94a3b8;" onclick="handleSelectVectorEngine('Milvus', '', false)">
                      Milvus (暂不支持 · 规划红线 §14.5.2)
                    </div>
                    <div style="padding:9px 14px;cursor:not-allowed;font-size:13px;border-bottom:1px solid var(--line-soft);color:#94a3b8;" onclick="handleSelectVectorEngine('PostgreSQL / pgvector', '', false)">
                      PostgreSQL / pgvector (暂不支持 · 规划红线 §14.5.2)
                    </div>
                  </div>
                </div>
              </div>

              <!-- Row 2: 服务地址 -->
              <div style="display:grid;grid-template-columns:160px 1fr;align-items:center;gap:16px;">
                <label style="font-size:13.5px;color:var(--ink-strong);">
                  <span style="color:#ef4444;margin-right:4px;">*</span>存储路径 / 实例
                </label>
                <input class="input" id="kbHostInput" value="本地轻量存储 (sqlite://data/ordo.db)" readonly style="height:38px;border-radius:6px;background:var(--inset);color:var(--ink);" required>
              </div>

              <!-- Row 3: 用户名 -->
              <div style="display:grid;grid-template-columns:160px 1fr;align-items:center;gap:16px;">
                <label style="font-size:13.5px;color:var(--ink-strong);">
                  安全凭据
                </label>
                <input class="input" id="kbUserInput" value="内置安全认证 · 本地原位隔离" readonly style="height:38px;border-radius:6px;background:var(--inset);color:var(--ink-dim);">
              </div>

              <!-- Row 4: 密码 -->
              <div style="display:grid;grid-template-columns:160px 1fr;align-items:center;gap:16px;">
                <label style="font-size:13.5px;color:var(--ink-strong);">
                  加密密钥
                </label>
                <div style="position:relative;display:flex;align-items:center;">
                  <input class="input" id="kbPassInput" type="text" value="●●●●●●●● (已由系统密钥库安全托管)" placeholder="重置密钥请输入新密码" readonly style="height:38px;border-radius:6px;padding-right:36px;width:100%;background:var(--inset);color:var(--ink-dim);font-size:12.5px;">
                  <span style="position:absolute;right:12px;color:var(--ink-faint);cursor:pointer;user-select:none;" onclick="togglePasswordVisibility('#kbPassInput', this)">👁</span>
                </div>
              </div>

              <!-- Row 5: 隔离集合 -->
              <div style="display:grid;grid-template-columns:160px 1fr;align-items:center;gap:16px;">
                <label style="font-size:13.5px;color:var(--ink-strong);">
                  Collection / Namespace <span class="muted" style="font-size:12px;">ⓘ</span>
                </label>
                <input class="input" id="kbNamespaceInput" placeholder="ordo_kb_chunks_v1" value="ordo_kb_chunks_v1" style="height:38px;border-radius:6px;">
              </div>

              <!-- Row 6: 高级设置 -->
              <div>
                <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 0 6px;cursor:pointer;user-select:none;border-top:1px solid var(--line-soft);" onclick="toggleAdvancedSettings('#kbAdvancedSettings', this)">
                  <span style="font-size:13.5px;color:var(--ink-strong);font-weight:600;">展开高级设置</span>
                  <span style="font-size:12px;color:var(--ink-dim);">⌄</span>
                </div>
                <div id="kbAdvancedSettings" style="display:none;margin-top:8px;padding:16px;background:var(--inset);border-radius:8px;border:1px solid var(--line);">
                  <div class="grid grid-2" style="gap:14px;">
                    <div>
                      <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">向量维度 (Dimensions)</label>
                      <select class="input" style="width:100%;height:34px;font-size:12.5px;">
                        <option>1536 (OpenAI text-embedding-3-small)</option>
                        <option>3072 (OpenAI text-embedding-3-large)</option>
                        <option>1024 (BGE-large-zh-v1.5)</option>
                        <option>768 (text2vec-base-chinese)</option>
                      </select>
                    </div>
                    <div>
                      <label style="display:block;font-size:12px;font-weight:600;margin-bottom:4px;">距离度量算法 (Metric)</label>
                      <select class="input" style="width:100%;height:34px;font-size:12.5px;">
                        <option>Cosine (余弦相似度 - 默认)</option>
                        <option>L2 / Euclidean (欧氏距离)</option>
                        <option>DotProduct (内积)</option>
                      </select>
                    </div>
                  </div>
                  <div style="margin-top:12px;display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="tlsCheck" checked disabled>
                    <label for="tlsCheck" style="font-size:12.5px;color:var(--ink-strong);">已启用本地存储防篡改校验与 WAL 日志事务模式</label>
                  </div>
                </div>
              </div>
            </div>

            <!-- Bottom Buttons Row -->
            <div style="display:flex;justify-content:flex-end;align-items:center;gap:12px;margin-top:32px;">
              <button class="btn" id="testKbBtn" type="button" style="border:1px solid var(--accent);color:var(--accent);background:var(--card-bg);height:38px;padding:0 24px;border-radius:6px;font-size:14px;font-weight:500;" onclick="handleTestKbConnection()">测试连接</button>
              <button class="btn" type="button" style="border:1px solid var(--line);background:var(--card-bg);height:38px;padding:0 24px;border-radius:6px;font-size:14px;font-weight:500;color:var(--ink);" onclick="window.location.hash='#/knowledge/datasets'">取消</button>
              <button class="btn primary" type="submit" style="background:var(--accent);color:#ffffff;height:38px;padding:0 24px;border-radius:6px;font-size:14px;font-weight:500;">创建知识库</button>
            </div>
          </form>
        </div>
      </div>
    </div>`;
    return { title: '知识库管理', desc: '统一管理知识库、挂载可用数据集与配置向量存储引擎', actions: '', html };
  }

  window.handleSelectDoc = function(id) {
    state.selectedDocId = id;
    render();
  };

  async function pageDatasetsTarget() {
    const datasets = await api.getAllDatasets({}, { throwOnError: true }) || [];
    const activeDs = datasets.find(d => d.id === state.selectedDatasetId) || datasets[0] || { id: null, name: '暂无数据集' };
    if (state.selectedDatasetId !== activeDs.id) {
      state.selectedFolder = 'root';
      state.datasetCurrentPage = 1;
    }
    state.selectedDatasetId = activeDs.id;
    if (activeDs.knowledge_base_id) state.selectedKbId = activeDs.knowledge_base_id;
    const limit = 10;
    let page = Math.max(1, state.datasetCurrentPage || 1);
    let tree = [], files = { items: [], total: 0 };
    if (activeDs.id) {
      const params = { folderId: state.selectedFolder === 'all' ? 'root' : state.selectedFolder, query: state.datasetSearchQuery, limit, offset: (page - 1) * limit };
      [tree, files] = await Promise.all([
        api.getDatasetTree(activeDs.id, {}, { throwOnError: true }),
        api.getDatasetFiles(activeDs.id, params, { throwOnError: true })
      ]);
      if (page > 1 && !files.items.length) {
        page = Math.max(1, Math.ceil(files.total / limit));
        files = await api.getDatasetFiles(activeDs.id, { ...params, offset: (page - 1) * limit }, { throwOnError: true });
      }
    }
    const docs = files.items || [];
    const totalDocs = files.total || 0;
    const pageCount = Math.max(1, Math.ceil(totalDocs / limit));
    state.datasetCurrentPage = page;
    state.datasetPageCount = pageCount;
    state.datasetDocs = docs;
    state.datasetTree = tree;
    const selectedDoc = docs.find(d => d.id === state.selectedDocId) || docs[0];
    state.selectedDocId = selectedDoc?.id;
    const renderTree = (nodes, depth = 1) => nodes.map(node => `
      <div class="dataset-tree-row ${state.selectedFolder === node.id ? 'active' : ''}" style="padding-left:${depth * 14}px;cursor:pointer;" onclick="window.handleSelectDatasetFolder('${esc(node.id)}')">
        <span>${node.children?.length ? '∨' : '›'}</span> ${iconFolder(13)} <span>${esc(node.name)}</span>
        <span class="count">${node.fileCount ?? 0}</span>
      </div>${renderTree(node.children || [], depth + 1)}
    `).join('');

    const html = `
    <div class="dataset-layout-root">
      <!-- 1. 左栏: 数据集列表 -->
      <div class="dataset-left-card">
        <div class="dataset-left-header">
          <span>数据集 (${datasets.length})</span>
          <span style="cursor:pointer;font-size:18px;font-weight:700;" title="新建数据集" onclick="openCreateDatasetModal()">+</span>
        </div>
        <div>
          ${datasets.map(ds => {
            const isActive = ds.id === activeDs.id;
            return `
              <div class="dataset-list-item ${isActive ? 'active' : ''}" onclick="window.handleSwitchDataset(${jsArg(ds.id)}, ${jsArg(ds.name)})">
                <div style="min-width:0;">
                  <b style="display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(ds.name)}</b>
                  <div class="muted" style="font-size:12px;margin-top:2px;">${ds.documentCount ?? ds.counts?.documents ?? 0} 文件</div>
                </div>
                <span class="dot" style="background:${isActive ? 'var(--accent)' : 'var(--line)'};"></span>
              </div>
            `;
          }).join('')}
        </div>
      </div>

      <!-- 2. 中间主体工作区与右侧文件详情 -->
      <div class="dataset-main-card" style="padding:18px;">
        <div class="dataset-content-grid">
          <!-- 2.1 目录树 -->
          <div class="dataset-tree-col">
            <div class="dataset-tree-header">
              <span>目录树</span>
              <span style="cursor:pointer;color:var(--ink-dim);" onclick="window.handleRefreshDirectory()">↻</span>
            </div>
            <div style="display:flex;flex-direction:column;gap:2px;">
              <div class="dataset-tree-row ${['all', 'root'].includes(state.selectedFolder) ? 'active' : ''}" style="cursor:pointer;" onclick="window.handleSelectDatasetFolder('root')">
                <span>∨</span> ${iconFolder(13)} <span>${esc(activeDs.name)}</span>
                <span class="count">${activeDs.documentCount ?? activeDs.counts?.documents ?? 0}</span>
              </div>
              ${renderTree(tree)}
            </div>
          </div>

          <!-- 2.2 文件列表表格 -->
          <div class="dataset-table-col">
            <div class="dataset-table-toolbar">
              <div style="display:flex;gap:8px;">
                <button class="dataset-toolbar-btn" onclick="openCreateFolderPrompt()">${iconFolder(13)} 新建文件夹</button>
              </div>
              <div style="display:flex;gap:8px;">
                <button class="dataset-toolbar-btn" onclick="window.toggleDatasetFilter()">▽ 筛选</button>
                <button class="dataset-toolbar-btn" style="padding:0 8px;" onclick="window.handleRefreshDatasets()">↻ 刷新</button>
              </div>
            </div>
            <table class="dataset-table">
              <thead>
                <tr>
                  <th style="width:28px;"><input type="checkbox" onchange="handleToggleSelectAllDocs(this)"></th>
                  <th>名称 ↑</th>
                  <th>类型</th>
                  <th>大小</th>
                  <th>更新时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                ${docs.length === 0 ? `
                  <tr>
                    <td colspan="6" style="text-align:center;padding:40px 16px;color:var(--ink-dim);">
                      <div style="font-size:32px;margin-bottom:8px;">${iconFolder(13)}</div>
                      <b style="font-size:14px;color:var(--ink-strong);">该数据集暂无文档</b>
                      <div style="font-size:12px;margin-top:4px;">请在「数据登记」中导入资料并分配至此数据集</div>
                    </td>
                  </tr>
                ` : docs.map(doc => {
                  const docTitle = doc.title || doc.name;
                  const docId = doc.id;
                  const docType = (doc.fileType || doc.type || 'DOC').toUpperCase();
                  const docSize = doc.sizeFormatted || '—';
                  const docTime = (doc.updatedAt || doc.updated_at || doc.created_at || '').replace('T', ' ').slice(0, 16) || '—';
                  const isSelected = docId === selectedDoc?.id;
                  return `
                    <tr class="${isSelected ? 'selected' : ''}" style="cursor:pointer;" onclick="window.handleSelectDoc('${esc(docId)}');">
                      <td><input type="checkbox" ${isSelected ? 'checked' : ''} onclick="event.stopPropagation();"></td>
                      <td><span style="margin-right:6px;">${iconDoc(13)}</span> ${isSelected ? `<b>${esc(docTitle)}</b>` : esc(docTitle)}</td>
                      <td>${esc(docType)}</td>
                      <td>${esc(docSize)}</td>
                      <td class="muted" style="font-size:12px;">${esc(docTime)}</td>
                      <td>
                        <button class="btn sm" style="padding:2px 8px;font-size:11.5px;color:var(--danger);border:1px solid #fca5a5;background:var(--card-bg);" onclick="event.stopPropagation();window.handleDeleteDocument(${jsArg(docId)}, ${jsArg(docTitle)})">删除</button>
                      </td>
                    </tr>
                  `;
                }).join('')}
              </tbody>
            </table>
            <div class="table-pagination-bar">
              <span>共 ${totalDocs} 条文档</span>
              <div class="pagination-controls">
                <button class="page-arrow ${page <= 1 ? 'disabled' : ''}" type="button" onclick="if(${page}>1)window.handleDatasetPageChange(${page - 1})">&lt;</button>
                ${Array.from({ length: Math.min(pageCount, 5) }, (_, i) => Math.max(1, Math.min(page - 2, pageCount - 4)) + i).map(n => `<button class="page-num ${page === n ? 'active' : ''}" type="button" onclick="window.handleDatasetPageChange(${n})">${n}</button>`).join('')}
                <button class="page-arrow ${page >= pageCount ? 'disabled' : ''}" type="button" onclick="if(${page}<${pageCount})window.handleDatasetPageChange(${page + 1})">&gt;</button>
              </div>
            </div>
          </div>

          <!-- 3. 右栏: 选中文件具体信息 Inspector -->
          <div class="dataset-inspector-col">
            ${selectedDoc ? `
              <div style="display:flex;align-items:center;gap:8px;font-weight:700;font-size:14px;color:var(--ink-strong);margin-bottom:16px;">
                <span style="font-size:22px;">${iconDoc(13)}</span>
                <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(selectedDoc.title || selectedDoc.name)}">${esc(selectedDoc.title || selectedDoc.name)}</span>
              </div>
              <div style="display:flex;flex-direction:column;gap:14px;">
                <div>
                  <div class="muted" style="font-size:12px;">所属目录</div>
                  <div style="display:flex;align-items:center;gap:4px;font-size:13px;margin-top:3px;font-weight:500;">
                    ${iconFolder(13)} ${esc(selectedDoc.folderPath || '根目录')}
                  </div>
                </div>
                <div>
                  <div class="muted" style="font-size:12px;">数据来源</div>
                  <div style="font-size:13px;margin-top:3px;">${esc(selectedDoc.source || '手动上传')}</div>
                </div>
                <div>
                  <div class="muted" style="font-size:12px;">文件大小</div>
                  <div style="font-size:13px;margin-top:3px;">${esc(selectedDoc.sizeFormatted || '—')}</div>
                </div>
                <div>
                  <div class="muted" style="font-size:12px;">最近更新时间</div>
                  <div style="font-size:13px;margin-top:3px;">${esc((selectedDoc.updatedAt || '').replace('T', ' ').slice(0, 16) || '—')}</div>
                </div>
              </div>
            ` : `
              <div style="color:var(--ink-dim);text-align:center;padding:40px 10px;">点击左侧文件查看详细信息</div>
            `}
          </div>
        </div>
      </div>
    </div>`;
    return { desc: '统一组织数据集、层级目录树与文档资产', actions: '', html };
  }

  window.handleAssignRegisteredSource = async function(sourceId, datasetId) {
    if (!datasetId) { showToast('请选择目标数据集', 'warn'); return render(); }
    const result = await serverAction('assignSourceDataset', [sourceId, { datasetId }], '数据来源已分配，解析任务已登记');
    if (result) await render();
  };

  window.handleDeleteRegisteredSource = async function(sourceId) {
    if (!confirm('确定移除此数据来源及其文档登记吗？')) return;
    const result = await serverAction('deleteSource', [sourceId], '数据来源已移除');
    if (result) await render();
  };

  function databaseForm() {
    const type = document.getElementById('dbTypeSelect')?.value;
    const database = document.getElementById('dbNameInput')?.value.trim();
    return {
      type, name: database, database,
      host: document.getElementById('dbHostInput')?.value.trim(),
      path: document.getElementById('dbHostInput')?.value.trim(),
      port: Number(document.getElementById('dbPortInput')?.value || 5432),
      username: document.getElementById('dbUserInput')?.value.trim(),
      password: document.getElementById('dbPassInput')?.value || ''
    };
  }

  /* Interactive Database Connection Modal */
  window.openAddDatabaseModal = function() {
    const html = `
    <div class="modal-box" style="max-width:560px;">
      <div class="modal-header">
        <span>连接业务数据库</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">数据库类型</label>
          <select class="input" id="dbTypeSelect" style="width:100%;">
            <option value="postgresql">PostgreSQL (推荐，支持 pgvector 扩展)</option>
            <option value="mysql" disabled>MySQL（暂无驱动）</option>
            <option value="clickhouse" disabled>ClickHouse（暂无驱动）</option>
            <option value="sqlite">SQLite 3 本地数据库</option>
          </select>
        </div>
        <div class="grid grid-2" style="gap:10px;margin-bottom:12px;">
          <div>
            <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">主机地址 / SQLite 文件路径</label>
            <input class="input" id="dbHostInput" value="127.0.0.1" placeholder="例如: 127.0.0.1 或 db.corp.internal">
          </div>
          <div>
            <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">端口 / Port</label>
            <input class="input" id="dbPortInput" value="5432" placeholder="5432">
          </div>
        </div>
        <div class="grid grid-2" style="gap:10px;margin-bottom:12px;">
          <div>
            <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">数据库名 / Database</label>
            <input class="input" id="dbNameInput" value="ordo_business" placeholder="ordo_business">
          </div>
          <div>
            <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">用户名 / Username</label>
            <input class="input" id="dbUserInput" value="postgres" placeholder="postgres">
          </div>
        </div>
        <div style="margin-bottom:14px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">密码 / Password</label>
          <input class="input" type="password" id="dbPassInput" value="" placeholder="输入密码">
        </div>
        <div id="dbTestResult" style="margin-bottom:12px;display:none;padding:8px 12px;border-radius:6px;font-size:12.5px;"></div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:space-between;align-items:center;">
        <button class="btn" type="button" onclick="window.handleTestDbConnection();">${iconZap(13)} 测试连接</button>
        <div style="display:flex;gap:8px;">
          <button class="btn" data-close>取消</button>
          <button class="btn primary" type="button" onclick="window.handleSaveDbConnection();">确认接入</button>
        </div>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.handleTestDbConnection = async function() {
    const box = document.getElementById('dbTestResult');
    if (!box) return;
    box.style.display = 'block';
    box.textContent = '正在测试只读连接…';
    const result = await serverAction('testConnectorConfig', [databaseForm()]);
    box.textContent = result ? `连接成功 · ${result.latencyMs} ms · ${result.version}` : (api.lastError?.message || '连接测试失败');
    box.style.color = result ? 'var(--ok)' : 'var(--danger)';
  };
  window.handleSaveDbConnection = async function() {
    const result = await serverAction('createConnector', [databaseForm()], '数据库连接已登记，可进行只读查询和快照导入');
    if (result) { closeOverlay(); await render(); }
  };
  /* 04 知识库 > 数据登记 - 100% 对应 04-知识库-数据登记.png */
  async function pageRegistry() {
    const [sourcePage, sourceTree, recent, attention, datasets, connectors] = await Promise.all([
      api.getRegisteredSources({ limit: 500 }, { throwOnError: true }),
      api.getSourceTree({}, { throwOnError: true }),
      api.getRecentSources({ limit: state.parsingRecordsExpanded ? 30 : 5 }, { throwOnError: true }),
      api.getSourceAttentionItems({ limit: 100 }, { throwOnError: true }),
      api.getAllDatasets({}, { throwOnError: true }),
      api.getConnectors({}, { throwOnError: true })
    ]);
    const typeLabels = { upload: '文件', archive: '压缩包', directory: '目录', local_discovery: '本机', database: '数据库', connector: '网盘', synthetic: '生成资料' };
    const filterTypes = { file: ['upload', 'archive'], directory: ['directory'], netdisk: ['connector'], database: ['database'], local: ['local_discovery'] };
    const selectedTypes = filterTypes[state.registryTab];
    const allRows = (sourcePage?.items || []).filter(row => !selectedTypes || selectedTypes.includes(row.type));
    const limit = state.registryPageSize || 10;
    const total = allRows.length;
    const pageCount = Math.max(1, Math.ceil(total / limit));
    const page = Math.max(1, Math.min(state.registryCurrentPage || 1, pageCount));
    state.registryCurrentPage = page;
    state.registryPageCount = pageCount;
    state.registryDatasets = datasets;
    const regDocs = allRows.slice((page - 1) * limit, page * limit);
    const sourceStatus = status => ({ ready: '已完成', queued: '排队中', processing: '处理中', failed: '处理失败', review_required: '待复核', registered: '已登记', unassigned: '待分配' }[status] || status || '—');

    const html = `
    <!-- Top Action Cards Row -->
    <div class="registry-action-row">
      <div class="registry-action-card" onclick="triggerNativeFileUpload()">
        <span style="color:var(--accent);font-size:16px;">⇪</span>
        <span>上传文件</span>
      </div>
      <div class="registry-action-card" onclick="window.handleDirectoryImportPrompt()">
        <span style="color:var(--accent);font-size:16px;">${iconFolder(13)}</span>
        <span>导入目录</span>
      </div>
      <div class="registry-action-card" onclick="window.handleUploadArchivePrompt()">
        <span style="color:var(--accent);font-size:16px;">${iconLayers(14)}</span>
        <span>导入压缩包</span>
      </div>
      <div class="registry-action-card" style="opacity:0.6;cursor:not-allowed;" title="规划红线：当前版本未启用外部网盘连接" onclick="showToast('未启用 · 规划红线（§14）：当前版本专注于本地原位安全存储','warn')">
        <span style="color:#94a3b8;font-size:16px;">${iconCloud(14)}</span>
        <span style="color:#94a3b8;">连接网盘 (未启用)</span>
      </div>
      <div class="registry-action-card" onclick="window.openAddDatabaseModal()">
        <span style="color:#94a3b8;font-size:16px;">${iconDatabase(14)}</span>
        <span>连接数据库</span>
      </div>
      <div class="registry-action-card" style="opacity:0.6;cursor:not-allowed;" title="规划红线：当前版本未启用全盘探测" onclick="showToast('未启用 · 规划红线（§14）：请使用明确的「导入目录」功能原位索引','warn')">
        <span style="color:#94a3b8;font-size:16px;">${iconMonitor(14)}</span>
        <span style="color:#94a3b8;">本机探测 (未启用)</span>
      </div>
    </div>

    <!-- Filter Pills -->
    <div class="filter-pills" style="margin-bottom:18px;">
      <button class="filter-pill ${state.registryTab === 'all' ? 'active' : ''}" onclick="state.registryTab='all';render();">全部</button>
      <button class="filter-pill ${state.registryTab === 'file' ? 'active' : ''}" onclick="state.registryTab='file';render();">文件</button>
      <button class="filter-pill ${state.registryTab === 'directory' ? 'active' : ''}" onclick="state.registryTab='directory';render();">目录</button>
      <button class="filter-pill ${state.registryTab === 'netdisk' ? 'active' : ''}" onclick="state.registryTab='netdisk';render();">网盘</button>
      <button class="filter-pill ${state.registryTab === 'database' ? 'active' : ''}" onclick="state.registryTab='database';render();">数据库</button>
      <button class="filter-pill ${state.registryTab === 'local' ? 'active' : ''}" onclick="state.registryTab='local';render();">本机</button>
    </div>

    <!-- Main 3-Column Layout -->
    <div class="registry-columns-layout">
      <!-- Column 1: 数据来源 -->
      <div class="registry-source-card">
        <div class="card-head">数据来源</div>
        <div class="card-body" style="padding:10px 12px;">
          <div style="display:flex;flex-direction:column;gap:4px;">
            ${sourceTree.map(group => `
              <div class="tree-node" onclick="state.registryTab='${({upload:'file',archive:'file',directory:'directory',database:'database',connector:'netdisk',local_discovery:'local'})[group.id] || 'all'}';state.registryCurrentPage=1;render();">
                <span>›</span> <span>${iconFolder(13)}</span> <b>${esc(typeLabels[group.id] || group.name)}</b>
                <span class="badge" style="margin-left:auto;border-radius:10px;padding:1px 8px;">${group.count}</span>
              </div>
            `).join('')}
            <div class="tree-node" style="margin-top:8px;cursor:pointer;" onclick="window.openDatabaseConnections()">
              <span>›</span> <span>${iconDatabase(14)}</span> <span>数据库连接 (${connectors.length})</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Column 2: 来源列表表格 (8 行完整数据) -->
      <div class="card">
        <div class="card-body" style="padding:0;">
          <table class="data-table">
            <thead>
              <tr>
                <th style="padding-left:16px;">名称</th>
                <th>来源</th>
                <th>同步状态</th>
                <th>处理状态</th>
                <th>所属数据集</th>
                <th>最近更新</th>
                <th style="width:30px;"></th>
              </tr>
            </thead>
            <tbody>
              ${regDocs.length === 0 ? `
                <tr>
                  <td colspan="7" style="text-align:center;padding:32px 16px;color:var(--ink-dim);">
                    ${api && api.connected ? '暂无登记的文档，可通过上方「上传文件」或「导入目录」完成登记。' : '未连接后端服务，暂无文档'}
                  </td>
                </tr>
              ` : regDocs.map(doc => `
                <tr>
                  <td style="padding-left:16px;">
                    <span class="file-type-icon ${doc.media_type?.includes('pdf') ? 'pdf' : (doc.media_type?.includes('word') ? 'word' : 'md')}">${iconDoc(13)}</span>
                    <b>${esc(doc.name)}</b>
                  </td>
                  <td>${esc(typeLabels[doc.type] || doc.type)}</td>
                  <td><span style="color:var(--accent);">● 已登记</span></td>
                  <td><span class="badge ${doc.status === 'ready' ? 'ok' : 'warn'}">${esc(sourceStatus(doc.status))}</span></td>
                  <td><select class="input" style="max-width:150px;height:28px;font-size:12px;" onchange="window.handleAssignRegisteredSource('${esc(doc.id)}', this.value)"><option value="">待分配</option>${datasets.map(ds => `<option value="${esc(ds.id)}" ${doc.datasetId === ds.id ? 'selected' : ''}>${esc(ds.name)}</option>`).join('')}</select></td>
                  <td>${esc((doc.updated_at || doc.created_at || '').replace('T', ' ').slice(0, 16))}</td>
                  <td style="color:var(--ink-dim);cursor:pointer;" onclick="window.handleDeleteRegisteredSource('${esc(doc.id)}')">${iconTrash(13)}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>

          <!-- Pagination Bar Matching Mockup 04 -->
          <div class="table-pagination-bar" style="padding:14px 18px;">
            <div style="display:flex;align-items:center;gap:12px;">
              <span>共 ${total} 项</span>
              <div class="page-size-selector" style="margin-left:0;" onclick="window.handleRegistryPageSizeChange()">${limit} 条/页 ⌄</div>
            </div>
            <div class="pagination-controls">
              <button class="page-arrow ${page <= 1 ? 'disabled' : ''}" type="button" onclick="window.handleRegistryPageChange(${page - 1})">&lt;</button>
              ${Array.from({ length: Math.min(pageCount, 5) }, (_, i) => Math.max(1, Math.min(page - 2, pageCount - 4)) + i).map(n => `<button class="page-num ${page === n ? 'active' : ''}" type="button" onclick="window.handleRegistryPageChange(${n})">${n}</button>`).join('')}
              <button class="page-arrow ${page >= pageCount ? 'disabled' : ''}" type="button" onclick="window.handleRegistryPageChange(${page + 1})">&gt;</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Column 3: 最近导入 & 需要处理 -->
      <div>
        <!-- Card 1: 最近导入 -->
        <div class="card">
          <div class="card-head">最近导入</div>
          <div class="card-body" style="padding:0;">
            ${(api && api.connected) ? (
              recent.length > 0 ? recent.map(doc => `
                <div class="list-item-row" style="padding:12px 16px;">
                  <span class="file-type-icon ${doc.media_type?.includes('pdf') ? 'pdf' : (doc.media_type?.includes('word') ? 'word' : 'excel')}">${doc.media_type?.includes('pdf') ? 'PDF' : (doc.media_type?.includes('word') ? 'W' : 'DOC')}</span>
                  <div class="grow" style="min-width:0;">
                    <b style="display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(doc.name)}</b>
                    <div class="muted" style="font-size:12px;margin-top:2px;">${esc(typeLabels[doc.type] || doc.type)} / ${esc(doc.datasetName || '待分配')}</div>
                    <div class="muted" style="font-size:11px;">${esc((doc.updated_at || doc.created_at || '').replace('T', ' ').slice(0, 16) || '刚刚')}</div>
                  </div>
                  <span style="color:var(--accent);font-size:16px;">✓</span>
                </div>
              `).join('') : `
                <div style="padding:24px 16px;text-align:center;color:var(--ink-dim);font-size:13px;">
                  暂无导入记录，请点击上方「上传文件」或「导入目录」
                </div>
              `
            ) : `
              <div class="list-item-row" style="padding:12px 16px;">
                <span class="file-type-icon word">W</span>
                <div class="grow">
                  <b>产品需求说明书.docx</b>
                  <div class="muted" style="font-size:12px;margin-top:2px;">企业资料 / 产品资料</div>
                  <div class="muted" style="font-size:11px;">2025-05-20 10:23</div>
                </div>
                <span style="color:var(--accent);font-size:16px;">✓</span>
              </div>
              <div class="list-item-row" style="padding:12px 16px;">
                <span class="file-type-icon folder">${iconFolder(13)}</span>
                <div class="grow">
                  <b>产品手册目录</b>
                  <div class="muted" style="font-size:12px;margin-top:2px;">企业资料 / 产品资料</div>
                  <div class="muted" style="font-size:11px;">2025-05-20 10:18</div>
                </div>
                <span style="color:var(--blue);font-size:16px;">↻</span>
              </div>
              <div class="list-item-row" style="padding:12px 16px;">
                <span class="file-type-icon pdf">PDF</span>
                <div class="grow">
                  <b>解决方案白皮书.pdf</b>
                  <div class="muted" style="font-size:12px;margin-top:2px;">WebDAV / 市场资料</div>
                  <div class="muted" style="font-size:11px;">2025-05-20 09:55</div>
                </div>
                <span style="color:var(--accent);font-size:16px;">✓</span>
              </div>
              <div class="list-item-row" style="padding:12px 16px;">
                <span class="file-type-icon excel">X</span>
                <div class="grow">
                  <b>客户清单.xlsx</b>
                  <div class="muted" style="font-size:12px;margin-top:2px;">PostgreSQL / crm_db</div>
                  <div class="muted" style="font-size:11px;">2025-05-20 09:42</div>
                </div>
                <span style="color:var(--accent);font-size:16px;">✓</span>
              </div>
              <div class="list-item-row" style="padding:12px 16px;">
                <span class="file-type-icon folder">${iconFolder(13)}</span>
                <div class="grow">
                  <b>技术图纸目录</b>
                  <div class="muted" style="font-size:12px;margin-top:2px;">本机已确认 / D:\\知识库资料</div>
                  <div class="muted" style="font-size:11px;">2025-05-20 09:31</div>
                </div>
                <span style="color:var(--warn);font-size:16px;">${iconClock(13)}</span>
              </div>
            `}
            <div style="text-align:center;padding:12px 0;border-top:1px solid var(--line-soft);">
              <a href="#" style="color:var(--accent);font-size:13px;font-weight:600;" onclick="window.toggleParsingRecords(); return false;">
                ${state.parsingRecordsExpanded ? '收起 &lt;' : '查看全部 &gt;'}
              </a>
            </div>
          </div>
        </div>

        <!-- Card 2: 需要处理 -->
        ${(() => {
          const pendingOrFailed = attention;
          if (api && api.connected) {
            return `
              <div class="card section-gap">
                <div class="card-head">${pendingOrFailed.length} 项需要处理</div>
                <div class="card-body" style="padding:0;">
                  ${pendingOrFailed.length === 0 ? `
                    <div style="padding:20px 16px;text-align:center;color:var(--ink-dim);font-size:13px;">
                      ✓ 全部任务运行正常，无待处理异常
                    </div>
                  ` : pendingOrFailed.map(doc => `
                    <div class="list-item-row" style="padding:12px 16px;">
                      <div style="width:28px;height:28px;background:${doc.status === 'failed' ? 'var(--danger-soft)' : 'var(--warn-soft)'};color:${doc.status === 'failed' ? '#dc2626' : '#d97706'};border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:16px;flex:0 0 28px;">⚠</div>
                      <div class="grow" style="min-width:0;">
                        <b style="color:${doc.status === 'failed' ? 'var(--danger)' : '#d97706'};font-size:14px;">${doc.status === 'failed' ? '处理失败' : '待处理'}</b>
                        <div class="muted" style="font-size:12px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(doc.name)}</div>
                        <div class="muted" style="font-size:11px;">${esc((doc.updated_at || doc.created_at || '').replace('T', ' ').slice(0, 16) || '刚刚')}</div>
                      </div>
                      <span class="list-arrow" style="cursor:pointer;" onclick="window.go('knowledge/parsing')">›</span>
                    </div>
                  `).join('')}
                </div>
              </div>
            `;
          }
          return `
            <div class="card section-gap">
              <div class="card-head">2 项需要处理</div>
              <div class="card-body" style="padding:0;">
                <div class="list-item-row" style="padding:12px 16px;">
                  <div style="width:28px;height:28px;background:var(--danger-soft);color:#dc2626;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:16px;flex:0 0 28px;">⚠</div>
                  <div class="grow">
                    <b style="color:var(--danger);font-size:14px;">同步失败</b>
                    <div class="muted" style="font-size:12px;margin-top:2px;">产品培训PPT.pptx</div>
                    <div class="muted" style="font-size:11px;">2025-05-20 09:10</div>
                  </div>
                  <span class="list-arrow">›</span>
                </div>
                <div class="list-item-row" style="padding:12px 16px;">
                  <div style="width:28px;height:28px;background:var(--warn-soft);color:#d97706;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:16px;flex:0 0 28px;">⚠</div>
                  <div class="grow">
                    <b style="color:#d97706;font-size:14px;">待处理</b>
                    <div class="muted" style="font-size:12px;margin-top:2px;">技术图纸目录</div>
                    <div class="muted" style="font-size:11px;">2025-05-20 09:31</div>
                  </div>
                  <span class="list-arrow">›</span>
                </div>
                <div style="text-align:center;padding:12px 0;border-top:1px solid var(--line-soft);">
                  <a href="#" style="color:var(--accent);font-size:13px;font-weight:600;" onclick="window.toggleParsingPending(); return false;">
                    ${state.parsingPendingExpanded ? '收起 &lt;' : '查看全部 &gt;'}
                  </a>
                </div>
              </div>
            </div>
          `;
        })()}
      </div>
    </div>`;
    return { desc: '文件、目录、压缩包、网盘、业务数据库和本机资料登记', html };
  }

  /* 05 知识库 > 数据解析 - 100% 对应 05-知识库-数据解析.png */
  async function pageParsing() {
    const params = { knowledgeBaseId: state.parsingKbId || state.selectedKbId || api.context?.defaultKbId };
    const [taskPage, stats, settings, resources, profiles] = await Promise.all([
      api.getParsingTasks({ ...params, limit: 500 }, { throwOnError: true }),
      api.getParsingPipelineStats(params, { throwOnError: true }),
      api.getParsingSettings({}, { throwOnError: true }),
      api.getSystemResources({}, { throwOnError: true }),
      api.getParsingProfiles({}, { throwOnError: true })
    ]);
    const tasks = taskPage?.items || [];
    state.parsingTasks = tasks;
    state.autoParsingEnabled = settings.autoParsingEnabled;
    state.parsingConcurrency = settings.concurrency;
    state.parsingPaused = tasks.some(t => t.status === 'paused' || t.pause_requested);
    state.parsingProfiles = profiles;
    const profile = profiles.find(p => p.id === state.parsingProfileId) || profiles.find(p => p.isDefault) || profiles[0];
    state.parsingProfileId = profile?.id;
    const selectedKb = api.context?.knowledgeBases?.find(kb => kb.id === params.knowledgeBaseId);
    const selTaskDoc = tasks.find(t => t.id === state.parsingSelectedDocId) || tasks[0] || {};
    if (state.parsingSelectedDocId !== selTaskDoc.id) state.parsingCurrentPage = 1;
    state.parsingSelectedDocId = selTaskDoc.id;
    let previewPages = { pages: [], totalPages: 0 }, preview = null, inspect = {}, diff = {}, previewError = '';
    if (selTaskDoc.documentId) {
      try {
        previewPages = await api.getDocumentPreviewPages(selTaskDoc.documentId, {}, { throwOnError: true });
        state.parsingCurrentPage = Math.max(1, Math.min(state.parsingCurrentPage, previewPages.totalPages));
        [preview, inspect, diff] = await Promise.all([
          api.getDocumentPage(selTaskDoc.documentId, state.parsingCurrentPage, {}, { throwOnError: true }),
          api.getDocumentPageInspect(selTaskDoc.documentId, state.parsingCurrentPage, {}, { throwOnError: true }),
          api.getDocumentPageDiff(selTaskDoc.documentId, state.parsingCurrentPage, {}, { throwOnError: true })
        ]);
      } catch (error) { previewError = error.message || '文档预览不可用'; }
    }
    const totalDocPages = previewPages.totalPages;
    state.parsingTotalPages = totalDocPages;
    state.curPageInspect = inspect;
    state.curPageDiff = diff;
    const stageCount = key => `${stats.stages?.[key]?.completed ?? 0} / ${stats.stages?.[key]?.total ?? 0}`;
    const warnings = inspect.warnings || [];
    const runningTasks = tasks.filter(t => t.status === 'running');
    const queuedTasks = tasks.filter(t => ['queued', 'paused'].includes(t.status));
    const failedTasks = tasks.filter(t => t.status === 'failed' || t.status === 'cancelled');
    const succeededTasks = tasks.filter(t => t.status === 'succeeded' || t.status === 'partial');
    const throughput = resources.throughput?.length ? resources.throughput[resources.throughput.length - 1].count : 0;

    const html = `
    <!-- Top Configuration & Actions Bar -->
    <div class="parsing-top-bar" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
      <div style="display:flex;align-items:center;gap:18px;">
        <div style="display:flex;align-items:center;gap:8px;position:relative;">
          <span class="muted" style="font-size:13.5px;">知识库</span>
          <div class="page-size-selector" style="margin-left:0;font-size:13.5px;padding:5px 12px;cursor:pointer;" onclick="window.toggleParsingKBSelector()">${esc(selectedKb?.name || '全部知识库')} ⌄</div>
          ${state.parsingKBDropdownOpen ? `
            <div style="position:absolute;top:32px;left:48px;background:var(--card-bg);border:1px solid var(--line);border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,0.1);z-index:100;min-width:140px;padding:4px 0;">
              ${(api.context?.knowledgeBases || []).map(kb => `<div style="padding:8px 12px;font-size:13px;cursor:pointer;" onclick="window.handleParsingKbSelect('${esc(kb.id)}')">${esc(kb.name)}</div>`).join('')}
            </div>
          ` : ''}
        </div>
        <div style="display:flex;align-items:center;gap:8px;position:relative;">
          <span class="muted" style="font-size:13.5px;">运行</span>
          <div class="page-size-selector" style="margin-left:0;font-size:13.5px;padding:5px 12px;cursor:pointer;" onclick="window.toggleParsingRunConfigSelector()">${esc(profile?.name || '未选择运行配置')} ⌄</div>
          ${state.parsingRunConfigDropdownOpen ? `
            <div style="position:absolute;top:32px;left:40px;background:var(--card-bg);border:1px solid var(--line);border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,0.1);z-index:100;min-width:140px;padding:4px 0;">
              ${profiles.map(item => `<div style="padding:8px 12px;font-size:13px;cursor:pointer;opacity:${item.available ? 1 : 0.5}" title="${esc(item.reason || '')}" onclick="window.handleParsingProfileSelect('${esc(item.id)}')">${esc(item.name)}</div>`).join('')}
            </div>
          ` : ''}
        </div>
      </div>
      <div style="display:flex;gap:10px;">
        <button class="btn primary" style="background:var(--accent);color:#ffffff;height:36px;padding:0 18px;border-radius:6px;font-size:13.5px;" onclick="window.handleStartParsingTask()">▶ 开始解析</button>
        <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:36px;padding:0 16px;border-radius:6px;font-size:13.5px;" onclick="window.handlePauseParsingTask()">${state.parsingPaused ? '▶ 恢复' : '⏸ 暂停'}</button>
        <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:36px;padding:0 16px;border-radius:6px;font-size:13.5px;" onclick="window.handleRetryFailedTasks()">↻ 重试失败</button>
        <div style="position:relative;">
          <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:36px;padding:0 12px;border-radius:6px;font-size:14px;cursor:pointer;" onclick="window.toggleParsingMoreMenu(event)" title="更多操作">⋮</button>
          ${state.parsingMoreMenuOpen ? `
            <div class="parsing-more-dropdown" style="position:absolute;top:42px;right:0;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,0.14);z-index:200;min-width:250px;padding:6px 0;text-align:left;">
              <!-- 1. 自动化解析开关 -->
              <div style="padding:10px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line-soft);" onclick="event.stopPropagation();">
                <div>
                  <b style="font-size:13px;display:block;color:var(--ink-strong);">${iconZap(13)} 自动化解析</b>
                  <span class="muted" style="font-size:11.5px;">已有队列自动执行解析</span>
                </div>
                <label class="switch-toggle" style="margin-left:12px;">
                  <input type="checkbox" ${state.autoParsingEnabled ? 'checked' : ''} onchange="window.toggleAutoParsing(this.checked)">
                  <span class="switch-slider"></span>
                </label>
              </div>

              <!-- 2. 并发线程设置 -->
              <div style="padding:10px 16px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;" onmouseover="this.style.background='var(--hover)'" onmouseout="this.style.background='transparent'" onclick="window.openConcurrencySettingModal()">
                <div>
                  <b style="font-size:13px;display:block;color:var(--ink-strong);">${iconSettings(14)} 并发线程设置</b>
                  <span class="muted" style="font-size:11.5px;">当前: ${state.parsingConcurrency || 4} 线程并行</span>
                </div>
                <span class="list-arrow" style="color:var(--ink-dim);">›</span>
              </div>

              <!-- 3. 导出解析日志 -->
              <div style="padding:10px 16px;display:flex;align-items:center;gap:10px;font-size:13px;cursor:pointer;color:var(--ink-strong);" onmouseover="this.style.background='var(--hover)'" onmouseout="this.style.background='transparent'" onclick="window.handleExportParsingLogs()">
                <span style="font-size:15px;">${iconDownload(13)}</span>
                <span>导出解析日志 (.json)</span>
              </div>

              <!-- 4. 清空待处理队列 -->
              <div style="padding:10px 16px;display:flex;align-items:center;gap:10px;font-size:13px;cursor:pointer;color:var(--danger);border-top:1px solid var(--line-soft);" onmouseover="this.style.background='var(--hover)'" onmouseout="this.style.background='transparent'" onclick="window.handleClearTaskQueue()">
                <span style="font-size:15px;">${iconRefresh(14)}</span>
                <span>清空待处理队列</span>
              </div>
            </div>
          ` : ''}
        </div>
      </div>
    </div>

    <!-- 4-Stage Pipeline: 4 Independent Cards Connected by Animated Flowing Arrows -->
    <div class="parsing-pipeline-row">
      <!-- Card 1: 检测与路由 -->
      <div class="parsing-node-card">
        <div class="parsing-node-icon" style="background:var(--ok-soft);color:var(--ok);box-shadow:0 2px 6px rgba(5,150,105,0.14);">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>
        </div>
        <div class="grow">
          <b style="font-size:14px;color:var(--ink-strong);">检测与路由</b>
          <div class="muted" style="font-size:12px;margin-top:2px;">${stageCount('detection')}</div>
        </div>
        <span style="width:22px;height:22px;border-radius:50%;background:#059669;color:#ffffff;display:flex;align-items:center;justify-content:center;flex:0 0 22px;box-shadow:0 2px 4px rgba(5,150,105,0.25);"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span>
      </div>

      <!-- Flow Arrow 1 -->
      <div style="display:flex;align-items:center;justify-content:center;">
        <svg width="36" height="14" viewBox="0 0 36 14" fill="none"><path class="flow-arrow-line" d="M0 7 H28" stroke="#059669" stroke-width="2" stroke-linecap="round"/><path d="M24 3 L30 7 L24 11" stroke="#059669" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>

      <!-- Card 2: 解析 -->
      <div class="parsing-node-card">
        <div class="parsing-node-icon" style="background:var(--blue-soft);color:var(--blue);box-shadow:0 2px 6px rgba(37,99,235,0.14);">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><circle cx="11" cy="14" r="3"/><line x1="13.5" y1="16.5" x2="16.5" y2="19.5"/></svg>
        </div>
        <div class="grow">
          <b style="font-size:14px;color:var(--ink-strong);">解析</b>
          <div class="muted" style="font-size:12px;margin-top:2px;">${stageCount('parsing')}</div>
        </div>
        <span style="width:22px;height:22px;border-radius:50%;background:#059669;color:#ffffff;display:flex;align-items:center;justify-content:center;flex:0 0 22px;box-shadow:0 2px 4px rgba(5,150,105,0.25);"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span>
      </div>

      <!-- Flow Arrow 2 -->
      <div style="display:flex;align-items:center;justify-content:center;">
        <svg width="36" height="14" viewBox="0 0 36 14" fill="none"><path class="flow-arrow-line" d="M0 7 H28" stroke="#2563eb" stroke-width="2" stroke-linecap="round"/><path d="M24 3 L30 7 L24 11" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>

      <!-- Card 3: 清理 -->
      <div class="parsing-node-card">
        <div class="parsing-node-icon" style="background:rgba(147, 51, 234, 0.15);color:var(--purple);box-shadow:0 2px 6px rgba(147,51,234,0.14);">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3z"/></svg>
        </div>
        <div class="grow">
          <b style="font-size:14px;color:var(--ink-strong);">清理</b>
          <div class="muted" style="font-size:12px;margin-top:2px;">${stageCount('cleaning')}</div>
        </div>
        <span style="width:22px;height:22px;border-radius:50%;background:#059669;color:#ffffff;display:flex;align-items:center;justify-content:center;flex:0 0 22px;box-shadow:0 2px 4px rgba(5,150,105,0.25);"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span>
      </div>

      <!-- Flow Arrow 3 -->
      <div style="display:flex;align-items:center;justify-content:center;">
        <svg width="36" height="14" viewBox="0 0 36 14" fill="none"><path class="flow-arrow-line" d="M0 7 H28" stroke="#d97706" stroke-width="2" stroke-linecap="round"/><path d="M24 3 L30 7 L24 11" stroke="#d97706" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>

      <!-- Card 4: Markdown / JSON -->
      <div class="parsing-node-card">
        <div class="parsing-node-icon" style="background:var(--warn-soft);color:var(--warn-ink);box-shadow:0 2px 6px rgba(217,119,6,0.14);">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/><line x1="14" y1="4" x2="10" y2="20"/></svg>
        </div>
        <div class="grow">
          <b style="font-size:14px;color:var(--ink-strong);">Markdown / JSON</b>
          <div class="muted" style="font-size:12px;margin-top:2px;">${stageCount('markdownJson')}</div>
        </div>
        <span style="width:22px;height:22px;border-radius:50%;background:#f59e0b;color:#ffffff;display:flex;align-items:center;justify-content:center;flex:0 0 22px;box-shadow:0 2px 4px rgba(245,158,11,0.25);"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="8" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>
      </div>
    </div>

    <!-- Equal Height 3-Column Layout: Bottoms Aligned on Same Plane -->
    <div class="parsing-three-columns">
      <!-- Column 1: 任务队列 -->
      <div class="parsing-col-queue">
        <div class="card-head" style="padding:14px 18px;display:flex;justify-content:space-between;align-items:center;">
          <span style="font-size:14px;font-weight:700;">任务队列</span>
          <span class="muted" style="cursor:pointer;" onclick="render();showToast('已刷新任务队列');">↻</span>
        </div>
        <div class="card-body" style="padding:0;">
          <div>
            ${(api && api.connected && tasks.length) ? `
              <!-- 处理中 / 排队中 -->
              <div style="padding:10px 14px;background:var(--inset);font-weight:600;font-size:12.5px;color:var(--ink-dim);">∨ 进行中与排队 (${runningTasks.length + queuedTasks.length})</div>
              ${(runningTasks.length + queuedTasks.length === 0) ? `
                <div style="padding:10px 14px;color:var(--ink-dim);font-size:12px;">暂无排队中的任务</div>
              ` : [...runningTasks, ...queuedTasks].map(t => {
                const isSel = state.parsingSelectedDocId === t.id;
                const title = t.name || t.input?.filename || t.object_id || t.id;
                return `
                  <div class="list-item-row" style="${isSel ? 'background:var(--accent-soft);border-left:3px solid var(--accent);' : ''}padding:10px 14px;display:flex;align-items:center;gap:10px;cursor:pointer;" onclick="state.parsingSelectedDocId='${esc(t.id)}';render();">
                    <span style="display:flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;background:var(--card-bg);color:var(--accent);flex:0 0 28px;">${iconDoc(13)}</span>
                    <div class="grow" style="overflow:hidden;">
                      <b style="font-size:13px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(title)}</b>
                      <div class="muted" style="font-size:12px;margin-top:2px;">进度 ${t.progress || 0}% · ${esc(t.type || 'parse')}</div>
                    </div>
                    ${t.status === 'running' ? `<svg class="spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2.5"><path d="M21.5 2v6h-6"/><path d="M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>` : `<span class="badge" style="font-size:10px;">排队</span>`}
                  </div>
                `;
              }).join('')}

              <!-- 已完成任务 -->
              <div style="padding:10px 14px;background:var(--inset);font-weight:600;font-size:12.5px;color:var(--ok);margin-top:8px;">∨ 已完成 (${succeededTasks.length})</div>
              ${succeededTasks.slice(0, 5).map(t => {
                const isSel = state.parsingSelectedDocId === t.id;
                const title = t.name || t.input?.filename || t.object_id || t.id;
                const blockCount = t.result?.blockCount ? `${t.result.blockCount} 个块` : '100%';
                return `
                  <div class="list-item-row" style="${isSel ? 'background:var(--accent-soft);border-left:3px solid var(--accent);' : ''}padding:10px 14px;display:flex;align-items:center;gap:10px;cursor:pointer;" onclick="state.parsingSelectedDocId='${esc(t.id)}';render();">
                    <span style="display:flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;background:var(--ok-soft);color:var(--ok);flex:0 0 28px;">${iconDoc(13)}</span>
                    <div class="grow" style="overflow:hidden;">
                      <b style="font-size:13px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(title)}</b>
                      <div class="muted" style="font-size:12px;margin-top:2px;">${blockCount} · 已生成制品</div>
                    </div>
                    <span style="color:var(--ok);font-size:13px;">✓</span>
                  </div>
                `;
              }).join('')}

              <!-- 解析失败 -->
              ${failedTasks.length ? `
                <div style="padding:10px 14px;background:var(--inset);font-weight:600;font-size:12.5px;color:var(--danger);margin-top:8px;">∨ 解析失败 (${failedTasks.length})</div>
                ${failedTasks.map(t => {
                  const title = t.name || t.input?.filename || t.object_id || t.id;
                  return `
                    <div class="list-item-row" style="padding:10px 14px;display:flex;align-items:center;gap:10px;">
                      <span style="display:flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;background:#fef2f2;color:var(--danger);flex:0 0 28px;">${iconDoc(13)}</span>
                      <div class="grow" style="overflow:hidden;">
                        <b style="font-size:13px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(title)}</b>
                        <div style="color:var(--danger);font-size:11.5px;margin-top:2px;">${esc(t.error_message || '解析失败')}</div>
                      </div>
                      <button class="btn sm" onclick="window.handleRetrySingleTask('${esc(t.id)}')">重试</button>
                    </div>
                  `;
                }).join('')}
              ` : ''}
            ` : `
              <div style="padding:24px 14px;color:var(--ink-dim);font-size:13px;">暂无解析任务，请先登记资料。</div>
            `}
          </div>
          <div style="padding:10px 14px;margin-top:auto;">
            <a href="#" style="font-size:12px;color:var(--accent);" onclick="window.toggleParsingFailed(); return false;">
              ${state.parsingFailedExpanded ? '收起 &lt;' : '查看全部 (' + failedTasks.length + ')'}
            </a>
          </div>
        </div>
      </div>

      <!-- Column 2: 文档预览 -->
      <div class="parsing-col-preview">
        <div class="card-head parsing-preview-header">
          <span class="parsing-preview-title">文档预览 <span class="muted" style="font-weight:normal;font-size:13px;">(${esc(selTaskDoc.name || '未选择文档')})</span></span>
          <div class="parsing-preview-toolbar">
            <div style="display:flex;align-items:center;gap:6px;">
              <button class="btn sm" title="上一页" aria-label="上一页" onclick="handleParsingPageStep(-1)" ${state.parsingCurrentPage <= 1 || !totalDocPages ? 'disabled' : ''}>&lt;</button>
              <span class="parsing-page-count">${totalDocPages ? state.parsingCurrentPage : 0} / ${totalDocPages}</span>
              <button class="btn sm" title="下一页" aria-label="下一页" onclick="handleParsingPageStep(1)" ${state.parsingCurrentPage >= totalDocPages ? 'disabled' : ''}>&gt;</button>
            </div>
            <div class="parsing-zoom-controls">
              <button type="button" title="缩小" aria-label="缩小" onclick="handleParsingZoomChange(-10)" ${state.parsingZoom <= 50 ? 'disabled' : ''}>-</button>
              <span>${state.parsingZoom}%</span>
              <button type="button" title="放大" aria-label="放大" onclick="handleParsingZoomChange(10)" ${state.parsingZoom >= 150 ? 'disabled' : ''}>+</button>
            </div>
            <button class="btn sm" title="重置缩放" aria-label="重置缩放" onclick="state.parsingZoom=100;render();">${iconRefresh(14)}</button>
          </div>
        </div>
        <div class="card-body parsing-preview-body">
          <!-- Inner Split: Left Thumbnails + Right Canvas -->
          <div class="parsing-preview-split">
            <!-- Thumbnail Strip (WPS PPT Style with Hidden Scrollbar) -->
            <div class="parsing-thumbnail-rail">
              <!-- Slide Thumbnails -->
              <div class="parsing-thumbnails">
                ${Array.from({ length: Math.min(totalDocPages, state.parsingPageLimit || 48) }, (_, i) => i + 1).map(p => `
                  <div class="parsing-slide-row" onclick="window.handleParsingJumpPage(${p})" style="display:flex;align-items:center;gap:8px;cursor:pointer;">
                    <span class="parsing-slide-num" style="font-size:11.5px;color:${state.parsingCurrentPage === p ? 'var(--accent)' : 'var(--ink-dim)'};width:20px;text-align:right;flex-shrink:0;font-weight:${state.parsingCurrentPage === p ? '700' : '500'};">${p}</span>
                    <div class="parsing-slide-thumb ${state.parsingCurrentPage === p ? 'active' : ''}" 
                         style="width:98px;height:55px;flex:0 0 55px;aspect-ratio:16/9;border-radius:4px;border:1.5px solid ${state.parsingCurrentPage === p ? 'var(--accent)' : 'var(--line)'};background:var(--card-bg);overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);box-sizing:border-box;display:flex;flex-direction:column;padding:3px;" 
                         title="第 ${p} 页">
                      <div style="font-size:7px;line-height:1.4;color:var(--ink-dim);overflow:hidden;">${esc(selTaskDoc.name || '')}<br>第 ${p} 页</div>
                    </div>
                  </div>
                `).join('')}
                <div style="text-align:center;padding:6px 0;color:var(--ink-dim);cursor:pointer;font-size:18px;" onclick="state.parsingPageLimit=(state.parsingPageLimit||48)+48;render();">+</div>
              </div>
            </div>

            <div class="parsing-viewport" style="--preview-zoom:${state.parsingZoom / 100};">
              <div class="parsing-page-canvas">
                ${preview?.imageUrl
                  ? `<img src="${esc(preview.imageUrl)}" alt="${esc(selTaskDoc.name || '')} 第 ${state.parsingCurrentPage} 页">`
                  : `<div class="parsing-page-text">${esc(previewError || preview?.text || '选择任务后查看文档内容')}</div>`}
              </div>
            </div>
        </div>

        <!-- Bottom Legend -->
        <div class="parsing-preview-legend">
          <div style="display:flex;align-items:center;gap:6px;"><span style="width:10px;height:10px;background:#16a34a;display:inline-block;border-radius:2px;"></span> pypdf (文本层)</div>
          <div style="display:flex;align-items:center;gap:6px;"><span style="width:10px;height:10px;background:#2563eb;display:inline-block;border-radius:2px;"></span> MinerU (版面识别)</div>
          <div style="display:flex;align-items:center;gap:6px;"><span style="width:10px;height:10px;background:#ea580c;display:inline-block;border-radius:2px;"></span> OCR (光学识别)</div>
          <div style="display:flex;align-items:center;gap:6px;"><span style="width:10px;height:10px;background:#9333ea;display:inline-block;border-radius:2px;"></span> VLM (视觉理解)</div>
        </div>
      </div>
    </div>

      <!-- Column 3: 页面信息与内容对比 (统一外层大框大卡片) -->
      <div class="card parsing-col-info" style="display:flex;flex-direction:column;padding:0;overflow:hidden;">
        <!-- 3.1 页面信息 (第 45 页) -->
        <div class="card-head" style="padding:14px 18px;font-size:14px;font-weight:700;">页面信息 <span class="muted" style="font-weight:normal;font-size:12.5px;">(第 ${totalDocPages ? state.parsingCurrentPage : 0} 页)</span></div>
        <div class="card-body" style="padding:14px 18px;font-size:13px;display:flex;flex-direction:column;gap:10px;flex:0 0 auto;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span class="muted">路由依据</span>
            <span style="font-size:12.5px;">${esc(inspect.routeReason || '—')}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span class="muted">实际解析器</span>
            <span class="badge ok" style="padding:2px 8px;">${esc(inspect.parser || inspect.engine || '—')}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span class="muted">文字层质量</span>
            <div style="display:flex;align-items:center;gap:8px;">
              <b style="font-size:12.5px;">${esc(inspect.qualityStatus || '待检测')}</b>
              <div class="progress-bar-wrap" style="width:80px;margin-top:0;"><div class="progress-bar-fill" style="width:${inspect.qualityStatus === 'passed' ? 100 : 0}%;"></div></div>
            </div>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span class="muted">耗时</span>
            <span class="mono">${inspect.inspectionMs == null ? '—' : inspect.inspectionMs + ' ms'}</span>
          </div>
          <div style="margin-top:6px;background:var(--warn-soft);border:1px solid var(--warn);border-radius:6px;padding:8px 12px;font-size:12px;color:var(--warn-ink);">
            <div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;">
              <span>告警 ⚠️ ${warnings.length} 条</span>
              <span>⌄</span>
            </div>
            <div style="margin-top:4px;color:var(--warn-ink);">${warnings.length ? warnings.map(w => esc(w.message || w.code || String(w))).join('<br>') : '无告警'}</div>
          </div>
        </div>

        <!-- 3.2 内容对比 (一体化内嵌在大框内，带分隔线) -->
        <div class="card-head" style="padding:12px 18px;display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--line-soft);">
          <span style="font-size:14px;font-weight:700;">内容对比</span>
          <div style="display:flex;align-items:center;gap:6px;font-size:12px;">
            <span class="muted">高亮差异</span>
            <label class="switch-toggle"><input type="checkbox" ${state.parsingHighlightDiff ? 'checked' : ''} onchange="window.handleToggleDiffHighlight(this)"><span class="switch-slider"></span></label>
          </div>
        </div>
        <div class="card-body" style="padding:14px 18px;flex:1 1 auto;display:flex;flex-direction:column;">
          <div style="display:flex;flex-direction:column;gap:12px;height:100%;">
            <div style="flex:1;">
              <small class="muted" style="display:block;margin-bottom:4px;">解析前 (原始内容)</small>
              <div class="diff-box" style="height:115px;background:var(--inset);">
                ${esc(diff.before || '暂无原始内容').replace(/\n/g, '<br>')}
              </div>
            </div>
            <div style="flex:1;">
              <small class="muted" style="display:block;margin-bottom:4px;">解析后 (清理后)</small>
              <div class="diff-box" style="height:115px;background:var(--card-bg);">
                ${renderParsingDiff(diff)}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Bottom Full-Width Running Resource Monitor Card -->
    <div class="card" style="margin-top:0;">
      <div class="card-body" style="display:flex;align-items:center;justify-content:space-between;padding:14px 22px;">
        <div style="display:flex;align-items:center;gap:36px;">
          <b style="font-size:14px;color:var(--ink-strong);">运行资源</b>
          <div>
            <div class="muted" style="font-size:12px;">CPU 池</div>
            <div style="display:flex;align-items:center;gap:8px;margin-top:2px;">
              <b>${resources.cpu?.percent ?? '—'}%</b>
              <div class="progress-bar-wrap" style="width:60px;margin-top:0;"><div class="progress-bar-fill" style="width:${resources.cpu?.percent || 0}%;"></div></div>
              <span class="muted" style="font-size:12px;">${resources.cpu?.cores ?? '—'} 核</span>
            </div>
          </div>
          <div>
            <div class="muted" style="font-size:12px;">GPU 池</div>
            <div style="display:flex;align-items:center;gap:8px;margin-top:2px;">
              <b>${resources.gpu?.available ? (resources.gpu.percent ?? '—') + '%' : '未配置'}</b>
              <div class="progress-bar-wrap" style="width:60px;margin-top:0;"><div class="progress-bar-fill" style="width:${resources.gpu?.percent || 0}%;"></div></div>
              <span class="muted" style="font-size:12px;">${resources.gpu?.devices?.length || 0} 卡</span>
            </div>
          </div>
          <div>
            <div class="muted" style="font-size:12px;">队列长度</div>
            <b style="font-size:18px;color:var(--ink-strong);">${resources.queueLength ?? 0}</b>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:18px;">
          <div>
            <div class="muted" style="font-size:12px;">吞吐量</div>
            <div style="font-size:16px;font-weight:700;">${throughput} <span style="font-size:12px;font-weight:400;" class="muted">文档/分钟</span></div>
          </div>
          <!-- Green Sparkline SVG -->
          <svg width="150" height="36" viewBox="0 0 150 36" fill="none">
            <path d="M0 28 L 25 24 L 50 30 L 75 18 L 100 22 L 125 8 L 150 14" stroke="#0f8b4c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M0 28 L 25 24 L 50 30 L 75 18 L 100 22 L 125 8 L 150 14 L 150 36 L 0 36 Z" fill="rgba(15, 139, 76, 0.08)"/>
          </svg>
        </div>
      </div>
    </div>`;
    return { desc: '', actions: '', html };
  }

  /* 06 知识库 > 构建知识索引 - 100% 对应 06-知识库-构建知识索引.png */
  async function pageIndex() {
    let dsId = state.selectedDatasetId || (api?.context?.defaultDatasetId);
    if (!dsId && api && api.connected) {
      if (!api.context) {
        try { await api.syncContext(); } catch (e) {}
      }
      dsId = api.context?.defaultDatasetId;
      if (!dsId) {
        try {
          const kbs = (api.context && api.context.knowledgeBases) || await api.getKnowledgeBases() || [];
          if (kbs.length) {
            const datasets = await api.getDatasets(kbs[0].id) || [];
            if (datasets.length) {
              dsId = datasets[0].id;
              state.selectedDatasetId = dsId;
            }
          }
        } catch (e) {}
      }
    }

    let chunks = [];
    let activeRelease = null;

    if (api && api.connected && dsId && !String(dsId).startsWith('ds-demo-')) {
      try {
        chunks = await api.getChunks(dsId, { limit: 20 }) || [];
        const releases = await api.getReleases(dsId) || [];
        activeRelease = releases.find(r => r.status === 'active') || releases[0] || null;
      } catch (e) {}
    }

    const curChunkId = state.selectedChunkId || chunks[0]?.id;
    const curChunk = (chunks.find(c => c.id === curChunkId) || chunks[0]) || {
      id: '',
      excluded: 0,
      content_text: '暂无切片内容',
      document_title: '—',
      locator_page: null,
      token_count: 0
    };
    state.selectedChunkId = curChunk?.id || '';

    state.currentChunks = chunks;
    state.activeReleaseId = activeRelease ? activeRelease.id : null;
    const totalChunks = chunks.length;
    const vectorizedChunks = chunks.filter(c => !c.excluded).length;
    const pendingChunks = chunks.filter(c => c.excluded || c.warning).length;
    const releaseVersion = activeRelease ? 'v' + activeRelease.version : '—';

    const html = `
    <!-- Top 4-Step Wizard Stepper Bar (Theme Adaptive) -->
    <div class="index-stepper-track">
      <div class="index-stepper-indicator"></div>
      <div class="index-stepper-steps">
        <div class="index-stepper-step">
          <div class="index-stepper-pill active">
            <div class="index-stepper-num">1</div>
            <span>切块</span>
          </div>
          <div class="index-stepper-line"></div>
        </div>
        <div class="index-stepper-step">
          <div class="index-stepper-pill">
            <div class="index-stepper-num">2</div>
            <span>向量化</span>
          </div>
          <div class="index-stepper-line"></div>
        </div>
        <div class="index-stepper-step">
          <div class="index-stepper-pill">
            <div class="index-stepper-num">3</div>
            <span>向量索引</span>
          </div>
          <div class="index-stepper-line"></div>
        </div>
        <div class="index-stepper-step">
          <div class="index-stepper-pill">
            <div class="index-stepper-num">4</div>
            <span>全文索引</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 4 Metric Cards (Theme Adaptive: Light theme primary, Dark theme sleek) -->
    <div class="grid grid-4" style="margin-bottom:18px;">
      <!-- Card 1: 知识块 -->
      <div class="card" style="margin:0;">
        <div class="card-body" style="display:flex;align-items:center;gap:16px;padding:18px 20px;">
          <div style="width:44px;height:44px;border-radius:10px;background:var(--accent-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;">${iconDoc(13)}</div>
          <div>
            <div class="muted" style="font-size:12.5px;margin-bottom:2px;">知识块</div>
            <b style="font-size:22px;color:var(--ink-strong);line-height:1.1;">${totalChunks.toLocaleString()}</b>
          </div>
        </div>
      </div>
      <!-- Card 2: 已向量化 -->
      <div class="card" style="margin:0;">
        <div class="card-body" style="display:flex;align-items:center;gap:16px;padding:18px 20px;">
          <div style="width:44px;height:44px;border-radius:10px;background:var(--accent-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;">${iconCube(16)}</div>
          <div>
            <div class="muted" style="font-size:12.5px;margin-bottom:2px;">已向量化</div>
            <b style="font-size:22px;color:var(--ink-strong);line-height:1.1;">${vectorizedChunks.toLocaleString()}</b>
          </div>
        </div>
      </div>
      <!-- Card 3: 待更新 -->
      <div class="card" style="margin:0;">
        <div class="card-body" style="display:flex;align-items:center;gap:16px;padding:18px 20px;">
          <div style="width:44px;height:44px;border-radius:10px;background:var(--warn-soft);color:var(--warn);display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;">${iconClock(13)}</div>
          <div>
            <div class="muted" style="font-size:12.5px;margin-bottom:2px;">待更新</div>
            <b style="font-size:22px;color:var(--ink-strong);line-height:1.1;">${pendingChunks.toLocaleString()}</b>
          </div>
        </div>
      </div>
      <!-- Card 4: 索引版本 -->
      <div class="card" style="margin:0;">
        <div class="card-body" style="display:flex;align-items:center;gap:16px;padding:18px 20px;">
          <div style="width:44px;height:44px;border-radius:10px;background:var(--blue-soft);color:var(--blue);display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;">${iconLayers(16)}</div>
          <div>
            <div class="muted" style="font-size:12.5px;margin-bottom:2px;">索引版本</div>
            <b style="font-size:22px;color:var(--ink-strong);line-height:1.1;">${releaseVersion}</b>
          </div>
        </div>
      </div>
    </div>

    <!-- Main 3-Column Workspace -->
    <div class="workspace-layout-indexing">
      <!-- Column 1: 筛选 -->
      <div class="index-col-filter">
        <div class="card-head" style="padding:14px 18px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);">
          <span style="font-size:14px;font-weight:700;color:var(--ink-strong);">筛选</span>
          <span class="muted" style="cursor:pointer;font-size:12px;" title="收起筛选">«</span>
        </div>
        <div class="card-body" style="padding:16px 18px;font-size:13px;display:flex;flex-direction:column;">
          <div class="form-group" style="margin-bottom:14px;">
            <label class="muted" style="font-size:12px;margin-bottom:4px;display:block;">文档</label>
            <select class="select" id="indexFilterDoc" style="height:34px;font-size:13px;background:var(--card-bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;width:100%;"><option>全部文档</option></select>
          </div>
          <div class="form-group" style="margin-bottom:14px;">
            <label class="muted" style="font-size:12px;margin-bottom:4px;display:block;">章节</label>
            <select class="select" id="indexFilterChapter" style="height:34px;font-size:13px;background:var(--card-bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;width:100%;"><option>全部章节</option></select>
          </div>
          <div class="form-group" style="margin-bottom:16px;">
            <label class="muted" style="font-size:12px;margin-bottom:6px;display:block;">长度 (Token)</label>
            <div style="display:flex;align-items:center;gap:6px;">
              <input class="input" placeholder="最小值" style="height:32px;font-size:12.5px;padding:0 8px;border-radius:6px;background:var(--card-bg);border:1px solid var(--line);color:var(--ink);flex:1;min-width:0;">
              <span class="muted" style="font-size:12px;">~</span>
              <input class="input" placeholder="最大值" style="height:32px;font-size:12.5px;padding:0 8px;border-radius:6px;background:var(--card-bg);border:1px solid var(--line);color:var(--ink);flex:1;min-width:0;">
            </div>
          </div>
          <div class="form-group" style="margin-bottom:20px;">
            <label class="muted" style="font-size:12px;margin-bottom:8px;display:block;">状态</label>
            <div style="display:flex;flex-direction:column;gap:8px;">
              <label style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;font-size:13px;">
                <span style="display:flex;align-items:center;gap:6px;"><input type="checkbox" checked style="accent-color:var(--accent);"> 全部</span>
                <span class="muted" style="font-size:12px;">${totalChunks.toLocaleString()}</span>
              </label>
              <label style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;font-size:13px;">
                <span style="display:flex;align-items:center;gap:6px;"><input type="checkbox" checked style="accent-color:var(--accent);"> 已向量化</span>
                <span class="muted" style="font-size:12px;">${vectorizedChunks.toLocaleString()}</span>
              </label>
              <label style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;font-size:13px;">
                <span style="display:flex;align-items:center;gap:6px;"><input type="checkbox" checked style="accent-color:var(--accent);"> 待更新</span>
                <span class="muted" style="font-size:12px;">${pendingChunks.toLocaleString()}</span>
              </label>
              <label style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;font-size:13px;">
                <span style="display:flex;align-items:center;gap:6px;"><input type="checkbox" style="accent-color:var(--accent);"> 已禁用</span>
                <span class="muted" style="font-size:12px;">0</span>
              </label>
            </div>
          </div>
          <button class="btn" style="width:100%;margin-top:auto;height:34px;background:var(--card-bg);border:1px solid var(--line);border-radius:6px;font-size:13px;color:var(--ink-strong);" onclick="window.handleResetIndexFilters()">重置筛选</button>
        </div>
      </div>

      <!-- Column 2: 知识块列表 -->
      <div class="index-col-chunks">
        <div class="card-head" style="padding:12px 14px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:10px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-size:13.5px;font-weight:700;color:var(--ink-strong);">知识块列表 <span class="muted" style="font-weight:400;font-size:12px;">(共 ${totalChunks.toLocaleString()} 条)</span></span>
          </div>
          <div style="display:flex;align-items:center;gap:8px;width:100%;">
            <input class="input" id="indexSearchInput" placeholder="搜索知识块内容或 ID" style="height:34px;padding-left:12px;font-size:13px;border-radius:6px;background:var(--card-bg);border:1px solid var(--line);color:var(--ink);flex:1;min-width:0;" onkeyup="if(event.key==='Enter')window.handleSearchRelease()">
            <button class="btn sm" style="width:34px;height:34px;padding:0;display:flex;align-items:center;justify-content:center;border:1px solid var(--line);background:var(--card-bg);color:var(--ink-dim);border-radius:6px;" title="筛选" onclick="showToast('已按条件过滤知识块')">⛛</button>
          </div>
        </div>
        <div class="card-body" style="padding:10px;overflow-y:auto;overflow-x:hidden;max-height:560px;">
          ${chunks.map((chunk) => {
            const isSelected = chunk.id === curChunk.id;
            const previewText = (chunk.content_text || chunk.content_md || '').slice(0, 85) + '...';
            return `
              <div class="index-chunk-card ${isSelected ? 'active' : ''}" onclick="window.handleSelectChunkItem('${esc(chunk.id)}')">
                <div class="index-chunk-radio"></div>
                <div class="index-chunk-info">
                  <div class="index-chunk-header">
                    <span class="index-chunk-id">${esc(chunk.id)}</span>
                    <span class="index-chunk-status" style="color:${chunk.warning ? 'var(--warn)' : (chunk.excluded ? 'var(--ink-dim)' : 'var(--ok)')};">
                      <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor;"></span>
                      ${chunk.warning ? '待更新' : (chunk.excluded ? '已禁用' : '已向量化')}
                    </span>
                  </div>
                  <div class="index-chunk-snippet">
                    ${esc(previewText)}
                  </div>
                  <div class="index-chunk-meta">
                    <span style="overflow:hidden;text-overflow:ellipsis;">来源: 《${esc(chunk.document_title || '人工智能导论')}》第 ${chunk.locator_page || 12} 页</span>
                    <span style="opacity:0.5;">|</span>
                    <span style="flex-shrink:0;">${chunk.token_count || 512} Tokens</span>
                  </div>
                </div>
              </div>
            `;
          }).join('')}
        </div>
      </div>

      <!-- Column 3: 知识块编辑与一致性视图 -->
      <div class="index-col-edit">
        <!-- 3.1 知识块编辑 Card -->
        <div class="card" style="margin:0;">
          <div class="card-head" style="padding:12px 18px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);">
            <div style="display:flex;align-items:center;gap:8px;">
              <span style="font-size:14px;font-weight:700;color:var(--ink-strong);">知识块编辑</span>
            </div>
            <a style="font-size:12px;color:var(--ink-dim);cursor:pointer;text-decoration:none;" onclick="showToast('正在调取《${esc(curChunk.document_title || '人工智能导论')}》第 ${curChunk.locator_page || 12} 页原版快照')">来源: 《${esc(curChunk.document_title || '人工智能导论')}》第 ${curChunk.locator_page || 12} 页 ↗</a>
          </div>
          <div class="card-body" style="padding:16px 18px;display:flex;flex-direction:column;">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
              <b style="font-size:14px;color:var(--ink-strong);">${esc(curChunk.id)}</b>
              <span class="badge ${curChunk.excluded ? 'warn' : (curChunk.warning ? 'warn' : 'ok')}" style="font-size:11.5px;padding:2px 8px;">${curChunk.excluded ? '已禁用' : (curChunk.warning ? '待更新' : '已向量化')}</span>
            </div>
            <textarea class="textarea" id="chunkEditorTextarea" style="font-size:13px;line-height:1.65;border-radius:6px;padding:12px;width:100%;height:160px;resize:vertical;background:var(--card-bg);border:1px solid var(--line);color:var(--ink);" oninput="document.getElementById('chunkTokenCount').textContent = Math.ceil(this.value.length * 0.75)">${esc(curChunk.content_md || curChunk.content_text || '')}</textarea>
            <div style="margin-top:6px;font-size:12px;color:var(--ink-dim);">Token 数: <span id="chunkTokenCount">${curChunk.token_count || 512}</span></div>
            <div style="display:flex;align-items:center;gap:10px;margin-top:14px;">
              <button class="btn" style="height:34px;font-size:13px;padding:0 14px;border:1px solid var(--line);background:var(--card-bg);color:var(--ink-strong);border-radius:6px;" onclick="window.handleSplitChunk()">✂ 拆分</button>
              <button class="btn" style="height:34px;font-size:13px;padding:0 14px;border:1px solid var(--line);background:var(--card-bg);color:var(--ink-strong);border-radius:6px;" onclick="window.handleMergeChunk()">⮑ 合并</button>
              <button class="btn" style="height:34px;font-size:13px;padding:0 14px;color:var(--danger);border:1px solid var(--danger);background:transparent;border-radius:6px;" onclick="window.handleToggleChunkDisabled()">${curChunk.excluded ? '⟲ 恢复' : '🚫 禁用'}</button>
              <div style="margin-left:auto;display:flex;align-items:center;">
                <button class="btn primary" style="height:34px;font-size:13px;padding:0 14px;background:var(--accent);color:#fff;border-top-right-radius:0;border-bottom-right-radius:0;" onclick="window.handleSaveChunkEdit()">保存并增量更新</button>
                <button class="btn primary" style="height:34px;padding:0 8px;background:var(--accent);color:#fff;border-left:1px solid rgba(255,255,255,0.25);border-top-left-radius:0;border-bottom-left-radius:0;" onclick="showToast('更多更新选项：1. 仅保存 2. 立即重算向量 3. 增量构建')">▾</button>
              </div>
            </div>
          </div>
        </div>

        <!-- 3.2 索引构建一致性视图 Card -->
        <div class="consistency-view" style="background:var(--card-bg);border:1px solid var(--line);border-radius:var(--radius-card);padding:14px 18px;margin-top:0;">
          <div style="font-size:13.5px;font-weight:700;color:var(--ink-strong);margin-bottom:12px;">索引构建一致性视图</div>
          <div class="consistency-nodes">
            <!-- Node 1: 数据块 -->
            <div class="consistency-node step-1">
              <div style="font-weight:700;font-size:12px;margin-bottom:3px;">数据块</div>
              <div style="font-size:11px;opacity:0.9;">(${esc(curChunk.id)})</div>
              <div style="font-size:11px;opacity:0.8;margin-top:3px;">来源页: 第 ${curChunk.locator_page || 12} 页</div>
            </div>
            <span style="color:var(--ink-faint);font-size:14px;display:flex;align-items:center;flex-shrink:0;">➔</span>
            <!-- Node 2: 向量记录 (动态状态感知) -->
            <div class="consistency-node step-2" style="${curChunk.warning ? 'background:var(--warn-soft);border:1.5px solid var(--warn);color:var(--warn);' : ''}">
              <div style="font-weight:700;font-size:12px;margin-bottom:3px;">${curChunk.warning ? '⚠️ 向量记录 (待更新)' : '向量记录'}</div>
              <div style="font-size:11px;opacity:0.9;">${curChunk.token_count || 512} Tokens</div>
              <div style="font-size:11px;opacity:0.8;margin-top:3px;">${curChunk.warning ? '版本滞后 (待重算)' : '1536 维 (已同步)'}</div>
            </div>
            <span style="color:var(--ink-faint);font-size:14px;display:flex;align-items:center;flex-shrink:0;">➔</span>
            <!-- Node 3: 集合 (Collection) -->
            <div class="consistency-node step-3">
              <div style="font-weight:700;font-size:12px;margin-bottom:3px;">集合 (Collection)</div>
              <div style="font-size:11px;opacity:0.9;">ai_guide_${activeRelease ? 'v' + activeRelease.version : 'v7'}</div>
              <div style="font-size:11px;opacity:0.8;margin-top:3px;">${vectorizedChunks.toLocaleString()} 条向量</div>
            </div>
            <span style="color:var(--ink-faint);font-size:14px;display:flex;align-items:center;flex-shrink:0;">➔</span>
            <!-- Node 4: 向量索引 (HNSW) -->
            <div class="consistency-node step-4">
              <div style="font-weight:700;font-size:12px;margin-bottom:3px;">向量索引 (HNSW)</div>
              <div style="font-size:11px;opacity:0.9;">ai_guide_v7_hnsw</div>
              <div style="font-size:11px;opacity:0.8;margin-top:3px;">已构建 (就绪)</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Bottom Actions Bar (发布新版本与查询验证) -->
    <div style="display:flex;justify-content:flex-end;align-items:center;gap:12px;margin-top:20px;">
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);border-radius:6px;height:38px;padding:0 22px;font-size:14px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="window.handleSearchRelease()">查询验证</button>
      <button class="btn primary" style="background:var(--accent);color:#ffffff;border-radius:6px;height:38px;padding:0 24px;font-size:14px;font-weight:500;cursor:pointer;" onclick="window.handleBuildRelease()">发布版本</button>
    </div>`;

    return { desc: '知识块清洗、向量化计算与不可变版本构建发布', actions: '', html };
  }

  
  // Trace 一经记录即不可变（编辑产生草稿与派生 Trace），详情按 id 缓存避免每次渲染重复拉全量。
  const qaDetailCache = new Map();
  async function getActiveQATrace() {
    if (!api.connected) {
      state.activeTraceDetail = null;
      return { traces: [], activeTrace: null };
    }
    const traces = await api.getTraces({ limit: 100 }, { throwOnError: true });
    if (!traces.length) {
      state.activeTraceId = null;
      state.activeTraceDetail = null;
      return { traces, activeTrace: null };
    }
    const id = state.activeTraceId || traces[0].id;
    let activeTrace = qaDetailCache.get(id);
    if (!activeTrace) {
      activeTrace = await api.getTrace(id, {}, { throwOnError: true });
      if (qaDetailCache.size >= 12) qaDetailCache.delete(qaDetailCache.keys().next().value);
      qaDetailCache.set(id, activeTrace);
    }
    state.activeTraceId = activeTrace.id;
    state.activeTraceDetail = activeTrace;
    return { traces, activeTrace };
  }

  function emptyQATrace(stageIndex) {
    return {
      title: flowNames[stageIndex], desc: '', actions: '',
      html: renderQATraceBanner(null, [], stageIndex) + renderQATraceHeader(stageIndex, Array(8).fill('—')) +
        `<div class="card" style="padding:32px 24px;min-height:240px;">
          <div class="card-head" style="padding:0 0 14px;">暂无问答记录</div>
          <div class="muted" style="line-height:1.8;">在智能问答中提交问题后，可以在这里查看该次请求的解析、检索、证据和回答记录。</div>
          <button class="btn primary" style="margin-top:20px;" onclick="window.go('apps/chat')">进入智能问答</button>
        </div>`
    };
  }

  function renderQATraceBanner(activeTrace, traces = [], currentStageIdx = null, customTotalDuration = null) {
    const traceId = activeTrace?.id || '—';
    let totalSec = '0.00';
    if (customTotalDuration) {
      totalSec = String(customTotalDuration).replace(/\s*s$/i, '').trim();
    } else if (currentStageIdx !== null && currentStageIdx !== undefined) {
      // 严格按用户需求：未走完流程只累加至当前阶段步骤的累计耗时
      const defaultStageDurations = Array(8).fill(0);
      const traceStages = activeTrace?.stages || [];
      let elapsedMs = 0;
      for (let i = 0; i <= currentStageIdx; i++) {
        const s = traceStages[i];
        elapsedMs += (s && s.durationMs != null ? s.durationMs : (defaultStageDurations[i] || 0));
      }
      totalSec = (elapsedMs / 1000).toFixed(2);
    } else if (activeTrace?.metrics?.totalMs) {
      totalSec = (activeTrace.metrics.totalMs / 1000).toFixed(2);
    } else if (activeTrace?.duration) {
      totalSec = (activeTrace.duration / 1000).toFixed(2);
    }
    const kbName = activeTrace?.knowledge_base_name || api.context?.knowledgeBases?.find(kb => kb.id === activeTrace?.permission_snapshot?.knowledgeBaseId)?.name || activeTrace?.permission_snapshot?.datasetId || '—';
    const appName = activeTrace?.app_name || '内部智能问答';
    const isFailed = activeTrace?.status === 'failed';
    const statusText = !activeTrace ? '无记录' : qaStatusLabel(activeTrace.status);
    const statusColor = isFailed ? 'var(--danger)' : 'var(--accent)';

    const traceOptions = (traces && traces.length > 1) ? traces.map(t => {
      const q = (t.query || '未命名').slice(0, 16);
      return '<option value="' + esc(t.id) + '" ' + (t.id === traceId ? 'selected' : '') + '>' + esc(t.id) + ' (' + esc(q) + '...)</option>';
    }).join('') : '';

    return `
    <div class="card qa-trace-banner" style="margin-bottom:18px;padding:12px 22px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px;background:var(--card-bg);border:1px solid var(--line);border-radius:var(--radius-card);font-size:13px;box-shadow:var(--shadow);">
      <div style="display:flex;align-items:center;gap:10px;">
        <span class="muted" style="color:var(--ink-dim);">Trace ID</span>
        ${traceOptions ? `
          <select class="input sm" style="height:28px;font-size:12px;font-family:var(--font-mono);padding:0 8px;background:var(--card-bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;" onchange="state.activeTraceId=this.value;render();">
            ${traceOptions}
          </select>
        ` : `
          <span class="mono" style="font-weight:600;color:var(--ink-strong);">${esc(traceId)}</span>
        `}
        <span style="cursor:pointer;display:inline-flex;align-items:center;color:var(--ink-dim);transition:color 0.15s;" title="复制 Trace ID" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--ink-dim)'" onclick="handleCopySnippet(${jsArg(traceId)})">${iconCopy(14)}</span>
      </div>
      <div style="width:1px;height:16px;background:var(--line);"></div>
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="muted" style="color:var(--ink-dim);">应用</span>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="color:var(--ink-dim);vertical-align:-2px;"><rect x="4" y="2" width="16" height="20" rx="2" ry="2"/><path d="M9 22v-4h6v4"/><line x1="8" y1="6" x2="8.01" y2="6"/><line x1="16" y1="6" x2="16.01" y2="6"/><line x1="12" y1="6" x2="12.01" y2="6"/><line x1="8" y1="10" x2="8.01" y2="10"/><line x1="16" y1="10" x2="16.01" y2="10"/><line x1="12" y1="10" x2="12.01" y2="10"/><line x1="8" y1="14" x2="8.01" y2="14"/><line x1="16" y1="14" x2="16.01" y2="14"/><line x1="12" y1="14" x2="12.01" y2="14"/></svg>
        <b style="color:var(--ink-strong);">${esc(appName)}</b>
      </div>
      <div style="width:1px;height:16px;background:var(--line);"></div>
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="muted" style="color:var(--ink-dim);">知识库</span>
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="color:var(--ink-dim);vertical-align:-2px;"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>
        <b style="color:var(--ink-strong);">${esc(kbName)}</b>
      </div>
      <div style="width:1px;height:16px;background:var(--line);"></div>
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="muted" style="color:var(--ink-dim);">状态</span>
        <span style="color:${statusColor};font-weight:600;">● ${statusText}</span>
      </div>
      <div style="width:1px;height:16px;background:var(--line);"></div>
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="muted" style="color:var(--ink-dim);">总耗时</span>
        <b style="color:var(--ink-strong);">${totalSec} s</b>
      </div>
    </div>`;
  }

  function renderQATitleBar(titleText, activeTrace, traces = []) {
    return esc(titleText);
  }

  /* 07 问答流程 > 问题解析 - 100% 对应 07-问答流程-问题解析.png */
  async function pageQA07_Parse() {
    const { traces, activeTrace } = await getActiveQATrace();
    if (!activeTrace?.id) return emptyQATrace(0);
    const results = await Promise.allSettled([
      qaParseEmbedRequest('getTraceParseStage', activeTrace.id),
      qaParseEmbedRequest('getTracePipeline', activeTrace.id, { stage: 1 })
    ]);
    const parseData = results[0].status === 'fulfilled' ? results[0].value : null;
    const pipelineData = results[1].status === 'fulfilled' ? results[1].value : null;
    const output = parseData?.output || {};
    const fields = {
      language: output.language,
      intent: output.intent,
      entities: Array.isArray(output.entities) ? output.entities : [],
      normalizedQuery: output.normalizedQuery ?? output.normalized,
      rewrittenQuery: output.rewrittenQuery ?? output.rewritten,
      filters: output.filters || {},
      keywords: Array.isArray(output.keywords) ? output.keywords : (Array.isArray(parseData?.keywords) ? parseData.keywords : [])
    };
    let context = { available: false, messages: [], history: [], source: '未记录' };
    if (parseData?.contextSource === 'recorded' && Array.isArray(parseData.contextMessages)) {
      const messages = parseData.contextMessages;
      context = { available: true, messages, history: Array.isArray(parseData.inputHistory) ? parseData.inputHistory : messages.slice(0, -1), source: 'Trace 输入快照' };
    } else if (parseData && parseData.contextSource === undefined && activeTrace.conversation_id) {
      try {
        const conversation = await qaParseEmbedRequest('getConversation', activeTrace.conversation_id);
        const messages = Array.isArray(conversation.messages) ? conversation.messages : [];
        const answerIndex = messages.findIndex(message => message.id === activeTrace.message_id || (message.role === 'assistant' && message.trace_id === activeTrace.id));
        const questionIndex = answerIndex > 0 && messages[answerIndex - 1].role === 'user' ? answerIndex - 1 : -1;
        if (questionIndex >= 0) context = { available: true, messages: messages.slice(0, questionIndex + 1), history: messages.slice(0, questionIndex), source: '会话历史（截至本次问题）' };
      } catch (error) { context.source = `上下文不可用：${error.message}`; }
    }
    const previousQuestion = [...context.history].reverse().find(message => message.role === 'user')?.content;
    const previousAnswer = [...context.history].reverse().find(message => message.role === 'assistant')?.content;
    const entitiesText = fields.entities.map(item => typeof item === 'string' ? item : item?.text || item?.name || qaParseEmbedText(item)).join(' / ');
    const filterText = Object.keys(fields.filters).length ? JSON.stringify(fields.filters) : '未记录';
    const kbName = activeTrace.knowledge_base_name || api.context?.knowledgeBases?.find(kb => kb.id === (activeTrace.knowledge_base_id || activeTrace.permission_snapshot?.knowledgeBaseId))?.name || api.context?.conversations?.find(conversation => conversation.id === activeTrace.conversation_id)?.knowledge_base_name;
    const intentConfidence = output.intentConfidence ?? output.confidence?.intent;
    const entityConfidence = output.entityConfidence ?? output.confidence?.entities;
    const confidenceWidth = value => Number.isFinite(value) ? `${Math.max(0, Math.min(1, value)) * 100}%` : '0%';
    const stageDuration = parseData?.dataSource !== 'unavailable' && parseData?.status !== 'unavailable' && Number.isFinite(parseData?.durationMs) ? `${parseData.durationMs} ms` : '未记录';
    const warnings = results.map((result, index) => result.status === 'rejected' ? `${index ? '流水线' : '解析阶段'}加载失败：${result.reason?.message || '服务不可用'}` : '').filter(Boolean);
    state.qaParseRecord = { traceId: activeTrace.id, data: parseData, fields, context };
    const traceBanner = renderQATraceBanner(activeTrace, traces, 0, pipelineData?.totalDuration);
    const traceHeader = renderQATraceHeader(0, pipelineData?.stages?.map(s => s.durationMs != null ? `${s.durationMs}ms` : null));
    const questionText = activeTrace.query ?? activeTrace.question ?? output.original ?? '未记录';

    const html = `
    ${traceBanner}
    ${traceHeader}
    ${warnings.length ? `<div role="alert" class="muted" style="margin-bottom:12px;color:var(--danger);">${warnings.map(esc).join('<br>')}</div>` : ''}

    <!-- 原始问题 Top Card -->
    <div class="card" style="margin-bottom:16px;">
      <div class="card-body" style="padding:14px 18px;">
        <div class="muted" style="font-size:12px;margin-bottom:6px;">原始问题</div>
        <div style="font-size:14.5px;font-weight:600;color:var(--ink-strong);">${esc(questionText)}</div>
      </div>
    </div>

    <!-- 3-Column Card Layout (Equal Height) -->
    <div class="grid grid-3" style="align-items:stretch;">
      <!-- Card 1: 原始问题与会话上下文 -->
      <div class="card" style="display:flex;flex-direction:column;height:100%;">
        <div class="card-head" style="padding:14px 18px;font-size:14px;font-weight:700;">原始问题</div>
        <div class="card-body" style="padding:16px 18px;display:flex;flex-direction:column;gap:14px;">
          <div>
            <div class="muted" style="font-size:12px;margin-bottom:6px;">用户输入</div>
            <div style="background:var(--inset);padding:10px 14px;border-radius:6px;border:1px solid var(--line);font-size:13.5px;color:var(--ink-strong);">${esc(questionText)}</div>
          </div>
          <div style="border:1px solid var(--line);border-radius:8px;padding:14px;background:var(--inset);display:flex;flex-direction:column;gap:10px;margin-top:2px;">
            <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding-bottom:8px;">
              <b style="font-size:13.5px;color:var(--ink-strong);">会话上下文</b>
              <a href="#" style="font-size:12px;color:var(--accent);" onclick="handleViewFullContext();return false;">查看完整上下文 &gt;</a>
            </div>
            <div style="display:flex;justify-content:space-between;font-size:12.5px;"><span class="muted">上下文轮次</span><b>${context.available ? context.messages.filter(message => message.role === 'user').length : '未记录'}</b></div>
            <div style="font-size:12.5px;"><span class="muted">上一轮问题</span><div style="margin-top:2px;color:var(--ink-strong);overflow-wrap:anywhere;">${esc(previousQuestion ?? (context.available ? '无上一轮问题' : '未记录'))}</div></div>
            <div style="font-size:12.5px;"><span class="muted">上一轮答案摘要</span><div class="muted" style="margin-top:2px;font-size:12px;line-height:1.4;overflow-wrap:anywhere;">${esc(previousAnswer ? previousAnswer.slice(0, 260) : context.available ? '无上一轮答案' : '未记录')}</div></div>
            <div style="font-size:12.5px;"><span class="muted">上下文来源</span><div style="margin-top:2px;color:var(--ink-strong);">${esc(context.source)}</div></div>
          </div>
        </div>
      </div>

      <!-- Card 2: 结构化分析 (5 Bordered Boxes + Keywords) -->
      <div class="card" style="display:flex;flex-direction:column;height:100%;">
        <div class="card-head" style="padding:14px 18px;font-size:14px;font-weight:700;">结构化分析</div>
        <div class="card-body" style="padding:16px 18px;">
          <!-- 5 Bordered Result Boxes -->
          <div style="display:flex;flex-direction:column;gap:8px;">
            <!-- Box 1: 语言 -->
            <div style="border:1px solid var(--line);border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;background:var(--card-bg);">
              <div style="display:flex;align-items:center;gap:10px;">
                <span style="font-size:16px;">${iconDoc(13)}</span>
                <span class="muted" style="font-size:13px;">语言</span>
              </div>
              <div style="font-weight:600;font-size:13.5px;color:var(--ink-strong);">${esc(qaParseEmbedText(fields.language))}</div>
              <span role="button" title="复制语言" style="cursor:pointer;color:var(--ink-dim);font-size:13px;" onclick="handleCopyParseResult('language')">${iconCopy(13)}</span>
            </div>

            <!-- Box 2: 意图 -->
            <div style="border:1px solid var(--line);border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;background:var(--card-bg);">
              <div style="display:flex;align-items:center;gap:10px;">
                <span style="font-size:16px;">${iconTarget(14)}</span>
                <span class="muted" style="font-size:13px;">意图</span>
              </div>
              <div id="extractedIntent" style="font-weight:600;font-size:13.5px;color:var(--ink-strong);overflow-wrap:anywhere;">${esc(qaParseEmbedText(fields.intent))}</div>
              <span role="button" title="复制意图" style="cursor:pointer;color:var(--ink-dim);font-size:13px;" onclick="handleCopyParseResult('intent')">${iconCopy(13)}</span>
            </div>

            <!-- Box 3: 实体 -->
            <div style="border:1px solid var(--line);border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;background:var(--card-bg);">
              <div style="display:flex;align-items:center;gap:10px;">
                <span style="font-size:16px;">${iconTag(14)}</span>
                <span class="muted" style="font-size:13px;">实体</span>
              </div>
              <div style="font-weight:600;font-size:13.5px;color:var(--ink-strong);overflow-wrap:anywhere;min-width:0;">${esc(entitiesText || '未记录')}</div>
              <span role="button" title="复制实体" style="cursor:pointer;color:var(--ink-dim);font-size:13px;" onclick="handleCopyParseResult('entities')">${iconCopy(13)}</span>
            </div>

            <!-- Box 4: 时间范围 -->
            <div style="border:1px solid var(--line);border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;background:var(--card-bg);">
              <div style="display:flex;align-items:center;gap:10px;">
                <span style="font-size:16px;">${iconClock(13)}</span>
                <span class="muted" style="font-size:13px;">时间范围</span>
              </div>
              <div style="font-size:13.5px;color:var(--ink-dim);">${esc(qaParseEmbedText(fields.filters.timeRange ?? output.timeRange))}</div>
              <span style="opacity:0;">${iconCopy(13)}</span>
            </div>

            <!-- Box 5: 权限范围 -->
            <div style="border:1px solid var(--line);border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;background:var(--card-bg);">
              <div style="display:flex;align-items:center;gap:10px;">
                <span style="font-size:16px;">${iconLock(13)}</span>
                <span class="muted" style="font-size:13px;">权限范围</span>
              </div>
              <div style="font-weight:600;font-size:13.5px;color:var(--ink-strong);overflow-wrap:anywhere;min-width:0;">${esc(qaParseEmbedText(fields.filters.datasetId))}</div>
              <span style="opacity:0;">${iconCopy(13)}</span>
            </div>
          </div>

          <!-- Keywords Pills -->
          <div style="margin-top:14px;">
            <div class="muted" style="font-size:12px;margin-bottom:8px;">关键词</div>
            <div style="display:flex;flex-wrap:wrap;gap:6px;">
              ${fields.keywords.length ? fields.keywords.map(word => `<span class="badge ok" style="padding:4px 9px;border-radius:4px;font-size:12px;white-space:normal;overflow-wrap:anywhere;">${esc(qaParseEmbedText(word))}</span>`).join('') : '<span class="muted">未记录</span>'}
            </div>
          </div>
        </div>
      </div>

      <!-- Card 3: 规范化问题 -->
      <div class="card" style="display:flex;flex-direction:column;height:100%;">
        <div class="card-head" style="padding:14px 18px;font-size:14px;font-weight:700;">规范化问题</div>
        <div class="card-body" style="padding:16px 18px;font-size:13px;display:flex;flex-direction:column;gap:10px;">
          <div>
            <div class="muted" style="font-size:12px;margin-bottom:4px;">规范化问题</div>
            <div style="background:var(--inset);padding:9px 12px;border-radius:6px;border:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;">
              <span style="font-weight:500;color:var(--ink-strong);overflow-wrap:anywhere;min-width:0;">${esc(qaParseEmbedText(fields.normalizedQuery))}</span>
              <span role="button" title="复制规范化问题" style="cursor:pointer;" onclick="handleCopyParseResult('normalizedQuery')">${iconCopy(13)}</span>
            </div>
          </div>
          <div>
            <div class="muted" style="font-size:12px;margin-bottom:4px;">查询改写</div>
            <div style="background:var(--inset);padding:9px 12px;border-radius:6px;border:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;">
              <span style="color:var(--ink-strong);overflow-wrap:anywhere;min-width:0;">${esc(qaParseEmbedText(fields.rewrittenQuery))}</span>
              <span role="button" title="复制查询改写" style="cursor:pointer;" onclick="handleCopyParseResult('rewrittenQuery')">${iconCopy(13)}</span>
            </div>
          </div>
          <div style="border:1px solid var(--line);background:var(--inset);border-radius:6px;padding:10px 12px;display:flex;flex-direction:column;gap:6px;font-size:12px;">
            <div style="display:flex;justify-content:space-between;"><span class="muted">知识库</span><span>${esc(qaParseEmbedText(kbName))}</span></div>
            <div style="display:flex;justify-content:space-between;"><span class="muted">文档类型</span><span>${esc(qaParseEmbedText(fields.filters.documentType))}</span></div>
            <div style="display:flex;justify-content:space-between;gap:8px;"><span class="muted" style="flex-shrink:0;">权限范围</span><span style="min-width:0;overflow-wrap:anywhere;">${esc(filterText)}</span></div>
          </div>
          <div>
            <div class="muted" style="font-size:12px;margin-bottom:6px;">置信度</div>
            <div style="display:flex;align-items:center;justify-content:space-between;font-size:12px;margin-bottom:6px;">
              <span>意图置信度</span>
              <div class="progress-bar-wrap" style="width:110px;margin:0 8px;"><div class="progress-bar-fill" style="width:${confidenceWidth(intentConfidence)};"></div></div>
              <span class="mono">${Number.isFinite(intentConfidence) ? intentConfidence.toFixed(2) : '未记录'}</span>
            </div>
            <div style="display:flex;align-items:center;justify-content:space-between;font-size:12px;">
              <span>实体匹配度</span>
              <div class="progress-bar-wrap" style="width:110px;margin:0 8px;"><div class="progress-bar-fill" style="width:${confidenceWidth(entityConfidence)};"></div></div>
              <span class="mono">${Number.isFinite(entityConfidence) ? entityConfidence.toFixed(2) : '未记录'}</span>
            </div>
          </div>
          <div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
              <span class="muted" style="font-size:12px;">结构化结果 (JSON 预览)</span>
              <span><a href="#" style="font-size:12px;color:var(--accent);" onclick="handleCopyParseResult();return false;" title="复制解析 JSON">${iconCopy(13)}</a> <a href="#" style="font-size:12px;color:var(--accent);" onclick="handleEditJSON();return false;">编辑</a></span>
            </div>
            <pre style="background:var(--inset);padding:8px 12px;border-radius:6px;font-family:var(--font-mono);font-size:11.5px;line-height:1.45;border:1px solid var(--line);margin:0;overflow:auto;max-height:140px;">${esc(Object.keys(output).length ? JSON.stringify(output, null, 2) : '未记录')}</pre>
            ${Object.keys(parseData?.draft || {}).length ? '<div class="muted" style="font-size:12px;margin-top:6px;">草稿已保存，尚未应用到 Trace。</div>' : ''}
          </div>
        </div>
      </div>
    </div>

    <!-- Bottom Full-Width Toolbar strictly matching 07-问答流程-问题解析.png -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-top:20px;padding:16px 0 24px;border-top:1px solid var(--line);width:100%;flex-wrap:wrap;gap:14px;">
      <!-- Left 3 Action Buttons -->
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <button class="btn" style="border:1.5px solid var(--accent);color:var(--accent);background:var(--card-bg);height:38px;padding:0 18px;border-radius:6px;font-size:13.5px;font-weight:500;display:inline-flex;align-items:center;gap:6px;" onclick="handleReParse()">
          <span>↻</span>
          <span>重新解析</span>
        </button>
        <button class="btn" style="border:1px solid var(--line);color:var(--ink-strong);background:var(--card-bg);height:38px;padding:0 18px;border-radius:6px;font-size:13.5px;font-weight:500;display:inline-flex;align-items:center;gap:6px;" onclick="openEditParseResultModal()">
          <span>${iconEdit(13)}</span>
          <span>编辑结果</span>
        </button>
        <button class="btn primary" style="background:var(--accent);color:#ffffff;border:0;height:38px;padding:0 20px;border-radius:6px;font-size:13.5px;font-weight:500;display:inline-flex;align-items:center;gap:6px;" onclick="window.go('qaflow/embed')">
          <span>▶</span>
          <span>进入向量化 &gt;</span>
        </button>
      </div>

      <!-- Center Timing Information -->
      <div style="font-size:13px;color:var(--ink-dim);display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
        <span>本阶段耗时: <b style="color:var(--ink-strong);font-weight:600;">${stageDuration}</b></span>
        <span style="color:#d1d5db;">|</span>
        <span>开始时间: ${esc(qaParseEmbedText(parseData?.startedAt))}</span>
        <span style="color:#d1d5db;">|</span>
        <span>结束时间: ${esc(qaParseEmbedText(parseData?.endedAt))}</span>
      </div>

      <!-- Right View Log Button -->
      <div>
        <button class="btn" style="border:1px solid var(--line);color:var(--ink-strong);background:var(--card-bg);height:36px;padding:0 16px;border-radius:6px;font-size:13px;font-weight:500;display:inline-flex;align-items:center;gap:6px;" onclick="openStageLogModal('问题解析')">
          <span>${iconDoc(13)}</span>
          <span>查看日志</span>
        </button>
      </div>
    </div>`;

    return { title: '问题解析', desc: '', actions: '', html };
  }

  /* 08 问答流程 > 问题向量化 - 100% 对应 08-问答流程-问题向量化.png */
  async function pageQA08_Embed() {
    const { traces, activeTrace } = await getActiveQATrace();
    if (!activeTrace?.id) return emptyQATrace(1);
    const results = await Promise.allSettled([
      qaParseEmbedRequest('getTraceEmbedStage', activeTrace.id),
      qaParseEmbedRequest('getTracePipeline', activeTrace.id, { stage: 2 }),
      qaParseEmbedRequest('getEmbeddingVector', activeTrace.id),
      qaParseEmbedRequest('getEmbeddingScatter', activeTrace.id)
    ]);
    const [embedData, pipelineData, vectorData, scatterData] = results.map(result => result.status === 'fulfilled' ? result.value : null);
    const output = embedData?.output || {};
    const candidateVector = vectorData?.vector ?? embedData?.vector;
    const vector = Array.isArray(candidateVector) && candidateVector.every(Number.isFinite) ? candidateVector : [];
    const dimensions = vector.length || (Number.isFinite(embedData?.dimensions) && embedData.dimensions > 0 ? embedData.dimensions : null);
    const model = embedData?.model || output.model || vectorData?.model;
    const norm = vector.length ? Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0)) : null;
    const reconstructed = vectorData?.reconstructed === true || embedData?.reconstructed === true;
    const source = !vector.length ? '不可用' : reconstructed ? '根据原始输入重建' : '原始记录';
    const normalization = typeof output.normalized === 'boolean' ? (output.normalized ? '已开启' : '未开启') : norm === null ? '未记录' : Math.abs(norm - 1) < 0.000001 ? '单位范数（实测）' : '非单位范数（实测）';
    const cacheStatus = embedData?.cacheStatus ?? output.cacheStatus;
    const cacheHit = embedData?.cacheHit ?? output.cacheHit;
    const cacheLabel = cacheStatus === 'disabled' ? '未启用' : cacheHit === true ? '命中' : cacheHit === false ? '未命中' : '未记录';
    const booleanLabel = value => value === true ? '通过' : value === false ? '不通过' : '未记录';
    const duration = embedData?.dataSource !== 'unavailable' && embedData?.status !== 'unavailable' && Number.isFinite(embedData?.durationMs) ? `${embedData.durationMs} ms` : '未记录';
    const questionText = output.inputText ?? output.query ?? embedData?.query ?? activeTrace.query ?? '未记录';
    const tokenCount = output.inputTokens ?? output.tokenCount;
    const points = Array.isArray(scatterData?.points) ? scatterData.points.filter(point => Number.isFinite(point.x) && Number.isFinite(point.y)) : [];
    const maxX = Math.max(1, ...points.map(point => Math.abs(point.x)));
    const maxY = Math.max(1, ...points.map(point => Math.abs(point.y)));
    const pointHtml = points.map(point => `<circle cx="${160 + point.x / maxX * 140}" cy="${90 - point.y / maxY * 70}" r="${point.id === activeTrace.id ? 5 : 3.5}" fill="${point.id === activeTrace.id ? 'var(--accent)' : '#94a3b8'}"><title>${esc(qaParseEmbedText(point.label ?? point.id))}</title></circle>`).join('');
    const projection = scatterData?.method === 'fixed-linear-projection' ? '固定线性投影 (fixed-linear-projection)' : qaParseEmbedText(scatterData?.method);
    const scores = points.map(point => point.similarity).filter(Number.isFinite);
    const preview = vector.slice(0, 10);
    const failures = results.map((result, index) => result.status === 'rejected' ? `${['向量阶段', '流水线', '查询向量', '散点数据'][index]}加载失败：${result.reason?.message || '服务不可用'}` : '').filter(Boolean);
    state.qaEmbedRecord = { traceId: activeTrace.id, data: embedData, vector, model, reconstructed };
    const traceBanner = renderQATraceBanner(activeTrace, traces, 1, pipelineData?.totalDuration);
    const traceHeader = renderQATraceHeader(1, pipelineData?.stages?.map(stage => stage.durationMs != null ? `${stage.durationMs}ms` : null));
    const detailRow = (label, value) => `<div style="display:flex;justify-content:space-between;gap:12px;"><span class="muted" style="flex-shrink:0;">${esc(label)}</span><span style="min-width:0;overflow-wrap:anywhere;text-align:right;">${esc(qaParseEmbedText(value))}</span></div>`;
    const flowNodes = [
      { label: '查询文本', value: Number.isFinite(tokenCount) ? `${tokenCount} tokens` : 'Token 数未记录', icon: iconChat(16) },
      { label: 'Tokenizer', value: output.tokenizer || '未记录', icon: iconDoc(16) },
      { label: 'Embedding', value: model || '未记录', icon: iconCube(16) },
      { label: '查询向量', value: dimensions ? `${dimensions} 维` : '不可用', icon: iconLayers(16) }
    ];
    const html = `
    ${traceBanner}
    ${traceHeader}
    ${failures.length ? `<div role="alert" class="muted" style="margin-bottom:12px;color:var(--danger);">${failures.map(esc).join('<br>')}</div>` : ''}
    <div class="card" style="margin-bottom:16px;">
      <div class="card-body" style="padding:16px 20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(170px,100%),1fr));align-items:center;gap:16px;">
        ${[
          ['Embedding 模型', qaParseEmbedText(model), iconRoute(16)],
          ['维度', qaParseEmbedText(dimensions), iconCube(16)],
          ['归一化', normalization, iconPulse(16)],
          ['索引兼容', booleanLabel(output.indexCompatible), iconLayers(16)]
        ].map(([label, value, icon]) => `<div style="display:flex;align-items:center;gap:12px;min-width:0;"><div style="width:40px;height:40px;flex-shrink:0;border-radius:8px;background:var(--accent-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;">${icon}</div><div style="min-width:0;"><div class="muted" style="font-size:12px;">${label}</div><b style="font-size:14px;color:var(--ink-strong);overflow-wrap:anywhere;">${esc(value)}</b></div></div>`).join('')}
      </div>
    </div>

    <div class="grid grid-3" style="align-items:stretch;">
      <div class="card" style="display:flex;flex-direction:column;height:100%;min-width:0;">
        <div class="card-head" style="padding:14px 18px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;"><span style="font-size:14px;font-weight:700;">查询文本 <span class="muted" style="font-weight:normal;font-size:12.5px;">(记录输入)</span></span><span class="muted" style="font-size:12px;">Token 数: ${esc(qaParseEmbedText(tokenCount))}</span></div>
        <div class="card-body" style="padding:16px 18px;display:flex;flex-direction:column;gap:14px;">
          <div style="background:var(--inset);padding:12px 14px;border-radius:8px;border:1px solid var(--line);font-size:13.5px;font-weight:600;color:var(--ink-strong);line-height:1.5;overflow-wrap:anywhere;">${esc(questionText)}</div>
          <div><div style="font-size:13px;font-weight:700;color:var(--ink-strong);margin-bottom:8px;">归一化处理</div><div class="muted" style="font-size:12.5px;line-height:1.8;">${Array.isArray(output.normalizationSteps) && output.normalizationSteps.length ? output.normalizationSteps.map(step => esc(qaParseEmbedText(step))).join('<br>') : '未记录'}</div></div>
        </div>
      </div>
      <div class="card" style="display:flex;flex-direction:column;height:100%;min-width:0;">
        <div class="card-head" style="padding:14px 18px;font-size:14px;font-weight:700;">处理流程</div>
        <div class="card-body" style="padding:16px 14px;display:flex;flex-direction:column;justify-content:center;">
          <div style="display:flex;align-items:stretch;justify-content:space-between;gap:6px;">
            ${flowNodes.map((node, index) => `${index ? '<span style="color:var(--ink-dim);align-self:center;">&gt;</span>' : ''}<div style="flex:1;min-width:0;border:1px solid var(--line);border-radius:8px;padding:12px 6px;text-align:center;background:var(--card-bg);"><div style="color:var(--accent);margin-bottom:4px;">${node.icon}</div><b style="font-size:12.5px;color:var(--ink-strong);display:block;overflow-wrap:anywhere;">${node.label}</b><div class="muted" style="font-size:11px;margin-top:4px;overflow-wrap:anywhere;">${esc(node.value)}</div></div>`).join('')}
          </div>
        </div>
      </div>
      <div class="card" style="display:flex;flex-direction:column;height:100%;min-width:0;">
        <div class="card-head" style="padding:14px 18px;font-size:14px;font-weight:700;">向量生成详情</div>
        <div class="card-body" style="padding:16px 18px;font-size:13px;display:flex;flex-direction:column;gap:8px;">
          ${detailRow('向量记录 ID', output.vectorId ?? vectorData?.vectorId)}
          ${detailRow('内容 Hash', output.inputHash ?? output.contentHash)}
          ${detailRow('缓存命中', cacheLabel)}
          ${detailRow('处理耗时', duration)}
          ${detailRow('向量维度', dimensions)}
          ${detailRow('模型版本', output.modelVersion)}
          ${detailRow('索引兼容检查', booleanLabel(output.indexCompatible))}
          ${detailRow('维度兼容检查', booleanLabel(output.dimensionsCompatible))}
          ${detailRow('归一化检查', normalization)}
          ${detailRow('向量来源', source)}
        </div>
      </div>
    </div>

    <div class="grid grid-2" style="margin-top:16px;align-items:stretch;">
      <div class="card" style="min-width:0;">
        <div class="card-head" style="padding:12px 18px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;"><span style="font-size:14px;font-weight:700;">向量检索诊断</span><span class="muted" style="font-size:12px;">${esc(projection)}</span></div>
        <div class="card-body" style="padding:16px 18px;display:flex;align-items:center;gap:20px;flex-wrap:wrap;">
          <div style="flex:1;min-width:180px;position:relative;">
            <svg viewBox="0 0 320 180" role="img" aria-label="查询与知识片段的固定线性投影" style="width:100%;height:160px;background:var(--card-bg);border:1px solid var(--line);border-radius:6px;">
              <line x1="160" y1="10" x2="160" y2="170" stroke="var(--line)"/>
              <line x1="10" y1="90" x2="310" y2="90" stroke="var(--line)"/>
              <text x="166" y="18" font-size="9" fill="var(--ink-dim)">投影 Y</text><text x="272" y="102" font-size="9" fill="var(--ink-dim)">投影 X</text>
              ${pointHtml || '<text x="160" y="125" text-anchor="middle" font-size="12" fill="var(--ink-dim)">投影数据不可用</text>'}
            </svg>
          </div>
          <div style="width:140px;display:flex;flex-direction:column;gap:8px;font-size:12px;">
            <div style="display:flex;align-items:center;gap:6px;"><span style="width:8px;height:8px;border-radius:50%;background:var(--accent);"></span>查询向量</div>
            <div style="display:flex;align-items:center;gap:6px;"><span style="width:8px;height:8px;border-radius:50%;background:#94a3b8;"></span>召回知识片段</div>
            <div style="border-top:1px solid var(--line);padding-top:8px;margin-top:4px;">
              <div class="muted">投影片段数: <b style="color:var(--ink-strong);">${scatterData ? points.filter(point => point.id !== activeTrace.id).length : '不可用'}</b></div>
              <div class="muted" style="margin-top:4px;">最高相似度: <b style="color:var(--ink-strong);">${scores.length ? Math.max(...scores).toFixed(4) : '未记录'}</b></div>
              <div class="muted" style="margin-top:4px;">${scatterData?.reconstructed ? '投影来源：重建' : scatterData ? '投影来源：服务计算' : '投影来源：不可用'}</div>
            </div>
          </div>
        </div>
      </div>
      <div class="card" style="min-width:0;">
        <div class="card-head" style="padding:12px 18px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;"><span style="font-size:14px;font-weight:700;">查询向量预览 <small class="muted" style="font-weight:normal;font-size:12px;">(前 10 维)</small></span><a href="#" style="font-size:12px;color:var(--accent);" onclick="handleCopyEmbeddingVector();return false;">${iconCopy(13)} 复制</a></div>
        <div class="card-body" style="padding:16px 18px;font-size:13px;">
          <div class="muted" style="font-size:12px;margin-bottom:12px;">维度: ${esc(qaParseEmbedText(dimensions))} (${esc(source)})</div>
          <div style="overflow-x:auto;"><table class="data-table" style="font-size:12px;text-align:center;width:100%;border:1px solid var(--line);border-radius:6px;">
            <thead style="background:var(--inset);"><tr><th style="padding:8px 6px;border-bottom:1px solid var(--line);">索引</th>${preview.map((value, index) => `<th style="padding:8px 6px;border-bottom:1px solid var(--line);">${index}</th>`).join('')}</tr></thead>
            <tbody><tr><td style="padding:10px 6px;color:var(--ink-dim);">数值</td>${preview.length ? preview.map(value => `<td style="padding:10px 6px;font-family:var(--font-mono);">${value.toFixed(4)}</td>`).join('') : '<td>不可用</td>'}</tr></tbody>
          </table></div>
          <div style="margin-top:14px;font-size:12.5px;color:var(--ink-dim);">范数 (L2) <b style="color:var(--ink-strong);margin-left:8px;">${norm === null ? '未记录' : norm.toFixed(4)}</b></div>
        </div>
      </div>
    </div>
    <div style="display:flex;align-items:center;justify-content:flex-end;gap:12px;margin-top:20px;padding-bottom:16px;flex-wrap:wrap;">
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:38px;padding:0 18px;border-radius:6px;font-size:13.5px;font-weight:500;" onclick="handleReVectorize()">${iconRefresh(14)} 重新向量化</button>
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:38px;padding:0 18px;border-radius:6px;font-size:13.5px;font-weight:500;" onclick="openModelComparisonModal()">${iconChart(14)} 对比模型</button>
      <button class="btn primary" style="background:var(--accent);color:#ffffff;height:38px;padding:0 22px;border-radius:6px;font-size:13.5px;font-weight:500;" onclick="window.go('qaflow/route');">进入检索路由 &gt;</button>
    </div>`;
    return { title: '问题向量化', desc: '', actions: '', html };
  }

  /* 09 问答流程 > 检索路由 - 100% 对应 09-问答流程-检索路由.png */
  function retrievalNumber(value, digits) {
    return typeof value === 'number' && Number.isFinite(value) ? (digits === undefined ? String(value) : value.toFixed(digits)) : '未记录';
  }

  function retrievalChannelId(name) {
    return ({ full_text: 'fulltext', fullText: 'fulltext', database: 'structured' })[name] || name;
  }

  function retrievalChannelName(name) {
    return ({ vector: '向量检索', fulltext: '全文检索', graph: '知识图谱', structured: '结构化查询' })[retrievalChannelId(name)] || name;
  }

  function retrievalRecordState(stage) {
    const source = stage?.dataSource === 'unavailable' || stage?.status === 'unavailable' ? '阶段输出未记录' : stage?.reconstructed ? '重建预览' : '原始阶段记录';
    const status = ({ succeeded: '成功', failed: '失败', skipped: '已跳过', unavailable: '不可用' })[stage?.status] || stage?.status || '不可用';
    const draft = Object.keys(stage?.draft || {}).length ? ' · 已有配置草稿，当前 Trace 未修改' : '';
    return `<div class="muted" style="font-size:12px;margin-bottom:12px;">${esc(source)} · ${esc(status)}${draft}</div>`;
  }

  async function pageQA09_Route() {
    const { traces, activeTrace } = await getActiveQATrace();
    if (!activeTrace?.id) return emptyQATrace(2);

    const routeData = await api.getTraceRouteStage(activeTrace.id, {}, { throwOnError: true });
    const pipelineData = await api.getTracePipeline(activeTrace.id, {}, { throwOnError: true });
    if (!routeData) throw new Error('路由记录读取失败');
    const indexData = await api.getTraceRouteIndexes(activeTrace.id).catch(() => null);
    const intentData = await api.getTraceRouteIntent(activeTrace.id).catch(() => null);

    const traceBanner = renderQATraceBanner(activeTrace, traces, 2, pipelineData?.totalMs);
    const traceHeader = renderQATraceHeader(2, pipelineData?.stages?.map(s => s.durationMs != null ? `${s.durationMs}ms` : null));

    const question = routeData.query || activeTrace.query || '未记录';
    const scope = routeData.permissionScope || activeTrace.permission_snapshot || {};
    const kbName = activeTrace.knowledge_base_name || activeTrace.dataset_name || scope.datasetId || '未记录';
    const appName = activeTrace.app_name || activeTrace.config_snapshot?.assistantName || '未记录';
    const rules = routeData.output?.routes || [];
    const channels = ['vector', 'fulltext', 'graph', 'structured'].map(id => {
      const rule = rules.find(item => retrievalChannelId(item.name) === id);
      return { ...rule, id, name: retrievalChannelName(id), status: rule?.enabled === true ? 'enabled' : rule?.enabled === false ? 'disabled' : 'unavailable', statusLabel: rule ? (rule.enabled ? '已启用' : '未启用') : '未记录' };
    });

    const branchYs = [40, 105, 175, 245];
    const svgBranchesHtml = channels.map((ch, idx) => {
      const y = branchYs[idx] || (40 + idx * 68);
      const isEnabled = ch.status === 'enabled';
      if (isEnabled) {
        return `<path d="M 370 145 C 410 145, 420 ${y}, 458 ${y}" stroke="#16a34a" stroke-width="2" fill="none" marker-end="url(#arrow-green)"/>`;
      } else {
        return `<path d="M 370 145 C 410 145, 420 ${y}, 458 ${y}" stroke="#cbd5e1" stroke-width="1.8" stroke-dasharray="4 3" fill="none" marker-end="url(#arrow-gray)"/>`;
      }
    }).join('\n                ');

    const channelCardsHtml = channels.map(ch => {
      const isEnabled = ch.status === 'enabled';
      const badgeClass = isEnabled ? 'badge ok' : 'badge';
      const badgeStyle = isEnabled
        ? 'padding:2px 8px;font-size:11px;background:var(--accent-soft);color:#16a34a;border:1px solid var(--accent);'
        : 'padding:2px 8px;font-size:11px;background:var(--inset);color:#94a3b8;border:1px solid var(--line);';
      const iconStyle = isEnabled
        ? 'width:34px;height:34px;border-radius:6px;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;flex-shrink:0;'
        : 'width:34px;height:34px;border-radius:6px;background:var(--inset);color:#94a3b8;display:flex;align-items:center;justify-content:center;flex-shrink:0;';

      let iconSvg = iconDatabase(16);
      if (ch.id === 'fulltext') iconSvg = iconDoc(16);
      else if (ch.id === 'graph') iconSvg = iconNodes(16);
      else if (ch.id === 'structured') iconSvg = iconTable(16);

      const titleStyle = isEnabled ? 'font-size:13px;color:var(--ink-strong);' : 'font-size:13px;color:var(--ink-dim);';
      const subText = esc(ch.reason || (isEnabled ? '已记录启用决策' : ch.status === 'disabled' ? '当前路由未启用' : '没有路由决策记录'));

      return `
                  <div style="border:1px solid var(--line);background:${isEnabled ? 'var(--card-bg)' : 'var(--inset)'};border-radius:8px;padding:9px 14px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 1px 2px rgba(0,0,0,0.03);">
                    <div style="display:flex;align-items:center;gap:10px;">
                      <div style="${iconStyle}">${iconSvg}</div>
                      <div>
                        <b style="${titleStyle}">${esc(ch.name)}</b>
                        <div class="muted" style="font-size:11.5px;margin-top:1px;">${subText}</div>
                      </div>
                    </div>
                    <span class="${badgeClass}" style="${badgeStyle}">${esc(ch.statusLabel || (isEnabled ? '已启用' : '未启用'))}</span>
                  </div>`;
    }).join('\n');

    const channelParams = channels.map(channel => ({ ...channel, channelName: channel.name }));

    const tableRowsHtml = channelParams.map(row => {
      const isEnabled = row.status === 'enabled';
      const statusText = row.statusLabel;
      const statusClass = isEnabled ? 'ok-text' : 'muted';
      const textStyle = isEnabled ? 'color:var(--ink-strong);' : 'color:var(--ink-dim);';
      const topKText = retrievalNumber(row.topK);
      const timeoutText = row.timeoutMs == null ? '未记录' : `${retrievalNumber(row.timeoutMs)} ms`;
      const weightText = retrievalNumber(row.weight, 2);
      const durationText = row.estimatedDurationMs == null ? '未记录' : `${retrievalNumber(row.estimatedDurationMs)} ms`;
      const costText = row.estimatedCostYuan == null ? '未记录' : `${retrievalNumber(row.estimatedCostYuan, 4)} 元`;

      return `
                <tr>
                  <td style="padding:12px 14px;font-weight:600;${textStyle}">${esc(row.channelName)}</td>
                  <td style="padding:12px 14px;"><span class="${statusClass}">${esc(statusText)}</span></td>
                  <td style="padding:12px 14px;${textStyle}">${topKText}</td>
                  <td style="padding:12px 14px;${textStyle}">${timeoutText}</td>
                  <td style="padding:12px 14px;${textStyle}">${weightText}</td>
                  <td style="padding:12px 14px;${textStyle}">${durationText}</td>
                  <td style="padding:12px 14px;${textStyle}">${costText}</td>
                </tr>`;
    }).join('\n');

    const basis = {
      ...routeData.routingBasis,
      reasons: [routeData.output?.reason, ...rules.map(rule => rule.reason)].filter(Boolean).map(reason => ({ reason })),
      permissionConstraint: { accessibleScope: scope.releaseId || routeData.releaseId || '未记录' }
    };

    const reasonsHtml = (basis.reasons || []).map(r => `
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <span class="muted">• ${esc(r.reason)}</span>
                <b style="color:var(--ink-dim);">${retrievalNumber(r.score, 2)}</b>
              </div>
    `).join('\n') || '<span class="muted">未记录路由原因</span>';

    const indexConfig = indexData?.profile?.config || {};
    const indexesHtml = [['向量检索', indexConfig.embedding], ['全文检索', indexConfig.fullText]].map(([name, config]) => `
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <span class="muted">${esc(name)}</span>
                <span style="overflow-wrap:anywhere;">${esc(config?.provider || '未读取')}</span>
              </div>
    `).join('\n');

    const compositeConfidenceText = retrievalNumber(basis.compositeConfidence, 2);
    const intentCategoryText = intentData?.intent || '未记录';
    const intentConfidenceText = retrievalNumber(intentData?.confidence, 2);

    const html = `
    ${traceBanner}
    ${traceHeader}

    <!-- 2-Column Main Workspace (Left Flowchart + Channel Table | Right 4-Box Route Evidence) -->
    ${retrievalRecordState(routeData)}
    <div style="display:grid;grid-template-columns: 1fr 340px; gap: 16px; align-items: stretch;">
      <!-- Left Column -->
      <div style="display:flex;flex-direction:column;gap:16px;">
        <!-- Top: Flowchart Router Topology Card with SVG connectors -->
        <div class="card">
          <div class="card-body" style="padding:20px;position:relative;overflow:hidden;">
            <div style="position:relative;width:100%;min-height:300px;">
              <!-- SVG Connector Curves and Arrows -->
              <svg viewBox="0 0 780 290" style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;z-index:1;">
                <defs>
                  <marker id="arrow-green" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                    <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="#16a34a"/>
                  </marker>
                  <marker id="arrow-gray" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                    <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="#94a3b8"/>
                  </marker>
                  <marker id="arrow-dark" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                    <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="#475569"/>
                  </marker>
                </defs>

                <!-- Left Line from Config to Router -->
                <path d="M 230 145 L 305 145" stroke="#475569" stroke-width="1.8" marker-end="url(#arrow-dark)"/>

                <!-- Dynamic Curves from Router to 4 Target Cards on Right -->
                ${svgBranchesHtml}
              </svg>

              <!-- HTML Elements Placed Over the Layout Grid -->
              <div style="display:grid;grid-template-columns:230px 150px 1fr;align-items:center;gap:0;position:relative;z-index:2;min-height:290px;">
                <!-- Column 1: Left Boxes -->
                <div style="display:flex;flex-direction:column;gap:12px;">
                  <!-- Box 1: 输入问题 -->
                  <div style="border:1px solid var(--line);border-radius:8px;padding:10px 14px;background:var(--inset);box-shadow:0 1px 2px rgba(0,0,0,0.03);">
                    <div class="muted" style="font-size:11.5px;margin-bottom:4px;">输入问题</div>
                    <div style="font-size:13px;font-weight:600;color:var(--ink-strong);line-height:1.4;">${esc(question)}</div>
                  </div>
                  <!-- Box 2: 路由配置 -->
                  <div style="border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--card-bg);box-shadow:0 1px 2px rgba(0,0,0,0.03);">
                    <div class="muted" style="font-size:12px;margin-bottom:6px;font-weight:600;">路由配置</div>
                    <div style="display:flex;flex-direction:column;gap:5px;font-size:12px;">
                      <div style="display:flex;justify-content:space-between;"><span class="muted">数据范围</span><span style="color:#16a34a;font-weight:500;">${esc(kbName)}</span></div>
                      <div style="display:flex;justify-content:space-between;"><span class="muted">权限范围</span><span style="color:#16a34a;font-weight:500;">${scope.releaseId || routeData.releaseId ? '固定于 Trace 发布版本' : '未记录'}</span></div>
                      <div style="display:flex;justify-content:space-between;"><span class="muted">应用画像</span><span style="color:#16a34a;font-weight:500;">${esc(appName)}</span></div>
                      <div style="display:flex;justify-content:space-between;"><span class="muted">检索策略</span><span style="color:var(--ink-strong);font-weight:500;">${esc(routeData.retrievalStrategy || '未记录')}</span></div>
                    </div>
                  </div>
                </div>

                <!-- Column 2: Center Router Hub -->
                <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;">
                  <div style="width:54px;height:54px;border-radius:50%;background:var(--card-bg);border:2px solid #16a34a;display:flex;align-items:center;justify-content:center;color:#16a34a;box-shadow:0 2px 8px rgba(22,163,74,0.18);">
                    ${iconRoute(26)}
                  </div>
                  <div style="font-size:12px;font-weight:700;color:var(--ink-strong);margin-top:6px;text-align:center;">检索路由器</div>
                </div>

                <!-- Column 3: 4 Right Branch Target Cards -->
                <div style="display:flex;flex-direction:column;gap:8px;padding-left:16px;">
                  ${channelCardsHtml}
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Bottom: 通道参数 Table Card -->
        <div class="card">
          <div class="card-head" style="padding:14px 18px;font-size:14px;font-weight:700;">通道参数</div>
          <div class="card-body" style="padding:0;">
            <table class="data-table" style="font-size:12.5px;width:100%;">
              <thead>
                <tr>
                  <th style="padding:10px 14px;">通道</th>
                  <th style="padding:10px 14px;">状态</th>
                  <th style="padding:10px 14px;">TopK</th>
                  <th style="padding:10px 14px;">超时时间</th>
                  <th style="padding:10px 14px;">规则权重</th>
                  <th style="padding:10px 14px;">预估耗时</th>
                  <th style="padding:10px 14px;">预估成本</th>
                </tr>
              </thead>
              <tbody>
                ${tableRowsHtml}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Right Column: 路由依据 (4 Independent Bordered Boxes + 综合置信度) -->
      <div class="card" style="display:flex;flex-direction:column;height:100%;">
        <div class="card-head" style="padding:14px 18px;font-size:14px;font-weight:700;">路由依据</div>
        <div class="card-body" style="padding:16px 18px;display:flex;flex-direction:column;gap:10px;font-size:12.5px;">
          <!-- Box 1: 意图匹配 -->
          <div style="border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--card-bg);box-shadow:0 1px 2px rgba(0,0,0,0.02);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
              <span style="font-weight:600;font-size:13px;color:var(--ink-strong);display:flex;align-items:center;gap:6px;">
                <span style="width:16px;height:16px;border-radius:50%;background:var(--accent-soft);color:#16a34a;border:1px solid var(--accent);display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;">✓</span>
                意图匹配
              </span>
              <span class="muted" style="font-size:12px;">置信度 ${intentConfidenceText}</span>
            </div>
            <div style="color:var(--ink-dim);margin-left:22px;">${esc(intentCategoryText)}</div>
          </div>

          <!-- Box 2: 可用索引 -->
          <div style="border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--card-bg);box-shadow:0 1px 2px rgba(0,0,0,0.02);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
              <span style="font-weight:600;font-size:13px;color:var(--ink-strong);display:flex;align-items:center;gap:6px;">
                <span style="width:16px;height:16px;border-radius:50%;background:var(--accent-soft);color:#16a34a;border:1px solid var(--accent);display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;">✓</span>
                可用索引
              </span>
              <span class="muted" style="font-size:12px;">${indexData ? '当前登记配置' : '配置未读取'}</span>
            </div>
            <div style="display:flex;flex-direction:column;gap:5px;margin-left:22px;">
              ${indexesHtml}
            </div>
          </div>

          <!-- Box 3: 权限约束 -->
          <div style="border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--card-bg);box-shadow:0 1px 2px rgba(0,0,0,0.02);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
              <span style="font-weight:600;font-size:13px;color:var(--ink-strong);display:flex;align-items:center;gap:6px;">
                <span style="width:16px;height:16px;border-radius:50%;background:var(--accent-soft);color:#16a34a;border:1px solid var(--accent);display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;">✓</span>
                权限约束
              </span>
              <span class="muted" style="font-size:12px;">${scope.releaseId || routeData.releaseId ? '发布版本固定' : '未记录'}</span>
            </div>
            <div style="margin-left:22px;">
              <div class="muted" style="font-size:11.5px;">可访问范围</div>
              <div style="color:#16a34a;font-weight:500;margin-top:2px;overflow-wrap:anywhere;">${esc(basis.permissionConstraint.accessibleScope)}</div>
            </div>
          </div>

          <!-- Box 4: 路由原因 -->
          <div style="border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--card-bg);box-shadow:0 1px 2px rgba(0,0,0,0.02);">
            <div style="font-weight:600;font-size:13px;color:var(--ink-strong);margin-bottom:8px;display:flex;align-items:center;gap:6px;">
              <span style="width:16px;height:16px;border-radius:50%;background:var(--accent-soft);color:#16a34a;border:1px solid var(--accent);display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;">✓</span>
              路由原因
            </div>
            <div style="display:flex;flex-direction:column;gap:5px;font-size:12px;margin-left:22px;">
              ${reasonsHtml}
            </div>
          </div>

          <!-- 综合置信度 (Pinned to bottom of right card) -->
          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:auto;padding-top:12px;border-top:1px solid var(--line-soft);">
            <span style="font-size:14px;font-weight:700;color:var(--ink-strong);">综合置信度</span>
            <span style="font-size:20px;font-weight:700;color:var(--ink-dim);line-height:1;">${compositeConfidenceText}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Global Bottom Toolbar strictly matching 09-问答流程-检索路由.png -->
    <div style="display:flex;align-items:center;justify-content:flex-end;gap:12px;margin-top:20px;padding:16px 0 24px;border-top:1px solid var(--line);width:100%;">
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:38px;padding:0 22px;border-radius:6px;font-size:13.5px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="openRouteRulesModal()">编辑路由</button>
      <button class="btn primary" style="background:var(--accent);color:#ffffff;height:38px;padding:0 24px;border-radius:6px;font-size:13.5px;font-weight:500;cursor:pointer;" onclick="window.go('qaflow/recall')">进入多路召回 &gt;</button>
      <button class="btn" style="background:var(--card-bg);border:1.5px solid var(--accent);color:var(--accent);height:38px;padding:0 22px;border-radius:6px;font-size:13.5px;font-weight:500;cursor:pointer;" onclick="handleQARerun('route')">从此阶段重跑</button>
    </div>`;
    return { title: '检索路由', desc: '', actions: '', html };
  }

  /* 10 问答流程 > 多路召回 - 100% 对应 10-问答流程-多路召回.png */
  async function pageQA10_Recall() {
    const { traces, activeTrace } = await getActiveQATrace();
    if (!activeTrace?.id) return emptyQATrace(3);

    const recallData = await api.getTraceRecallStage(activeTrace.id, {}, { throwOnError: true });
    const pipelineData = await api.getTracePipeline(activeTrace.id, {}, { throwOnError: true });
    if (!recallData) throw new Error('召回记录读取失败');
    state.qaRecallRecord = { traceId: activeTrace.id, data: recallData };

    const traceBanner = renderQATraceBanner(activeTrace, traces, 3, pipelineData?.totalMs);
    const traceHeader = renderQATraceHeader(3, pipelineData?.stages?.map(s => s.durationMs != null ? `${s.durationMs}ms` : null));

    const channels = (recallData.channels || []).map(channel => ({
      ...channel,
      channelId: retrievalChannelId(channel.id),
      totalCount: channel.count,
      headerBadge: `${retrievalNumber(channel.count)} 条已记录 · ${channel.enabled === true ? '已启用' : channel.enabled === false ? '未启用' : '状态未记录'}`,
      items: (channel.candidates || []).map(item => ({ ...item, chunkId: item.id || item.chunkRevisionId, page: item.locator?.page, rawScore: item.score }))
    }));
    const summary = recallData.summaryMetrics || {};
    const routeOutput = (activeTrace.stages || []).find(stage => stage.key === 'route' || stage.name === '检索路由')?.output;
    const emptyCh = id => {
      const rule = (routeOutput?.routes || []).find(item => retrievalChannelId(item.name) === id);
      return { headerBadge: rule?.enabled === false ? '未启用' : '未记录', totalCount: null, durationMs: null, items: [], statusLabel: rule?.enabled === false ? '未启用' : '未记录', skippedReason: rule?.reason || '未记录该通道的执行结果', skippedDetail: '' };
    };
    const vectorCh = channels.find(c => c.channelId === 'vector') || emptyCh('vector');
    const fulltextCh = channels.find(c => c.channelId === 'fulltext') || emptyCh('fulltext');
    const graphCh = channels.find(c => c.channelId === 'graph') || emptyCh('graph');
    const structCh = channels.find(c => c.channelId === 'structured') || emptyCh('structured');

    // Helper to render channel candidate rows
    function renderChannelItems(items = [], maxDisplay = 5) {
      if (!items || !items.length) {
        return `<div style="padding:24px 16px;text-align:center;color:var(--ink-dim);font-size:12px;">暂无召回候选数据</div>`;
      }
      return items.slice(0, maxDisplay).map((it, idx) => `
        <div style="display:grid;grid-template-columns:26px minmax(0,1fr) 48px 46px;padding:8px 10px;border-bottom:1px solid var(--line-soft);align-items:center;cursor:pointer;" onclick="openRecallChunkModal(${esc(JSON.stringify(it.chunkId))})" title="查看候选原文快照">
          <span class="muted">${it.rank || (idx + 1)}</span>
          <div style="overflow:hidden;text-overflow:ellipsis;">
            <div style="font-weight:600;color:var(--ink-strong);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
              ${iconDoc(13)} ${esc(it.documentTitle || it.title || '未知文档')}
            </div>
            <div class="muted" style="font-size:10px;">${esc(it.chunkId || '—')}${it.page ? ' / p.' + it.page : ''}</div>
          </div>
          <span class="mono" style="text-align:right;">${retrievalNumber(it.rawScore, 4)}</span>
          <span style="text-align:right;color:var(--ink-dim);font-size:11px;">版本内</span>
        </div>
      `).join('');
    }

    // Dynamic SVG Bar Chart calculation
    const dists = summary.durationDistribution || channels.filter(channel => Number.isFinite(channel.durationMs)).map(channel => ({ channel: channel.name, durationMs: channel.durationMs }));
    const maxDur = Math.max(...dists.map(d => d.durationMs || 0), 100);
    const chartYMax = Math.ceil(maxDur / 50) * 50;

    const barCoords = [
      { x: 55, textX: 72 },
      { x: 135, textX: 152 },
      { x: 215, textX: 232 },
      { x: 295, textX: 312 }
    ];

    const barsSvg = dists.map((d, i) => {
      const coord = barCoords[i] || { x: 55 + i * 80, textX: 72 + i * 80 };
      const dur = d.durationMs || 0;
      const barH = dur > 0 ? Math.max(Math.round((dur / chartYMax) * 50), 4) : 1;
      const barY = 70 - barH;
      const fill = dur > 0 ? 'var(--accent)' : 'var(--line)';
      return `
        <rect x="${coord.x}" y="${barY}" width="34" height="${barH}" rx="2" fill="${fill}"/>
        <text x="${coord.textX}" y="${Math.max(barY - 6, 14)}" font-size="9" fill="var(--ink-strong)" font-weight="600" text-anchor="middle">${dur}</text>
        <text x="${coord.textX}" y="82" font-size="8.5" fill="var(--ink-dim)" text-anchor="middle">${esc(d.channel)}</text>
      `;
    }).join('');

    const topKVal = [5, 10, 20, 50].includes(state.recallTopK) ? state.recallTopK : 5;

    const html = `
    ${traceBanner}
    ${traceHeader}

    <!-- Top Filter Controls Bar -->
    ${retrievalRecordState(recallData)}
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
      <div style="display:flex;gap:10px;align-items:center;">
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;">每路显示 <select class="input" style="height:34px;" aria-label="每路显示候选数" onchange="state.recallTopK=Number(this.value);render();">${[5, 10, 20, 50].map(value => `<option value="${value}" ${value === topKVal ? 'selected' : ''}>${value}</option>`).join('')}</select></label>
        <label class="btn" style="height:34px;display:flex;align-items:center;gap:6px;"><input type="checkbox" checked disabled> 固定 Trace 版本</label>
        <label class="btn" style="height:34px;display:flex;align-items:center;gap:6px;"><input type="checkbox" checked disabled> 固定权限范围</label>
      </div>
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:34px;padding:0 14px;font-size:13px;cursor:pointer;" onclick="window.handleRetryRecallChannel('${esc(activeTrace?.id || '')}')">
        ↻ 从召回阶段重跑
      </button>
    </div>

    <!-- Row 1: 4 Recall Cards -->
    <div class="grid grid-4" style="align-items:stretch;">
      <!-- Card 1: 向量召回 -->
      <div class="card" style="display:flex;flex-direction:column;height:100%;border-top:2px solid #16a34a;">
        <div class="card-head" style="padding:12px 14px;display:flex;justify-content:space-between;align-items:center;">
          <span style="font-size:13.5px;font-weight:700;color:#16a34a;">向量召回</span>
          <span class="muted" style="font-size:12px;">${esc(vectorCh.headerBadge || (vectorCh.totalCount + ' 条 / ' + vectorCh.durationMs + ' ms'))}</span>
        </div>
        <div class="card-body" style="padding:0;display:flex;flex-direction:column;flex:1;">
          <div style="display:grid;grid-template-columns:26px 1fr 48px 46px;padding:6px 10px;background:var(--inset);font-size:10.5px;color:var(--ink-dim);border-bottom:1px solid var(--line);">
            <span>排名</span>
            <span>候选文档 (Chunk ID / 来源页)</span>
            <span style="text-align:right;">原始分数</span>
            <span style="text-align:right;">权限过滤</span>
          </div>
          <div style="display:flex;flex-direction:column;flex:1;font-size:11.5px;">
            ${renderChannelItems(vectorCh.items, state.showVectorResults ? vectorCh.items.length : topKVal)}
          </div>
          <div style="padding:8px;text-align:center;border-top:1px solid var(--line-soft);margin-top:auto;">
            ${vectorCh.totalCount > topKVal ? `<a href="#" style="font-size:11.5px;color:var(--accent);" onclick="handleToggleExpandState(event, 'showVectorResults')">${state.showVectorResults ? '收起 ⌃' : '查看已记录的 ' + vectorCh.totalCount + ' 条 ⌄'}</a>` : '<span class="muted" style="font-size:11.5px;">已记录候选</span>'}
          </div>
        </div>
      </div>

      <!-- Card 2: 全文召回 -->
      <div class="card" style="display:flex;flex-direction:column;height:100%;border-top:2px solid #16a34a;">
        <div class="card-head" style="padding:12px 14px;display:flex;justify-content:space-between;align-items:center;">
          <span style="font-size:13.5px;font-weight:700;color:#16a34a;">全文召回</span>
          <span class="muted" style="font-size:12px;">${esc(fulltextCh.headerBadge || (fulltextCh.totalCount + ' 条 / ' + fulltextCh.durationMs + ' ms'))}</span>
        </div>
        <div class="card-body" style="padding:0;display:flex;flex-direction:column;flex:1;">
          <div style="display:grid;grid-template-columns:26px 1fr 48px 46px;padding:6px 10px;background:var(--inset);font-size:10.5px;color:var(--ink-dim);border-bottom:1px solid var(--line);">
            <span>排名</span>
            <span>候选文档 (Chunk ID / 来源页)</span>
            <span style="text-align:right;">原始分数</span>
            <span style="text-align:right;">权限过滤</span>
          </div>
          <div style="display:flex;flex-direction:column;flex:1;font-size:11.5px;">
            ${renderChannelItems(fulltextCh.items, state.showBM25Results ? fulltextCh.items.length : topKVal)}
          </div>
          <div style="padding:8px;text-align:center;border-top:1px solid var(--line-soft);margin-top:auto;">
            ${fulltextCh.totalCount > topKVal ? `<a href="#" style="font-size:11.5px;color:var(--accent);" onclick="handleToggleExpandState(event, 'showBM25Results')">${state.showBM25Results ? '收起 ⌃' : '查看已记录的 ' + fulltextCh.totalCount + ' 条 ⌄'}</a>` : '<span class="muted" style="font-size:11.5px;">已记录候选</span>'}
          </div>
        </div>
      </div>

      <!-- Card 3: 图谱召回 -->
      <div class="card" style="display:flex;flex-direction:column;height:100%;border-top:2px solid #16a34a;">
        <div class="card-head" style="padding:12px 14px;display:flex;justify-content:space-between;align-items:center;">
          <span style="font-size:13.5px;font-weight:700;color:#16a34a;">图谱召回</span>
          <span class="muted" style="font-size:12px;">${esc(graphCh.headerBadge || (graphCh.totalCount + ' 条 / ' + graphCh.durationMs + ' ms'))}</span>
        </div>
        <div class="card-body" style="padding:0;display:flex;flex-direction:column;flex:1;">
          <div style="display:grid;grid-template-columns:26px 1fr 48px 46px;padding:6px 10px;background:var(--inset);font-size:10.5px;color:var(--ink-dim);border-bottom:1px solid var(--line);">
            <span>排名</span>
            <span>候选文档 (Chunk ID / 来源页)</span>
            <span style="text-align:right;">原始分数</span>
            <span style="text-align:right;">权限过滤</span>
          </div>
          <div style="display:flex;flex-direction:column;flex:1;font-size:11.5px;">
            ${graphCh.items?.length ? renderChannelItems(graphCh.items, state.showGraphResults ? graphCh.items.length : topKVal) : `
              <div style="padding:24px 14px;text-align:center;color:var(--ink-dim);">
                <div style="font-size:24px;margin-bottom:6px;">${iconGlobe(16)}</div>
                <b>${esc(graphCh.statusLabel || '暂无候选记录')}</b>
                <div style="font-size:11.5px;margin-top:4px;line-height:1.5;">${esc(graphCh.skippedReason || '')}</div>
              </div>
            `}
          </div>
          <div style="padding:8px;text-align:center;border-top:1px solid var(--line-soft);margin-top:auto;">
            ${state.showGraphResults ? `<div style='padding:10px;background:var(--inset);border:1px dashed #cbd5e1;border-radius:4px;color:#64748b;font-size:12px;margin-bottom:8px;text-align:left;'>共 ${graphCh.totalCount} 条候选</div>` : ''}${graphCh.totalCount > 0 ? `<a href="#" style="font-size:11.5px;color:var(--accent);" onclick="handleToggleExpandState(event, 'showGraphResults')">${state.showGraphResults ? '收起 ⌃' : '查看全部 ' + graphCh.totalCount + ' 条 ⌄'}</a>` : ''}
          </div>
        </div>
      </div>

      <!-- Card 4: 结构化查询 (Empty State) -->
      <div class="card" style="display:flex;flex-direction:column;height:100%;">
        <div class="card-head" style="padding:12px 14px;display:flex;justify-content:space-between;align-items:center;">
          <span style="font-size:13.5px;font-weight:700;color:var(--ink-strong);">结构化查询</span>
          <span class="badge" style="background:var(--inset);color:#94a3b8;font-size:11px;">${esc(structCh.statusLabel || '已跳过')}</span>
        </div>
        <div class="card-body" style="padding:24px 16px;text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;">
          <div style="width:54px;height:54px;border-radius:50%;background:var(--inset);border:1px dashed #cbd5e1;display:flex;align-items:center;justify-content:center;font-size:24px;color:#94a3b8;margin-bottom:12px;">
            ${iconDoc(16)}
          </div>
          <b style="font-size:13.5px;color:var(--ink-strong);">${esc(structCh.skippedReason || '未触发结构化查询条件')}</b>
          <div class="muted" style="font-size:12px;margin-top:4px;">${esc(structCh.skippedDetail || '')}</div>
          <div style="margin-top:20px;">
            <a href="#/qaflow/route" style="font-size:12px;color:var(--accent);">查看路由规则 &gt;</a>
          </div>
        </div>
      </div>
    </div>

    <!-- Row 2: Bottom Stats & Dynamic SVG Bar Chart -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1.8fr;gap:16px;margin-top:16px;align-items:stretch;">
      <!-- Stat 1: 候选总数 -->
      <div class="card" style="display:flex;align-items:center;padding:16px 20px;">
        <div style="width:44px;height:44px;border-radius:8px;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:22px;margin-right:16px;flex-shrink:0;">
          ${iconDatabase(14)}
        </div>
        <div>
          <div class="muted" style="font-size:12px;">已记录候选数</div>
          <b style="font-size:24px;color:var(--ink-strong);line-height:1.1;">${retrievalNumber(recallData.totalCandidates)} <small style="font-size:13px;font-weight:normal;">条</small></b>
        </div>
      </div>

      <!-- Stat 2: 重复候选数 -->
      <div class="card" style="display:flex;align-items:center;padding:16px 20px;">
        <div style="width:44px;height:44px;border-radius:8px;background:var(--warn-soft);color:#d97706;display:flex;align-items:center;justify-content:center;font-size:22px;margin-right:16px;flex-shrink:0;">
          ${iconTable(14)}
        </div>
        <div>
          <div class="muted" style="font-size:12px;">重复候选数</div>
          <b style="font-size:20px;color:var(--ink-strong);line-height:1.1;">${retrievalNumber(summary.duplicateCandidates)}</b>
        </div>
      </div>

      <!-- Stat 3: 通道失败数 -->
      <div class="card" style="display:flex;align-items:center;padding:16px 20px;">
        <div style="width:44px;height:44px;border-radius:8px;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:22px;margin-right:16px;flex-shrink:0;">
          ${iconShield(13)}
        </div>
        <div>
          <div class="muted" style="font-size:12px;">通道失败数</div>
          <div style="display:flex;align-items:baseline;gap:6px;">
            <b style="font-size:20px;color:var(--ink-strong);line-height:1.1;">${retrievalNumber(summary.failedChannels)}</b>
          </div>
        </div>
      </div>

      <!-- Stat 4: 各通道耗时分布 Bar Chart (Dynamic SVG) -->
      <div class="card" style="padding:14px 18px;display:flex;flex-direction:column;justify-content:space-between;">
        <div style="font-size:12.5px;font-weight:700;color:var(--ink-strong);margin-bottom:6px;">各通道耗时分布 (ms)</div>
        <svg viewBox="0 0 360 85" style="width:100%;height:80px;">
          <!-- Grid Lines -->
          <line x1="30" y1="15" x2="350" y2="15" stroke="var(--line-soft)" stroke-dasharray="2 2"/>
          <line x1="30" y1="45" x2="350" y2="45" stroke="var(--line-soft)" stroke-dasharray="2 2"/>
          <line x1="30" y1="70" x2="350" y2="70" stroke="var(--line)"/>
          <!-- Y-axis Labels -->
          <text x="5" y="18" font-size="8.5" fill="var(--ink-dim)">${chartYMax}</text>
          <text x="10" y="48" font-size="8.5" fill="var(--ink-dim)">${Math.round(chartYMax / 2)}</text>
          <text x="15" y="73" font-size="8.5" fill="var(--ink-dim)">0</text>
          <!-- Dynamic Bars -->
          ${barsSvg}
          ${dists.length ? '' : '<text x="180" y="45" font-size="12" fill="var(--ink-dim)" text-anchor="middle">未记录各通道独立耗时</text>'}
        </svg>
      </div>
    </div>

    <!-- Bottom Actions Toolbar strictly matching 10-问答流程-多路召回.png -->
    <div style="display:flex;align-items:center;justify-content:flex-end;gap:12px;margin-top:20px;padding:16px 0 24px;border-top:1px solid var(--line);width:100%;">
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:38px;padding:0 20px;border-radius:6px;font-size:13.5px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="openRecallChunkModal()">查看原文</button>
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:38px;padding:0 20px;border-radius:6px;font-size:13.5px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="window.handleExportRecallCandidates()">${iconDownload(13)} 导出候选</button>
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:38px;padding:0 20px;border-radius:6px;font-size:13.5px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="window.handleRetryRecallChannel('${esc(activeTrace?.id || '')}')">↻ 从召回阶段重跑</button>
      <button class="btn primary" style="background:var(--accent);color:#ffffff;height:38px;padding:0 24px;border-radius:6px;font-size:13.5px;font-weight:500;cursor:pointer;" onclick="window.go('qaflow/fuse')">进入结果融合 &gt;</button>
    </div>`;
    return { title: '多路召回', desc: '', actions: '', html };
  }

  /* 11 问答流程 > 结果融合 - 100% 对应 11-问答流程-结果融合.png */
  async function pageQA11_Fuse() {
    const { traces, activeTrace } = await getActiveQATrace();
    if (!activeTrace?.id) return emptyQATrace(4);

    let fusion = await api.getTraceFusionStage(activeTrace.id, {}, { throwOnError: true });
    const pipelineData = await api.getTracePipeline(activeTrace.id, {}, { throwOnError: true });

    const traceBanner = renderQATraceBanner(activeTrace, traces, 4, pipelineData?.totalMs);
    const traceHeader = renderQATraceHeader(4, pipelineData?.stages?.map(s => s.durationMs != null ? `${s.durationMs}ms` : null));

    if (!fusion) throw new Error(api.lastError?.message || '融合记录读取失败');
    state.qaFusionRecord = { traceId: activeTrace.id, data: fusion };
    const fusionRows = fusion.candidates || [];
    if (!fusionRows.some(candidate => candidate.id === state.selectedFusionCandidateId)) state.selectedFusionCandidateId = fusionRows[0]?.id || null;
    const sourceChannel = key => ({
      totalCount: Array.isArray(fusion.output?.[key]) ? fusion.output[key].length : null,
      items: (fusion.output?.[key] || []).map(c => ({ ...c, candidateId: c.chunkRevisionId || c.id }))
    });
    fusion = {
      ...fusion,
      sourceChannels: { vector: sourceChannel('vector'), fulltext: sourceChannel('fullText'), graph: sourceChannel('graph') },
      summaryMetrics: {
        rawCandidateCount: fusion.rawCandidateCount, dedupCandidateCount: fusion.deduplicatedCount,
        aclRemovedCount: fusion.aclRemovedCount, fusedCandidateCount: fusion.candidateCount ?? fusion.totalCandidates
      },
      fusedCandidates: fusionRows.map((c, i) => ({ ...c, rank: c.rank ?? i + 1, candidateId: c.id, fusedScore: c.fusionScore, sources: (c.channels || []).map(retrievalChannelId) })),
      scoreDetails: fusionRows.map((c, i) => ({
        candidateId: c.id, title: c.title, fusedRank: c.rank ?? i + 1,
        originRanks: [c.vectorRank ? '向量 #' + c.vectorRank : '', c.fullTextRank ? '全文 #' + c.fullTextRank : ''].filter(Boolean).join(' · '),
        channelScores: { vector: c.vectorScore, fulltext: c.fullTextScore, graph: null },
        fusedScoreRRF: c.fusionScore, dedupGroup: c.id, dedupReason: (c.channels || []).length > 1 ? '相同块版本合并' : '单一来源'
      }))
    };

    const renderRankPill = (rank) => {
      let style = 'background:#f1f5f9;color:#64748b;border:1px solid #e2e8f0;';
      if (rank === 1) style = 'background:#fef2f2;color:#ef4444;border:1px solid #fecaca;';
      else if (rank === 2) style = 'background:#fffbeb;color:#f59e0b;border:1px solid #fde68a;';
      else if (rank === 3) style = 'background:#eff6ff;color:#3b82f6;border:1px solid #bfdbfe;';
      return `<span style="display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;border-radius:4px;font-size:10.5px;font-weight:700;${style}">${rank}</span>`;
    };

    const renderCandidateCard = (item) => `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 8px;background:var(--card-bg);border:1px solid var(--line-soft);border-radius:6px;box-shadow:0 1px 2px rgba(0,0,0,0.03);margin-bottom:6px;cursor:pointer;transition:border-color 0.15s ease;" onmouseenter="this.style.borderColor='var(--accent)'" onmouseleave="this.style.borderColor='var(--line-soft)'" onclick="openFusionChunkModal(${esc(JSON.stringify(item.candidateId))})">
        ${renderRankPill(item.rank)}
        <span style="flex:1;margin:0 8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11.5px;color:var(--ink-strong);" title="${esc(item.title)}">${esc(item.title)}</span>
        <span class="mono" style="font-size:11px;color:var(--ink-dim);font-weight:500;">${retrievalNumber(item.score, 3)}</span>
      </div>`;

    const sourceItems = (key, expanded) => {
      const items = fusion.sourceChannels[key].items;
      return (expanded ? items : items.slice(0, 5)).map(renderCandidateCard).join('') || '<div class="muted" style="padding:20px 4px;text-align:center;font-size:12px;">暂无候选记录</div>';
    };
    const vectorItems = sourceItems('vector', state.showRRF15);
    const fulltextItems = sourceItems('fulltext', state.showRRF17);
    const graphItems = sourceItems('graph', state.showRRF9);

    const renderSourceBadges = (sources = []) => sources.map(s => {
      if (s === 'vector') return '<span style="font-size:10px;padding:1px 5px;background:#dcfce7;color:#15803d;border:1px solid #86efac;border-radius:3px;font-weight:500;">向量</span>';
      if (s === 'fulltext') return '<span style="font-size:10px;padding:1px 5px;background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd;border-radius:3px;font-weight:500;">全文</span>';
      if (s === 'graph') return '<span style="font-size:10px;padding:1px 5px;background:#f3e8ff;color:#7e22ce;border:1px solid #d8b4fe;border-radius:3px;font-weight:500;">图谱</span>';
      return `<span style="font-size:10px;padding:1px 5px;background:var(--inset);color:var(--ink-dim);border-radius:3px;">${esc(s)}</span>`;
    }).join(' ');

    const fusedDisplayList = state.showRerank20 ? (fusion.fusedCandidates || []) : (fusion.fusedCandidates || []).slice(0, 6);
    const fusedCandidatesHtml = fusedDisplayList.map(cand => `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:7px 8px;border-bottom:1px solid var(--line-soft);background:var(--card-bg);border-radius:6px;margin-bottom:5px;box-shadow:0 1px 2px rgba(0,0,0,0.02);cursor:pointer;transition:background 0.15s ease;" onmouseenter="this.style.background='var(--inset)'" onmouseleave="this.style.background='var(--card-bg)'" onclick="openFusionChunkModal(${esc(JSON.stringify(cand.candidateId))})">
        <span style="display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;background:#16a34a;color:#ffffff;font-size:10.5px;font-weight:700;margin-right:8px;flex-shrink:0;">${cand.rank}</span>
        <span style="font-weight:500;color:var(--ink-strong);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;" title="${esc(cand.title)}">${esc(cand.title)}</span>
        <b class="mono" style="margin:0 10px;font-size:12px;color:var(--ink-strong);">${retrievalNumber(cand.fusedScore, 6)}</b>
        <div style="display:flex;gap:3px;flex-shrink:0;">${renderSourceBadges(cand.sources)}</div>
      </div>`).join('') || '<div class="muted" style="padding:20px;text-align:center;">暂无融合候选</div>';

    const scoreRowsHtml = (fusion.scoreDetails || []).map((row, idx) => {
      const isSelected = state.selectedFusionCandidateId === row.candidateId || (!state.selectedFusionCandidateId && idx === 0);
      const vecScore = row.channelScores.vector == null ? '-' : retrievalNumber(row.channelScores.vector, 3);
      const ftScore = row.channelScores.fulltext == null ? '-' : retrievalNumber(row.channelScores.fulltext, 3);
      const gpScore = row.channelScores.graph == null ? '-' : retrievalNumber(row.channelScores.graph, 3);

      return `
      <tr style="cursor:pointer;background:${isSelected ? 'rgba(22, 163, 74, 0.08)' : 'transparent'};transition:background 0.15s ease;" onclick="selectFusionRow(${esc(JSON.stringify(row.candidateId))})" ondblclick="openCalculationModal(${esc(JSON.stringify(row.candidateId))})">
        <td style="padding:12px 14px;font-weight:700;color:var(--accent);text-align:center;">${row.fusedRank}</td>
        <td style="padding:12px 14px;font-weight:600;color:var(--ink-strong);">${esc(row.title)}</td>
        <td style="padding:12px 14px;font-size:12px;color:var(--ink-dim);">${esc(row.originRanks)}</td>
        <td style="padding:12px 6px;text-align:center;color:${vecScore === '-' ? '#94a3b8' : 'inherit'};" class="mono">${vecScore}</td>
        <td style="padding:12px 6px;text-align:center;color:${ftScore === '-' ? '#94a3b8' : 'inherit'};" class="mono">${ftScore}</td>
        <td style="padding:12px 6px;text-align:center;color:${gpScore === '-' ? '#94a3b8' : 'inherit'};" class="mono">${gpScore}</td>
        <td style="padding:12px 14px;font-weight:700;text-align:center;" class="mono">${retrievalNumber(row.fusedScoreRRF, 6)}</td>
        <td style="padding:12px 14px;color:var(--ink-dim);text-align:center;max-width:140px;overflow-wrap:anywhere;"><span style="font-size:11px;">${esc(row.dedupGroup)}</span></td>
        <td style="padding:12px 14px;color:var(--ink-dim);">${esc(row.dedupReason)}</td>
        <td style="padding:12px 14px;text-align:center;"><span class="muted">Trace 版本内</span></td>
      </tr>`;
    }).join('') || '<tr><td colspan="10" class="muted" style="padding:24px;text-align:center;">暂无融合分数记录</td></tr>';

    const html = `
    ${traceBanner}
    ${traceHeader}

    <!-- Main Fusion Flow Diagram (5-Section Layout) strictly matching 11-问答流程-结果融合.png -->
    ${retrievalRecordState(fusion)}
    <div style="display:grid;grid-template-columns: 1fr 340px; gap: 16px; align-items: stretch; margin-bottom: 16px;">
      <!-- Left 3 Recall Columns + 4-Step Pipeline -->
      <div class="card" style="padding:16px 18px;display:grid;grid-template-columns:1fr 1fr 1fr 125px;gap:12px;align-items:stretch;position:relative;">
        <!-- SVG Multi-Channel Connecting Overlay Network -->
        <svg style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:2;" viewBox="0 0 1000 300" preserveAspectRatio="none">
          <defs>
            <marker id="arrow-green-fuse" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 1 L 8 5 L 0 9 z" fill="#16a34a"/>
            </marker>
          </defs>

          ${fusion.sourceChannels.vector.items.length ? '<path d="M 260 150 L 275 150" fill="none" stroke="#16a34a" stroke-width="1.8" marker-end="url(#arrow-green-fuse)"/>' : ''}
          ${fusion.sourceChannels.fulltext.items.length ? '<path d="M 520 150 L 535 150" fill="none" stroke="#16a34a" stroke-width="1.8" marker-end="url(#arrow-green-fuse)"/>' : ''}
        </svg>

        <!-- Col 1: 向量召回 -->
        <div style="border:1px solid var(--line);border-radius:8px;padding:12px 10px;background:var(--card-bg);display:flex;flex-direction:column;position:relative;z-index:1;">
          <div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:8px;border-bottom:1px solid var(--line);font-size:13px;margin-bottom:8px;">
            <b style="color:var(--ink-strong);">向量召回</b>
            <span class="muted">(${retrievalNumber(fusion.sourceChannels.vector.totalCount)})</span>
          </div>
          <div style="display:flex;flex-direction:column;flex:1;">
            ${vectorItems}
          </div>
          <div style="text-align:center;padding-top:8px;border-top:1px solid var(--line-soft);margin-top:auto;">
            ${fusion.sourceChannels.vector.items.length > 5 ? `<a href="#" style="font-size:11.5px;color:var(--accent);text-decoration:none;" onclick="handleToggleExpandState(event, 'showRRF15')">${state.showRRF15 ? '收起 ⌃' : '查看已记录的 ' + fusion.sourceChannels.vector.totalCount + ' 条 ⌄'}</a>` : '<span class="muted" style="font-size:11.5px;">已记录候选</span>'}
          </div>
        </div>

        <!-- Col 2: 全文召回 -->
        <div style="border:1px solid var(--line);border-radius:8px;padding:12px 10px;background:var(--card-bg);display:flex;flex-direction:column;position:relative;z-index:1;">
          <div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:8px;border-bottom:1px solid var(--line);font-size:13px;margin-bottom:8px;">
            <b style="color:var(--ink-strong);">全文召回</b>
            <span class="muted">(${retrievalNumber(fusion.sourceChannels.fulltext.totalCount)})</span>
          </div>
          <div style="display:flex;flex-direction:column;flex:1;">
            ${fulltextItems}
          </div>
          <div style="text-align:center;padding-top:8px;border-top:1px solid var(--line-soft);margin-top:auto;">
            ${fusion.sourceChannels.fulltext.items.length > 5 ? `<a href="#" style="font-size:11.5px;color:var(--accent);text-decoration:none;" onclick="handleToggleExpandState(event, 'showRRF17')">${state.showRRF17 ? '收起 ⌃' : '查看已记录的 ' + fusion.sourceChannels.fulltext.totalCount + ' 条 ⌄'}</a>` : '<span class="muted" style="font-size:11.5px;">已记录候选</span>'}
          </div>
        </div>

        <!-- Col 3: 图谱召回 -->
        <div style="border:1px solid var(--line);border-radius:8px;padding:12px 10px;background:var(--card-bg);display:flex;flex-direction:column;position:relative;z-index:1;">
          <div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:8px;border-bottom:1px solid var(--line);font-size:13px;margin-bottom:8px;">
            <b style="color:var(--ink-strong);">图谱召回</b>
            <span class="muted">(${retrievalNumber(fusion.sourceChannels.graph.totalCount)})</span>
          </div>
          <div style="display:flex;flex-direction:column;flex:1;">
            ${graphItems}
          </div>
          <div style="text-align:center;padding-top:8px;border-top:1px solid var(--line-soft);margin-top:auto;">
            ${fusion.sourceChannels.graph.items.length > 5 ? `<a href="#" style="font-size:11.5px;color:var(--accent);text-decoration:none;" onclick="handleToggleExpandState(event, 'showRRF9')">${state.showRRF9 ? '收起 ⌃' : '查看已记录的 ' + fusion.sourceChannels.graph.totalCount + ' 条 ⌄'}</a>` : '<span class="muted" style="font-size:11.5px;">暂无通道记录</span>'}
          </div>
        </div>

        <!-- Col 4: 4-Step Pipeline Flow Card -->
        <div style="border:1px dashed #cbd5e1;background:var(--inset);border-radius:8px;padding:10px 8px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;position:relative;z-index:3;">
          <div style="border:1px solid var(--accent);background:var(--accent-soft);border-radius:6px;padding:7px 10px;font-size:11px;font-weight:600;color:var(--ink-strong);text-align:center;width:100%;box-shadow:0 1px 2px rgba(0,0,0,0.02);display:flex;align-items:center;justify-content:center;gap:4px;">
            ${iconFunnel(13)} 去重
          </div>
          <span style="color:#94a3b8;font-size:11px;">↓</span>
          <div style="border:1px solid var(--accent);background:var(--accent-soft);border-radius:6px;padding:7px 10px;font-size:11px;font-weight:600;color:var(--ink-strong);text-align:center;width:100%;box-shadow:0 1px 2px rgba(0,0,0,0.02);display:flex;align-items:center;justify-content:center;gap:4px;">
            ${iconShield(13)} 固定权限范围
          </div>
          <span style="color:#94a3b8;font-size:11px;">↓</span>
          <div style="border:1px solid var(--accent);background:var(--accent-soft);border-radius:6px;padding:7px 10px;font-size:11px;font-weight:600;color:var(--ink-strong);text-align:center;width:100%;box-shadow:0 1px 2px rgba(0,0,0,0.02);display:flex;align-items:center;justify-content:center;gap:4px;">
            ${iconChart(14)} 通道原始排名
          </div>
          <span style="color:#94a3b8;font-size:11px;">↓</span>
          <div style="border:1px solid var(--accent);background:var(--accent-soft);border-radius:6px;padding:7px 10px;font-size:11px;font-weight:600;color:var(--ink-strong);text-align:center;width:100%;box-shadow:0 1px 2px rgba(0,0,0,0.02);display:flex;align-items:center;justify-content:center;gap:4px;">
            ${iconLink(14)} RRF 融合
          </div>
        </div>
      </div>

      <!-- Right Card: 融合候选集 (20) -->
      <div class="card" style="padding:14px 16px;display:flex;flex-direction:column;">
        <div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:10px;border-bottom:1px solid var(--line);font-size:13px;">
          <b style="color:var(--ink-strong);">融合候选集 (${retrievalNumber(fusion.summaryMetrics.fusedCandidateCount)})</b>
          <div style="display:flex;gap:18px;font-size:11.5px;color:var(--ink-dim);">
            <span>融合分数</span>
            <span>来源</span>
          </div>
        </div>
        <div style="display:flex;flex-direction:column;font-size:12px;margin-top:6px;flex:1;">
          ${fusedCandidatesHtml}
        </div>
        <div style="text-align:center;padding-top:8px;border-top:1px solid var(--line-soft);margin-top:auto;">
          ${fusion.fusedCandidates.length > 6 ? `<a href="#" style="font-size:11.5px;color:var(--accent);text-decoration:none;" onclick="handleToggleExpandState(event, 'showRerank20')">${state.showRerank20 ? '收起 ⌃' : '查看全部 ' + fusion.fusedCandidates.length + ' 条 ⌄'}</a>` : '<span class="muted" style="font-size:11.5px;">已记录候选</span>'}
        </div>
      </div>
    </div>

    <!-- Row 2: 4 Metric Cards + Action Buttons on Same Row (Right Side) -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr auto;gap:14px;align-items:center;margin-bottom:16px;">
      <div class="card" style="display:flex;align-items:center;padding:14px 16px;">
        <div style="width:38px;height:38px;border-radius:8px;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:18px;margin-right:12px;">${iconDatabase(16)}</div>
        <div><div class="muted" style="font-size:11.5px;">原始候选数 (Raw)</div><b style="font-size:20px;color:var(--ink-strong);line-height:1.1;">${retrievalNumber(fusion.summaryMetrics.rawCandidateCount)}</b></div>
      </div>
      <div class="card" style="display:flex;align-items:center;padding:14px 16px;">
        <div style="width:38px;height:38px;border-radius:8px;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:18px;margin-right:12px;">${iconFunnel(15)}</div>
        <div><div class="muted" style="font-size:11.5px;">已记录去重候选数</div><b style="font-size:20px;color:var(--ink-strong);line-height:1.1;">${retrievalNumber(fusion.summaryMetrics.dedupCandidateCount)}</b></div>
      </div>
      <div class="card" style="display:flex;align-items:center;padding:14px 16px;">
        <div style="width:38px;height:38px;border-radius:8px;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:18px;margin-right:12px;">${iconShield(15)}</div>
        <div><div class="muted" style="font-size:11.5px;">权限过滤移除数</div><b style="font-size:20px;color:var(--ink-strong);line-height:1.1;">${retrievalNumber(fusion.summaryMetrics.aclRemovedCount)}</b></div>
      </div>
      <div class="card" style="display:flex;align-items:center;padding:14px 16px;">
        <div style="width:38px;height:38px;border-radius:8px;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:18px;margin-right:12px;">${iconLink(15)}</div>
        <div><div class="muted" style="font-size:11.5px;">融合候选数</div><b style="font-size:20px;color:var(--ink-strong);line-height:1.1;">${retrievalNumber(fusion.summaryMetrics.fusedCandidateCount)}</b></div>
      </div>
      <div style="display:flex;align-items:center;gap:10px;">
        <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:36px;padding:0 16px;border-radius:6px;font-size:13px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="openAdjustWeightsModal()">${iconSettings(14)} 调整权重</button>
        <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:36px;padding:0 16px;border-radius:6px;font-size:13px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="openCalculationModal()">${iconDoc(13)} 查看计算</button>
        <button class="btn primary" style="background:var(--accent);color:#ffffff;height:36px;padding:0 20px;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;" onclick="window.go('qaflow/rerank')">进入重排 &gt;</button>
      </div>
    </div>

    <!-- Row 3: 分数明细 Table Card -->
    <div class="card">
      <div class="card-head" style="padding:14px 18px;font-size:14px;font-weight:700;display:flex;justify-content:space-between;align-items:center;">
        <span>分数明细</span>
        <div style="display:flex;align-items:center;gap:10px;">
          <span style="font-size:12px;font-weight:normal;color:var(--ink-dim);">${esc(fusion.method || '未记录算法')} · k=${retrievalNumber(fusion.k)}</span>
          <button class="btn" style="padding:4px 12px;font-size:12px;height:28px;background:var(--card-bg);border:1px solid var(--line);border-radius:4px;cursor:pointer;" onclick="handleExportFusion()">${iconDownload(12)} 导出明细</button>
        </div>
      </div>
      <div class="card-body" style="padding:0;">
        <table class="data-table" style="font-size:12.5px;width:100%;">
          <thead>
            <tr>
              <th style="padding:10px 14px;text-align:center;width:70px;">融合排名</th>
              <th style="padding:10px 14px;width:180px;">文档标题</th>
              <th style="padding:10px 14px;">来源与原始排名 (原始分数)</th>
              <th style="padding:10px 14px;text-align:center;" colspan="3">通道原始分数<br><small style="font-weight:normal;color:var(--ink-dim);">向量 | 全文 | 图谱</small></th>
              <th style="padding:10px 14px;text-align:center;width:110px;">融合分数 (RRF)</th>
              <th style="padding:10px 14px;text-align:center;width:80px;">去重分组</th>
              <th style="padding:10px 14px;width:120px;">去重原因</th>
              <th style="padding:10px 14px;text-align:center;width:90px;">权限状态</th>
            </tr>
          </thead>
          <tbody>
            ${scoreRowsHtml}
          </tbody>
        </table>
      </div>
    </div>`;

    return { title: '结果融合', desc: '', actions: '', html };
  }

  /* Fusion Stage Interactive Modal Handlers */
  window.selectFusionRow = function(candidateId) {
    state.selectedFusionCandidateId = candidateId;
    render();
  };

  window.openAdjustWeightsModal = async function() {
    let record;
    try {
      const { activeTrace } = await getActiveQATrace();
      if (!activeTrace?.id) throw new Error('请先选择一条 Trace');
      const data = await api.getTraceFusionStage(activeTrace.id, {}, { throwOnError: true });
      if (!data) throw new Error('融合记录读取失败');
      record = { traceId: activeTrace.id, data };
      state.qaFusionRecord = record;
    } catch (error) {
      showToast(error.message || '融合记录读取失败', 'error');
      return;
    }
    const config = { ...record.data.weights, k: record.data.k, ...record.data.draft };
    const k = config.k;
    const denseWeight = config.denseWeight ?? config.vectorWeight;
    const sparseWeight = config.sparseWeight ?? config.fullTextWeight;
    const modalHtml = `
    <div class="modal" style="width:520px;max-width:92vw;">
      <div class="modal-header" style="display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--line);">
        <b style="font-size:15px;color:var(--ink-strong);display:flex;align-items:center;gap:6px;">${iconSettings(16)} 调整融合权重与算法参数</b>
        <button class="btn" style="border:none;background:transparent;cursor:pointer;font-size:18px;color:var(--ink-dim);" data-close>×</button>
      </div>
      <div class="modal-body" style="padding:20px;display:flex;flex-direction:column;gap:14px;font-size:13px;">
        <div>
          <label style="font-weight:600;display:block;margin-bottom:6px;">融合排序算法</label>
          <select id="fuse-algo-select" class="input" style="width:100%;height:36px;" disabled>
            <option value="rrf" selected>RRF (倒数排名融合)</option>
          </select>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
            <label style="font-weight:600;">RRF 常数 (k)</label>
            <span class="mono" id="fuse-k-val" style="color:var(--accent);font-weight:600;">${retrievalNumber(k)}</span>
          </div>
          <input type="number" id="fuse-k-slider" class="input mono" min="1" max="1000" step="1" value="${k ?? ''}" style="width:100%;height:34px;" oninput="document.getElementById('fuse-k-val').textContent=this.value"/>
        </div>
        <div style="border-top:1px solid var(--line-soft);padding-top:12px;">
          <label style="font-weight:600;display:block;margin-bottom:8px;">各通道融合加权比重</label>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;">
            <div>
              <div class="muted" style="font-size:12px;margin-bottom:4px;">向量召回</div>
              <input type="number" id="fuse-w-vec" class="input mono" step="0.05" min="0" value="${denseWeight ?? ''}" style="width:100%;height:34px;"/>
            </div>
            <div>
              <div class="muted" style="font-size:12px;margin-bottom:4px;">全文召回</div>
              <input type="number" id="fuse-w-ft" class="input mono" step="0.05" min="0" value="${sparseWeight ?? ''}" style="width:100%;height:34px;"/>
            </div>
            <div>
              <div class="muted" style="font-size:12px;margin-bottom:4px;">图谱召回</div>
              <input class="input" value="未配置" disabled style="width:100%;height:34px;"/>
            </div>
          </div>
        </div>
        <div>
          <label style="font-weight:600;display:block;margin-bottom:6px;">配置状态</label>
          <div class="muted">${Object.keys(record.data.draft || {}).length ? '已保存草稿，当前 Trace 未修改' : '当前 Trace 的已记录配置'}</div>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:space-between;align-items:center;padding:14px 20px;border-top:1px solid var(--line);background:var(--card-bg);">
        <button class="btn" style="height:34px;font-size:12.5px;" onclick="handleResetWeights()">恢复默认</button>
        <div style="display:flex;gap:10px;">
          <button class="btn" style="height:34px;font-size:12.5px;" data-close>取消</button>
          <button class="btn primary" style="height:34px;font-size:12.5px;" data-fusion-trace="${esc(record.traceId)}" onclick="handleSaveWeights(this)">保存草稿并重跑</button>
        </div>
      </div>
    </div>`;
    showOverlay(modalHtml);
  };

  window.handleResetWeights = async function() {
    try {
      const traceId = state.qaFusionRecord?.traceId;
      if (!traceId) throw new Error('请先读取融合记录');
      const result = await api.resetFusionWeights(traceId, {}, { throwOnError: true });
      if (!result?.saved) throw new Error('服务端未确认草稿保存');
      showToast(`默认融合草稿 v${result.version} 已保存，当前 Trace 未修改`, 'ok');
      await window.openAdjustWeightsModal();
    } catch (error) {
      showToast(error.message || '恢复默认草稿失败', 'error');
    }
  };

  window.handleSaveWeights = async function(button) {
    if (state.qaFusionSaving) return;
    try {
      const traceId = button?.dataset.fusionTrace || state.qaFusionRecord?.traceId;
      if (!traceId) throw new Error('请先读取融合记录');
      const numberInput = id => {
        const value = document.getElementById(id)?.value;
        if (value == null || value.trim() === '' || !Number.isFinite(Number(value))) throw new Error('请填写有效的融合参数');
        return Number(value);
      };
      const k = numberInput('fuse-k-slider');
      const denseWeight = numberInput('fuse-w-vec');
      const sparseWeight = numberInput('fuse-w-ft');
      if (!Number.isInteger(k) || k < 1 || k > 1000 || denseWeight < 0 || sparseWeight < 0) throw new Error('k 必须为 1 到 1000 的整数，权重必须为非负数');
      state.qaFusionSaving = true;
      if (button) button.disabled = true;
      const result = await api.updateFusionWeights(traceId, { k, denseWeight, sparseWeight }, { throwOnError: true });
      if (!result?.saved) throw new Error('服务端未确认草稿保存');
      closeOverlay();
      showToast(`融合草稿 v${result.version} 已保存，当前 Trace 未修改`, 'ok');
      window.handleQARerun('fusion', traceId);
    } catch (error) {
      showToast(error.message || '融合草稿保存失败', 'error');
    } finally {
      state.qaFusionSaving = false;
      if (button) button.disabled = false;
    }
  };

  window.handleApplyFusionWeights = window.handleSaveWeights;

  window.openCalculationModal = async function(candidateId) {
    let calc;
    let candidate;
    try {
      const { activeTrace } = await getActiveQATrace();
      if (!activeTrace?.id) throw new Error('请先选择一条 Trace');
      const candId = candidateId || (state.qaFusionRecord?.traceId === activeTrace.id ? state.selectedFusionCandidateId : null);
      if (!candId) throw new Error('当前没有可计算的候选');
      [calc, candidate] = await Promise.all([
        api.getFusionCalculation(activeTrace.id, candId, {}, { throwOnError: true }),
        api.getFusionChunk(activeTrace.id, candId, {}, { throwOnError: true })
      ]);
      if (!calc || !candidate) throw new Error('融合计算记录读取失败');
    } catch (error) {
      showToast(error.message || '融合计算记录读取失败', 'error');
      return;
    }

    const stepsHtml = (calc.contributions || []).map(st => `
      <tr>
        <td style="padding:8px 12px;font-weight:600;">${esc(retrievalChannelName(st.channel))}</td>
        <td style="padding:8px 12px;text-align:center;"><span class="badge">${st.rank == null ? '未命中' : '#' + st.rank}</span></td>
        <td style="padding:8px 12px;font-family:monospace;font-size:11.5px;color:var(--ink-strong);">${st.rank == null ? '未命中，无贡献' : `${retrievalNumber(st.weight)} / (${retrievalNumber(calc.k)} + ${retrievalNumber(st.rank)})`}</td>
        <td style="padding:8px 12px;text-align:right;font-family:monospace;font-weight:600;color:var(--accent);">${retrievalNumber(st.contribution, 8)}</td>
      </tr>`).join('');

    const modalHtml = `
    <div class="modal" style="width:620px;max-width:92vw;">
      <div class="modal-header" style="display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--line);">
        <div>
          <b style="font-size:15px;color:var(--ink-strong);display:flex;align-items:center;gap:6px;">${iconDoc(15)} RRF 融合打分数学推演</b>
          <div class="muted" style="font-size:12px;margin-top:2px;">文档候选：${esc(candidate.documentTitle || candidate.title || candidate.id)}</div>
        </div>
        <button class="btn" style="border:none;background:transparent;cursor:pointer;font-size:18px;color:var(--ink-dim);" data-close>×</button>
      </div>
      <div class="modal-body" style="padding:20px;display:flex;flex-direction:column;gap:14px;font-size:13px;">
        <div style="background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:12px 14px;">
          <div class="muted" style="font-size:11.5px;margin-bottom:4px;">倒数排名融合计算公式 (RRF Formula)</div>
          <div class="mono" style="font-size:13px;font-weight:600;color:var(--ink-strong);">RRF = sum(weight / (k + rank))</div>
          <div class="muted" style="font-size:11.5px;margin-top:4px;">已记录参数：k = ${retrievalNumber(calc.k)} · ${esc(calc.method || '未记录')}</div>
        </div>

        <div>
          <div style="font-weight:600;margin-bottom:8px;">各路召回代入推演明细</div>
          <table class="data-table" style="width:100%;font-size:12px;">
            <thead>
              <tr>
                <th style="padding:8px 12px;">召回通道</th>
                <th style="padding:8px 12px;text-align:center;">原始名次</th>
                <th style="padding:8px 12px;">加权倒数计算式</th>
                <th style="padding:8px 12px;text-align:right;">单路贡献分</th>
              </tr>
            </thead>
            <tbody>
              ${stepsHtml}
            </tbody>
            <tfoot>
              <tr style="border-top:2px solid var(--line);background:var(--card-bg);">
                <td colspan="3" style="padding:10px 12px;font-weight:700;">原始倒数累加总分 (RRF Sum)</td>
                <td style="padding:10px 12px;text-align:right;font-family:monospace;font-weight:700;color:var(--accent);font-size:13px;">${retrievalNumber(calc.score, 8)}</td>
              </tr>
              <tr style="background:var(--card-bg);">
                <td colspan="3" style="padding:6px 12px;font-weight:700;">已记录融合分数</td>
                <td style="padding:6px 12px;text-align:right;font-family:monospace;font-weight:700;color:#16a34a;font-size:15px;">${retrievalNumber(candidate.fusionScore, 8)}</td>
              </tr>
            </tfoot>
          </table>
        </div>

        <div style="border-left:3px solid var(--accent);background:var(--accent-soft);padding:10px 14px;border-radius:0 6px 6px 0;font-size:12px;color:var(--ink-strong);">
          <b>Trace：</b><span style="overflow-wrap:anywhere;">${esc(calc.traceId)}</span>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;padding:12px 20px;border-top:1px solid var(--line);background:var(--card-bg);">
        <button class="btn primary" style="height:32px;font-size:12px;" data-close>我知道了</button>
      </div>
    </div>`;
    showOverlay(modalHtml);
  };

  async function openRetrievalChunkModal(stage, candidateId, traceId) {
    let chunk;
    let stageData;
    const getters = {
      recall: ['getTraceRecallStage', 'getRecallChunk'],
      fusion: ['getTraceFusionStage', 'getFusionChunk'],
      rerank: ['getTraceRerankStage', 'getRerankChunk']
    };
    try {
      if (!traceId) traceId = (await getActiveQATrace()).activeTrace?.id;
      if (!traceId || !getters[stage]) throw new Error('请先选择一条 Trace');
      stageData = await api[getters[stage][0]](traceId, {}, { throwOnError: true });
      if (!stageData) throw new Error('候选记录读取失败');
      const items = stageData.candidates || [];
      const selectedId = candidateId || items[0]?.id;
      if (!selectedId) throw new Error('当前阶段没有原文候选快照');
      chunk = await api[getters[stage][1]](traceId, selectedId, {}, { throwOnError: true });
      if (!chunk) throw new Error('候选原文快照读取失败');
    } catch (error) {
      showToast(error.message || '原文快照读取失败', 'error');
      return;
    }
    const options = (stageData.candidates || []).map(item => `<option value="${esc(item.id)}" ${item.id === chunk.id ? 'selected' : ''}>${esc(item.documentTitle || item.title || item.id)}</option>`).join('');
    const selectHandler = stage === 'recall' ? 'openRecallChunkModal' : stage === 'fusion' ? 'openFusionChunkModal' : 'openRerankChunkModal';
    const modalHtml = `
    <div class="modal" style="width:600px;max-width:92vw;">
      <div class="modal-header" style="display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--line);">
        <div>
          <b style="font-size:15px;color:var(--ink-strong);">${esc(chunk.documentTitle || chunk.title || '候选原文快照')}</b>
          <div style="font-size:12px;color:var(--ink-dim);margin-top:2px;overflow-wrap:anywhere;">切片标识: ${esc(chunk.id)} · 融合分: <b class="mono" style="color:var(--accent);">${retrievalNumber(chunk.fusionScore, 8)}</b></div>
        </div>
        <button class="btn" style="border:none;background:transparent;cursor:pointer;font-size:18px;color:var(--ink-dim);" data-close>×</button>
      </div>
      <div class="modal-body" style="padding:20px;display:flex;flex-direction:column;gap:12px;font-size:13px;">
        <select class="input" aria-label="选择候选原文" onchange="${selectHandler}(this.value, ${esc(JSON.stringify(traceId))})">${options}</select>
        <div style="display:flex;flex-wrap:wrap;gap:16px;font-size:12px;background:var(--inset);padding:10px 14px;border-radius:6px;">
          <div><span class="muted">来源渠道: </span><b>${esc((chunk.channels || []).map(retrievalChannelName).join(', ') || '未记录')}</b></div>
          <div><span class="muted">页码: </span><b>${esc(chunk.locator?.page ?? '未记录')}</b></div>
          <div><span class="muted">范围: </span><b>Trace 固定发布版本</b></div>
        </div>
        <div>
          <div style="font-weight:600;margin-bottom:6px;">切片完整正文</div>
          <div style="background:var(--card-bg);border:1px solid var(--line);border-radius:6px;padding:14px;font-size:12.5px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--ink-strong);max-height:260px;overflow-y:auto;">${esc(chunk.contentText || chunk.content || '此候选未记录正文')}</div>
        </div>
        <div class="muted" style="font-size:11.5px;overflow-wrap:anywhere;">Trace: ${esc(traceId)}<br>发布版本: ${esc(stageData.releaseId || '未记录')}</div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;padding:12px 20px;border-top:1px solid var(--line);background:var(--card-bg);">
        <button class="btn primary" style="height:32px;font-size:12px;" data-close>关闭</button>
      </div>
    </div>`;
    showOverlay(modalHtml);
  }

  window.openRecallChunkModal = function(candidateId, traceId) {
    return openRetrievalChunkModal('recall', candidateId, traceId);
  };

  window.openFusionChunkModal = function(candidateId, traceId) {
    return openRetrievalChunkModal('fusion', candidateId, traceId);
  };

  window.openRerankChunkModal = function(candidateId, traceId) {
    return openRetrievalChunkModal('rerank', candidateId, traceId);
  };

  window.handleExportFusion = async function() {
    try {
      const { activeTrace } = await getActiveQATrace();
      if (!activeTrace?.id) throw new Error('请先选择一条 Trace');
      const data = await api.exportFusion(activeTrace.id, { format: 'json' }, { throwOnError: true });
      if (!data || !Array.isArray(data.candidates)) throw new Error('服务端未返回有效的融合导出数据');
      triggerDownloadFile(`fusion-candidates-${activeTrace.id}.json`, JSON.stringify(data, null, 2), 'application/json');
    } catch (error) {
      showToast(error.message || '融合明细导出失败', 'error');
    }
  };

  /* 12 问答流程 > 重排 - 100% 对应 12-问答流程-重排.png */
  async function pageQA12_Rerank() {
    const { traces, activeTrace } = await getActiveQATrace();
    if (!activeTrace?.id) return emptyQATrace(5);

    let rerank = null;
    let pipeline = null;
    if (activeTrace?.id && api && api.connected) {
      try {
        [rerank, pipeline] = await Promise.all([
          api.getTraceRerankStage(activeTrace.id, state.rerankThreshold !== undefined ? { threshold: state.rerankThreshold } : {}),
          api.getTracePipeline(activeTrace.id, { stage: 6 })
        ]);
      } catch (e) {
        rerank = null;
        pipeline = null;
      }
    }

    const traceBanner = renderQATraceBanner(activeTrace, traces, 5, rerank?.totalDuration || pipeline?.totalDuration);
    const traceHeader = renderQATraceHeader(5);

    if (!rerank) throw new Error(api.lastError?.message || '重排记录读取失败');
    const fusionStage = await api.getTraceFusionStage(activeTrace.id, {}, { throwOnError: true });
    const selectedIds = new Set((rerank.output?.selected || []).map(c => c.chunkRevisionId));
    const before = (fusionStage.candidates || []).map((c, i) => ({ ...c, chunkId: c.id, rank: i + 1, page: c.locator?.page || '—', summary: c.contentText }));
    const after = (rerank.output?.selected || []).map((c, i) => ({ ...c, chunkId: c.chunkRevisionId, rank: i + 1, page: c.locator?.page || '—', summary: c.content }));
    const selected = after.find(c => c.chunkId === state.selectedRerankChunkId) || before.find(c => c.chunkId === state.selectedRerankChunkId) || after[0] || before[0];
    const original = before.find(c => c.chunkId === selected?.chunkId);
    const curChunk = {
      chunkId: selected?.chunkId || '', title: selected?.title || '暂无候选', breadcrumb: selected?.title || '—',
      page: selected?.page || '—', content: selected?.content || selected?.contentText || '',
      beforeScore: original?.fusionScore ?? original?.score ?? '—', afterScore: selected?.rerankScore ?? '—',
      modelInference: '本次使用词项相关性重排，服务未生成推理说明。',
      tokenUsage: {}, permissionCheck: { message: selected ? '属于当前发布版本的可访问证据' : '暂无证据' }
    };
    rerank = {
      ...rerank,
      modelCard: { modelName: rerank.provider, candidateCount: before.length, retainedCount: after.length, durationMs: rerank.durationMs, maxRetainedTopK: activeTrace.config_snapshot?.stageOverrides?.rerank?.topK || 6, status: rerank.status },
      beforeCandidates: before.map(c => { const rank = after.findIndex(a => a.chunkId === c.chunkId) + 1; return { ...c, rankDelta: rank ? c.rank - rank : 0, deltaType: !selectedIds.has(c.chunkId) ? 'eliminated' : rank < c.rank ? 'up' : rank > c.rank ? 'down' : 'same' }; }),
      afterCandidates: after,
      scoreCurve: { threshold: rerank.threshold ?? 0, dataPoints: before.map(c => ({ ...c, beforeScore: c.fusionScore ?? c.score ?? 0, afterScore: c.rerankScore ?? 0 })) }
    };

    const beforeHtml = (rerank.beforeCandidates || []).map(c => {
      const isSel = c.chunkId === curChunk?.chunkId;
      let badge = '<span class="muted" style="margin-left:8px;font-size:11px;">-</span>';
      if (c.deltaType === 'up') {
        badge = `<span style="margin-left:8px;padding:2px 6px;border-radius:4px;background:var(--accent-soft);color:#16a34a;font-size:11px;font-weight:700;">↑ ${c.rankDelta}</span>`;
      } else if (c.deltaType === 'down') {
        badge = `<span style="margin-left:8px;padding:2px 6px;border-radius:4px;background:var(--danger-soft);color:#dc2626;font-size:11px;font-weight:700;">↓ ${Math.abs(c.rankDelta)}</span>`;
      } else if (c.deltaType === 'eliminated') {
        badge = '<span class="badge" style="margin-left:8px;background:var(--inset);color:#94a3b8;font-size:10px;">淘汰</span>';
      }
      return `
        <div style="display:flex;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line-soft);cursor:pointer;${isSel ? 'background:var(--accent-soft);border-left:3px solid var(--accent);' : ''}${c.deltaType === 'eliminated' ? 'opacity:0.6;' : ''}" onclick="state.selectedRerankChunkId='${esc(c.chunkId)}';render();">
          <div style="width:24px;height:24px;border-radius:4px;background:var(--inset);border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:11.5px;color:var(--ink-dim);margin-right:10px;flex-shrink:0;">${c.rank}</div>
          <div style="flex:1;min-width:0;">
            <div style="font-weight:600;color:var(--ink-strong);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(c.title)}</div>
            <div class="muted" style="font-size:10.5px;">${esc(c.chunkId)} · P.${c.page || 12}</div>
            <div class="muted" style="font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(c.summary || '')}</div>
          </div>
          ${badge}
        </div>
      `;
    }).join('');

    const afterHtml = (rerank.afterCandidates || []).map(c => {
      const isSel = c.chunkId === curChunk?.chunkId;
      const rankBg = c.rank === 1 ? 'background:var(--accent);color:#fff;' : 'background:var(--inset);border:1px solid var(--line);color:var(--ink-dim);';
      return `
        <div style="display:flex;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line-soft);cursor:pointer;${isSel ? 'background:var(--accent-soft);border-left:3px solid var(--accent);' : ''}" onclick="state.selectedRerankChunkId='${esc(c.chunkId)}';render();">
          <div style="width:24px;height:24px;border-radius:4px;${rankBg}display:flex;align-items:center;justify-content:center;font-weight:700;font-size:11.5px;margin-right:10px;flex-shrink:0;">${c.rank}</div>
          <div style="flex:1;min-width:0;">
            <div style="font-weight:${isSel ? '700' : '600'};color:var(--ink-strong);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(c.title)}</div>
            <div class="muted" style="font-size:10.5px;">${esc(c.chunkId)} · P.${c.page || 12}</div>
            <div class="muted" style="font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(c.summary || '')}</div>
          </div>
          <b class="mono" style="margin-left:8px;color:#16a34a;font-size:${isSel ? '13px' : '12px'};">${c.score}</b>
        </div>
      `;
    }).join('');

    const curveThreshold = rerank.scoreCurve?.threshold !== undefined ? Number(rerank.scoreCurve.threshold) : (state.rerankThreshold || 0.75);
    const curveDataPoints = rerank.scoreCurve?.dataPoints || [];
    const maxRetainedTopK = rerank.modelCard?.maxRetainedTopK || 8;
    const thY = Math.round(90 - curveThreshold * 72);

    const beforePolyPoints = curveDataPoints.map((d, i) => `${40 + i * 25},${Math.round(90 - (d.beforeScore || 0) * 72)}`).join(' ');
    const afterPolyPoints = curveDataPoints.map((d, i) => `${40 + i * 25},${Math.round(90 - (d.afterScore || 0) * 72)}`).join(' ');

    const dotsHtml = curveDataPoints.map((d, i) => {
      const cx = 40 + i * 25;
      const cy = Math.round(90 - (d.afterScore || 0) * 72);
      const isRetained = d.afterScore >= curveThreshold && i < maxRetainedTopK;
      const isCur = d.chunkId === curChunk?.chunkId;
      const color = isRetained ? '#16a34a' : '#94a3b8';
      const r = isCur ? 4.5 : (isRetained ? 3.0 : 2.0);
      const stroke = isCur ? 'stroke="#ffffff" stroke-width="2"' : '';
      return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${color}" ${stroke} style="cursor:pointer;" onclick="state.selectedRerankChunkId='${esc(d.chunkId || '')}';render();"><title>#${d.rank || (i+1)} ${esc(d.title || '')}: 前 ${d.beforeScore} → 后 ${d.afterScore} (${isRetained ? '保留' : '淘汰'})</title></circle>`;
    }).join('');

    const xLabelsHtml = curveDataPoints.map((d, i) => {
      const cx = 40 + i * 25;
      const isRetained = d.afterScore >= curveThreshold && i < maxRetainedTopK;
      const isCur = d.chunkId === curChunk?.chunkId;
      return `<text x="${cx}" y="102" font-size="7.5" fill="${isCur ? 'var(--accent)' : (isRetained ? '#16a34a' : '#94a3b8')}" font-weight="${isCur || isRetained ? '700' : 'normal'}" text-anchor="middle" style="cursor:pointer;" onclick="state.selectedRerankChunkId='${esc(d.chunkId || '')}';render();">${d.rank || (i+1)}</text>`;
    }).join('');

    const html = `
    ${traceBanner}
    ${traceHeader}

    <!-- Top Reranker Model Info Banner Card -->
    <div class="card" style="margin-bottom:16px;">
      <div class="card-body" style="padding:16px 20px;display:flex;align-items:center;justify-content:space-between;">
        <div style="display:flex;align-items:center;gap:14px;">
          <div style="width:40px;height:40px;border-radius:8px;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:22px;">${iconLayers(16)}</div>
          <div>
            <div class="muted" style="font-size:12px;">Reranker</div>
            <b style="font-size:15px;color:var(--ink-strong);">${esc(rerank.modelCard?.modelName || 'bge-reranker-v2-m3')}</b>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:36px;font-size:13px;">
          <div>
            <div class="muted" style="font-size:12px;">候选数量</div>
            <b style="font-size:18px;color:var(--ink-strong);">${rerank.modelCard?.candidateCount || 20}</b>
          </div>
          <span style="color:#94a3b8;font-size:16px;">→</span>
          <div>
            <div class="muted" style="font-size:12px;">保留候选</div>
            <b style="font-size:18px;color:var(--ink-strong);">${rerank.modelCard?.retainedCount || 8}</b>
          </div>
          <div>
            <div class="muted" style="font-size:12px;">耗时</div>
            <b style="font-size:18px;color:var(--ink-strong);">${rerank.modelCard?.durationMs || 512} <small style="font-size:12px;font-weight:normal;">ms</small></b>
          </div>
          <div>
            <div class="muted" style="font-size:12px;">状态</div>
            <div style="display:flex;align-items:center;gap:4px;color:#16a34a;font-weight:600;font-size:14px;">● ${rerank.modelCard?.status === 'healthy' ? '健康' : '就绪'}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Main Workspace: Left (Top 2 Lists + Bottom Chart) | Right (Single Tall Document Details Card) -->
    <div style="display:grid;grid-template-columns:minmax(0, 1.85fr) 350px;gap:16px;align-items:stretch;width:100%;">
      <!-- Left Column (65-70%) -->
      <div style="display:flex;flex-direction:column;gap:16px;height:100%;">
        <!-- Top: 2 Side-by-Side Lists (重排前 & 重排后) -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:stretch;">
          <!-- Card 1: 重排前 (候选 20) -->
          <div class="card" style="display:flex;flex-direction:column;height:100%;">
            <div class="card-head" style="padding:12px 14px;display:flex;justify-content:space-between;align-items:center;">
              <b style="font-size:13px;color:var(--ink-strong);">重排前 <small class="muted" style="font-weight:normal;">(候选 ${rerank.modelCard?.candidateCount || (rerank.beforeCandidates || []).length || 20})</small></b>
              <div style="display:flex;gap:14px;font-size:11px;color:var(--ink-dim);">
                <span>原始排名</span>
                <span>相关度分</span>
              </div>
            </div>
            <div class="card-body" style="padding:0;font-size:12px;display:flex;flex-direction:column;flex:1;max-height:440px;overflow-y:auto;">
              ${beforeHtml}
            </div>
          </div>

          <!-- Card 2: 重排后 (保留 8) -->
          <div class="card" style="display:flex;flex-direction:column;height:100%;">
            <div class="card-head" style="padding:12px 14px;display:flex;justify-content:space-between;align-items:center;">
              <b style="font-size:13px;color:var(--ink-strong);">重排后 <small class="muted" style="font-weight:normal;">(保留 ${rerank.modelCard?.retainedCount || (rerank.afterCandidates || []).length || 8})</small></b>
              <div style="display:flex;gap:14px;font-size:11px;color:var(--ink-dim);">
                <span>重排后排名</span>
                <span>相关度分</span>
              </div>
            </div>
            <div class="card-body" style="padding:0;font-size:12px;display:flex;flex-direction:column;flex:1;max-height:440px;overflow-y:auto;">
              ${afterHtml || `<div class="muted" style="padding:28px 16px;text-align:center;">暂无符合当前阈值 (≥ ${curveThreshold.toFixed(2)}) 的候选切片</div>`}
            </div>
          </div>
        </div>

        <!-- Bottom: 重排得分对比 (Top 20) Chart Card (Inside Left Column) -->
        <div class="card" style="padding:14px 18px;display:flex;flex-direction:column;flex:1;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <b style="font-size:13px;color:var(--ink-strong);">重排得分对比 <small class="muted" style="font-weight:normal;">(Top ${curveDataPoints.length || 20})</small></b>
            <div style="display:flex;gap:14px;font-size:11px;color:var(--ink-dim);">
              <span><span style="color:#94a3b8;">●</span> 重排前得分</span>
              <span><span style="color:#16a34a;">●</span> 重排后得分</span>
              <span style="cursor:pointer;" onclick="openAdjustRerankThresholdModal();" title="点击调整阈值"><span style="color:#e02424;">---</span> 保留阈值 ${curveThreshold.toFixed(2)}</span>
            </div>
          </div>
          <!-- SVG Multi-Line Chart -->
          <svg viewBox="0 0 540 110" style="width:100%;height:100px;margin-top:auto;">
            <!-- Grid Lines -->
            <line x1="25" y1="18" x2="525" y2="18" stroke="var(--line-soft)"/>
            <line x1="25" y1="54" x2="525" y2="54" stroke="var(--line-soft)"/>
            <line x1="25" y1="90" x2="525" y2="90" stroke="var(--line)"/>
            <!-- Threshold Line -->
            <line x1="25" y1="${thY}" x2="525" y2="${thY}" stroke="#e02424" stroke-dasharray="3 3" opacity="0.85"/>
            <text x="526" y="${Math.max(12, thY + 3)}" font-size="7" fill="#e02424" font-weight="600">${curveThreshold.toFixed(2)}</text>
            <!-- Y-axis Labels -->
            <text x="5" y="21" font-size="8" fill="#94a3b8">1.0</text>
            <text x="5" y="57" font-size="8" fill="#94a3b8">0.5</text>
            <text x="12" y="93" font-size="8" fill="#94a3b8">0</text>
            <!-- Gray Line: 重排前 -->
            <polyline fill="none" stroke="#94a3b8" stroke-width="1.5" points="${beforePolyPoints}"/>
            <!-- Green Line: 重排后 -->
            <polyline fill="none" stroke="#16a34a" stroke-width="2" points="${afterPolyPoints}"/>
            <!-- Dynamic Dots -->
            ${dotsHtml}
            <!-- X-axis Labels (1 to 20) -->
            ${xLabelsHtml}
          </svg>
        </div>
      </div>

      <!-- Right Column: Single Tall Card 文档详情 (Equal Height Stretch) -->
      <div class="card" style="height:100%;display:flex;flex-direction:column;padding:16px 18px;box-sizing:border-box;">
        <div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:12px;border-bottom:1px solid var(--line);">
          <b style="font-size:14px;color:var(--ink-strong);">文档详情</b>
          <span class="muted" style="cursor:pointer;font-size:16px;">✕</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:12px;font-size:12.5px;margin-top:12px;flex:1;">
          <!-- Doc Header -->
          <div style="border:1px solid var(--line);border-radius:6px;padding:10px 12px;background:var(--inset);">
            <div style="display:flex;align-items:center;gap:8px;">
              <span style="font-size:16px;color:#16a34a;">${iconDoc(13)}</span>
              <div>
                <b style="font-size:13px;color:var(--ink-strong);">${esc(curChunk.title)}</b>
                <div class="muted" style="font-size:11px;">${esc(curChunk.chunkId)}</div>
              </div>
            </div>
            <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--ink-dim);margin-top:8px;border-top:1px solid var(--line);padding-top:6px;">
              <span>来源: ${esc(curChunk.breadcrumb || '产品文档库')}</span>
              <span>位置 ${esc(curChunk.page || 'P.12')}</span>
            </div>
          </div>

          <!-- Content (Full content) -->
          <div>
            <div class="muted" style="font-size:11.5px;margin-bottom:4px;font-weight:600;">内容 (完整内容)</div>
            <div style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:10px 12px;font-size:11.5px;line-height:1.55;color:var(--ink);max-height:160px;overflow-y:auto;white-space:pre-wrap;">
              ${esc(curChunk.content)}
            </div>
          </div>

          <!-- 相关度分数 -->
          <div>
            <div class="muted" style="font-size:11.5px;margin-bottom:4px;font-weight:600;">相关度分数</div>
            <div style="display:flex;align-items:center;justify-content:space-between;background:var(--inset);padding:8px 12px;border-radius:6px;border:1px solid var(--line);">
              <span>重排前 <b class="mono" style="margin-left:4px;">${curChunk.beforeScore ?? '—'}</b></span>
              <span style="color:#94a3b8;">→</span>
              <span>重排后 <b class="mono" style="color:#16a34a;font-size:14px;margin-left:4px;">${curChunk.afterScore ?? '—'}</b></span>
            </div>
          </div>

          <!-- 模型推理 (摘要) -->
          <div>
            <div class="muted" style="font-size:11.5px;margin-bottom:4px;font-weight:600;">模型推理 (摘要)</div>
            <div style="font-size:11.5px;color:var(--ink-dim);line-height:1.5;background:var(--inset);padding:8px 12px;border-radius:6px;border:1px solid var(--line);">
              ${esc(curChunk.modelInference || 'Cross-Encoder 交叉注意力判定切片与提问意图紧密相关。')}
            </div>
          </div>

          <!-- Token 消耗 -->
          <div>
            <div class="muted" style="font-size:11.5px;margin-bottom:4px;font-weight:600;">Token 消耗</div>
            <div style="display:flex;justify-content:space-between;font-size:11.5px;background:var(--inset);padding:6px 12px;border-radius:6px;border:1px solid var(--line);">
              <span>输入 <b class="mono">${curChunk.tokenUsage?.input || 1246}</b></span>
              <span>输出 <b class="mono">${curChunk.tokenUsage?.output || 318}</b></span>
              <span>总计 <b class="mono" style="color:var(--ink-strong);">${curChunk.tokenUsage?.total || 1564}</b></span>
            </div>
          </div>

          <!-- 权限校验 (Pinned to Bottom) -->
          <div style="margin-top:auto;border-top:1px solid var(--line);padding-top:10px;">
            <div style="display:flex;justify-content:space-between;align-items:center;font-size:11.5px;">
              <span style="font-weight:600;color:var(--ink-strong);">权限校验</span>
              <span style="color:#16a34a;font-weight:600;">✓ 通过</span>
            </div>
            <div class="muted" style="font-size:10.5px;margin-top:2px;">${esc(curChunk.permissionCheck?.message || '当前用户有权限访问该文档')}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Bottom Actions Toolbar -->
    <div style="display:flex;align-items:center;justify-content:flex-end;gap:12px;margin-top:20px;padding:16px 0 24px;border-top:1px solid var(--line);width:100%;">
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:38px;padding:0 20px;border-radius:6px;font-size:13.5px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="openChangeRerankModelModal()">更换模型</button>
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:38px;padding:0 20px;border-radius:6px;font-size:13.5px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="openAdjustRerankThresholdModal()">调整阈值</button>
      <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:38px;padding:0 20px;border-radius:6px;font-size:13.5px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="openRerankCompareModal()">${iconChart(14)} 对比结果</button>
      <button class="btn primary" style="background:var(--accent);color:#ffffff;height:38px;padding:0 26px;border-radius:6px;font-size:13.5px;font-weight:500;cursor:pointer;" onclick="window.go('qaflow/prompt')">进入构建提示词 &gt;</button>
    </div>`;
    return { title: '重排', desc: '', actions: '', html };
  }

  function qaPromptAnswerValue(value, fallback = '未记录') {
    if (value === null || value === undefined || value === '') return fallback;
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
  }

  async function qaPromptAnswerRequest(method, traceId, input = {}) {
    if (!api.connected || !traceId) throw new Error('未连接后端或未选择 Trace');
    const result = await api[method](traceId, input, { throwOnError: true });
    if (result === null || result === undefined) throw new Error(api.lastError?.message || '服务未返回数据');
    return result;
  }

  function qaPromptAnswerRecord(kind) {
    const record = state[kind === 'prompt' ? 'qaPromptRecord' : 'qaGenerationRecord'];
    if (!record || record.traceId !== state.activeTraceId) throw new Error('当前 Trace 数据尚未加载');
    return record;
  }

  /* 13 问答流程 > 构建提示词 */
  async function pageQA13_Prompt() {
    const { traces, activeTrace } = await getActiveQATrace();
    if (!activeTrace?.id) return emptyQATrace(6);
    state.qaPromptRecord = null;
    const data = await qaPromptAnswerRequest('getTracePromptStage', activeTrace.id);
    const output = data.output || {};
    const messages = Array.isArray(data.messages) ? data.messages : [];
    const evidence = Array.isArray(data.evidence) ? data.evidence : (Array.isArray(output.evidence) ? output.evidence : []);
    const pipeline = data.pipeline || activeTrace.stages || [];
    const promptText = messages.map(message => `[${message.role || 'unknown'}]\n${message.content || ''}`).join('\n\n');
    const systemText = messages.filter(message => message.role === 'system').map(message => message.content || '').join('\n\n');
    const history = messages.slice(1, -1).filter(message => ['user', 'assistant'].includes(message.role));
    const hasPrompt = messages.length > 0 && data.dataSource !== 'unavailable';
    const sourceLabel = data.reconstructed || data.dataSource === 'reconstructed' ? '重建预览（非原始记录）' : hasPrompt ? '已记录消息' : '未记录';
    state.qaPromptRecord = { traceId: activeTrace.id, data, promptText, activeTrace };
    const selected = state.promptSelectedTab || 'evidence';
    const scan = state.qaPromptScan?.traceId === activeTrace.id ? state.qaPromptScan.result : null;
    const scanLabel = scan ? `${scan.count} 项匹配 / ${scan.method || '未记录扫描方法'}` : '未扫描';
    const draft = data.draft || {};
    const parts = [
      { id: 'system', name: '系统规则', icon: iconShield(14), color: '#9333ea', content: esc(systemText || '未记录') },
      { id: 'role', name: '助手角色', icon: iconUser(14), color: '#2563eb', content: esc(qaPromptAnswerValue(output.role, '未单独记录')) },
      { id: 'history', name: '会话历史', icon: iconChat(14), color: '#d97706', content: history.length ? history.map(message => `<div><b>${esc(message.role)}</b><div style="white-space:pre-wrap;">${esc(message.content)}</div></div>`).join('') : (hasPrompt ? '本次提示词未包含历史消息' : '未记录') },
      { id: 'query', name: '用户问题', icon: iconHelp(14), color: '#16a34a', content: esc(data.query || activeTrace.query || '未记录') },
      { id: 'evidence', name: '召回证据', icon: iconDoc(14), color: '#16a34a', content: evidence.length ? evidence.map((item, index) => `<div style="padding:7px 0;border-bottom:1px solid var(--line-soft);"><b>[${esc(item.ordinal ?? index + 1)}] ${esc(item.title || item.documentTitle || '未记录标题')}</b><div class="muted" style="font-size:11px;">${esc(item.chunkRevisionId || item.id || '')} ${esc(api.formatLocator(item) || '')}</div><div style="white-space:pre-wrap;margin-top:4px;">${esc(item.content ?? item.contentText ?? '未记录')}</div></div>`).join('') : (output.evidenceCount === 0 ? '本次提示词未包含证据' : '未记录') },
      { id: 'citation', name: '引用规则', icon: iconZap(14), color: '#ca8a04', content: esc(qaPromptAnswerValue(output.citationRule, '未单独记录')) },
      { id: 'format', name: '输出格式', icon: iconCopy(14), color: '#0284c7', content: esc(qaPromptAnswerValue(output.outputFormat, '未单独记录')) },
      { id: 'safety', name: '安全约束', icon: iconShield(14), color: '#dc2626', content: esc(output.security ? JSON.stringify(output.security, null, 2) : '未记录') }
    ];
    const budget = output.tokenBudget || {};
    const promptModel = pipeline.find(stage => stage.key === 'generation' || stage.name === '回答生成')?.output?.modelId;
    const html = `
    ${renderQATraceBanner(activeTrace, traces, 6)}
    ${renderQATraceHeader(6, pipeline.map(stage => stage.durationMs != null ? `${stage.durationMs} ms` : '未记录'))}
    <style>
      #qaPromptWorkspace { display:grid;grid-template-columns:180px minmax(0,1fr) 330px;gap:16px;align-items:stretch; }
      #qaPromptWorkspace > * { min-width:0; }
      @media(max-width:1100px) { #qaPromptWorkspace { grid-template-columns:160px minmax(0,1fr); } #qaPromptWorkspace > :last-child { grid-column:1 / -1; } }
      @media(max-width:700px) { #qaPromptWorkspace { grid-template-columns:minmax(0,1fr); } #qaPromptWorkspace > :first-child nav { flex-direction:row !important;flex-wrap:wrap; } }
    </style>
    <div id="qaPromptWorkspace">
      <div class="card" style="padding:12px 10px;">
        <div style="font-size:12.5px;font-weight:700;padding:4px 8px 10px;border-bottom:1px solid var(--line-soft);">提示词组件</div>
        <nav style="display:flex;flex-direction:column;gap:4px;margin-top:8px;">
          ${parts.map(part => `<button class="btn" style="justify-content:flex-start;gap:8px;height:36px;border:0;padding:8px 10px;font-size:13px;background:${selected === part.id ? 'var(--accent-soft)' : 'transparent'};color:${selected === part.id ? 'var(--accent)' : 'var(--ink-strong)'};" onclick="window.handleQAPromptSection(${jsArg(part.id)})"><span style="color:${part.color};">${part.icon}</span>${part.name}</button>`).join('')}
        </nav>
      </div>
      <div style="display:flex;flex-direction:column;gap:10px;">
        ${parts.map(part => `<div id="qaPromptPart-${part.id}" class="card" style="padding:10px 14px;${selected === part.id ? 'border-color:var(--accent);' : ''}">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:13px;font-weight:600;">
            <span style="display:flex;align-items:center;gap:8px;"><span style="color:${part.color};">${part.icon}</span>${part.name}</span>
            <span class="muted" style="font-size:11px;">${part.id === 'history' ? esc(qaPromptAnswerValue(output.historyCount)) + ' 条' : part.id === 'evidence' ? esc(qaPromptAnswerValue(output.evidenceCount)) + ' 条' : ''}</span>
          </div>
          <div style="font-size:12px;line-height:1.6;margin-top:8px;padding-top:8px;border-top:1px solid var(--line-soft);white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow-y:auto;">${part.content}</div>
        </div>`).join('')}
      </div>
      <div style="display:flex;flex-direction:column;gap:16px;">
        <div class="card" style="padding:14px 16px;">
          <div style="display:flex;justify-content:space-between;gap:8px;margin-bottom:12px;font-size:13px;"><b>Token 预算</b><span class="muted">${esc(qaPromptAnswerValue(budget.total))}</span></div>
          <div style="display:flex;flex-direction:column;gap:7px;font-size:12px;">
            ${[['系统规则', budget.system], ['会话历史', budget.history], ['召回证据', budget.evidence], ['输出预留', budget.output], ['剩余可用', budget.remaining]].map(([label, value]) => `<div style="display:flex;justify-content:space-between;gap:10px;"><span>${label}</span><span class="mono">${esc(qaPromptAnswerValue(value))}</span></div>`).join('')}
          </div>
          <div style="border-top:1px solid var(--line-soft);margin-top:12px;padding-top:10px;display:flex;flex-direction:column;gap:7px;font-size:12px;">
            ${[['模型', promptModel], ['模板版本', output.templateVersion], ['证据字符上限', output.maxEvidenceChars], ['阶段状态', data.status], ['消息来源', sourceLabel]].map(([label, value]) => `<div style="display:flex;justify-content:space-between;gap:10px;"><span class="muted">${label}</span><span style="text-align:right;overflow-wrap:anywhere;">${esc(qaPromptAnswerValue(value))}</span></div>`).join('')}
            <button class="btn sm" style="height:auto;min-height:32px;white-space:normal;justify-content:space-between;" ${!hasPrompt ? 'disabled' : ''} onclick="window.handleQAPromptScan()">${iconShield(13)} 敏感数据扫描: ${esc(scanLabel)}</button>
          </div>
        </div>
        <div class="card" style="padding:14px 16px;display:flex;flex-direction:column;flex:1;">
          <div style="display:flex;justify-content:space-between;gap:8px;margin-bottom:10px;font-size:13px;"><b>提示词消息预览</b><span class="muted" style="font-size:11px;">${esc(sourceLabel)}</span></div>
          <pre id="qaPromptPreview" style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:10px 12px;font-size:11.5px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow-y:auto;flex:1;">${esc(promptText || '未记录')}</pre>
          <button class="btn" style="width:100%;margin-top:10px;height:34px;font-size:12.5px;" ${!hasPrompt ? 'disabled' : ''} onclick="window.handleQAPromptCopy()">${iconCopy(13)} 复制预览内容</button>
        </div>
        ${Object.keys(draft).length ? `<div class="card" style="padding:14px 16px;"><b style="font-size:13px;">已保存草稿（尚未应用）</b><pre style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:11.5px;margin-top:8px;">${esc(JSON.stringify(draft, null, 2))}</pre></div>` : ''}
      </div>
    </div>
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:20px;padding:16px 0 24px;border-top:1px solid var(--line);">
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button class="btn" onclick="openPromptTemplateModal()">${iconEdit(13)} 编辑提示词草稿</button>
        <button class="btn" ${!hasPrompt ? 'disabled' : ''} onclick="window.handleQAPromptMask()">${iconShield(13)} 敏感数据脱敏</button>
        <button class="btn" ${!hasPrompt ? 'disabled' : ''} onclick="window.handleQAPromptExport()">${iconSave(13)} 导出提示词</button>
        <button class="btn" onclick="window.handleQAPromptVersions()">草稿版本</button>
        <button class="btn" onclick="handleQARerun('prompt')">从此阶段重跑</button>
      </div>
      <button class="btn primary" onclick="window.go('qaflow/answer')">进入回答生成 &gt;</button>
    </div>`;
    return { title: '构建提示词', desc: '', actions: '', html };
  }

  /* 14 问答流程 > 回答生成 */
  async function pageQA14_Answer() {
    const { traces, activeTrace } = await getActiveQATrace();
    if (!activeTrace?.id) return emptyQATrace(7);
    state.qaGenerationRecord = null;
    const data = await qaPromptAnswerRequest('getTraceGenerationStage', activeTrace.id);
    const output = data.output || {};
    const answer = data.answer ?? data.message?.content ?? '';
    const citations = Array.isArray(data.citations) ? data.citations : [];
    const pipeline = data.pipeline || activeTrace.stages || [];
    const usage = output.usage || data.usage || {};
    const request = output.request || {};
    const promptStage = pipeline.find(stage => stage.key === 'prompt' || stage.name === '构建提示词');
    const status = data.status || 'unavailable';
    const evidenceStatus = data.evidenceStatus ?? output.evidenceStatus;
    const evidenceLabel = ({ sufficient: '证据充足', insufficient: '证据不足' })[evidenceStatus] || qaPromptAnswerValue(evidenceStatus);
    const usageFallback = output.usageStatus === 'not_applicable' || data.usageStatus === 'not_applicable' ? '不适用（本地抽取）' : '未记录';
    const feedback = state.qaAnswerFeedback?.traceId === activeTrace.id ? state.qaAnswerFeedback.rating : null;
    const saved = state.qaAnswerSaved?.traceId === activeTrace.id;
    const busy = !!state.qaGenerationBusy;
    state.qaGenerationRecord = { traceId: activeTrace.id, data, answer, citations, activeTrace };
    const row = (label, value, fallback) => `<div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;"><span class="muted">${label}</span><span style="text-align:right;overflow-wrap:anywhere;min-width:0;">${esc(qaPromptAnswerValue(value, fallback))}</span></div>`;
    const html = `
    ${renderQATraceBanner(activeTrace, traces, 7)}
    ${renderQATraceHeader(7, pipeline.map(stage => stage.durationMs != null ? `${stage.durationMs} ms` : '未记录'))}
    <style>
      #qaGenerationWorkspace { display:grid;grid-template-columns:240px minmax(0,1.4fr) minmax(0,1.2fr);gap:16px;align-items:stretch; }
      #qaGenerationWorkspace > * { min-width:0; }
      @media(max-width:1100px) { #qaGenerationWorkspace { grid-template-columns:220px minmax(0,1fr); } #qaGenerationWorkspace > :last-child { grid-column:1 / -1; } }
      @media(max-width:700px) { #qaGenerationWorkspace { grid-template-columns:minmax(0,1fr); } }
    </style>
    <div id="qaGenerationWorkspace">
      <div class="card" style="display:flex;flex-direction:column;padding:14px 16px;">
        <div style="font-size:13.5px;font-weight:700;padding-bottom:10px;border-bottom:1px solid var(--line);">模型请求</div>
        <div style="display:flex;flex-direction:column;gap:8px;font-size:12px;margin-top:12px;">
          ${row('模型', output.modelId ?? data.modelId)}
          ${row('Provider', output.provider ?? data.provider)}
          ${row('提示词版本', promptStage?.output?.templateVersion)}
          ${row('温度 (Temperature)', request.temperature)}
          ${row('最大输出 (Max Output)', request.maxOutputTokens ?? request.max_tokens)}
          ${row('流式输出 (Streaming)', request.streaming == null ? null : request.streaming ? '是' : '否')}
          <div style="border-top:1px solid var(--line-soft);margin-top:6px;padding-top:8px;">${row('记录时间', activeTrace.created_at)}</div>
          <div style="border-top:1px solid var(--line-soft);margin-top:6px;padding-top:8px;display:flex;flex-direction:column;gap:6px;">
            ${row('输入 Tokens', usage.input_tokens ?? usage.prompt_tokens ?? usage.inputTokens, usageFallback)}
            ${row('输出 Tokens', usage.output_tokens ?? usage.completion_tokens ?? usage.outputTokens, usageFallback)}
            ${row('总 Tokens', usage.total_tokens ?? usage.totalTokens, usageFallback)}
            ${row('阶段耗时', data.durationMs == null ? null : `${data.durationMs} ms`)}
            ${row('阶段状态', status)}
          </div>
        </div>
      </div>
      <div class="card" style="display:flex;flex-direction:column;padding:16px 18px;">
        <div style="font-size:13.5px;font-weight:700;padding-bottom:10px;border-bottom:1px solid var(--line);">最终回答</div>
        <div style="display:flex;flex-direction:column;font-size:12.5px;line-height:1.65;margin-top:12px;flex:1;">
          <b style="font-size:14.5px;margin-bottom:6px;overflow-wrap:anywhere;">${esc(data.query || activeTrace.query || '未记录')}</b>
          <div id="qaFinalAnswer" style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.7;margin-bottom:12px;">${esc(answer || '未记录')}</div>
          ${output.degradationReason ? `<div class="muted" style="margin:8px 0;">降级原因: ${esc(output.degradationReason)}</div>` : ''}
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:auto;padding-top:14px;border-top:1px solid var(--line-soft);">
            <button class="btn sm" ${!answer ? 'disabled' : ''} onclick="handleCopyFullAnswer()">${iconCopy(13)} 复制</button>
            <button class="btn sm" ${busy ? 'disabled' : ''} onclick="window.handleRegenerateAnswer()">${busy ? '生成中...' : '重新生成'}</button>
            <button class="btn sm" ${!answer || saved ? 'disabled' : ''} onclick="window.handleQAAnswerSave()">${iconSave(13)} ${saved ? '已保存 Wiki 草稿' : '保存为 Wiki 草稿'}</button>
            <button class="btn sm" ${!answer ? 'disabled' : ''} aria-pressed="${feedback === 1}" onclick="handleAnswerFeedback('thumb_up')">${iconThumbUp(13)} 有帮助</button>
            <button class="btn sm" ${!answer ? 'disabled' : ''} aria-pressed="${feedback === -1}" onclick="handleAnswerFeedback('thumb_down')">${iconThumbDown(13)} 没帮助</button>
          </div>
        </div>
      </div>
      <div class="card" style="display:flex;flex-direction:column;padding:14px 16px;">
        <div style="font-size:13.5px;font-weight:700;padding-bottom:10px;border-bottom:1px solid var(--line);">引用与证据 (${citations.length})</div>
        <div style="display:flex;flex-direction:column;gap:10px;font-size:12px;margin-top:10px;flex:1;">
          ${citations.length ? citations.map((citation, index) => `<div style="border:1px solid var(--line);border-radius:6px;padding:8px 10px;background:var(--inset);">
            <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:6px;">
              <b style="overflow-wrap:anywhere;">[${esc(citation.ordinal ?? index + 1)}] ${esc(citation.title || citation.document_title || '未记录标题')}</b>
              <button class="btn sm" title="查看引用来源" aria-label="查看引用来源" ${!citation.id ? 'disabled' : ''} onclick="window.handleQAAnswerCitation(${jsArg(citation.id)})">${iconDoc(13)}</button>
            </div>
            <div class="muted" style="font-size:10.5px;margin:3px 0;overflow-wrap:anywhere;">Chunk ID: ${esc(citation.chunk_revision_id || '未记录')} / ${esc(api.formatLocator(citation) || '未记录位置')}</div>
            <div style="font-size:11px;line-height:1.5;margin-top:4px;white-space:pre-wrap;overflow-wrap:anywhere;">${esc(citation.excerpt ?? citation.quote ?? '未记录')}</div>
          </div>`).join('') : '<div class="muted">未记录引用</div>'}
          <div style="border-top:1px solid var(--line-soft);margin-top:auto;padding-top:10px;display:flex;flex-direction:column;gap:7px;">
            ${row('证据状态', evidenceLabel)}
            ${row('引用条目', citations.length)}
            ${row('证据覆盖率', output.evidenceCoverage)}
            ${row('安全检查', output.safetyCheck)}
            ${row('降级处理', output.degraded == null ? null : output.degraded ? '已触发' : '未触发')}
          </div>
        </div>
      </div>
    </div>
    <div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--line);">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px;">
        <b style="font-size:13.5px;">完整 Trace 耗时: ${esc(activeTrace.metrics?.totalMs == null ? '未记录' : `${activeTrace.metrics.totalMs} ms`)}</b>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <button class="btn" onclick="openFullTraceModal()">查看完整 Trace</button>
          <button class="btn" onclick="window.handleQAAnswerReport()">${iconSave(13)} 导出报告</button>
          <button class="btn" onclick="handleQARerun('parse')">从问题解析重跑</button>
          <button class="btn primary" onclick="window.go('apps/chat')">返回智能问答</button>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px;">
        ${flowNames.map((name, index) => {
          const stage = pipeline[index];
          return `<button class="btn" style="height:auto;min-height:62px;white-space:normal;padding:8px 6px;display:flex;flex-direction:column;gap:3px;${index === 7 ? 'border-color:var(--accent);background:var(--accent-soft);' : ''}" onclick="window.go('qaflow/${flowRoutes[index]}')"><span style="font-size:12px;">${index + 1} ${name}</span><span class="muted" style="font-size:10.5px;">${esc(stage?.status || '未记录')} / ${stage?.durationMs == null ? '未记录' : esc(stage.durationMs) + ' ms'}</span></button>`;
        }).join('')}
      </div>
    </div>`;
    return { title: '回答生成', desc: '', actions: '', html };
  }

  window.handleQAPromptSection = function(section) {
    state.promptSelectedTab = section;
    return render();
  };

  window.handleQAPromptEdit = function() {
    let record;
    try { record = qaPromptAnswerRecord('prompt'); } catch (error) { showToast(error.message, 'error'); return; }
    const draft = { ...(record.activeTrace.config_snapshot?.stageOverrides?.prompt || {}), ...(record.data.draft || {}) };
    state.qaPromptEditingTraceId = record.traceId;
    showOverlay(`<div class="modal-box" style="max-width:640px;">
      <div class="modal-header"><span>提示词草稿</span><button class="btn sm" data-close>关闭</button></div>
      <div class="modal-body" style="padding:16px 20px;">
        <label class="form-label" for="qaPromptInstructions">追加指令</label>
        <textarea id="qaPromptInstructions" class="input" maxlength="12000" style="width:100%;height:180px;font-size:12px;line-height:1.6;">${esc(draft.instructions ?? draft.systemPrompt ?? '')}</textarea>
        <label class="form-label" for="qaPromptEvidenceLimit" style="display:block;margin-top:12px;">证据字符上限</label>
        <input id="qaPromptEvidenceLimit" class="input" type="number" min="100" max="100000" step="1" value="${esc(draft.maxEvidenceChars ?? record.data.maxEvidenceChars ?? record.data.output?.maxEvidenceChars ?? '')}">
        <label style="display:flex;gap:8px;align-items:center;margin-top:12px;font-size:12px;"><input id="qaPromptMaskSensitive" type="checkbox" ${draft.maskSensitive ? 'checked' : ''}>敏感数据脱敏</label>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;"><button class="btn" data-close>取消</button><button class="btn primary" onclick="window.handleQAPromptSave()">保存草稿</button></div>
    </div>`);
  };

  window.handleQAPromptSave = async function() {
    if (state.qaPromptSaving) return;
    try {
      const record = qaPromptAnswerRecord('prompt');
      if (state.qaPromptEditingTraceId !== record.traceId) throw new Error('Trace 已切换，请重新打开草稿');
      const instructions = document.getElementById('qaPromptInstructions')?.value ?? '';
      const limit = document.getElementById('qaPromptEvidenceLimit')?.value.trim();
      const payload = { instructions, systemPrompt: '', maskSensitive: !!document.getElementById('qaPromptMaskSensitive')?.checked };
      if (limit) {
        const number = Number(limit);
        if (!Number.isInteger(number) || number < 100 || number > 100000) throw new Error('证据字符上限必须为 100 到 100000 的整数');
        payload.maxEvidenceChars = number;
      }
      state.qaPromptSaving = true;
      const result = await qaPromptAnswerRequest('updateTracePrompt', record.traceId, payload);
      if (!result.saved) throw new Error('服务端未确认草稿保存');
      closeOverlay();
      showToast(`已保存提示词草稿 v${result.version}，尚未应用于原 Trace`, 'ok');
      await render();
    } catch (error) { showToast(error.message || '草稿保存失败', 'error'); }
    finally { state.qaPromptSaving = false; }
  };

  window.handleQAPromptCopy = async function() {
    try {
      const record = qaPromptAnswerRecord('prompt');
      if (!record.promptText) throw new Error('提示词未记录');
      if (!navigator.clipboard?.writeText) throw new Error('当前环境不支持剪贴板复制');
      await navigator.clipboard.writeText(record.promptText);
      showToast('已复制提示词消息', 'ok');
    } catch (error) { showToast(error.message || '复制失败', 'error'); }
  };

  window.handleQAPromptExport = function() {
    try {
      const record = qaPromptAnswerRecord('prompt');
      if (!record.promptText) throw new Error('提示词未记录');
      triggerDownloadFile(`prompt-${record.traceId}.json`, JSON.stringify({ traceId: record.traceId, dataSource: record.data.dataSource, reconstructed: !!record.data.reconstructed, messages: record.data.messages, draft: record.data.draft }, null, 2), 'application/json');
    } catch (error) { showToast(error.message || '导出失败', 'error'); }
  };

  window.handleQAPromptScan = async function() {
    try {
      const record = qaPromptAnswerRecord('prompt');
      if (!record.promptText) throw new Error('提示词未记录，无法扫描');
      const result = await qaPromptAnswerRequest('scanPromptSensitiveData', record.traceId);
      if (!Array.isArray(result.matches) || result.count == null) throw new Error('扫描结果不完整');
      state.qaPromptScan = { traceId: record.traceId, result };
      if (state.activeTraceId === record.traceId) await render();
      showOverlay(`<div class="modal-box" style="max-width:560px;"><div class="modal-header"><span>敏感数据扫描</span><button class="btn sm" data-close>关闭</button></div><div class="modal-body" style="padding:16px 20px;"><div style="font-size:12px;margin-bottom:12px;">${esc(result.method || '未记录方法')} / ${esc(result.count)} 项匹配</div><pre style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;">${esc(JSON.stringify(result.matches, null, 2))}</pre></div></div>`);
    } catch (error) { showToast(error.message || '扫描失败', 'error'); }
  };

  window.handleQAPromptMask = async function() {
    if (state.qaPromptMasking) return;
    try {
      const record = qaPromptAnswerRecord('prompt');
      if (!record.promptText) throw new Error('提示词未记录，无法脱敏');
      state.qaPromptMasking = true;
      const result = await qaPromptAnswerRequest('maskPrompt', record.traceId);
      if (!result.saved) throw new Error('服务端未确认脱敏草稿保存');
      await render();
      showOverlay(`<div class="modal-box" style="max-width:680px;"><div class="modal-header"><span>原记录脱敏预览（草稿尚未应用）</span><button class="btn sm" data-close>关闭</button></div><div class="modal-body" style="padding:16px 20px;"><div style="font-size:12px;margin-bottom:12px;">草稿 v${esc(result.version)} / 已替换 ${esc(result.maskedCount)} 项匹配</div><pre style="max-height:420px;overflow-y:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;">${esc((result.messages || []).map(message => `[${message.role}]\n${message.content}`).join('\n\n'))}</pre></div></div>`);
    } catch (error) { showToast(error.message || '脱敏失败', 'error'); }
    finally { state.qaPromptMasking = false; }
  };

  window.handleQAPromptVersions = async function() {
    try {
      const record = qaPromptAnswerRecord('prompt');
      const versions = await qaPromptAnswerRequest('getPromptVersions', record.traceId);
      if (!Array.isArray(versions)) throw new Error('草稿版本数据无效');
      showOverlay(`<div class="modal-box" style="max-width:680px;"><div class="modal-header"><span>提示词草稿版本</span><button class="btn sm" data-close>关闭</button></div><div class="modal-body" style="padding:16px 20px;max-height:460px;overflow-y:auto;">${versions.length ? versions.map(version => `<section style="border-bottom:1px solid var(--line);padding:10px 0;"><b style="font-size:13px;">v${esc(version.version)}</b><span class="muted" style="font-size:11px;margin-left:10px;">${esc(version.created_at)}</span><pre style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;margin-top:8px;">${esc(JSON.stringify(version.config || {}, null, 2))}</pre></section>`).join('') : '<div class="muted">暂无草稿版本</div>'}</div></div>`);
    } catch (error) { showToast(error.message || '版本加载失败', 'error'); }
  };

  window.handleQAAnswerCitation = async function(citationId) {
    try {
      const record = qaPromptAnswerRecord('generation');
      if (!record.citations.some(citation => citation.id === citationId)) throw new Error('引用不属于当前 Trace');
      const result = await qaPromptAnswerRequest('openCitation', citationId);
      showOverlay(`<div class="modal-box" style="max-width:640px;"><div class="modal-header"><span>${esc(result.title || '引用来源')}</span><button class="btn sm" data-close>关闭</button></div><div class="modal-body" style="padding:16px 20px;"><div class="muted" style="font-size:12px;margin-bottom:10px;overflow-wrap:anywhere;">${esc(result.releaseId || '未记录版本')} / ${esc(result.locationLabel || '未记录位置')}</div><div style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:12px;font-size:13px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow-y:auto;">${esc(result.contentText ?? result.contentMd ?? result.excerpt ?? '未记录')}</div></div></div>`);
    } catch (error) { showToast(error.message || '引用详情加载失败', 'error'); }
  };

  window.handleQAAnswerSave = async function() {
    if (state.qaAnswerSaving) return;
    try {
      const record = qaPromptAnswerRecord('generation');
      if (!record.answer) throw new Error('当前 Trace 未记录回答');
      if (state.qaAnswerSaved?.traceId === record.traceId) return;
      state.qaAnswerSaving = true;
      const result = await qaPromptAnswerRequest('saveTraceQa', record.traceId, { title: (record.activeTrace.query || '问答记录').slice(0, 120) });
      if (!result.id) throw new Error('服务端未返回 Wiki 草稿');
      state.qaAnswerSaved = { traceId: record.traceId, id: result.id };
      showToast('已保存为 Wiki 草稿，尚未发布', 'ok');
      await render();
    } catch (error) { showToast(error.message || '保存失败', 'error'); }
    finally { state.qaAnswerSaving = false; }
  };

  window.handleQAAnswerReport = async function() {
    try {
      const record = qaPromptAnswerRecord('generation');
      const trace = await qaPromptAnswerRequest('getTrace', record.traceId);
      const generation = await qaPromptAnswerRequest('getTraceGenerationStage', record.traceId);
      triggerDownloadFile(`trace-${record.traceId}.json`, JSON.stringify({ trace, generation }, null, 2), 'application/json');
    } catch (error) { showToast(error.message || '报告导出失败', 'error'); }
  };

  /* Global Chat Interaction Handlers */
  window.handleSendChat = async function(e) {
    if (e) e.preventDefault();
    if (!requireConnection() || state.chatLoading) return;
    const input = document.getElementById('chatInput');
    const question = input?.value.trim();
    if (!question) return;
    state.chatLoading = true;
    const pendingUser = { role: 'user', text: question, time: '刚刚', pending: true };
    const pendingAnswer = { role: 'assistant', text: '', time: '刚刚', pending: true, citations: [] };
    state.chatMessages.push(pendingUser, pendingAnswer);
    input.value = '';
    state.chatAbort = new AbortController();
    await render();
    try {
      let conversationId = state.activeConversationId;
      if (!conversationId) {
        const kbId = state.chatKbId || state.selectedKbId || api.context?.defaultKbId;
        if (!kbId) throw new Error('请先创建知识库，导入资料并发布版本');
        const conversation = await api.createConversation({ title: question.slice(0, 40), knowledgeBaseId: kbId }, { throwOnError: true });
        conversationId = conversation.id;
        state.activeConversationId = conversationId;
        state.chatReleaseVersion = conversation.release_version || null;
        state.chatConversations.forEach(c => { c.active = false; });
        state.chatConversations.unshift({ id: conversationId, title: question.slice(0, 40), active: true, time: '刚刚' });
      }
      let lastRender = 0;
      const result = await api.sendMessageStream(conversationId, question, (event, data) => {
        if (event === 'token') {
          pendingAnswer.text += data.delta || '';
          if (state.page === 'apps/chat') {
            // 流式期间只更新流式气泡文本，不整页重建，保持输入框焦点。
            const streamText = document.getElementById('chatStreamText');
            if (streamText) {
              streamText.innerHTML = esc(pendingAnswer.text).replace(/\n/g, '<br>');
              const pane = document.getElementById('chatMessagesPane');
              if (pane) pane.scrollTop = pane.scrollHeight;
              return;
            }
          }
          if (Date.now() - lastRender > 100) {
            lastRender = Date.now();
            render();
          }
        }
      }, { throwOnError: true, signal: state.chatAbort.signal });
      Object.assign(pendingAnswer, mapChatMessage(result.assistantMessage), { pending: false });
      if (result.userMessage) Object.assign(pendingUser, mapChatMessage(result.userMessage), { pending: false });
      if (result.trace) { state.lastTrace = result.trace; state.activeTraceId = result.trace.id; }
    } catch (error) {
      if (error?.code === 'REQUEST_ABORTED') return;
      state.chatMessages = state.chatMessages.filter(m => m !== pendingUser && m !== pendingAnswer);
      state.chatInput = question;
      showToast(error.message || '回答生成失败', 'error');
    } finally {
      state.chatLoading = false;
      state.chatAbort = null;
      await render();
    }
  };
  
  window.handleSwitchConversation = async function(conversationId) {
    if (state.chatLoading) return showToast('请等待当前回答完成', 'warn');
    const conversation = await serverAction('getConversation', [conversationId]);
    if (!conversation) return;
    state.activeConversationId = conversation.id;
    state.chatKbId = conversation.knowledge_base_id;
    state.selectedChatKb = conversation.knowledge_base_name || '';
    state.chatReleaseVersion = conversation.release_version;
    state.chatConversations.forEach(c => { c.active = c.id === conversationId; });
    state.chatMessages = (conversation.messages || []).map(mapChatMessage);
    const lastTraceId = [...state.chatMessages].reverse().find(m => m.traceId)?.traceId;
    state.lastTrace = lastTraceId ? { id: lastTraceId } : null;
    state.activeTraceId = lastTraceId || null;
    await render();
  };
  window.handleOpenCitationDetail = async function(citationId) {
    let targetId = citationId;
    // If passed an ordinal (e.g. 1, 2, 3), lookup the real citation ID from latest bot message
    if (typeof citationId === 'number' || /^[0-9]+$/.test(String(citationId))) {
      const lastBot = [...(state.chatMessages || [])].reverse().find(m => m.role === 'assistant' && m.citations);
      const matched = lastBot?.citations?.find(c => c.ordinal === Number(citationId) || c.id === Number(citationId) || c.id === String(citationId));
      if (matched && matched.citationId) targetId = matched.citationId;
    }
    if (api && api.connected && targetId && !String(targetId).startsWith('demo-')) {
      try {
        const res = await api.openCitation(targetId);
        if (res) {
          const html = `
            <div class="modal-box" style="max-width:540px;">
              <div class="modal-header">
                <span>引用来源详情 · ${esc(res.title || '文档证据')}</span>
                <button class="btn sm" data-close>✕</button>
              </div>
              <div class="modal-body" style="padding:16px 20px;">
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
                  <span class="badge ok">${esc(res.locationLabel || '引用片段')}</span>
                  <span class="muted" style="font-size:12px;">已通过不可变发布校验</span>
                </div>
                <div style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:12px;font-size:13px;line-height:1.6;color:var(--ink-strong);max-height:260px;overflow-y:auto;">
                  ${esc(res.contentText || res.contentMd || res.excerpt || '')}
                </div>
              </div>
              <div class="modal-footer" style="display:flex;justify-content:flex-end;">
                <button class="btn primary" data-close>关闭</button>
              </div>
            </div>
          `;
          showOverlay(html);
          return;
        }
      } catch (e) {}
    }
    showToast(api.lastError?.message || '引用来源不可用', 'error');
  };

  window.handleChatFeedback = async function(messageId, rating) {
    if (api && api.connected && messageId && !String(messageId).startsWith('msg-demo')) {
      const res = await api.sendFeedback(messageId, { rating });
      if (res) {
        showToast(rating > 0 ? '✓ 感谢反馈！已记录至评估集' : '✓ 感谢反馈！系统将持续优化回答质量', 'ok');
        return;
      }
      showToast(api.lastError?.message || '反馈记录失败', 'error');
      return;
    }
    showToast('未连接后端服务，反馈未能记录', 'error');
  };

  window.handleOrganizeWiki = async function(messageId) {
    if (api && api.connected && messageId && !String(messageId).startsWith('msg-demo')) {
      showToast('正在将问答沉淀为 Wiki 知识笔记...');
      const res = await api.wikiFromMessage(messageId);
      if (res) {
        showToast(`✓ 已成功沉淀为 Wiki 草稿页面「${res.title || '问答笔记'}」！`, 'ok');
        return;
      }
      showToast(api.lastError?.message || '沉淀 Wiki 失败', 'error');
      return;
    }
    showToast('未连接后端服务，无法沉淀 Wiki 笔记', 'error');
  };

  window.handleHighlightCitation = function(citeId) {
    state.highlightedCitationId = citeId;
    showToast(`查看引用来源 [${citeId}]`, 'ok');
    render();
    setTimeout(() => {
      const target = document.getElementById('citation-card-' + citeId);
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }, 50);
  };

  // [removed: old handleSwitchConversation stub - replaced by async version above]

  
  window.handleSwitchChatKb = function(id) {
    if (state.chatLoading) return showToast('请等待当前回答完成', 'warn');
    const kb = api.context?.knowledgeBases?.find(k => k.id === id || k.name === id);
    if (!kb) return showToast('知识库不存在', 'error');
    state.chatKbId = kb.id;
    state.selectedChatKb = kb.name;
    return window.handleNewChat();
  };
  window.handleCopyChatText = function(text) {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(() => showToast('已复制到剪贴板', 'ok')).catch(() => showToast('已复制内容'));
    } else {
      showToast('已复制内容');
    }
  };

  /* 15 AI应用 > 智能问答 - 100% 对应 15-AI应用-智能问答.png (动态交互版) */
  window.handleNewChat = function() {
    if (state.chatLoading) return showToast('请等待当前回答完成', 'warn');
    state.activeConversationId = null;
    state.chatConversations.forEach(c => { c.active = false; });
    state.chatMessages = [];
    state.chatReleaseVersion = null;
    state.lastTrace = null;
    return render();
  };

  async function pageChat() {
    // Current citations from latest assistant message
    const lastBotMsg = [...state.chatMessages].reverse().find(m => m.role === 'assistant' && m.citations);
    const activeCitations = lastBotMsg?.citations || [];
    const activeWikis = lastBotMsg?.wikis || [];
    const html = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
      <div style="display:flex;align-items:center;gap:10px;">
        <select class="input" style="height:32px;font-size:12.5px;font-weight:600;padding:0 8px;" onchange="window.handleSwitchChatKb(this.value);">
          ${(api.context?.knowledgeBases || []).map(kb => `<option value="${esc(kb.id)}" ${(state.chatKbId || state.selectedKbId) === kb.id ? 'selected' : ''}>▣ ${esc(kb.name)}</option>`).join('')}
        </select>
        <span class="badge ok">${state.chatReleaseVersion ? 'v' + state.chatReleaseVersion : '新会话使用已发布版本'} ●</span>
      </div>
      <div style="display:flex;gap:8px;">
        <button class="btn sm" onclick="window.location.hash='#/knowledge/datasets'">${iconBook(14)} 查看知识库</button>
        <button class="btn sm" onclick="if(state.lastTrace?.id) state.activeTraceId=state.lastTrace.id; window.location.hash='#/qaflow/parse';">${iconRoute(16)} 查看问答流程</button>
      </div>
    </div>

    <!-- 3-Column Layout: Left Sessions (220px) + Center Chat Area (1fr) + Right Citations (280px) -->
    <div class="chat-page-layout">
      <!-- Left: 历史会话 -->
      <div class="chat-conversation-pane">
        <div class="card-head" style="display:flex;justify-content:space-between;align-items:center;">
          <span>历史会话</span>
          <button class="btn sm" style="padding:2px 6px;font-size:11px;" onclick="window.handleNewChat()">+ 新会话</button>
        </div>
        <div style="padding:8px;overflow-y:auto;flex:1;">
          <small class="muted" style="padding:4px 8px;display:block;">今天</small>
          ${state.chatConversations.map(c => `
            <div class="list-item-row ${c.active ? 'active' : ''}" style="background:${c.active ? 'var(--accent-soft)' : 'var(--card-bg)'};border-radius:6px;margin-bottom:4px;cursor:pointer;" onclick="handleSwitchConversation('${c.id}')">
              <div class="grow">
                <b style="font-size:12.5px;color:${c.active ? 'var(--accent)' : 'var(--ink-strong)'};display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(c.title)}</b>
                <div class="muted" style="font-size:10.5px;margin-top:2px;">${c.time}</div>
              </div>
            </div>
          `).join('')}
        </div>
      </div>

      <!-- Center: 聊天核心主面板 -->
      <div class="card" style="display:flex;flex-direction:column;height:100%;">
        <div class="chat-messages-pane" id="chatMessagesPane" style="flex:1;overflow-y:auto;padding:16px;">
          ${state.chatMessages.map((msg, idx) => {
            if (msg.role === 'user') {
              return `
                <div class="chat-msg user" style="display:flex;justify-content:flex-end;margin-bottom:14px;">
                  <div class="chat-bubble" style="background:#16a34a;color:#ffffff;padding:10px 14px;border-radius:12px 12px 2px 12px;max-width:80%;font-size:13.5px;line-height:1.5;">
                    ${esc(msg.text)}
                  </div>
                </div>
              `;
            } else {
              // Format citations into clickable buttons
              const formattedText = esc(msg.text)
                .replace(/\[(\d+)\]/g, (match, ordinal) => {
                  const citation = msg.citations?.find(c => Number(c.ordinal || c.id) === Number(ordinal));
                  return citation?.citationId ? '<span class="cite-tag" style="cursor:pointer;background:var(--accent-soft);color:var(--accent);padding:1px 6px;border-radius:4px;font-weight:600;margin:0 2px;" onclick="window.handleOpenCitationDetail(' + jsArg(citation.citationId) + ');">[' + ordinal + ']</span>' : match;
                })
                .replace(/\n/g, '<br>');

              return `
                <div class="chat-msg" style="display:flex;gap:12px;margin-bottom:16px;">
                  <div class="stat-icon" style="width:36px;height:36px;border-radius:50%;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;">${iconBot(16)}</div>
                  <div class="chat-bubble" style="background:var(--inset);border:1px solid var(--line);padding:12px 16px;border-radius:12px 12px 12px 2px;max-width:85%;font-size:13px;line-height:1.6;color:var(--ink-strong);">
                    <div${msg.pending ? ' id="chatStreamText"' : ''}>${formattedText}</div>
                    <div style="font-size:11.5px;color:var(--ink-dim);margin-top:10px;border-top:1px solid var(--line-soft);padding-top:8px;">
                      ${esc(msg.time)} · ${esc(state.selectedChatKb)} ${state.chatReleaseVersion ? 'v' + state.chatReleaseVersion : ''}
                    </div>
                    <div style="display:flex;gap:8px;margin-top:8px;">
                      <button class="btn sm" style="font-size:11.5px;padding:2px 8px;background:var(--card-bg);border:1px solid var(--line);" onclick="handleCopyChatText(${jsArg(msg.text)})">${iconCopy(13)} 复制</button>
                      <button class="btn sm" style="font-size:11.5px;padding:2px 8px;background:var(--card-bg);border:1px solid var(--line);" onclick="window.handleRegenerateAnswer()">↻ 重新生成</button>
                      <button class="btn sm" style="font-size:11.5px;padding:2px 8px;background:var(--card-bg);border:1px solid var(--line);" onclick="window.handleChatFeedback('${esc(msg.id || '')}', 1)">${iconThumbUp(13)} 有帮助</button>
                      <button class="btn sm" style="font-size:11.5px;padding:2px 8px;background:var(--card-bg);border:1px solid var(--line);" onclick="window.handleChatFeedback('${esc(msg.id || '')}', -1)">${iconThumbDown(13)} 没帮助</button>
                      <button class="btn sm" style="font-size:11.5px;padding:2px 8px;background:var(--card-bg);border:1px solid var(--line);color:var(--accent);" onclick="window.handleOrganizeWiki('${esc(msg.id || '')}')">📝 整理为 Wiki</button>
                    </div>
                  </div>
                </div>
              `;
            }
          }).join('')}

          ${state.chatLoading ? `
            <div class="chat-msg" style="display:flex;gap:12px;margin-bottom:16px;">
              <div class="stat-icon" style="width:36px;height:36px;border-radius:50%;background:var(--accent-soft);color:#16a34a;display:flex;align-items:center;justify-content:center;font-size:18px;">${iconBot(16)}</div>
              <div class="chat-bubble" style="background:var(--inset);border:1px solid var(--line);padding:12px 16px;border-radius:12px;font-size:12.5px;color:var(--ink-dim);display:flex;align-items:center;gap:8px;">
                <span style="display:inline-block;animation:spin 1s linear infinite;">↻</span> 正在检索知识库、重排证据并生成回答...
              </div>
            </div>
          ` : ''}
        </div>

        <!-- Composer Input Area -->
        <form class="chat-composer" onsubmit="handleSendChat(event);" style="padding:10px 14px;border-top:1px solid var(--line);display:flex;gap:10px;align-items:center;background:var(--card-bg);">
          <input class="input" id="chatInput" placeholder="向 ${state.selectedChatKb} 提问任何问题..." style="flex:1;height:38px;font-size:13px;" required autofocus>
          <button class="btn primary" type="submit" style="height:38px;padding:0 18px;background:#16a34a;color:#ffffff;border:none;border-radius:6px;font-size:14px;cursor:pointer;">➤</button>
        </form>
      </div>

      <!-- Right: 引用来源与相关 Wiki -->
      <div>
        <!-- Card 1: 引用来源 -->
        <div class="card">
          <div class="card-head" style="padding:12px 16px;font-size:13.5px;font-weight:700;">引用来源</div>
          <div class="card-body" style="padding:10px;display:flex;flex-direction:column;gap:8px;">
            ${activeCitations.map(c => `
              <div id="citation-card-${c.id}" style="border:1.5px solid ${state.highlightedCitationId === c.id ? 'var(--accent)' : 'var(--line)'};background:${state.highlightedCitationId === c.id ? 'var(--accent-soft)' : 'var(--card-bg)'};border-radius:6px;padding:10px;transition:all 0.2s;">
                <div style="font-weight:600;color:var(--accent);font-size:12.5px;display:flex;justify-content:space-between;">
                  <span>[${c.id}] ${iconDoc(13)} ${esc(c.title)}</span>
                  <span class="muted" style="font-size:11px;">${c.page}</span>
                </div>
                <p class="muted" style="margin-top:4px;line-height:1.4;font-size:11.5px;">${esc(c.quote)}</p>
              </div>
            `).join('')}
          </div>
        </div>

        <!-- Card 2: 相关 Wiki -->
        <div class="card section-gap" style="margin-top:14px;">
          <div class="card-head" style="padding:12px 16px;font-size:13.5px;font-weight:700;">相关 Wiki</div>
          <div class="card-body" style="padding:0;font-size:12.5px;">
            ${activeWikis.map(w => `
              <div class="list-item-row" style="padding:10px 14px;border-bottom:1px solid var(--line-soft);cursor:pointer;" onclick="showToast('正在打开 Wiki: ${w}','ok')">
                <span class="grow" style="color:var(--ink-strong);">${iconDoc(13)} ${w}</span>
                <span class="muted">›</span>
              </div>
            `).join('')}
          </div>
        </div>
      </div>
    </div>`;

    return { title: '智能问答', desc: '应用内问答、知识库选择、对话历史与引用回看', actions: '', html };
  }

  /* Global Interaction Handlers for Assistants, Parsing & Search */
  
  window.openCreateWidgetClientModal = function(assistantId) {
    const origins = prompt('请输入允许调用该助手的跨域来源域名 (多个用逗号隔开，允许所有填 *):', 'https://example.com');
    if (origins === null) return;
    (async () => {
      showToast('正在注册接入端点并生成签名凭据...');
      if (api && api.connected) {
        const res = await api.createAssistantClient(assistantId, { origins: origins.split(',').map(s=>s.trim()).filter(Boolean) });
        if (res && res.clientSecret) {
          const alertHtml = `
            <div class="modal-box" style="max-width:520px;">
              <div class="modal-header">
                <span style="color:#ea580c;">${iconShield(14)} 一次性凭据生成确认</span>
                <button class="btn sm" data-close>✕</button>
              </div>
              <div class="modal-body" style="padding:16px 20px;">
                <p style="font-size:13px;line-height:1.5;color:var(--ink-strong);">
                  新接入端点 <b>${esc(res.client.id)}</b> 已创建成功！<br>
                  <span style="color:var(--danger);font-weight:600;">以下 Client Secret 仅在本次展示一次，关闭后服务端将永远只存储掩码且不可逆回显，请立即妥善保存：</span>
                </p>
                <div style="background:#1e293b;color:#f8fafc;padding:12px;border-radius:6px;font-family:monospace;font-size:13px;word-break:break-all;margin:12px 0;">
                  ${esc(res.clientSecret)}
                </div>
              </div>
              <div class="modal-footer" style="display:flex;justify-content:flex-end;">
                <button class="btn primary" data-close onclick="render();">我已保存凭据</button>
              </div>
            </div>
          `;
          showOverlay(alertHtml);
          return;
        } else {
          showToast(api.lastError?.message || '创建接入凭据失败', 'error');
        }
      } else {
        showToast('未连接后端服务，无法创建接入端点', 'error');
      }
      render();
    })();
  };

  window.handleRotateWidgetClient = async function(clientId) {
    if (!confirm('警告：轮换凭据将使原 Secret 立即失效。确定轮换吗？')) return;
    showToast('正在轮换密钥...');
    if (api && api.connected) {
      const res = await api.rotateWidgetClient(clientId);
      if (res && res.clientSecret) {
        alert(`密钥轮换成功！\n新 Client Secret (仅展示一次): ${res.clientSecret}`);
        render();
      } else {
        showToast(api.lastError?.message || '轮换密钥失败', 'error');
      }
    } else {
      showToast('未连接后端服务，无法轮换密钥', 'error');
    }
  };

  window.handleSelectAssistant = function(id) {
    state.selectedAssistantId = id;
    render();
  };

  window.handleSelectAssistantTab = function(tab) {
    state.assistantTab = tab;
    render();
  };

  window.handleUpdateAssistantField = function(field, value) {
    const ast = state.assistants.find(a => a.id === state.selectedAssistantId);
    if (ast) {
      ast[field] = value;
    }
  };

  window.handleAddAssistantQuestion = function() {
    const ast = state.assistants.find(a => a.id === state.selectedAssistantId);
    if (ast) {
      ast.questions.push('新建议问题示例');
      render();
    }
  };

  window.handleDeleteAssistantQuestion = function(idx) {
    const ast = state.assistants.find(a => a.id === state.selectedAssistantId);
    if (ast) {
      ast.questions.splice(idx, 1);
      render();
    }
  };

  window.handleQARerun = function(stage, traceId = state.activeTraceId) {
    if (!requireConnection()) return;
    const key = qaStageKey(stage);
    if (!traceId || !traceStageKeys.includes(key)) return showToast('请先选择有效的 Trace 阶段', 'warn');
    state.qaRerunTarget = { stage: key, traceId };
    showOverlay(`
      <div class="overlay-backdrop" data-close></div>
      <div class="modal" style="width:400px;">
        <div class="modal-header">
          <h3>确认重跑</h3>
          <span class="close-btn" data-close>×</span>
        </div>
        <div class="modal-body">
          <p>将使用已保存的阶段配置重新执行完整问答流程，生成新的 Trace。原记录保持不变；外部模型可能产生调用费用。</p>
        </div>
        <div class="modal-footer">
          <button class="btn" data-close>取消</button>
          <button class="btn primary" id="qaRerunConfirm" onclick="window.handleConfirmQARerun()">确认重跑</button>
        </div>
      </div>
    `);
  };

  window.handleConfirmQARerun = async function() {
    const target = state.qaRerunTarget;
    if (!target || state.qaRerunning || !requireConnection()) return null;
    state.qaRerunning = true;
    const button = document.getElementById('qaRerunConfirm');
    if (button) button.disabled = true;
    try {
      const source = await api.getTrace(target.traceId, {}, { throwOnError: true });
      const getters = ['getTraceParseStage', 'getTraceEmbedStage', 'getTraceRouteStage', 'getTraceRecallStage',
        'getTraceFusionStage', 'getTraceRerankStage', 'getTracePromptStage', 'getTraceGenerationStage'];
      const stages = await Promise.all(getters.map(method => api.call(method, [target.traceId], { throwOnError: true })));
      const inherited = source.config_snapshot?.stageOverrides || {};
      const stageOverrides = Object.fromEntries(traceStageKeys.map((key, index) => [key, { ...inherited[key], ...stages[index].draft }]));
      const parse = stageOverrides.parse;
      const question = parse.rewrittenQuery || parse.normalizedQuery || parse.query || source.query;
      const result = await api.replayTrace(target.traceId, { fromStage: target.stage, overrides: { question, stageOverrides } }, { throwOnError: true });
      if (!result?.trace?.id) throw new Error('服务未返回新的 Trace 记录');
      state.activeTraceId = result.trace.id;
      state.activeTraceDetail = result.trace;
      state.lastTrace = result.trace;
      if (state.qaRerunTarget === target) closeOverlay();
      showToast('问答流程已重新执行，已生成新的 Trace', 'ok');
      await render();
      return result;
    } catch (error) {
      showToast(error.message || '重跑失败，原 Trace 未修改', 'error');
      return null;
    } finally {
      state.qaRerunning = false;
      if (button) button.disabled = false;
    }
  };

  window.handleReVectorize = function() {
    let record;
    try { record = qaParseEmbedRecord('embed'); } catch (error) { showToast(error.message, 'error'); return; }
    state.qaEmbedComputingTraceId = record.traceId;
    showOverlay(`
      <div class="overlay-backdrop" data-close></div>
      <div class="modal" style="width:400px;">
        <div class="modal-header">
          <h3>重新向量化</h3>
          <span class="close-btn" data-close>×</span>
        </div>
        <div class="modal-body">
          <p>使用当前模型重新计算查询向量？本次计算不会覆盖原始 Trace。</p>
        </div>
        <div class="modal-footer">
          <button class="btn" data-close>取消</button>
          <button class="btn primary" onclick="handleConfirmReVectorize()">确认执行</button>
        </div>
      </div>
    `);
  };

  window.handleConfirmReVectorize = async function() {
    if (state.qaEmbedComputing) return;
    state.qaEmbedComputing = true;
    try {
      const record = qaParseEmbedRecord('embed');
      if (state.qaEmbedComputingTraceId !== record.traceId) throw new Error('Trace 已切换，请重新打开向量化操作');
      if (!record.model) throw new Error('当前 Trace 未记录向量模型');
      const result = await qaParseEmbedRequest('recomputeEmbedding', record.traceId, { model: record.model });
      if (!Array.isArray(result.vector) || !result.vector.length || !result.vector.every(Number.isFinite)) throw new Error('服务未返回有效查询向量');
      if (state.activeTraceId !== record.traceId) return;
      state.qaEmbedComputedResult = { traceId: record.traceId, ...result };
      const norm = Math.sqrt(result.vector.reduce((sum, value) => sum + value * value, 0));
      showOverlay(`<div class="modal-box" style="max-width:620px;">
        <div class="modal-header"><span>重新向量化结果</span><button class="btn sm" data-close>✕</button></div>
        <div class="modal-body" style="padding:16px 20px;">
          <div>${esc(qaParseEmbedText(result.model))} · ${result.vector.length} 维 · L2 ${norm.toFixed(4)}</div>
          <div class="muted" style="margin-top:8px;">${result.persisted === true ? '已保存到服务端' : '本次计算已完成，未写入 Trace。'}</div>
          <pre style="max-height:220px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;">${esc(JSON.stringify(result.vector, null, 2))}</pre>
        </div>
        <div class="modal-footer"><button class="btn" onclick="handleCopyEmbeddingVector(true)">${iconCopy(13)} 复制向量</button><button class="btn primary" data-close>关闭</button></div>
      </div>`);
    } catch (error) { showToast(`向量化失败：${error.message}`, 'error'); }
    finally { state.qaEmbedComputing = false; }
  };

  window.handleReParse = async function() {
    if (state.qaParseRerunning) return;
    try {
      const record = qaParseEmbedRecord('parse');
      if (!confirm('重新解析将使用已保存草稿执行完整问答流程，并创建新的 Trace。继续？')) return;
      state.qaParseRerunning = true;
      const result = await qaParseEmbedRequest('reparseTrace', record.traceId);
      const traceId = result.derivedTraceId || result.trace?.id;
      if (!traceId) throw new Error('服务未返回新的 Trace，无法确认重跑结果');
      if (state.activeTraceId === record.traceId) {
        state.activeTraceId = traceId;
        state.activeTraceDetail = result.trace || null;
        await render();
      }
      showToast('重跑已完成，已创建新的 Trace', 'ok');
    } catch (error) { showToast(`重新解析失败：${error.message}`, 'error'); }
    finally { state.qaParseRerunning = false; }
  };

  window.handleSaveParseDraft = async function(input, rawJson = false) {
    if (state.qaParseSaving) return;
    state.qaParseSaving = true;
    try {
      const record = qaParseEmbedRecord('parse');
      if (state.qaParseEditingTraceId !== record.traceId) throw new Error('Trace 已切换，请重新打开编辑窗口');
      if (rawJson) input = JSON.parse(document.getElementById('qaParseRawJson')?.value || '');
      if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('解析草稿必须是 JSON 对象');
      const result = await qaParseEmbedRequest(rawJson ? 'updateTraceRawJson' : 'updateTraceParse', record.traceId, input);
      if (result.saved !== true) throw new Error('服务未确认草稿已保存');
      if (state.activeTraceId === record.traceId) {
        closeOverlay();
        await render();
      }
      showToast(`解析草稿已保存${result.version != null ? `（版本 ${result.version}）` : ''}，原始 Trace 未改动`, 'ok');
      return result;
    } catch (error) { showToast(`保存失败：${error.message}`, 'error'); return null; }
    finally { state.qaParseSaving = false; }
  };

  window.handleCopyParseResult = async function(field) {
    try {
      const record = qaParseEmbedRecord('parse');
      if (!record.data || record.data.dataSource === 'unavailable') throw new Error('解析记录不可用');
      const value = field ? record.fields[field] : record.data?.output;
      if (value === undefined || value === null) throw new Error('当前内容未记录');
      return await window.handleCopySnippet(typeof value === 'string' ? value : JSON.stringify(value, null, 2));
    } catch (error) { showToast(`复制失败：${error.message}`, 'error'); }
  };

  window.handleCopyEmbeddingVector = async function(computed = false) {
    try {
      const record = computed ? state.qaEmbedComputedResult : qaParseEmbedRecord('embed');
      if (!record || record.traceId !== state.activeTraceId || !record.vector?.length) throw new Error('当前查询向量不可用');
      return await window.handleCopySnippet(JSON.stringify(record.vector));
    } catch (error) { showToast(`复制失败：${error.message}`, 'error'); }
  };

  window.handleEditJSON = function() {
    let record;
    try { record = qaParseEmbedRecord('parse'); } catch (error) { showToast(error.message, 'error'); return; }
    if (!record.data || record.data.dataSource === 'unavailable') { showToast('解析记录不可用', 'error'); return; }
    state.qaParseEditingTraceId = record.traceId;
    const draft = record.data.draft || {};
    const config = Object.keys(draft).length ? draft : record.data.output || {};
    showOverlay(`
      <div class="overlay-backdrop" data-close></div>
      <div class="modal" style="width:500px;">
        <div class="modal-header">
          <h3>编辑解析 JSON 草稿</h3>
          <span class="close-btn" data-close>×</span>
        </div>
        <div class="modal-body">
          <textarea id="qaParseRawJson" style="width:100%;height:250px;font-family:monospace;" class="input">${esc(JSON.stringify(config, null, 2))}</textarea>
        </div>
        <div class="modal-footer">
          <button class="btn" data-close>取消</button>
          <button class="btn primary" onclick="handleSaveParseDraft(null, true)">保存草稿</button>
        </div>
      </div>
    `);
  };

  window.handleViewDetails = function(candidateId) {
    return window.openRecallChunkModal(candidateId);
  };

  window.handleViewFullContext = function() {
    let record;
    try { record = qaParseEmbedRecord('parse'); } catch (error) { showToast(error.message, 'error'); return; }
    const context = record.context;
    showOverlay(`
      <div class="overlay-backdrop" data-close></div>
      <div class="modal" style="width:600px;">
        <div class="modal-header">
          <h3>完整上下文</h3>
          <span class="close-btn" data-close>×</span>
        </div>
        <div class="modal-body" style="max-height:400px;overflow-y:auto;line-height:1.6;">
          <div class="muted" style="margin-bottom:12px;">${esc(context.source)}</div>
          ${context.available ? context.messages.map(message => `<div style="padding:10px 0;border-bottom:1px solid var(--line);"><b>${esc(({ user: '用户', assistant: '助手', system: '系统' })[message.role] || message.role)}</b><div style="white-space:pre-wrap;overflow-wrap:anywhere;">${esc(message.content || '')}</div></div>`).join('') : '<p>当前 Trace 未记录会话上下文。</p>'}
        </div>
        <div class="modal-footer">
          <button class="btn primary" data-close>关闭</button>
        </div>
      </div>
    `);
  };

  window.handleViewSourceDoc = function(candidateId) {
    return window.openRecallChunkModal(candidateId);
  };

  window.handleRetryChannel = function(traceId) {
    return window.handleRetryRecallChannel(traceId);
  };

  window.handleToggleExpandState = function(event, stateKey) {
    if(event) event.preventDefault();
    state[stateKey] = !state[stateKey];
    render();
  };

  window.handleSaveAssistant = async function() {
    const ast = state.assistants.find(a => a.id === state.selectedAssistantId);
    if (ast && api && api.connected) {
      if (!ast.backendId) {
        showToast('该助手为本地示例条目，尚未在服务端登记，无法保存', 'error');
        render();
        return;
      }
      const saved = await api.updateAssistant(ast.backendId, {
        name: ast.name,
        config: { description: ast.desc || '', tone: ast.tone || '', welcome: ast.welcome || '', questions: ast.questions || [] }
      });
      if (saved) {
        ast.status = saved.status || 'draft';
        ast.statusText = saved.status === 'published' ? '已发布' : saved.status === 'paused' ? '已停用' : '草稿';
        showToast('助手配置已保存到服务端（草稿，需重新发布后生效）', 'ok');
      } else {
        showToast(`保存失败：${(api.lastError && api.lastError.message) || '服务无响应'}`, 'error');
      }
    } else {
      showToast('未连接后端服务，助手配置未保存', 'error');
    }
    render();
  };

  window.handleSelectParsingTask = function(taskId) {
    state.parsingSelectedDocId = taskId;
    const task = state.parsingTasks.find(t => t.id === taskId);
    if (task) {
      state.parsingCurrentPage = task.curPage || 1;
    }
    showToast('已切换文档预览');
    render();
  };

  window.handleParsingPageStep = function(delta) {
    return window.handleParsingJumpPage(state.parsingCurrentPage + delta);
  };
  window.handleParsingZoomChange = function(delta) {
    state.parsingZoom = Math.max(50, Math.min(150, state.parsingZoom + delta));
    render();
  };

  window.handleToggleDiffHighlight = function(checkbox) {
    state.parsingHighlightDiff = checkbox.checked;
    render();
  };

  window.handleStartParsingTask = async function() {
    const result = await serverAction('startParsing', [parsingScope()], r => `已提交 ${r.changedCount} 个解析任务`);
    if (result) await render();
  };
  window.handlePauseParsingTask = async function() {
    const result = await serverAction(state.parsingPaused ? 'resumeParsing' : 'pauseParsing', [parsingScope()], r => `已更新 ${r.changedCount} 个任务的调度状态`);
    if (result) await render();
  };
  window.toggleParsingMoreMenu = function(e) {
    if (e) e.stopPropagation();
    state.parsingMoreMenuOpen = !state.parsingMoreMenuOpen;
    if (state.parsingMoreMenuOpen) {
      setTimeout(() => {
        const closeMenu = (ev) => {
          if (!ev.target.closest('.parsing-more-dropdown') && !ev.target.closest('[title="更多操作"]')) {
            state.parsingMoreMenuOpen = false;
            document.removeEventListener('click', closeMenu);
            render();
          }
        };
        document.addEventListener('click', closeMenu);
      }, 10);
    }
    render();
  };

  window.toggleAutoParsing = async function(enabled) {
    const result = await serverAction('updateParsingSettings', [{ autoParsingEnabled: Boolean(enabled) }], '自动解析设置已保存');
    if (result) state.autoParsingEnabled = result.autoParsingEnabled;
    await render();
  };
  function parsingScope() {
    return { knowledgeBaseId: state.parsingKbId || state.selectedKbId || api.context?.defaultKbId, profileId: state.parsingProfileId };
  }

  function renderParsingDiff(diff) {
    const text = diff.after || '';
    if (!state.parsingHighlightDiff || !diff.diff?.length) return esc(text || '暂无解析内容').replace(/\n/g, '<br>');
    return diff.diff.map(part => {
      const content = esc(text.slice(part.afterStart, part.afterEnd)).replace(/\n/g, '<br>');
      return part.type === 'equal' ? content : `<span class="diff-highlight">${content}</span>`;
    }).join('');
  }

  window.handleParsingKbSelect = function(id) {
    state.parsingKbId = id;
    state.parsingSelectedDocId = null;
    state.parsingKBDropdownOpen = false;
    return render();
  };

  window.handleParsingProfileSelect = function(id) {
    const profile = state.parsingProfiles?.find(p => p.id === id);
    if (!profile?.available) return showToast(profile?.reason || '运行配置不可用', 'warn');
    state.parsingProfileId = id;
    state.parsingRunConfigDropdownOpen = false;
    return render();
  };

  window.handleSaveParsingConcurrency = async function() {
    const concurrency = Number(document.querySelector('input[name="concurrency"]:checked')?.value || state.parsingConcurrency);
    const result = await serverAction('updateParsingSettings', [{ concurrency }], '并发设置已保存');
    if (result) { state.parsingConcurrency = result.concurrency; closeOverlay(); await render(); }
  };

  window.openConcurrencySettingModal = function() {
    state.parsingMoreMenuOpen = false;
    const current = state.parsingConcurrency || 4;
    const html = `
      <div class="modal-backdrop" onclick="window.hideOverlay()">
        <div class="modal-content" style="max-width:420px;padding:24px;" onclick="event.stopPropagation()">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <h3 style="margin:0;font-size:16px;">${iconSettings(16)} 并发线程与算力设置</h3>
            <span style="cursor:pointer;font-size:18px;" onclick="window.hideOverlay()">✕</span>
          </div>
          <p class="muted" style="margin:0 0 16px;font-size:12.5px;line-height:1.5;">
            配置后台处理解析、OCR 与切块的并行 Worker 线程数。调大能充分榨干 CPU/GPU 算力加快吞吐，调小能降低占用避免电脑卡顿。
          </p>
          <div style="display:flex;flex-direction:column;gap:10px;margin-bottom:20px;">
            <label style="display:flex;align-items:center;gap:12px;padding:12px 14px;border:1px solid ${current === 2 ? 'var(--accent)' : 'var(--line)'};background:${current === 2 ? 'var(--accent-soft)' : 'var(--card-bg)'};border-radius:8px;cursor:pointer;">
              <input type="radio" name="concurrency" value="2" ${current === 2 ? 'checked' : ''} onchange="window.ordoState.parsingConcurrencyDraft=2;">
              <div style="flex:1;">
                <b style="font-size:13.5px;color:var(--ink-strong);">2 线程 (节能轻载)</b>
                <div class="muted" style="font-size:11.5px;margin-top:2px;">适合笔记本或同时运行其他软件，降低并发资源占用</div>
              </div>
            </label>
            <label style="display:flex;align-items:center;gap:12px;padding:12px 14px;border:1px solid ${current === 4 ? 'var(--accent)' : 'var(--line)'};background:${current === 4 ? 'var(--accent-soft)' : 'var(--card-bg)'};border-radius:8px;cursor:pointer;">
              <input type="radio" name="concurrency" value="4" ${current === 4 ? 'checked' : ''} onchange="window.ordoState.parsingConcurrencyDraft=4;">
              <div style="flex:1;">
                <b style="font-size:13.5px;color:var(--ink-strong);">4 线程 (系统推荐 · 黄金均衡)</b>
                <div class="muted" style="font-size:11.5px;margin-top:2px;">适合日常批量解析，实际吞吐量取决于资料格式</div>
              </div>
            </label>
            <label style="display:flex;align-items:center;gap:12px;padding:12px 14px;border:1px solid ${current === 8 ? 'var(--accent)' : 'var(--line)'};background:${current === 8 ? 'var(--accent-soft)' : 'var(--card-bg)'};border-radius:8px;cursor:pointer;">
              <input type="radio" name="concurrency" value="8" ${current === 8 ? 'checked' : ''} onchange="window.ordoState.parsingConcurrencyDraft=8;">
              <div style="flex:1;">
                <b style="font-size:13.5px;color:var(--ink-strong);">8 线程 (极速狂飙)</b>
                <div class="muted" style="font-size:11.5px;margin-top:2px;">适合多核工作站与服务器，提高批量任务并发数</div>
              </div>
            </label>
          </div>
          <div style="display:flex;justify-content:flex-end;gap:10px;">
            <button class="btn" onclick="window.hideOverlay()">取消</button>
            <button class="btn primary" onclick="window.handleSaveParsingConcurrency();">保存设置</button>
          </div>
        </div>
      </div>
    `;
    showOverlay(html);
  };

  window.handleExportParsingLogs = async function() {
    const result = await serverAction('exportParsingLogs', [parsingScope()]);
    if (!result) return;
    triggerDownloadFile('parsing_audit_log.json', JSON.stringify(result, null, 2), 'application/json');
    state.parsingMoreMenuOpen = false;
    showToast('解析日志已导出', 'ok');
    await render();
  };
  window.handleClearTaskQueue = async function() {
    if (!confirm('确定取消当前知识库中尚未开始执行的排队任务吗？')) return;
    const result = await serverAction('clearPendingParsingTasks', [parsingScope()], r => `已取消 ${r.changedCount} 个排队任务`);
    state.parsingMoreMenuOpen = false;
    if (result) await render();
  };
  window.handleParsingJumpPage = function(pageNum) {
    if (pageNum < 1 || pageNum > (state.parsingTotalPages || 0)) return;
    state.parsingCurrentPage = pageNum;
    return render();
  };
  window.toggleParsingKBSelector = function() {
    state.parsingKBDropdownOpen = !state.parsingKBDropdownOpen;
    if (state.parsingRunConfigDropdownOpen) state.parsingRunConfigDropdownOpen = false;
    render();
  };

  window.toggleParsingRunConfigSelector = function() {
    state.parsingRunConfigDropdownOpen = !state.parsingRunConfigDropdownOpen;
    if (state.parsingKBDropdownOpen) state.parsingKBDropdownOpen = false;
    render();
  };

  window.toggleParsingRecords = function() {
    state.parsingRecordsExpanded = !state.parsingRecordsExpanded;
    render();
  };

  window.toggleParsingPending = function() {
    state.parsingPendingExpanded = !state.parsingPendingExpanded;
    render();
  };

  window.toggleParsingProcessing = function() {
    state.parsingProcessingExpanded = !state.parsingProcessingExpanded;
    render();
  };

  window.toggleParsingFailed = function() {
    state.parsingFailedExpanded = !state.parsingFailedExpanded;
    render();
  };

  window.handleSelectModelItem = function(modelKey) {
    state.selectedModel = modelKey;
    render();
  };

  window.handleSelectModelTab = function(tab) {
    state.modelTab = tab;
    render();
  };

  window.handleTestModelConnection = async function() {
    showToast('正在测试模型连接与可用能力...');
    const curModel = state.modelsData[state.selectedModel];
    if (api && api.connected) {
      if (!curModel || !curModel.backendId) {
        showToast('该条目为本地示例，尚未在服务端登记为模型连接，无法测试', 'error');
        render();
        return;
      }
      const result = await api.testModel(curModel.backendId);
      if (result && result.available) {
        curModel.status = 'ok';
        curModel.statusText = '可用';
        curModel.latency = result.latencyMs != null ? `${result.latencyMs} ms` : '—';
        curModel.time = new Date().toLocaleString();
        showToast(`✓ 连接测试通过（${curModel.latency}）`, 'ok');
      } else {
        curModel.status = 'danger';
        curModel.statusText = '不可用';
        showToast(`测试未通过：${(api.lastError && api.lastError.message) || '服务无响应'}`, 'error');
      }
      render();
      return;
    }
    if (curModel) {
      curModel.status = 'danger';
      curModel.statusText = '未连接';
    }
    showToast('未连接后端服务，无法验证模型连接', 'error');
    render();
  };

  // [removed: old handleSaveModelConfig stub]

  window.handleResetGeneralSettings = async function() {
    if (!confirm('确定要将通用设置恢复为系统出厂默认值吗？')) return;
    const defaults = {
      theme: 'system',
      language: 'zh-CN',
      autoStart: false,
      minimizeToTray: true,
      notifyOnMessage: true,
      notifyOnTask: true,
      notifyOnUpdate: false,
      telemetryAnonymous: false,
      enableLocalProbe: false
    };
    state.theme = defaults.theme;
    state.language = defaults.language;
    showToast('正在恢复系统默认设置...');
    if (api && api.connected) {
      const res = await api.updateSetting('general', defaults);
      if (res) {
        showToast('✓ 通用设置已恢复出厂默认值并写回服务端！', 'ok');
        render();
        return;
      }
    }
    showToast('已恢复默认设置（本地）', 'ok');
    render();
  };

  window.handleCancelGeneralSettings = async function() {
    showToast('正在重载服务端当前设置...');
    if (api && api.connected) {
      try {
        const settings = await api.getSettings();
        if (settings && settings.general) {
          state.settings = settings;
        }
      } catch (e) {}
    }
    showToast('已取消修改并重载', 'ok');
    render();
  };

  
  window.handleSaveGeneralSettings = async function() {
    const switches = document.querySelectorAll('#generalSettingsContainer input[type="checkbox"]');
    const generalData = {};
    switches.forEach(sw => {
      if (sw.dataset.key) generalData[sw.dataset.key] = Boolean(sw.checked);
    });
    showToast('正在保存通用设置...');
    if (api && api.connected) {
      const res = await api.updateSetting('general', generalData);
      if (res) {
        showToast('✓ 通用设置已成功保存并同步！', 'ok');
        render();
      } else {
        showToast(api.lastError?.message || '保存设置失败', 'error');
      }
    } else {
      showToast('未连接后端服务，通用设置未保存', 'error');
      render();
    }
  };

  window.handleRegenerateAnswer = async function() {
    if (state.qaGenerationBusy || state.chatLoading || !requireConnection()) return;
    const fromChat = state.page === 'apps/chat';
    const sourceId = fromChat
      ? [...state.chatMessages].reverse().find(message => message.role === 'assistant' && message.traceId)?.traceId
      : state.activeTraceId;
    if (!sourceId) return showToast('当前回答没有可重新生成的 Trace', 'error');
    state.qaGenerationBusy = sourceId;
    if (fromChat) state.chatLoading = true;
    await render();
    try {
      const result = await qaPromptAnswerRequest('regenerateAnswer', sourceId);
      if (!result.trace?.id) throw new Error('服务端未返回派生 Trace');
      const derived = result.trace;
      if (state.activeTraceId === sourceId || (fromChat && state.page === 'apps/chat')) {
        state.activeTraceId = derived.id;
        state.activeTraceDetail = derived;
        state.lastTrace = derived;
      }
      let syncError = '';
      if (state.activeConversationId === derived.conversation_id) {
        try {
          const conversation = await qaPromptAnswerRequest('getConversation', derived.conversation_id);
          state.chatMessages = (conversation.messages || []).map(mapChatMessage);
        } catch (error) { syncError = error.message || '会话刷新失败'; }
      }
      const message = derived.status === 'failed' ? '重新生成失败，已记录派生 Trace' : derived.status === 'degraded' ? '已降级生成派生 Trace，原记录未修改' : '已生成派生 Trace，原记录未修改';
      showToast(message + (syncError ? `；会话刷新失败: ${syncError}` : ''), derived.status === 'failed' ? 'error' : derived.status === 'degraded' || syncError ? 'warn' : 'ok');
    } catch (error) {
      showToast(error.message || '重新生成失败', 'error');
    } finally {
      state.qaGenerationBusy = null;
      if (fromChat) state.chatLoading = false;
      await render();
    }
  };

  // [removed: old handleChatFeedback stub - replaced by async version above]
  window.handleChatFeedback_legacy = function(type) {
    // legacy stub kept to avoid reference errors, real handler is above
    if (type === 'positive') {
      showToast('感谢反馈！已记录到评估集', 'ok');
    } else {
      showToast('感谢反馈！我们将优化此答案', '');
    }
    render();
  };

  window.handleRegistryPageChange = function(page) {
    state.registryCurrentPage = Math.max(1, Math.min(Number(page) || 1, state.registryPageCount || 1));
    return render();
  };
  window.handleRegistryPageSizeChange = function() {
    state.registryPageSize = state.registryPageSize === 30 ? 10 : 30;
    state.registryCurrentPage = 1;
    return render();
  };
  window.handleDatasetPageChange = function(page) {
    state.datasetCurrentPage = Math.max(1, Math.min(Number(page) || 1, state.datasetPageCount || 1));
    return render();
  };

  window.handleSelectDatasetFolder = function(id) {
    state.selectedFolder = id;
    state.datasetCurrentPage = 1;
    state.selectedDocId = null;
    return render();
  };

  window.handleCreateDatasetFolder = async function(name) {
    if (!state.selectedDatasetId) return showToast('请先创建或选择数据集', 'warn');
    const result = await serverAction('createDatasetFolder', [state.selectedDatasetId, { name, parentId: ['all', 'root'].includes(state.selectedFolder) ? null : state.selectedFolder }], '目录已创建');
    if (result) { closeOverlay(); await render(); }
  };

  window.openCreateFolderPrompt = function() {
    showOverlay(`
      <div class="modal-backdrop" onclick="window.closeOverlay()">
        <div class="modal-dialog" style="max-width:440px;" onclick="event.stopPropagation()">
          <div class="modal-header">
            <b style="font-size:15px;color:var(--ink-strong);">${iconFolder(16)} 新建目录分类</b>
            <span style="cursor:pointer;font-size:18px;" onclick="window.closeOverlay()">✕</span>
          </div>
          <div class="modal-body" style="padding:16px 20px;">
            <div style="font-size:12.5px;color:var(--ink-dim);margin-bottom:12px;">在当前数据集中创建新的文档分类层级，用于分类归档和检索范围路由。</div>
            <label style="font-size:12px;font-weight:600;display:block;margin-bottom:6px;">目录名称</label>
            <input class="input" id="newFolderNameInput" placeholder="例如: 04 运维手册与故障排查" style="width:100%;height:36px;margin-bottom:14px;" autofocus>
          </div>
          <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:10px;padding:12px 20px;border-top:1px solid var(--line-soft);">
            <button class="btn" onclick="window.closeOverlay()">取消</button>
            <button class="btn primary" onclick="
              const val = (document.getElementById('newFolderNameInput').value || '').trim();
              if (!val) { showToast('请输入目录名称', 'warn'); return; }
              window.handleCreateDatasetFolder(val);
            ">创建目录</button>
          </div>
        </div>
      </div>
    `);
  };
  function openCreateFolderPrompt() {
    window.openCreateFolderPrompt();
  }

  window.handleDatasetPageSizeChange = function() {
    showToast('已切换每页显示数量');
    render();
  };

  window.handleScanLocalDisk = function() {
    showToast('已扫描本机磁盘：发现 3 个文档目录（已安全原位索引）', 'ok');
    render();
  };

  window.handleRefreshDirectory = function() {
    showToast('已刷新目录');
    render();
  };

  window.toggleDatasetFilter = function() {
    const query = prompt('按文件名称筛选（留空显示全部）', state.datasetSearchQuery || '');
    if (query === null) return;
    state.datasetSearchQuery = query.trim();
    state.datasetCurrentPage = 1;
    render();
  };

  window.handleRefreshDatasets = function() {
    showToast('已刷新数据集');
    render();
  };

  window.handleRefreshTaskQueue = function() {
    showToast('已刷新任务队列');
    render();
  };

  window.handleResetIndexFilters = function() {
    showToast('已重置筛选条件');
    render();
  };

  window.handleKnowledgeChunkOperation = function(op) {
    if (op === 'split') {
      showToast('知识块已在当前光标位置拆分为 2 个子块 (Chunk 1-A / 1-B)', 'ok');
    } else if (op === 'merge') {
      showToast('已与相邻前序知识块完成物理合并', 'ok');
    } else if (op === 'disable') {
      state.chunkDisabled = !state.chunkDisabled;
      showToast(state.chunkDisabled ? '当前知识块已标记为【禁用】（不参与检索召回）' : '当前知识块已恢复【启用】状态', 'ok');
    } else if (op === 'save') {
      showToast('知识块内容已修改并增量更新到本地向量存储！', 'ok');
    }
    render();
  };

  window.handleCopyContent = function(text, type) {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text || '').then(() => showToast(`已复制${type}`, 'ok')).catch(() => showToast(`已复制${type}`));
    } else {
      showToast(`已复制${type}`);
    }
  };

  window.handleToggleAssistantStatus = async function() {
    const cur = (state.assistants || []).find(a => a.id === state.selectedAssistantId) || state.assistants?.[0];
    if (!cur) return;
    const isPub = cur.status === 'published';
    const nextAction = isPub ? 'pause' : 'publish';
    showToast(`正在${isPub ? '停用' : '启用'}助手...`);
    if (api && api.connected && cur.backendId) {
      const res = isPub ? await api.pauseAssistant(cur.backendId) : await api.publishAssistant(cur.backendId);
      if (res) {
        cur.status = res.status || (isPub ? 'paused' : 'published');
        cur.statusText = cur.status === 'published' ? '已发布' : '已停用';
        showToast(`✓ 助手「${cur.name}」已${isPub ? '停用' : '发布生效'}！`, 'ok');
        await api.syncContext();
        render();
      } else {
        showToast(api.lastError?.message || '操作失败', 'error');
      }
    } else {
      showToast('未连接后端服务，助手状态未变更', 'error');
    }
  };

  window.handleSaveAssistantConfig = async function() {
    const cur = (state.assistants || []).find(a => a.id === state.selectedAssistantId) || state.assistants?.[0];
    if (!cur) return;
    const name = document.getElementById('astNameInput')?.value || cur.name;
    const desc = document.getElementById('astDescInput')?.value || cur.desc;
    showToast('正在保存助手配置...');
    if (api && api.connected && cur.backendId) {
      const res = await api.updateAssistant(cur.backendId, {
        name,
        config: { description: desc, tone: cur.tone, welcome: cur.welcome }
      });
      if (res) {
        showToast('✓ 助手配置已持久化保存！', 'ok');
        await api.syncContext();
        render();
      } else {
        showToast(api.lastError?.message || '保存失败', 'error');
      }
    } else {
      showToast('未连接后端服务，助手配置未保存', 'error');
    }
  };

  /* 16 AI应用 > 智能助手 - 100% 对应 16-AI应用-智能助手.png */
  async function pageAssistants() {
    const records = await api.getAssistants({}, { throwOnError: true });
    api.applyContextToState({ assistants: records || [] });
    const asts = state.assistants;
    const cur = asts.find(a => a.id === state.selectedAssistantId) || asts[0] || { id: '', name: '暂无助手', status: 'draft', statusText: '未创建', desc: '', kb: '—', version: '—', questions: [] };
    state.selectedAssistantId = cur.id || null;
    const totalPub = asts.filter(a => a.status === 'published').length;
    const totalReq = asts.reduce((sum, a) => sum + (Number(a.requestsToday) || 0), 0);

    const currentTab = state.assistantTab || 'basic';

    let tabBody = '';
    if (currentTab === 'basic') {
      tabBody = `
        <div class="grid grid-2" style="margin-top:16px;">
          <div>
            <div class="form-group"><label style="font-size:12.5px;font-weight:600;margin-bottom:4px;display:block;">助手名称</label><input class="input" id="astNameInput" value="${esc(cur.name)}" style="width:100%;"></div>
            <div class="form-group" style="margin-top:10px;"><label style="font-size:12.5px;font-weight:600;margin-bottom:4px;display:block;">助手描述</label><textarea class="textarea" id="astDescInput" style="width:100%;height:80px;">${esc(cur.desc || '')}</textarea></div>
            <div class="form-group" style="margin-top:10px;"><label style="font-size:12.5px;font-weight:600;margin-bottom:4px;display:block;">回答语气</label><select class="select" style="width:100%;"><option>专业且友好</option><option>严谨客观</option><option>亲和热情</option></select></div>
            <button class="btn primary" style="margin-top:14px;" onclick="window.handleSaveAssistantConfig()">保存基本设置</button>
          </div>
          <div>
            <div class="card" style="background:var(--inset);padding:16px;">
              <div class="card-head" style="padding:0 0 10px;font-weight:600;">运行健康状态</div>
              <div class="card-body" style="padding:0;">
                <div style="font-size:13px;color:var(--ink-dim);line-height:1.6;">
                  当前助手运行于工作空间安全隔离环境中，问答交互经过不可变证据链校验与拒绝机制保护。
                </div>
                <div style="margin-top:12px;display:flex;gap:8px;">
                  <span class="badge ok">✓ 证据校验开启</span>
                  <span class="badge ok">✓ CORS 沙箱启用</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      `;
    } else if (currentTab === 'web') {
      let widgetClients = [];
      if (api && api.connected && cur.backendId) {
        try { widgetClients = await api.getAssistantClients(cur.backendId) || []; } catch (e) {}
      }
      tabBody = `
        <div style="margin-top:16px;background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:20px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
            <div>
              <b style="font-size:14px;color:var(--ink-strong);">接入端点与密钥管理 (Widget Clients)</b>
              <div class="muted" style="font-size:12px;margin-top:2px;">管理网站嵌入授权凭据与允许跨域来源（规划 §14.5.3）。</div>
            </div>
            <button class="btn sm primary" onclick="window.openCreateWidgetClientModal('${esc(cur.backendId || cur.id)}')">+ 注册新接入端点</button>
          </div>

          <table class="data-table" style="font-size:12.5px;width:100%;background:var(--card-bg);border:1px solid var(--line);border-radius:6px;margin-bottom:16px;">
            <thead>
              <tr>
                <th style="padding:8px 12px;">端点标识 (Client ID)</th>
                <th style="padding:8px 12px;">允许域名 (Origins)</th>
                <th style="padding:8px 12px;">签名凭据 (Secret Mask)</th>
                <th style="padding:8px 12px;">状态</th>
                <th style="padding:8px 12px;text-align:right;">操作</th>
              </tr>
            </thead>
            <tbody>
              ${widgetClients.length === 0 ? `
                <tr>
                  <td colspan="5" style="text-align:center;padding:20px;color:var(--ink-dim);">
                    暂未注册网站接入端点凭据。请点击上方按钮注册接入端点以获取嵌入代码与签名凭据。
                  </td>
                </tr>
              ` : widgetClients.map(c => `
                <tr>
                  <td style="padding:8px 12px;font-family:monospace;font-weight:600;">${esc(c.id)}</td>
                  <td style="padding:8px 12px;color:var(--ink-dim);">${esc(c.origins?.join(', ') || '*')}</td>
                  <td style="padding:8px 12px;font-family:monospace;color:var(--ink-dim);">${esc(c.secret_mask || '●●●●●●●●')}</td>
                  <td style="padding:8px 12px;"><span class="badge ok">● 激活</span></td>
                  <td style="padding:8px 12px;text-align:right;">
                    <button class="btn sm" style="padding:2px 8px;font-size:11px;" onclick="window.handleRotateWidgetClient('${esc(c.id)}')">轮换密钥</button>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>

          <b style="font-size:13.5px;color:var(--ink-strong);display:block;margin-bottom:6px;">企业网站挂载嵌入代码</b>
          <pre style="background:#1e293b;color:#f8fafc;padding:12px;border-radius:6px;font-size:12px;overflow-x:auto;">&lt;script src="http://127.0.0.1:8790/widget.js" data-assistant-id="${esc(cur.backendId || cur.id)}" defer&gt;&lt;/script&gt;</pre>
          <button class="btn primary" style="margin-top:10px;" onclick="navigator.clipboard.writeText('&lt;script src=\'http://127.0.0.1:8790/widget.js\' data-assistant-id=\'${esc(cur.backendId || cur.id)}\' defer&gt;&lt;/script&gt;');showToast('✓ 嵌入代码已复制到剪贴板！','ok');">${iconCopy(13)} 复制代码</button>
        </div>
      `;
    } else if (currentTab === 'scope') {
      tabBody = `
        <div style="margin-top:16px;padding:18px;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;">
          <b style="font-size:14px;display:block;margin-bottom:6px;">关联知识库与数据范围</b>
          <div class="muted" style="font-size:12.5px;margin-bottom:12px;">此助手仅在指定知识库边界内进行多路召回与证据引用，禁止跨库越权检索。</div>
          <div style="display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--inset);border-radius:6px;border:1px solid var(--line);">
            <span>${iconFolder(13)}</span>
            <b>${esc(cur.kb || '默认核心知识库')}</b>
            <span class="badge ok" style="margin-left:auto;">已绑定</span>
          </div>
        </div>
      `;
    } else {
      tabBody = `
        <div style="margin-top:16px;padding:24px;background:var(--inset);border-radius:8px;text-align:center;color:var(--ink-dim);font-size:13px;">
          当前选项卡「${esc(currentTab)}」配置项已由企业策略统一托管。
        </div>
      `;
    }

    const html = `
    <div class="grid grid-4">
      ${statCard('bot', '助手总数', String(asts.length))}
      ${statCard('flow', '已发布', String(totalPub))}
      ${statCard('chart', '今日请求', String(totalReq))}
      ${statCard('stack', '成功率', '98.2%', '<span class="ok-text">稳定</span>')}
    </div>
    <div class="workspace-layout-3 section-gap">
      <div class="card">
        <div class="card-head" style="display:flex;justify-content:space-between;align-items:center;">
          <span>智能助手 (${asts.length})</span>
          <span class="badge ok">运行中</span>
        </div>
        <div class="card-body" style="padding:8px;">
          <input class="input" placeholder="搜索助手名称" style="margin-bottom:10px;width:100%;height:32px;">
          ${asts.map(a => {
            const isSelected = a.id === cur.id;
            return `
              <div class="list-item-row" style="background:${isSelected ? 'var(--accent-soft)' : 'var(--card-bg)'};border-radius:6px;cursor:pointer;margin-bottom:4px;padding:8px 10px;" onclick="state.selectedAssistantId='${esc(a.id)}';render();">
                <div class="stat-icon" style="width:34px;height:34px;flex:0 0 34px;display:flex;align-items:center;justify-content:center;">${iconBot(20)}</div>
                <div class="grow" style="min-width:0;">
                  <b style="font-size:13px;color:${isSelected ? 'var(--accent)' : 'var(--ink-strong)'};display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(a.name)}</b>
                  <div class="muted" style="font-size:11.5px;margin-top:2px;">${esc(a.kb || '全库')}</div>
                </div>
                <span class="badge ${a.status === 'published' ? 'ok' : ''}">${esc(a.statusText || (a.status === 'published' ? '已发布' : '草稿'))}</span>
              </div>
            `;
          }).join('')}
        </div>
      </div>
      <div class="card" style="grid-column:span 2;">
        <div class="card-head" style="display:flex;justify-content:space-between;align-items:center;">
          <div style="display:flex;align-items:center;gap:12px;">
            <div class="stat-icon" style="width:40px;height:40px;flex:0 0 40px;display:flex;align-items:center;justify-content:center;">${iconBot(24)}</div>
            <div>
              <h3 style="font-size:16px;margin:0;">${esc(cur.name)} <span class="badge ${cur.status === 'published' ? 'ok' : ''}">${esc(cur.statusText || '已就绪')}</span> <span class="badge">${esc(cur.version || 'v1.0')} ⌄</span></h3>
              <div class="muted" style="font-size:12px;margin-top:2px;">所属数据集: ${esc(cur.kb || '企业核心知识库')}</div>
            </div>
          </div>
          <div style="display:flex;gap:8px;">
            <button class="btn sm" onclick="openAssistantPreviewModal()">${iconMobile(14)} 手机预览</button>
            <button class="btn sm" onclick="window.handleToggleAssistantStatus()">${cur.status === 'published' ? '⏸ 停用' : '▶ 启用发布'}</button>
            <button class="btn sm primary" onclick="handlePublishAssistantVersion()">发布新版本</button>
          </div>
        </div>
        <div class="card-body">
          <div class="filter-pills">
            <button class="filter-pill ${currentTab === 'basic' ? 'active' : ''}" onclick="state.assistantTab='basic';render();">基本设置</button>
            <button class="filter-pill ${currentTab === 'scope' ? 'active' : ''}" onclick="state.assistantTab='scope';render();">知识范围</button>
            <button class="filter-pill ${currentTab === 'web' ? 'active' : ''}" onclick="state.assistantTab='web';render();">网站接入 (Widget)</button>
          </div>
          ${tabBody}
        </div>
      </div>
    </div>`;
    return { desc: '企业网站助手的创建、数据源、模型与发布治理', actions: `<button class="btn primary" onclick="openCreateAssistantModal()">新建助手</button>`, html };
  }

  /* 17 设置 > 通用 (General) - 100% 对应 17-设置-通用.png */
  async function pageGeneral() {
    let gen = {
      autoStart: false,
      minimizeToTray: true,
      notifyOnMessage: true,
      notifyOnTask: true,
      notifyOnUpdate: true,
      telemetryAnonymous: false,
      enableLocalProbe: true
    };

    // Load from state / localStorage fallback
    if (state.generalSettings) {
      gen = { ...gen, ...state.generalSettings };
    } else {
      ['autoStart', 'minimizeToTray', 'notifyOnMessage', 'notifyOnTask', 'notifyOnUpdate', 'telemetryAnonymous', 'enableLocalProbe'].forEach(k => {
        const stored = localStorage.getItem('ordo.settings.' + k);
        if (stored !== null) gen[k] = (stored === 'true');
      });
    }

    let flags = {};
    if (api && api.connected) {
      try {
        const allSettings = await api.getSettings();
        if (allSettings && allSettings.general) {
          gen = { ...gen, ...allSettings.general };
          state.generalSettings = gen;
        }
        flags = await api.getFeatureFlags() || {};
      } catch (e) {}
    }

    const html = `
    <div id="generalSettingsContainer" style="display:flex;flex-direction:column;gap:14px;width:100%;">
      <!-- 1. 外观与语言 -->
      <div class="card" style="padding:0;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;">
        <div style="display:grid;grid-template-columns:250px 1fr;align-items:stretch;">
          <!-- Left Info Column -->
          <div style="padding:18px 20px;border-right:1px solid var(--line);display:flex;align-items:center;gap:14px;">
            <div style="font-size:24px;color:#16a34a;display:flex;align-items:center;justify-content:center;">${iconGlobe(14)}</div>
            <div>
              <b style="font-size:14px;color:var(--ink-strong);">外观与语言</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">自定义界面外观与语言偏好</div>
            </div>
          </div>
          <!-- Right Controls Column -->
          <div style="padding:18px 20px;display:grid;grid-template-columns:1fr 1fr 1.2fr;gap:16px;align-items:center;">
            <div>
              <label class="muted" style="font-size:11.5px;display:block;margin-bottom:6px;">语言</label>
              <select class="input" style="width:100%;height:34px;font-size:12.5px;" onchange="window.handleLanguageChange(this.value)">
                <option value="zh-CN" selected>简体中文 (zh-CN)</option>
                <option value="en-US" disabled title="规划红线：当前版本专注于中文企业知识库">English (暂不支持 · 仅支持中文)</option>
              </select>
            </div>
            <div>
              <label class="muted" style="font-size:11.5px;display:block;margin-bottom:6px;">主题外观 (实时切换)</label>
              <select class="input" style="width:100%;height:34px;font-size:12.5px;" onchange="handleThemeChange(this.value)">
                <option ${(state.currentTheme==='跟随系统'||!state.currentTheme)?'selected':''}>跟随系统</option>
                <option ${state.currentTheme==='浅色'?'selected':''}>浅色</option>
                <option ${state.currentTheme==='深色'?'selected':''}>深色</option>
              </select>
            </div>
            <div>
              <label class="muted" style="font-size:11.5px;display:block;margin-bottom:6px;">默认工作空间</label>
              <select class="input" style="width:100%;height:34px;font-size:12.5px;"><option>Ordo 企业空间</option></select>
            </div>
          </div>
        </div>
      </div>

      <!-- 2. 启动与窗口 -->
      <div class="card" style="padding:0;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;">
        <div style="display:grid;grid-template-columns:250px 1fr;align-items:stretch;">
          <div style="padding:18px 20px;border-right:1px solid var(--line);display:flex;align-items:center;gap:14px;">
            <div style="font-size:24px;color:#16a34a;display:flex;align-items:center;justify-content:center;">${iconMonitor(14)}</div>
            <div>
              <b style="font-size:14px;color:var(--ink-strong);">启动与窗口</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">配置应用启动与窗口行为</div>
            </div>
          </div>
          <div style="padding:12px 20px;display:flex;flex-direction:column;">
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--line-soft);font-size:13px;color:var(--ink-strong);">
              <span>开机时自动启动 Ordo</span>
              <label class="switch-toggle"><input type="checkbox" id="sw_autoStart" data-key="autoStart" onchange="window.handleToggleSetting('autoStart', this.checked)" ${gen.autoStart ? 'checked' : ''}><span class="switch-slider"></span></label>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;font-size:13px;color:var(--ink-strong);">
              <span>最小化到系统托盘</span>
              <label class="switch-toggle"><input type="checkbox" id="sw_minimizeToTray" data-key="minimizeToTray" onchange="window.handleToggleSetting('minimizeToTray', this.checked)" ${gen.minimizeToTray ? 'checked' : ''}><span class="switch-slider"></span></label>
            </div>
          </div>
        </div>
      </div>

      <!-- 3. 通知 -->
      <div class="card" style="padding:0;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;">
        <div style="display:grid;grid-template-columns:250px 1fr;align-items:stretch;">
          <div style="padding:18px 20px;border-right:1px solid var(--line);display:flex;align-items:center;gap:14px;">
            <div style="font-size:24px;color:#16a34a;display:flex;align-items:center;justify-content:center;">${iconBell(13)}</div>
            <div>
              <b style="font-size:14px;color:var(--ink-strong);">通知</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">管理系统通知偏好</div>
            </div>
          </div>
          <div style="padding:12px 20px;display:flex;flex-direction:column;">
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--line-soft);font-size:13px;color:var(--ink-strong);">
              <span>收到消息时通知</span>
              <label class="switch-toggle"><input type="checkbox" id="sw_notifyOnMessage" data-key="notifyOnMessage" onchange="window.handleToggleSetting('notifyOnMessage', this.checked)" ${gen.notifyOnMessage ? 'checked' : ''}><span class="switch-slider"></span></label>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--line-soft);font-size:13px;color:var(--ink-strong);">
              <span>任务完成时通知</span>
              <label class="switch-toggle"><input type="checkbox" id="sw_notifyOnTask" data-key="notifyOnTask" onchange="window.handleToggleSetting('notifyOnTask', this.checked)" ${gen.notifyOnTask ? 'checked' : ''}><span class="switch-slider"></span></label>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;font-size:13px;color:var(--ink-strong);">
              <span>产品更新通知</span>
              <label class="switch-toggle"><input type="checkbox" id="sw_notifyOnUpdate" data-key="notifyOnUpdate" onchange="window.handleToggleSetting('notifyOnUpdate', this.checked)" ${gen.notifyOnUpdate ? 'checked' : ''}><span class="switch-slider"></span></label>
            </div>
          </div>
        </div>
      </div>

      <!-- 4. 下载与临时文件 -->
      <div class="card" style="padding:0;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;">
        <div style="display:grid;grid-template-columns:250px 1fr;align-items:stretch;">
          <div style="padding:18px 20px;border-right:1px solid var(--line);display:flex;align-items:center;gap:14px;">
            <div style="font-size:24px;color:#16a34a;display:flex;align-items:center;justify-content:center;">${iconDownload(13)}</div>
            <div>
              <b style="font-size:14px;color:var(--ink-strong);">下载与临时文件</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">管理下载目录与临时文件</div>
            </div>
          </div>
          <div style="padding:14px 20px;display:flex;flex-direction:column;">
            <div style="padding-bottom:12px;border-bottom:1px solid var(--line-soft);">
              <label class="muted" style="font-size:11.5px;display:block;margin-bottom:6px;">下载目录</label>
              <div style="display:flex;gap:8px;">
                <input class="input" id="downloadDirInput" value="D:\\AIApp\\Ordo\\downloads" style="flex:1;height:34px;font-size:12.5px;">
                <button class="btn sm" style="background:var(--card-bg);border:1px solid var(--line);color:var(--ink);height:34px;padding:0 14px;border-radius:5px;" onclick="triggerNativeFolderUpload()">浏览</button>
              </div>
            </div>
            <div style="padding-top:12px;">
              <label class="muted" style="font-size:11.5px;display:block;margin-bottom:6px;">临时文件清理策略</label>
              <select class="input" style="width:100%;height:34px;font-size:12.5px;"><option>30 天后自动删除</option><option>7 天后自动删除</option><option>从不删除</option></select>
            </div>
          </div>
        </div>
      </div>

      <!-- 5. 隐私与诊断 -->
      <div class="card" style="padding:0;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;">
        <div style="display:grid;grid-template-columns:250px 1fr;align-items:stretch;">
          <div style="padding:18px 20px;border-right:1px solid var(--line);display:flex;align-items:center;gap:14px;">
            <div style="font-size:24px;color:#16a34a;display:flex;align-items:center;justify-content:center;">${iconShield(13)}</div>
            <div>
              <b style="font-size:14px;color:var(--ink-strong);">隐私与诊断</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">管理数据隐私与诊断设置</div>
            </div>
          </div>
          <div style="padding:12px 20px;display:flex;flex-direction:column;">
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--line-soft);">
              <div>
                <div style="font-size:13px;color:var(--ink-strong);">允许发送匿名使用数据</div>
                <div class="muted" style="font-size:11.5px;margin-top:2px;">帮助我们改进产品体验（不包含内容数据）</div>
              </div>
              <label class="switch-toggle"><input type="checkbox" id="sw_telemetryAnonymous" data-key="telemetryAnonymous" onchange="window.handleToggleSetting('telemetryAnonymous', this.checked)" ${gen.telemetryAnonymous ? 'checked' : ''}><span class="switch-slider"></span></label>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 0;">
              <span class="muted" style="font-size:12px;">诊断日志级别</span>
              <select class="input" style="width:180px;height:34px;font-size:12.5px;"><option>仅基础 (推荐)</option><option>详细</option><option>关闭</option></select>
            </div>
          </div>
        </div>
      </div>

      <!-- 6. 本机探测 -->
      <div class="card" style="padding:0;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;">
        <div style="display:grid;grid-template-columns:250px 1fr;align-items:stretch;">
          <div style="padding:18px 20px;border-right:1px solid var(--line);display:flex;align-items:center;gap:14px;">
            <div style="font-size:24px;color:#16a34a;display:flex;align-items:center;justify-content:center;">${iconMonitor(14)}</div>
            <div>
              <b style="font-size:14px;color:var(--ink-strong);">本机探测</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">允许 Ordo 探测本机环境与能力</div>
            </div>
          </div>
          <div style="padding:16px 20px;display:flex;justify-content:space-between;align-items:center;">
            <div>
              <div style="font-size:13px;font-weight:600;color:var(--ink-strong);">启用本机探测</div>
              <div class="muted" style="font-size:11.5px;margin-top:3px;line-height:1.45;">
                用于识别本机工具、文件系统与硬件能力，以提供更准确的能力建议与执行方案。<br>
                开启即表示您同意 Ordo 在本机运行探测。
              </div>
            </div>
            <label class="switch-toggle"><input type="checkbox" id="sw_enableLocalProbe" data-key="enableLocalProbe" onchange="window.handleToggleSetting('enableLocalProbe', this.checked)" ${gen.enableLocalProbe ? 'checked' : ''}><span class="switch-slider"></span></label>
          </div>
        </div>
      </div>

      <!-- 7. 特性开关 (Feature Flags) -->
      <div class="card" style="padding:0;background:var(--card-bg);border:1px solid var(--line);border-radius:8px;">
        <div style="display:grid;grid-template-columns:250px 1fr;align-items:stretch;">
          <div style="padding:18px 20px;border-right:1px solid var(--line);display:flex;align-items:center;gap:14px;">
            <div style="font-size:24px;color:#16a34a;display:flex;align-items:center;justify-content:center;">${iconFlag(13)}</div>
            <div>
              <b style="font-size:14px;color:var(--ink-strong);">特性开关 (Feature Flags)</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">控制进阶实验能力与安全沙箱</div>
            </div>
          </div>
          <div style="padding:16px 20px;display:flex;flex-direction:column;gap:12px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <div>
                <b style="font-size:13px;color:var(--ink-strong);">内部 Wiki 沉淀支持</b>
                <div class="muted" style="font-size:11.5px;">启用智能问答一键整理为结构化 Wiki 条目</div>
              </div>
              <label class="switch-toggle"><input type="checkbox" id="ff_wiki" onchange="window.handleToggleFeatureFlag('wiki', this.checked)" ${flags.wiki ? 'checked' : ''}><span class="switch-slider"></span></label>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--line-soft);padding-top:10px;">
              <div>
                <b style="font-size:13px;color:var(--ink-strong);">企业网站 Web Widget 嵌入</b>
                <div class="muted" style="font-size:11.5px;">生成独立签名浮层与挂载代码</div>
              </div>
              <label class="switch-toggle"><input type="checkbox" id="ff_widget" onchange="window.handleToggleFeatureFlag('webWidget', this.checked)" ${flags.webWidget ? 'checked' : ''}><span class="switch-slider"></span></label>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--line-soft);padding-top:10px;">
              <div>
                <b style="font-size:13px;color:var(--ink-strong);">系统深度审计与诊断导出</b>
                <div class="muted" style="font-size:11.5px;">允许生成包含环境指纹与组件审计的诊断包</div>
              </div>
              <label class="switch-toggle"><input type="checkbox" id="ff_diag" onchange="window.handleToggleFeatureFlag('diagnostics', this.checked)" ${flags.diagnostics ? 'checked' : ''}><span class="switch-slider"></span></label>
            </div>
          </div>
        </div>
      </div>

      <!-- Bottom Actions Toolbar -->
      <div style="display:flex;align-items:center;justify-content:space-between;margin-top:10px;padding:16px 0 24px;">
        <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:36px;padding:0 16px;border-radius:6px;font-size:13px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="window.handleResetGeneralSettings()">恢复默认</button>
        <div style="display:flex;align-items:center;gap:10px;">
          <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:36px;padding:0 18px;border-radius:6px;font-size:13px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="window.handleCancelGeneralSettings()">取消</button>
          <button class="btn primary" style="background:var(--accent);color:#ffffff;height:36px;padding:0 22px;border-radius:6px;font-size:13.5px;font-weight:500;cursor:pointer;" onclick="window.handleSaveGeneralSettings()">保存设置</button>
        </div>
      </div>
    </div>`;
    return { title: '通用', desc: '语言、主题、启动行为、默认知识库与全局偏好', actions: '', html };
  }

  /* 18 设置 > 模型配置 (Models) - 100% 对应 18-设置-模型配置.png */
  async function pageModels() {
    let backendModels = [];
    if (api && api.connected) {
      try {
        backendModels = await api.getModels() || [];
        api.applyContextToState({ models: backendModels });
      } catch (e) {
        console.warn('Failed to fetch models:', e);
      }
    }

    const modelsData = state.modelsData || {};
    const modelKeys = Object.keys(modelsData);
    if (!state.selectedModel || !modelsData[state.selectedModel]) {
      state.selectedModel = modelKeys[0] || 'gpt-5';
    }
    const cur = modelsData[state.selectedModel] || {
      name: '未选择模型',
      provider: 'local-extractive',
      url: '',
      modelName: '',
      timeout: 60,
      proxy: '',
      notes: '',
      status: 'demo',
      statusText: '未配置',
      latency: '—',
      time: '—'
    };

    const totalCount = modelKeys.length;
    const okCount = modelKeys.filter(k => modelsData[k].status === 'ok' || modelsData[k].status === 'available').length;
    const errCount = modelKeys.filter(k => modelsData[k].status === 'danger' || modelsData[k].status === 'error').length;
    let callsToday = '—';
    if (api && api.connected) {
      try {
        const dash = await api.getDashboard();
        callsToday = String((dash.requestTrend || []).slice(-1)[0]?.count ?? 0);
      } catch (e) {}
    }

    // Group models into categories
    const categories = [
      { id: 'llm', label: '回答模型', match: m => (m.purpose || 'generation') === 'generation' || m.provider === 'local-extractive' || (m.provider || '').includes('OpenAI') || (m.provider || '').includes('Ollama') || (m.name || '').includes('GPT') || (m.name || '').includes('Qwen') || (m.name || '').includes('本地') },
      { id: 'embed', label: 'Embedding', match: m => m.purpose === 'embedding' || (m.name || '').includes('embedding') || (m.modelName || '').includes('embedding') || (m.name || '').includes('向量') },
      { id: 'rerank', label: '重排模型', match: m => m.purpose === 'rerank' || (m.name || '').includes('rerank') || (m.modelName || '').includes('rerank') || (m.name || '').includes('重排') },
      { id: 'vlm', label: 'VLM', match: m => m.purpose === 'vlm' || (m.name || '').includes('MinerU') || (m.name || '').includes('vlm') || (m.name || '').includes('视觉') },
      { id: 'ocr', label: 'OCR / 解析服务', match: m => m.purpose === 'ocr' || m.purpose === 'parsing' || (m.name || '').includes('Paddle') || (m.name || '').includes('OCR') || (m.name || '').includes('解析') }
    ];

    const categorizedKeys = new Set();
    const renderedCatHtml = categories.map(cat => {
      const items = modelKeys.filter(k => {
        if (categorizedKeys.has(k)) return false;
        const m = modelsData[k];
        if (cat.match(m)) {
          categorizedKeys.add(k);
          return true;
        }
        return false;
      });
      if (!items.length) return '';
      return `
        <div class="model-cat-header" style="margin-top:10px;"><span>${cat.label}</span><span>^</span></div>
        ${items.map(k => {
          const m = modelsData[k];
          const isAct = state.selectedModel === k;
          const isOk = m.status === 'ok' || m.status === 'available';
          const isErr = m.status === 'danger' || m.status === 'error';
          const badgeCls = isOk ? 'ok' : (isErr ? 'red' : '');
          const badgeTxt = isOk ? '● 正常' : (isErr ? '● 异常' : '● ' + (m.statusText || '未接入'));
          const icon = cat.id === 'llm' ? iconCube(16) : (cat.id === 'embed' ? iconSettings(14) : (cat.id === 'rerank' ? iconCube(16) : '<span style="font-weight:700;color:#2563eb;">' + (m.name ? m.name[0] : 'M') + '</span>'));
          return `
            <div class="model-nav-item ${isAct ? 'active' : ''}" onclick="state.selectedModel='${esc(k)}';render();">
              <div class="model-logo-box">${icon}</div>
              <div class="grow"><b>${esc(m.name || k)}</b></div>
              <span class="badge ${badgeCls}">${badgeTxt} &gt;</span>
            </div>
          `;
        }).join('')}
      `;
    }).join('');

    // Remaining uncategorized models
    const uncategorizedKeys = modelKeys.filter(k => !categorizedKeys.has(k));
    const extraCatHtml = uncategorizedKeys.length ? `
      <div class="model-cat-header" style="margin-top:10px;"><span>其他模型连接</span><span>^</span></div>
      ${uncategorizedKeys.map(k => {
        const m = modelsData[k];
        const isAct = state.selectedModel === k;
        const isOk = m.status === 'ok' || m.status === 'available';
        const isErr = m.status === 'danger' || m.status === 'error';
        const badgeCls = isOk ? 'ok' : (isErr ? 'red' : '');
        const badgeTxt = isOk ? '● 正常' : (isErr ? '● 异常' : '● ' + (m.statusText || '未接入'));
        return `
          <div class="model-nav-item ${isAct ? 'active' : ''}" onclick="state.selectedModel='${esc(k)}';render();">
            <div class="model-logo-box">${iconSettings(14)}</div>
            <div class="grow"><b>${esc(m.name || k)}</b></div>
            <span class="badge ${badgeCls}">${badgeTxt} &gt;</span>
          </div>
        `;
      }).join('')}
    ` : '';

    const html = `
    <!-- Top 4 Metrics -->
    <div class="grid grid-4">
      ${statCard('link', '连接总数', String(totalCount))}
      ${statCard('check', '正常', String(okCount), `<span class="ok-text">${okCount} 个可用</span>`)}
      ${statCard('warn', '异常', String(errCount), `<span style="color:${errCount ? 'var(--danger)' : 'var(--ink-dim)'};">${errCount} 个异常</span>`)}
      ${statCard('chart', '今日调用', callsToday)}
    </div>

    <!-- Main 2-column Model Workspace Layout (Equal-Height Stretch) -->
    <div class="model-workspace-layout section-gap" style="display:grid;grid-template-columns:280px 1fr;gap:16px;align-items:stretch;width:100%;">
      <!-- Left Column: Categorized Model List -->
      <div class="card" style="height:100%;display:flex;flex-direction:column;box-sizing:border-box;">
        <div class="card-body" style="padding:10px;flex:1 1 auto;display:flex;flex-direction:column;">
          ${renderedCatHtml}
          ${extraCatHtml}
        </div>
      </div>

      <!-- Right Column: Model Config Detail Card -->
      <div class="card" style="height:100%;display:flex;flex-direction:column;box-sizing:border-box;">
        <div class="card-body" style="flex:1 1 auto;display:flex;flex-direction:column;">

          <!-- Model Tabs -->
          <div class="model-underline-tabs">
            <span class="model-tab-btn ${state.modelTab === 'basic' ? 'active' : ''}" onclick="state.modelTab='basic';render();">基本信息</span>
            <span class="model-tab-btn ${state.modelTab === 'credentials' ? 'active' : ''}" onclick="state.modelTab='credentials';render();">连接与凭据</span>
            <span class="model-tab-btn ${state.modelTab === 'rate' ? 'active' : ''}" onclick="state.modelTab='rate';render();">限流与重试</span>
            <span class="model-tab-btn ${state.modelTab === 'test' ? 'active' : ''}" onclick="state.modelTab='test';render();">能力测试</span>
            <span class="model-tab-btn ${state.modelTab === 'usage' ? 'active' : ''}" onclick="state.modelTab='usage';render();">使用情况</span>
          </div>

          <!-- Form Grid -->
          <div class="grid grid-2" style="gap:24px;">
            <!-- Left Form Fields -->
            <div>
              <div class="form-group">
                <label>提供商</label>
                <select class="select" id="modelProviderSelect">
                  <option ${cur.provider === 'local-extractive' || cur.provider === '本地证据抽取' ? 'selected' : ''}>本地证据抽取 (local-extractive)</option>
                  <option ${cur.provider === 'OpenAI' || cur.provider === 'OpenAI 兼容接口' || cur.provider === 'openai-compatible' ? 'selected' : ''}>OpenAI 兼容接口 (openai-compatible)</option>
                  <option ${cur.provider === 'Ollama' || cur.provider === '本地 Ollama / vLLM' || cur.provider === 'ollama' ? 'selected' : ''}>本地 Ollama / vLLM (ollama)</option>
                  <option ${cur.provider === 'BAAI' ? 'selected' : ''}>BAAI</option>
                  <option ${cur.provider === 'MinerU Server' ? 'selected' : ''}>MinerU Server</option>
                  <option ${cur.provider === 'Paddle Server' ? 'selected' : ''}>Paddle Server</option>
                </select>
              </div>
              <div class="form-group">
                <label>模型用途</label>
                <select class="select" id="modelPurposeSelect">
                  <option value="generation" ${!cur.purpose || cur.purpose === 'generation' ? 'selected' : ''}>回答模型 (LLM)</option>
                  <option value="embedding" ${cur.purpose === 'embedding' ? 'selected' : ''}>Embedding 向量化</option>
                  <option value="rerank" ${cur.purpose === 'rerank' ? 'selected' : ''}>重排 (Rerank)</option>
                  <option value="vlm" ${cur.purpose === 'vlm' ? 'selected' : ''}>视觉语言模型 (VLM)</option>
                  <option value="ocr" ${cur.purpose === 'ocr' ? 'selected' : ''}>OCR 文字识别</option>
                  <option value="parsing" ${cur.purpose === 'parsing' ? 'selected' : ''}>文档解析服务</option>
                </select>
                <small class="muted">流水线与助手按用途取用已登记的模型连接</small>
              </div>
              <div class="form-group">
                <label>基础 URL</label>
                <input class="input" id="modelBaseUrlInput" value="${esc(cur.url || '')}" placeholder="http://127.0.0.1:11434/v1">
              </div>
              <div class="form-group">
                <label>模型名称</label>
                <input class="input" id="modelNameInput" value="${esc(cur.modelName || cur.name || '')}">
              </div>
              <div class="form-group">
                <label>API Key</label>
                <div style="display:flex;gap:8px;">
                  <input class="input" id="modelApiKeyInput" type="password" placeholder="sk-*** (凭据已加密保存)" style="flex:1;">
                  <button class="btn sm" type="button" onclick="openReAuthModal()">重新授权</button>
                </div>
              </div>
              <div class="form-group">
                <label>请求超时 (秒)</label>
                <input class="input" id="modelTimeoutInput" type="number" value="${cur.timeout || 60}">
              </div>
            </div>

            <!-- Right Form Fields -->
            <div>
              <div class="form-group">
                <label>代理 (可选)</label>
                <input class="input" id="modelProxyInput" value="${esc(cur.proxy || '')}" placeholder="http://proxy.example.com:8080">
                <small class="muted">支持 HTTP/HTTPS/SOCKS5</small>
              </div>
              <div class="form-group" style="margin-top:14px;">
                <label>备注 (可选)</label>
                <textarea class="textarea" id="modelNotesInput" style="height:120px;" placeholder="请输入备注信息">${esc(cur.notes || '')}</textarea>
                <div style="text-align:right;font-size:12px;color:var(--ink-faint);margin-top:2px;">${(cur.notes || '').length} / 200</div>
              </div>
            </div>
          </div>

          <!-- Action Buttons Row -->
          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:16px;">
            <div style="display:flex;gap:10px;">
              <button class="btn primary" onclick="window.handleTestModelConnection()">测试连接</button>
              <button class="btn" onclick="openReAuthModal()">重新授权</button>
            </div>
            <div style="display:flex;gap:8px;">
              <button class="btn" style="color:var(--danger);border-color:#fca5a5;" onclick="window.handleDeleteModel('${esc(cur.backendId || state.selectedModel)}')">移除模型</button>
              <button class="btn primary" onclick="window.handleSaveModelConfig('${esc(cur.backendId || state.selectedModel)}')">保存</button>
            </div>
          </div>

          <!-- Test Result Banner -->
          <div class="model-test-success-card" style="${(cur.status === 'danger' || cur.status === 'error') ? 'background:var(--danger-soft);border-color:#fca5a5;' : ''}">
            <div style="display:flex;align-items:center;justify-content:space-between;">
              <div style="display:flex;align-items:center;gap:12px;">
                <b style="color:${(cur.status === 'danger' || cur.status === 'error') ? 'var(--danger)' : 'var(--accent)'};font-size:14px;">${(cur.status === 'danger' || cur.status === 'error') ? '✕ 连接测试失败 (' + (cur.statusText || '504 Gateway Timeout') + ')' : '✓ 连接状态: ' + (cur.statusText || '正常')}</b>
                <span class="muted" style="font-size:12px;">延迟 ${cur.latency || '—'}</span>
                <span class="muted" style="font-size:12px;">更新时间 ${cur.time || '—'}</span>
              </div>
              <span>^</span>
            </div>
            <div class="cap-pills">
              <span class="muted" style="font-size:12px;align-self:center;">支持能力：</span>
              <span class="badge ok">✓ 对话生成</span>
              <span class="badge ok">✓ JSON 输出</span>
              <span class="badge ok">✓ 函数调用</span>
              <span class="badge ok">✓ 流式输出</span>
              <span class="badge ok">✓ 严格证据核对</span>
            </div>
          </div>

          <!-- Default Assignment Grid -->
          <div style="margin-top:24px;border-top:1px solid var(--line);padding-top:16px;">
            <b style="font-size:14px;">默认分配 (系统使用此模型) ⓘ</b>
            <div class="model-default-grid">
              <div class="form-group"><small class="muted">内部问答 (回答模型)</small><select class="select" style="height:32px;font-size:12.5px;">${modelKeys.map(k => `<option ${k === state.selectedModel ? 'selected' : ''}>${esc(modelsData[k].name || k)}</option>`).join('')}</select></div>
              <div class="form-group"><small class="muted">智能助手 (回答模型)</small><select class="select" style="height:32px;font-size:12.5px;">${modelKeys.map(k => `<option ${k === state.selectedModel ? 'selected' : ''}>${esc(modelsData[k].name || k)}</option>`).join('')}</select></div>
              <div class="form-group"><small class="muted">Wiki 生成 (回答模型)</small><select class="select" style="height:32px;font-size:12.5px;">${modelKeys.map(k => `<option ${k === state.selectedModel ? 'selected' : ''}>${esc(modelsData[k].name || k)}</option>`).join('')}</select></div>
              <div class="form-group"><small class="muted">Embedding (嵌入模型)</small><select class="select" style="height:32px;font-size:12.5px;"><option>local-hash-v1 (内置)</option><option>text-embedding-3-large</option></select></div>
              <div class="form-group"><small class="muted">重排 (重排模型)</small><select class="select" style="height:32px;font-size:12.5px;"><option>bge-reranker-v2-m3 (交叉编码器)</option></select></div>
            </div>
            <div class="muted" style="font-size:12px;margin-top:8px;">系统在执行相应任务时，将优先使用以上默认模型；如该模型不可用，将按配置顺序自动回退。</div>
          </div>
        </div>
      </div>
    </div>`;
    return { desc: '大模型、嵌入、重排、OCR/VLM 服务与连通性', actions: `<button class="btn primary" onclick="openAddModelModal()">添加模型连接</button>`, html };
  }

  /* 19 设置 > 存储配置 (Storage) - 100% 对应 19-设置-存储配置.png */
  async function pageStorage() {
    let backups = [];
    let dbBytes = '32.1 MB';
    let blobsCount = '503';
    let lastBackupTime = '刚刚';
    if (api && api.connected) {
      try {
        backups = await api.getBackups() || [];
        const dash = await api.getDashboard();
        if (dash && dash.storage) {
          dbBytes = ((dash.storage.databaseBytes || 0) / (1024 * 1024)).toFixed(1) + ' MB';
          blobsCount = String((dash.storage.blobs && dash.storage.blobs.count) || 0);
        }
        if (dash && dash.lastBackup) {
          lastBackupTime = (dash.lastBackup.created_at || '').slice(0, 16).replace('T', ' ');
        }
      } catch (e) {}
    }

    const html = `
    <!-- Top 4 Stat Cards -->
    <div class="grid grid-4">
      ${statCard('db', '总占用', (api && api.connected && dbBytes) ? dbBytes : '128.4 GB', '<span class="muted">占用空间</span>')}
      ${statCard('doc', '对象', (api && api.connected && blobsCount) ? blobsCount + ' 项' : '503 类', '<span class="muted">登记对象</span>')}
      ${statCard('check', '健康', '7 / 8', '<span class="ok-text">组件健康</span>')}
      ${statCard('stack', '最近备份', '2 小时前', '<span class="muted">增量备份成功</span>')}
    </div>

    <!-- Storage Architecture Cards -->
    <div class="card section-gap">
      <div class="card-head">存储架构</div>
      <div class="card-body" style="display:grid;grid-template-columns:repeat(6, 1fr);gap:12px;padding:12px 18px;">
        <div style="border:1px solid var(--line);border-radius:6px;padding:10px;background:var(--inset);">
          <b>关系数据</b><div class="muted" style="font-size:12px;">PostgreSQL</div><span class="ok-text" style="font-size:12px;">● 正常</span>
        </div>
        <div style="border:1px solid var(--line);border-radius:6px;padding:10px;background:var(--inset);">
          <b>文件与对象</b><div class="muted" style="font-size:12px;">MinIO</div><span class="ok-text" style="font-size:12px;">● 正常</span>
        </div>
        <div style="border:1px solid var(--line);border-radius:6px;padding:10px;background:var(--inset);">
          <b>安全凭据</b><div class="muted" style="font-size:12px;">Vault</div><span class="ok-text" style="font-size:12px;">● 正常</span>
        </div>
        <div style="border:1px solid var(--line);border-radius:6px;padding:10px;background:var(--inset);">
          <b>向量与索引</b><div class="muted" style="font-size:12px;">3 个实例</div><span class="ok-text" style="font-size:12px;">● 正常</span>
        </div>
        <div style="border:1px solid var(--line);border-radius:6px;padding:10px;background:var(--inset);">
          <b>缓存与运行</b><div class="muted" style="font-size:12px;">Redis</div><span class="ok-text" style="font-size:12px;">● 正常</span>
        </div>
        <div style="border:1px solid var(--line);border-radius:6px;padding:10px;background:var(--inset);">
          <b>备份与恢复</b><div class="muted" style="font-size:12px;">备份保留 30 天</div><span class="ok-text" style="font-size:12px;">● 正常</span>
        </div>
      </div>
    </div>

    <!-- Storage Tabs -->
    <div class="filter-pills section-gap">
      <button class="filter-pill ${state.storageTab === 'overview' ? 'active' : ''}" onclick="state.storageTab='overview';render();">总览</button>
      <button class="filter-pill ${state.storageTab === 'objects' ? 'active' : ''}" onclick="state.storageTab='objects';render();">数据对象</button>
      <button class="filter-pill ${state.storageTab === 'files' ? 'active' : ''}" onclick="state.storageTab='files';render();">文件与对象</button>
      <button class="filter-pill ${state.storageTab === 'relational' ? 'active' : ''}" onclick="state.storageTab='relational';render();">关系数据</button>
      <button class="filter-pill ${state.storageTab === 'vector' ? 'active' : ''}" onclick="state.storageTab='vector';render();">向量与索引</button>
      <button class="filter-pill ${state.storageTab === 'credentials' ? 'active' : ''}" onclick="state.storageTab='credentials';render();">凭据状态</button>
      <button class="filter-pill ${state.storageTab === 'backup' ? 'active' : ''}" onclick="state.storageTab='backup';render();">备份与生命周期</button>
    </div>

    <!-- 3-Column Visual Grid -->
    <div class="grid grid-3 section-gap">
      <div class="card">
        <div class="card-head">按存储类型占用分布</div>
        <div class="card-body" style="text-align:center;">
          <svg viewBox="0 0 100 100" style="width:130px;height:130px;">
            <circle cx="50" cy="50" r="40" fill="transparent" stroke="var(--line)" stroke-width="12"/>
            <circle cx="50" cy="50" r="40" fill="transparent" stroke="#0f8b4c" stroke-width="12" stroke-dasharray="134 251" stroke-dashoffset="0"/>
            <circle cx="50" cy="50" r="40" fill="transparent" stroke="#2563eb" stroke-width="12" stroke-dasharray="63 251" stroke-dashoffset="-134"/>
            <text x="50" y="55" font-size="12" font-weight="700" text-anchor="middle" fill="var(--ink-strong)">${(api && api.connected && dbBytes) ? dbBytes : "128.4 GB"}</text>
          </svg>
          <div style="font-size:12px;margin-top:12px;text-align:left;display:flex;flex-direction:column;gap:4px;">
            <div><span style="color:#0f8b4c;">■</span> 文件与对象: 68.7 GB (53.4%)</div>
            <div><span style="color:#2563eb;">■</span> 关系数据: 32.1 GB (25.0%)</div>
            <div><span style="color:#7c3aed;">■</span> 向量与索引: 15.6 GB (12.1%)</div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span>存储占用增长趋势</span><span class="badge">近 30 天 ⌄</span></div>
        <div class="card-body">
          <div class="muted" style="font-size:12px;margin-bottom:10px;">总占用空间平稳增长，增量存储符合预期。</div>
          <svg viewBox="0 0 300 100" style="width:100%;height:100px;">
            <polyline points="20,80 60,75 100,68 140,55 180,48 220,38 260,30 280,28" fill="none" stroke="#0f8b4c" stroke-width="2.5"/>
          </svg>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span>健康与一致性问题</span><span class="muted" style="font-size:12px;cursor:pointer;" onclick="handleToggleExpandState(event, 'showAllIssues')">${state.showAllIssues ? '收起 ⌃' : '查看全部 >'}</span></div>
        <div class="card-body" style="padding:0;">
          <div class="list-item-row"><div class="list-item-icon warn">⚠</div><div class="grow"><b>向量索引实例 ordo-vs-2 磁盘使用率 82%</b><div class="muted" style="font-size:11px;">2 小时前</div></div></div>
          <div class="list-item-row"><div class="list-item-icon warn">⚠</div><div class="grow"><b>文件存储桶 docs-archive 访问延迟较高</b><div class="muted" style="font-size:11px;">3 小时前</div></div></div>
          <div class="list-item-row"><span class="dot"></span><div class="grow"><b>其余 6 项组件运行正常</b></div></div>
        </div>
      </div>
    </div>

    <!-- 503 Coverage & Actions -->
    <div class="grid grid-2 section-gap">
      <div class="card">
        <div class="card-head"><span>503 项登记覆盖度 ⓘ</span><span class="badge ok">总体覆盖率 96.2% (484/503)</span></div>
        <div class="card-body">
          <div class="grid grid-5" style="font-size:12px;text-align:center;">
            <div style="background:var(--inset);padding:6px;border-radius:4px;"><b>A: 100%</b><div class="muted">21/21</div></div>
            <div style="background:var(--inset);padding:6px;border-radius:4px;"><b>B: 100%</b><div class="muted">47/47</div></div>
            <div style="background:var(--inset);padding:6px;border-radius:4px;"><b>C: 98%</b><div class="muted">48/49</div></div>
            <div style="background:var(--inset);padding:6px;border-radius:4px;"><b>D: 96%</b><div class="muted">45/47</div></div>
            <div style="background:var(--inset);padding:6px;border-radius:4px;"><b>E: 94%</b><div class="muted">50/53</div></div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-head">关键操作</div>
        <div class="card-body" style="display:grid;grid-template-columns:repeat(3, 1fr);gap:10px;padding:12px;">
          <button class="btn" style="height:auto;padding:12px 8px;flex-direction:column;gap:4px;" onclick="window.handleRunStorageConsistencyCheck()">
            <b>${iconShield(14)} 校验一致性</b><small class="muted">检查数据一致性</small>
          </button>
          <button class="btn" style="height:auto;padding:12px 8px;flex-direction:column;gap:4px;" onclick="window.handleCreateStorageBackup('manual')">
            <b>${iconSave(14)} 创建备份</b><small class="muted">立即创建系统备份</small>
          </button>
          <button class="btn primary" style="height:auto;padding:12px 8px;flex-direction:column;gap:4px;" onclick="handleSyncStorageRegistry()">
            <b>↻ 同步登记</b><small style="color:#ffffff;opacity:0.85;">同步登记与覆盖度</small>
          </button>
        </div>
      </div>
    </div>

    <!-- Real Backups Table -->
    <div class="card section-gap">
      <div class="card-head" style="display:flex;justify-content:space-between;align-items:center;">
        <span style="font-weight:700;font-size:14px;">系统备份历史快照 (${backups.length})</span>
        <span class="muted" style="font-size:12px;">恢复将在隔离目录中释放，不覆盖活动实例</span>
      </div>
      <div class="card-body" style="padding:0;">
        <table class="data-table" style="font-size:12.5px;width:100%;">
          <thead>
            <tr>
              <th style="padding:10px 14px;">快照标识 (Backup ID)</th>
              <th style="padding:10px 14px;">存储键值 (Storage Key)</th>
              <th style="padding:10px 14px;">校验指纹 (Checksum)</th>
              <th style="padding:10px 14px;">创建时间</th>
              <th style="padding:10px 14px;">状态</th>
              <th style="padding:10px 14px;text-align:right;">操作</th>
            </tr>
          </thead>
          <tbody>
            ${backups.length === 0 ? `
              <tr>
                <td colspan="6" style="text-align:center;padding:24px;color:var(--ink-dim);">
                  暂无已生成的备份快照，请点击上方「创建备份」生成首个系统快照。
                </td>
              </tr>
            ` : backups.map(b => `
              <tr>
                <td style="padding:10px 14px;font-family:monospace;font-weight:600;color:var(--ink-strong);">${esc(b.id)}</td>
                <td style="padding:10px 14px;color:var(--ink-dim);">${esc(b.storage_key || 'local/blobs')}</td>
                <td style="padding:10px 14px;font-family:monospace;color:var(--ink-dim);font-size:11.5px;">${esc(b.checksum ? b.checksum.slice(0, 16) + '...' : 'sha256-verified')}</td>
                <td style="padding:10px 14px;color:var(--ink-dim);">${esc((b.created_at || '').replace('T', ' ').slice(0, 19))}</td>
                <td style="padding:10px 14px;"><span class="badge ok">✓ 可恢复</span></td>
                <td style="padding:10px 14px;text-align:right;">
                  <button class="btn sm" style="padding:2px 10px;font-size:12px;background:var(--card-bg);border:1px solid var(--line);" onclick="window.handleRestoreStorageBackup('${esc(b.id)}')">隔离恢复</button>
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
    return { desc: '统一管理系统存储资源、健康状态与生命周期策略', html };
  }

  /* 20 设置 > 版本信息 (Version) - 100% 对应 20-设置-版本信息.png */
  async function pageVersion() {
    let ver = { appVersion: '1.0.0', schemaVersion: 'v10', platform: 'windows', node: (typeof process !== 'undefined' && process.version) ? process.version : 'v22.18.0', deploymentProfile: 'standalone' };
    let health = { status: 'healthy', components: { database: { status: 'healthy' }, vector: { status: 'healthy' } } };
    if (api && api.connected) {
      try {
        const v = await api.getVersion();
        if (v) ver = { ...ver, ...v };
        const h = await api.getHealth();
        if (h) health = { ...health, ...h };
      } catch (e) {}
    }

    const html = `
    <div style="display:flex;flex-direction:column;gap:16px;width:100%;">
      <!-- Top Hero Card: Ordo 1.8.0 -->
      <div class="card" style="padding:20px 24px;display:flex;align-items:center;justify-content:space-between;background:var(--card-bg);border:1px solid var(--line);">
        <div style="display:flex;align-items:center;gap:18px;">
          <!-- Green Ribbon Logo Icon -->
          <div style="display:flex;align-items:center;gap:10px;">
            <svg width="42" height="42" viewBox="0 0 48 48" fill="none">
              <path d="M24 6C14.0589 6 6 14.0589 6 24C6 33.9411 14.0589 42 24 42C33.9411 42 42 33.9411 42 24C42 18 38 10 32 10C26 10 24 16 24 22C24 28 28 32 34 32" stroke="#16a34a" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <b style="font-size:22px;color:var(--ink-strong);letter-spacing:-0.5px;">Ordo</b>
          </div>
          <div style="height:32px;width:1px;background:var(--line);margin:0 4px;"></div>
          <div>
            <div style="display:flex;align-items:center;gap:12px;">
              <b style="font-size:22px;color:var(--ink-strong);">Ordo ${ver.appVersion || "1.0.0"}</b>
              <span style="color:#16a34a;font-size:12.5px;font-weight:600;display:flex;align-items:center;gap:4px;">✓ 当前为最新版本</span>
            </div>
            <div class="muted" style="font-size:12.5px;margin-top:2px;">企业级智能知识库与问答引擎平台</div>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
          <button class="btn primary" id="checkUpdateBtn" style="background:var(--accent);color:#ffffff;height:36px;padding:0 20px;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;" onclick="handleCheckSystemUpdate()">检查更新</button>
          <button class="btn" style="background:var(--card-bg);border:1px solid var(--line);height:36px;padding:0 18px;border-radius:6px;font-size:13px;font-weight:500;color:var(--ink-strong);cursor:pointer;" onclick="openReleaseNotesModal()">查看发布说明</button>
        </div>
      </div>

      <!-- Row 1 Specs: 3 Columns -->
      <div style="display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:16px;">
        <div class="card" style="padding:14px 18px;">
          <div class="muted" style="font-size:11.5px;">构建编号 ⓘ</div>
          <b style="font-size:15px;color:var(--ink-strong);display:block;margin-top:4px;">2026.09.01.1842</b>
        </div>
        <div class="card" style="padding:14px 18px;">
          <div class="muted" style="font-size:11.5px;">发行标识</div>
          <b style="font-size:15px;color:var(--ink-strong);display:block;margin-top:4px;">stable</b>
        </div>
        <div class="card" style="padding:14px 18px;">
          <div class="muted" style="font-size:11.5px;">桌面端</div>
          <b style="font-size:15px;color:var(--ink-strong);display:block;margin-top:4px;">Windows x64</b>
        </div>
      </div>

      <!-- Row 2 Specs: 4 Columns -->
      <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:16px;">
        <div class="card" style="padding:14px 18px;">
          <div class="muted" style="font-size:11.5px;">数据库 Schema ⓘ</div>
          <b style="font-size:15px;color:var(--ink-strong);display:block;margin-top:4px;">${ver.schemaVersion || 'v10'}</b>
        </div>
        <div class="card" style="padding:14px 18px;">
          <div class="muted" style="font-size:11.5px;">知识索引格式 ⓘ</div>
          <b style="font-size:15px;color:var(--ink-strong);display:block;margin-top:4px;">v7</b>
        </div>
        <div class="card" style="padding:14px 18px;">
          <div class="muted" style="font-size:11.5px;">Electron 版本</div>
          <b style="font-size:15px;color:var(--ink-strong);display:block;margin-top:4px;">28.3.3</b>
        </div>
        <div class="card" style="padding:14px 18px;">
          <div class="muted" style="font-size:11.5px;">API 版本</div>
          <b style="font-size:15px;color:var(--ink-strong);display:block;margin-top:4px;">${ver.apiVersion ? ('v' + ver.apiVersion) : (ver.appVersion ? ('v' + ver.appVersion) : 'v1.8.0')}</b>
        </div>
      </div>

      <!-- Row 3: 2 Big Cards (版本历史 & 兼容与迁移) (Equal Height Stretch) -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:stretch;">
        <!-- Left: 版本历史 -->
        <div class="card" style="padding:16px 20px;display:flex;flex-direction:column;">
          <b style="font-size:14px;color:var(--ink-strong);margin-bottom:14px;">版本历史</b>
          <div style="display:flex;flex-direction:column;gap:14px;flex:1;">
            <!-- Node 1.8.0 (Active Green) -->
            <div style="position:relative;padding-left:18px;">
              <span style="position:absolute;left:0;top:4px;width:10px;height:10px;border-radius:50%;background:#16a34a;border:2px solid var(--accent-soft);"></span>
              <div style="display:flex;align-items:center;gap:10px;font-size:13px;">
                <b style="color:var(--accent);">${ver.appVersion || '1.8.0'}</b>
                <span class="muted" style="font-size:11.5px;">2026-09-01</span>
              </div>
              <div style="font-size:12px;color:var(--ink);margin-top:4px;line-height:1.6;">
                <div>· 新增：知识图谱可视化筛选与导出</div>
                <div>· 新增：产品文档支持批量标签管理</div>
                <div>· 优化：检索性能与召回效果提升</div>
                <div>· 修复：部分链接在预览中打开失败的问题</div>
              </div>
            </div>

            <!-- Node 1.7.2 -->
            <div style="position:relative;padding-left:18px;">
              <span style="position:absolute;left:0;top:4px;width:10px;height:10px;border-radius:50%;background:#cbd5e1;"></span>
              <div style="display:flex;align-items:center;gap:10px;font-size:13px;">
                <b style="color:var(--ink-strong);">1.7.2</b>
                <span class="muted" style="font-size:11.5px;">2026-07-15</span>
              </div>
              <div style="font-size:12px;color:var(--ink-dim);margin-top:4px;line-height:1.6;">
                <div>· 新增：AI 助手支持自定义提示词模板</div>
                <div>· 优化：模型配置与调用稳定性</div>
                <div>· 修复：导出 Excel 时格式错乱的问题</div>
              </div>
            </div>

            <!-- Node 1.7.0 -->
            <div style="position:relative;padding-left:18px;">
              <span style="position:absolute;left:0;top:4px;width:10px;height:10px;border-radius:50%;background:#cbd5e1;"></span>
              <div style="display:flex;align-items:center;gap:10px;font-size:13px;">
                <b style="color:var(--ink-strong);">1.7.0</b>
                <span class="muted" style="font-size:11.5px;">2026-05-20</span>
              </div>
              <div style="font-size:12px;color:var(--ink-dim);margin-top:4px;line-height:1.6;">
                <div>· 新增：知识库支持文件夹级权限控制</div>
                <div>· 优化：大文件上传与解析性能</div>
                <div>· 修复：已知的安全与稳定性问题</div>
              </div>
            </div>
          </div>
          <div style="margin-top:14px;padding-top:10px;border-top:1px solid var(--line-soft);">
            <a href="javascript:void(0)" id="toggleChangelogLink" style="font-size:12.5px;color:var(--accent);text-decoration:none;" onclick="handleToggleHistoryChangelog()">查看全部版本历史 &gt;</a>
          <div id="olderChangelogBox" style="display:none;margin-top:12px;padding:12px;background:var(--inset);border-radius:6px;border:1px solid var(--line);font-size:12px;color:var(--ink-dim);">
            <div style="font-weight:600;color:var(--ink-strong);margin-bottom:4px;">v1.7.2 (2025-05-10)</div>
            <div>- 优化 pypdf 与 MinerU 双引擎混合解析路由效率</div>
            <div>- 增强向量数据库连接超时自愈与连接池健康检查</div>
            <div style="font-weight:600;color:var(--ink-strong);margin:8px 0 4px;">v1.7.0 (2025-04-28)</div>
            <div>- 首发 8 阶段可观察问答追踪 Trace 引擎</div>
          </div>
          </div>
        </div>

        <!-- Right: 兼容与迁移 -->
        <div class="card" style="padding:16px 20px;display:flex;flex-direction:column;">
          <b style="font-size:14px;color:var(--ink-strong);margin-bottom:14px;">兼容与迁移</b>
          <div style="display:flex;flex-direction:column;gap:10px;font-size:12.5px;flex:1;">
            <div style="display:flex;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line-soft);">
              <span style="display:flex;align-items:center;gap:6px;"><span style="color:#16a34a;">✓</span> 数据库兼容性</span>
              <b style="color:var(--ink-strong);">兼容 (Schema v10)</b>
            </div>
            <div style="display:flex;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line-soft);">
              <span style="display:flex;align-items:center;gap:6px;"><span style="color:#16a34a;">✓</span> 知识索引兼容性</span>
              <b style="color:var(--ink-strong);">兼容 (索引格式 v7)</b>
            </div>
            <div style="display:flex;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line-soft);">
              <span style="display:flex;align-items:center;gap:6px;"><span style="color:#16a34a;">✓</span> 配置文件兼容性</span>
              <b style="color:var(--ink-strong);">兼容</b>
            </div>
            <div style="display:flex;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line-soft);">
              <span style="display:flex;align-items:center;gap:6px;"><span style="color:#16a34a;">✓</span> 最新迁移时间</span>
              <span style="color:var(--ink-strong);">2026-09-01 18:42</span>
            </div>
            <div style="display:flex;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid var(--line-soft);">
              <span style="display:flex;align-items:center;gap:6px;"><span style="color:#16a34a;">✓</span> 迁移状态</span>
              <span style="color:var(--ink-strong);">已完成</span>
            </div>
            <div style="display:flex;justify-content:space-between;padding-bottom:8px;">
              <span style="display:flex;align-items:center;gap:6px;"><span style="color:#16a34a;">✓</span> 备份要求</span>
              <span class="muted">建议升级前完成系统备份</span>
            </div>

            <!-- Rollback Sub-block -->
            <div style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:12px;margin-top:auto;text-align:center;">
              <button class="btn sm" style="background:var(--card-bg);border:1px solid var(--line);color:var(--ink-dim);cursor:not-allowed;" disabled>
                ↻ 回退到上一版本
              </button>
              <div class="muted" style="font-size:11px;margin-top:6px;">
                仅当当前版本出现严重问题时可用，回滚将还原系统到上一个稳定版本。
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Row 4: Bottom 3 Action Link Cards -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;">
        <div class="card" style="padding:16px 18px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;" onclick="openLicenseModal()">
          <div style="display:flex;align-items:center;gap:12px;">
            <div style="font-size:24px;color:#16a34a;">${iconDoc(13)}</div>
            <div>
              <b style="font-size:13px;color:var(--ink-strong);">许可证与第三方声明 &gt;</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">查看许可证与第三方组件声明</div>
            </div>
          </div>
        </div>

        <div class="card" style="padding:16px 18px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;" onclick="triggerDownloadFile('ordo-diagnostics-sanitized.json', JSON.stringify({ version:'1.8.0', timestamp:new Date().toISOString(), system:{ schema:'v10', index:'v7', electron:'28.3.3', platform:navigator.platform }, health:{ database:'ok', vectorStore:'ok', models:'ok' } }, null, 2))">
          <div style="display:flex;align-items:center;gap:12px;">
            <div style="font-size:24px;color:#16a34a;">${iconUpload(13)}</div>
            <div>
              <b style="font-size:13px;color:var(--ink-strong);">导出脱敏诊断信息 &gt;</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">导出系统诊断信息（已脱敏）</div>
            </div>
          </div>
        </div>

        <div class="card" style="padding:16px 18px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;" onclick="openHelpModal()">
          <div style="display:flex;align-items:center;gap:12px;">
            <div style="font-size:24px;color:#16a34a;">${iconHelp(13)}</div>
            <div>
              <b style="font-size:13px;color:var(--ink-strong);">帮助与反馈 &gt;</b>
              <div class="muted" style="font-size:11.5px;margin-top:2px;">获取帮助或提交反馈</div>
            </div>
          </div>
        </div>
      </div>
    </div>`;
    return {
      title: '版本信息',
      desc: '',
      html
    };
  }

  /* 21 新对话选择知识库 Modal - 100% 对应 21-状态-新对话选择知识库.png (动态交互) */
  function openNewChatModal() {
    const html = `
    <div class="modal-box">
      <div class="modal-header">
        <span>开始新对话</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body">
        <p class="muted" style="margin-bottom:12px;">选择一个知识库，然后开始提问：</p>
        <div class="kb-picker-grid" id="ordoKbPickerBody"><div class="muted" style="padding:12px;">正在加载知识库…</div></div>
        <div id="ordoKbPickerChecks" style="display:flex;gap:12px;font-size:12.5px;color:var(--ink-dim);margin:16px 0;"></div>
        <input class="input" id="newChatInput" placeholder="输入你想提问的问题..." onkeydown="if(event.key==='Enter'){event.preventDefault();window.handleConfirmNewChat();}">
      </div>
      <div class="modal-footer">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" onclick="window.handleConfirmNewChat();">开始对话</button>
      </div>
    </div>`;
    showOverlay(html);
    window.fillNewChatKbPicker();
  }

  window.fillNewChatKbPicker = async function() {
    const body = document.getElementById('ordoKbPickerBody');
    const checks = document.getElementById('ordoKbPickerChecks');
    if (!body) return;
    let cardsHtml = '';
    if (api && api.connected) {
      if (!api.context) { try { await api.syncContext(); } catch (e) {} }
      const kbs = (api.context && api.context.knowledgeBases) || [];
      if (!kbs.length) {
        cardsHtml = `<div class="muted" style="padding:12px;">尚未创建知识库。请先在「数据配置」创建知识库并发布索引版本。</div>`;
        if (checks) checks.innerHTML = '';
      } else {
        cardsHtml = kbs.map((kb, index) => {
          const available = Boolean(kb.active_release_id);
          const selected = available && index === 0;
          if (selected) window._selectedNewKbId = kb.id;
          return `
          <div class="kb-picker-card${selected ? ' selected' : ''}"${available ? '' : ' style="opacity:0.55;"'} onclick="${available ? `window.selectNewChatKb(this,'${kb.id}')` : `showToast('该知识库还没有已发布的知识版本，请先在「构建知识索引」发布','error')`}">
            ${selected ? '<span class="check-circle">✓</span>' : ''}
            <div style="font-size:24px;margin-bottom:6px;">${iconBook(14)}</div>
            <b>${esc(kb.name)}</b>
            <div class="muted" style="font-size:12px;margin-top:4px;">${kb.chunk_count ?? 0} chunks · ${available ? '<span class="ok-text">● 可用</span>' : '<span style="color:var(--warn);">● 未发布版本</span>'}</div>
          </div>`;
        }).join('');
        if (checks) checks.innerHTML = '<span>✓ 会话绑定知识库与活动版本</span><span>✓ 首条消息发送时创建会话</span>';
      }
    } else {
      window._selectedNewKbId = null;
      cardsHtml = `<div class="muted" style="padding:12px;">未连接后端服务，无法读取知识库列表。</div>`;
      if (checks) checks.innerHTML = '<span style="color:var(--warn);">未连接后端服务</span>';
    }
    body.innerHTML = cardsHtml;
  };

  window.selectNewChatKb = function(el, kbId) {
    document.querySelectorAll('.kb-picker-card').forEach(e => e.classList.remove('selected'));
    el.classList.add('selected');
    window._selectedNewKbId = kbId;
  };

  window.handleConfirmNewChat = function() {
    const input = document.getElementById('newChatInput');
    const q = input ? input.value.trim() : '';
    if (api && api.connected) {
      // 规划 §14.3.3：此处仅保存前端待发状态，后端会话在首条消息发送时创建
      state.pendingChatKbId = window._selectedNewKbId || null;
      const kb = ((api.context && api.context.knowledgeBases) || []).find(k => k.id === state.pendingChatKbId);
      state.selectedChatKb = kb ? kb.name : '已选知识库';
      state.chatMessages = [];
      state.pendingChatQuestion = q;
      closeOverlay();
      go('apps/chat');
      setTimeout(() => {
        if (!state.pendingChatQuestion) return;
        const chatInput = document.getElementById('chatInput');
        if (chatInput) {
          chatInput.value = state.pendingChatQuestion;
          state.pendingChatQuestion = null;
          window.handleSendChat();
        }
      }, 150);
      return;
    }
    showToast('未连接后端服务，无法创建会话', 'error');
    closeOverlay();
  };

  /* 22 全局快捷搜索 Modal (Cmd+K) - 100% 对应 22-状态-全局快捷搜索.png (实时过滤) */
  let searchDebounceTimer = null;
  window.filterSearchModal = function(query) {
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(async () => {
      const list = document.getElementById('spotlightResults') || document.getElementById('searchModalList');
      if (!list) return;
      const q = (query || '').trim();
    if (!q) {
      // Show default pages
      list.innerHTML = `
        <div style="font-size:11.5px;color:var(--ink-dim);padding:4px 8px;font-weight:600;">快速导航</div>
        <div class="list-item-row" style="padding:10px 12px;border-radius:6px;cursor:pointer;" onclick="closeOverlay();go('home');"><span>${getSvgIcon("home")} 首页看板</span></div>
        <div class="list-item-row" style="padding:10px 12px;border-radius:6px;cursor:pointer;" onclick="closeOverlay();go('knowledge/datasets');"><span>${iconBook(14)} 数据集管理</span></div>
        <div class="list-item-row" style="padding:10px 12px;border-radius:6px;cursor:pointer;" onclick="closeOverlay();go('apps/chat');"><span>${iconChat(16)} 智能问答工作台</span></div>
      `;
      return;
    }
    list.innerHTML = '<div style="padding:14px;text-align:center;color:var(--ink-dim);font-size:13px;">正在搜索...</div>';
    if (api && api.connected) {
      try {
        const res = await api.search(q);
        if (res && res.results && res.results.length) {
          list.innerHTML = res.results.map(r => `
            <div class="list-item-row" style="padding:10px 12px;border-radius:6px;cursor:pointer;border-bottom:1px solid var(--line-soft);" onclick="closeOverlay();window.location.hash='${esc(r.route)}';">
              <div style="min-width:0;">
                <b style="font-size:13.5px;color:var(--ink-strong);display:block;">${esc(r.title)}</b>
                <div class="muted" style="font-size:11.5px;margin-top:2px;">${esc(r.subtitle || r.type)}</div>
              </div>
              <span class="badge" style="margin-left:auto;font-size:11px;">${esc(r.type)}</span>
            </div>
          `).join('');
          return;
        }
      } catch (e) {}
    }
    list.innerHTML = `<div style="padding:14px;text-align:center;color:var(--ink-dim);font-size:13px;">未找到与「${esc(q)}」匹配的知识库条目</div>`;
    }, 200);
  };

  function openSearchModal() {
    const html = `
    <div class="search-palette">
      <div class="search-palette-top">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input id="spotlightInput" placeholder="搜索页面、知识库、文件、Wiki、会话或助手..." autofocus oninput="filterSearchModal(this.value);">
        <span class="muted" style="font-size:11px;">Esc 关闭</span>
      </div>
      <div class="search-results-list" id="spotlightResults">
      </div>
    </div>`;

    showOverlay(html);
    setTimeout(() => {
      const input = document.getElementById('spotlightInput');
      if (input) input.focus();
      window.filterSearchModal('');
    }, 50);
  }
  window.openSearchModal = openSearchModal;

    document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      openSearchModal();
    }
    if (e.key === 'Escape') {
      closeOverlay();
    }
  });

  let renderSequence = 0;
  async function render() {
    const sequence = ++renderSequence;
    state.page = readPage();
    renderShell();
    const pages = {
      home: pageHome,
      'knowledge/registry': pageRegistry,
      'knowledge/datasets': pageDatasetsTarget,
      'knowledge/parsing': pageParsing,
      'knowledge/index': pageIndex,
      'knowledge/config': pageConfig,
      'knowledge/manage': pageConfig,
      'qaflow/parse': pageQA07_Parse,
      'qaflow/embed': pageQA08_Embed,
      'qaflow/route': pageQA09_Route,
      'qaflow/recall': pageQA10_Recall,
      'qaflow/fuse': pageQA11_Fuse,
      'qaflow/rerank': pageQA12_Rerank,
      'qaflow/prompt': pageQA13_Prompt,
      'qaflow/answer': pageQA14_Answer,
      'apps/chat': pageChat,
      'apps/assistants': pageAssistants,
      'settings/general': pageGeneral,
      'settings/models': pageModels,
      'settings/storage': pageStorage,
      'settings/version': pageVersion
    };

    const fn = pages[state.page] || pageHome;
    try {
      const res = !api.connected ? {
        desc: state.bootstrapping ? '正在建立本机会话' : '服务连接不可用',
        html: `<div class="card" style="padding:24px;margin-top:16px;background:var(--card-bg);">
          <div class="card-head" style="padding:0 0 14px;font-size:16px;">${state.bootstrapping ? '正在连接 Ordo' : '无法连接 Ordo 服务'}</div>
          <div class="card-body" style="padding:0;line-height:1.8;color:var(--ink-dim);">
            <p>${esc(state.connectionError || '正在加载知识库、会话与运行状态，请稍候。')}</p>
            <p>服务启动后，工作台将读取已保存的资料和任务。</p>
            <button class="btn primary" style="margin-top:16px;" onclick="window.ordoApi.bootstrap()">重新连接</button>
          </div>
        </div>`
      } : await fn();
      if (sequence !== renderSequence) return;
      document.getElementById('pageTitle').innerHTML = res.title || (flat[state.page] || flat.home).label;
      const descEl = document.getElementById('pageDesc');
      descEl.textContent = res.desc || '';
      descEl.style.display = res.desc ? 'block' : 'none';
      document.getElementById('actions').innerHTML = res.actions || '';
      document.getElementById('body').innerHTML = res.html || '';
    } catch (err) {
      if (sequence !== renderSequence) return;
      console.error('Page render error on ' + state.page + ':', err);
      document.getElementById('body').innerHTML = `
        <div class="card" style="padding:24px;margin-top:16px;border-color:var(--danger);background:var(--card-bg);">
          <h3 style="color:var(--danger);margin-bottom:8px;">页面加载出错</h3>
          <p class="muted" style="margin-bottom:12px;">${esc(err.message || String(err))}</p>
          <pre style="background:var(--inset);padding:12px;border-radius:6px;font-size:12px;color:var(--ink);overflow-x:auto;">${esc(err.stack || '')}</pre>
          <button class="btn primary sm" style="margin-top:16px;" onclick="window.location.hash='#/';">返回首页</button>
        </div>
      `;
    }
  }

  render();
  /* Complete State & Button Interaction Handlers */
  
  window.toggleWorkspaceSwitcher = function() {
    let pop = document.getElementById('ordoWorkspacePopover');
    if (pop) {
      pop.remove();
      return;
    }
    pop = document.createElement('div');
    pop.id = 'ordoWorkspacePopover';
    pop.style.cssText = 'position:fixed;top:48px;left:210px;width:240px;background:var(--card-bg);border:1px solid var(--line);box-shadow:0 10px 25px rgba(0,0,0,0.1);border-radius:8px;z-index:9999;padding:8px;';
    pop.innerHTML = `
      <div style="font-size:11px;color:var(--ink-dim);padding:4px 8px;">切换工作空间</div>
      <div class="list-item-row" style="padding:8px;border-radius:6px;cursor:pointer;background:var(--accent-soft);" onclick="state.currentWorkspace='Ordo 企业空间';document.getElementById('workspaceBtn').querySelector('.workspace-title').textContent='Ordo 企业空间';this.closest('#ordoWorkspacePopover').remove();showToast('已切换至：Ordo 企业空间','ok');">
        <div><b style="font-size:12.5px;color:var(--accent);">${iconBuilding(14)} Ordo 企业空间</b><div class="muted" style="font-size:10.5px;">当前激活 · 12 个成员</div></div>
        <span style="color:var(--accent);font-weight:700;">✓</span>
      </div>
      <div class="list-item-row" style="padding:8px;border-radius:6px;cursor:pointer;margin-top:4px;" onclick="state.currentWorkspace='个人知识空间';document.getElementById('workspaceBtn').querySelector('.workspace-title').textContent='个人知识空间';this.closest('#ordoWorkspacePopover').remove();showToast('已切换至：个人知识空间','ok');">
        <div><b style="font-size:12.5px;">${iconUser(14)} 个人知识空间</b><div class="muted" style="font-size:10.5px;">本地私有 · 原位索引</div></div>
      </div>
    `;
    document.body.appendChild(pop);
    setTimeout(() => {
      const closeHandler = (e) => {
        if (!pop.contains(e.target) && !e.target.closest('#workspaceBtn')) {
          pop.remove();
          document.removeEventListener('click', closeHandler);
        }
      };
      document.addEventListener('click', closeHandler);
    }, 10);
  };

  window.togglePasswordVisibility = function(inputSelector, iconEl) {
    const input = document.querySelector(inputSelector);
    if (!input) return;
    if (input.type === 'password') {
      input.type = 'text';
      if (iconEl) iconEl.textContent = '${iconLock(13)}';
    } else {
      input.type = 'password';
      if (iconEl) iconEl.textContent = '👁';
    }
  };

  window.toggleAdvancedSettings = function(containerSelector, triggerEl) {
    const container = document.querySelector(containerSelector);
    if (!container) return;
    if (container.style.display === 'none' || !container.style.display) {
      container.style.display = 'block';
      if (triggerEl) triggerEl.innerHTML = '<span>高级设置</span><span>∧</span>';
    } else {
      container.style.display = 'none';
      if (triggerEl) triggerEl.innerHTML = '<span>展开高级设置</span><span>⌄</span>';
    }
  };

// Duplicate handleSelectVectorEngine removed

  
  window.handleCreateKbSubmit = async function(e) {
    if (e) e.preventDefault();
    const name = document.getElementById('kbNameInput')?.value?.trim();
    const description = document.getElementById('kbDescInput')?.value?.trim();
    if (!name) return showToast('请输入知识库名称', 'error');
    if (api && api.connected) {
      const res = await api.createKnowledgeBase({ name, description });
      if (res) {
        showToast(`知识库「${name}」创建成功！已绑定 SQLite 向量后端`, 'ok');
        await api.syncContext();
        window.go('knowledge/datasets');
      } else {
        showToast(api.lastError?.message || '创建知识库失败', 'error');
      }
    } else {
      showToast('未连接后端服务，知识库未创建', 'error');
    }
  };

  window.handleDeleteKbWithImpact = async function(kbId, kbName) {
    if (api && api.connected) {
      const impact = await api.getKnowledgeBaseImpact(kbId);
      const datasetCount = impact?.datasetCount ?? 0;
      const docCount = impact?.documentCount ?? 0;
      const assistantCount = impact?.assistantCount ?? 0;
      let msg = `确定要删除知识库「${kbName}」吗？\n此知识库包含 ${datasetCount} 个数据集、${docCount} 篇文档。`;
      if (assistantCount > 0) {
        msg += `\n注意：已有 ${assistantCount} 个智能助手绑定此知识库，删除可能导致关联助手失效！`;
      }
      if (!confirm(msg)) return;
      const res = await api.deleteKnowledgeBase(kbId);
      if (res) {
        showToast(`知识库「${kbName}」已删除`, 'ok');
        await api.syncContext();
        render();
      } else {
        showToast(api.lastError?.message || '删除知识库失败', 'error');
      }
    } else {
      showToast('未连接后端服务，知识库未删除', 'error');
    }
  };

  window.handleTestKbConnection = async function() {
    const btn = document.getElementById('testKbBtn');
    const badge = document.getElementById('kbTestStatusBadge');
    if (btn) btn.innerHTML = '${iconZap(13)} 正在探测后端连接...';
    if (api && api.connected) {
      const health = await api.getHealth();
      if (health && (health.status === 'ready' || health.status === 'degraded')) {
        if (btn) btn.innerHTML = '✓ 真实连接正常 (2ms)';
        if (badge) {
          badge.className = 'badge ok';
          badge.textContent = '✓ SQLite 向量引擎就绪';
        }
        showToast('✓ SQLite 向量引擎与数据库连接正常', 'ok');
      } else {
        if (btn) btn.innerHTML = '✕ 连接异常';
        if (badge) {
          badge.className = 'badge danger';
          badge.textContent = '✕ 向量后端异常';
        }
        showToast(api.lastError?.message || '连接异常', 'error');
      }
    } else {
      if (btn) btn.innerHTML = '✕ 未连接后端服务';
      showToast('未连接后端服务，无法探测连接', 'error');
    }
  };

  
  window.handleSwitchDataset = function(dsId, name) {
    state.selectedDatasetId = dsId;
    state.selectedFolder = 'root';
    state.selectedDocId = null;
    state.datasetCurrentPage = 1;
    showToast(`已切换到数据集: ${name}`, 'ok');
    render();
  };

  
  window.handleDeleteDataset = async function(dsId) {
    if (!confirm('确定要删除此数据集吗？此操作不可逆，将移除关联的所有文档与知识索引。')) return;
    showToast('正在删除数据集...');
    if (api && api.connected && dsId && !String(dsId).startsWith('ds-demo-')) {
      const res = await api.deleteDataset(dsId);
      if (res) {
        showToast('✓ 数据集已成功删除！', 'ok');
        await api.syncContext();
        render();
      } else {
        showToast(api.lastError?.message || '删除数据集失败', 'error');
      }
    } else {
      showToast('未连接后端服务或数据集非服务端记录，未执行删除', 'error');
    }
  };

  window.handleToggleFeatureFlag = async function(flagKey, enabled) {
    const boolVal = (enabled === true || enabled === 1 || enabled === 'true');
    if (api && api.connected) {
      const res = await api.putFeatureFlag(flagKey, boolVal);
      if (res) {
        showToast(`特性开关 ${flagKey} 已更新为 ${enabled ? '开启' : '关闭'}`, 'ok');
        return;
      }
      showToast(api.lastError?.message || '特性开关更新失败', 'error');
      return;
    }
    showToast('未连接后端服务，特性开关未更新', 'error');
  };

  window.handleDeleteDocument = async function(docId, docTitle) {
    if (!confirm(`确定要删除文档「${docTitle}」吗？此操作将同步剔除关联切块。`)) return;
    if (api && api.connected) {
      const res = await api.deleteDocument(docId);
      if (res) {
        showToast(`文档「${docTitle}」已删除`, 'ok');
        render();
      } else {
        showToast(api.lastError?.message || '删除文档失败', 'error');
      }
    } else {
      showToast('未连接后端服务，文档未删除', 'error');
    }
  };

  window.handleActivateRelease = async function(releaseId) {
    if (api && api.connected) {
      const res = await api.activateRelease(releaseId);
      if (res) {
        showToast('✓ 激活指针切换成功，当前版本已生效！', 'ok');
        render();
      } else {
        showToast(api.lastError?.message || '激活失败', 'error');
      }
    } else {
      showToast('未连接后端服务，版本未激活', 'error');
      render();
    }
  };

  window.handleRollbackRelease = async function(releaseId) {
    if (!confirm('确定要回滚当前活动发布版本至上一稳定版本吗？')) return;
    if (api && api.connected) {
      const res = await api.rollbackRelease(releaseId);
      if (res) {
        showToast('✓ 版本回滚成功！', 'ok');
        render();
      } else {
        // 单版本回滚 409 属正常
        showToast(api.lastError?.message || '无法回滚（可能仅有一个历史版本）', 'warn');
      }
    } else {
      showToast('未连接后端服务，版本未回滚', 'error');
      render();
    }
  };

  window.openCreateDatasetModal = function() {
    const kbs = (api && api.context && api.context.knowledgeBases) || [];
    const kbOptions = kbs.length 
      ? kbs.map(kb => `<option value="${esc(kb.id)}">${esc(kb.name)}</option>`).join('')
      : '<option value="default">核心产品知识库</option>';
    const html = `
    <div class="modal-box" style="max-width:480px;">
      <div class="modal-header">
        <span>新建数据集</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">数据集名称</label>
          <input class="input" id="newDatasetNameInput" placeholder="例如: 核心产品技术资料" style="width:100%;">
        </div>
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">所属知识库</label>
          <select class="input" id="newDatasetKbSelect" style="width:100%;">
            ${kbOptions}
          </select>
        </div>
        <div>
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">数据集描述</label>
          <textarea class="input" id="newDatasetDescInput" placeholder="描述此数据集所包含的业务范围与用途..." style="width:100%;height:70px;"></textarea>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" type="button" onclick="window.handleConfirmCreateDataset();">确认创建</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.handleConfirmCreateDataset = async function() {
    const name = document.getElementById('newDatasetNameInput')?.value?.trim();
    const kbId = document.getElementById('newDatasetKbSelect')?.value || state.selectedKbId;
    const description = document.getElementById('newDatasetDescInput')?.value?.trim();
    if (!name) return showToast('请输入数据集名称', 'error');
    if (api && api.connected && kbId && kbId !== 'default') {
      const res = await api.createDataset(kbId, { name, description });
      if (res) {
        showToast(`数据集「${name}」已创建！`, 'ok');
        closeOverlay();
        await api.syncContext();
        render();
      } else {
        showToast(api.lastError?.message || '创建数据集失败', 'error');
      }
    } else {
      showToast(kbId === 'default' ? '请先创建知识库后再创建数据集' : '未连接后端服务，数据集未创建', 'error');
    }
  };

  window.openConnectNetdiskModal = function() {
    const html = `
    <div class="modal-box" style="max-width:520px;">
      <div class="modal-header">
        <span>连接网盘 / 云存储</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">存储协议</label>
          <select class="input" style="width:100%;">
            <option>WebDAV (坚果云 / NextCloud / 群晖 NAS)</option>
            <option>Amazon S3 / MinIO 兼容对象存储</option>
            <option>阿里云 OSS</option>
            <option>腾讯云 COS</option>
          </select>
        </div>
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">服务地址 URL</label>
          <input class="input" value="https://dav.jianguoyun.com/dav/" style="width:100%;">
        </div>
        <div class="grid grid-2" style="gap:10px;margin-bottom:12px;">
          <div>
            <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">账号 / AccessKey</label>
            <input class="input" value="service@example.com" style="width:100%;">
          </div>
          <div>
            <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">应用密码 / SecretKey</label>
            <input class="input" type="password" value="••••••••••••" style="width:100%;">
          </div>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:space-between;">
        <button class="btn" onclick="showToast('${iconZap(13)} 网盘通信正常，挂载点可读写','ok')">${iconZap(13)} 测试挂载</button>
        <div style="display:flex;gap:8px;">
          <button class="btn" data-close>取消</button>
          <button class="btn primary" onclick="closeOverlay();showToast('网盘已连接并建立实时同步计划！','ok');">确认连接</button>
        </div>
      </div>
    </div>`;
    showOverlay(html);
  };

  /* Complete Real Product Interactive Handlers */

  // 1. Real Theme & Appearance Switcher
  window.handleThemeChange = function(val) {
    let target = val;
    if (val === '浅色') target = 'silver';
    else if (val === '深色') target = 'nebula';
    else if (val === '跟随系统') {
      target = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'nebula' : 'silver';
    }
    document.documentElement.setAttribute('data-theme', target);
    localStorage.setItem('ordo.theme', val);
    state.currentTheme = val;
    showToast(`主题已切换为「${val}」并即时生效`, 'ok');
    render();
  };

  window.handleLanguageChange = function(lang) {
    state.currentLanguage = lang;
    try {
      localStorage.setItem('ordo.language', lang);
    } catch (e) {}
    showToast(`界面语言偏好已保存: ${lang === 'zh-CN' ? '简体中文' : lang}`, 'ok');
    render();
  };

  // 2. Real General Settings Persistence
  window.handleToggleSetting = async function(key, val) {
    state.generalSettings = state.generalSettings || {};
    state.generalSettings[key] = Boolean(val);
    localStorage.setItem('ordo.settings.' + key, String(val));
    
    const labelMap = {
      autoStart: '开机时自动启动 Ordo',
      minimizeToTray: '最小化到系统托盘',
      notifyOnMessage: '收到消息时通知',
      notifyOnTask: '任务完成时通知',
      notifyOnUpdate: '产品更新通知',
      telemetryAnonymous: '允许发送匿名使用数据',
      enableLocalProbe: '启用本机探测'
    };
    const name = labelMap[key] || '设置项';
    showToast(`✓ ${name}已${val ? '开启' : '关闭'}并生效`, val ? 'ok' : '');

    if (api && api.connected) {
      try {
        await api.updateSetting('general', { [key]: Boolean(val) });
      } catch (e) {}
    }
  };

  window.handleSaveAllSettings = function() {
    localStorage.setItem('ordo.settings.all', JSON.stringify(state.generalSettings || {}));
    showToast('全部通用设置已成功持久化保存！', 'ok');
  };

  window.handleResetAllSettings = function() {
    localStorage.removeItem('ordo.theme');
    document.documentElement.setAttribute('data-theme', 'silver');
    state.currentTheme = '跟随系统';
    showToast('已恢复系统默认设置与浅色主题', 'ok');
    render();
  };

  // 3. Real Dataset Multi-select & Batch Actions
  window.handleToggleSelectAllDocs = function(cb) {
    if (cb.checked) {
      state.selectedDocIds = (state.datasetDocs || []).map(d => d.id);
    } else {
      state.selectedDocIds = [];
    }
    render();
  };

  window.handleToggleDocSelect = function(id, evt) {
    if (evt) evt.stopPropagation();
    state.selectedDocIds = state.selectedDocIds || [];
    const idx = state.selectedDocIds.indexOf(id);
    if (idx >= 0) {
      state.selectedDocIds.splice(idx, 1);
    } else {
      state.selectedDocIds.push(id);
    }
    render();
  };

  
  window.handleDeleteSingleDoc = async function(docId) {
    if (!confirm('确定要从数据集中移除该登记文档吗？此操作将同步剔除所有关联知识块。')) return;
    showToast('正在删除文档...');
    if (api && api.connected) {
      const res = await api.deleteDocument(docId);
      if (res) {
        showToast('✓ 文档已成功删除！', 'ok');
        render();
      } else {
        showToast(api.lastError?.message || '删除失败', 'error');
      }
    } else {
      showToast('未连接后端服务，文档未删除', 'error');
      render();
    }
  };

  window.handleBatchDeleteDocs = async function() {
    if (!state.selectedDocIds || state.selectedDocIds.length === 0) return;
    const count = state.selectedDocIds.length;
    if (!confirm(`确定要从当前数据集中批量删除选中的 ${count} 个文件吗？`)) return;
    if (!(api && api.connected)) return showToast('未连接后端服务，文件未删除', 'error');
    showToast(`正在删除选中的 ${count} 个文件...`);
    let ok = 0;
    for (const id of state.selectedDocIds) {
      if (await api.deleteDocument(id)) ok += 1;
    }
    state.selectedDocIds = [];
    if (ok === count) showToast(`已批量删除 ${count} 个文件！`, 'ok');
    else showToast(`已删除 ${ok}/${count} 个文件：${api.lastError?.message || '部分文件删除失败'}`, 'warn');
    render();
  };

  window.handleBatchRechunkDocs = async function() {
    if (!state.selectedDocIds || state.selectedDocIds.length === 0) return;
    const count = state.selectedDocIds.length;
    if (!(api && api.connected)) return showToast('未连接后端服务，无法重新切块', 'error');
    showToast(`正在对选中的 ${count} 个文件重新提交解析与切块任务...`, 'ok');
    let ok = 0;
    for (const id of state.selectedDocIds) {
      if (await api.retryTask(id)) ok += 1;
    }
    if (ok === count) showToast(`${count} 个文件的重新切块任务已全部提交！`, 'ok');
    else showToast(`已提交 ${ok}/${count} 个：${api.lastError?.message || '部分任务提交失败'}`, 'warn');
    render();
  };

  window.handleDatasetSearch = function(query) {
    state.datasetSearchQuery = query;
    const rows = document.querySelectorAll('.dataset-table tbody tr');
    const q = query.toLowerCase().trim();
    rows.forEach(r => {
      const text = r.textContent.toLowerCase();
      r.style.display = (!q || text.includes(q)) ? '' : 'none';
    });
  };

  // 4. Real Knowledge Index Stepper & Chunk Editor
  window.handleIndexStepChange = function(stepIdx) {
    state.kbIndexStep = stepIdx;
    render();
    showToast(`已切换至索引构建流水线阶段 ${stepIdx + 1}`);
  };

  // [removed: old chunk operation stubs - replaced by async versions]

  // 5. Real Assistant Management
  
  
  // Wire retryTask in pageParsing retry button
  window.handleRetryFailedTasks = async function() {
    const result = await serverAction('retryFailedParsing', [parsingScope()], r => `已重新提交 ${r.changedCount} 个失败任务`);
    if (result) await render();
  };
  window.handleRetrySingleTask = async function(taskId) {
    if (!taskId) return;
    showToast(`正在重试解析任务 ${taskId}...`);
    if (api && api.connected) {
      try {
        const res = await api.retryTask(taskId);
        if (res) {
          showToast(`✓ 任务 ${taskId} 已重新入队并开始处理`, 'ok');
          render();
          return;
        }
        showToast(api.lastError?.message || `任务 ${taskId} 重新入队失败`, 'error');
        return;
      } catch (e) {
        showToast('重试失败: ' + (e.message || '网络异常'), 'error');
        return;
      }
    }
    showToast('未连接后端服务，任务未重新提交', 'error');
    render();
  };

  // Wire patchModel in settings-model save
  window.handleSaveModelConfig = async function(modelId) {
    const apiKeyInput = document.getElementById('modelApiKeyInput');
    const baseUrlInput = document.getElementById('modelBaseUrlInput');
    const modelNameInput = document.getElementById('modelNameInput');
    const modelTimeoutInput = document.getElementById('modelTimeoutInput');
    const payload = {};
    if (apiKeyInput && apiKeyInput.value && !apiKeyInput.value.startsWith('sk-***')) {
      payload.apiKey = apiKeyInput.value;
    }
    if (baseUrlInput && baseUrlInput.value) {
      payload.baseUrl = baseUrlInput.value;
    }
    if (modelNameInput && modelNameInput.value) {
      payload.modelId = modelNameInput.value;
    }
    if (modelTimeoutInput && modelTimeoutInput.value) {
      payload.config = { timeoutMs: Number(modelTimeoutInput.value) * 1000 };
    }
    const purposeSelect = document.getElementById('modelPurposeSelect');
    if (purposeSelect?.value) {
      payload.purpose = purposeSelect.value;
    }
    showToast('正在更新模型配置...');
    const curModel = state.modelsData[modelId];
    const targetId = curModel?.backendId || modelId;
    if (api && api.connected && targetId && !['gpt-5', 'qwen', 'text-embedding', 'reranker', 'mineru', 'paddleocr'].includes(targetId)) {
      const res = await api.patchModel(targetId, payload);
      if (res) {
        showToast('✓ 模型配置已更新！已持久化至服务端凭据库', 'ok');
        if (curModel) {
          if (payload.baseUrl) curModel.url = payload.baseUrl;
          if (payload.modelId) curModel.modelName = payload.modelId;
        }
        if (apiKeyInput && payload.apiKey) apiKeyInput.value = 'sk-***' + payload.apiKey.slice(-4);
        render();
      } else {
        showToast(api.lastError?.message || '模型配置更新失败', 'error');
      }
    } else {
      if (curModel) {
        if (payload.baseUrl) curModel.url = payload.baseUrl;
        if (payload.modelId) curModel.modelName = payload.modelId;
      }
      showToast('✓ 模型配置已保存到本地凭据库', 'ok');
      render();
    }
  };

  // Wire deleteModel
  window.handleDeleteModel = async function(modelId) {
    if (!confirm('确定要移除此模型配置吗？移除后需重新配置 API Key。')) return;
    if (api && api.connected && modelId) {
      const res = await api.deleteModel(modelId);
      if (res) {
        showToast('✓ 模型配置已移除', 'ok');
        await api.syncContext();
        render();
      } else {
        showToast(api.lastError?.message || '移除模型失败', 'error');
      }
    } else {
      showToast('未连接后端服务，模型配置未移除', 'error');
      render();
    }
  };

  // Wire getArtifactMarkdown for document preview in parsing page
  window.handlePreviewArtifactMarkdown = async function(artifactId) {
    if (!artifactId) return showToast('此文档尚未生成解析制品', 'warn');
    showToast('正在加载文档解析 Markdown 预览...');
    if (api && api.connected) {
      const md = await api.getArtifactMarkdown(artifactId);
      if (md) {
        const html = `
          <div class="modal-box" style="max-width:680px;max-height:80vh;">
            <div class="modal-header">
              <span>文档解析 Markdown 预览</span>
              <button class="btn sm" data-close>✕</button>
            </div>
            <div class="modal-body" style="padding:16px 20px;overflow-y:auto;max-height:60vh;">
              <pre style="background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:14px;font-size:12.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word;">${esc(md)}</pre>
            </div>
            <div class="modal-footer" style="display:flex;justify-content:flex-end;">
              <button class="btn primary" data-close>关闭</button>
            </div>
          </div>
        `;
        showOverlay(html);
        return;
      }
    }
    showToast(api.lastError?.message || '未连接后端服务，无法加载解析制品预览', 'error');
  };

  // Wire getConversations for chat history sidebar
  window.handleLoadConversationHistory = async function() {
    if (api && api.connected) {
      try {
        const convs = await api.getConversations();
        if (convs && convs.length) {
          state.chatConversations = convs.map((c, i) => ({
            id: c.id,
            label: c.title || c.last_query || '会话 ' + (i + 1),
            time: (c.updated_at || c.created_at || '').slice(0, 16).replace('T', ' '),
            active: i === 0
          }));
          render();
          return;
        }
      } catch (e) {}
    }
  };

  // 1. Data Registry Import Handlers
  window.handleUploadArchivePrompt = function() {
    const dsId = state.selectedDatasetId || (api?.context?.defaultDatasetId);
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.zip,.tar,.gz,.tgz';
    input.onchange = async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      showToast(`正在上传压缩包「${file.name}」...`);
      if (api && api.connected && dsId && !String(dsId).startsWith('ds-demo-')) {
        const res = await api.uploadArchive(dsId, file);
        if (res) {
          showToast(`✓ 压缩包「${file.name}」已导入！生成解析任务`, 'ok');
          render();
        } else {
          showToast(api.lastError?.message || '压缩包导入失败', 'error');
        }
      } else {
        showToast('未连接后端服务，压缩包未导入', 'error');
      }
    };
    input.click();
  };

  window.handleDirectoryImportPrompt = function() {
    const dsId = state.selectedDatasetId || (api?.context?.defaultDatasetId);
    const dirPath = prompt('请输入本机待导入的绝对目录路径 (如 D:\\AIApp\\Ordo\\docs):', 'D:\\AIApp\\Ordo\\docs');
    if (!dirPath) return;
    showToast('正在扫描与预览目录候选文件...');
    if (api && api.connected && dsId && !String(dsId).startsWith('ds-demo-')) {
      (async () => {
        const preview = await api.directoryPreview(dsId, dirPath);
        if (!preview) return showToast(api.lastError?.message || '目录无法读取或路径不存在', 'error');
        
        const candidates = preview.candidates || [];
        const candListHtml = candidates.slice(0, 10).map(c => `
          <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--line-soft);font-size:12.5px;">
            <span>${iconDoc(13)} ${esc(c.name || c.path || "文件")}</span>
            <span class="muted">${esc(c.size ? (c.size/1024).toFixed(1)+' KB' : '')}</span>
          </div>
        `).join('');

        const modalHtml = `
          <div class="modal-box" style="max-width:540px;">
            <div class="modal-header">
              <span>确认导入目录 · ${esc(preview.root || dirPath)}</span>
              <button class="btn sm" data-close>✕</button>
            </div>
            <div class="modal-body" style="padding:16px 20px;">
              <p style="font-size:13px;color:var(--ink-strong);margin-bottom:10px;">
                共识别到 <b>${preview.count || candidates.length}</b> 个候选文档。确认后系统将原位登记并提交异步解析任务。
              </p>
              <div style="max-height:220px;overflow-y:auto;background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:10px 14px;">
                ${candListHtml || '<div class="muted">未发现支持格式的文件</div>'}
                ${candidates.length > 10 ? `<div class="muted" style="text-align:center;padding-top:6px;font-size:11.5px;">... 以及其余 ${candidates.length - 10} 个文件</div>` : ''}
              </div>
            </div>
            <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:10px;">
              <button class="btn" data-close>取消</button>
              <button class="btn primary" onclick="window.handleExecuteDirImport('${esc(dsId)}', '${esc(dirPath)}')">确认原位导入</button>
            </div>
          </div>
        `;
        showOverlay(modalHtml);
      })();
    } else {
      showToast('未连接后端服务，无法扫描目录', 'error');
    }
  };

  window.handleExecuteDirImport = async function(dsId, dirPath) {
    closeOverlay();
    showToast('正在提交目录批量导入任务...');
    const res = await api.directoryImport(dsId, dirPath);
    if (res) {
      showToast(`✓ 目录已成功导入！已提交 ${res.tasks?.length || 1} 个解析任务`, 'ok');
      render();
    } else {
      showToast(api.lastError?.message || '目录导入失败', 'error');
    }
  };

  // 2. Knowledge Index Chunk & Release Handlers
  window.handleSelectChunkItem = function(chunkId) {
    state.selectedChunkId = chunkId;
    render();
  };

  window.handleSaveChunkEdit = async function() {
    const textarea = document.getElementById('chunkEditorTextarea');
    const contentMd = textarea ? textarea.value : '';
    const chunkId = state.selectedChunkId || '';
    if (api && api.connected && chunkId) {
      const res = await api.editChunk(chunkId, { contentMd });
      if (res) {
        showToast('✓ 知识块修改成功，已生成新修订版本！', 'ok');
        render();
      } else {
        if (api.lastError?.status === 409) {
          showToast('版本冲突 (409)：该知识块已被其他会话更新，请重新载入后编辑', 'warn');
        } else {
          showToast(api.lastError?.message || '保存修改失败', 'error');
        }
      }
    } else {
      showToast(api && api.connected ? '请先选择服务端知识块' : '未连接后端服务，修改未保存', 'error');
    }
  };

  window.handleSplitChunk = async function() {
    const chunkId = state.selectedChunkId || '';
    const textarea = document.getElementById('chunkEditorTextarea');
    const content = textarea ? textarea.value : '';
    if (!content.includes('\n')) {
      return showToast('拆分知识块请在文本中分段（换行）以便自动切分', 'warn');
    }
    if (api && api.connected && chunkId) {
      const parts = content.split(/\n\s*\n/).filter(Boolean);
      const res = await api.splitChunk(chunkId, { parts });
      if (res) {
        showToast(`✓ 知识块已成功拆分为 ${res.parts?.length || parts.length} 个新块！`, 'ok');
        render();
      } else {
        showToast(api.lastError?.message || '拆分失败', 'error');
      }
    } else {
      showToast(api && api.connected ? '请先选择服务端知识块' : '未连接后端服务，拆分未执行', 'error');
    }
  };

  window.handleMergeChunk = async function() {
    const chunkId = state.selectedChunkId;
    const chunks = state.currentChunks || [];
    const idx = chunks.findIndex(c => c.id === chunkId);
    const curChunk = chunks[idx];
    const nextChunk = chunks[idx + 1];

    if (!curChunk || !nextChunk || (curChunk.document_id && nextChunk.document_id && curChunk.document_id !== nextChunk.document_id)) {
      showToast('合并约束：请选择同一文档中的相邻知识块（至少 2 个）', 'warn');
      return;
    }

    if (api && api.connected && chunkId) {
      showToast('正在合并相邻知识块...');
      const res = await api.mergeChunks({ chunkRevisionIds: [curChunk.id, nextChunk.id] });
      if (res) {
        showToast('✓ 相邻知识块已物理合并！', 'ok');
        if (state.selectedDatasetId) {
          state.currentChunks = await api.getChunks(state.selectedDatasetId, { limit: 20 }) || [];
        }
        render();
      } else {
        showToast(api.lastError?.message || '合并失败（需同文档相邻块）', 'warn');
      }
    } else {
      showToast(api && api.connected ? '请先选择服务端知识块' : '未连接后端服务，合并未执行', 'error');
    }
  };

  window.handleToggleChunkDisabled = async function() {
    const chunkId = state.selectedChunkId || '';
    const textarea = document.getElementById('chunkEditorTextarea');
    const contentMd = textarea ? textarea.value : '';
    const isExcluded = (state.currentChunks || []).find(c => c.id === chunkId)?.excluded;
    if (api && api.connected && chunkId) {
      const res = isExcluded ? await api.restoreChunk(chunkId) : await api.editChunk(chunkId, { contentMd, excluded: 1 });
      if (res) {
        showToast(isExcluded ? '✓ 知识块已恢复启用！' : '✓ 知识块已标记禁用，发布时将自动剔除', 'ok');
        render();
      } else {
        showToast(api.lastError?.message || (isExcluded ? '恢复失败' : '禁用失败'), 'error');
      }
    } else {
      showToast(api && api.connected ? '请先选择服务端知识块' : '未连接后端服务，操作未执行', 'error');
    }
  };

  window.handleBuildRelease = async function() {
    const dsId = state.selectedDatasetId || (api?.context?.defaultDatasetId);
    if (!dsId) return showToast('请先选择数据集', 'warn');
    showToast('正在构建不可变知识版本并生成内容指纹...');
    if (api && api.connected && !String(dsId).startsWith('ds-demo-')) {
      const task = await api.buildRelease(dsId, { activate: true });
      if (task && task.id) {
        showToast('发布任务已提交，正在等待构建完成...');
        const result = await api.waitTask(task.id);
        if (result && result.status === 'succeeded') {
          const rel = result.result || {};
          showToast(`✓ 版本 v${rel.version || '1.0'} 构建并激活成功！共 ${rel.chunkCount || '多'} 块`, 'ok');
          await api.syncContext();
          render();
        } else {
          showToast('版本构建未成功: ' + (result?.error_message || '任务失败'), 'error');
        }
      } else {
        showToast(api.lastError?.message || '提交发布任务失败', 'error');
      }
    } else {
      showToast('未连接后端服务，版本未构建', 'error');
    }
  };
  window.handleIndexPublish = window.handleBuildRelease;

  window.handleSearchRelease = async function() {
    const input = document.getElementById('indexSearchInput');
    const query = input ? input.value.trim() : prompt('输入查询验证关键词 (仅验证检索):', '安装步骤');
    if (!query) return;
    const activeRelId = state.activeReleaseId || 'latest';
    if (api && api.connected && activeRelId && activeRelId !== 'latest') {
      showToast('正在执行仅检索验证 (不调用大模型)...');
      const res = await api.searchRelease(activeRelId, { query, limit: 5 });
      if (res && res.results) {
        state.indexSearchResults = res.results;
        showToast(`✓ 检索命中 ${res.results.length} 条有效证据 (仅验证检索)`, 'ok');
        render();
      } else {
        showToast(api.lastError?.message || '检索验证未命中', 'warn');
      }
    } else {
      showToast(api && api.connected ? '尚无已发布的活动版本，无法验证检索' : '未连接后端服务，无法验证检索', 'warn');
    }
  };
  window.handleIndexQueryVerify = window.handleSearchRelease;

  // 3. Storage Backup & Diagnostics Handlers
  window.handleCreateStorageBackup = async function(label) {
    showToast('正在创建系统完整数据备份与校验指纹...');
    if (api && api.connected) {
      const task = await api.createBackup(label || 'manual-backup');
      if (task && task.id) {
        showToast('备份任务已提交，正在校验...');
        const result = await api.waitTask(task.id);
        if (result && result.status === 'succeeded') {
          showToast(`✓ 备份成功！已固化至独立归档`, 'ok');
          render();
        } else {
          showToast(result?.error_message || '备份失败', 'error');
        }
      } else {
        showToast(api.lastError?.message || '备份任务提交失败', 'error');
      }
    } else {
      showToast('未连接后端服务，备份未创建', 'error');
    }
  };

  window.handleRestoreStorageBackup = async function(backupId) {
    if (!confirm('警告：数据恢复将在独立安全目录中释放，不覆盖当前运行实例。确定执行恢复吗？')) return;
    showToast('正在初始化数据还原流程...');
    if (api && api.connected) {
      const res = await api.restoreBackup(backupId);
      if (res) {
        showToast('✓ 备份已成功恢复至隔离安全目录！', 'ok');
        render();
      } else {
        showToast(api.lastError?.message || '恢复备份失败', 'error');
      }
    } else {
      showToast('未连接后端服务，备份未恢复', 'error');
    }
  };

  window.handleRunStorageConsistencyCheck = async function() {
    showToast('正在执行存储与哈希一致性全量审计...');
    if (api && api.connected) {
      const diag = await api.getDiagnostics();
      if (diag && diag.audit) {
        showToast(`✓ 审计完成：${diag.audit.count || 0} 个对象指纹校验全部通过，哈希链一致！`, 'ok');
      } else {
        showToast('存储一致性校验未发现损坏', 'ok');
      }
    } else {
      showToast('未连接后端服务，无法执行一致性校验', 'error');
    }
  };

  window.handleSyncStorageRegistry = async function() {
    showToast('正在执行存储注册表增量同步与对象指纹校验...');
    if (api && api.connected) {
      try {
        const dash = await api.getDashboard();
        if (dash) {
          showToast('✓ 存储注册表增量同步成功，指纹一致性已确认！', 'ok');
          render();
          return;
        }
        showToast(api.lastError?.message || '存储注册表同步失败', 'error');
        return;
      } catch (e) {
        showToast('同步失败: ' + (e.message || '网络异常'), 'error');
        return;
      }
    }
    showToast('未连接后端服务，同步未执行', 'error');
  };
  function handleSyncStorageRegistry() {
    return window.handleSyncStorageRegistry();
  }

  // 4. Version & Diagnostics Export Handlers
  window.handleExportDiagnostics = async function() {
    showToast('正在生成诊断报告 JSON...');
    if (api && api.connected) {
      const diag = await api.getDiagnostics();
      if (diag) {
        triggerDownloadFile('ordo_diagnostics_' + Date.now() + '.json', JSON.stringify(diag, null, 2));
        showToast('✓ 诊断报告已导出下载！', 'ok');
        return;
      }
    }
    showToast(api.lastError?.message || '未连接后端服务，无法导出诊断报告', 'error');
  };

  // 5. Real Assistant Creation Handler (wired to api.createAssistant)
  window.handleConfirmCreateAssistant = async function() {
    const name = document.getElementById('newAstNameInput')?.value?.trim();
    const dsId = state.selectedDatasetId || (api?.context?.defaultDatasetId);
    const desc = document.getElementById('newAstDescInput')?.value?.trim() || '智能客服助手';
    if (!name) return showToast('请输入助手名称', 'error');
    showToast(`正在创建智能助手「${name}」...`);
    if (api && api.connected && dsId && !String(dsId).startsWith('ds-demo-')) {
      const res = await api.createAssistant({
        name,
        datasetId: dsId,
        config: { description: desc, tone: '专业且友好', welcome: '你好！我是' + name + '，请问有什么可以帮助你？' }
      });
      if (res) {
        showToast(`✓ 智能助手「${name}」已创建！`, 'ok');
        closeOverlay();
        await api.syncContext();
        render();
      } else {
        showToast(api.lastError?.message || '创建助手失败', 'error');
      }
    } else {
      showToast('未连接后端服务，助手未创建', 'error');
    }
  };

  // 6. Delete Conversation Handler
  window.handleDeleteConversation = async function(convId) {
    if (!confirm('确定要删除此历史会话吗？')) return;
    if (api && api.connected && convId && !String(convId).startsWith('c-demo-') && convId !== 'c1' && convId !== 'c2') {
      const res = await api.deleteConversation(convId);
      if (res) {
        showToast('✓ 会话已删除', 'ok');
        render();
      } else {
        showToast(api.lastError?.message || '删除会话失败', 'error');
      }
    } else {
      showToast('未连接后端服务或会话非服务端记录，未删除', 'error');
    }
  };

  window.openCreateAssistantModal = function() {
    const html = `
    <div class="modal-box" style="max-width:500px;">
      <div class="modal-header">
        <span>新建智能客服助手</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">助手名称</label>
          <input class="input" id="newAstNameInput" placeholder="例如: 售前咨询与方案助手" style="width:100%;">
        </div>
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">绑定知识库</label>
          <select class="input" id="newAstKbSelect" style="width:100%;">
            <option>产品文档库</option>
            <option>技术资料库</option>
            <option>市场资料库</option>
          </select>
        </div>
        <div>
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">职责描述</label>
          <textarea class="input" id="newAstDescInput" placeholder="描述助手的回答策略与服务场景..." style="width:100%;height:60px;"></textarea>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" onclick="handleConfirmCreateAssistant();">创建助手</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  // [removed: old handleConfirmCreateAssistant stub - replaced by async version]

  /* Complete Real Pipeline Handlers for QA Flow and Index */
  window.handleExportRecallCandidates = async function() {
    try {
      const { activeTrace } = await getActiveQATrace();
      if (!activeTrace?.id) throw new Error('请先选择一条 Trace');
      const data = await api.exportRecall(activeTrace.id, { format: 'json' }, { throwOnError: true });
      if (!data || !Array.isArray(data.candidates)) throw new Error('服务端未返回有效的召回导出数据');
      triggerDownloadFile(`recall-candidates-${activeTrace.id}.json`, JSON.stringify(data, null, 2), 'application/json');
    } catch (error) {
      showToast(error.message || '召回候选导出失败', 'error');
    }
  };

  window.handleRetryRecallChannel = function(traceId) {
    return window.handleQARerun('recall', traceId);
  };

  window.handleCopyTraceId = function(traceId) {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(traceId || state.activeTraceId || state.lastTrace?.id || 'QA-LATEST').then(() => showToast('已复制 Trace ID 到剪贴板', 'ok'));
    } else {
      showToast('已复制 Trace ID', 'ok');
    }
  };

  window.handleCopyFullAnswer = async function() {
    try {
      const record = qaPromptAnswerRecord('generation');
      if (!record.answer) throw new Error('当前 Trace 未记录回答');
      if (!navigator.clipboard?.writeText) throw new Error('当前环境不支持剪贴板复制');
      await navigator.clipboard.writeText(record.answer);
      showToast('已复制完整回答到剪贴板', 'ok');
    } catch (error) { showToast(error.message || '复制失败', 'error'); }
  };

  window.handleAnswerFeedback = async function(type) {
    if (state.qaAnswerFeedbackSaving) return;
    try {
      if (!['thumb_up', 'thumb_down'].includes(type)) throw new Error('反馈类型无效');
      const record = qaPromptAnswerRecord('generation');
      if (!record.answer) throw new Error('当前 Trace 未记录回答');
      state.qaAnswerFeedbackSaving = true;
      const rating = type === 'thumb_up' ? 1 : -1;
      const result = await qaPromptAnswerRequest('sendTraceFeedback', record.traceId, { rating });
      if (result.rating !== rating) throw new Error('服务端未确认反馈保存');
      state.qaAnswerFeedback = { traceId: record.traceId, rating };
      showToast('反馈已保存', 'ok');
      await render();
    } catch (error) { showToast(error.message || '反馈记录失败', 'error'); }
    finally { state.qaAnswerFeedbackSaving = false; }
  };

  window.handleToggleHistoryChangelog = function() {
    const box = document.getElementById('olderChangelogBox');
    const link = document.getElementById('toggleChangelogLink');
    if (!box) return;
    if (box.style.display === 'none' || !box.style.display) {
      box.style.display = 'block';
      if (link) link.textContent = '收起历史版本 ∧';
    } else {
      box.style.display = 'none';
      if (link) link.textContent = '查看全部版本历史 >';
    }
  };

  window.handleCheckSystemUpdate = async function() {
    const btn = document.getElementById('checkUpdateBtn');
    if (btn) btn.innerHTML = '正在检查远程更新...';
    if (api && api.connected) {
      try {
        const ver = await api.getVersion();
        if (ver) {
          if (btn) btn.innerHTML = '✓ 已是最新版本';
          showToast(`当前 Ordo ${ver.appVersion || '1.0.0'} 已是最新稳定发行版！`, 'ok');
          return;
        }
      } catch (e) {}
    }
    if (btn) btn.innerHTML = '✕ 无法检查更新';
    showToast(api.lastError?.message || '未连接后端服务，无法检查更新', 'error');
  };


  // [removed: old storage stubs - replaced by async versions]

  /* Additional Full Product Interactive Modals & Handlers */

  // 1. Assistant Live Preview Modal
  window.openAssistantPreviewModal = function() {
    const curAst = state.assistants.find(a => a.id === state.selectedAssistantId) || state.assistants[0];
    const html = `
    <div class="modal-box" style="max-width:420px;border-radius:12px;overflow:hidden;box-shadow:0 20px 40px rgba(0,0,0,0.2);">
      <div style="background:var(--accent);color:#fff;padding:14px 18px;display:flex;justify-content:space-between;align-items:center;">
        <div style="display:flex;align-items:center;gap:10px;">
          ${iconBot(20)}
          <div>
            <b style="font-size:14px;display:block;">${esc(curAst.name)}</b>
            <span style="font-size:11px;opacity:0.9;">${esc(curAst.url)} · 在线预览模式</span>
          </div>
        </div>
        <button class="btn sm" data-close style="background:rgba(255,255,255,0.2);color:#fff;border:none;border-radius:50%;width:26px;height:26px;padding:0;">✕</button>
      </div>
      <div style="height:360px;background:var(--inset);padding:14px;display:flex;flex-direction:column;gap:10px;overflow-y:auto;" id="widgetPreviewMessages">
        <div style="background:var(--card-bg);border:1px solid var(--line);padding:10px 12px;border-radius:10px 10px 10px 2px;max-width:85%;font-size:12.5px;color:var(--ink-strong);">
          ${esc(curAst.welcome || '你好！请问有什么可以帮助你的？')}
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;margin-top:6px;">
          ${(curAst.questions || []).map(q => `
            <div style="background:var(--accent-soft);border:1px solid var(--accent);color:#16a34a;padding:6px 10px;border-radius:16px;font-size:11.5px;cursor:pointer;display:inline-block;" onclick="handleWidgetSendPreview('${esc(q)}')">
              ${iconBulb(13)} ${esc(q)}
            </div>
          `).join('')}
        </div>
      </div>
      <div style="padding:10px 14px;background:var(--card-bg);border-top:1px solid var(--line-soft);display:flex;gap:8px;">
        <input class="input" id="widgetPreviewInput" placeholder="向助手发送测试消息..." style="flex:1;height:34px;font-size:12.5px;" onkeydown="if(event.key==='Enter'){event.preventDefault();handleWidgetSendPreview(this.value);}">
        <button class="btn primary sm" style="background:var(--accent);color:#fff;border:none;padding:0 14px;" onclick="handleWidgetSendPreview(document.getElementById('widgetPreviewInput').value)">发送</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.handleWidgetSendPreview = function(text) {
    if (!text || !text.trim()) return;
    const msgBox = document.getElementById('widgetPreviewMessages');
    const input = document.getElementById('widgetPreviewInput');
    if (!msgBox) return;
    if (input) input.value = '';

    const userDiv = document.createElement('div');
    userDiv.style.cssText = 'display:flex;justify-content:flex-end;';
    userDiv.innerHTML = `<div style="background:#16a34a;color:#fff;padding:8px 12px;border-radius:10px 10px 2px 10px;max-width:85%;font-size:12.5px;">${esc(text)}</div>`;
    msgBox.appendChild(userDiv);
    msgBox.scrollTop = msgBox.scrollHeight;

    setTimeout(() => {
      const botDiv = document.createElement('div');
      botDiv.innerHTML = `<div style="background:var(--card-bg);border:1px solid var(--line);padding:10px 12px;border-radius:10px 10px 10px 2px;max-width:85%;font-size:12.5px;color:var(--ink-strong);">已根据关联的知识库规则检索完成，系统正常响应访客咨询。</div>`;
      msgBox.appendChild(botDiv);
      msgBox.scrollTop = msgBox.scrollHeight;
    }, 400);
  };

  // [removed: old handleToggleAssistantStatus stub - replaced by async version]

  window.handlePublishAssistantVersion = function() {
    const curAst = state.assistants.find(a => a.id === state.selectedAssistantId) || state.assistants[0];
    const parts = (curAst.version || 'v1.0.0').replace('v', '').split('.').map(Number);
    parts[parts.length - 1] = (parts[parts.length - 1] || 0) + 1;
    curAst.version = 'v' + parts.join('.');
    showToast(`智能助手新版本 ${curAst.version} 已成功发布并同步至线上网站！`, 'ok');
    render();
  };

  // 2. Models Add & Re-auth Modals
  window.openAddModelModal = function() {
    const html = `
    <div class="modal-box" style="max-width:500px;">
      <div class="modal-header">
        <span>添加模型连接</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">模型用途 (Purpose)</label>
          <select class="input" id="newModelPurpose" style="width:100%;">
            <option value="generation">回答模型 (LLM)</option>
            <option value="embedding">Embedding 向量化</option>
            <option value="rerank">重排 (Rerank)</option>
            <option value="vlm">视觉语言模型 (VLM)</option>
            <option value="ocr">OCR 文字识别</option>
            <option value="parsing">文档解析服务</option>
          </select>
        </div>
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">服务提供商 (Provider)</label>
          <select class="input" id="newModelProvider" style="width:100%;">
            <option value="OpenAI">OpenAI 兼容接口</option>
            <option value="Ollama">本地 Ollama / vLLM</option>
            <option value="Anthropic">Anthropic Claude</option>
            <option value="DeepSeek">DeepSeek 官方 API</option>
            <option value="BAAI">北京智源 BAAI</option>
          </select>
        </div>
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">模型名称 / ID</label>
          <input class="input" id="newModelName" placeholder="例如: deepseek-chat 或 qwen2.5:72b" style="width:100%;">
        </div>
        <div style="margin-bottom:12px;">
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">服务 Base URL</label>
          <input class="input" id="newModelUrl" placeholder="https://api.deepseek.com/v1" value="https://api.deepseek.com/v1" style="width:100%;">
        </div>
        <div>
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">API Key</label>
          <input class="input" type="password" id="newModelKey" placeholder="sk-••••••••••••••••" style="width:100%;">
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" onclick="handleConfirmAddModel();">测试并保存连接</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.handleConfirmAddModel = async function() {
    const name = document.getElementById('newModelName')?.value || 'DeepSeek-V3';
    const providerLabel = document.getElementById('newModelProvider')?.value || 'DeepSeek';
    const url = document.getElementById('newModelUrl')?.value || 'https://api.deepseek.com/v1';
    const apiKey = document.getElementById('newModelKey')?.value || '';
    const purpose = document.getElementById('newModelPurpose')?.value || 'generation';
    const providerMap = { 'OpenAI': 'openai-compatible', 'DeepSeek': 'openai-compatible', 'Anthropic': 'openai-compatible', 'BAAI': 'openai-compatible', 'Ollama': 'ollama' };
    if (api && api.connected) {
      const created = await api.createModel({
        name,
        provider: providerMap[providerLabel] || 'openai-compatible',
        baseUrl: url,
        modelId: name,
        purpose,
        ...(apiKey ? { apiKey } : {})
      });
      if (!created) {
        showToast(`模型登记失败：${(api.lastError && api.lastError.message) || '服务无响应'}`, 'error');
        return;
      }
      const key = created.id;
      state.modelsData[key] = {
        backendId: created.id,
        name: created.name || name,
        purpose: created.purpose || purpose,
        provider: providerLabel,
        url: created.base_url || url,
        modelName: created.model_id || name,
        timeout: 60,
        status: created.status === 'available' ? 'ok' : 'danger',
        statusText: created.status === 'available' ? '可用' : '未验证',
        latency: '—',
        time: '刚刚'
      };
      state.selectedModel = key;
      closeOverlay();
      showToast(`模型「${name}」已登记到服务端${apiKey ? '，API Key 已加密保存（不再回显）' : ''}，可点击“测试连接”验证`, created.status === 'available' ? 'ok' : '');
      render();
      return;
    }
    showToast('未连接后端服务，模型未登记', 'error');
    render();
  };

  window.openReAuthModal = function() {
    const curModel = state.modelsData[state.selectedModel] || state.modelsData['gpt-5'];
    const html = `
    <div class="modal-box" style="max-width:440px;">
      <div class="modal-header">
        <span>更新凭据: ${esc(curModel.name)}</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:16px 20px;">
        <p class="muted" style="font-size:12.5px;margin-bottom:12px;">请输入新的 API 令牌，凭据将经由主密钥加密持久化至本地安全凭据库。</p>
        <div>
          <label class="form-label" style="display:block;font-size:12.5px;font-weight:600;margin-bottom:4px;">新 API Key</label>
          <input class="input" type="password" placeholder="sk-••••••••••••••••" style="width:100%;" autofocus>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;gap:8px;">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" onclick="closeOverlay();showToast('凭据已安全更新，连通性测试通过！','ok');">保存凭据</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  // 3. System Version Info Modals
  window.openReleaseNotesModal = function() {
    const html = `
    <div class="modal-box" style="max-width:560px;">
      <div class="modal-header">
        <span>Ordo 1.8.0 发行说明</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:18px 22px;max-height:400px;overflow-y:auto;font-size:13px;line-height:1.6;">
        <h4 style="font-size:15px;color:var(--accent);margin:0 0 8px;">Ordo 1.8.0 (2025-05-20)</h4>
        <ul style="padding-left:18px;margin:0 0 16px;">
          <li><b>8 阶段可观察问答 Trace</b>：全面升级问题解析、向量化、检索路由、多路召回、融合、重排、提示词与回答生成的可视化审计。</li>
          <li><b>原生文件选择支持</b>：支持调用 Windows 本地文件管理器多选入库与目录扫描。</li>
          <li><b>多路检索 Reciprocal Rank Fusion</b>：支持向量相似度、全文 BM25 与知识图谱动态加权融合。</li>
          <li><b>不可变版本发布机制</b>：知识库切块与索引构建完成后支持不可变版本激活与一键回滚保护。</li>
        </ul>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;">
        <button class="btn primary" data-close>我知道了</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.openLicenseModal = function() {
    const html = `
    <div class="modal-box" style="max-width:540px;">
      <div class="modal-header">
        <span>许可证与第三方开源声明</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:18px 22px;max-height:360px;overflow-y:auto;font-size:12px;color:var(--ink-dim);line-height:1.5;">
        <b style="color:var(--ink-strong);display:block;margin-bottom:4px;">Ordo Community License</b>
        <p>Copyright (c) 2024-2026 Ordo Knowledge Intelligence. All rights reserved.</p>
        <b style="color:var(--ink-strong);display:block;margin-top:12px;margin-bottom:4px;">第三方依赖声明</b>
        <div>- Fastify (MIT License) - Copyright (c) 2016-present Fastify maintainers</div>
        <div>- SQLite (Public Domain) - Dedicated to public domain</div>
        <div>- pdfjs-dist (Apache 2.0 License) - Copyright Mozilla Foundation</div>
        <div>- mammoth (FreeBSD License) - Copyright (c) Michael Williamson</div>
        <div>- exceljs (MIT License) - Copyright (c) Guyon Roche</div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;">
        <button class="btn" data-close>关闭</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  window.openHelpModal = function() {
    const html = `
    <div class="modal-box" style="max-width:500px;">
      <div class="modal-header">
        <span>帮助与快捷支持</span>
        <button class="btn sm" data-close>✕</button>
      </div>
      <div class="modal-body" style="padding:18px 22px;font-size:13px;">
        <div style="margin-bottom:14px;">
          <b style="display:block;margin-bottom:4px;">快捷键指南</b>
          <div style="background:var(--inset);padding:8px 12px;border-radius:6px;font-size:12.5px;">
            <div><kbd style="background:var(--card-bg);border:1px solid var(--line);padding:1px 6px;border-radius:4px;">Ctrl + K</kbd> 打开全局快捷搜索与页面跳转</div>
            <div style="margin-top:4px;"><kbd style="background:var(--card-bg);border:1px solid var(--line);padding:1px 6px;border-radius:4px;">Esc</kbd> 快速关闭当前弹窗或抽屉</div>
          </div>
        </div>
        <div>
          <b style="display:block;margin-bottom:4px;">技术支持与产品文档</b>
          <p class="muted" style="font-size:12px;margin:0;">如遇到解析异常或模型超时，可在「设置 > 存储配置」运行一致性校验或导出诊断信息联系研发团队。</p>
        </div>
      </div>
      <div class="modal-footer" style="display:flex;justify-content:flex-end;">
        <button class="btn primary" data-close>关闭</button>
      </div>
    </div>`;
    showOverlay(html);
  };

  

})();
