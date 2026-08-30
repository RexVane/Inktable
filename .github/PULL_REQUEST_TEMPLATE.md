## Summary

What this PR changes, and why.

## Test plan

- [ ] `cd services/api && uv run pytest` (if the sidecar changed)
- [ ] `cd apps/desktop && npm test` (if the desktop app changed)
- [ ] `cd apps/desktop && node scripts/csp-hash.js` (if `renderer/index.html` inline scripts changed)

## Notes

Does not move, copy, or rename user files unless that is the explicit point of the change.
