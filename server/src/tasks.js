'use strict';

const { id, now, AppError, parseJson, stableJson } = require('./core');

class TaskService {
  constructor(db, audit, config) {
    this.db = db;
    this.audit = audit;
    this.config = config;
    this.handlers = new Map();
    this.running = new Set();
    this.db.run("UPDATE tasks SET status='queued',progress=0,started_at=NULL,finished_at=NULL,cancel_requested=0,pause_requested=0,error_code=NULL,error_message=NULL,updated_at=? WHERE status='running'", now());
  }

  register(type, handler) { this.handlers.set(type, handler); }

  create({ workspaceId = this.config.localWorkspaceId, type, objectType, objectId, idempotencyKey, input = {} }) {
    if (!this.handlers.has(type)) throw new AppError(400, 'TASK_TYPE_UNSUPPORTED', `不支持的任务类型: ${type}`);
    const existing = this.db.one('SELECT * FROM tasks WHERE workspace_id=? AND idempotency_key=?', workspaceId, idempotencyKey);
    const inputFingerprint = stableJson(input);
    if (existing) {
      const existingInput = parseJson(existing.input_json, {});
      if (stableJson(existingInput) !== inputFingerprint) throw new AppError(409, 'IDEMPOTENCY_CONFLICT', '相同幂等键对应了不同的任务输入');
      if (['queued','running','succeeded','partial'].includes(existing.status)) return existing;
      this.db.run("UPDATE tasks SET status='queued',progress=0,result_json='{}',error_code=NULL,error_message=NULL,cancel_requested=0,pause_requested=0,finished_at=NULL,updated_at=? WHERE id=?", now(), existing.id);
      setImmediate(() => this.execute(existing.id));
      return this.get(existing.id, workspaceId);
    }
    const taskId = id('task');
    const timestamp = now();
    this.db.run('INSERT INTO tasks(id,workspace_id,type,object_type,object_id,idempotency_key,status,progress,input_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
      taskId, workspaceId, type, objectType, objectId, idempotencyKey, 'queued', 0, JSON.stringify(input), '{}', timestamp, timestamp);
    this.event(taskId, workspaceId, 'info', 'queued', '任务已进入队列', { type, objectType, objectId });
    this.audit.append({ workspaceId, action: 'task.create', objectType: 'task', objectId: taskId, details: { type, objectType, objectId } });
    setImmediate(() => this.execute(taskId));
    return this.get(taskId, workspaceId);
  }

  event(taskId, workspaceId, level, eventType, message, data = {}) {
    const sequence = (this.db.one('SELECT COALESCE(MAX(sequence),0)+1 AS sequence FROM task_events WHERE task_id=?', taskId)?.sequence || 1);
    this.db.run('INSERT INTO task_events(id,workspace_id,task_id,sequence,level,event_type,message,data_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
      id('evt'), workspaceId, taskId, sequence, level, eventType, message, JSON.stringify(data), now());
  }

  async execute(taskId) {
    if (this.running.has(taskId)) return;
    const task = this.db.one('SELECT * FROM tasks WHERE id=?', taskId);
    if (!task || task.status !== 'queued') return;
    const handler = this.handlers.get(task.type);
    if (!handler) return;
    this.running.add(taskId);
    try {
      this.db.run("UPDATE tasks SET status='running',attempt=attempt+1,started_at=?,updated_at=? WHERE id=?", now(), now(), taskId);
      this.event(taskId, task.workspace_id, 'info', 'started', '任务开始执行');
      const context = {
        taskId,
        workspaceId: task.workspace_id,
        input: parseJson(task.input_json, {}),
        checkpoint: (progress, message, data = {}) => {
          const current = this.db.one('SELECT cancel_requested,pause_requested FROM tasks WHERE id=?', taskId);
          if (current?.cancel_requested) throw new AppError(409, 'TASK_CANCELLED', '任务已取消');
          if (current?.pause_requested) throw new AppError(409, 'TASK_PAUSED', '任务已在安全检查点暂停');
          const value = Math.max(0, Math.min(99, Math.round(progress)));
          this.db.run('UPDATE tasks SET progress=?,updated_at=? WHERE id=?', value, now(), taskId);
          if (message) this.event(taskId, task.workspace_id, 'info', 'progress', message, { progress: value, ...data });
        }
      };
      const result = await handler(context);
      const status = result?.status === 'partial' ? 'partial' : 'succeeded';
      this.db.run('UPDATE tasks SET status=?,progress=100,result_json=?,finished_at=?,updated_at=? WHERE id=?',
        status, JSON.stringify(result || {}), now(), now(), taskId);
      this.event(taskId, task.workspace_id, status === 'partial' ? 'warn' : 'info', status, status === 'partial' ? '任务部分完成' : '任务执行成功', result || {});
      this.audit.append({ workspaceId: task.workspace_id, action: `task.${status}`, objectType: 'task', objectId: taskId, details: { type: task.type } });
    } catch (error) {
      const cancelled = error.code === 'TASK_CANCELLED';
      const paused = error.code === 'TASK_PAUSED';
      const status = cancelled ? 'cancelled' : paused ? 'paused' : 'failed';
      const code = error.code || 'TASK_FAILED';
      const message = error instanceof AppError ? error.message : '任务执行失败';
      if (task.type === 'release.build') {
        const releaseId = parseJson(task.input_json, {}).releaseId;
        if (releaseId) this.db.run("UPDATE knowledge_releases SET status='failed',quality_json=?,manifest_json=? WHERE id=? AND workspace_id=? AND status NOT IN ('active','ready')", JSON.stringify({ valid: false, errorCode: code, errorMessage: message, failedAt: now() }), JSON.stringify({ releaseId, failed: true, errorCode: code }), releaseId, task.workspace_id);
      }
      this.db.run("UPDATE tasks SET status=?,progress=?,result_json='{}',error_code=?,error_message=?,finished_at=?,updated_at=? WHERE id=?",
        status, 0, code, message, paused ? null : now(), now(), taskId);
      this.event(taskId, task.workspace_id, cancelled || paused ? 'warn' : 'error', status, message, { code });
      this.audit.append({ workspaceId: task.workspace_id, action: `task.${status}`, objectType: 'task', objectId: taskId, result: status, details: { type: task.type, code } });
    } finally {
      this.running.delete(taskId);
    }
  }

  resumeQueued() {
    for (const task of this.db.all("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at")) setImmediate(() => this.execute(task.id));
  }

  get(taskId, workspaceId = this.config.localWorkspaceId) {
    const task = this.db.one('SELECT * FROM tasks WHERE id=? AND workspace_id=?', taskId, workspaceId);
    if (!task) throw new AppError(404, 'NOT_FOUND', '任务不存在或不可访问');
    task.events = this.db.all('SELECT * FROM task_events WHERE task_id=? AND workspace_id=? ORDER BY sequence', taskId, workspaceId);
    return task;
  }

  list(workspaceId, { status, type, limit = 100, offset = 0 } = {}) {
    const clauses = ['workspace_id=?'];
    const params = [workspaceId];
    if (status) { clauses.push('status=?'); params.push(status); }
    if (type) { clauses.push('type=?'); params.push(type); }
    const total = this.db.one(`SELECT COUNT(*) AS count FROM tasks WHERE ${clauses.join(' AND ')}`, ...params)?.count || 0;
    const items = this.db.all(`SELECT * FROM tasks WHERE ${clauses.join(' AND ')} ORDER BY created_at DESC LIMIT ? OFFSET ?`, ...params, limit, offset);
    return { items, total, limit, offset };
  }

  cancel(taskId, workspaceId) {
    const task = this.get(taskId, workspaceId);
    if (!['queued','running','paused'].includes(task.status)) throw new AppError(409, 'INVALID_STATE', '当前任务状态不可取消');
    if (task.status === 'queued') {
      this.db.run("UPDATE tasks SET status='cancelled',cancel_requested=1,finished_at=?,updated_at=? WHERE id=?", now(), now(), taskId);
      this.event(taskId, workspaceId, 'warn', 'cancelled', '任务在执行前取消');
    } else {
      this.db.run('UPDATE tasks SET cancel_requested=1,updated_at=? WHERE id=?', now(), taskId);
      this.event(taskId, workspaceId, 'warn', 'cancel_requested', '已请求在安全检查点取消');
    }
    return this.get(taskId, workspaceId);
  }

  pause(taskId, workspaceId) {
    const task = this.get(taskId, workspaceId);
    if (task.status !== 'running') throw new AppError(409, 'INVALID_STATE', '只有运行中的任务可以暂停');
    this.db.run('UPDATE tasks SET pause_requested=1,updated_at=? WHERE id=?', now(), taskId);
    this.event(taskId, workspaceId, 'warn', 'pause_requested', '已请求在安全检查点暂停');
    return this.get(taskId, workspaceId);
  }

  resume(taskId, workspaceId) {
    const task = this.get(taskId, workspaceId);
    if (task.status !== 'paused') throw new AppError(409, 'INVALID_STATE', '只有已暂停任务可以继续');
    this.db.run("UPDATE tasks SET status='queued',progress=0,result_json='{}',pause_requested=0,cancel_requested=0,error_code=NULL,error_message=NULL,finished_at=NULL,updated_at=? WHERE id=?", now(), taskId);
    this.event(taskId, workspaceId, 'info', 'resume_queued', '任务已从持久化输入重新排队');
    setImmediate(() => this.execute(taskId));
    return this.get(taskId, workspaceId);
  }

  retry(taskId, workspaceId) {
    const task = this.get(taskId, workspaceId);
    if (!['failed','cancelled','partial'].includes(task.status)) throw new AppError(409, 'INVALID_STATE', '只有失败、取消或部分成功任务可以重试');
    this.db.run("UPDATE tasks SET status='queued',progress=0,result_json='{}',cancel_requested=0,pause_requested=0,error_code=NULL,error_message=NULL,finished_at=NULL,updated_at=? WHERE id=?", now(), taskId);
    this.event(taskId, workspaceId, 'info', 'retry_queued', '任务已重新进入队列');
    setImmediate(() => this.execute(taskId));
    return this.get(taskId, workspaceId);
  }

  async wait(taskId, workspaceId, timeoutMs = 10_000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const task = this.get(taskId, workspaceId);
      if (['succeeded','partial','failed','cancelled','paused'].includes(task.status)) return task;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    throw new AppError(408, 'TASK_WAIT_TIMEOUT', '等待任务完成超时');
  }
}

module.exports = { TaskService };
