(function () {
  "use strict";

  const fmt = new Intl.NumberFormat("fr-FR");
  const fmtFCFA = (n) => `${fmt.format(Math.round(n || 0))} FCFA`;

  let currentRange = { start: null, end: null };
  let currentUser = null; // { username, role }

  // ---------------------------------------------------------------------
  // apiFetch : wrapper autour de fetch() qui renvoie vers /login si la
  // session a expiré ou n'existe plus (réponse 401 de l'API). Toutes les
  // routes /api/* sont protégées côté serveur ; ceci gère le cas où la
  // session expire pendant que la page est déjà ouverte.
  // ---------------------------------------------------------------------
  async function apiFetch(url, opts) {
    const res = await fetch(url, opts);
    if (res.status === 401) {
      window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
      throw new Error("Session expirée");
    }
    return res;
  }

  // ---------------------------------------------------------------------
  // Helpers période
  // ---------------------------------------------------------------------
  function isoDate(d) {
    return d.toISOString().slice(0, 10);
  }
  function startOfWeek(d) {
    const day = (d.getDay() + 6) % 7; // lundi = 0
    const s = new Date(d);
    s.setDate(d.getDate() - day);
    return s;
  }
  function endOfWeek(d) {
    const s = startOfWeek(d);
    s.setDate(s.getDate() + 6);
    return s;
  }
  function presetRange(preset) {
    const today = new Date();
    if (preset === "week") return { start: isoDate(startOfWeek(today)), end: isoDate(endOfWeek(today)) };
    if (preset === "month") {
      const s = new Date(today.getFullYear(), today.getMonth(), 1);
      const e = new Date(today.getFullYear(), today.getMonth() + 1, 0);
      return { start: isoDate(s), end: isoDate(e) };
    }
    if (preset === "year") {
      return { start: `${today.getFullYear()}-01-01`, end: `${today.getFullYear()}-12-31` };
    }
    return { start: null, end: null }; // all
  }

  function setRange(start, end) {
    currentRange = { start, end };
    document.getElementById("range-start").value = start || "";
    document.getElementById("range-end").value = end || "";
    const label = document.getElementById("period-label");
    label.textContent = start && end ? `du ${start} au ${end}` : "tout l'historique";
    refreshPeriodData();
  }

  document.getElementById("preset-buttons").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-preset]");
    if (!btn) return;
    document.querySelectorAll("#preset-buttons button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const r = presetRange(btn.dataset.preset);
    setRange(r.start, r.end);
  });

  document.getElementById("apply-range").addEventListener("click", () => {
    document.querySelectorAll("#preset-buttons button").forEach((b) => b.classList.remove("active"));
    const s = document.getElementById("range-start").value || null;
    const e = document.getElementById("range-end").value || null;
    setRange(s, e);
  });

  // ---------------------------------------------------------------------
  // Résumé (KPI + banques)
  // ---------------------------------------------------------------------
  async function refreshSummary() {
    const params = new URLSearchParams();
    if (currentRange.start) params.set("start", currentRange.start);
    if (currentRange.end) params.set("end", currentRange.end);
    const res = await apiFetch(`/api/summary?${params}`);
    const data = await res.json();

    document.getElementById("kpi-total").textContent = fmtFCFA(data.total);
    document.getElementById("kpi-count").textContent = `${data.count} paiement(s)`;
    document.getElementById("kpi-orange").textContent = fmtFCFA(data.by_categorie.Orange);
    document.getElementById("kpi-mtn").textContent = fmtFCFA(data.by_categorie.MTN);
    document.getElementById("kpi-autre").textContent = fmtFCFA(data.by_categorie.Autre);

    const tbody = document.querySelector("#banks-table tbody");
    tbody.innerHTML = "";
    if (data.by_banque.length === 0) {
      tbody.innerHTML = `<tr><td colspan="2" class="empty-state">Aucun paiement sur cette période.</td></tr>`;
    }
    for (const row of data.by_banque) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${escapeHtml(row.banque)}</td><td class="num">${fmtFCFA(row.total)}</td>`;
      tbody.appendChild(tr);
    }
  }

  // ---------------------------------------------------------------------
  // Anomalies / à vérifier
  // ---------------------------------------------------------------------
  async function refreshAnomalies() {
    const res = await apiFetch("/api/anomalies");
    const rows = await res.json();
    document.getElementById("anomalies-count").textContent = rows.length;
    const list = document.getElementById("anomalies-list");
    list.innerHTML = "";
    if (rows.length === 0) {
      list.innerHTML = `<div class="empty-state">Rien à signaler. ✓</div>`;
      return;
    }
    for (const r of rows) {
      const div = document.createElement("div");
      div.className = "anomaly-card";
      const reason = (r.review_reason || "").replace(/^\s*\|\s*/, "");
      const isMissing = r.status === "a_verifier_disparu";
      div.innerHTML = `
        <div class="row1">
          <div><strong>${escapeHtml(r.raison_sociale || "(sans nom)")}</strong> — ${escapeHtml(r.banque || "")}</div>
          <div class="amount">${fmtFCFA(r.recouvrement_utilise)}</div>
        </div>
        <div class="hint small">${escapeHtml(r.date)} · ${escapeHtml(r.libelle || "")}</div>
        ${reason ? `<div class="anomaly-reason">${escapeHtml(reason)}</div>` : ""}
        ${isMissing ? `<button data-key="${escapeHtml(r.key)}" data-info="${escapeHtml(r.raison_sociale)} — ${fmtFCFA(r.recouvrement_utilise)} — ${escapeHtml(r.date)}">Confirmer l'annulation</button>` : ""}
      `;
      list.appendChild(div);
    }
    list.querySelectorAll("button[data-key]").forEach((btn) => {
      btn.addEventListener("click", () => openConfirmModal(btn.dataset.key, btn.dataset.info));
    });
  }

  // ---------------------------------------------------------------------
  // Modal de confirmation d'annulation
  // ---------------------------------------------------------------------
  const modalBackdrop = document.getElementById("modal-backdrop");
  let modalKey = null;

  function openConfirmModal(key, info) {
    modalKey = key;
    document.getElementById("modal-record-info").textContent = info;
    document.getElementById("modal-motif").value = "";
    document.getElementById("modal-error").hidden = true;
    modalBackdrop.hidden = false;
  }
  function closeModal() {
    modalBackdrop.hidden = true;
    modalKey = null;
  }
  document.getElementById("modal-cancel").addEventListener("click", closeModal);
  modalBackdrop.addEventListener("click", (e) => { if (e.target === modalBackdrop) closeModal(); });

  document.getElementById("modal-confirm").addEventListener("click", async () => {
    const motif = document.getElementById("modal-motif").value.trim();
    const agent = document.getElementById("modal-agent").value.trim();
    const errorEl = document.getElementById("modal-error");
    if (!motif) {
      errorEl.textContent = "Le motif est obligatoire.";
      errorEl.hidden = false;
      return;
    }
    const res = await apiFetch("/api/confirm-cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: modalKey, motif, confirmed_by: agent }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.error || "Erreur lors de la confirmation.";
      errorEl.hidden = false;
      return;
    }
    closeModal();
    refreshAnomalies();
    refreshSummary();
    refreshRecords();
  });

  // ---------------------------------------------------------------------
  // Détail des paiements
  // ---------------------------------------------------------------------
  async function refreshRecords() {
    const params = new URLSearchParams();
    if (currentRange.start) params.set("start", currentRange.start);
    if (currentRange.end) params.set("end", currentRange.end);
    const search = document.getElementById("search-input").value.trim();
    const categorie = document.getElementById("filter-categorie").value;
    const banque = document.getElementById("filter-banque").value;
    if (search) params.set("search", search);
    if (categorie) params.set("categorie", categorie);
    if (banque) params.set("banque", banque);

    const res = await apiFetch(`/api/records?${params}`);
    const rows = await res.json();
    const tbody = document.querySelector("#records-table tbody");
    tbody.innerHTML = "";
    const statusLabels = { actif: "Actif", a_verifier_disparu: "À vérifier", annule_confirme: "Annulé (confirmé)" };
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(r.date)}</td>
        <td>${escapeHtml(r.raison_sociale)}</td>
        <td>${escapeHtml(r.libelle)}</td>
        <td>${escapeHtml(r.banque)}</td>
        <td>${escapeHtml(r.numero_facture) || "—"}</td>
        <td>${escapeHtml(r.categorie)}</td>
        <td class="num">${fmtFCFA(r.recouvrement_utilise)}</td>
        <td><span class="status-pill status-${r.status}">${statusLabels[r.status] || r.status}</span></td>
      `;
      tbody.appendChild(tr);
    }
    const note = document.getElementById("records-note");
    note.textContent = rows.length >= 2000
      ? "Affichage à l'écran limité aux 2000 lignes les plus récentes — l'export Excel, lui, contient toutes les lignes correspondant aux filtres."
      : `${rows.length} ligne(s) affichée(s).`;

    document.getElementById("export-records-link").href = `/api/export/records.xlsx?${params}`;
  }
  document.getElementById("apply-filters").addEventListener("click", refreshRecords);
  document.getElementById("search-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") refreshRecords();
  });

  // ---------------------------------------------------------------------
  // Clic sur une tuile KPI (Total / Orange / MTN / Autres) : filtre le
  // détail des paiements sur cette catégorie pour la période en cours,
  // et amène directement sur le tableau (exploitants, montants, n° de
  // facture, banque, date) — avec l'export Excel déjà filtré pareil.
  // ---------------------------------------------------------------------
  document.querySelectorAll(".kpi-clickable").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById("filter-categorie").value = btn.dataset.categorie || "";
      document.getElementById("search-input").value = "";
      refreshRecords();
      document.getElementById("records-section").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  // ---------------------------------------------------------------------
  // Journal des imports
  // ---------------------------------------------------------------------
  async function refreshUploads() {
    const res = await apiFetch("/api/uploads");
    const rows = await res.json();
    const tbody = document.querySelector("#uploads-table tbody");
    tbody.innerHTML = "";
    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state">Aucun import pour le moment.</td></tr>`;
    }
    for (const u of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(u.filename)}</td>
        <td>${escapeHtml(u.uploaded_at)}</td>
        <td class="num">${u.n_parsed}</td>
        <td class="num">${u.n_new}</td>
        <td class="num">${u.n_reconfirmed}</td>
        <td class="num">${u.n_missing_flagged}</td>
        <td class="num">${u.needs_review_count}</td>
      `;
      tbody.appendChild(tr);
    }
  }

  // ---------------------------------------------------------------------
  // Liste des banques (pour le filtre)
  // ---------------------------------------------------------------------
  async function refreshBanksFilter() {
    const res = await apiFetch("/api/banks");
    const banks = await res.json();
    const select = document.getElementById("filter-banque");
    const current = select.value;
    select.innerHTML = `<option value="">Toutes banques</option>` +
      banks.map((b) => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join("");
    select.value = current;
  }

  function refreshPeriodData() {
    refreshSummary();
    refreshRecords();
  }

  // ---------------------------------------------------------------------
  // Import de fichier (drag & drop + sélection)
  // ---------------------------------------------------------------------
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const idleView = document.getElementById("dropzone-idle");
  const busyView = document.getElementById("dropzone-busy");
  const resultBox = document.getElementById("upload-result");

  dropzone.addEventListener("click", () => fileInput.click());
  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); })
  );
  dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) uploadFile(fileInput.files[0]);
    fileInput.value = "";
  });

  async function uploadFile(file) {
    idleView.hidden = true;
    busyView.hidden = false;
    resultBox.hidden = true;

    const form = new FormData();
    form.append("file", file);

    try {
      const res = await apiFetch("/api/upload", { method: "POST", body: form });
      const data = await res.json();
      idleView.hidden = false;
      busyView.hidden = true;
      resultBox.hidden = false;
      if (!res.ok) {
        resultBox.className = "upload-result error";
        resultBox.textContent = data.error || "Erreur lors de l'import.";
        return;
      }
      resultBox.className = "upload-result ok";
      resultBox.innerHTML =
        `<strong>${file.name}</strong> importé — ` +
        `${data.n_parsed} ligne(s) analysée(s), ` +
        `<strong>${data.n_new}</strong> nouveau(x), ` +
        `<strong>${data.n_reconfirmed}</strong> déjà connu(s), ` +
        `<strong>${data.n_missing_flagged}</strong> disparu(s) signalé(s), ` +
        `<strong>${data.needs_review_count}</strong> ligne(s) à vérifier.`;
      refreshAll();
    } catch (err) {
      idleView.hidden = false;
      busyView.hidden = true;
      resultBox.hidden = false;
      resultBox.className = "upload-result error";
      resultBox.textContent = "Erreur réseau lors de l'import : " + err.message;
    }
  }

  // ---------------------------------------------------------------------
  // Utilitaire
  // ---------------------------------------------------------------------
  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function refreshAll() {
    refreshSummary();
    refreshAnomalies();
    refreshUploads();
    refreshBanksFilter();
    refreshRecords();
    if (currentUser && currentUser.role === "admin") {
      refreshUsers();
      refreshAuditLog();
    }
  }

  // ---------------------------------------------------------------------
  // Utilisateur connecté / déconnexion
  // ---------------------------------------------------------------------
  document.getElementById("logout-btn").addEventListener("click", async () => {
    await apiFetch("/api/logout", { method: "POST" }).catch(() => {});
    window.location.href = "/login";
  });

  function applyCurrentUser(user) {
    currentUser = user;
    document.getElementById("user-info").textContent =
      `${user.username} · ${user.role === "admin" ? "Administrateur" : "Utilisateur"}`;
    document.getElementById("admin-section").hidden = user.role !== "admin";
  }

  // ---------------------------------------------------------------------
  // Administration — comptes utilisateurs
  // ---------------------------------------------------------------------
  const ROLE_LABELS = { admin: "Administrateur", user: "Utilisateur" };

  async function refreshUsers() {
    const res = await apiFetch("/api/admin/users");
    if (!res.ok) return;
    const users = await res.json();
    const tbody = document.querySelector("#users-table tbody");
    tbody.innerHTML = "";
    for (const u of users) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(u.username)}</td>
        <td>${ROLE_LABELS[u.role] || escapeHtml(u.role)}</td>
        <td>${escapeHtml((u.created_at || "").slice(0, 10))}</td>
        <td></td>
      `;
      const actionsTd = tr.querySelector("td:last-child");

      const resetBtn = document.createElement("button");
      resetBtn.className = "btn-secondary btn-small";
      resetBtn.textContent = "Nouveau mot de passe";
      resetBtn.addEventListener("click", async () => {
        const pw = window.prompt(`Nouveau mot de passe pour ${u.username} (8 caractères min.) :`);
        if (!pw) return;
        const r = await apiFetch(`/api/admin/users/${u.id}/reset-password`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: pw }),
        });
        const data = await r.json();
        if (!r.ok) { window.alert(data.error || "Erreur."); return; }
        window.alert(`Mot de passe mis à jour pour ${u.username}.`);
      });
      actionsTd.appendChild(resetBtn);

      const delBtn = document.createElement("button");
      delBtn.className = "btn-secondary btn-small btn-danger-text";
      delBtn.textContent = "Supprimer";
      delBtn.addEventListener("click", async () => {
        if (!window.confirm(`Supprimer définitivement le compte ${u.username} ?`)) return;
        const r = await apiFetch(`/api/admin/users/${u.id}`, { method: "DELETE" });
        const data = await r.json();
        if (!r.ok) { window.alert(data.error || "Erreur."); return; }
        if (currentUser && u.username === currentUser.username) {
          window.location.href = "/login";
          return;
        }
        refreshUsers();
        refreshAuditLog();
      });
      actionsTd.appendChild(delBtn);

      tbody.appendChild(tr);
    }
  }

  document.getElementById("create-user-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = document.getElementById("user-form-error");
    errorEl.hidden = true;
    const username = document.getElementById("new-username").value.trim();
    const password = document.getElementById("new-password").value;
    const role = document.getElementById("new-role").value;
    const res = await apiFetch("/api/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.error || "Erreur lors de la création du compte.";
      errorEl.hidden = false;
      return;
    }
    document.getElementById("create-user-form").reset();
    refreshUsers();
    refreshAuditLog();
  });

  // ---------------------------------------------------------------------
  // Administration — journal d'audit
  // ---------------------------------------------------------------------
  async function refreshAuditLog() {
    const res = await apiFetch("/api/admin/audit-log");
    if (!res.ok) return;
    const rows = await res.json();
    const tbody = document.querySelector("#audit-table tbody");
    tbody.innerHTML = "";
    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-state">Aucune entrée.</td></tr>`;
    }
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml((r.created_at || "").replace("T", " ").slice(0, 19))}</td>
        <td>${escapeHtml(r.actor)}</td>
        <td>${escapeHtml(r.action)}</td>
        <td>${escapeHtml(r.detail || "")}</td>
      `;
      tbody.appendChild(tr);
    }
  }

  // ---------------------------------------------------------------------
  // Administration — réinitialisation complète de la base
  // ---------------------------------------------------------------------
  const resetModal = document.getElementById("reset-modal-backdrop");
  document.getElementById("open-reset-modal").addEventListener("click", () => {
    document.getElementById("reset-motif").value = "";
    document.getElementById("reset-phrase").value = "";
    document.getElementById("reset-password").value = "";
    document.getElementById("reset-modal-error").hidden = true;
    resetModal.hidden = false;
  });
  function closeResetModal() { resetModal.hidden = true; }
  document.getElementById("reset-modal-cancel").addEventListener("click", closeResetModal);
  resetModal.addEventListener("click", (e) => { if (e.target === resetModal) closeResetModal(); });

  document.getElementById("reset-modal-confirm").addEventListener("click", async () => {
    const motif = document.getElementById("reset-motif").value.trim();
    const phrase = document.getElementById("reset-phrase").value.trim();
    const password = document.getElementById("reset-password").value;
    const errorEl = document.getElementById("reset-modal-error");
    errorEl.hidden = true;

    const res = await apiFetch("/api/admin/reset-database", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ motif, phrase, password }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.error || "Erreur lors de la réinitialisation.";
      errorEl.hidden = false;
      return;
    }
    closeResetModal();
    window.alert("La base de données a été réinitialisée. Vous pouvez maintenant réimporter les fichiers depuis le début.");
    refreshAll();
  });

  // ---------------------------------------------------------------------
  // Démarrage : vérifie la session, affiche l'utilisateur, puis charge les
  // données (période par défaut = exercice en cours)
  // ---------------------------------------------------------------------
  document.querySelector('#preset-buttons button[data-preset="year"]').classList.add("active");
  const initial = presetRange("year");
  currentRange = initial;
  document.getElementById("range-start").value = initial.start || "";
  document.getElementById("range-end").value = initial.end || "";
  document.getElementById("period-label").textContent = `du ${initial.start} au ${initial.end}`;

  (async function boot() {
    try {
      const res = await apiFetch("/api/me");
      const user = await res.json();
      applyCurrentUser(user);
    } catch (err) {
      return; // apiFetch a déjà redirigé vers /login
    }
    refreshAll();
  })();
})();
