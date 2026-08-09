(() => {
  const t = key => window.skeinI18n.t(key);
  const request = async (path, options) => {
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw Object.assign(new Error(data.error || response.statusText), { data });
    return data;
  };
  const languageControl = () => `<label>${t("language")} <select id="language-choice"><option value="auto">${t("automatic")}</option><option value="en">English</option><option value="fr">Français</option></select></label>`;
  const showLogin = () => {
    document.body.insertAdjacentHTML("beforeend", `<div class="auth-overlay"><div class="auth-card"><h1>Skein</h1><p>${t("signIn")}</p><form id="login-form"><input name="username" autocomplete="username" placeholder="${t("username")}" required><input name="password" type="password" autocomplete="current-password" placeholder="${t("password")}" required><button>${t("signIn")}</button><div class="auth-error"></div></form>${languageControl()}</div></div>`);
    const language = document.querySelector("#language-choice"); language.value = window.skeinI18n.selected; language.onchange = () => window.skeinI18n.set(language.value);
    document.querySelector("#login-form").onsubmit = async event => {
      event.preventDefault(); const fields = Object.fromEntries(new FormData(event.target));
      try { await request("/api/auth/login", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(fields) }); location.reload(); }
      catch (error) { document.querySelector(".auth-error").textContent = error.message; }
    };
  };
  const renderAdmin = async session => {
    const can = permission => session.user.permissions.includes(permission);
    const section = document.createElement("section"); section.className = "workspace admin-panel admin-only";
    section.innerHTML = `<div class="head"><div><p class="eyebrow">RBAC CONTROL PLANE</p><h2>${t("administration")}</h2></div></div><div id="rbac-users"></div><div id="rbac-settings"></div><div id="privacy-stats"></div>`;
    document.querySelector("main").prepend(section);
    if (can("users.manage")) {
      const [users, profiles] = await Promise.all([request("/api/users"), request("/api/rbac/profiles")]);
      const profileOptions = selected => profiles.map(profile => `<label class="profile-option" title="${profile.permissions.join(", ")}"><input type="checkbox" value="${profile.id}" ${selected.includes(profile.id)?"checked":""}> <b>${profile.name}</b><small>${profile.description}</small></label>`).join("");
      document.querySelector("#rbac-users").innerHTML = `<h3>User and profile management</h3><form id="create-user" class="rbac-create"><input name="username" placeholder="${t("username")}" required><input name="password" type="password" minlength="8" placeholder="${t("password")} (8+)" required><div class="profile-picker">${profileOptions(["workflow_operator"])}</div><button>${t("createUser")}</button></form><div id="user-table" class="user-table">${users.map(user => `<div class="rbac-user"><div><b>${user.username}</b><small>${user.permissions.join(" · ")}</small></div><div class="profile-picker" data-profiles="${user.id}">${profileOptions(user.profiles.map(profile=>profile.id))}</div><label><input type="checkbox" data-active="${user.id}" ${user.active?"checked":""}> ${t("active")}</label><input type="password" data-password="${user.id}" minlength="8" placeholder="New password"><button data-save-user="${user.id}">Save</button></div>`).join("")}</div>`;
      document.querySelector("#create-user").onsubmit = async event => { event.preventDefault(); const form=event.target; const fields=Object.fromEntries(new FormData(form)); fields.profiles=[...form.querySelectorAll('.profile-picker input:checked')].map(input=>input.value); await request("/api/users",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(fields)}); location.reload(); };
      document.querySelectorAll("[data-save-user]").forEach(button => button.onclick = async () => { const id=button.dataset.saveUser; const password=document.querySelector(`[data-password="${id}"]`).value; const payload={active:document.querySelector(`[data-active="${id}"]`).checked,profiles:[...document.querySelectorAll(`[data-profiles="${id}"] input:checked`)].map(input=>input.value)}; if(password)payload.password=password; await request(`/api/users/${id}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)}); location.reload(); });
    }
    if (can("settings.manage")) {
      const settings=await request("/api/admin/settings");
      document.querySelector("#rbac-settings").innerHTML=`<h3>Execution settings</h3><label><input id="allow-mode" type="checkbox" ${settings.users_can_choose_execution_mode?"checked":""}> ${t("allowModeChoice")}</label> <button id="save-policy">${t("savePolicy")}</button>`;
      document.querySelector("#save-policy").onclick=()=>request("/api/admin/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({users_can_choose_execution_mode:document.querySelector("#allow-mode").checked})});
    }
    if (can("server_stats.read")) {
      const stats=await request("/api/server-stats"); const summary=stats.summary;
      document.querySelector("#privacy-stats").innerHTML=`<h3>Privacy-safe server statistics</h3><p class="privacy-notice">Content excluded: objective, prompt, result, deliverable, artifacts, username and user ID.</p><div class="metric-summary"><div><b>${summary.request_steps}</b><span>REQUEST STEPS</span></div><div><b>${summary.total_tokens}</b><span>TOKENS</span></div><div><b>${summary.total_duration_seconds}s</b><span>DURATION</span></div><div><b>${summary.average_power_w}W</b><span>AVERAGE POWER</span></div><div><b>${summary.energy_wh}Wh</b><span>ENERGY</span></div></div><div class="stats-table">${stats.requests.map(row=>`<div><time>${new Date(row.started_at*1000).toLocaleString()}</time><code>${row.request_ref}</code><span>${row.model||"N/A"}</span><span>${row.total_tokens} tokens</span><span>${row.duration_seconds}s</span><span>${row.average_power_w}W</span><span>${row.energy_wh}Wh</span></div>`).join("")}</div>`;
    }
  };
  const boot = async () => {
    try {
      const session = await request("/api/auth/me"); window.skeinSession = session;
      const can=permission=>session.user.permissions.includes(permission); const administrationAccess=session.user.permissions.some(permission=>["users.manage","settings.manage","models.manage","server_stats.read"].includes(permission));
      if(!administrationAccess)document.body.classList.add("role-user"); if (!can("settings.manage") && !session.policy.users_can_choose_execution_mode) document.body.classList.add("mode-locked");
      document.querySelector(".stack-bar").insertAdjacentHTML("afterbegin", `<div class="user-bar"><span>${t("signedInAs")} <b>${session.user.username}</b> · ${session.user.profiles.map(profile=>profile.name).join(", ")}</span>${languageControl()}<button id="logout">${t("signOut")}</button></div>`);
      const language = document.querySelector("#language-choice"); language.value=window.skeinI18n.selected; language.onchange=()=>window.skeinI18n.set(language.value);
      document.querySelector("#logout").onclick=async()=>{await request("/api/auth/logout",{method:"POST",body:"{}"});location.reload();};
      document.querySelector(".runtime-gate").classList.toggle("permission-hidden",!can("models.manage")); document.querySelector(".hardware").classList.toggle("permission-hidden",!session.user.permissions.some(permission=>["server_stats.read","settings.manage","models.manage"].includes(permission))); document.querySelector(".model-control").classList.toggle("permission-hidden",!can("models.manage")); document.querySelectorAll("[data-stack]").forEach(x=>x.classList.toggle("permission-hidden",!can("settings.manage")));
      if(administrationAccess) await renderAdmin(session);
      window.skeinNavigation.init(session);
      const script=document.createElement("script"); script.src="/app.js"; document.body.appendChild(script);
    } catch (error) { showLogin(); }
  };
  addEventListener("DOMContentLoaded", boot);
})();
