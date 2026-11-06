# OpenCode module

This is a normal Orqella module, not part of the daemon. It demonstrates:

- a sidecar backend;
- module-declared settings;
- a page and an overview card;
- a generic app handoff using `app.open` with an authenticated `path` (the auth token is passed through the URL and removed by OpenCode);
- deep links to a session through `?session=<id>`, used by Orqella's
  `conversation.open` hook. The module pairs, then opens OpenCode's
  `/server/<key>/session/<id>` route using Orqella's `{server_key}` app-path
  placeholder.

Install it with:

```bash
orqella module install modules/opencode
```

Requirements:

- the OpenCode service is running locally;
- its service credential is at `~/.config/opencode/service.json` (the module
  also checks the XDG state directory and the macOS/Windows config locations);
- the module's `base_url` setting points at the service. Leave it at the
  default `http://127.0.0.1:49374` to use the service port from `service.json`;
- an `opencode` proxy mount is configured for the app surface:

```toml
[[proxy]]
id = "opencode"
title = "OpenCode"
base_url = "http://127.0.0.1:49374"
host = "opencode.orqella.localhost"
websocket = true
```

The sidecar runs unsandboxed because it reads OpenCode's local service credential
and talks to the managed service over loopback. It never needs an
OpenCode-specific host API. `backend.read_paths` documents the credential
locations.

The module works on Linux, macOS, and Windows when OpenCode is installed and
its managed service is running. The sidecar uses Python 3.9+ (with the
`python`/`py -3` fallback) and has no OS-specific UI.
