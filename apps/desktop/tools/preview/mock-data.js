// 演示数据：模拟一座已索引的个人资料库，供 UI 预览截图使用。
const now = Math.floor(Date.now() / 1000);
const day = 86400;

const FILES = [
  { id: 1, file_id: 1, name: '房屋租赁合同-回龙观.pdf', ext: '.pdf', size: 2318336, mtime: now - day * 0, source_name: '微信文件', volatile: true, state: 'ready', path: '/Users/demo/xwechat_files/msg/file/2026-08/房屋租赁合同-回龙观.pdf' },
  { id: 2, file_id: 2, name: '装修报价单-整包-0806.pdf', ext: '.pdf', size: 1153433, mtime: now - day * 1, source_name: '微信文件', volatile: true, state: 'ready', path: '/Users/demo/xwechat_files/msg/file/2026-08/装修报价单-整包-0806.pdf' },
  { id: 3, file_id: 3, name: '毕业论文终稿-v12.docx', ext: '.docx', size: 8912896, mtime: now - day * 2, source_name: '下载', volatile: false, state: 'ready', path: '/Users/demo/Downloads/毕业论文终稿-v12.docx' },
  { id: 4, file_id: 4, name: '重疾险条款-康惠保2.0.pdf', ext: '.pdf', size: 3672064, mtime: now - day * 3, source_name: '下载', volatile: false, state: 'ready', path: '/Users/demo/Downloads/重疾险条款-康惠保2.0.pdf' },
  { id: 5, file_id: 5, name: '汝窑天青釉研究笔记.md', ext: '.md', size: 48230, mtime: now - day * 3, source_name: '文稿', volatile: false, state: 'ready', path: '/Users/demo/Documents/notes/汝窑天青釉研究笔记.md' },
  { id: 6, file_id: 6, name: '体检报告-2026年度.pdf', ext: '.pdf', size: 5214208, mtime: now - day * 5, source_name: 'QQ 文件', volatile: true, state: 'missing', preserved_path: '/Users/demo/Library/Application Support/Ordo/preserved/体检报告-2026年度.pdf', path: '/Users/demo/Documents/Tencent Files/file/体检报告-2026年度.pdf' },
  { id: 7, file_id: 7, name: '操作系统期末真题-2024.pdf', ext: '.pdf', size: 1887436, mtime: now - day * 6, source_name: '下载', volatile: false, state: 'ready', path: '/Users/demo/Downloads/操作系统期末真题-2024.pdf' },
  { id: 8, file_id: 8, name: '项目周报-第32周.docx', ext: '.docx', size: 96256, mtime: now - day * 7, source_name: '微信文件', volatile: true, state: 'ready', path: '/Users/demo/xwechat_files/msg/file/2026-08/项目周报-第32周.docx' },
  { id: 9, file_id: 9, name: '发票-MacBook Pro.pdf', ext: '.pdf', size: 302080, mtime: now - day * 9, source_name: '微信文件', volatile: true, state: 'ready', path: '/Users/demo/xwechat_files/msg/file/2026-07/发票-MacBook Pro.pdf' },
  { id: 10, file_id: 10, name: '会议纪要-0810-产品评审.md', ext: '.md', size: 21504, mtime: now - day * 3, source_name: '文稿', volatile: false, state: 'ready', path: '/Users/demo/Documents/notes/会议纪要-0810-产品评审.md' },
  { id: 11, file_id: 11, name: '银行流水-2025下半年.pdf', ext: '.pdf', size: 4415488, mtime: now - day * 12, source_name: '下载', volatile: false, state: 'missing', path: '/Users/demo/Downloads/银行流水-2025下半年.pdf' },
  { id: 12, file_id: 12, name: '实验数据汇总-第三批.xlsx', ext: '.xlsx', size: 743424, mtime: now - day * 13, source_name: 'QQ 文件', volatile: true, state: 'ready', path: '/Users/demo/Documents/Tencent Files/file/实验数据汇总-第三批.xlsx' },
  { id: 13, file_id: 13, name: '深度学习课程讲义-Ch7.pdf', ext: '.pdf', size: 15938355, mtime: now - day * 15, source_name: '下载', volatile: false, state: 'ready', path: '/Users/demo/Downloads/深度学习课程讲义-Ch7.pdf' },
  { id: 14, file_id: 14, name: '读书笔记-乡土中国.txt', ext: '.txt', size: 35840, mtime: now - day * 18, source_name: '文稿', volatile: false, state: 'ready', path: '/Users/demo/Documents/notes/读书笔记-乡土中国.txt' },
  { id: 15, file_id: 15, name: '劳动合同-2024续签.pdf', ext: '.pdf', size: 1224704, mtime: now - day * 21, source_name: '下载', volatile: false, state: 'ready', path: '/Users/demo/Downloads/劳动合同-2024续签.pdf' },
  { id: 16, file_id: 16, name: '产品需求文档-知识库v2.docx', ext: '.docx', size: 542720, mtime: now - day * 22, source_name: '微信文件', volatile: true, state: 'ready', path: '/Users/demo/xwechat_files/msg/file/2026-07/产品需求文档-知识库v2.docx' },
  { id: 17, file_id: 17, name: '旅行攻略-滇西北环线.md', ext: '.md', size: 68608, mtime: now - day * 25, source_name: '文稿', volatile: false, state: 'ready', path: '/Users/demo/Documents/notes/旅行攻略-滇西北环线.md' },
  { id: 18, file_id: 18, name: '公积金提取指南-北京.pdf', ext: '.pdf', size: 891289, mtime: now - day * 30, source_name: '下载', volatile: false, state: 'ready', path: '/Users/demo/Downloads/公积金提取指南-北京.pdf' },
];

const STATS = {
  files: 2587,
  deduped: 34,
  recent7: 46,
  recent30: 189,
  by_source: [
    { name: '微信文件', c: 1210 },
    { name: '下载', c: 863 },
    { name: 'QQ 文件', c: 397 },
    { name: '文稿', c: 117 },
  ],
  by_ext: [
    { ext: '.pdf', c: 1450 },
    { ext: '.docx', c: 520 },
    { ext: '.md', c: 214 },
    { ext: '.txt', c: 186 },
    { ext: '.xlsx', c: 120 },
    { ext: '', c: 97 },
  ],
};

const CATEGORIES = {
  tree: [
    { id: 1, name: '合同与票据', depth: 0, total_count: 86 },
    { id: 2, name: '学业资料', depth: 0, total_count: 214 },
    { id: 3, name: '课程讲义', depth: 1, total_count: 78 },
    { id: 4, name: '生活事务', depth: 0, total_count: 65 },
  ],
  unclassified: 2222,
};

const BOOKS = { books: [
  { id: 1, name: '毕业论文', n: 24 },
  { id: 2, name: '装修专题', n: 12 },
  { id: 3, name: '保险理赔', n: 7 },
] };

const DETAIL = {
  file: FILES[1] ? { ...FILES[1], source: { name: '微信文件' } } : {},
  document: {
    title: '装修报价单-整包-0806',
    summary_text: '整包装修报价：套内 89㎡，半包 6.8 万，主材包 5.2 万；含水电改造、封阳台、全屋乳胶漆。付款分四期，质保两年。',
  },
  sections: [
    { section_path: '一、工程总览', page: 1, text: '本报价单适用于回龙观某小区 89㎡ 两居室整包装修。工期预计 75 个工作日，含基础施工、主材安装与竣工保洁。开工前需完成物业备案与消防申报。' },
    { section_path: '二、拆改与水电 › 2.3 水电改造', page: 2, text: '水电改造按实测米数计价：强电 58 元/米，弱电 45 元/米，水路 62 元/米。全屋预估改造量 260 米，预算 1.48 万元，超出部分按实结算并留存影像记录。' },
    { section_path: '三、门窗工程 › 3.1 封阳台', page: 4, text: '封阳台采用断桥铝 70 系窗框、5+27A+5 双层中空玻璃，单价 680 元/㎡。北向阳台 6.4㎡，含窗纱一体与排水孔加工，合计 4352 元。' },
    { section_path: '四、付款方式', page: 6, text: '合同签订后支付 30% 作为首期款；水电验收合格支付 30%；泥木完工支付 30%；竣工验收合格后 7 日内支付尾款 10%。任一节点验收不合格，业主有权暂停后续付款。' },
  ],
  truncated: false,
};

const SEARCH_RESULT = {
  total: 3,
  routes: { lexical: true, vector: true },
  hedge: null,
  files: [
    {
      name: '装修报价单-整包-0806.pdf', ext: '.pdf', path: FILES[1].path, source_name: '微信文件', volatile: true,
      snippets: [
        { page: 4, section_path: '三、门窗工程 › 3.1 封阳台', text: '封阳台采用断桥铝 70 系窗框、5+27A+5 双层中空玻璃，单价 680 元/㎡。北向阳台 6.4㎡，合计 4352 元。' },
        { page: 6, section_path: '四、付款方式', text: '竣工验收合格后 7 日内支付尾款 10%。封阳台与窗纱一体安装完成后随泥木阶段一并验收。' },
      ],
    },
    {
      name: '装修合同-金螳螂-0801.pdf', ext: '.pdf', path: '/Users/demo/xwechat_files/msg/file/2026-08/装修合同-金螳螂-0801.pdf', source_name: '微信文件', volatile: true,
      snippets: [
        { page: 3, section_path: '第二章 › 工程范围', text: '乙方负责封阳台、全屋地面找平及厨卫防水工程，防水层高度不低于 1.8 米，并提供闭水试验记录。' },
      ],
    },
    {
      name: '旅行攻略-滇西北环线.md', ext: '.md', path: FILES[16].path, source_name: '文稿', volatile: false,
      snippets: [
        { section_path: '住宿 › 香格里拉', text: '客栈顶层的封阳台改成了观景茶室，落地窗正对石卡雪山，是整条线路里性价比最高的一晚。' },
      ],
    },
  ],
};

const ANSWER = {
  status: 'answered',
  answer: '封阳台的合同单价为 680 元/㎡，采用断桥铝 70 系窗框和 5+27A+5 双层中空玻璃 [C1] [C4]。你家北向阳台面积 6.4㎡，含窗纱一体与排水孔加工后合计 4352 元 [C1]。施工范围上，封阳台属于乙方承包内容，随泥木阶段一并验收 [C2] [C5]，验收合格后才进入尾款支付节点 [C3]。',
  hedge: null,
  citations: [
    { tag: 'C1', file_id: 2, file_name: '装修报价单-整包-0806.pdf', file_path: FILES[1].path, page: 4, section_path: '三、门窗工程 › 3.1 封阳台', snippet: '封阳台采用断桥铝 70 系窗框、5+27A+5 双层中空玻璃，单价 680 元/㎡。', text: '封阳台采用断桥铝 70 系窗框、5+27A+5 双层中空玻璃，单价 680 元/㎡。北向阳台 6.4㎡，含窗纱一体与排水孔加工，合计 4352 元。' },
    { tag: 'C2', file_id: 2, file_name: '装修合同-金螳螂-0801.pdf', file_path: '/Users/demo/xwechat_files/msg/file/2026-08/装修合同-金螳螂-0801.pdf', page: 3, section_path: '第二章 › 工程范围', snippet: '乙方负责封阳台、全屋地面找平及厨卫防水工程。', text: '乙方负责封阳台、全屋地面找平及厨卫防水工程，防水层高度不低于 1.8 米，并提供闭水试验记录。' },
    { tag: 'C3', file_id: 2, file_name: '装修报价单-整包-0806.pdf', file_path: FILES[1].path, page: 6, section_path: '四、付款方式', snippet: '竣工验收合格后 7 日内支付尾款 10%。', text: '合同签订后支付 30% 作为首期款；水电验收合格支付 30%；泥木完工支付 30%；竣工验收合格后 7 日内支付尾款 10%。' },
    { tag: 'C4', file_id: 2, file_name: '装修报价单-整包-0806.pdf', file_path: FILES[1].path, page: 2, section_path: '二、主材说明', snippet: '门窗主材为断桥铝 70 系，玻璃配置 5+27A+5。', text: '门窗主材为断桥铝 70 系，玻璃配置 5+27A+5，气密性等级 7 级。' },
    { tag: 'C5', file_id: 2, file_name: '装修合同-金螳螂-0801.pdf', file_path: '/Users/demo/xwechat_files/msg/file/2026-08/装修合同-金螳螂-0801.pdf', page: 5, section_path: '第三章 › 验收', snippet: '泥木阶段验收含封阳台、瓷砖铺贴与吊顶基层。', text: '泥木阶段验收含封阳台、瓷砖铺贴与吊顶基层，业主应在 3 日内组织验收。' },
  ],
};

const SOURCES = { sources: [
  { id: 1, name: '微信文件', path: '/Users/demo/xwechat_files', enabled: true, watching: true, exists: true, volatile: true, auto_preserve: true, file_count: 1210 },
  { id: 2, name: '下载', path: '/Users/demo/Downloads', enabled: true, watching: true, exists: true, volatile: false, auto_preserve: false, file_count: 863 },
  { id: 3, name: 'QQ 文件', path: '/Users/demo/Documents/Tencent Files', enabled: true, watching: false, exists: true, volatile: true, auto_preserve: false, file_count: 397 },
  { id: 4, name: '文稿', path: '/Users/demo/Documents/notes', enabled: false, watching: false, exists: true, volatile: false, auto_preserve: false, file_count: 117 },
] };

const TREE_ROOTS = { roots: [
  { id: 1, name: '微信文件', path: '/Users/demo/xwechat_files', count: 1210 },
  { id: 2, name: '下载', path: '/Users/demo/Downloads', count: 863 },
  { id: 3, name: 'QQ 文件', path: '/Users/demo/Documents/Tencent Files', count: 397 },
] };

function treeLevel(dir) {
  const prefix = dir.replace(/\/+$/, '') + '/';
  const dirs = {};
  const files = [];
  FILES.forEach((f) => {
    if (!f.path.startsWith(prefix)) return;
    const rest = f.path.slice(prefix.length);
    if (rest.includes('/')) {
      const head = rest.split('/')[0];
      dirs[head] = (dirs[head] || 0) + 1;
    } else {
      files.push({ id: f.id, name: f.name, path: f.path, ext: f.ext, state: f.state });
    }
  });
  return {
    dir,
    dirs: Object.keys(dirs).sort().map((n) => ({ name: n, path: prefix + n, count: dirs[n] })),
    files,
    truncated: false,
  };
}

module.exports = {
  route(pathname, method, params) {
    if (pathname === '/stats') return STATS;
    if (pathname === '/categories') return CATEGORIES;
    if (pathname === '/books') return BOOKS;
    if (pathname === '/files/tree') {
      const dir = params.get('dir');
      return dir ? treeLevel(dir) : TREE_ROOTS;
    }
    if (pathname === '/classify/auto_ext') return { classified: 0 };
    if (pathname === '/index/embed_backfill') return { embedded: 0, remaining: 0, available: true };
    if (pathname === '/settings/ocr') return { enabled: true, available: true };
    if (pathname === '/reports/weekly') {
      return {
        week: '2026-W33', generated: false, path: '/Users/demo/reports/2026-W33.md',
        markdown: '# 知识库周报 · 2026-W33\n\n本周新收录 **46** 个文件 · 库内共 2587 个\n\n## 类型分布\n\n- .pdf：28 个\n- .docx：11 个',
      };
    }
    if (pathname === '/integrations/ccswitch') {
      return { available: true, providers: [
        { app_type: 'codex', name: 'anyrouter', endpoint: 'https://anyrouter.example/v1', model: 'gpt-5.5', api_key: 'sk-demo-1', is_current: true, openai_native: true },
        { app_type: 'claude', name: 'QWQ', endpoint: 'https://qwq.example/v1', model: 'claude-opus-5', api_key: 'sk-demo-2', is_current: false, openai_native: false },
      ] };
    }
    if (pathname === '/health') {
      return { ok: true, checks: { embedding: { ok: true, available: true, model: 'ollama-bge-m3-d1024' } } };
    }
    if (/^\/files\/\d+\/content$/.test(pathname)) {
      return {
        file_id: 2, total: DETAIL.sections.length,
        sections: DETAIL.sections.map((s, i) => ({ id: i + 1, ordinal: i, ...s })),
        offset: 0, has_more: false,
      };
    }
    if (pathname === '/settings/llm') return { configured: true, available: true, model: 'qwen3-max' };
    if (pathname === '/settings/llm/test') return { configured: true, available: true, model: 'qwen3-max', message: '模型连接正常' };
    if (pathname === '/watch/status') return { running: true, watched: ['a', 'b', 'c'], counters: { indexed: 132 }, activity: [] };
    if (pathname === '/watch/start') return { ok: true };
    if (pathname === '/index/status') return { pending: 0 };
    if (pathname === '/search') return SEARCH_RESULT;
    if (pathname === '/ask') return ANSWER;
    if (pathname === '/sources') return SOURCES;
    if (/^\/files\/\d+\/detail$/.test(pathname)) return DETAIL;
    if (pathname === '/files') {
      const offset = Number(params.get('offset') || 0);
      let list = FILES;
      if (params.get('group') === 'ext') {
        const sizes = {};
        FILES.forEach((f) => { const k = (f.ext || '').toLowerCase(); sizes[k] = (sizes[k] || 0) + 1; });
        list = [...FILES].sort((a, b) => {
          const ka = (a.ext || '').toLowerCase();
          const kb = (b.ext || '').toLowerCase();
          if (sizes[kb] !== sizes[ka]) return sizes[kb] - sizes[ka];
          if (ka !== kb) return ka < kb ? -1 : 1;
          return b.mtime - a.mtime;
        });
      }
      return { total: STATS.files, files: offset === 0 ? list : [] };
    }
    return {};
  },
};
