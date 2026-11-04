(() => {
  const $ = (id) => document.getElementById(id);
  let context = { node: { id: "local", name: "local" }, config: {} };
  let busy = false;
  let opened = false;

  const backend = (path, method = "GET", body) => window.orqella.call("backend.request", {
    method,
    path,
    ...(body !== undefined ? { body } : {}),
  });

  const showError = (title, detail) => {
    $("title").textContent = title;
    $("detail").textContent = detail;
    $("actions").hidden = false;
  };

  const openWithPair = async (path) => {
    const pair = await backend("/api/pair", "POST", {});
    if (!pair?.auth_token) throw new Error("Could not authenticate with OpenCode");
    const target = path || "/";
    const separator = target.includes("?") ? "&" : "?";
    return window.orqella.call("app.open", {
      mount: "opencode",
      node: context.node?.id || "local",
      path: `${target}${separator}auth_token=${encodeURIComponent(pair.auth_token)}`,
    });
  };

  const openWorkspace = async () => {
    if (busy) return;
    busy = true;
    $("actions").hidden = true;
    $("title").textContent = "Opening workspace…";
    $("detail").textContent = `Connecting to ${context.node?.name || "this machine"}.`;
    try {
      const status = await window.orqella.call("proxy.status", { mount: "opencode", node: context.node?.id || "local" });
      if (!status.ok) {
        showError("OpenCode is not reachable", status.error || "No service is listening.");
        return;
      }
      opened = true;
      await openWithPair("");
    } catch (error) {
      showError("Could not open OpenCode", error.message || "The OpenCode service is unavailable.");
    } finally {
      busy = false;
    }
  };

  $("retry").addEventListener("click", openWorkspace);
  window.orqella.ready((ctx) => {
    context = ctx;
    openWorkspace();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !opened) return;
    opened = false;
    showError("Workspace closed", "Open it again when you are ready.");
  });
})();
