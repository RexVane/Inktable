import asyncio
import time

from .core import AppError, gen_id, now, parse_json, stable_json

TERMINAL_STATES = {'succeeded', 'partial', 'failed', 'cancelled'}


class TaskService:
    def __init__(self, db, audit, config):
        self.db = db
        self.audit = audit
        self.config = config
        self.handlers = {}
        self.running = set()
        self._scheduled = {}
        self._closing = False
        self.parsing_manual = set()
        self.db.run("UPDATE tasks SET status='queued',progress=0,started_at=NULL,finished_at=NULL,cancel_requested=0,pause_requested=0,error_code=NULL,error_message=NULL,updated_at=? WHERE status='running'", now())

    def register(self, task_type, handler):
        self.handlers[task_type] = handler

    def _schedule(self, task_id):
        if self._closing or task_id in self._scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
            future = loop.create_task(self.execute(task_id))
            self._scheduled[task_id] = future
            future.add_done_callback(lambda _: self._scheduled.pop(task_id, None))
        except RuntimeError:
            # 允许在无事件循环的上下文（如脚本）中排队，下次 resume_queued 兜底
            pass

    def create(self, workspace_id=None, task_type=None, object_type=None, object_id=None, idempotency_key=None, input=None, **_ignored):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        if task_type not in self.handlers:
            raise AppError(400, 'TASK_TYPE_UNSUPPORTED', f'不支持的任务类型: {task_type}')
        existing = self.db.one('SELECT * FROM tasks WHERE workspace_id=? AND idempotency_key=?', workspace_id, idempotency_key)
        input = input or {}
        input_fingerprint = stable_json(input)
        if existing:
            existing_input = parse_json(existing['input_json'], {})
            if stable_json(existing_input) != input_fingerprint:
                raise AppError(409, 'IDEMPOTENCY_CONFLICT', '相同幂等键对应了不同的任务输入')
            if existing['status'] in ('queued', 'running', 'paused', 'succeeded', 'partial'):
                return existing
            self.db.run("UPDATE tasks SET status='queued',progress=0,result_json='{}',error_code=NULL,error_message=NULL,cancel_requested=0,pause_requested=0,finished_at=NULL,updated_at=? WHERE id=?", now(), existing['id'])
            self._schedule(existing['id'])
            return self.get(existing['id'], workspace_id)
        task_id = gen_id('task')
        timestamp = now()
        self.db.run('INSERT INTO tasks(id,workspace_id,type,object_type,object_id,idempotency_key,status,progress,input_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    task_id, workspace_id, task_type, object_type, object_id, idempotency_key, 'queued', 0,
                    stable_json(input), '{}', timestamp, timestamp)
        self.event(task_id, workspace_id, 'info', 'queued', '任务已进入队列', {'type': task_type, 'objectType': object_type, 'objectId': object_id})
        self.audit.append(workspace_id=workspace_id, action='task.create', object_type='task', object_id=task_id,
                          details={'type': task_type, 'objectType': object_type, 'objectId': object_id})
        self._schedule(task_id)
        return self.get(task_id, workspace_id)

    def event(self, task_id, workspace_id, level, event_type, message, data=None):
        data = data or {}
        sequence = (self.db.one('SELECT COALESCE(MAX(sequence),0)+1 AS sequence FROM task_events WHERE task_id=?', task_id) or {}).get('sequence', 1)
        self.db.run('INSERT INTO task_events(id,workspace_id,task_id,sequence,level,event_type,message,data_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    gen_id('evt'), workspace_id, task_id, sequence, level, event_type, message, stable_json(data), now())

    async def execute(self, task_id):
        if task_id in self.running:
            return
        queued = self.db.one("SELECT * FROM tasks WHERE id=? AND status='queued'", task_id)
        if not queued:
            return
        if queued['type'] == 'document.parse':
            while not self._closing:
                setting = self.db.one("SELECT value_json FROM settings WHERE workspace_id=? AND key='ingestion'", queued['workspace_id'])
                settings = (setting or {}).get('value') or {}
                if not settings.get('autoParsingEnabled', True) and queued['workspace_id'] not in self.parsing_manual:
                    return
                limit = settings.get('concurrency', 4)
                active = self.db.one("SELECT COUNT(*) n FROM tasks WHERE workspace_id=? AND type='document.parse' AND status='running'", queued['workspace_id'])['n']
                if active < limit:
                    break
                await asyncio.sleep(0.05)
            if self._closing:
                return
        handler = self.handlers.get(queued['type'])
        if not handler:
            return
        self.running.add(task_id)
        task = None
        try:
            claim = self.db.run("UPDATE tasks SET status='running',attempt=attempt+1,started_at=?,updated_at=? WHERE id=? AND status='queued'", now(), now(), task_id)
            if not claim.rowcount:
                return
            task = self.db.one('SELECT * FROM tasks WHERE id=?', task_id)
            self.event(task_id, task['workspace_id'], 'info', 'started', '任务开始执行')

            async def checkpoint(progress, message=None, data=None):
                current = self.db.one('SELECT cancel_requested,pause_requested FROM tasks WHERE id=?', task_id)
                if current and current['cancel_requested']:
                    raise AppError(409, 'TASK_CANCELLED', '任务已取消')
                if current and current['pause_requested']:
                    raise AppError(409, 'TASK_PAUSED', '任务已在安全检查点暂停')
                value = max(0, min(99, round(progress)))
                self.db.run('UPDATE tasks SET progress=?,updated_at=? WHERE id=?', value, now(), task_id)
                if message:
                    self.event(task_id, task['workspace_id'], 'info', 'progress', message, {'progress': value, **(data or {})})
                await asyncio.sleep(0)

            context = {'taskId': task_id, 'workspaceId': task['workspace_id'], 'input': parse_json(task['input_json'], {}), 'checkpoint': checkpoint}
            result = await handler(context)
            status = 'partial' if isinstance(result, dict) and result.get('status') == 'partial' else 'succeeded'
            finished = now()
            committed = self.db.transaction(lambda: self._commit_success(task_id, task['workspace_id'], status, result or {}, finished))
            if not committed:
                current = self.db.one('SELECT cancel_requested,pause_requested FROM tasks WHERE id=?', task_id)
                if current and current['cancel_requested']:
                    raise AppError(409, 'TASK_CANCELLED', '任务已取消')
                if current and current['pause_requested']:
                    raise AppError(409, 'TASK_PAUSED', '任务已在安全检查点暂停')
                raise AppError(409, 'TASK_STATE_CHANGED', '任务状态已发生变化')
            self.audit.append(workspace_id=task['workspace_id'], action=f'task.{status}', object_type='task', object_id=task_id, details={'type': task['type']})
        except AppError as error:
            self._fail(task_id, task, error)
        except BaseException as error:  # noqa: BLE001 - 任务执行器必须兜底
            self._fail(task_id, task, AppError(500, 'TASK_FAILED', str(error) or '任务执行失败'))
        finally:
            self.running.discard(task_id)

    def _commit_success(self, task_id, workspace_id, status, result, finished):
        update = self.db.run("UPDATE tasks SET status=?,progress=100,result_json=?,finished_at=?,updated_at=? WHERE id=? AND status='running' AND cancel_requested=0 AND pause_requested=0",
                             status, stable_json(result), finished, finished, task_id)
        if not update.rowcount:
            return False
        self.event(task_id, workspace_id, 'warn' if status == 'partial' else 'info', status,
                   '任务部分完成' if status == 'partial' else '任务执行成功', result)
        return True

    def _fail(self, task_id, task, error):
        cancelled = error.code == 'TASK_CANCELLED'
        paused = error.code == 'TASK_PAUSED'
        status = 'cancelled' if cancelled else 'paused' if paused else 'failed'
        code = error.code or 'TASK_FAILED'
        message = error.message if isinstance(error, AppError) else '任务执行失败'
        if task and task['type'] == 'release.build':
            release_id = (parse_json(task['input_json'], {}) or {}).get('releaseId')
            if release_id:
                self.db.run("UPDATE knowledge_releases SET status='failed',quality_json=?,manifest_json=? WHERE id=? AND workspace_id=? AND status NOT IN ('active','ready')",
                            stable_json({'valid': False, 'errorCode': code, 'errorMessage': message, 'failedAt': now()}),
                            stable_json({'releaseId': release_id, 'failed': True, 'errorCode': code}), release_id, task['workspace_id'])
        finished = None if paused else now()
        self.db.run("UPDATE tasks SET status=?,result_json='{}',error_code=?,error_message=?,finished_at=?,updated_at=?,pause_requested=0 WHERE id=? AND status='running'",
                    status, code, message, finished, now(), task_id)
        self.event(task_id, task['workspace_id'] if task else self.config['localWorkspaceId'],
                   'warn' if cancelled or paused else 'error', status, message, {'code': code})
        if task:
            self.audit.append(workspace_id=task['workspace_id'], action=f'task.{status}', object_type='task', object_id=task_id,
                              result=status, details={'type': task['type'], 'code': code})

    def resume_queued(self):
        for task in self.db.all("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at"):
            self._schedule(task['id'])

    async def shutdown(self):
        self._closing = True
        # Let isolated parser processes reach their bounded timeout and commit before closing SQLite.
        if self._scheduled:
            await asyncio.gather(*list(self._scheduled.values()), return_exceptions=True)

    def get(self, task_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        task = self.db.one('SELECT * FROM tasks WHERE id=? AND workspace_id=?', task_id, workspace_id)
        if not task:
            raise AppError(404, 'NOT_FOUND', '任务不存在或不可访问')
        task['events'] = self.db.all('SELECT * FROM task_events WHERE task_id=? AND workspace_id=? ORDER BY sequence', task_id, workspace_id)
        return task

    def list(self, workspace_id, status=None, task_type=None, limit=100, offset=0):
        clauses = ['workspace_id=?']
        params = [workspace_id]
        if status:
            clauses.append('status=?')
            params.append(status)
        if task_type:
            clauses.append('type=?')
            params.append(task_type)
        total = (self.db.one(f"SELECT COUNT(*) AS count FROM tasks WHERE {' AND '.join(clauses)}", *params) or {}).get('count', 0)
        items = self.db.all(f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ? OFFSET ?", *params, limit, offset)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def cancel(self, task_id, workspace_id):
        task = self.get(task_id, workspace_id)
        if task['status'] in TERMINAL_STATES:
            raise AppError(409, 'INVALID_STATE', '当前任务状态不可取消')
        timestamp = now()
        if task['status'] == 'queued':
            self.db.run("UPDATE tasks SET status='cancelled',cancel_requested=1,pause_requested=0,finished_at=?,updated_at=? WHERE id=? AND workspace_id=? AND status='queued'", timestamp, timestamp, task_id, workspace_id)
            self.event(task_id, workspace_id, 'warn', 'cancelled', '任务在执行前取消')
        elif task['status'] == 'paused':
            self.db.run("UPDATE tasks SET status='cancelled',cancel_requested=1,pause_requested=0,finished_at=?,updated_at=? WHERE id=? AND workspace_id=? AND status='paused'", timestamp, timestamp, task_id, workspace_id)
            self.event(task_id, workspace_id, 'warn', 'cancelled', '已取消已暂停任务')
        else:
            self.db.run("UPDATE tasks SET cancel_requested=1,pause_requested=0,updated_at=? WHERE id=? AND workspace_id=? AND status='running'", timestamp, task_id, workspace_id)
            self.event(task_id, workspace_id, 'warn', 'cancel_requested', '已请求在安全检查点取消')
        return self.get(task_id, workspace_id)

    def pause(self, task_id, workspace_id):
        task = self.get(task_id, workspace_id)
        if task['status'] == 'queued':
            self.db.run("UPDATE tasks SET status='paused',pause_requested=0,updated_at=? WHERE id=? AND workspace_id=? AND status='queued'", now(), task_id, workspace_id)
            self.event(task_id, workspace_id, 'info', 'paused', '排队中的任务已暂停')
            return self.get(task_id, workspace_id)
        if task['status'] != 'running':
            raise AppError(409, 'INVALID_STATE', '只有运行中的任务可以暂停')
        result = self.db.run("UPDATE tasks SET pause_requested=1,cancel_requested=0,updated_at=? WHERE id=? AND workspace_id=? AND status='running'", now(), task_id, workspace_id)
        if not result.rowcount:
            raise AppError(409, 'INVALID_STATE', '任务状态已发生变化')
        self.event(task_id, workspace_id, 'warn', 'pause_requested', '已请求在安全检查点暂停')
        return self.get(task_id, workspace_id)

    def resume(self, task_id, workspace_id):
        task = self.get(task_id, workspace_id)
        if task['status'] != 'paused':
            raise AppError(409, 'INVALID_STATE', '只有已暂停任务可以继续')
        result = self.db.run("UPDATE tasks SET status='queued',pause_requested=0,cancel_requested=0,error_code=NULL,error_message=NULL,finished_at=NULL,updated_at=? WHERE id=? AND workspace_id=? AND status='paused'", now(), task_id, workspace_id)
        if not result.rowcount:
            raise AppError(409, 'INVALID_STATE', '任务状态已发生变化')
        self.event(task_id, workspace_id, 'info', 'resume_queued', '任务已从持久化输入重新排队')
        self._schedule(task_id)
        return self.get(task_id, workspace_id)

    def retry(self, task_id, workspace_id):
        task = self.get(task_id, workspace_id)
        if task['status'] not in ('failed', 'cancelled', 'partial'):
            raise AppError(409, 'INVALID_STATE', '只有失败、取消或部分成功任务可以重试')
        self.db.run("UPDATE tasks SET status='queued',progress=0,result_json='{}',cancel_requested=0,pause_requested=0,error_code=NULL,error_message=NULL,finished_at=NULL,updated_at=? WHERE id=?", now(), task_id)
        self.event(task_id, workspace_id, 'info', 'retry_queued', '任务已重新进入队列')
        self._schedule(task_id)
        return self.get(task_id, workspace_id)

    async def wait(self, task_id, workspace_id, timeout_ms=10_000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            task = self.get(task_id, workspace_id)
            if task['status'] in ('succeeded', 'partial', 'failed', 'cancelled', 'paused'):
                return task
            await asyncio.sleep(0.025)
        raise AppError(408, 'TASK_WAIT_TIMEOUT', '等待任务完成超时')
