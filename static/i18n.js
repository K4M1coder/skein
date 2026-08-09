(() => {
  const messages = {
    en: {
      language: "Language",
      automatic: "Automatic",
      english: "English",
      french: "Français",
      signIn: "Sign in",
      username: "Username",
      password: "Password",
      signOut: "Sign out",
      administration: "Administration",
      users: "Users",
      createUser: "Create user",
      allowModeChoice: "Allow users to choose Local / Sandbox",
      savePolicy: "Save policy",
      role: "Role",
      active: "Active",
      actions: "Actions",
      signedInAs: "Signed in as",
      clearOwnHistory: "Clear my history",
      clearAllHistory: "Clear all users' history",
      confirmClearOwnHistory: "Permanently delete all of your workflow history and deliverables?",
      confirmClearAllHistory: "Permanently delete workflow history and deliverables for every user?",
      historyCleared: "Workflow history cleared.",
    },
    fr: {
      language: "Langue",
      automatic: "Automatique",
      english: "Anglais",
      french: "Français",
      signIn: "Se connecter",
      username: "Nom d’utilisateur",
      password: "Mot de passe",
      signOut: "Se déconnecter",
      administration: "Administration",
      users: "Utilisateurs",
      createUser: "Créer l’utilisateur",
      allowModeChoice: "Autoriser les utilisateurs à choisir Local / Sandbox",
      savePolicy: "Enregistrer la politique",
      role: "Rôle",
      active: "Actif",
      actions: "Actions",
      signedInAs: "Connecté en tant que",
      clearOwnHistory: "Vider mon historique",
      clearAllHistory: "Vider l’historique de tous les utilisateurs",
      confirmClearOwnHistory: "Supprimer définitivement tout votre historique de workflows et vos livrables ?",
      confirmClearAllHistory: "Supprimer définitivement l’historique et les livrables de tous les utilisateurs ?",
      historyCleared: "Historique des workflows supprimé.",
    },
  };
  const pairs = {
    "Un objectif.": "One objective.", "Plusieurs cerveaux.": "Multiple minds.",
    "Le CPU organise. Les modèles raisonnent. Les agents exécutent selon des politiques inspectables.": "The CPU organizes. Models reason. Agents execute under inspectable policies.",
    "Lancer le workflow →": "Run workflow →", "RUNTIMES REQUIS": "REQUIRED RUNTIMES",
    "Exécution & terminal": "Execution & terminal", "PUISSANCE GPU": "GPU POWER",
    "TÂCHES TERMINÉES": "COMPLETED TASKS", "EN COURS": "RUNNING",
    "RÉSULTATS & LIVRABLES": "RESULTS & DELIVERABLES", "Sorties du workflow": "Workflow outputs",
    "Télécharger le rapport .md": "Download report .md", "Synthèse du workflow": "Workflow summary",
    "Livrable final": "Final deliverable", "Résultat final": "Final result",
    "Télécharger le projet .zip": "Download project .zip", "Visualiser": "Preview", "Exécuter": "Execute",
    "Aucun workflow sélectionné": "No workflow selected", "Aucune exécution.": "No execution yet.",
    "RECENT RUNS": "RECENT RUNS", "EVENT STREAM": "EVENT STREAM", "Model registry & runtime": "Model registry & runtime",
    "+ Ajouter un modèle": "+ Add model", "Reasoner et worker doivent être chargés.": "Reasoner and worker must be loaded.",
    "⚡ Auto-détecter et charger les modèles locaux": "⚡ Auto-detect and load local models",
    "INFÉRENCES LIVE": "LIVE INFERENCES", "dépendance(s)": "dependency(ies)", "en attente": "pending",
    "Non affecté": "Unassigned", "Aucun GPU détecté.": "No GPU detected.", "Charger": "Load",
    "Aucun modèle enregistré.": "No registered model.", "Enregistrer": "Save", "DURÉE": "DURATION",
    "PUISSANCE MOY.": "AVERAGE POWER", "ÉNERGIE EST.": "ESTIMATED ENERGY", "Étape": "Step",
    "sans modèle": "no model", "RÉSUMÉ": "SUMMARY", "Sans résumé": "No summary", "LIVRABLE": "DELIVERABLE",
    "FICHIERS PRODUITS": "PRODUCED FILES", "Hypothèses": "Assumptions", "Preuves": "Evidence",
    "Actions suivantes": "Next actions", "ERREUR": "ERROR", "Dernier résultat disponible": "Latest available result",
    "Visualiser en Markdown": "Preview as Markdown", "Le livrable apparaîtra lorsque les étapes auront produit une sortie.": "The deliverable will appear after the steps produce an output.",
    "Commande dans le workspace du workflow actif": "Command in the active workflow workspace",
    "Docker · réseau désactivé · CPU/RAM/PID limités · filesystem isolé.": "Docker · network disabled · CPU/RAM/PID limited · isolated filesystem.",
    "DANGER · Commandes exécutées sur Windows sans isolation. Accès filesystem réel depuis le workspace.": "DANGER · Commands run on Windows without isolation. Real filesystem access from the workspace.",
    "IMAGE ABSENTE": "IMAGE MISSING", "Exécution en cours…": "Execution in progress…", "Commande en cours…": "Command running…",
    "Aucun fichier n’était requis ou produit pour cette demande.": "No file was required or produced for this request.",
    "Reasoner et worker sont prêts · workflows en mode LIVE": "Reasoner and worker are ready · workflows use LIVE mode",
    "Reasoner et worker doivent être chargés.": "Reasoner and worker must be loaded.",
    "Chargement CUDA en cours…": "Loading CUDA…", "Démarrage des deux serveurs et chargement des poids…": "Starting both servers and loading weights…",
    "GPU DÉTECTÉS": "DETECTED GPUS", "Lancez un objectif pour matérialiser son DAG.": "Run an objective to materialize its DAG.",
    "Ajouter une API OAuth2 robuste avec refresh tokens et tests de sécurité": "Add a robust OAuth2 API with refresh tokens and security tests",
    "Nom du profil": "Profile name", "Commande dans le workspace du workflow actif": "Command in the active workflow workspace",
  };
  const saved = localStorage.getItem("skein_language") || "auto";
  const resolved = saved === "auto" ? ((navigator.language || "en").toLowerCase().startsWith("fr") ? "fr" : "en") : (saved === "fr" ? "fr" : "en");
  window.skeinI18n = {
    selected: saved, language: resolved,
    t(key) { return messages[resolved]?.[key] || messages.en[key] || key; },
    set(value) { localStorage.setItem("skein_language", value); location.reload(); },
  };
  const translate = root => {
    root.querySelectorAll?.("[data-i18n]").forEach(el => { el.textContent = window.skeinI18n.t(el.dataset.i18n); });
    root.querySelectorAll?.("[placeholder]").forEach(el => {
      if (resolved === "en") Object.entries(pairs).forEach(([french, english]) => { el.placeholder = el.placeholder.replaceAll(french, english); });
    });
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const value = node.nodeValue.trim();
      if (!value) continue;
      if (resolved === "en") {
        let translated=node.nodeValue;
        Object.entries(pairs).forEach(([french, english]) => { translated = translated.replaceAll(french, english); });
        if(translated!==node.nodeValue) node.nodeValue=translated;
      }
      if (resolved === "fr") {
        const hit = Object.entries(pairs).find(([, english]) => english === value);
        if (hit) node.nodeValue = node.nodeValue.replace(value, hit[0]);
      }
    }
    document.documentElement.lang = resolved;
  };
  window.skeinTranslate = translate;
  addEventListener("DOMContentLoaded", () => {
    translate(document.body);
    new MutationObserver(records => records.forEach(r => {
      r.addedNodes.forEach(n => n.nodeType === 1 ? translate(n) : (n.nodeType === 3 && n.parentElement ? translate(n.parentElement) : null));
      if(r.type === "characterData" && r.target.parentElement) translate(r.target.parentElement);
    })).observe(document.body, { childList: true, characterData:true, subtree: true });
  });
})();
