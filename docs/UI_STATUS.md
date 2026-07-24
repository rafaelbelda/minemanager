# Web UI — what's built, what isn't

Status of [`../web/`](../web/) against the API contract in
[`UI_CONTEXT.md`](UI_CONTEXT.md). The UI targets the **v1 manage-only** scope
(PLAN.md §11); anything outside that is listed here rather than silently absent.

**Summary:** every REST endpoint and every event action the hub exposes today is
consumed by the UI. What's missing is either blocked on a hub endpoint that
doesn't exist yet, or a convenience the v1 scope doesn't require.

---

## 1. API coverage

Every path from [`UI_CONTEXT.md` §9](UI_CONTEXT.md#9-quick-reference-full-endpoint-list):

| Endpoint | Used by |
|---|---|
| `GET /api/health` | topbar health indicator (15s poll), hub version badge |
| `GET /api/nodes` | tree, hub stats, node cards, online gating (10s poll) |
| `POST /api/nodes` | "Add node" → enrollment card |
| `POST /api/nodes/{id}/reenroll` | node detail → "Re-enroll" |
| `DELETE /api/nodes/{id}` | node detail → "Delete node" (confirmed) |
| `GET /api/nodes/{id}/instances` | tree, node detail table, hub stats |
| `POST /api/nodes/{id}/instances` | node detail → "+ Add instance" |
| `PATCH /api/instances/{id}` | settings tab → "Save changes" |
| `DELETE /api/instances/{id}` | settings tab → "Delete instance" (confirmed) |
| `POST /api/instances/{id}/power/{op}` | all four ops, header + table row buttons |
| `POST /api/instances/{id}/console` | console input |
| `GET /api/instances/{id}/files` | file browser, directory navigation |
| `GET /api/instances/{id}/files/read` | editor open |
| `POST /api/instances/{id}/files/write` | editor save, new file |
| `DELETE /api/instances/{id}/files` | per-row delete (confirmed, recursive for dirs) |
| `PUT /api/instances/{id}/secrets` | secrets → Set / Overwrite |
| `GET /api/instances/{id}/secrets` | secrets → SET / NOT SET state |
| `WS /api/nodes/{id}/events` | one socket per node, capped backoff reconnect |

Event actions (§5): `console.output` and `log.line` → terminal;
`state.changed` → status badges, power gating, tree dots; `heartbeat` →
instance→state map + node liveness. No action goes unhandled.

Error conventions (§7) are mapped centrally in
[`api.js`](../web/api.js) — `409` becomes "Node offline — can't reach the
agent", `502` surfaces the agent's own `detail`, `504` offers a retry.

---

## 2. Built

**Nodes & enrollment** — infrastructure tree with live status dots and a
name/hostname/instance filter; hub overview with online / instance / running /
needs-attention counters and node cards; add-node flow showing the one-time
token with a copy button, an install hint built from the hub's own origin, and a
3s poll that flips to "agent connected" when the node comes online; re-enroll
and delete.

**Instances** — per-node table with type, live state, root and inline power
controls; create and delete; full spec editing (name, type, root, start command,
auto-restart, RCON host/port) via `PATCH`, with the "applies on next start"
caveat stated in the form and repeated in the save toast.

**Power** — start/stop/restart/kill, disabled per current run state and gated on
`node.online` so the common `409` is avoided rather than surfaced. Kill asks for
confirmation. The POST result seeds state; `state.changed` drives it thereafter.

**Console** — live terminal fed by the events socket, Minecraft log parsing for
both the `[HH:MM:SS] [Thread/LEVEL]:` and `[HH:MM:SS LEVEL]:` formats, WARN/ERROR
colouring, 1500-line cap, autoscroll that releases when you scroll up, scanline
and clear toggles, and a line-send input that echoes locally. Shows the gap-#3
hint ("restart to attach console") when a running instance has no stream.

**Files** — jailed browser with directory navigation and breadcrumb, text editor
with a line-number gutter and comment highlighting, save (also `Ctrl/Cmd+S`),
new file, delete with confirm, and an unsaved-changes guard on tab switch, file
switch and page unload.

**Secrets** — shows which keys are set without ever displaying a value; set,
overwrite, and add arbitrary keys. `forwarding_secret` is offered for Velocity
instances.

**Chrome** — deep links (`#/node/<id>`, `#/inst/<id>`) survive reload; toasts for
every action outcome; offline nodes show a banner and disable live controls
throughout.

---

## 3. Missing — blocked on the hub

These need a backend endpoint first. Tracked in PLAN.md §11b.

| Gap | UI today | Needs |
|---|---|---|
| **Binary upload** | upload button visible but disabled, tooltip "coming soon" | REST surface for the agent's `files.upload` |
| **Historical logs** | reachable the long way: Files → `logs/` → `latest.log` | a log endpoint; rotated `*.log.gz` list but can't be opened (`files/read` is utf-8, so it 502s) |
| **RCON console** | not offered at all | REST surface for the agent's `rcon.command` |
| **Audit log** | no history view | the model to be populated and exposed |
| **`desired_running`** | deliberately not shown | reconciliation on agent reconnect; showing it now would imply a restore guarantee that doesn't exist |
| **Secret deletion** | can overwrite, can't remove | `DELETE /api/instances/{id}/secrets/{key}` |
| **Explicit mkdir** | new directories only happen implicitly, via `files/write` creating parents | REST surface for the agent's `files.mkdir` |
| **File rename / move** | not offered | no API for it |
| **Console before agent attach** | hint shown, nothing more possible | agent-side attach to pre-existing sessions (gap #3) |

## 4. Missing — UI not built yet

Not blocked by anything; simply out of v1 scope.

- **Theme controls.** The mock exposed accent and phosphor colour pickers. Both
  are CSS custom properties (`--mm-accent`, `--mm-phosphor`) and the scanline
  toggle is wired, but there is no picker UI. Dark theme only.
- **Console command history.** No up-arrow recall of previously sent lines.
- **Console scrollback and search.** Client buffer is capped at 1500 lines with
  no "load earlier" and no find-in-console.
- **Large-file editing.** The editor renders the whole document; the hub caps
  reads at 5 MB, but a file near that ceiling will feel slow. No virtualisation.
- **Bulk actions.** No multi-select power operations across instances.
- **Filter depth.** Matches node name, hostname and instance name — not
  `root_dir` or instance type.
- **Enrollment countdown.** The TTL is stated ("expires in 15 minutes") but not
  counted down live, and an expired token isn't visually marked.
- **Instance create validation.** `root_dir` is not checked for existence on the
  node before the record is created; a bad path surfaces later as a `502`.
- **Accessibility.** Dialogs handle Escape/Enter and focus their first field but
  have no focus trap or ARIA roles; the tree is not keyboard-navigable.
- **Automated tests.** `web/` has none. It was verified end-to-end by driving a
  headless browser against the hub with a stand-in agent, but that harness is
  not checked in.

## 5. Out of scope by design

- **Login/session UI.** The hub runs behind Authelia + WireGuard and the proxy
  authenticates the user — see [`UI_CONTEXT.md` §2](UI_CONTEXT.md#2-base-url-auth-cors).
- **Pagination.** Small-scale by design (§6.7); full lists are rendered.
- **Editing node `hostname` / `agent_version`.** Both are reported by the agent,
  not declared by the operator.
