(() => {
  const text = key => window.skeinI18n.t(key);
  let activeView = "execution";
  let administrationAccess = false;
  let executionAccess = false;
  let historyAccess = false;
  let activeWorkflowSection;
  let lowerSection;
  let intro;

  const sections = () => ({
    execution: [document.querySelector(".hero"), document.querySelector(".models"), document.querySelector(".workflow-catalog"), document.querySelector(".tool-plane"), activeWorkflowSection],
    history: [lowerSection, activeWorkflowSection],
    administration: [document.querySelector(".admin-panel"), document.querySelector(".runtime-gate"), document.querySelector(".hardware"), document.querySelector(".model-control")],
  });
  const activate = view => {
    if (view === "administration" && !administrationAccess) view = executionAccess ? "execution" : "history";
    if (view === "execution" && !executionAccess) view = historyAccess ? "history" : "administration";
    if (view === "history" && !historyAccess) view = executionAccess ? "execution" : "administration";
    activeView = view;
    document.body.classList.toggle("history-view", view === "history");
    Object.values(sections()).flat().filter(Boolean).forEach(section => section.classList.add("view-section-hidden"));
    sections()[view].filter(Boolean).forEach(section => section.classList.remove("view-section-hidden"));
    document.querySelectorAll("[data-main-view]").forEach(button => button.classList.toggle("active", button.dataset.mainView === view));
    intro.innerHTML = `<h2>${text(`${view}Title`)}</h2><p>${text(`${view}Text`)}</p>`;
    location.hash = view;
    scrollTo({ top: 0, behavior: "smooth" });
  };
  window.skeinNavigation = {
    init(session) {
      administrationAccess = session.user.permissions.some(permission => ["users.manage","users.verify","settings.manage","email.manage","models.manage","server_stats.read"].includes(permission));
      executionAccess = session.user.permissions.includes("workflows.execute");
      historyAccess = session.user.permissions.some(permission => ["workflows.read_own","workflows.read_all"].includes(permission));
      activeWorkflowSection = document.querySelector("#title")?.closest(".workspace");
      lowerSection = document.querySelector(".lower");
      const navigation = document.createElement("nav");
      navigation.className = "main-navigation"; navigation.dataset.section=text("workspace");
      navigation.innerHTML = `${executionAccess?`<button data-main-view="execution"><i data-lucide="message-square-plus"></i><span>${text("execution")}</span></button>`:""}${historyAccess?`<button data-main-view="history"><i data-lucide="history"></i><span>${text("history")}</span></button>`:""}${administrationAccess ? `<button data-main-view="administration"><i data-lucide="settings-2"></i><span>${text("administration")}</span></button>` : ""}`;
      const sidebar=document.createElement("aside"); sidebar.className="app-sidebar"; sidebar.setAttribute("aria-label",text("primaryNavigation"));
      const brand=document.querySelector("body > header .brand"); sidebar.append(brand,navigation);
      const status=document.createElement("div"); status.className="sidebar-status"; status.innerHTML=`<b><i></i>${text("localControlPlane")}</b><span>${text("gpuWorkspace")}</span>`; sidebar.append(status);
      document.body.prepend(sidebar); window.lucide?.createIcons();
      intro = document.createElement("section"); intro.className = "view-intro"; document.querySelector("main").prepend(intro);
      navigation.querySelectorAll("button").forEach(button => button.onclick = () => activate(button.dataset.mainView));
      lowerSection?.addEventListener("click", event => { if (event.target.closest(".run")) setTimeout(() => activate("history"), 0); });
      const requested = location.hash.slice(1);
      activate(["execution", "history", "administration"].includes(requested) ? requested : (executionAccess?"execution":historyAccess?"history":"administration"));
    },
    show(view) { activate(view); },
  };
})();
