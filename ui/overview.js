(() => {
  const H = window.Harness;
  const $ = (id) => document.getElementById(id);
  const node = () => context?.node?.id || "local";

  let context = null;
  let registry = { harnesses: [], default: "" };
  let harnesses = [];
  let selected = "";
  let cwd = "";
  let busy = false;

  const backend = (path, method = "GET", body) => H.backend(path, method, body);
  const harness = (id) => harnesses.find((entry) => entry.id === id) || registry.harnesses.find((entry) => entry.id === id) || { id, label: id };
  const storage = {
    async get(key) {
      try { return await window.orqella.call("storage.get", { key }); } catch { return null; }
    },
    async set(key, value) {
      try { await window.orqella.call("storage.set", { key, value }); } catch {}
    },
  };

  const feedback = (text, kind = "") => {
    const element = $("feedback");
    element.textContent = text || "";
    element.className = `feedback ${kind}`;
    element.hidden = !text;
  };
  const setState = (text, kind = "") => {
    $("state").textContent = text;
    $("state").className = `state ${kind}`;
  };
  const setBusy = (value) => {
    busy = value;
    const entry = harnesses.find((item) => item.id === selected) || {};
    const ready = entry.installed !== false && entry.enabled !== false;
    $("start").disabled = value || !ready;
    $("prompt").disabled = value;
    $("harness").disabled = value;
  };

  function updateNativeButton() {
    $("native").hidden = !selectedEntry().web?.mount;
  }

  function renderHarnessSelect() {
    const choices = registry.harnesses.filter((entry) => entry.enabled !== false);
    $("harness").innerHTML = choices.map((entry) => {
      const state = harnesses.find((item) => item.id === entry.id) || {};
      const suffix = state.installed === false ? " · not installed" : state.running ? " · live" : "";
      return `<option value="${H.escapeHtml(entry.id)}">${H.escapeHtml(entry.label + suffix)}</option>`;
    }).join("");
    $("harness").value = selected;
    updateNativeButton();
  }

  function selectedEntry() {
    return harnesses.find((entry) => entry.id === selected) || { id: selected, label: selected };
  }

  async function refreshState() {
    const entry = selectedEntry();
    setState(entry.installed === false ? "not installed" : entry.running ? "live" : "ready", entry.installed === false ? "bad" : entry.running ? "ok" : "");
    feedback(
      entry.installed === false
        ? entry.error || "Install this harness on the selected machine."
        : "",
      entry.installed === false ? "bad" : "",
    );
    await loadCwd();
    updateNativeButton();
    setBusy(false);
  }

  async function loadCwd() {
    if (cwd) {
      $("cwdValue").textContent = cwd;
      $("cwdValue").title = cwd;
      return;
    }
    const remembered = await storage.get("last_cwd");
    if (remembered?.path) {
      cwd = remembered.path;
      $("cwdValue").textContent = cwd;
      $("cwdValue").title = cwd;
      return;
    }
    $("cwdValue").textContent = "Choose a folder";
    $("cwdValue").title = "Workspace folder";
  }

  async function chooseCwd() {
    try {
      const result = await window.orqella.call("system.pick_directory", {
        node: node(),
        title: "Choose a workspace folder",
        start: cwd || "",
      });
      if (result?.path) {
        cwd = result.path;
        $("cwdValue").textContent = cwd;
        $("cwdValue").title = cwd;
        await storage.set("last_cwd", { path: cwd });
        return;
      }
      if (result?.cancelled) return;
    } catch {}
    // Older shells have no picker; hand off to the full workspace, which has
    // its own directory browser.
    await window.orqella.call("module.open", { path: `/?harness=${encodeURIComponent(selected)}` });
  }

  async function loadSessions(reset = false, includeHistory = false) {
    if (reset) $("sessionList").innerHTML = "";
    $("sessionMore").textContent = "Loading…";
    try {
      const result = await backend("/api/sessions?limit=12");
      const local = result.sessions || [];
      const rows = local.slice();
      if (includeHistory) {
        const targets = harnesses.filter((entry) => entry.kind === "acp" && entry.enabled !== false && entry.installed !== false);
        const found = await Promise.allSettled(targets.map((entry) => backend(`/api/sessions/discover?harness=${encodeURIComponent(entry.id)}`)));
        const indexed = new Set(local.map((session) => `${session.harness}:${session.harness_session_id}`));
        for (const item of found) {
          if (item.status !== "fulfilled") continue;
          for (const session of item.value.sessions || []) {
            const key = `${session.harness}:${session.session_id}`;
            if (indexed.has(key)) continue;
            indexed.add(key);
            rows.push({ ...session, discovered: true, updated_at: session.updated_at });
          }
        }
      }
      rows.sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
      const visible = rows.slice(0, 8);
      $("sessionList").innerHTML = visible.length
        ? visible.map((row) => {
            const attributes = row.discovered
              ? `data-history="1" data-harness="${H.escapeHtml(row.harness)}" data-harness-session="${H.escapeHtml(row.session_id)}" data-cwd="${H.escapeHtml(row.cwd || "")}" data-title="${H.escapeHtml(row.title || "")}"`
              : `data-session="${H.escapeHtml(row.id)}"`;
            const chip = row.discovered ? `<span class="chip">history</span>` : "";
            return `<button class="session-item" type="button" ${attributes}><b>${H.escapeHtml(row.title || "Untitled")}</b><small>${H.escapeHtml(harness(row.harness).label)}${row.updated_at ? " · " + H.escapeHtml(H.fmtWhen(row.updated_at)) : ""} ${chip}</small></button>`;
          }).join("")
        : `<div class="session-more">No sessions yet.</div>`;
      $("sessionList").querySelectorAll("[data-session], [data-history]").forEach((button) => {
        button.onclick = () => {
          if (button.dataset.history) {
            return openHistory(button.dataset.harness, button.dataset.harnessSession, button.dataset.cwd, button.dataset.title);
          }
          return openSession(button.dataset.session);
        };
      });
      $("sessionMore").textContent = rows.length > visible.length ? `${rows.length - visible.length} earlier sessions in the workspace` : "No earlier sessions";
    } catch (error) {
      $("sessionMore").textContent = error.message || "Could not load sessions";
    }
  }

  async function openSession(id) {
    await window.orqella.call("module.open", { path: `/?session=${encodeURIComponent(id)}` });
  }

  async function openHistory(harnessId, harnessSessionId, cwd, title) {
    try {
      $("sessionMore").textContent = "Opening session…";
      const adopted = await backend("/api/sessions/adopt", "POST", {
        harness: harnessId,
        session_id: harnessSessionId,
        cwd: cwd || "",
        title: title || "",
      });
      await window.orqella.call("module.open", { path: `/?session=${encodeURIComponent(adopted.id)}` });
    } catch (error) {
      feedback(error.message || "Could not open the session", "bad");
    }
  }

  async function openNative() {
    const entry = selectedEntry();
    const web = entry.web;
    if (!web?.mount) return feedback("This harness has no web workspace.", "bad");
    try {
      let status = await backend(`/api/native/status?harness=${encodeURIComponent(entry.id)}`);
      if (!status.available) throw new Error(status.error || "No web workspace is configured.");
      if (!status.running) status = await backend("/api/native/start", "POST", { harness: entry.id });
      if (!status.running && status.error) throw new Error(status.error);
      let token = "";
      if (web.handoff) {
        const pair = await backend("/api/native/pair", "POST", { harness: entry.id });
        token = pair?.auth_token || "";
      }
      const path = token ? `/?auth_token=${encodeURIComponent(token)}` : "/";
      await window.orqella.call("app.open", { mount: web.mount, node: node(), path });
    } catch (error) {
      feedback(error.message || "Could not open the native workspace", "bad");
    }
  }

  async function openWorkspace() {
    const query = `/?harness=${encodeURIComponent(selected)}${cwd ? `&cwd=${encodeURIComponent(cwd)}` : ""}`;
    await window.orqella.call("module.open", { path: query });
  }

  async function startSession(event) {
    event.preventDefault();
    if (busy) return;
    const prompt = $("prompt").value.trim();
    if (!prompt) {
      $("prompt").focus();
      return feedback("Write a prompt first.", "bad");
    }
    if (!cwd) return feedback("Choose a workspace folder first.", "bad");
    setBusy(true);
    feedback("Starting a session…", "ok");
    try {
      const created = await backend("/api/sessions", "POST", { harness: selected, cwd });
      await backend(`/api/sessions/${encodeURIComponent(created.id)}/prompt`, "POST", { text: prompt });
      $("prompt").value = "";
      await window.orqella.call("module.open", { path: `/?session=${encodeURIComponent(created.id)}` });
    } catch (error) {
      feedback(error.message || "Could not start the session", "bad");
    } finally {
      setBusy(false);
    }
  }

  function closeSessions() {
    $("sessionMenu").hidden = true;
    $("sessionsToggle").setAttribute("aria-expanded", "false");
    $("sessionsChevron").textContent = "▾";
  }

  async function toggleSessions() {
    const menu = $("sessionMenu");
    if (!menu.hidden) return closeSessions();
    menu.hidden = false;
    $("sessionsToggle").setAttribute("aria-expanded", "true");
    $("sessionsChevron").textContent = "▴";
    await loadSessions(true, true);
  }

  function wire() {
    $("harness").onchange = async () => {
      selected = $("harness").value;
      await storage.set("overview_harness", selected);
      cwd = "";
      setBusy(false);
      setState("checking");
      await refreshState();
      await loadSessions(true);
    };
    $("open").onclick = () => openWorkspace();
    $("native").onclick = () => openNative();
    $("chooseCwd").onclick = () => chooseCwd();
    $("launch").onsubmit = startSession;
    $("prompt").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        $("launch").requestSubmit();
      }
    });
    $("sessionsToggle").onclick = (event) => { event.stopPropagation(); toggleSessions(); };
    $("sessionMenu").addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", () => closeSessions());
  }

  window.orqella.ready(async (ctx) => {
    context = ctx;
    if (ctx.config?.show_overview_button === false) $("open").hidden = true;
    wire();
    try {
      registry = await backend("/api/registry");
      const state = await backend("/api/harnesses");
      harnesses = state.harnesses || [];
      const remembered = await storage.get("overview_harness");
      selected = remembered?.id || registry.default || ctx.config?.default_harness || registry.harnesses[0]?.id || "opencode";
      if (!registry.harnesses.some((entry) => entry.id === selected && entry.enabled !== false)) {
        selected = registry.harnesses.find((entry) => entry.enabled !== false)?.id || "opencode";
      }
      renderHarnessSelect();
      await refreshState();
      await loadSessions(true);
    } catch (error) {
      feedback(error.message || "Agent Harness is unavailable", "bad");
      setState("error", "bad");
      setBusy(false);
    }
  });
})();
