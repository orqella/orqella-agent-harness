# OpenCode module

An Orqella module that opens the local OpenCode service as an app surface and
offers a small overview launcher.

- `backend/server.py` reads OpenCode's managed-service credential, forwards to
  its HTTP API, and pairs the app surface. It never owns OpenCode credentials
  beyond that file.
- Use `ORQELLA_HOST_HOME` for host paths; `HOME` is the module home.
- `GET /api/service` is read-only. `POST /api/service/start` is the explicit
  start action; do not start OpenCode just because the overview mounted unless
  the `auto_start_service` setting is enabled.
- Surface `ServiceCredentialInvalid` separately from a missing service. Do not
  swallow malformed `service.json`.
- The paired auth token is passed in the app URL because that is the current
  OpenCode handoff contract. Treat it as sensitive and prefer a one-time
  handoff if OpenCode adds one.
- `base_url` must be loopback; never send the local service password elsewhere.
- Strip `ORQELLA_MODULE_TOKEN`/`ORQELLA_MODULE_CONFIG` from child processes.
- Load `/assets/theme.css` and `/assets/module.css`; use core primitives.
- Add backend tests under `backend/test_server.py`; run it and
  `orqella module check .` before publishing.
