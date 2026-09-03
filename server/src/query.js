'use strict';

const { id, now, required, AppError, parseJson, stableJson } = require('./core');
const { formatLocator } = require('./models');

class QueryService {
  constructor({ db, knowledge, models, audit, config }) {
    this.db = db;
    this.knowledge = knowledge;
    this.models = models;
    this.audit = audit;
    this.config = config;
  }

  createConversation(rawInput, workspaceId = this.config.localWorkspaceId, requestId) {
    const input = { ...(rawInput || {}) };
    // 兼容 Web 工作台 api 客户端的 snake_case 形状
    if (!input.knowledgeBaseId && input.knowledge_base_id) input.knowledgeBaseId = input.knowledge_base_id;
    const kb = this.knowledge.ensureKnowledgeBase(required(input.knowledgeBaseId, 'knowledgeBaseId'), workspaceId);
    const dataset = input.datasetId
      ? this.knowledge.ensureDataset(input.datasetId, workspaceId)
      : this.knowledge.ensureDataset(kb.default_dataset_id, workspaceId);
    if (dataset.knowledge_base_id !== kb.id) throw new AppError(400, 'SCOPE_MISMATCH', '数据集不属于所选知识库');
    const releaseId = input.releaseId || dataset.active_release_id;
    if (!releaseId) throw new AppError(409, 'ACTIVE_RELEASE_REQUIRED', '知识库还没有活动知识版本');
    const release = this.knowledge.getRelease(releaseId, workspaceId);
    if (release.dataset_id !== dataset.id || !['active','superseded','retained','ready'].includes(release.status)) {
      throw new AppError(409, 'RELEASE_INVALID', '知识版本与会话范围不兼容');
    }
    if (input.modelConnectionId) this.models.get(input.modelConnectionId, workspaceId);
    const conversationId = id('conv');
    const timestamp = now();
    this.db.run(`INSERT INTO conversations(id,workspace_id,knowledge_base_id,dataset_id,release_id,title,status,model_connection_id,strict_evidence,created_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?)`, conversationId, workspaceId, kb.id, dataset.id, release.id,
      input.title || '新对话', 'active', input.modelConnectionId || null, input.strictEvidence === false ? 0 : 1, timestamp, timestamp);
    this.audit.append({ workspaceId, action: 'conversation.create', objectType: 'conversation', objectId: conversationId, requestId, details: { knowledgeBaseId: kb.id, datasetId: dataset.id, releaseId: release.id } });
    return this.getConversation(conversationId, workspaceId);
  }

  listConversations(workspaceId = this.config.localWorkspaceId, { limit = 100, offset = 0 } = {}) {
    const from = `FROM conversations c JOIN knowledge_bases kb ON kb.id=c.knowledge_base_id JOIN datasets d ON d.id=c.dataset_id
      JOIN knowledge_releases kr ON kr.id=c.release_id`;
    const total = this.db.one(`SELECT COUNT(*) AS count ${from} WHERE c.workspace_id=? AND c.deleted_at IS NULL`, workspaceId)?.count || 0;
    const items = this.db.all(`SELECT c.*,kb.name AS knowledge_base_name,d.name AS dataset_name,kr.version AS release_version,
      (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count
      ${from}
      WHERE c.workspace_id=? AND c.deleted_at IS NULL ORDER BY c.updated_at DESC LIMIT ? OFFSET ?`, workspaceId, limit, offset);
    return { items, total, limit, offset };
  }

  getConversation(conversationId, workspaceId = this.config.localWorkspaceId) {
    const conversation = this.db.one(`SELECT c.*,kb.name AS knowledge_base_name,d.name AS dataset_name,kr.version AS release_version
      FROM conversations c JOIN knowledge_bases kb ON kb.id=c.knowledge_base_id JOIN datasets d ON d.id=c.dataset_id
      JOIN knowledge_releases kr ON kr.id=c.release_id WHERE c.id=? AND c.workspace_id=? AND c.deleted_at IS NULL`, conversationId, workspaceId);
    if (!conversation) throw new AppError(404, 'NOT_FOUND', '会话不存在或不可访问');
    conversation.messages = this.db.all('SELECT * FROM messages WHERE conversation_id=? AND workspace_id=? ORDER BY created_at,id', conversationId, workspaceId)
      .map(message => {
        message.citations = this.db.all('SELECT id,title,locator_json,excerpt,ordinal,document_id,document_revision_id,chunk_revision_id,release_id FROM citations WHERE message_id=? AND workspace_id=? ORDER BY ordinal', message.id, workspaceId);
        return message;
      });
    return conversation;
  }

  deleteConversation(conversationId, workspaceId = this.config.localWorkspaceId, requestId) {
    this.getConversation(conversationId, workspaceId);
    this.db.run("UPDATE conversations SET status='deleted',deleted_at=?,updated_at=? WHERE id=? AND workspace_id=?", now(), now(), conversationId, workspaceId);
    this.audit.append({ workspaceId, action: 'conversation.delete', objectType: 'conversation', objectId: conversationId, requestId });
    return { deleted: true };
  }

  async ask(conversationId, input, workspaceId = this.config.localWorkspaceId, requestId, traceMetadata = {}) {
    return this.askStream(conversationId, input, workspaceId, requestId, null, traceMetadata);
  }

  async askStream(conversationId, input, workspaceId = this.config.localWorkspaceId, requestId, onEvent = null, traceMetadata = {}) {
    const conversation = this.getConversation(conversationId, workspaceId);
    if (conversation.status !== 'active') throw new AppError(409, 'INVALID_STATE', '当前会话不可继续问答');
    const metadataConfig = traceMetadata.configSnapshot || {};
    if (metadataConfig.modelConnectionId !== undefined && metadataConfig.modelConnectionId !== null) {
      this.models.get(metadataConfig.modelConnectionId, workspaceId);
      conversation.model_connection_id = metadataConfig.modelConnectionId;
    }
    if (metadataConfig.strictEvidence !== undefined) conversation.strict_evidence = metadataConfig.strictEvidence ? 1 : 0;
    const question = required(input.question ?? input.query, 'question');
    // Delay persistence until generation and citation validation succeed. This
    // prevents a failed request from leaving a half-finished user message.
    const userMessageId = id('msg');
    const traceId = id('trace');
    const started = performance.now();
    const stages = [];
    const stage = (name, startedAt, status, output) => {
      const durationMs = Math.max(0, Math.round(performance.now() - startedAt));
      stages.push({ name, status, durationMs, output });
      if (onEvent) onEvent('stage', { name, status, durationMs });
    };

    let stageStart = performance.now();
    const queryPlan = parseQuestion(question, conversation);
    stage('问题解析', stageStart, 'succeeded', queryPlan);

    stageStart = performance.now();
    const embeddingSummary = { provider: 'local-hash-v1', model: 'ordo-hash-embedding-v1', dimensions: 128, inputHash: require('./core').hash(question), degraded: false };
    stage('问题向量化', stageStart, 'succeeded', embeddingSummary);

    stageStart = performance.now();
    const route = routeQuery(question);
    stage('检索路由', stageStart, 'succeeded', route);

    stageStart = performance.now();
    const retrieval = this.knowledge.searchRelease(conversation.release_id, question, workspaceId, { limit: input.topK || 8 });
    const candidates = retrieval.results;
    const candidateSummary = item => ({
      chunkRevisionId: item.chunkRevisionId,
      documentId: item.documentId,
      documentRevisionId: item.documentRevisionId,
      title: item.title,
      content: item.content,
      locator: item.locator,
      rank: item.rank,
      score: item.fusionScore,
      vectorRank: item.vectorRank,
      vectorScore: item.vectorScore,
      fullTextRank: item.fullTextRank,
      fullTextScore: item.fullTextScore,
      rerankScore: item.rerankScore
    });
    const retrievalOutput = {
      routes: retrieval.routes,
      candidateCount: candidates.length,
      vector: candidates.filter(item => item.vectorRank != null).map(item => ({ ...candidateSummary(item), rank: item.vectorRank, score: item.vectorScore })),
      fullText: candidates.filter(item => item.fullTextRank != null).map(item => ({ ...candidateSummary(item), rank: item.fullTextRank, score: item.fullTextScore })),
      fusion: candidates.map(candidateSummary)
    };
    stage('多路召回', stageStart, 'succeeded', retrievalOutput);

    stageStart = performance.now();
    stage('结果融合', stageStart, 'succeeded', {
      method: retrieval.routes?.fusion?.method || 'rrf',
      k: retrieval.routes?.fusion?.k || 60,
      candidateCount: candidates.length,
      rawCandidateCount: retrievalOutput.vector.length + retrievalOutput.fullText.length,
      deduplicatedCount: candidates.length,
      permissionFilteredCount: candidates.length,
      vector: retrievalOutput.vector,
      fullText: retrievalOutput.fullText,
      candidates: retrievalOutput.fusion
    });

    stageStart = performance.now();
    const selected = candidates.filter(item => (item.rerankScore != null && item.rerankScore > 0) || (item.fullTextScore != null && item.fullTextScore > 0) || (item.vectorScore != null && item.vectorScore > 0.35)).slice(0, 6);
    const selectedIds = new Set(selected.map(item => item.chunkRevisionId));
    stage('重排', stageStart, 'succeeded', {
      provider: 'local-lexical-v1',
      threshold: 0.35,
      inputCount: candidates.length,
      selectedCount: selected.length,
      selected: selected.map(item => ({ ...candidateSummary(item), rank: item.rank, score: item.rerankScore })),
      rejected: candidates.filter(item => !selectedIds.has(item.chunkRevisionId)).map(item => ({ ...candidateSummary(item), reason: '未达到保留阈值' }))
    });

    const evidenceStatus = selected.length ? 'sufficient' : 'insufficient';
    stageStart = performance.now();
    const promptSummary = { templateVersion: 'strict-evidence-v1', strictEvidence: Boolean(conversation.strict_evidence), evidenceCount: selected.length, maxEvidenceChars: 12000,
      security: { evidenceTreatedAsUntrusted: true, hiddenReasoningStored: false, secretsIncluded: false } };
    stage('构建提示词', stageStart, 'succeeded', promptSummary);

    stageStart = performance.now();
    let generated;
    let degraded = false;
    let tokensStreamed = false;
    const history = (conversation.messages || [])
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .slice(-6)
      .map(m => ({ role: m.role, content: m.content }));
    const onModelToken = onEvent ? (delta) => {
      tokensStreamed = true;
      onEvent('token', { delta });
    } : null;

    const strictEvidence = Boolean(conversation.strict_evidence);
    if (strictEvidence && !selected.length) {
      generated = require('./models').localEvidenceAnswer(question, selected);
      degraded = true;
      generated.degradationReason = 'NO_SUPPORTING_EVIDENCE';
    } else {
      try {
        generated = await this.models.generate({
          connectionId: conversation.model_connection_id,
          workspaceId,
          question,
          evidence: selected,
          strictEvidence,
          history,
          onToken: onModelToken
        });
      } catch (error) {
        if (!selected.length && error.code !== 'FEATURE_DISABLED') throw error;
        generated = require('./models').localEvidenceAnswer(question, selected);
        degraded = true;
        generated.degradationReason = error.code || 'MODEL_UNAVAILABLE';
      }
    }
    const validOrdinals = generated.citationOrdinals.filter(ordinal => ordinal >= 1 && ordinal <= selected.length);
    if (validOrdinals.length !== generated.citationOrdinals.length) throw new AppError(502, 'CITATION_INVALID', '回答包含无效引用');
    if (strictEvidence && selected.length && !validOrdinals.length && !isEvidenceRefusal(generated.content)) {
      throw new AppError(502, 'EVIDENCE_CITATION_REQUIRED', '严格证据模式要求回答包含有效引用或明确拒答');
    }
    stage('回答生成', stageStart, degraded ? 'degraded' : 'succeeded', {
      provider: generated.provider, modelId: generated.modelId, evidenceStatus, citationCount: validOrdinals.length,
      degraded, degradationReason: generated.degradationReason || null, usage: generated.usage
    });

    const assistantMessageId = id('msg');
    const finished = now();
    this.db.transaction(() => {
      this.db.run("INSERT INTO messages(id,workspace_id,conversation_id,role,content,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)",
        userMessageId, workspaceId, conversationId, 'user', question, '{}', finished);
      this.db.run("INSERT INTO messages(id,workspace_id,conversation_id,role,content,evidence_status,trace_id,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        assistantMessageId, workspaceId, conversationId, 'assistant', generated.content, evidenceStatus, traceId,
        JSON.stringify({ provider: generated.provider, modelId: generated.modelId, degraded }), finished);
      const configSnapshot = traceMetadata.configSnapshot || {
        modelConnectionId: conversation.model_connection_id || null,
        strictEvidence: Boolean(conversation.strict_evidence),
        topK: input.topK || 8
      };
      const inputSnapshot = traceMetadata.inputSnapshot || { question, topK: input.topK || 8, ...(traceMetadata.idempotencyKey ? { idempotencyKey: traceMetadata.idempotencyKey } : {}) };
      const permissionSnapshot = traceMetadata.permissionSnapshot || { workspaceId, conversationId, datasetId: conversation.dataset_id, releaseId: conversation.release_id };
      this.db.run(`INSERT INTO query_traces(id,workspace_id,conversation_id,message_id,release_id,query,status,evidence_status,stages_json,metrics_json,created_at,parent,root,trace_type,replay_from_stage,config_snapshot,input_snapshot,permission_snapshot,retention)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, traceId, workspaceId, conversationId, assistantMessageId, conversation.release_id, question,
        degraded ? 'degraded' : 'succeeded', evidenceStatus, JSON.stringify(stages), JSON.stringify({ totalMs: Math.round(performance.now() - started), candidateCount: candidates.length, selectedEvidence: selected.length }), finished,
        traceMetadata.parentTraceId || null, traceMetadata.rootTraceId || traceId, traceMetadata.traceType || 'original', traceMetadata.replayFromStage || null,
        JSON.stringify(configSnapshot), JSON.stringify(inputSnapshot), JSON.stringify(permissionSnapshot), traceMetadata.retention || 'standard');
      validOrdinals.forEach((ordinal, index) => {
        const item = selected[ordinal - 1];
        this.db.run(`INSERT INTO citations(id,workspace_id,trace_id,message_id,release_id,document_id,document_revision_id,chunk_revision_id,title,locator_json,excerpt,ordinal,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`, id('cite'), workspaceId, traceId, assistantMessageId, conversation.release_id,
          item.documentId, item.documentRevisionId, item.chunkRevisionId, item.title, JSON.stringify(item.locator || {}), item.content.slice(0, 500), index + 1, finished);
      });
      if (conversation.title === '新对话') this.db.run('UPDATE conversations SET title=?,updated_at=? WHERE id=? AND workspace_id=?', question.slice(0, 60), finished, conversationId, workspaceId);
      else this.db.run('UPDATE conversations SET updated_at=? WHERE id=? AND workspace_id=?', finished, conversationId, workspaceId);
    });
    if (onEvent && !tokensStreamed) {
      const text = String(generated.content || '');
      const step = 4;
      for (let i = 0; i < text.length; i += step) {
        onEvent('token', { delta: text.slice(i, i + step) });
        await new Promise(r => setTimeout(r, 10));
      }
    }
    const finalResult = {
      userMessage: this.db.one('SELECT * FROM messages WHERE id=?', userMessageId),
      assistantMessage: { ...this.db.one('SELECT * FROM messages WHERE id=?', assistantMessageId), citations: this.db.all('SELECT * FROM citations WHERE message_id=? ORDER BY ordinal', assistantMessageId) },
      trace: this.getTrace(traceId, workspaceId)
    };
    if (onEvent) onEvent('done', finalResult);
    return finalResult;
  }

  getTrace(traceId, workspaceId = this.config.localWorkspaceId) {
    const trace = this.db.one('SELECT * FROM query_traces WHERE id=? AND workspace_id=?', traceId, workspaceId);
    if (!trace) throw new AppError(404, 'NOT_FOUND', '问答 Trace 不存在或不可访问');
    // Snapshot columns were added after the original trace schema. Parse them
    // explicitly so old rows (and new rows) have the same public shape.
    for (const field of ['config_snapshot', 'input_snapshot', 'permission_snapshot']) trace[field] = parseJson(trace[field], {});
    if (!trace.root) trace.root = trace.id;
    trace.citations = this.db.all('SELECT id,title,locator_json,excerpt,ordinal,document_id,document_revision_id,chunk_revision_id,release_id FROM citations WHERE trace_id=? AND workspace_id=? ORDER BY ordinal', traceId, workspaceId);
    return trace;
  }

  listTraces(workspaceId = this.config.localWorkspaceId, { conversationId, limit = 100, offset = 0 } = {}) {
    const clauses = ['workspace_id=?'];
    const params = [workspaceId];
    if (conversationId) { clauses.push('conversation_id=?'); params.push(conversationId); }
    const total = this.db.one(`SELECT COUNT(*) AS count FROM query_traces WHERE ${clauses.join(' AND ')}`, ...params)?.count || 0;
    const items = this.db.all(`SELECT * FROM query_traces WHERE ${clauses.join(' AND ')} ORDER BY created_at DESC LIMIT ? OFFSET ?`, ...params, limit, offset);
    return { items, total, limit, offset };
  }

  async replayTrace(traceId, input = {}, workspaceId = this.config.localWorkspaceId, requestId, idempotencyKey = null) {
    const source = this.getTrace(traceId, workspaceId);
    const body = input && typeof input === 'object' ? input : {};
    const fromStage = body.fromStage === undefined || body.fromStage === null || body.fromStage === '' ? null : String(body.fromStage);
    // The query pipeline currently persists all stage inputs together. Only a
    // replay from the pipeline boundary is therefore a real replay; claiming
    // that a later stage can be resumed would silently fabricate state.
    const supported = new Set(['问题解析', 'question_parse', 'parse', 'start', 'all']);
    if (fromStage && !supported.has(fromStage)) {
      throw new AppError(422, 'REPLAY_UNSUPPORTED', `不支持从阶段“${fromStage}”重放：当前仅支持从问题解析阶段重新执行完整问答流水线`, { fromStage, supportedFromStages: [...supported] });
    }
    const overrides = body.overrides && typeof body.overrides === 'object' && !Array.isArray(body.overrides) ? body.overrides : {};
    const allowed = ['question', 'query', 'topK', 'modelConnectionId', 'strictEvidence'];
    const unknown = Object.keys(overrides).filter(key => !allowed.includes(key));
    if (unknown.length) throw new AppError(400, 'VALIDATION_ERROR', 'replay overrides 包含不支持的字段', { fields: unknown });
    const replayInput = {
      question: overrides.question ?? overrides.query ?? source.query,
      ...(overrides.topK === undefined ? {} : { topK: overrides.topK }),
      ...(overrides.modelConnectionId === undefined ? {} : { modelConnectionId: overrides.modelConnectionId }),
      ...(overrides.strictEvidence === undefined ? {} : { strictEvidence: overrides.strictEvidence })
    };
    const requestFingerprint = stableJson({ sourceTraceId: traceId, fromStage, overrides: replayInput });
    if (idempotencyKey) {
      const existing = this.db.one("SELECT * FROM query_traces WHERE workspace_id=? AND trace_type='replay' AND json_extract(input_snapshot,'$.idempotencyKey')=? ORDER BY created_at LIMIT 1", workspaceId, String(idempotencyKey));
      if (existing) {
        const existingInput = parseJson(existing.input_snapshot, {});
        if (existingInput.requestFingerprint !== requestFingerprint) throw new AppError(409, 'IDEMPOTENCY_CONFLICT', '相同幂等键对应了不同的 Trace 重放输入');
        const existingTrace = this.getTrace(existing.id, workspaceId);
        return { trace: existingTrace, assistantMessage: existingTrace.message_id ? this.db.one('SELECT * FROM messages WHERE id=? AND workspace_id=?', existingTrace.message_id, workspaceId) : null, replayed: false, idempotent: true };
      }
    }
    const configSnapshot = {
      modelConnectionId: overrides.modelConnectionId !== undefined ? overrides.modelConnectionId : source.config_snapshot?.modelConnectionId ?? null,
      strictEvidence: overrides.strictEvidence !== undefined ? Boolean(overrides.strictEvidence) : source.config_snapshot?.strictEvidence ?? true,
      topK: overrides.topK !== undefined ? overrides.topK : source.config_snapshot?.topK ?? 8
    };
    const result = await this.ask(source.conversation_id, replayInput, workspaceId, requestId, {
      parentTraceId: traceId,
      rootTraceId: source.root || source.id,
      traceType: 'replay',
      replayFromStage: fromStage || '问题解析',
      configSnapshot,
      inputSnapshot: { sourceTraceId: traceId, fromStage: fromStage || '问题解析', overrides: replayInput, idempotencyKey: idempotencyKey ? String(idempotencyKey) : null, requestFingerprint },
      permissionSnapshot: source.permission_snapshot || { workspaceId, conversationId: source.conversation_id, releaseId: source.release_id },
      retention: source.retention || 'standard',
      idempotencyKey: idempotencyKey ? String(idempotencyKey) : null
    });
    return { trace: result.trace, userMessage: result.userMessage, assistantMessage: result.assistantMessage, replayed: true, idempotent: false };
  }

  compareTraces(traceId, otherTraceId, workspaceId = this.config.localWorkspaceId) {
    const left = this.getTrace(traceId, workspaceId);
    const right = this.getTrace(otherTraceId, workspaceId);
    const parseStages = trace => Array.isArray(trace.stages) ? trace.stages : parseJson(trace.stages_json, []);
    const leftStages = parseStages(left);
    const rightStages = parseStages(right);
    const stageNames = [...new Set([...leftStages, ...rightStages].map(stage => stage.name))];
    const stages = stageNames.map(name => {
      const a = leftStages.find(stage => stage.name === name) || null;
      const b = rightStages.find(stage => stage.name === name) || null;
      return { name, left: a, right: b, durationDiffMs: (b?.durationMs || 0) - (a?.durationMs || 0), statusChanged: (a?.status || null) !== (b?.status || null) };
    });
    const candidatesFrom = trace => {
      const stage = parseStages(trace).find(item => item.name === '多路召回');
      const output = stage?.output || {};
      return Array.isArray(output.fusion) ? output.fusion : [];
    };
    const leftCandidates = candidatesFrom(left);
    const rightCandidates = candidatesFrom(right);
    const key = candidate => candidate.chunkRevisionId || candidate.documentId || candidate.title;
    const leftKeys = new Set(leftCandidates.map(key));
    const rightKeys = new Set(rightCandidates.map(key));
    const candidates = {
      left: leftCandidates, right: rightCandidates,
      added: rightCandidates.filter(item => !leftKeys.has(key(item))),
      removed: leftCandidates.filter(item => !rightKeys.has(key(item))),
      common: rightCandidates.filter(item => leftKeys.has(key(item)))
    };
    const answerFrom = trace => trace.message_id ? this.db.one("SELECT id,content,evidence_status FROM messages WHERE id=? AND workspace_id=? AND role='assistant'", trace.message_id, workspaceId) : null;
    const leftAnswer = answerFrom(left);
    const rightAnswer = answerFrom(right);
    const leftMs = Number(left.metrics?.totalMs || 0);
    const rightMs = Number(right.metrics?.totalMs || 0);
    const answer = { left: leftAnswer, right: rightAnswer, changed: (leftAnswer?.content || '') !== (rightAnswer?.content || ''), contentChanged: (leftAnswer?.content || '') !== (rightAnswer?.content || '') };
    const timing = { leftMs, rightMs, deltaMs: rightMs - leftMs };
    return { traceId, otherTraceId, stages, stageDiffs: stages, candidates, candidateDiff: candidates, answer, answers: answer, timing, durationDiffMs: timing.deltaMs };
  }

  openCitation(citationId, workspaceId = this.config.localWorkspaceId) {
    const citation = this.db.one(`SELECT c.*,d.status AS document_status,cr.content_md,cr.content_text
      FROM citations c JOIN documents d ON d.id=c.document_id JOIN chunk_revisions cr ON cr.id=c.chunk_revision_id
      WHERE c.id=? AND c.workspace_id=?`, citationId, workspaceId);
    if (!citation) throw new AppError(404, 'NOT_FOUND', '引用不存在或不可访问');
    const releaseLink = this.db.one(`SELECT 1 AS allowed FROM release_chunks rc JOIN knowledge_releases kr ON kr.id=rc.release_id
      WHERE rc.release_id=? AND rc.chunk_revision_id=? AND kr.workspace_id=?`, citation.release_id, citation.chunk_revision_id, workspaceId);
    if (!releaseLink) throw new AppError(410, 'CITATION_INVALID', '引用不再属于固定知识版本');
    return {
      id: citation.id, title: citation.title, locator: citation.locator, excerpt: citation.excerpt,
      documentStatus: citation.document_status,
      documentId: citation.document_id, documentRevisionId: citation.document_revision_id,
      chunkRevisionId: citation.chunk_revision_id, releaseId: citation.release_id,
      contentMd: citation.content_md, contentText: citation.content_text, locationLabel: formatLocator(citation.locator)
    };
  }

  feedback(messageId, input, workspaceId = this.config.localWorkspaceId, requestId) {
    const message = this.db.one("SELECT * FROM messages WHERE id=? AND workspace_id=? AND role='assistant'", messageId, workspaceId);
    if (!message) throw new AppError(404, 'NOT_FOUND', '回答消息不存在或不可访问');
    const rating = Number(input.rating);
    if (![1,-1].includes(rating)) throw new AppError(400, 'VALIDATION_ERROR', 'rating 只能是 1 或 -1');
    const feedbackId = id('fb');
    this.db.run(`INSERT INTO feedback(id,workspace_id,message_id,rating,reason,created_at) VALUES(?,?,?,?,?,?)
      ON CONFLICT(workspace_id,message_id) DO UPDATE SET rating=excluded.rating,reason=excluded.reason,created_at=excluded.created_at`,
      feedbackId, workspaceId, messageId, rating, input.reason || '', now());
    this.audit.append({ workspaceId, action: 'feedback.save', objectType: 'message', objectId: messageId, requestId, details: { rating } });
    return this.db.one('SELECT * FROM feedback WHERE workspace_id=? AND message_id=?', workspaceId, messageId);
  }
}

function parseQuestion(question, conversation) {
  const normalized = String(question).trim().replace(/\s+/g, ' ');
  const entities = [...new Set(normalized.match(/[A-Za-z]+[-_.]?[A-Za-z0-9]*|[\u4e00-\u9fff]{2,8}/g) || [])].slice(0, 12);
  const needsClarification = normalized.length < 2;
  return {
    original: question,
    normalized,
    language: /[\u4e00-\u9fff]/.test(normalized) ? 'zh-CN' : 'und',
    intent: /怎么|如何|步骤|安装|配置/.test(normalized) ? 'procedure' : /多少|几|统计/.test(normalized) ? 'fact' : 'knowledge_query',
    entities,
    filters: { workspaceId: conversation.workspace_id, datasetId: conversation.dataset_id, releaseId: conversation.release_id },
    needsClarification,
    policy: conversation.strict_evidence ? 'strict_evidence' : 'evidence_preferred'
  };
}

function isEvidenceRefusal(content) {
  return /(无法回答|不能回答|未找到|没有找到|证据不足|无法确认|无法确定|不知道)/.test(String(content || ''));
}

function routeQuery(question) {
  const exact = /[A-Z]{2,}[-_]?\d+|\b\d{3,}\b/.test(question);
  const table = /表格|字段|列|行|统计|数量/.test(question);
  return {
    routes: [
      { name: 'full_text', enabled: true, weight: exact ? 1.4 : 1 },
      { name: 'vector', enabled: true, weight: table ? 0.9 : 1 },
      { name: 'graph', enabled: false, reason: 'R2 图谱检索未启用' },
      { name: 'database', enabled: false, reason: 'R2 受控数据库模板未启用' }
    ],
    reason: exact ? '包含精确型号或编号，提高全文权重' : table ? '表格问题保留结构化块优先级' : '使用向量与全文混合检索'
  };
}

module.exports = { QueryService, parseQuestion, routeQuery };
