(() => {
  const text = key => window.skeinI18n.t(key);
  const capitalize = s => s[0].toUpperCase() + s.slice(1);
  let activeView = "execution";
  let executionAccess = false;
  let historyAccess = false;
  let workflowLibraryAccess = false;
  let accessibleDomains = [];
  let lastAdminDomain = null;
  let activeWorkflowSection;
  let lowerSection;
  let intro;

  // Each domain is an independent Administration sub-page, gated by its own
  // permission set. A role with only one accessible domain skips the submenu
  // entirely and lands straight on it (e.g. a Statistics Auditor never sees an
  // expandable tree for a single destination).
  // "hardware" is shared by three permissions and listed last: a single-domain
  // role (e.g. Model Manager) should land on its own domain first and reach
  // the shared one through the submenu, not the other way around.
  const ADMIN_DOMAINS = [
    { key: "access", icon: "users", nav: "accessControl", permissions: ["users.manage"] },
    { key: "policy", icon: "sliders-horizontal", nav: "executionPolicy", permissions: ["settings.manage"] },
    { key: "email", icon: "mail", nav: "email", permissions: ["email.manage"] },
    { key: "stats", icon: "activity", nav: "statistics", permissions: ["server_stats.read"] },
    { key: "models", icon: "package", nav: "models", permissions: ["models.manage"] },
    { key: "hardware", icon: "cpu", nav: "hardware", permissions: ["server_stats.read", "settings.manage", "models.manage"] },
    { key: "logs", icon: "terminal", nav: "systemLog", permissions: ["settings.manage"] },
  ];
  const domainSections = {
    access: () => [document.querySelector(".admin-access")],
    policy: () => [document.querySelector(".admin-policy")],
    email: () => [document.querySelector(".admin-email")],
    stats: () => [document.querySelector(".admin-stats")],
    hardware: () => [document.querySelector(".hardware")],
    models: () => [document.querySelector(".runtime-gate"), document.querySelector(".model-control"), document.querySelector(".model-files"), document.querySelector(".model-hub")],
    logs: () => [document.querySelector(".admin-logs")],
  };
  const isAdminView = view => typeof view === "string" && view.startsWith("admin-");
  const domainKey = view => view.slice("admin-".length);

  const sections = () => ({
    execution: [document.querySelector(".hero"), document.querySelector(".models"), document.querySelector(".tool-plane"), activeWorkflowSection],
    workflows: [document.querySelector(".workflow-catalog")],
    history: [lowerSection, activeWorkflowSection],
    ...Object.fromEntries(ADMIN_DOMAINS.map(domain => [`admin-${domain.key}`, domainSections[domain.key]()])),
  });

  // A single fallback priority replaces four hand-written per-view redirects:
  // try execution, then workflows, then history, then the first accessible
  // administration domain. Any unreachable or unknown view resolves here.
  const fallbackChain = () => [
    executionAccess && "execution",
    workflowLibraryAccess && "workflows",
    historyAccess && "history",
    accessibleDomains[0] && `admin-${accessibleDomains[0].key}`,
  ].filter(Boolean);
  const isViewAccessible = view => {
    if (view === "execution") return executionAccess;
    if (view === "workflows") return workflowLibraryAccess;
    if (view === "history") return historyAccess;
    if (isAdminView(view)) return accessibleDomains.some(domain => domain.key === domainKey(view));
    return false;
  };
  const resolveView = requested => {
    let view = requested;
    if (view === "administration") {
      view = accessibleDomains.some(domain => domain.key === lastAdminDomain) ? `admin-${lastAdminDomain}`
        : accessibleDomains[0] && `admin-${accessibleDomains[0].key}`;
    }
    if (view && isViewAccessible(view)) return view;
    return fallbackChain()[0] || null;
  };

  const activate = requested => {
    const view = resolveView(requested);
    if (!view) return;
    if (isAdminView(view)) lastAdminDomain = domainKey(view);
    activeView = view;
    document.body.classList.toggle("history-view", view === "history");
    Object.values(sections()).flat().filter(Boolean).forEach(section => section.classList.add("view-section-hidden"));
    (sections()[view] || []).filter(Boolean).forEach(section => section.classList.remove("view-section-hidden"));
    document.querySelectorAll("[data-main-view]").forEach(button =>
      button.classList.toggle("active", button.dataset.mainView === view || (button.dataset.mainView === "administration" && isAdminView(view))));
    document.querySelectorAll("[data-admin-view]").forEach(button => button.classList.toggle("active", `admin-${button.dataset.adminView}` === view));
    document.querySelector(".sub-navigation")?.classList.toggle("expanded", isAdminView(view));
    const introKey = isAdminView(view) ? `admin${capitalize(domainKey(view))}` : view;
    intro.innerHTML = `<h2>${text(`${introKey}Title`)}</h2><p>${text(`${introKey}Text`)}</p>`;
    location.hash = view;
    scrollTo({ top: 0, behavior: "smooth" });
  };
  window.skeinNavigation = {
    init(session) {
      const permissions = session.user.permissions;
      executionAccess = permissions.includes("workflows.execute");
      historyAccess = permissions.some(permission => ["workflows.read_own", "workflows.read_all"].includes(permission));
      workflowLibraryAccess = permissions.includes("workflow_templates.read");
      accessibleDomains = ADMIN_DOMAINS.filter(domain => domain.permissions.some(permission => permissions.includes(permission)));
      const administrationAccess = accessibleDomains.length > 0;
      activeWorkflowSection = document.querySelector("#title")?.closest(".workspace");
      lowerSection = document.querySelector(".lower");
      const navigation = document.createElement("nav");
      navigation.className = "main-navigation"; navigation.dataset.section=text("workspace");
      const adminButton = administrationAccess
        ? `<button data-main-view="administration"><i data-lucide="settings-2"></i><span>${text("administration")}</span>${accessibleDomains.length > 1 ? `<i class="submenu-chevron" data-lucide="chevron-down"></i>` : ""}</button>`
        : "";
      const subNavigation = accessibleDomains.length > 1
        ? `<div class="sub-navigation">${accessibleDomains.map(domain => `<button data-admin-view="${domain.key}"><i data-lucide="${domain.icon}"></i><span>${text(domain.nav)}</span></button>`).join("")}</div>`
        : "";
      navigation.innerHTML = `${executionAccess?`<button data-main-view="execution"><i data-lucide="message-square-plus"></i><span>${text("execution")}</span></button>`:""}${workflowLibraryAccess?`<button data-main-view="workflows"><i data-lucide="git-branch"></i><span>${text("workflows")}</span></button>`:""}${historyAccess?`<button data-main-view="history"><i data-lucide="history"></i><span>${text("history")}</span></button>`:""}${adminButton}${subNavigation}`;
      const sidebar=document.createElement("aside"); sidebar.className="app-sidebar"; sidebar.setAttribute("aria-label",text("primaryNavigation"));
      const brand=document.querySelector("body > header .brand"); sidebar.append(brand,navigation);
      const status=document.createElement("div"); status.className="sidebar-status"; status.innerHTML=`<b><i></i>${text("localControlPlane")}</b><span>${text("gpuWorkspace")}</span>`; sidebar.append(status);
      document.body.prepend(sidebar); window.lucide?.createIcons();
      intro = document.createElement("section"); intro.className = "view-intro"; document.querySelector("main").prepend(intro);
      navigation.querySelectorAll("[data-main-view]").forEach(button => button.onclick = () => activate(button.dataset.mainView));
      navigation.querySelectorAll("[data-admin-view]").forEach(button => button.onclick = () => activate(`admin-${button.dataset.adminView}`));
      lowerSection?.addEventListener("click", event => { if (event.target.closest(".run")) setTimeout(() => activate("history"), 0); });
      activate(location.hash.slice(1) || "execution");
    },
    show(view) { activate(view); },
  };
})();
