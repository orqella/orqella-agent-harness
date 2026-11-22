(() => {
  const H = window.Harness;
  const $ = (id) => document.getElementById(id);
  let registry = { version: 1, default: "", harnesses: [] };
  let probes = {};
  let context = null;

  const backend = (path, method = "GET", body) => H.backend(path, method, body);
  const say = (text, kind = "") => {
    $("status").textContent = text;
    $("status").className = `small ${kind === "bad" ? "bad" : kind === "ok" ? "ok" : "muted"}`;
  };

  const splitArgs = (value) => value.split("\n").map((line) => line.trim()).filter(Boolean);
  const parseEnv = (value) => {
    const env = {};
    for (const line of value.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const index = trimmed.indexOf("=");
      if (index <= 0) continue;
      env[trimmed.slice(0, index).trim()] = trimmed.slice(index + 1).trim();
    }
    return env;
  };
  const envText = (env) => Object.entries(env || {}).map(([key, value]) => `${key}=${value}`).join("\n");

  function reportRegistry(status = "") {
    try {
      parent.postMessage({
        type: "orqella:agent-registry",
        harnesses: registry.harnesses
          .filter((entry) => entry.enabled !== false)
          .map((entry) => ({ id: entry.id, label: entry.label, command: entry.command })),
        default: registry.default || "",
        status,
      }, "*");
    } catch (_) { /* not embedded */ }
  }

  function render() {
    $("harnessList").innerHTML = registry.harnesses.map((entry, index) => {
      const probe = probes[entry.id];
      const probeChip = !probe
        ? `<span class="chip">unchecked</span>`
        : probe.installed
          ? `<span class="chip ok" title="${H.escapeHtml(probe.command || "")}">installed${probe.version ? " · " + H.escapeHtml(probe.version) : ""}</span>`
          : `<span class="chip bad" title="${H.escapeHtml(probe.error || "")}">not installed</span>`;
      return `<article class="card flat harness-card" data-index="${index}">
        <div class="row spread">
          <div class="row wrap harness-title">
            <label class="config-check small"><input type="checkbox" data-field="enabled" ${entry.enabled !== false ? "checked" : ""}><span><b>${H.escapeHtml(entry.label)}</b></span></label>
            <span class="chip">${H.escapeHtml(entry.kind)}</span>
            <span class="chip mono">${H.escapeHtml(entry.id)}</span>
            ${entry.id === registry.default ? `<span class="chip ok">default</span>` : ""}
            ${entry.builtin ? `<span class="chip">built-in</span>` : ""}
            ${probeChip}
          </div>
          <div class="row wrap">
            <button class="button ghost small-button" type="button" data-probe="${H.escapeHtml(entry.id)}">Probe</button>
            ${entry.builtin ? "" : `<button class="button ghost small-button" type="button" data-remove="${index}">Remove</button>`}
          </div>
        </div>
        <div class="form-grid harness-fields">
          <label><span class="form-label">Label</span><input class="field" data-field="label" value="${H.escapeHtml(entry.label)}" autocomplete="off"></label>
          <label><span class="form-label">Command</span><input class="field mono" data-field="command" value="${H.escapeHtml(entry.command)}" autocomplete="off" placeholder="/path/to/agent"></label>
          ${entry.kind === "acp" ? `<label><span class="form-label">Transport</span><select class="field" data-field="transport">
            <option value="acp" ${(entry.transport || "acp") === "acp" ? "selected" : ""}>ACP over stdio</option>
            <option value="opencode" ${entry.transport === "opencode" ? "selected" : ""}>Shared service API</option>
          </select></label>
          <p class="config-help wide">The shared service API uses the harness's managed HTTP service, so sessions are live in the harness's own clients too.</p>` : ""}
          <label class="wide"><span class="form-label">Arguments — one per line</span><textarea class="field" data-field="args" rows="2" spellcheck="false">${H.escapeHtml((entry.args || []).join("\n"))}</textarea></label>
          <label class="wide"><span class="form-label">Environment — KEY=VALUE, ${"${VAR}"} reads the daemon environment</span><textarea class="field" data-field="env" rows="2" spellcheck="false">${H.escapeHtml(envText(entry.env))}</textarea></label>
          <label class="wide"><span class="form-label">Login hint</span><input class="field" data-field="login_hint" value="${H.escapeHtml(entry.login_hint || "")}" autocomplete="off"></label>
          <div class="wide web-fields">
            <p class="form-label">Web workspace</p>
            <div class="form-grid">
              <label><span class="form-label">Mount</span><input class="field mono" data-field="web_mount" value="${H.escapeHtml(entry.web?.mount || "")}" autocomplete="off" placeholder="proxy mount id"></label>
              <label><span class="form-label">URL</span><input class="field mono" data-field="web_url" value="${H.escapeHtml(entry.web?.url || "")}" autocomplete="off" placeholder="http://127.0.0.1:PORT"></label>
              <label><span class="form-label">Session path</span><input class="field mono" data-field="web_path" value="${H.escapeHtml(entry.web?.path || "")}" autocomplete="off" placeholder="/session/{session}"></label>
              <label><span class="form-label">Handoff</span><select class="field" data-field="web_handoff"><option value="" ${!entry.web?.handoff ? "selected" : ""}>None</option><option value="opencode" ${entry.web?.handoff === "opencode" ? "selected" : ""}>Managed service</option></select></label>
            </div>
            <p class="config-help">A mount adds <b>Open native workspace</b> for this harness. The handoff driver handles any start or authentication step.</p>
          </div>
          ${entry.kind === "acp" ? `<label><span class="form-label">Fallback command</span><input class="field mono" data-field="fallback_command" value="${H.escapeHtml(entry.fallback?.command || "")}" autocomplete="off" placeholder="npx"></label>
          <label class="wide"><span class="form-label">Fallback arguments</span><textarea class="field" data-field="fallback_args" rows="2" spellcheck="false">${H.escapeHtml((entry.fallback?.args || []).join("\n"))}</textarea></label>` : ""}
        </div>
        <div class="row wrap harness-flags">
          <label class="config-check small"><input type="checkbox" data-field="delegate_fs" ${entry.delegate_fs ? "checked" : ""}><span><b>Delegate file access</b><small class="config-help">The harness can read and write files inside the session folder through the module.</small></span></label>
          <label class="config-check small"><input type="checkbox" data-field="delegate_terminal" ${entry.delegate_terminal ? "checked" : ""}><span><b>Delegate terminal</b><small class="config-help">The harness can run commands inside the session folder. Every command is logged.</small></span></label>
        </div>
      </article>`;
    }).join("");
    wireRows();
    reportRegistry();
    requestAnimationFrame(reportHeight);
  }

  function wireRows() {
    $("harnessList").querySelectorAll("[data-probe]").forEach((button) => {
      button.onclick = async () => {
        button.disabled = true;
        try {
          probes[button.dataset.probe] = await backend("/api/registry/probe", "POST", { id: button.dataset.probe });
          render();
          say(`Probed ${button.dataset.probe}.`, "ok");
        } catch (error) {
          say(error.message || "Probe failed.", "bad");
          button.disabled = false;
        }
      };
    });
    $("harnessList").querySelectorAll("[data-remove]").forEach((button) => {
      button.onclick = () => {
        const index = Number(button.dataset.remove);
        const entry = registry.harnesses[index];
        if (!entry || entry.builtin) return;
        if (!confirm(`Remove ${entry.label} from the registry?`)) return;
        registry.harnesses.splice(index, 1);
        render();
      };
    });
  }

  function collect() {
    const harnesses = [];
    $("harnessList").querySelectorAll(".harness-card").forEach((card) => {
      const entry = registry.harnesses[Number(card.dataset.index)];
      if (!entry) return;
      const field = (name) => card.querySelector(`[data-field="${name}"]`);
      const value = (name) => field(name)?.value ?? "";
      const checked = (name) => !!field(name)?.checked;
      const next = {
        ...entry,
        label: value("label").trim() || entry.id,
        command: value("command").trim(),
        args: splitArgs(value("args")),
        env: parseEnv(value("env")),
        login_hint: value("login_hint").trim(),
        delegate_fs: checked("delegate_fs"),
        delegate_terminal: checked("delegate_terminal"),
        enabled: checked("enabled"),
      };
      next.transport = entry.kind === "acp" ? (value("transport") || "acp") : "acp";
      const webMount = value("web_mount").trim();
      next.web = webMount
        ? {
            mount: webMount,
            url: value("web_url").trim(),
            path: value("web_path").trim(),
            handoff: value("web_handoff"),
          }
        : null;
      if (entry.kind === "acp") {
        const command = value("fallback_command").trim();
        const args = splitArgs(value("fallback_args"));
        next.fallback = command ? { command, args } : null;
      }
      harnesses.push(next);
    });
    return {
      version: registry.version || 1,
      default: registry.default || harnesses[0]?.id || "",
      harnesses,
    };
  }

  async function save() {
    $("save").disabled = true;
    try {
      registry = await backend("/api/registry", "PUT", collect());
      probes = {};
      render();
      reportRegistry("saved");
      say("Registry saved.", "ok");
      return true;
    } catch (error) {
      say(error.message || "Could not save the registry.", "bad");
      try { parent.postMessage({ type: "orqella:agent-registry-error", message: error.message || "Could not save the registry." }, "*"); } catch (_) {}
      return false;
    } finally {
      $("save").disabled = false;
    }
  }

  async function reload() {
    $("reload").disabled = true;
    try {
      registry = await backend("/api/registry");
      render();
      reportRegistry();
      say("Registry loaded.", "ok");
    } catch (error) {
      say(error.message || "Could not load the registry.", "bad");
    } finally {
      $("reload").disabled = false;
    }
  }

  $("save").onclick = () => save();
  $("reload").onclick = () => reload();
  window.addEventListener("message", (event) => {
    const message = event.data;
    if (!message || message.type !== "orqella:set-agent-default") return;
    const id = String(message.id || "");
    const entry = registry.harnesses.find((item) => item.id === id && item.enabled !== false);
    if (!entry) {
      try { parent.postMessage({ type: "orqella:agent-registry-error", message: "That agent is not enabled in the registry." }, "*"); } catch (_) {}
      return;
    }
    const previous = registry.default;
    registry.default = id;
    render();
    save().then((ok) => {
      if (ok) return;
      registry.default = previous;
      render();
      reportRegistry();
    });
  });
  $("addCustom").onclick = () => {
    const id = $("customId").value.trim().toLowerCase();
    const label = $("customLabel").value.trim() || id;
    const command = $("customCommand").value.trim();
    const args = $("customArgs").value.trim();
    if (!id || !/^[a-z0-9][a-z0-9._-]*$/.test(id)) return ($("customError").textContent = "Use a lowercase id with letters, digits, dots, dashes, or underscores.", $("customError").hidden = false);
    if (registry.harnesses.some((entry) => entry.id === id)) return ($("customError").textContent = "That id already exists.", $("customError").hidden = false);
    if (!command) return ($("customError").textContent = "A command is required.", $("customError").hidden = false);
    registry.harnesses.push({
      id,
      label,
      kind: "exec",
      command,
      args: args ? args.split(/\s+/).filter(Boolean) : ["{prompt}"],
      env: {},
      login_hint: "",
      delegate_fs: false,
      delegate_terminal: false,
      builtin: false,
      enabled: true,
    });
    $("customId").value = "";
    $("customLabel").value = "";
    $("customCommand").value = "";
    $("customArgs").value = "";
    $("customError").hidden = true;
    render();
    say("Custom harness added. Save the registry to apply it.", "ok");
  };

  let heightTimer = null;
  function reportHeight() {
    clearTimeout(heightTimer);
    heightTimer = setTimeout(() => {
      const height = Math.ceil(Math.max(document.documentElement.scrollHeight, document.body.scrollHeight));
      parent.postMessage({ type: "orqella:frame-height", height }, "*");
    }, 30);
  }

  if (window.ResizeObserver) {
    const observer = new ResizeObserver(reportHeight);
    observer.observe(document.body);
    observer.observe(document.documentElement);
    const list = document.getElementById("harnessList");
    if (list) observer.observe(list);
  }
  window.addEventListener("load", reportHeight);
  window.addEventListener("resize", reportHeight);
  requestAnimationFrame(reportHeight);

  window.orqella.ready(async (ctx) => {
    context = ctx;
    if (ctx.config?.allow_adapter_install) say("Adapter fallback is enabled.", "ok");
    await reload();
  });
})();
