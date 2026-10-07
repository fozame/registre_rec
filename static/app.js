(function () {
  "use strict";

  const fmt = new Intl.NumberFormat("fr-FR");
  const fmtFCFA = (n) => `${fmt.format(Math.round(n || 0))} FCFA`;

  let currentRange = { start: null, end: null };
  const statusLabels = {
    actif: "Actif", a_verifier_disparu: "À vérifier", annule_confirme: "Annulé (confirmé)",
    remplace: "Remplacé (corrigé)", rapproche: "Rapproché",
  };
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
    refreshRapprochement();
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
    const exploitant = document.getElementById("filter-exploitant").value;
    if (exploitant) params.set("exploitant", exploitant);
    if (document.getElementById("filter-sans-facture").checked) params.set("sans_facture", "1");

    const res = await apiFetch(`/api/records?${params}`);
    const rows = await res.json();
    const tbody = document.querySelector("#records-table tbody");
    tbody.innerHTML = "";
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(r.date)}</td>
        <td>${escapeHtml(r.raison_sociale)}</td>
        <td>${escapeHtml(r.libelle)}</td>
        <td>${escapeHtml(r.banque)}</td>
        <td>${escapeHtml(r.numero_facture) || "—"}${r.facture_rapprochee ? `<br><span class="hint small">rapprochée : ${escapeHtml(r.facture_rapprochee)}</span>` : ""}${r.facture_issue ? `<br><span class="anomaly-reason">${escapeHtml(r.facture_issue)}</span>` : ""}</td>
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
  // Rapprochement & modifications entre fichiers
  // ---------------------------------------------------------------------
  async function refreshExploitants() {
    const res = await apiFetch("/api/exploitants");
    const list = await res.json();
    list.sort((a, b) => a.nom.localeCompare(b.nom, "fr"));
    for (const id of ["rap-exploitant", "filter-exploitant"]) {
      const sel = document.getElementById(id);
      const cur = sel.value;
      sel.innerHTML = `<option value="">Tous les exploitants</option>` + list.map((e) =>
        `<option value="${escapeHtml(e.key)}">${escapeHtml(e.nom)}` +
        `${e.n_sans_facture ? ` — ${e.n_sans_facture} sans facture` : ""}` +
        `${e.orthographes.length > 1 ? ` (${e.orthographes.length} orthographes)` : ""}</option>`).join("");
      sel.value = cur;
    }
  }

  function recLine(r, label, cls) {
    if (!r) return "";
    const fac = r.numero_facture ? `fact. <strong>${escapeHtml(r.numero_facture)}</strong>` : `<em>sans facture</em>`;
    return `<div class="rap-line ${cls}"><span class="lbl">${label}</span>
      ${escapeHtml(r.date)} · <strong>${escapeHtml(r.raison_sociale || "(sans nom)")}</strong> ·
      ${fmtFCFA(r.montant)} · ${fac}<br>
      <span class="hint small">${escapeHtml(r.libelle || "")} · ${escapeHtml(r.banque || "")} ·
      ${statusLabels[r.status] || escapeHtml(r.status)} · vu dans : ${escapeHtml((r.sources || []).join(", "))}</span></div>`;
  }

  async function postLien(url, body) {
    const res = await apiFetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { window.alert(data.error || "Erreur."); return false; }
    refreshRapprochement(); refreshAnomalies(); refreshSummary(); refreshRecords(); refreshExploitants();
    return true;
  }

  async function refreshRapprochement() {
    const exploitant = document.getElementById("rap-exploitant").value;
    const params = new URLSearchParams();
    if (exploitant) params.set("exploitant", exploitant);
    document.getElementById("export-bd-link").href = `/api/export/bd_finale.xlsx?${params}`;
    const res = await apiFetch(`/api/rapprochement?${params}`);
    const d = await res.json();

    document.getElementById("rap-chips").innerHTML = `
      <span class="rap-chip">Sans facture (comptés) : <strong>${d.sans_facture.count}</strong> — ${fmtFCFA(d.sans_facture.total)}</span>
      <span class="rap-chip">Décisions prises : <strong>${d.decisions.length}</strong></span>`;

    // --- lignes corrigées ---
    document.getElementById("rap-mod-count").textContent = d.modifications.length;
    const mods = document.getElementById("rap-mods");
    mods.innerHTML = d.modifications.length ? "" : `<div class="empty-state">Aucune correction en attente. ✓</div>`;
    d.modifications.forEach((m) => {
      const div = document.createElement("div");
      div.className = "anomaly-card";
      div.innerHTML = `
        <div class="row1"><span class="conf conf-${m.confiance}">Confiance ${m.confiance}</span>
          <span class="hint small">${escapeHtml(m.regle)}</span></div>
        <div class="rap-nature">${escapeHtml(m.nature)}</div>
        ${recLine(m.old, "Ancienne version (disparue)", "old")}
        ${m.news.map((n, i) => recLine(n, m.news.length > 1 ? `Nouvelle ligne ${i + 1}` : "Nouvelle version", "new")).join("")}
        <div class="rap-actions">
          <button class="ok">C'est la même opération corrigée</button>
          <button class="no">Ce n'est pas la même</button>
        </div>`;
      const keys = m.news.map((n) => n.key);
      div.querySelector(".ok").addEventListener("click", () =>
        postLien("/api/liens/valider", { type: "modification", key_from: m.old.key, keys_to: keys }));
      div.querySelector(".no").addEventListener("click", () =>
        postLien("/api/liens/rejeter", { type: "modification", key_from: m.old.key, keys_to: keys }));
      mods.appendChild(div);
    });

    // --- rapprochements ---
    document.getElementById("rap-rap-count").textContent = d.rapprochements.length;
    const raps = document.getElementById("rap-raps");
    raps.innerHTML = d.rapprochements.length ? "" : `<div class="empty-state">Aucun rapprochement proposé.</div>`;
    d.rapprochements.forEach((x) => {
      const div = document.createElement("div");
      div.className = "anomaly-card";
      div.innerHTML = `
        <div class="row1"><span class="conf conf-${x.confiance}">Confiance ${x.confiance}</span>
          <span class="amount">${fmtFCFA(x.old.montant)}</span></div>
        <div class="rap-nature">${escapeHtml(x.regle)}${x.autres_candidats ? ` — <strong>${x.autres_candidats}</strong> autre(s) paiement(s) avec facture possible(s)` : ""}</div>
        ${recLine(x.old, "Paiement sans facture", "old")}
        ${recLine(x.news[0], "Paiement avec facture", "new")}
        <div class="rap-actions">
          <button class="ok" data-mode="meme_paiement" title="La ligne sans facture n'est plus comptée (pas de double compte)">Même paiement</button>
          <button class="ok" data-mode="facture_seule" title="Les deux lignes restent comptées ; la facture est rattachée à la ligne sans facture">Rattacher la facture seulement</button>
          <button class="no">Rejeter</button>
        </div>`;
      div.querySelectorAll("button[data-mode]").forEach((b) => b.addEventListener("click", () =>
        postLien("/api/liens/valider", { type: "rapprochement", key_from: x.old.key, keys_to: [x.news[0].key], mode: b.dataset.mode })));
      div.querySelector(".no").addEventListener("click", () =>
        postLien("/api/liens/rejeter", { type: "rapprochement", key_from: x.old.key, keys_to: [x.news[0].key] }));
      raps.appendChild(div);
    });

    // --- factures suspectes ---
    document.getElementById("rap-fs-count").textContent = d.factures_suspectes.length;
    const tb = document.querySelector("#rap-fs-table tbody");
    tb.innerHTML = d.factures_suspectes.length ? "" : `<tr><td colspan="5" class="empty-state">Rien à signaler.</td></tr>`;
    d.factures_suspectes.forEach((f) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><span class="conf ${f.gravite === "à corriger" ? "conf-faible" : "conf-moyenne"}">${escapeHtml(f.gravite)}</span></td>
        <td>${escapeHtml(f.type)}</td><td>${escapeHtml(f.numero_facture)}</td><td>${escapeHtml(f.detail)}</td>
        <td class="hint small">${f.records.map((r) => `${escapeHtml(r.date)} · ${escapeHtml(r.raison_sociale)} · ${fmtFCFA(r.montant)}`).join("<br>")}</td>`;
      tb.appendChild(tr);
    });

    // --- décisions prises ---
    document.getElementById("rap-dec-count").textContent = d.decisions.length;
    const dec = document.getElementById("rap-decisions");
    dec.innerHTML = d.decisions.length ? "" : `<div class="empty-state">Aucune décision pour le moment.</div>`;
    d.decisions.slice().reverse().forEach((l) => {
      const div = document.createElement("div");
      div.className = "anomaly-card";
      const titre = l.type === "modification"
        ? (l.decided_by === "automatique" ? "Correction appliquée automatiquement" : "Correction validée")
        : (l.mode === "meme_paiement" ? "Rapprochement validé — même paiement" : "Rapprochement validé — facture rattachée");
      div.innerHTML = `
        <div class="row1"><strong>${titre}</strong>
          <span class="hint small">${escapeHtml(l.decided_by || "")} · ${escapeHtml((l.decided_at || "").slice(0, 16).replace("T", " "))}</span></div>
        <div class="rap-nature">${escapeHtml(l.nature || "")}</div>
        ${recLine(l.old, l.type === "modification" ? "Ancienne version" : "Paiement sans facture", "old")}
        ${l.news.map((n) => recLine(n, l.type === "modification" ? "Nouvelle version" : "Paiement avec facture", "new")).join("")}
        <div class="rap-actions"><button class="no">Annuler cette décision</button></div>`;
      div.querySelector(".no").addEventListener("click", () => {
        if (!window.confirm("Annuler cette décision ? Les paiements retrouvent leur statut précédent.")) return;
        postLien(`/api/liens/${l.id}/annuler`, {});
      });
      dec.appendChild(div);
    });
  }
  document.getElementById("rap-exploitant").addEventListener("change", refreshRapprochement);
  document.getElementById("filter-exploitant").addEventListener("change", refreshRecords);
  document.getElementById("filter-sans-facture").addEventListener("change", refreshRecords);

  // ---------------------------------------------------------------------
  // Journal des imports
  // ---------------------------------------------------------------------
  async function refreshUploads() {
    const res = await apiFetch("/api/uploads");
    const rows = await res.json();
    const tbody = document.querySelector("#uploads-table tbody");
    tbody.innerHTML = "";
    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty-state">Aucun import pour le moment.</td></tr>`;
    }
    for (const u of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(u.filename)}${u.avertissement ? `<div class="anomaly-reason">⚠ ${escapeHtml(u.avertissement)}</div>` : ""}</td>
        <td>${escapeHtml(u.uploaded_at)}</td>
        <td>${escapeHtml(u.fichier_modifie_le || "inconnu")}</td>
        <td>${u.periode_debut ? `${escapeHtml(u.periode_debut)} → ${escapeHtml(u.periode_fin)}` : `${escapeHtml(u.date_min || "")} → ${escapeHtml(u.date_max || "")}`}</td>
        <td class="num">${u.n_parsed}</td>
        <td class="num">${u.n_new}</td>
        <td class="num">${u.n_reconfirmed}</td>
        <td class="num">${u.n_missing_flagged}</td>
        <td class="num">${u.n_modifications_auto ?? "—"}</td>
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
        `<strong>${data.needs_review_count}</strong> ligne(s) à vérifier.` +
        `<br>Données du ${escapeHtml(data.periode_debut || "?")} au ${escapeHtml(data.periode_fin || "?")}` +
        (data.fichier_modifie_le ? ` — fichier modifié le ${escapeHtml(data.fichier_modifie_le)}` : "") + `. ` +
        `<strong>${data.n_modifications_auto}</strong> correction(s) appliquée(s) automatiquement, ` +
        `<strong>${data.n_modifications_a_valider}</strong> à valider, ` +
        `<strong>${data.n_rapprochements_proposes}</strong> rapprochement(s) proposé(s) — voir la section « Rapprochement ».` +
        (data.avertissement ? `<span class="warn">⚠ ${escapeHtml(data.avertissement)}</span>` : "");
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
    refreshExploitants();
    refreshRapprochement();
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
