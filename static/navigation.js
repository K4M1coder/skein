(() => {
  const labels = {
    en: {
      execution: "Execution",
      history: "History",
      administration: "Administration",
      executionTitle: "Workflow execution",
      executionText: "Create a workflow, follow its steps, inspect outputs, and run its deliverables.",
      historyTitle: "Workflow history",
      historyText: "Select a previous run to inspect its report, metrics, steps, and artifacts.",
      administrationTitle: "System administration",
      administrationText: "Manage access, execution policy, hardware pools, models, and runtimes.",
    },
    fr: {
      execution: "Exécution",
      history: "Historique",
      administration: "Administration",
      executionTitle: "Exécution des workflows",
      executionText: "Créez un workflow, suivez ses étapes, consultez ses sorties et exécutez ses livrables.",
      historyTitle: "Historique des workflows",
      historyText: "Sélectionnez une exécution précédente pour consulter son rapport, ses métriques, ses étapes et ses fichiers.",
      administrationTitle: "Administration du système",
      administrationText: "Gérez les accès, la politique d’exécution, les pools matériels, les modèles et les runtimes.",
    },
  };
  const language = () => window.skeinI18n?.language === "fr" ? "fr" : "en";
  const text = key => labels[language()][key];
  let activeView = "execution";
  let administrationAccess = false;
  let executionAccess = false;
  let historyAccess = false;
  let activeWorkflowSection;
  let lowerSection;
  let intro;

  const sections = () => ({
    execution: [document.querySelector(".hero"), document.querySelector(".models"), document.querySelector(".tool-plane"), activeWorkflowSection],
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
      administrationAccess = session.user.permissions.some(permission => ["users.manage","settings.manage","models.manage","server_stats.read"].includes(permission));
      executionAccess = session.user.permissions.includes("workflows.execute");
      historyAccess = session.user.permissions.some(permission => ["workflows.read_own","workflows.read_all"].includes(permission));
      activeWorkflowSection = document.querySelector("#title")?.closest(".workspace");
      lowerSection = document.querySelector(".lower");
      const navigation = document.createElement("nav");
      navigation.className = "main-navigation";
      navigation.innerHTML = `${executionAccess?`<button data-main-view="execution">${text("execution")}</button>`:""}${historyAccess?`<button data-main-view="history">${text("history")}</button>`:""}${administrationAccess ? `<button data-main-view="administration">${text("administration")}</button>` : ""}`;
      document.querySelector("header").after(navigation);
      intro = document.createElement("section"); intro.className = "view-intro"; document.querySelector("main").prepend(intro);
      navigation.querySelectorAll("button").forEach(button => button.onclick = () => activate(button.dataset.mainView));
      lowerSection?.addEventListener("click", event => { if (event.target.closest(".run")) setTimeout(() => activate("history"), 0); });
      const requested = location.hash.slice(1);
      activate(["execution", "history", "administration"].includes(requested) ? requested : (executionAccess?"execution":historyAccess?"history":"administration"));
    },
    show(view) { activate(view); },
  };
})();
