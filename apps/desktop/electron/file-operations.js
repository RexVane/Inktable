'use strict';

function validFileId(value) {
  const id = Number(value);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

/**
 * Move every currently registered target for one file to the OS trash, then
 * remove the library row. Paths never cross the renderer boundary: they are
 * resolved from file_id by the authenticated sidecar immediately before the
 * trusted native confirmation.
 */
async function trashFileById(fileId, deps) {
  const id = validFileId(fileId);
  if (!id) return { ok: false, error: '无效文件 ID' };
  const prepared = await deps.sidecarRequest(`/files/${id}/trash-targets`, 'GET');
  if (!prepared || !prepared.ok) {
    return { ok: false, error: (prepared && prepared.error) || '无法读取文件状态' };
  }
  const payload = prepared.data || {};
  const targets = Array.isArray(payload.targets)
    ? payload.targets.filter((item) => item && typeof item.path === 'string' &&
      (item.kind === 'source' || item.kind === 'preserved'))
    : [];
  if (!targets.length) return { ok: false, error: '原文件和保全副本都已不存在' };

  const confirmed = await deps.confirm({
    name: String(payload.name || '该文件'),
    source: targets.some((item) => item.kind === 'source'),
    preserved: targets.some((item) => item.kind === 'preserved'),
  });
  if (!confirmed) return { ok: false, cancelled: true };

  let moved = 0;
  const failedKinds = [];
  for (const target of targets) {
    try {
      await deps.trashItem(target.path);
      moved += 1;
    } catch {
      failedKinds.push(target.kind);
    }
  }
  if (failedKinds.length) {
    return {
      ok: false,
      partial: moved > 0,
      moved,
      failed: failedKinds.length,
      error: `有 ${failedKinds.length} 个文件未能移入废纸篓，库内记录已保留`,
    };
  }

  const removed = await deps.sidecarRequest('/files/remove', 'POST', { file_ids: [id] });
  if (!removed || !removed.ok) {
    return {
      ok: false,
      moved,
      error: '文件已移入废纸篓，但库内记录清理失败；重新扫描后可恢复一致状态',
    };
  }
  return { ok: true, moved, removed: Number(removed.data && removed.data.removed) || 0 };
}

module.exports = { trashFileById, validFileId };
