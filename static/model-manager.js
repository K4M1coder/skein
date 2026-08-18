(() => {
  const permissions = window.skeinSession?.user?.permissions || [];
  if (!permissions.includes("models.manage")) return;
  const $ = selector => document.querySelector(selector);
  const tr = key => window.skeinI18n.t(key);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const gb = bytes => (bytes || bytes === 0 ? `${(bytes / 1073741824).toFixed(2)} GB` : "—");

  const api = async (path, options) => {
    if (window.skeinSessionExpired) throw Object.assign(Error("session expired"), { silent: true });
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = Error(data.error || response.status); error.data = data; error.status = response.status;
      if (response.status === 401) { window.skeinAuth?.requireSignIn(); error.silent = true; }
      throw error;
    }
    return data;
  };
  const jsonPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const fail = (error, context) => window.showError(error, context);

  let files = [], downloadTimer = null;

  const renderFiles = () => {
    const needle = ($("#model-file-filter").value || "").trim().toLowerCase();
    const visible = needle ? files.filter(file => file.name.toLowerCase().includes(needle) || (file.quantization || "").toLowerCase().includes(needle)) : files;
    $("#model-file-list").innerHTML = visible.map(file => `<div class="model-file${file.registered ? " registered" : ""}">
      <div><strong>${esc(file.name)}</strong>${file.quantization ? ` <span class="quant">${esc(file.quantization)}</span>` : ""}
      <br><small>${esc(file.path)}</small></div>
      <span>${gb(file.size_bytes)}</span>
      <span class="file-state">${file.registered ? tr("alreadyRegistered") : file.too_small ? tr("belowMinimumSize") : tr("notRegistered")}</span>
      ${file.registered || file.too_small ? "" : `<button data-register="${esc(file.path)}">${tr("registerModel")}</button>`}
    </div>`).join("") || `<div class="event">${tr("noModelFiles")}</div>`;
    document.querySelectorAll("[data-register]").forEach(button => button.onclick = async () => {
      button.disabled = true;
      try { await jsonPost("/api/models/files/register", { path: button.dataset.register }); await refresh(); }
      catch (error) { fail(error, tr("registerModel")); button.disabled = false; }
    });
  };

  const loadFiles = async () => {
    const data = await api("/api/models/files");
    files = data.files || [];
    $("#model-file-summary").textContent = `${files.length} ${tr("weightFiles")} · ${tr("library")}: ${data.library}`;
    $("#model-root-list").innerHTML = (data.roots || []).map(root => `<li>${esc(root)}</li>`).join("");
    $("#model-file-warnings").innerHTML = (data.warnings || []).map(warning => `<li>${esc(warning)}</li>`).join("");
    renderFiles();
  };

  const loadRoots = async () => {
    const data = await api("/api/models/roots");
    $("#model-roots-input").value = (data.managed || []).join("\n");
    $("#model-roots-environment").textContent = (data.environment || []).join(" · ") || tr("none");
  };

  const renderDownloads = downloads => {
    $("#download-list").innerHTML = downloads.map(job => {
      const percent = job.progress === null || job.progress === undefined ? null : Math.round(job.progress * 100);
      return `<div class="download-row">
        <div><strong>${esc(job.filename)}</strong><br><small>${esc(job.repo || "")}</small></div>
        <div class="meter"><i style="width:${percent ?? 0}%"></i></div>
        <span>${percent === null ? gb(job.received_bytes) : `${percent}%`}</span>
        <b class="state-${esc(job.status)}">${esc(job.status)}</b>
        ${job.status === "RUNNING" ? `<button class="outline" data-cancel-download="${esc(job.id)}">${tr("cancel")}</button>` : ""}
        ${job.error ? `<small class="model-error">${esc(job.error)}</small>` : ""}
      </div>`;
    }).join("");
    document.querySelectorAll("[data-cancel-download]").forEach(button => button.onclick = async () => {
      button.disabled = true;
      try { await jsonPost(`/api/models/downloads/${button.dataset.cancelDownload}/cancel`); }
      catch (error) { fail(error, tr("cancel")); }
    });
  };

  const pollDownloads = async () => {
    try {
      const data = await api("/api/models/downloads");
      const jobs = data.downloads || [];
      renderDownloads(jobs);
      const running = jobs.some(job => job.status === "RUNNING");
      if (!running && downloadTimer) { clearInterval(downloadTimer); downloadTimer = null; await refresh(); }
    } catch (error) { /* transient poll failure must not break the panel */ }
  };

  const watchDownloads = () => { if (!downloadTimer) downloadTimer = setInterval(pollDownloads, 1000); pollDownloads(); };

  const renderRepoFiles = data => {
    $("#hf-repo-files").innerHTML = `<p class="eyebrow">${esc(data.repo)}${data.gated ? ` · ${tr("gatedRepository")}` : ""}</p>` + (data.files.map(file => `<div class="hf-file">
      <div><strong>${esc(file.filename)}</strong>${file.quantization ? ` <span class="quant">${esc(file.quantization)}</span>` : ""}</div>
      <span>${gb(file.size_bytes)}</span>
      ${file.downloadable ? `<button data-download="${esc(file.filename)}" data-repo="${esc(data.repo)}">${tr("download")}</button>` : `<span>${tr("unsupportedFileName")}</span>`}
    </div>`).join("") || `<div class="event">${tr("noGgufInRepository")}</div>`);
    document.querySelectorAll("[data-download]").forEach(button => button.onclick = async () => {
      button.disabled = true;
      try { await jsonPost("/api/models/huggingface/download", { repo: button.dataset.repo, filename: button.dataset.download }); watchDownloads(); }
      catch (error) { fail(error, tr("download")); button.disabled = false; }
    });
  };

  $("#hf-search-form").onsubmit = async event => {
    event.preventDefault();
    const button = $("#hf-search-form button[type=submit]");
    button.disabled = true;
    try {
      const data = await api(`/api/models/huggingface/search?q=${encodeURIComponent($("#hf-query").value.trim())}`);
      $("#hf-auth-state").textContent = data.authenticated ? tr("hfAuthenticated") : tr("hfAnonymous");
      $("#hf-results").innerHTML = data.results.map(item => `<div class="hf-result">
        <div><strong>${esc(item.repo)}</strong>${item.gated ? ` <span class="quant">${tr("gatedRepository")}</span>` : ""}<br><small>${esc((item.tags || []).join(" · "))}</small></div>
        <span>${item.downloads ?? "—"} ↓</span>
        <button class="outline" data-repo-files="${esc(item.repo)}">${tr("listWeightFiles")}</button>
      </div>`).join("") || `<div class="event">${tr("noHfResults")}</div>`;
      document.querySelectorAll("[data-repo-files]").forEach(entry => entry.onclick = async () => {
        entry.disabled = true;
        try { renderRepoFiles(await api(`/api/models/huggingface/files?repo=${encodeURIComponent(entry.dataset.repoFiles)}`)); }
        catch (error) { fail(error, tr("listWeightFiles")); }
        finally { entry.disabled = false; }
      });
    } catch (error) { fail(error, tr("huggingFaceSearch")); }
    finally { button.disabled = false; }
  };

  $("#model-upload-form").onsubmit = async event => {
    event.preventDefault();
    const input = $("#model-upload-file"), button = $("#model-upload-form button[type=submit]"), status = $("#model-upload-state");
    const file = input.files?.[0];
    if (!file) return;
    button.disabled = true;
    status.textContent = `${tr("uploading")} ${file.name} (${gb(file.size)})`;
    try {
      await api(`/api/models/upload?filename=${encodeURIComponent(file.name)}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Skein-Filename": encodeURIComponent(file.name) }, body: file,
      });
      status.textContent = tr("uploadCompleted");
      input.value = "";
      await refresh();
    } catch (error) { status.textContent = ""; fail(error, tr("uploadWeights")); }
    finally { button.disabled = false; }
  };

  $("#model-roots-form").onsubmit = async event => {
    event.preventDefault();
    const roots = $("#model-roots-input").value.split("\n").map(line => line.trim()).filter(Boolean);
    try { await jsonPost("/api/models/roots", { roots }); await refresh(); }
    catch (error) { fail(error, tr("modelRoots")); }
  };

  $("#model-file-filter").oninput = renderFiles;
  $("#refresh-model-files").onclick = () => refresh();

  const refresh = async () => {
    try { await Promise.all([loadFiles(), loadRoots()]); await pollDownloads(); }
    catch (error) { fail(error, tr("modelFileExplorer")); }
  };

  window.skeinModelManager = { refresh };
  refresh();
})();
