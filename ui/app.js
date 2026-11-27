(() => {
  const H = window.Harness;
  const $ = (id) => document.getElementById(id);
  const node = () => context?.node?.id || "local";
  const params = new URLSearchParams(location.search);
  const wantedSession = params.get("session") || "";
  const wantedHarness = params.get("harness") || "";
  const wantedCwd = params.get("cwd") || "";

  let context = null;
  let registry = { harnesses: [], default: "" };
  let harnesses = [];
  let sessions = [];
  let discovered = [];
  let discoverToken = 0;
  let current = null;
  let socket = null;
  let reconnectTimer = null;
  let lastSeq = 0;
  let image = null;
  let blocks = new Map();
  let toolEls = new Map();
  let planEntries = [];
  let streamTurn = 0;
  let usage = null;
  let activityTimer = null;
  let refreshing = false;
  let historyLoading = false;
  const commandCache = new Map();
  let commandItems = [];
  let commandIndex = 0;
  let commandLoading = false;
  let bulkExpanded = null;
  let hideThoughts = false;
  let hideTools = false;
  let terminalPolls = new Map();
  let pendingApprovals = new Map();
  let filter = "";
  let statusValue = "idle";
  let historyOpen = false;
  const inactiveSince = new Map();

  const storage = {
    async get(key) {
      try { return await window.orqella.call("storage.get", { key }); } catch { return null; }
    },
    async set(key, value) {
      try { await window.orqella.call("storage.set", { key, value }); } catch {}
    },
  };

  const harness = (id) => harnesses.find((entry) => entry.id === id) || registry.harnesses.find((entry) => entry.id === id) || { id, label: id };
  const groupedHarnesses = () => registry.harnesses.filter((entry) => entry.enabled !== false);

  const displayStatus = (session) => {
    if (!session) return "idle";
    const status = session.status === "detached" ? "idle" : session.status || "idle";
    if (status === "waiting" || status === "failed") return status;
    if (session.native_active) return "working";
    return status === "starting" ? "working" : status;
  };

  const modelName = (model) => model?.label || model?.id || "";
  const modelDetail = (model) => {
    if (!model) return "";
    const base = model.provider ? `${model.provider}/${model.id}` : model.id || model.label || "";
    return model.variant ? `${base}#${model.variant}` : base;
  };
  const modelTitle = (model) => {
    const name = modelName(model);
    const detail = modelDetail(model);
    return detail && detail !== name ? `${name} · ${detail}` : name;
  };
  const renderModel = (session = current) => {
    const chip = $("modelChip");
    const model = session?.model;
    if (!model || !modelName(model)) {
      chip.hidden = true;
      chip.textContent = "";
      chip.removeAttribute("title");
      return;
    }
    chip.hidden = false;
    chip.textContent = `Model · ${modelName(model)}`;
    chip.title = `Current model: ${modelTitle(model)}`;
  };

  const setStatus = (status) => {
    statusValue = status || "idle";
    const chip = $("statusChip");
    chip.textContent = statusValue;
    const kind = statusValue === "working" || statusValue === "waiting"
      ? "warn"
      : statusValue === "failed" || statusValue === "detached" ? "bad" : "ok";
    chip.className = `chip ${kind}`;
    const busy = statusValue === "working" || statusValue === "waiting";
    const canSteer = busy && Boolean(current?.native_active);
    $("stop").hidden = !busy;
    $("send").disabled = (!canSteer && busy) || !current || statusValue === "detached";
    $("prompt").disabled = !current || statusValue === "detached";
    $("resume").hidden = !current || statusValue !== "detached";
  };

  const setTitle = (title) => {
    $("title").textContent = title || "Workspace";
    document.title = title ? `${title} · Agent Harness` : "Agent Harness";
  };

  function openRail() {
    $("rail").classList.add("open");
    $("railBackdrop").hidden = false;
    $("sessionsButton").setAttribute("aria-expanded", "true");
  }

  function closeRail() {
    $("rail").classList.remove("open");
    $("railBackdrop").hidden = true;
    $("sessionsButton").setAttribute("aria-expanded", "false");
  }

  function toggleRail() {
    if ($("rail").classList.contains("open")) closeRail();
    else openRail();
  }

  const showEmpty = (message) => {
    current = null;
    $("empty").hidden = false;
    updateNativeButton();
    $("composer").hidden = true;
    $("transcript").innerHTML = "";
    $("railNote").textContent = message || "";
    setTitle("Workspace");
    setStatus("idle");
    renderModel(null);
  };

  const errorText = (error) => error?.message || String(error || "Something went wrong");

  const toast = (message, kind = "bad") => {
    const card = document.createElement("article");
    card.className = `card flat toast ${kind}`;
    card.textContent = message;
    $("transcript").appendChild(card);
    card.scrollIntoView({ block: "end" });
    setTimeout(() => card.remove(), kind === "ok" ? 4000 : 12000);
  };

  // -- sessions ----------------------------------------------------------

  async function loadAll() {
    registry = await H.backend("/api/registry");
    const state = await H.backend("/api/harnesses");
    harnesses = state.harnesses || [];
    await loadSessions();
    renderFilter();
    updateNativeButton();
    void loadDiscovered();
  }

  async function loadSessions() {
    const result = await H.backend("/api/sessions?limit=100");
    sessions = result.sessions || [];
    if (current) {
      const updated = sessions.find((session) => session.id === current.id);
      if (updated) current = updated;
    }
    renderRail();
    void pollActivity();
  }

  async function pollActivity() {
    try {
      const data = await H.backend("/api/native/activity");
      const map = data.sessions || {};
      let changed = false;
      const apply = (session, id) => {
        if (!session || !id) return;
        const hint = map[id];
        if (!hint) return;
        const active = Boolean(hint.active);
        let status = session.status;
        if (session.status !== "waiting" && session.status !== "failed") {
          if (active) {
            inactiveSince.delete(id);
            status = "working";
          } else if (session.status === "working") {
            // Activity includes a short cache, and a prompt sent locally can
            // arrive before the service records it as busy. Require a few
            // inactive polls before overriding a working session; a real turn
            // end also arrives over the session WebSocket.
            const since = inactiveSince.get(id) || Date.now();
            inactiveSince.set(id, since);
            if (Date.now() - since > 12000) {
              inactiveSince.delete(id);
              status = "idle";
            }
          } else {
            inactiveSince.delete(id);
            status = "idle";
          }
        }
        if (session.native_active !== active || session.status !== status) {
          session.native_active = active;
          session.status = status;
          changed = true;
        }
      };
      for (const session of sessions) apply(session, session.harness_session_id);
      for (const session of discovered) apply(session, session.session_id);
      if (current) apply(current, current.harness_session_id);
      if (changed) {
        renderRail();
        if (current) {
          setStatus(displayStatus(current));
          $("sessionInfo").innerHTML = renderSessionInfo(current);
        }
      }
    } catch {}
  }

  function startActivityPoll() {
    if (activityTimer) return;
    pollActivity();
    activityTimer = setInterval(() => {
      if (document.visibilityState !== "hidden") pollActivity();
    }, 5000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") pollActivity();
    });
  }

  async function loadDiscovered() {
    const token = ++discoverToken;
    discovered = [];
    const targets = harnesses.filter((entry) => entry.kind === "acp" && entry.enabled !== false && entry.installed !== false);
    if (!targets.length) {
      renderRail();
      return;
    }
    await Promise.allSettled(targets.map(async (entry) => {
      try {
        const result = await H.backend(`/api/sessions/discover?harness=${encodeURIComponent(entry.id)}`);
        if (token !== discoverToken) return;
        const known = new Set(discovered.map((session) => `${session.harness}:${session.session_id}`));
        for (const session of result.sessions || []) {
          const key = `${session.harness}:${session.session_id}`;
          if (known.has(key)) continue;
          known.add(key);
          discovered.push(session);
        }
        renderRail();
      } catch {}
    }));
  }

  function mergedSessions() {
    const indexed = new Set(sessions.map((session) => `${session.harness}:${session.harness_session_id}`));
    return [...sessions, ...discovered.filter((session) => !indexed.has(`${session.harness}:${session.session_id}`))]
      .sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  }

  function nativeEntry() {
    if (current) return harnesses.find((entry) => entry.id === current.harness) || null;
    return harnesses.find((entry) => entry.id === registry.default) || null;
  }

  function updateNativeButton() {
    $("railNative").hidden = !nativeEntry()?.web?.mount;
  }

  function applyView() {
    const transcript = $("transcript");
    transcript.classList.toggle("hide-thoughts", hideThoughts);
    transcript.classList.toggle("hide-tools", hideTools);
    for (const [id, hidden, label] of [
      ["toggleThoughts", hideThoughts, "thoughts"],
      ["toggleTools", hideTools, "tool calls"],
    ]) {
      const button = $(id);
      button.classList.toggle("off", hidden);
      button.setAttribute("aria-pressed", String(hidden));
      button.title = hidden ? `Show ${label}` : `Hide ${label}`;
    }
  }

  async function saveView() {
    await storage.set("view", { thoughts: hideThoughts, tools: hideTools });
  }

  function collapseOptions() {
    $("composerTools").classList.remove("expanded");
    $("optionsToggle").setAttribute("aria-expanded", "false");
  }

  function closeOverlays() {
    if (!$("deleteModal").hidden) {
      finishDelete(false);
      return;
    }
    closeModal("newModal");
    closeModal("dirModal");
    closeCommandMenu();
    closeRail();
    collapseOptions();
  }

  async function refreshAll() {
    try {
      await loadAll();
      if (current) {
        const updated = sessions.find((session) => session.id === current.id);
        if (updated) await selectSession(updated);
      }
    } catch (error) {
      toast(errorText(error));
    }
  }

  function renderFilter() {
    const select = $("harnessFilter");
    const enabled = groupedHarnesses();
    select.innerHTML = `<option value="">All harnesses</option>` + enabled.map((entry) => `<option value="${H.escapeHtml(entry.id)}">${H.escapeHtml(entry.label)}</option>`).join("");
    select.value = filter;
    select.onchange = () => {
      filter = select.value;
      renderRail();
    };
  }

  function sessionRow(session) {
    const info = harness(session.harness);
    const discoveredRow = Boolean(session.discovered);
    const active = current && (discoveredRow
      ? current.harness === session.harness && current.harness_session_id === session.session_id
      : current.id === session.id);
    const status = displayStatus(session);
    const chip = discoveredRow && !session.native_active ? "history" : status;
    const chipClass = chip === "working" || chip === "waiting"
      ? "warn"
      : chip === "failed" || chip === "detached" ? "bad" : chip === "history" ? "" : "ok";
    const attributes = discoveredRow
      ? `data-history="1" data-harness="${H.escapeHtml(session.harness)}" data-harness-session="${H.escapeHtml(session.session_id)}" data-cwd="${H.escapeHtml(session.cwd || "")}" data-title="${H.escapeHtml(session.title || "")}"`
      : `data-session="${H.escapeHtml(session.id)}"`;
    const title = H.escapeHtml(session.title || "Untitled");
    const remove = discoveredRow
      ? `data-delete-harness="${H.escapeHtml(session.harness)}" data-delete-session="${H.escapeHtml(session.session_id)}" data-delete-title="${title}"`
      : `data-delete-local="${H.escapeHtml(session.id)}" data-delete-title="${title}"`;
    return `<div class="session-row-wrap">
      <button type="button" class="session-row${active ? " active" : ""}" ${attributes} title="${title}">
        <span class="session-row-top"><b>${title}</b><span class="chip ${chipClass}">${H.escapeHtml(chip)}</span></span>
        <span class="session-row-bottom"><span>${H.escapeHtml(info.label)}</span><span>${H.escapeHtml(H.fmtWhen(session.updated_at))}</span></span>
        <span class="session-row-cwd mono">${H.escapeHtml(session.cwd || "")}</span>
      </button>
      <button type="button" class="session-delete" ${remove} title="Delete conversation" aria-label="Delete conversation">×</button>
    </div>`;
  }

  function renderRail() {
    const list = mergedSessions().filter((session) => !filter || session.harness === filter);
    if (!list.length) {
      $("sessionList").innerHTML = `<div class="empty small">No sessions yet.</div>`;
      $("railNote").textContent = "";
      return;
    }
    // Sessions the user has open stay visible; discovered history collapses so
    // the rail is not dominated by conversations from another client.
    const visible = list.filter((session) => !session.discovered || session.native_active);
    const history = list.filter((session) => session.discovered && !session.native_active);
    const rows = (items) => items.map(sessionRow).join("");
    const emptyVisible = history.length ? "No open sessions." : "No sessions yet.";
    $("sessionList").innerHTML = `
      <div class="session-group">${visible.length ? rows(visible) : `<div class="empty small">${emptyVisible}</div>`}</div>
      ${history.length ? `<details class="session-history"${historyOpen ? " open" : ""}>
        <summary><span>History</span><span class="chip">${history.length}</span></summary>
        <div class="session-history-list">${rows(history)}</div>
      </details>` : ""}
    `;
    $("sessionList").querySelectorAll("[data-session], [data-history]").forEach((button) => {
      button.onclick = () => {
        if (button.dataset.history) {
          return adoptSession(button.dataset.harness, button.dataset.harnessSession, button.dataset.cwd, button.dataset.title);
        }
        return openSessionById(button.dataset.session);
      };
    });
    $("sessionList").querySelectorAll(".session-delete").forEach((button) => {
      button.onclick = (event) => {
        event.stopPropagation();
        return deleteConversation(button);
      };
    });
    const details = $("sessionList").querySelector(".session-history");
    if (details) details.ontoggle = () => { historyOpen = details.open; };
    $("railNote").textContent = [
      `${visible.length} session${visible.length === 1 ? "" : "s"}`,
      ...(history.length ? [`${history.length} in history`] : []),
    ].join(" · ");
  }

  let deleteRequest = null;

  function askDelete(title) {
    return new Promise((resolve) => {
      deleteRequest = resolve;
      $("deleteDetail").textContent = title || "Untitled conversation";
      $("deleteModal").hidden = false;
      $("confirmDelete").focus();
    });
  }

  function finishDelete(value) {
    $("deleteModal").hidden = true;
    if (deleteRequest) {
      deleteRequest(value);
      deleteRequest = null;
    }
  }

  async function deleteConversation(button) {
    const localId = button.dataset.deleteLocal || "";
    const harnessId = button.dataset.deleteHarness || "";
    const harnessSessionId = button.dataset.deleteSession || "";
    const title = button.dataset.deleteTitle || "Untitled conversation";
    const confirmed = await askDelete(title);
    if (!confirmed) return;
    try {
      const result = localId
        ? await H.backend(`/api/sessions/${encodeURIComponent(localId)}?native=1`, "DELETE")
        : await H.backend("/api/sessions/forget", "POST", {
            harness: harnessId,
            session_id: harnessSessionId,
          });
      const wasCurrent = Boolean(current) && (
        (localId && current.id === localId)
        || (harnessId && current.harness === harnessId && current.harness_session_id === harnessSessionId)
      );
      sessions = sessions.filter((session) => session.id !== localId
        && !(harnessId && session.harness === harnessId && session.harness_session_id === harnessSessionId));
      discovered = discovered.filter((session) => !(session.harness === harnessId && session.session_id === harnessSessionId));
      closeRail();
      await loadSessions();
      if (wasCurrent) {
        current = null;
        if (sessions.length) await selectSession(sessions[0]);
        else showEmpty("Start a conversation with a configured harness.");
      } else {
        renderRail();
      }
      void loadDiscovered();
      toast(
        result.native_deleted
          ? "Conversation deleted."
          : "Conversation hidden in Orqella; the harness could not delete it.",
        "ok",
      );
    } catch (error) {
      toast(`Could not delete conversation: ${errorText(error)}`);
      renderRail();
    }
  }

  async function adoptSession(harnessId, harnessSessionId, cwd, title) {
    if (!harnessId || !harnessSessionId) return;
    try {
      $("railNote").textContent = "Opening session…";
      const adopted = await H.backend("/api/sessions/adopt", "POST", {
        harness: harnessId,
        session_id: harnessSessionId,
        cwd: cwd || "",
        title: title || "",
      });
      discovered = discovered.filter((session) => !(session.harness === harnessId && session.session_id === harnessSessionId));
      await loadSessions();
      const row = sessions.find((session) => session.id === adopted.id) || adopted;
      await selectSession(row);
    } catch (error) {
      toast(`Could not open session: ${errorText(error)}`);
      renderRail();
    }
  }

  async function openSessionById(id) {
    const session = sessions.find((entry) => entry.id === id);
    if (!session) return;
    return selectSession(session);
  }

  async function selectSession(session, options = {}) {
    const localUpdated = Date.parse(session.updated_at || "") || 0;
    const nativeUpdated = Number(session.native_updated || 0);
    const stale = options.refresh !== false
      && !refreshing
      && session.harness_session_id
      && session.status !== "working"
      && session.status !== "waiting"
      && (session.needs_sync || (!session.native_active && nativeUpdated > localUpdated + 1000));
    if (stale) {
      refreshing = true;
      closeSocket();
      current = null;
      $("empty").hidden = true;
      $("composer").hidden = true;
      $("transcript").innerHTML = `<p class="small muted loading">Refreshing history…</p>`;
      try {
        await H.backend(`/api/sessions/${encodeURIComponent(session.id)}/refresh`, "POST", {});
        await loadSessions();
        const updated = sessions.find((entry) => entry.id === session.id) || session;
        await selectSession(updated, { refresh: false });
        return;
      } catch (error) {
        toast(`Could not refresh history: ${errorText(error)}`);
      } finally {
        refreshing = false;
      }
    }
    closeRail();
    collapseOptions();
    closeCommandMenu();
    closeSocket();
    current = session;
    lastSeq = 0;
    blocks = new Map();
    toolEls = new Map();
    planEntries = [];
    streamTurn = 0;
    terminalPolls.forEach((timer) => clearInterval(timer));
    terminalPolls = new Map();
    pendingApprovals = new Map();
    $("transcript").innerHTML = `<p class="small muted loading">Loading conversation…</p>`;
    $("empty").hidden = true;
    $("composer").hidden = false;
    $("approval").hidden = true;
    $("plan").innerHTML = `<p class="small muted">No plan yet.</p>`;
    $("usage").innerHTML = `<p class="small muted">Reported by the harness when available.</p>`;
    $("sessionInfo").innerHTML = renderSessionInfo(session);
    setTitle(session.title || harness(session.harness).label);
    setStatus(displayStatus(session));
    renderModel(session);
    usage = session.usage || null;
    renderUsage();
    renderContextChip();
    updateNativeButton();
    renderRail();
    void pollActivity();
    try {
      const result = await H.backend(`/api/sessions/${encodeURIComponent(session.id)}/transcript?since=0`);
      $("transcript").innerHTML = "";
      const events = result.events || [];
      for (let index = 0; index < events.length; index += 1) {
        handleEvent(events[index], false);
        if (index > 0 && index % 200 === 0) {
          await new Promise((resolve) => requestAnimationFrame(() => resolve()));
        }
      }
      lastSeq = events.reduce((max, event) => Math.max(max, event.seq || 0), 0);
      if (!events.length) $("transcript").innerHTML = `<p class="small muted loading">No messages yet. Prompt the harness below.</p>`;
      scrollTranscript();
      connect();
    } catch (error) {
      $("transcript").innerHTML = `<article class="card flat bad">${H.escapeHtml(errorText(error))}</article>`;
    }
  }

  function renderSessionInfo(session) {
    const rows = [
      ["Harness", harness(session.harness).label],
      ["Status", displayStatus(session)],
      ["Folder", session.cwd || "—"],
      ["Session", session.id],
      ["Harness id", session.harness_session_id || "—"],
    ];
    if (session.model) rows.push(["Model", modelTitle(session.model)]);
    const agent = session.agent_info || {};
    if (agent.name) rows.push(["Agent", `${agent.name}${agent.version ? " " + agent.version : ""}`]);
    return `<dl class="info-list">${rows.map(([key, value]) => `<dt>${H.escapeHtml(key)}</dt><dd class="mono small">${H.escapeHtml(value)}</dd>`).join("")}</dl>`;
  }

  // -- websocket ---------------------------------------------------------

  function connect() {
    if (!current) return;
    closeSocket();
    const url = H.wsUrl(context, current.id, lastSeq);
    const ws = new WebSocket(url);
    socket = ws;
    ws.onmessage = (message) => {
      let event;
      try { event = JSON.parse(message.data); } catch { return; }
      if (event.type === "ready") {
        if (event.session) {
          current = { ...current, ...event.session };
          setStatus(displayStatus(current));
          setTitle(current.title || current.id);
          $("sessionInfo").innerHTML = renderSessionInfo(current);
          renderModel(current);
          renderConfigs(current.config_options || []);
          usage = event.session.usage || usage;
          renderUsage();
          renderContextChip();
          renderRail();
        }
        return;
      }
      if (typeof event.seq === "number" && event.seq <= lastSeq) return;
      if (typeof event.seq === "number") lastSeq = event.seq;
      handleEvent(event, true);
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      reconnectTimer = setTimeout(() => {
        if (current) connect();
      }, 1500);
    };
    ws.onerror = () => {};
  }

  function closeSocket() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    if (socket) {
      const old = socket;
      socket = null;
      try { old.close(); } catch {}
    }
  }

  // -- events ------------------------------------------------------------

  function handleEvent(event, live) {
    switch (event.type) {
      case "reset":
        blocks = new Map();
        toolEls = new Map();
        planEntries = [];
        streamTurn = 0;
        pendingApprovals = new Map();
        $("transcript").innerHTML = "";
        $("plan").innerHTML = `<p class="small muted">No plan yet.</p>`;
        usage = null;
        renderUsage();
        renderContextChip();
        return;
      case "message": return renderMessage(event, live);
      case "history": return prependHistory(event.events || []);
      case "tool_call": return renderToolCall(event, live);
      case "plan": return renderPlan(event.entries || []);
      case "permission_request":
        if (live) renderPermission(event);
        return;
      case "permission_resolved": return resolvePermissionCard(event);
      case "config":
        if (current) current.config_options = event.config_options || [];
        return renderConfigs(event.config_options || []);
      case "model":
        if (live && current && event.model) {
          current.model = event.model;
          renderModel(current);
          $("sessionInfo").innerHTML = renderSessionInfo(current);
        }
        return;
      case "mode": return;
      case "commands":
        if (current) commandCache.set(current.id, event.commands || []);
        if (!document.getElementById("commandMenu").hidden) refreshCommandMenu();
        return;
      case "session_info":
        if (event.title) {
          current.title = event.title;
          setTitle(event.title);
          renderRail();
        }
        return;
      case "status":
        if (!live) return;
        if (current) current.status = event.status;
        setStatus(displayStatus(current));
        renderRail();
        return;
      case "turn_end":
        streamTurn += 1;
        flushMessageRenders();
        $("approval").hidden = true;
        return;
      case "usage":
        const nextUsage = event.usage || (event.update && Number(event.update.size) ? event.update : null);
        if (nextUsage) {
          usage = nextUsage;
          if (current) current.usage = usage;
          renderUsage();
          renderContextChip();
        }
        return;
      case "notice": return renderNotice(event.update);
      case "error":
        appendError(event.error || "The harness reported an error");
        setStatus("failed");
        return;
      case "update": return renderUnknown(event);
      default: return;
    }
  }

  function prependHistory(events) {
    if (!events.length) return;
    const transcript = $("transcript");
    const existing = new Set(transcript.children);
    for (const event of events) handleEvent(event, false);
    const added = [...transcript.children].filter((node) => !existing.has(node));
    const first = transcript.firstChild;
    for (const node of added) transcript.insertBefore(node, first);
  }

  function historyNote(text) {
    let note = document.getElementById("historyNote");
    if (!note) {
      note = document.createElement("div");
      note.id = "historyNote";
      note.className = "history-note small muted";
    }
    $("transcript").prepend(note);
    note.textContent = text;
    note.hidden = false;
    return note;
  }

  async function loadOlderHistory() {
    if (!current || historyLoading || !current.has_history) return;
    historyLoading = true;
    const sessionId = current.id;
    const transcript = $("transcript");
    const previousHeight = transcript.scrollHeight;
    const previousTop = transcript.scrollTop;
    const note = historyNote("Loading older messages…");
    try {
      const result = await H.backend(
        `/api/sessions/${encodeURIComponent(current.id)}/history`,
        "POST",
        {},
      );
      if (!current || current.id !== sessionId) return;
      const events = result.events || [];
      if (events.length) prependHistory(events);
      current.has_history = Boolean(result.has_more);
      note.hidden = true;
      transcript.scrollTop = previousTop + (transcript.scrollHeight - previousHeight);
    } catch (error) {
      note.textContent = "Could not load older messages";
      toast(`Could not load older messages: ${errorText(error)}`);
    } finally {
      historyLoading = false;
    }
  }

  function commandMatch() {
    const value = $("prompt").value;
    if (!value.startsWith("/")) return null;
    const match = value.match(/^\/([A-Za-z0-9_.:-]*)(?:\s([\s\S]*))?$/);
    if (!match) return null;
    return {
      name: match[1].toLowerCase(),
      args: match[2] === undefined ? "" : match[2],
      hasArgs: match[2] !== undefined,
    };
  }

  function commandMenuOpen() {
    const menu = document.getElementById("commandMenu");
    return menu && !menu.hidden;
  }

  function closeCommandMenu() {
    const menu = document.getElementById("commandMenu");
    if (!menu) return;
    menu.hidden = true;
    menu.innerHTML = "";
    commandItems = [];
    commandIndex = 0;
  }

  function renderCommandMenu() {
    const menu = document.getElementById("commandMenu");
    if (!menu) return;
    if (!commandItems.length) {
      closeCommandMenu();
      return;
    }
    menu.innerHTML = commandItems.map((item, index) => {
      if (item.kind === "option") {
        return `<button type="button" class="command-item${index === commandIndex ? " active" : ""}" data-command="${H.escapeHtml(item.value)}">
          <span class="command-name">${H.escapeHtml(item.label || item.value)}</span>
          <span class="command-desc">${H.escapeHtml(item.description || `/${item.command} ${item.value}`)}</span>
        </button>`;
      }
      const hint = item.options?.length || item.input?.hint || String(item.description || "").includes("[")
        ? "takes arguments"
        : "";
      return `<button type="button" class="command-item${index === commandIndex ? " active" : ""}" data-command="${H.escapeHtml(item.name)}">
        <span class="command-name">/${H.escapeHtml(item.name)}</span>
        <span class="command-desc">${H.escapeHtml(item.description || "Run this command")}${hint ? ` · <span class="command-hint">${H.escapeHtml(hint)}</span>` : ""}</span>
      </button>`;
    }).join("");
    menu.hidden = false;
    menu.querySelectorAll("[data-command]").forEach((button, index) => {
      button.onmouseenter = () => {
        commandIndex = index;
        menu.querySelectorAll(".command-item").forEach((item, itemIndex) => {
          item.classList.toggle("active", itemIndex === index);
        });
      };
      button.onclick = () => chooseCommand(commandItems[index]);
    });
  }

  async function refreshCommandMenu() {
    const session = current;
    const match = commandMatch();
    if (!session || !match) {
      closeCommandMenu();
      return;
    }
    if (!commandCache.has(session.id)) {
      if (commandLoading) return;
      commandLoading = true;
      try {
        const result = await H.backend(`/api/sessions/${encodeURIComponent(session.id)}/commands`);
        commandCache.set(session.id, result.commands || []);
      } catch {
        commandCache.set(session.id, []);
      } finally {
        commandLoading = false;
      }
    }
    if (!current || current.id !== session.id) return;
    const latest = commandMatch();
    if (!latest) {
      closeCommandMenu();
      return;
    }
    const commands = commandCache.get(session.id) || [];
    const command = commands.find((item) => String(item.name || "").toLowerCase() === latest.name);
    if (latest.hasArgs && command?.options?.length) {
      const query = latest.args.toLowerCase();
      commandItems = command.options
        .filter((option) => {
          const value = String(option.value || "").toLowerCase();
          const label = String(option.label || "").toLowerCase();
          const description = String(option.description || "").toLowerCase();
          return !query || value.includes(query) || label.includes(query) || description.includes(query);
        })
        .slice(0, 12)
        .map((option) => ({ kind: "option", command: command.name, ...option }));
    } else if (!latest.hasArgs) {
      const query = latest.name;
      commandItems = commands
        .filter((item) => {
          const name = String(item.name || "").toLowerCase();
          const description = String(item.description || "").toLowerCase();
          return !query || name.includes(query) || description.includes(query);
        })
        .slice(0, 12)
        .map((item) => ({ kind: "command", ...item }));
    } else {
      closeCommandMenu();
      return;
    }
    commandIndex = Math.min(commandIndex, Math.max(0, commandItems.length - 1));
    renderCommandMenu();
  }

  function chooseCommand(item) {
    if (!item) return;
    if (item.kind === "option") {
      $("prompt").value = `/${item.command} ${item.value}`;
      closeCommandMenu();
      $("composer").requestSubmit();
      return;
    }
    $("prompt").value = `/${item.name} `;
    $("prompt").focus();
    if (item.options?.length) refreshCommandMenu();
    else closeCommandMenu();
  }

  function messageKey(event) {
    return `${event.role}:${event.message_id || `stream-${streamTurn}`}`;
  }

  function scrollTranscript() {
    const transcript = $("transcript");
    transcript.scrollTop = transcript.scrollHeight;
  }

  // Streaming deltas arrive far faster than a display frame. Rebuilding the
  // markdown for every chunk turns a long answer into quadratic DOM work on
  // WebKit and can keep a web process at a full core. Coalesce updates per
  // block and paint at most every STREAM_RENDER_MS.
  const STREAM_RENDER_MS = 80;

  function scheduleMessageRender(stored, scroll) {
    if (scroll) stored.scroll = true;
    if (stored.timer) return;
    stored.timer = setTimeout(() => {
      stored.timer = null;
      if (!stored.el.isConnected) return;
      stored.body.innerHTML = H.markdown(stored.text);
      if (stored.scroll) {
        stored.scroll = false;
        scrollTranscript();
      }
    }, STREAM_RENDER_MS);
  }

  function flushMessageRenders() {
    for (const stored of blocks.values()) {
      if (!stored.timer) continue;
      clearTimeout(stored.timer);
      stored.timer = null;
      if (stored.el.isConnected) stored.body.innerHTML = H.markdown(stored.text);
      if (stored.scroll) {
        stored.scroll = false;
        scrollTranscript();
      }
    }
  }

  function setThoughtExpanded(block, expanded) {
    if (!block) return;
    block.classList.toggle("collapsed", !expanded);
    const toggle = block.querySelector(".thought-toggle");
    if (!toggle) return;
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.title = expanded ? "Hide thinking" : "Show thinking";
    const chevron = block.querySelector(".message-chevron");
    if (chevron) chevron.textContent = expanded ? "▾" : "▸";
  }

  function applyAllCollapsibles(expanded) {
    bulkExpanded = expanded;
    for (const block of $("transcript").querySelectorAll(".message.thought")) {
      setThoughtExpanded(block, expanded);
    }
    for (const entry of toolEls.values()) {
      entry.userToggled = false;
      setToolExpanded(entry, expanded);
    }
  }

  function toggleAllCollapsibles() {
    const anyExpanded = [...toolEls.values()].some((entry) => entry.expanded)
      || [...$("transcript").querySelectorAll(".message.thought")].some((block) => !block.classList.contains("collapsed"));
    applyAllCollapsibles(!anyExpanded);
  }

  function insertPromptText(text) {
    const prompt = $("prompt");
    const start = prompt.selectionStart ?? prompt.value.length;
    const end = prompt.selectionEnd ?? start;
    prompt.value = prompt.value.slice(0, start) + text + prompt.value.slice(end);
    const caret = start + text.length;
    prompt.focus();
    prompt.setSelectionRange(caret, caret);
    prompt.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function runHotkey(action) {
    if (action === "send") {
      collapseOptions();
      if ($("prompt").value.trim() || image) $("composer").requestSubmit();
      else $("prompt").focus();
      return;
    }
    if (action === "newline") {
      collapseOptions();
      insertPromptText("\n");
      return;
    }
    if (action === "toggle-all") {
      toggleAllCollapsibles();
      return;
    }
    if (action === "command") {
      collapseOptions();
      if (!$("prompt").value.trim()) insertPromptText("/");
      else $("prompt").focus();
      return;
    }
    if (action === "close") closeOverlays();
  }

  function renderMessage(event, live) {
    const content = event.content || {};
    if (content.type !== "text") {
      const card = document.createElement("article");
      card.className = "card flat";
      card.innerHTML = `<p class="small muted">Unsupported content</p><pre class="mono small">${H.escapeHtml(JSON.stringify(content).slice(0, 4000))}</pre>`;
      $("transcript").appendChild(card);
      return;
    }
    const key = messageKey(event);
    let block = blocks.get(key);
    if (!block) {
      block = document.createElement("article");
      const role = event.role;
      block.className = `message ${role}`;
      const head = role === "thought"
        ? `<div class="message-head"><button class="thought-toggle" type="button" aria-expanded="false" title="Show thinking"><span class="message-chevron" aria-hidden="true">▸</span><span class="eyebrow">Thinking</span></button></div>`
        : `<div class="message-head"><span class="eyebrow">${H.escapeHtml(H.roleLabel(role))}</span></div>`;
      block.innerHTML = `${head}<div class="message-body"></div>`;
      $("transcript").appendChild(block);
      if (role === "thought") {
        block.querySelector(".thought-toggle").onclick = () => {
          setThoughtExpanded(block, block.classList.contains("collapsed"));
        };
        setThoughtExpanded(block, bulkExpanded === true);
      }
      blocks.set(key, { el: block, body: block.querySelector(".message-body"), text: "" });
    }
    const stored = blocks.get(key);
    stored.text += content.text || "";
    if (live) scheduleMessageRender(stored, event.role !== "thought");
    else stored.body.innerHTML = H.markdown(stored.text);
  }

  function setToolExpanded(entry, expanded) {
    entry.expanded = expanded;
    entry.body.hidden = !expanded;
    entry.el.querySelector(".tool-toggle").setAttribute("aria-expanded", String(expanded));
    entry.el.querySelector(".tool-chevron").textContent = expanded ? "▾" : "▸";
  }

  function renderToolBody(entry) {
    const command = entry.command ? `<pre class="tool-command">${H.escapeHtml(entry.command)}</pre>` : "";
    entry.content.innerHTML = command + (entry.contentHtml || "");
    for (const terminal of entry.content.querySelectorAll("[data-terminal]")) startTerminalPoll(terminal.dataset.terminal);
  }

  function renderToolCall(event, live) {
    const key = event.call_id || `tool-${Math.random().toString(36).slice(2)}`;
    const command = typeof event.raw_input?.command === "string" ? event.raw_input.command : "";
    const isCommand = event.kind === "execute" || Boolean(command);
    let entry = toolEls.get(key);
    if (!entry) {
      const card = document.createElement("article");
      card.className = "card flat tool";
      card.innerHTML = `<div class="tool-head">
        <button class="tool-toggle" type="button" aria-expanded="true">
          <span class="tool-chevron" aria-hidden="true">▾</span>
          <span class="tool-label"></span>
        </button>
      </div>
      <div class="tool-body">
        <div class="tool-meta">
          <b class="tool-title"></b>
          <span class="chip tool-status"></span>
          <span class="small mono muted tool-path" hidden></span>
        </div>
        <div class="tool-content"></div>
      </div>`;
      $("transcript").appendChild(card);
      entry = {
        el: card,
        body: card.querySelector(".tool-body"),
        content: card.querySelector(".tool-content"),
        label: card.querySelector(".tool-label"),
        contentHtml: "",
        command: command || (isCommand ? event.title || "" : ""),
        isCommand,
        expanded: true,
        userToggled: false,
      };
      card.querySelector(".tool-toggle").onclick = () => {
        entry.userToggled = true;
        setToolExpanded(entry, !entry.expanded);
      };
      toolEls.set(key, entry);
      renderToolBody(entry);
    }
    if (isCommand) entry.isCommand = true;
    const labelSource = entry.isCommand
      ? "execute"
      : (event.kind && event.kind !== "other" ? event.kind : event.name || "");
    if (labelSource) entry.label.textContent = labelSource.toUpperCase();
    else if (!entry.label.textContent) entry.label.textContent = "TOOL";
    const title = entry.el.querySelector(".tool-title");
    if (entry.isCommand) {
      if (!title.textContent) title.textContent = event.name === "shell" ? "Shell command" : "Command";
    } else if (event.title) {
      title.textContent = event.title;
    } else if (!title.textContent) {
      title.textContent = event.name || key;
    }
    if (command && entry.command !== command) {
      entry.command = command;
      renderToolBody(entry);
    }
    const status = entry.el.querySelector(".tool-status");
    if (event.status) {
      status.textContent = event.status;
      status.className = `chip tool-status ${event.status === "completed" ? "ok" : event.status === "failed" ? "bad" : "warn"}`;
      entry.el.dataset.status = event.status;
      if (!entry.userToggled) {
        const expanded = bulkExpanded === null ? event.status !== "completed" : bulkExpanded;
        setToolExpanded(entry, expanded);
      }
    }
    if (event.locations?.length) {
      const path = entry.el.querySelector(".tool-path");
      path.textContent = event.locations.map((location) => location.path).join(", ").slice(0, 240);
      path.hidden = false;
    }
    if (event.raw_output && entry.rawOutput !== event.raw_output) entry.rawOutput = event.raw_output;
    if (event.content?.length) {
      entry.contentHtml = event.content.map((item) => renderToolContent(item)).join("");
      renderToolBody(entry);
    }
    if (live) scrollTranscript();
  }

  function renderToolContent(item) {
    if (item.type === "diff") {
      return `<div class="tool-diff"><div class="tool-diff-path">${H.escapeHtml(item.path || "diff")}</div>${H.diff(item.oldText, item.newText)}</div>`;
    }
    if (item.type === "terminal") {
      return `<div class="terminal"><div class="small muted">Terminal <span class="mono">${H.escapeHtml(item.terminalId)}</span></div><pre class="terminal-output" data-terminal="${H.escapeHtml(item.terminalId)}">…</pre></div>`;
    }
    const content = item.content || item;
    if (content.type === "text") return `<div class="tool-text">${H.markdown(content.text || "")}</div>`;
    if (content.type === "image") return `<img class="tool-image" alt="Agent image" src="data:${H.escapeHtml(content.mimeType || "image/png")};base64,${H.escapeHtml(content.data || "")}">`;
    return `<pre class="small mono">${H.escapeHtml(JSON.stringify(content).slice(0, 4000))}</pre>`;
  }

  function startTerminalPoll(terminalId) {
    if (!terminalId || terminalPolls.has(terminalId)) return;
    const read = async () => {
      try {
        const result = await H.backend(`/api/terminals/${encodeURIComponent(terminalId)}`);
        for (const element of document.querySelectorAll(`[data-terminal="${CSS.escape(terminalId)}"]`)) {
          if (element.tagName === "PRE") {
            element.textContent = H.stripAnsi(result.output || "");
            element.scrollTop = element.scrollHeight;
          }
        }
        if (result.exitStatus) {
          clearInterval(terminalPolls.get(terminalId));
          terminalPolls.delete(terminalId);
        }
      } catch {
        clearInterval(terminalPolls.get(terminalId));
        terminalPolls.delete(terminalId);
      }
    };
    read();
    terminalPolls.set(terminalId, setInterval(read, 1200));
  }

  function renderPlan(entries) {
    planEntries = entries;
    if (!entries.length) {
      $("plan").innerHTML = `<p class="small muted">No plan yet.</p>`;
      return;
    }
    $("plan").innerHTML = `<ol class="plan-list">${entries.map((entry) => `<li class="plan-item ${H.escapeHtml(entry.status || "")}"><span class="plan-mark">${entry.status === "completed" ? "✓" : entry.status === "in_progress" ? "•" : "○"}</span><span>${H.escapeHtml(entry.content || entry.title || "")}</span></li>`).join("")}</ol>`;
  }

  function renderPermission(event) {
    const card = document.createElement("article");
    card.className = "card flat approval-card";
    const tool = event.tool_call || {};
    card.dataset.request = event.request_id;
    card.innerHTML = `<p class="eyebrow">Permission requested</p><h3>${H.escapeHtml(tool.title || "Run a tool")}</h3>
      ${tool.locations?.length ? `<p class="small mono muted">${H.escapeHtml(tool.locations.map((location) => location.path).join(", "))}</p>` : ""}
      ${(tool.content || []).map((item) => renderToolContent(item)).join("")}
      <div class="row wrap approval-actions"></div>
      <p class="small muted approval-note"></p>`;
    const actions = card.querySelector(".approval-actions");
    for (const option of event.options || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = option.kind?.startsWith("reject") ? "button ghost" : "button";
      button.textContent = option.name || option.option_id;
      button.onclick = () => decidePermission(card, option);
      actions.appendChild(button);
    }
    pendingApprovals.set(event.request_id, card);
    $("transcript").appendChild(card);
    scrollTranscript();
    renderApprovalBar(event);
  }

  function renderApprovalBar(event) {
    const bar = $("approval");
    bar.hidden = false;
    const tool = event.tool_call || {};
    bar.innerHTML = `<div class="row spread"><span><b>Waiting for approval</b> · ${H.escapeHtml(tool.title || "tool call")}</span><span class="row wrap" data-bar="${H.escapeHtml(event.request_id)}"></span></div>`;
    const target = bar.querySelector("[data-bar]");
    for (const option of event.options || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = option.kind?.startsWith("reject") ? "button ghost small-button" : "button small-button";
      button.textContent = option.name || option.option_id;
      button.onclick = () => decidePermission(pendingApprovals.get(event.request_id), option);
      target.appendChild(button);
    }
  }

  async function decidePermission(card, option) {
    if (!current || !card) return;
    const requestId = card.dataset.request;
    card.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    try {
      await H.backend(`/api/sessions/${encodeURIComponent(current.id)}/permission`, "POST", {
        request_id: requestId,
        option_id: option.option_id,
      });
      card.querySelector(".approval-note").textContent = `Answered: ${option.name || option.option_id}`;
    } catch (error) {
      card.querySelector(".approval-note").textContent = errorText(error);
      card.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    }
    $("approval").hidden = true;
  }

  function resolvePermissionCard(event) {
    const card = pendingApprovals.get(event.request_id);
    if (card) {
      card.querySelectorAll("button").forEach((button) => { button.disabled = true; });
      const note = card.querySelector(".approval-note");
      if (note && !note.textContent) note.textContent = event.option_id ? "Approved" : "Cancelled";
      pendingApprovals.delete(event.request_id);
    }
    $("approval").hidden = true;
  }

  function renderConfigs(options) {
    const target = $("configs");
    const section = $("composerConfigSection");
    if (section) section.hidden = !(options && options.length);
    target.innerHTML = "";
    for (const option of options || []) {
      if (option.type === "boolean") {
        const label = document.createElement("label");
        label.className = "config-check small";
        label.innerHTML = `<input type="checkbox" ${option.currentValue ? "checked" : ""}><span>${H.escapeHtml(option.name || option.id)}</span>`;
        label.querySelector("input").onchange = (input) => setConfig(option, input.target.checked, true);
        target.appendChild(label);
        continue;
      }
      if (option.type !== "select" || !option.options) continue;
      const values = option.options.flatMap((value) => (value.options ? value.options : [value]));
      const wrapper = document.createElement("label");
      wrapper.className = "config-select small";
      wrapper.innerHTML = `<span class="muted">${H.escapeHtml(option.name || option.id)}</span><select class="field">${values.map((value) => `<option value="${H.escapeHtml(value.value)}" ${value.value === option.currentValue ? "selected" : ""}>${H.escapeHtml(value.name || value.value)}</option>`).join("")}</select>`;
      wrapper.querySelector("select").onchange = (change) => setConfig(option, change.target.value, false);
      target.appendChild(wrapper);
    }
  }

  async function setConfig(option, value, boolean) {
    if (!current) return;
    try {
      const result = await H.backend(`/api/sessions/${encodeURIComponent(current.id)}/config`, "POST", {
        config_id: option.id,
        value,
        ...(boolean ? { type: "boolean" } : {}),
      });
      if (current) {
        current.config_options = result.config_options || current.config_options;
        if (result.session?.model) current.model = result.session.model;
        renderModel(current);
        $("sessionInfo").innerHTML = renderSessionInfo(current);
      }
      renderConfigs(result.config_options || []);
    } catch (error) {
      toast(errorText(error));
    }
  }

  function formatTokens(value) {
    return Number(value || 0).toLocaleString();
  }

  function costLabel(cost) {
    if (!cost || typeof cost.amount !== "number") return "";
    const currency = String(cost.currency || "").toUpperCase();
    const symbol = currency === "USD" ? "$" : currency === "EUR" ? "€" : currency === "GBP" ? "£" : "";
    const amount = cost.amount;
    const value = amount > 0 && amount < 0.01 ? amount.toFixed(6) : amount.toFixed(4);
    return `${symbol}${value}${symbol ? "" : " " + currency}`.trim();
  }

  function usageState() {
    if (!usage || !Number(usage.size)) return null;
    const used = Math.max(0, Number(usage.used) || 0);
    const size = Math.max(1, Number(usage.size) || 1);
    const percent = Math.min(100, (used / size) * 100);
    return {
      used,
      size,
      percent,
      className: percent >= 90 ? "bad" : percent >= 75 ? "warn" : "",
      cost: costLabel(usage.cost),
    };
  }

  function renderUsage() {
    const state = usageState();
    if (!state) {
      $("usage").innerHTML = `<p class="small muted">Reported by the harness when available.</p>`;
      return;
    }
    const percentLabel = state.percent < 10 ? state.percent.toFixed(1) : Math.round(state.percent);
    $("usage").innerHTML = `<div class="usage-context ${state.className}">
      <div class="usage-line"><span>Context</span><b>${percentLabel}% used</b></div>
      <div class="usage-track"><span class="usage-fill" style="width:${Math.max(2, state.percent)}%"></span></div>
      <div class="small muted">${formatTokens(state.used)} tokens used${state.cost ? ` · ${state.cost}` : ""}</div>
    </div>`;
  }

  function renderContextChip() {
    const chip = $("contextChip");
    const state = usageState();
    if (!state) {
      chip.hidden = true;
      return;
    }
    const label = state.percent < 10 ? state.percent.toFixed(1) : Math.round(state.percent);
    chip.hidden = false;
    chip.className = `context-chip ${state.className}`;
    chip.title = `Context: ${formatTokens(state.used)} tokens used (${state.percent.toFixed(1)}%)${state.cost ? ` · ${state.cost}` : ""}`;
    $("contextFill").style.width = `${Math.max(2, state.percent)}%`;
    $("contextText").textContent = `${formatTokens(state.used)} · ${label}%`;
  }

  function renderNotice(update) {
    const card = document.createElement("article");
    card.className = "card flat notice";
    const text = update?.message || update?.text || update?.title || JSON.stringify(update || {});
    card.innerHTML = `<p class="small">${H.escapeHtml(text)}</p>`;
    $("transcript").appendChild(card);
  }

  function renderUnknown(event) {
    const card = document.createElement("article");
    card.className = "card flat notice";
    card.innerHTML = `<p class="small muted">${H.escapeHtml(event.session_update || "update")}</p><pre class="mono small">${H.escapeHtml(JSON.stringify(event.update || {}).slice(0, 2000))}</pre>`;
    $("transcript").appendChild(card);
  }

  function appendError(message) {
    const card = document.createElement("article");
    card.className = "card flat bad";
    card.textContent = message;
    $("transcript").appendChild(card);
  }

  // -- Native web workspace ---------------------------------------------

  async function openNative(sessionId, harnessId) {
    const id = harnessId || current?.harness || wantedHarness || harnesses.find((entry) => entry.web?.mount)?.id;
    const entry = harnesses.find((item) => item.id === id);
    const web = entry?.web;
    if (!web?.mount) {
      toast("This harness has no web workspace.");
      return;
    }
    try {
      $("railNative").disabled = true;
      let status = await H.backend(`/api/native/status?harness=${encodeURIComponent(id)}`);
      if (!status.available) throw new Error(status.error || "No web workspace is configured.");
      if (!status.running) {
        status = await H.backend("/api/native/start", "POST", { harness: id });
      }
      if (!status.running && status.error) throw new Error(status.error);
      let token = "";
      if (web.handoff) {
        const pair = await H.backend("/api/native/pair", "POST", { harness: id });
        token = pair?.auth_token || "";
      }
      let target = sessionId && web.path ? web.path.replace("{session}", encodeURIComponent(sessionId)) : "/";
      if (token) {
        target += `${target.includes("?") ? "&" : "?"}auth_token=${encodeURIComponent(token)}`;
      }
      await window.orqella.call("app.open", {
        mount: web.mount,
        node: node(),
        path: target,
      });
    } catch (error) {
      toast(`Could not open the native workspace: ${errorText(error)}`);
    } finally {
      $("railNative").disabled = false;
    }
  }

  // -- new session -------------------------------------------------------

  const openModal = (id) => { $(id).hidden = false; };
  const closeModal = (id) => { $(id).hidden = true; };

  async function showNewSession(preselect = "") {
    $("newError").hidden = true;
    $("newTitleInput").value = "";
    const choices = groupedHarnesses();
    if (!choices.length) {
      toast("No ACP harness configured. Add one in module settings, or start OpenCode from the overview card.");
      return;
    }
    $("newHarness").innerHTML = choices.map((entry) => {
      const state = harnesses.find((item) => item.id === entry.id) || {};
      const suffix = state.installed === false ? " · not installed" : state.running ? " · running" : "";
      return `<option value="${H.escapeHtml(entry.id)}">${H.escapeHtml(entry.label + suffix)}</option>`;
    }).join("");
    let cwd = current?.cwd || wantedCwd || "";
    if (!cwd) {
      const remembered = await storage.get("last_cwd");
      cwd = remembered?.path || "";
    }
    if (!cwd) {
      try {
        const home = await H.backend("/api/fs/dirs");
        cwd = home.path || "";
      } catch {}
    }
    const requested = preselect || registry.default || context?.config?.default_harness;
    if (choices.some((entry) => entry.id === requested)) $("newHarness").value = requested;
    $("newCwd").value = cwd;
    updateNewHarnessHint();
    openModal("newModal");
    $("newTitleInput").focus();
  }

  function updateNewHarnessHint() {
    const entry = harnesses.find((item) => item.id === $("newHarness").value) || {};
    $("newHarnessHint").textContent = entry.installed === false
      ? entry.error || "This harness is not installed on the selected machine."
      : entry.login_hint || "The harness uses its own login and configuration.";
  }

  async function createSession() {
    const harnessId = $("newHarness").value;
    const cwd = $("newCwd").value.trim();
    const title = $("newTitleInput").value.trim();
    if (!harnessId) return;
    if (!cwd) {
      $("newError").textContent = "Choose a workspace folder.";
      $("newError").hidden = false;
      return;
    }
    $("createSession").disabled = true;
    $("newError").hidden = true;
    try {
      const created = await H.backend("/api/sessions", "POST", { harness: harnessId, cwd, title });
      await storage.set("last_cwd", { path: cwd });
      closeModal("newModal");
      await loadSessions();
      const row = sessions.find((session) => session.id === created.id) || created;
      await selectSession(row);
    } catch (error) {
      $("newError").textContent = errorText(error);
      $("newError").hidden = false;
    } finally {
      $("createSession").disabled = false;
    }
  }

  // -- directory picker --------------------------------------------------

  let dirPath = "";
  let dirParent = null;

  async function chooseDirectory(start) {
    try {
      const result = await window.orqella.call("system.pick_directory", {
        node: node(),
        title: "Choose a workspace folder",
        start: start || "",
      });
      if (result?.path) return result.path;
      if (result?.cancelled) return "";
    } catch {}
    await openDirBrowser(start);
    return "";
  }

  async function openDirBrowser(start) {
    $("dirChoose").disabled = true;
    $("dirList").innerHTML = `<p class="small muted">Loading…</p>`;
    openModal("dirModal");
    await loadDir(start || $("newCwd").value.trim());
  }

  async function loadDir(path) {
    try {
      const result = await H.backend(`/api/fs/dirs${path ? `?path=${encodeURIComponent(path)}` : ""}`);
      dirPath = result.path;
      dirParent = result.parent;
      $("dirPath").textContent = result.path;
      $("dirList").innerHTML = result.entries.length
        ? result.entries.map((entry) => `<button type="button" class="dir-row" data-path="${H.escapeHtml(entry.path)}">${H.escapeHtml(entry.name)}</button>`).join("")
        : `<p class="small muted">No folders here.</p>`;
      $("dirList").querySelectorAll("[data-path]").forEach((button) => {
        button.onclick = () => loadDir(button.dataset.path);
      });
      $("dirUp").disabled = !result.parent;
      $("dirChoose").disabled = !result.path;
    } catch (error) {
      $("dirList").innerHTML = `<p class="small bad">${H.escapeHtml(errorText(error))}</p>`;
      $("dirChoose").disabled = true;
    }
  }

  // -- composer ----------------------------------------------------------

  async function sendPrompt(event) {
    event?.preventDefault();
    if (!current || refreshing) return;
    if (statusValue === "working" && !current.native_active) {
      toast("Wait for the current turn to finish.");
      return;
    }
    const text = $("prompt").value.trim();
    if (!text && !image) return;
    $("composerError").hidden = true;
    $("send").disabled = true;
    const command = text.match(/^\/([A-Za-z0-9_.:-]+)(?:\s+([\s\S]*))?$/);
    try {
      if (command) {
        await H.backend(`/api/sessions/${encodeURIComponent(current.id)}/command`, "POST", {
          name: command[1],
          text: command[2] || "",
          images: image ? [image] : [],
        });
      } else {
        await H.backend(`/api/sessions/${encodeURIComponent(current.id)}/prompt`, "POST", {
          text,
          images: image ? [image] : [],
        });
      }
      $("prompt").value = "";
      closeCommandMenu();
      clearImage();
    } catch (error) {
      $("send").disabled = false;
      const message = errorText(error);
      $("composerError").textContent = message;
      $("composerError").hidden = false;
      toast(`Message not sent: ${message}`);
    }
  }

  async function stopPrompt() {
    if (!current) return;
    try {
      await H.backend(`/api/sessions/${encodeURIComponent(current.id)}/cancel`, "POST", {});
    } catch (error) {
      toast(errorText(error));
    }
  }

  async function resumeSession() {
    if (!current) return;
    try {
      await H.backend(`/api/sessions/${encodeURIComponent(current.id)}/resume`, "POST", {});
      await loadSessions();
      const updated = sessions.find((session) => session.id === current.id);
      if (updated) await selectSession(updated);
    } catch (error) {
      toast(errorText(error));
    }
  }

  function clearImage() {
    image = null;
    $("imageName").textContent = "Attach";
    $("imageInput").value = "";
  }

  function readImage(file) {
    return new Promise((resolve, reject) => {
      if (!file) return resolve(null);
      if (file.size > 4 * 1024 * 1024) return reject(new Error("Image must be smaller than 4 MB"));
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        const match = result.match(/^data:([^;]+);base64,(.*)$/);
        if (!match) return reject(new Error("Could not read the image"));
        resolve({ data: match[2], mimeType: match[1] });
      };
      reader.onerror = () => reject(new Error("Could not read the image"));
      reader.readAsDataURL(file);
    });
  }

  // -- wiring ------------------------------------------------------------

  function wire() {
    $("sessionsButton").onclick = () => toggleRail();
    $("closeRail").onclick = () => closeRail();
    $("railBackdrop").onclick = () => closeRail();
    $("emptyNew").onclick = () => showNewSession();
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") pollActivity();
    });
    $("railNew").onclick = () => { closeRail(); showNewSession(); };
    $("railNative").onclick = () => { closeRail(); openNative(current?.harness_session_id); };
    $("railRefresh").onclick = () => refreshAll();
    $("optionsToggle").onclick = () => {
      const expanded = $("composerTools").classList.toggle("expanded");
      $("optionsToggle").setAttribute("aria-expanded", String(expanded));
    };
    $("hotkeyList").querySelectorAll("[data-hotkey]").forEach((button) => {
      button.onclick = () => runHotkey(button.dataset.hotkey);
    });
    $("attachButton").onclick = () => $("imageInput").click();
    $("toggleThoughts").onclick = () => {
      hideThoughts = !hideThoughts;
      applyView();
      saveView();
    };
    $("toggleTools").onclick = () => {
      hideTools = !hideTools;
      applyView();
      saveView();
    };
    $("closeDelete").onclick = () => finishDelete(false);
    $("cancelDelete").onclick = () => finishDelete(false);
    $("confirmDelete").onclick = () => finishDelete(true);
    $("deleteModal").addEventListener("click", (event) => {
      if (event.target === $("deleteModal")) finishDelete(false);
    });
    $("closeNew").onclick = () => closeModal("newModal");
    $("cancelNew").onclick = () => closeModal("newModal");
    $("createSession").onclick = () => createSession();
    $("newHarness").onchange = () => updateNewHarnessHint();
    $("browse").onclick = async () => {
      const chosen = await chooseDirectory($("newCwd").value.trim());
      if (chosen) $("newCwd").value = chosen;
    };
    $("closeDir").onclick = () => closeModal("dirModal");
    $("dirUp").onclick = () => { if (dirParent) loadDir(dirParent); };
    $("dirChoose").onclick = () => {
      if (!dirPath) return;
      $("newCwd").value = dirPath;
      closeModal("dirModal");
    };
    $("composer").onsubmit = sendPrompt;
    $("stop").onclick = stopPrompt;
    $("resume").onclick = resumeSession;
    $("imageInput").onchange = async (event) => {
      try {
        image = await readImage(event.target.files?.[0]);
        $("imageName").textContent = image ? "Attached" : "Attach";
        $("composerError").hidden = true;
      } catch (error) {
        $("composerError").textContent = errorText(error);
        $("composerError").hidden = false;
        clearImage();
      }
    };
    $("prompt").addEventListener("keydown", (event) => {
      if (event.key === "Escape" && commandMenuOpen()) {
        event.preventDefault();
        closeCommandMenu();
        return;
      }
      if (commandMenuOpen()) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const delta = event.key === "ArrowDown" ? 1 : -1;
          commandIndex = (commandIndex + delta + commandItems.length) % commandItems.length;
          renderCommandMenu();
          return;
        }
        if (event.key === "Tab") {
          event.preventDefault();
          chooseCommand(commandItems[commandIndex]);
          return;
        }
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          const item = commandItems[commandIndex];
          const value = $("prompt").value.trim().toLowerCase();
          const needsArgs = Boolean(item && (item.kind === "option" || item.options?.length || item.input?.hint || String(item.description || "").includes("[")));
          if (item && !needsArgs && value === `/${item.name}`.toLowerCase()) {
            $("prompt").value = `/${item.name}`;
            closeCommandMenu();
            $("composer").requestSubmit();
          } else {
            chooseCommand(item);
          }
          return;
        }
      }
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      $("composer").requestSubmit();
    });
    $("prompt").addEventListener("input", () => {
      if (commandMatch()) refreshCommandMenu();
      else closeCommandMenu();
    });
    $("transcript").addEventListener("scroll", () => {
      const transcript = $("transcript");
      if (!current || historyLoading || !current.has_history) return;
      if (transcript.scrollTop > 80) return;
      if (transcript.scrollHeight <= transcript.clientHeight + 120) return;
      loadOlderHistory();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeOverlays();
        return;
      }
      const target = event.target;
      if (target && typeof target.closest === "function" && target.closest("input, textarea, select, [contenteditable='true']")) {
        return;
      }
      const altE = event.altKey && !event.ctrlKey && !event.metaKey && event.key.toLowerCase() === "e";
      const commandE = (event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === "e";
      if (!altE && !commandE) return;
      event.preventDefault();
      toggleAllCollapsibles();
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth > 760) closeRail();
    });
    $("dirModal").addEventListener("click", (event) => {
      if (event.target === $("dirModal")) closeModal("dirModal");
    });
    $("newModal").addEventListener("click", (event) => {
      if (event.target === $("newModal")) closeModal("newModal");
    });
    window.addEventListener("beforeunload", closeSocket);
  }

  window.orqella.ready(async (ctx) => {
    context = ctx;
    wire();
    const savedView = await storage.get("view").catch(() => null);
    hideThoughts = savedView?.thoughts === true;
    hideTools = savedView?.tools === true;
    applyView();
    try {
      await loadAll();
      startActivityPoll();
    } catch (error) {
      $("transcript").innerHTML = `<article class="card flat bad">${H.escapeHtml(errorText(error))}</article>`;
      return;
    }
    if (wantedSession) {
      const local = sessions.find((session) => session.id === wantedSession || session.harness_session_id === wantedSession);
      if (local) return openSessionById(local.id);
      if (wantedHarness && wantedCwd) {
        try {
          const adopted = await H.backend("/api/sessions/adopt", "POST", {
            harness: wantedHarness,
            session_id: wantedSession,
            cwd: wantedCwd,
          });
          await loadSessions();
          return selectSession(adopted);
        } catch (error) {
          const entry = harnesses.find((item) => item.id === wantedHarness);
          if (entry?.web?.mount) {
            toast("Could not open this session in Agent Harness; opening the native workspace.");
            return openNative(wantedSession, wantedHarness);
          }
          showEmpty(`Could not open ${wantedHarness} session ${wantedSession}: ${errorText(error)}`);
          return;
        }
      }
      const fallback = wantedHarness
        || harnesses.find((entry) => entry.web?.mount && entry.web?.handoff)?.id;
      const fallbackEntry = harnesses.find((entry) => entry.id === fallback);
      if (fallbackEntry?.web?.mount) {
        return openNative(wantedSession, fallbackEntry.id);
      }
      showEmpty(`Session ${wantedSession} is not indexed on this machine.`);
      return;
    }
    if (wantedHarness && groupedHarnesses().some((entry) => entry.id === wantedHarness)) {
      filter = wantedHarness;
      renderFilter();
    }
    if (sessions.length) {
      const candidate = filter ? sessions.find((session) => session.harness === filter) : sessions[0];
      if (candidate) return selectSession(candidate);
    }
    showEmpty("Start a conversation with a configured harness.");
  });
})();
