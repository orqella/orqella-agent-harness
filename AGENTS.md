# Agent Harness module

One Orqella module that hosts coding-agent harnesses. OpenCode, Claude Code,
Codex, Gemini, and other ACP agents all render in the same conversation UI;
arbitrary commands can run as one-shot `exec` harnesses. OpenCode additionally
keeps a native-workspace action for features the module does not mirror.

## Layout

- `backend/server.py` — loopback HTTP API and WebSocket event stream.
- `backend/registry.py` — built-in harness definitions, validation, probing.
- `backend/sessions.py` — session index, transcripts, normalized events, the
  ACP host implementation (permissions, filesystem, terminals), and the `exec`
  driver.
- `backend/acp.py` — ACP v1 client (JSON-RPC over stdio), process lifecycle, and
  terminal management.
- `backend/opencode.py` — the OpenCode native-workspace helper (service URL and
  pairing token only). OpenCode sessions run through the ACP driver.
- `ui/index.html` + `app.js` — conversation workspace.
- `ui/overview.html` + `overview.js` — harness switcher, prompt launcher, recent
  sessions.
- `ui/settings.html` + `settings.js` — agent registry editor, embedded in Settings → Agents.

## Contracts

- Use `ORQELLA_HOST_HOME` for host paths; `HOME` is the module home. Strip
  `ORQELLA_*` variables from every child process.
- Harnesses use their own login and configuration. The module never stores API
  keys or service passwords. OpenCode's pairing token is a URL handoff and is
  treated as sensitive.
- ACP filesystem and terminal delegation is opt-in per harness in settings.
  Paths are confined to the session root set, terminal environments are
  sanitized, output is capped, and every command and write is appended to
  `data/audit.jsonl`.
- Discovery is read-only. `POST /api/opencode/service/start` and
  `POST /api/sessions` are explicit actions.
- `GET /api/opencode/service` and `GET /api/harnesses` must never start a
  process.
- Module WebSocket URLs are relative to the page so the same code works on a
  remote node (`/n/<node>/m/agent-harness/ws/...`).
- Overview cards open the full workspace through the `module.open` host API.
- Existing harness history is discovered through ACP `session/list` and adopted
  on demand. Adopting registers the local row before `session/load` so replayed
  history has a transcript to land in. Never scan harness-specific storage. The
  rail keeps discovered history under a collapsed section and surfaces a history
  session only while it is actually running.
- Delete calls ACP `session/delete` when advertised, removes the local record and
  transcript, and records a tombstone in `data/hidden_sessions.json` so a cached
  `session/list` cannot resurrect the conversation. Deleting history directly
  uses the same tombstone/native-delete path.
- Thoughts/tools visibility is a client-side view toggle; do not remove events
  from the transcript for it.
- Streaming chunks are coalesced before markdown is re-rendered. Rebuilding a
  whole message per delta is quadratic DOM work and can keep a web process at a
  full core; keep the scheduled render path.
- The composer Hotkeys panel is the shortcut reference. Every row is a button
  that performs its action, so touch clients are not shown keys they cannot use.
- Historical `status` events are not authoritative. Skip them during transcript
  replay; the session row/WS `ready` event and native activity hint drive the
  header. For managed-service sessions the live session times override the
  stored status, and `idle`/`working` must mean the same thing in the rail and
  header.
- Opening a session may refresh history from the harness when its activity is
  newer than the stored transcript. The refresh endpoint preserves a backup and
  restores it if the harness does not replay.

## Tests

```bash
python3 -m unittest discover -s backend -p 'test_*.py'
orqella module check .
```

Add backend tests under `backend/test_*.py`; the mock ACP agent lives at
`backend/tests/mock_acp_agent.py`. Run both commands before publishing.
