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
orqella module install https://github.com/orqella/orqella-opencode
```

Requirements:

- OpenCode is installed so the sidecar can reach or start the shared service;
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

If an OpenCode service is already running, the sidecar only forwards to it. If
it is not running, the sidecar starts the shared background service with
`opencode service start` and then forwards. That command is idempotent, so it
never starts a second server on the same port. Setting `base_url` to a
non-default URL disables the automatic start; the configured service is then
treated as externally managed.

The module works on Linux, macOS, and Windows when OpenCode is installed. The
sidecar uses Python 3.9+ (with the `python`/`py -3` fallback) and has no
OS-specific UI.

## Steam Deck / immutable Linux

The tested SteamOS setup, including config import from another machine, the
proxy mount, and known product gaps, is documented in the Orqella core
repository:
[`docs/STEAMDECK.md`](https://github.com/orqella/core/blob/main/docs/STEAMDECK.md).
A separate `opencode serve --service` unit is optional: the module starts the
shared service on demand. If you do run a unit, use `--service`; the systemd
`opencode.service` and the module must not start two servers on the same port.
