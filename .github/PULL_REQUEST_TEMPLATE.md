## Summary / 摘要

What this PR changes, and why.  
这个 PR 改了什么、为什么改。

## Test plan / 测试

- [ ] `cd services/api && uv run pytest` (if the sidecar changed / 若改了 sidecar)
- [ ] `cd apps/desktop && npm test` (if the desktop app changed / 若改了桌面端)
- [ ] `cd apps/desktop && node scripts/csp-hash.js` (if `renderer/index.html` inline scripts changed / 若改了内联脚本)

## Notes / 说明

Does not move, copy, or rename user files unless that is the explicit point of the change.

除非改动的目的就是文件操作，否则不要移动、复制或改名用户原文件。
