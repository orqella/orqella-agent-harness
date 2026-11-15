(() => {
  const $ = (id) => document.getElementById(id);
  let context = { node: { id: "local", name: "local" }, config: {} };
  let status = null;
  let models = [];
  let sessionsCursor = null;
  let sessionsDone = false;
  let sessionsLoading = false;

  const feedback = (text, kind = "") => {
    $("feedback").textContent = text || "";
    $("feedback").className = `feedback ${kind}`;
  };
  const setBusy = (busy) => {
    $("start").disabled = busy || !status?.ok || !models.length;
    $("prompt").disabled = busy || !status?.ok;
    $("model").disabled = busy || !status?.ok || !models.length;
  };
  const base64url = (value) => btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const sessionPath = (id) => {
    const origin = new URL(status?.url || location.href).origin;
    return `/server/${base64url(origin)}/session/${encodeURIComponent(id)}`;
  };
  const node = () => context.node?.id || "local";
  const backend = (path, method = "GET", body) => window.orqella.call("backend.request", {
    method,
    path,
    ...(body !== undefined ? { body } : {}),
  });

  async function openWithPair(path) {
    const pair = await backend("/api/pair", "POST", {});
    if (!pair?.auth_token) throw new Error("Could not authenticate with OpenCode");
    const target = path || "/";
    const separator = target.includes("?") ? "&" : "?";
    return window.orqella.call("app.open", {
      mount: "opencode",
      node: node(),
      path: `${target}${separator}auth_token=${encodeURIComponent(pair.auth_token)}`,
    });
  }

  async function openSession(id) {
    try {
      await openWithPair(sessionPath(id));
      closeSessions();
      feedback("Opening session…", "ok");
    } catch (error) {
      feedback(error.message || "Could not open session", "bad");
    }
  }

  async function loadModels() {
    try {
      const data = await backend("/api/models");
      const providers = new Map((data.providers || []).map((provider) => [provider.id, provider.name]));
      models = (data.models || [])
        .filter((model) => model.enabled !== false && (model.status === undefined || model.status === "active"))
        .sort((a, b) => {
          const pa = providers.get(a.providerID) || a.providerID || "";
          const pb = providers.get(b.providerID) || b.providerID || "";
          return pa.localeCompare(pb) || String(a.name || a.id).localeCompare(String(b.name || b.id));
        });
      const select = $("model");
      select.innerHTML = models.length
        ? models.map((model, index) => `<option value="${index}">${escapeHtml(providers.get(model.providerID) || model.providerID || "Model")} · ${escapeHtml(model.name || model.id)}</option>`).join("")
        : `<option>No models</option>`;
      let selected = -1;
      try {
        const remembered = await window.orqella.call("storage.get", { key: "overview_model" });
        const modelID = remembered?.modelID || remembered?.id;
        if (remembered?.providerID && modelID) {
          selected = models.findIndex((model) => model.providerID === remembered.providerID
            && (model.modelID || model.id) === modelID);
        }
      } catch {}
      if (selected < 0 && data.default) {
        selected = models.findIndex((model) => model.providerID === data.default.providerID
          && (model.modelID || model.id) === (data.default.modelID || data.default.id));
      }
      if (selected >= 0) select.value = String(selected);
      setBusy(false);
    } catch (error) {
      setBusy(false);
      feedback(error.message || "Models unavailable", "bad");
    }
  }

  async function loadSessions(reset = false) {
    if (sessionsLoading || (sessionsDone && !reset)) return;
    if (reset) {
      sessionsCursor = null;
      sessionsDone = false;
      $("sessionList").innerHTML = "";
    }
    sessionsLoading = true;
    $("sessionMore").textContent = "Loading…";
    try {
      const query = new URLSearchParams({ limit: "25" });
      if (sessionsCursor) query.set("cursor", sessionsCursor);
      const result = await backend(`/api/sessions?${query}`);
      const sessions = result?.data || [];
      if (!sessions.length && !sessionsCursor) {
        $("sessionList").innerHTML = `<div class="session-more">No sessions yet.</div>`;
      } else {
        const rows = sessions.map((session) => {
          const updated = session.time?.updated || session.time?.created;
          const when = updated ? new Date(updated).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
          const detail = [when, session.agent, session.model?.id].filter(Boolean).join(" · ");
          return `<button class="session-item" type="button" data-session="${escapeHtml(session.id)}"><b>${escapeHtml(session.title || "Untitled session")}</b><small>${escapeHtml(detail)}</small></button>`;
        }).join("");
        $("sessionList").insertAdjacentHTML("beforeend", rows);
      }
      sessionsCursor = result?.cursor?.next || null;
      sessionsDone = !sessionsCursor;
      $("sessionMore").textContent = sessionsDone ? "No earlier sessions" : "Scroll for earlier sessions";
    } catch (error) {
      $("sessionMore").textContent = error.message || "Could not load sessions";
    } finally {
      sessionsLoading = false;
    }
  }

  function closeSessions() {
    $("sessionMenu").hidden = true;
    $("sessionsToggle").setAttribute("aria-expanded", "false");
    $("sessionsChevron").textContent = "▾";
  }

  function fitSessionMenu() {
    const menu = $("sessionMenu");
    if (menu.hidden) return;
    const toggle = $("sessionsToggle");
    const rect = toggle.getBoundingClientRect();
    const above = Math.floor(rect.top - 8);
    const below = Math.floor(window.innerHeight - rect.bottom - 8);
    const openBelow = above < 96 && below > above;
    if (openBelow) {
      menu.style.top = "calc(100% + 5px)";
      menu.style.bottom = "auto";
      menu.style.maxHeight = `${Math.max(48, Math.min(180, below))}px`;
    } else {
      menu.style.top = "auto";
      menu.style.bottom = "calc(100% + 5px)";
      menu.style.maxHeight = `${Math.max(48, Math.min(180, above))}px`;
    }
  }

  async function toggleSessions() {
    const menu = $("sessionMenu");
    if (!menu.hidden) return closeSessions();
    menu.hidden = false;
    $("sessionsToggle").setAttribute("aria-expanded", "true");
    $("sessionsChevron").textContent = "▴";
    fitSessionMenu();
    await loadSessions(true);
    fitSessionMenu();
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[character]));
  }

  $("sessionsToggle").addEventListener("click", (event) => { event.stopPropagation(); toggleSessions(); });
  window.addEventListener("resize", fitSessionMenu);
  $("sessionMenu").addEventListener("click", (event) => {
    event.stopPropagation();
    const item = event.target.closest("[data-session]");
    if (item) openSession(item.dataset.session);
  });
  $("sessionMenu").addEventListener("scroll", () => {
    const menu = $("sessionMenu");
    if (menu.scrollTop + menu.clientHeight >= menu.scrollHeight - 24) loadSessions(false);
  });
  document.addEventListener("click", (event) => {
    if (!$("sessionMenu").contains(event.target) && event.target !== $("sessionsToggle")) closeSessions();
  });

  $("open").addEventListener("click", async () => {
    try { await openWithPair(""); } catch (error) { feedback(error.message || "Could not open workspace", "bad"); }
  });

  $("launch").addEventListener("submit", async (event) => {
    event.preventDefault();
    const prompt = $("prompt").value.trim();
    if (!prompt) { $("prompt").focus(); return feedback("Write a prompt first.", "bad"); }
    const model = models[Number($("model").value)];
    if (!model) return feedback("Choose a model first.", "bad");
    setBusy(true);
    feedback("Starting a new session…", "ok");
    try {
      const result = await backend("/api/launch", "POST", {
        prompt,
        model: {
          id: model.modelID || model.id,
          providerID: model.providerID,
        },
      });
      const id = result?.session?.id;
      if (!id) throw new Error("OpenCode did not return a session");
      $("prompt").value = "";
      await openSession(id);
    } catch (error) {
      feedback(error.message || "Could not start session", "bad");
      setBusy(false);
    }
  });

  $("model").addEventListener("change", async () => {
    const model = models[Number($("model").value)];
    if (!model) return;
    try {
      await window.orqella.call("storage.set", {
        key: "overview_model",
        value: { providerID: model.providerID, modelID: model.modelID || model.id },
      });
    } catch {}
  });

  $("prompt").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) $("launch").requestSubmit();
  });

  window.orqella.ready(async (ctx) => {
    context = ctx;
    if (ctx.config?.show_overview_button === false) $("open").hidden = true;
    feedback("Checking the local service…");
    try {
      const service = await backend("/api/service");
      if (!service.running && ctx.config?.auto_start_service === true) {
        await backend("/api/service/start", "POST", {});
      }
      status = await window.orqella.call("proxy.status", { mount: "opencode", node: node() });
      $("state").textContent = status.ok ? "live" : "offline";
      $("state").classList.toggle("ok", !!status.ok);
      if (!status.ok) {
        feedback(status.error || "OpenCode is not running. Use ↗ or start a session to launch it.", "bad");
        setBusy(false);
        return;
      }
      await loadModels();
      feedback(context.config?.show_connection_details === false ? "Ready." : "Ready · " + (status.url || ""));
    } catch (error) {
      $("state").textContent = "offline";
      $("state").classList.remove("ok");
      feedback(error.message || "OpenCode is unavailable", "bad");
      setBusy(false);
    }
  });
})();
