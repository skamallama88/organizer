# 04 — Web UI: add and remove watches from the dashboard

**What to build:** The dashboard gets an "Add Watch" button that reveals an inline HTMX form. Fields: Watch ID (text input), Root path (dropdown populated from current `data_roots`), Rules path (text input, defaulted to `/config/rules_{watch_id}.yaml`). Each existing watch card on the dashboard gets a "Remove" button with an HTMX confirmation dialog. Submit calls `POST /watches`; success swaps the watch list partial back in. Remove calls `DELETE /watches/{watch_id}` with confirmation. Errors display inline. New partial: `watch_form.html`. Updated template: `dashboard.html`.

**Blocked by:** 03

**Status:** ready-for-agent

- [ ] "Add Watch" button on the dashboard triggers HTMX GET or inline swap of the `watch_form.html` partial
- [ ] The form includes: `id` (text), `root` (dropdown from `data_roots`, or freeform text with directory browsing), `rules_path` (text defaulting to `/config/rules_{id}.yaml`)
- [ ] Submit POSTs to `/watches`; success response replaces the form with the updated watch list partial
- [ ] "Remove" button per watch card with HTMX confirmation; DELETE to `/watches/{watch_id}`
- [ ] Validation error responses (422) shown inline on the form
- [ ] Updated `dashboard.html` renders watches as cards with the add/remove affordances
