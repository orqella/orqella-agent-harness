# Bundled markdown renderer

`markdown.js` is a self-contained browser bundle used by Agent Harness.

- **marked 18.3.0** — MIT — https://github.com/markedjs/marked
  See `marked-LICENSE.txt`.
- **DOMPurify 3.5.0** — MPL-2.0 OR Apache-2.0 — https://github.com/cure53/DOMPurify
  This build uses the Apache-2.0 option. See `DOMPurify-LICENSE.txt`.

The bundle exposes `window.OrqellaMarkdown.render(text)` and sanitizes the
rendered HTML with DOMPurify. Regenerate it with esbuild from an entry that
imports `marked` and `dompurify` and sets `window.OrqellaMarkdown`; keep this
notice with the generated file.
