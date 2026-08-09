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
  const renderAdmin = async () => {
    const section = document.createElement("section"); section.className = "workspace admin-panel admin-only";
    section.innerHTML = `<div class="head"><div><p class="eyebrow">ACCESS CONTROL</p><h2>${t("administration")}</h2></div></div><label><input id="allow-mode" type="checkbox"> ${t("allowModeChoice")}</label> <button id="save-policy">${t("savePolicy")}</button><form id="create-user" class="admin-grid"><input name="username" placeholder="${t("username")}" required><input name="password" type="password" minlength="8" placeholder="${t("password")} (8+)" required><select name="role"><option value="user">User</option><option value="admin">Admin</option></select><button>${t("createUser")}</button></form><div id="user-table" class="user-table"></div>`;
    document.querySelector("main").prepend(section);
    const refresh = async () => {
      const [users, settings] = await Promise.all([request("/api/users"), request("/api/admin/settings")]);
      document.querySelector("#allow-mode").checked = settings.users_can_choose_execution_mode;
      document.querySelector("#user-table").innerHTML = users.map(user => `<div class="user-row" style="grid-template-columns:1fr 100px 90px 1fr auto"><b>${user.username}</b><select data-role="${user.id}"><option value="user" ${user.role==="user"?"selected":""}>User</option><option value="admin" ${user.role==="admin"?"selected":""}>Admin</option></select><label><input type="checkbox" data-active="${user.id}" ${user.active?"checked":""}> ${t("active")}</label><input type="password" data-password="${user.id}" minlength="8" placeholder="New password"><button data-save-user="${user.id}">Save</button></div>`).join("");
      document.querySelectorAll("[data-save-user]").forEach(button => button.onclick = async () => { const id=button.dataset.saveUser; const password=document.querySelector(`[data-password="${id}"]`).value; const payload={role:document.querySelector(`[data-role="${id}"]`).value,active:document.querySelector(`[data-active="${id}"]`).checked}; if(password)payload.password=password; await request(`/api/users/${id}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)}); refresh(); });
    };
    document.querySelector("#save-policy").onclick = async () => request("/api/admin/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({users_can_choose_execution_mode:document.querySelector("#allow-mode").checked})});
    document.querySelector("#create-user").onsubmit = async event => { event.preventDefault(); await request("/api/users",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.fromEntries(new FormData(event.target))) }); event.target.reset(); refresh(); };
    await refresh();
  };
  const boot = async () => {
    try {
      const session = await request("/api/auth/me"); window.skeinSession = session;
      document.body.classList.add(`role-${session.user.role}`); if (session.user.role === "user" && !session.policy.users_can_choose_execution_mode) document.body.classList.add("mode-locked");
      document.querySelector(".stack-bar").insertAdjacentHTML("afterbegin", `<div class="user-bar"><span>${t("signedInAs")} <b>${session.user.username}</b> · ${session.user.role}</span>${languageControl()}<button id="logout">${t("signOut")}</button></div>`);
      const language = document.querySelector("#language-choice"); language.value=window.skeinI18n.selected; language.onchange=()=>window.skeinI18n.set(language.value);
      document.querySelector("#logout").onclick=async()=>{await request("/api/auth/logout",{method:"POST",body:"{}"});location.reload();};
      document.querySelector(".runtime-gate").classList.add("admin-only"); document.querySelector(".hardware").classList.add("admin-only"); document.querySelector(".model-control").classList.add("admin-only"); document.querySelectorAll("[data-stack]").forEach(x=>x.classList.add("admin-only"));
      if(session.user.role==="admin") await renderAdmin();
      window.skeinNavigation.init(session);
      const script=document.createElement("script"); script.src="/app.js"; document.body.appendChild(script);
    } catch (error) { showLogin(); }
  };
  addEventListener("DOMContentLoaded", boot);
})();
