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
    document.body.insertAdjacentHTML("beforeend", `<div class="auth-overlay"><div class="auth-card"><h1>Skein</h1><p>${t("signIn")}</p><form id="login-form"><input name="username" autocomplete="username" placeholder="${t("username")}" required><input name="password" type="password" autocomplete="current-password" placeholder="${t("password")}" required><button>${t("signIn")}</button></form><details><summary>${t("createAccount")}</summary><form id="register-form"><input name="username" autocomplete="username" placeholder="${t("username")}" required><input name="email" type="email" autocomplete="email" placeholder="Email" required><input name="password" type="password" minlength="8" autocomplete="new-password" placeholder="${t("password")} (8+)" required><button>${t("register")}</button></form></details><div class="auth-error"></div>${languageControl()}</div></div>`);
    const language = document.querySelector("#language-choice"); language.value = window.skeinI18n.selected; language.onchange = () => window.skeinI18n.set(language.value);
    document.querySelector("#login-form").onsubmit = async event => {
      event.preventDefault(); const fields = Object.fromEntries(new FormData(event.target));
      try { await request("/api/auth/login", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(fields) }); location.reload(); }
      catch (error) { document.querySelector(".auth-error").textContent = error.message; }
    };
    document.querySelector("#register-form").onsubmit = async event => {
      event.preventDefault(); const fields=Object.fromEntries(new FormData(event.target)); fields.language=window.skeinI18n.language;
      try { await request("/api/auth/register",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(fields)}); await request("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:fields.username,password:fields.password})}); location.reload(); }
      catch(error){ document.querySelector(".auth-error").textContent=error.message; }
    };
  };
  const showPending = session => {
    document.body.insertAdjacentHTML("beforeend",`<div class="auth-overlay"><div class="auth-card"><h2>${t("verifyEmail")}</h2><p>${t("pendingAccount")} <b>${session.user.email||t("yourEmail")}</b>.</p><form id="verify-form"><input name="code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" placeholder="000000" required><button>${t("verifyAccount")}</button></form><button id="resend-code">${t("resendCode")}</button><button id="pending-logout">${t("signOut")}</button><div class="auth-error"></div>${languageControl()}</div></div>`);
    const language=document.querySelector("#language-choice"); language.value=window.skeinI18n.selected; language.onchange=()=>window.skeinI18n.set(language.value);
    document.querySelector("#verify-form").onsubmit=async event=>{event.preventDefault();try{await request("/api/auth/verify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.fromEntries(new FormData(event.target)))});location.reload()}catch(error){document.querySelector(".auth-error").textContent=error.message}};
    document.querySelector("#resend-code").onclick=async()=>{try{const result=await request("/api/auth/resend",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({language:window.skeinI18n.language})});document.querySelector(".auth-error").textContent=t("codeSent").replace("{minutes}",result.expires_in_seconds/60)}catch(error){document.querySelector(".auth-error").textContent=error.message}};
    document.querySelector("#pending-logout").onclick=async()=>{await request("/api/auth/logout",{method:"POST",body:"{}"});location.reload()};
  };
  const renderAdmin = async session => {
    const can = permission => session.user.permissions.includes(permission);
    const section = document.createElement("section"); section.className = "workspace admin-panel admin-only";
    section.innerHTML = `<div class="head"><div><p class="eyebrow">${t("rbacControlPlane")}</p><h2>${t("administration")}</h2></div></div><div id="rbac-users"></div><div id="rbac-settings"></div><div id="smtp-settings"></div><div id="privacy-stats"></div>`;
    document.querySelector("main").prepend(section);
    if (can("users.manage")) {
      const [users, profiles] = await Promise.all([request("/api/users"), request("/api/rbac/profiles")]);
      const profileOptions = selected => profiles.map(profile => `<label class="profile-option" title="${profile.permissions.join(", ")}"><input type="checkbox" value="${profile.id}" ${selected.includes(profile.id)?"checked":""}> <b>${profile.name}</b><small>${profile.description}</small></label>`).join("");
      document.querySelector("#rbac-users").innerHTML = `<h3>${t("userProfileManagement")}</h3><form id="create-user" class="rbac-create"><input name="username" placeholder="${t("username")}" required><input name="email" type="email" placeholder="${t("optionalEmail")}"><input name="password" type="password" minlength="8" placeholder="${t("password")} (8+)" required><div class="profile-picker">${profileOptions(["workflow_operator"])}</div><button>${t("createUser")}</button></form><div id="user-table" class="user-table">${users.map(user => `<div class="rbac-user"><div><b>${user.username}</b><small>${user.email||t("noEmail")} · ${user.verified?t("verified"):t("pending")}<br>${user.permissions.join(" · ")}</small></div><div class="profile-picker" data-profiles="${user.id}">${profileOptions(user.profiles.map(profile=>profile.id))}</div><label><input type="checkbox" data-active="${user.id}" ${user.active?"checked":""}> ${t("active")}</label><input type="password" data-password="${user.id}" minlength="8" placeholder="${t("newPassword")}"><div><button data-save-user="${user.id}">${t("save")}</button>${!user.verified&&can("users.verify")?`<button data-approve-user="${user.id}">${t("approve")}</button>`:""}</div></div>`).join("")}</div>`;
      document.querySelector("#create-user").onsubmit = async event => { event.preventDefault(); const form=event.target; const fields=Object.fromEntries(new FormData(form)); fields.profiles=[...form.querySelectorAll('.profile-picker input:checked')].map(input=>input.value); await request("/api/users",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(fields)}); location.reload(); };
      document.querySelectorAll("[data-save-user]").forEach(button => button.onclick = async () => { const id=button.dataset.saveUser; const password=document.querySelector(`[data-password="${id}"]`).value; const payload={active:document.querySelector(`[data-active="${id}"]`).checked,profiles:[...document.querySelectorAll(`[data-profiles="${id}"] input:checked`)].map(input=>input.value)}; if(password)payload.password=password; await request(`/api/users/${id}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)}); location.reload(); });
      document.querySelectorAll("[data-approve-user]").forEach(button=>button.onclick=async()=>{await request(`/api/users/${button.dataset.approveUser}/approve`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});location.reload()});
    }
    if (can("settings.manage")) {
      const settings=await request("/api/admin/settings");
      document.querySelector("#rbac-settings").innerHTML=`<h3>${t("executionSettings")}</h3><label><input id="allow-mode" type="checkbox" ${settings.users_can_choose_execution_mode?"checked":""}> ${t("allowModeChoice")}</label> <button id="save-policy">${t("savePolicy")}</button>`;
      document.querySelector("#save-policy").onclick=()=>request("/api/admin/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({users_can_choose_execution_mode:document.querySelector("#allow-mode").checked})});
    }
    if(can("email.manage")){
      const config=await request("/api/admin/email");
      document.querySelector("#smtp-settings").innerHTML=`<h3>${t("outboundEmailServer")}</h3><form id="smtp-form" class="smtp-form"><input name="host" placeholder="${t("smtpHost")}" value="${config.host||""}" required><input name="port" type="number" value="${config.port||587}" required><select name="security"><option value="starttls" ${config.security==="starttls"?"selected":""}>STARTTLS</option><option value="ssl" ${config.security==="ssl"?"selected":""}>SSL/TLS</option><option value="plain" ${config.security==="plain"?"selected":""}>${t("plain")}</option></select><input name="username" placeholder="${t("smtpUsername")}" value="${config.username||""}"><input name="password" type="password" placeholder="${t("smtpPassword")}"><input name="from_address" type="email" placeholder="${t("senderAddress")}" value="${config.from_address||""}" required><button>${t("saveSmtp")}</button></form><form id="smtp-test" class="smtp-test"><input name="recipient" type="email" placeholder="${t("testRecipient")}" required><button>${t("sendTestEmail")}</button></form><div id="smtp-status"></div>`;
      document.querySelector("#smtp-form").onsubmit=async event=>{event.preventDefault();try{await request("/api/admin/email",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.fromEntries(new FormData(event.target)))});document.querySelector("#smtp-status").textContent=t("smtpSaved")}catch(error){document.querySelector("#smtp-status").textContent=error.message}};
      document.querySelector("#smtp-test").onsubmit=async event=>{event.preventDefault();try{await request("/api/admin/email/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.fromEntries(new FormData(event.target)))});document.querySelector("#smtp-status").textContent=t("testEmailSent")}catch(error){document.querySelector("#smtp-status").textContent=error.message}};
    }
    if (can("server_stats.read")) {
      const stats=await request("/api/server-stats"); const summary=stats.summary;
      document.querySelector("#privacy-stats").innerHTML=`<h3>${t("privacySafeStatistics")}</h3><p class="privacy-notice">${t("privacyNotice")}</p><div class="metric-summary"><div><b>${summary.request_steps}</b><span>${t("requestSteps")}</span></div><div><b>${summary.total_tokens}</b><span>TOKENS</span></div><div><b>${summary.total_duration_seconds}s</b><span>${t("duration")}</span></div><div><b>${summary.average_power_w}W</b><span>${t("averagePower")}</span></div><div><b>${summary.energy_wh}Wh</b><span>${t("energy")}</span></div></div><div class="stats-table">${stats.requests.map(row=>`<div><time>${new Date(row.started_at*1000).toLocaleString()}</time><code>${row.request_ref}</code><span>${row.model||"N/A"}</span><span>${row.total_tokens} tokens</span><span>${row.duration_seconds}s</span><span>${row.average_power_w}W</span><span>${row.energy_wh}Wh</span></div>`).join("")}</div>`;
    }
  };
  const boot = async () => {
    try {
      const session = await request("/api/auth/me"); window.skeinSession = session;
      if(!session.user.verified){showPending(session);return}
      const can=permission=>session.user.permissions.includes(permission); const administrationAccess=session.user.permissions.some(permission=>["users.manage","users.verify","settings.manage","email.manage","models.manage","server_stats.read"].includes(permission));
      if(!administrationAccess)document.body.classList.add("role-user"); if (!can("settings.manage") && !session.policy.users_can_choose_execution_mode) document.body.classList.add("mode-locked");
      document.querySelector(".stack-bar").insertAdjacentHTML("afterbegin", `<div class="user-bar"><span>${t("signedInAs")} <b>${session.user.username}</b> · ${session.user.profiles.map(profile=>profile.name).join(", ")}</span>${languageControl()}<button id="logout">${t("signOut")}</button></div>`);
      const language = document.querySelector("#language-choice"); language.value=window.skeinI18n.selected; language.onchange=()=>window.skeinI18n.set(language.value);
      document.querySelector("#logout").onclick=async()=>{await request("/api/auth/logout",{method:"POST",body:"{}"});location.reload();};
      document.querySelector(".runtime-gate").classList.toggle("permission-hidden",!can("models.manage")); document.querySelector(".hardware").classList.toggle("permission-hidden",!session.user.permissions.some(permission=>["server_stats.read","settings.manage","models.manage"].includes(permission))); document.querySelector(".model-control").classList.toggle("permission-hidden",!can("models.manage")); document.querySelectorAll("[data-stack]").forEach(x=>x.classList.toggle("permission-hidden",!can("settings.manage")));
      if(administrationAccess) await renderAdmin(session);
      window.skeinNavigation.init(session);
      await window.skeinWorkflowTemplates.init(session);
      const script=document.createElement("script"); script.src="/app.js?v=10"; document.body.appendChild(script);
    } catch (error) { showLogin(); }
  };
  addEventListener("DOMContentLoaded", boot);
})();
