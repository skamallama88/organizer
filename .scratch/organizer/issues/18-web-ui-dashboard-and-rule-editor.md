# 18 — Web UI: dashboard and rule editor

**What to build:** Server-rendered HTML pages with HTMX and Alpine.js. A watch folder dashboard showing configured watches with health status, rule count, and recent activity summary. A YAML rule editor page with inline validation errors, dry-run preview via HTMX, and atomic save with conflict detection. Sets up Jinja2 templates, static file serving, and the HTMX/Alpine.js assets.

**Blocked by:** 16 (dashboard needs config-discovered watches; rule editor needs watch_id and rules_path resolution)

**Status:** ready-for-agent

- [ ] Add Jinja2 template directory and static asset setup to the FastAPI app
- [ ] Add HTMX and Alpine.js client-side assets
- [ ] Implement watch dashboard template: list configured watches, show health status, rule counts, last-activity time, links to rule editor and log viewer
- [ ] Implement rule editor template: YAML textarea, validate button → HTMX to validation endpoint inline, dry-run preview → HTMX to dry-run endpoint, save button with conflict feedback
- [ ] Wire template routes into FastAPI app (`GET /` → dashboard, `GET /watches/{watch_id}/rules` → editor)
- [ ] Tests: template rendering, HTMX form interactions, validation and dry-run feedback, atomic save with conflict detection
