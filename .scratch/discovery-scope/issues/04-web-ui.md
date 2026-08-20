# 04 — Web UI: discovery scope dropdown

**What to build:** Surface the per-watch `discovery` scope as a control in the dashboard's per-watch schedule cell (`watch_list.html`), alongside `enabled` and `scan_interval`, so administrators can switch a watch between `recursive` and `top_level` without editing YAML.

**Blocked by:** 03

**Status:** completed

- [ ] In `watch_list.html`, add a `<select name="discovery">` inside the per-watch `PATCH` form with options `recursive` (default) and `top_level`, pre-selecting the watch's current value from `_build_watches`
- [ ] Keep the existing hidden-`enabled` / `scan_interval` fields and the `Save` button so the form still patches all per-watch controls together
- [ ] No change to the add-watch form (`watch_form.html`): scope defaults to `recursive` on add

**Tests / verification:**
- [ ] Browser/htmx check: selecting `top_level` and saving issues a `PATCH` with `discovery=top_level` and the row re-renders with the value selected
- [ ] Selecting `recursive` saves and round-trips
- [ ] `organizer.yaml` reflects the saved value after a save
