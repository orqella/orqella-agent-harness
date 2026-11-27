/* Shared helpers for the Agent Harness module surfaces. */
(() => {
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));

  const backend = (path, method = "GET", body) => window.orqella.call("backend.request", {
    method,
    path,
    ...(body !== undefined ? { body } : {}),
  });

  const fmtWhen = (value) => {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const now = Date.now();
    const diff = now - date.getTime();
    if (diff < 60_000) return "just now";
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };

  // The GFM renderer is bundled in ui/vendor/markdown.js. It sanitizes raw
  // HTML with DOMPurify and preserves single line breaks in chat messages.
  const markdown = (value) => window.OrqellaMarkdown
    ? window.OrqellaMarkdown.render(value)
    : `<p>${escapeHtml(value)}</p>`;

  const stripAnsi = (value) => String(value ?? "")
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");

  const diff = (oldText, newText) => {
    const before = String(oldText ?? "").split("\n");
    const after = String(newText ?? "").split("\n");
    if (before.length + after.length > 800) {
      return `<div class="diff-simple"><div class="diff-side"><b>Before</b><pre>${escapeHtml(before.join("\n"))}</pre></div><div class="diff-side"><b>After</b><pre>${escapeHtml(after.join("\n"))}</pre></div></div>`;
    }
    const rows = [];
    let start = 0;
    while (start < before.length && start < after.length && before[start] === after[start]) start += 1;
    let endBefore = before.length - 1;
    let endAfter = after.length - 1;
    while (endBefore >= start && endAfter >= start && before[endBefore] === after[endAfter]) {
      endBefore -= 1;
      endAfter -= 1;
    }
    if (start > 0) rows.push(`<span class="diff-line">${escapeHtml(before.slice(0, start).join("\n"))}</span>`);
    for (const line of before.slice(start, endBefore + 1)) rows.push(`<span class="diff-remove">- ${escapeHtml(line)}</span>`);
    for (const line of after.slice(start, endAfter + 1)) rows.push(`<span class="diff-add">+ ${escapeHtml(line)}</span>`);
    if (endBefore + 1 < before.length) rows.push(`<span class="diff-line">${escapeHtml(before.slice(endBefore + 1).join("\n"))}</span>`);
    return `<pre class="diff-block">${rows.join("\n")}</pre>`;
  };

  const wsUrl = (context, sessionId, since = 0) => {
    const moduleId = context?.module?.id || "agent-harness";
    const parts = location.pathname.split("/").filter(Boolean);
    const index = parts.lastIndexOf(moduleId);
    const base = index >= 0 ? `/${parts.slice(0, index + 1).join("/")}/` : "/";
    const url = new URL(`${base}ws/sessions/${encodeURIComponent(sessionId)}`, location.origin);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("since", String(since || 0));
    return url.toString();
  };

  const roleLabel = (role) => ({ agent: "Agent", user: "You", thought: "Thinking" }[role] || role);

  window.Harness = {
    backend,
    escapeHtml,
    fmtWhen,
    markdown,
    stripAnsi,
    diff,
    wsUrl,
    roleLabel,
  };
})();
