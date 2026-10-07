"""
xlsx_export.py — Génération de classeurs Excel à la volée pour les exports
(panneau "À vérifier" et détail des paiements), afin d'être exploités par les
collègues en dehors de l'outil (tableaux croisés, transmission, archivage).
"""

import io
from datetime import datetime, date

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="0B3D7A")   # bleu ART
HEADER_FONT = Font(color="FFFFFF", bold=True)
MONEY_FORMAT = "#,##0"


COLUMNS_RECORDS = [
    ("date", "Date", 12, "date"),
    ("raison_sociale", "Raison sociale", 34, "text"),
    ("libelle", "Libellé", 40, "text"),
    ("banque", "Banque", 18, "text"),
    ("categorie", "Catégorie", 12, "text"),
    ("numero_facture", "N° facture", 20, "text"),
    ("facture_rapprochee", "N° facture rapprochée", 20, "text"),
    ("montant", "Montant facturé", 16, "money"),
    ("recouvrement_declare", "Recouvrement déclaré", 18, "money"),
    ("recouvrement_utilise", "Recouvrement retenu", 18, "money"),
    ("status", "Statut", 18, "text"),
    ("needs_review", "À vérifier", 10, "bool"),
    ("review_reason", "Motif / anomalie", 50, "text"),
    ("confirmed_motif", "Motif de confirmation", 40, "text"),
    ("confirmed_by", "Confirmé par", 16, "text"),
    ("confirmed_at", "Confirmé le", 20, "text"),
    ("first_seen_upload", "Premier import", 30, "text"),
    ("last_seen_upload", "Dernier import", 30, "text"),
    ("key", "Clé technique", 40, "text"),
]

STATUS_LABELS = {
    "actif": "Actif",
    "a_verifier_disparu": "À vérifier (disparu)",
    "annule_confirme": "Annulé (confirmé)",
    "remplace": "Remplacé (version corrigée)",
    "rapproche": "Rapproché (même paiement)",
}

STATUS_COUNTED = {"actif"}


def _cell_value(row, field, kind):
    v = row.get(field)
    if kind == "date":
        if not v:
            return None
        try:
            return datetime.strptime(v, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return v
    if kind == "bool":
        return "Oui" if v else "Non"
    if kind == "text" and field == "status":
        return STATUS_LABELS.get(v, v)
    return v


def build_records_workbook(rows, title="Paiements", subtitle=None):
    """rows: liste de dict (sortie de storage.get_records / get_anomalies)."""
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]

    start_row = 1
    if subtitle:
        ws.cell(row=1, column=1, value=subtitle).font = Font(italic=True, size=10, color="51687F")
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(COLUMNS_RECORDS))
        start_row = 3

    header_row = start_row
    for col_idx, (field, label, width, kind) in enumerate(COLUMNS_RECORDS, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=label)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    for r_idx, row in enumerate(rows, start=header_row + 1):
        for col_idx, (field, label, width, kind) in enumerate(COLUMNS_RECORDS, start=1):
            cell = ws.cell(row=r_idx, column=col_idx, value=_cell_value(row, field, kind))
            if kind == "money":
                cell.number_format = MONEY_FORMAT
            if kind == "date":
                cell.number_format = "dd/mm/yyyy"

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    if rows:
        ws.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(len(COLUMNS_RECORDS))}{header_row + len(rows)}"
        )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def export_filename(prefix):
    return f"{prefix}_{date.today().isoformat()}.xlsx"


# ===========================================================================
# BD FINALE DU RECOUVREMENT — classeur complet multi-onglets
# ===========================================================================
from openpyxl.styles import Border, Side  # noqa: E402

import rapprochement as rap  # noqa: E402
import storage  # noqa: E402

WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
FILL_OK = PatternFill("solid", fgColor="E3F2EC")
FILL_WARN = PatternFill("solid", fgColor="FFF3DC")
FILL_BAD = PatternFill("solid", fgColor="FBE4E1")
FILL_GREY = PatternFill("solid", fgColor="EEEEEE")
CONF_FILL = {"forte": FILL_OK, "moyenne": FILL_WARN, "faible": FILL_BAD}


def _sheet(wb, title, columns, rows, subtitle=None, first=False):
    """columns : [(libellé, largeur, type)] ; rows : listes de valeurs.
    Renvoie la feuille. type ∈ text|money|date|wrap."""
    ws = wb.active if first else wb.create_sheet()
    ws.title = title[:31]
    r0 = 1
    if subtitle:
        ws.cell(row=1, column=1, value=subtitle).font = Font(italic=True, size=10, color="51687F")
        r0 = 3
    for i, (label, width, kind) in enumerate(columns, start=1):
        c = ws.cell(row=r0, column=i, value=label)
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    for ri, row in enumerate(rows, start=r0 + 1):
        for ci, ((label, width, kind), v) in enumerate(zip(columns, row), start=1):
            if kind == "date" and isinstance(v, str) and v:
                try:
                    v = datetime.strptime(v[:10], "%Y-%m-%d").date()
                except ValueError:
                    pass
            c = ws.cell(row=ri, column=ci, value=v)
            if kind == "money":
                c.number_format = MONEY_FORMAT
            elif kind == "date":
                c.number_format = "dd/mm/yyyy"
            c.alignment = WRAP if kind == "wrap" else TOP
    ws.freeze_panes = ws.cell(row=r0 + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{r0}:{get_column_letter(len(columns))}{r0 + len(rows)}"
    ws._r0 = r0
    return ws


def _fill_row(ws, row_idx, ncols, fill):
    for c in range(1, ncols + 1):
        ws.cell(row=row_idx, column=c).fill = fill


def _expl_name(r, names):
    return names.get(r.get("exploitant_key") or "__NI__", r.get("raison_sociale"))


def build_bd_finale(conn, exploitant=None):
    """Construit la BD finale. Si exploitant (clé) est fourni, tout le classeur
    est limité à cet exploitant. Renvoie (BytesIO, nom de l'exploitant)."""
    records = storage._all_records(conn)
    a = storage.analyse_rapprochement(conn, exploitant=exploitant)
    names = {e["key"]: e["nom"] for e in a["exploitants"]}
    nom_expl = names.get(exploitant) if exploitant else None
    by_key = {r["key"]: r for r in records}
    events = storage.events_by_key(conn)
    uploads = list(reversed(storage.get_uploads(conn)))  # ordre chronologique d'import
    up_period = {}
    for u in uploads:
        up_period[u["filename"]] = (u.get("periode_debut"), u.get("periode_fin"), u.get("fichier_modifie_le"))

    sel = [r for r in records if rap.matches_exploitant(r, exploitant)]
    sel.sort(key=lambda r: (r["date"], r["raison_sociale"] or ""))
    today = date.today().isoformat()
    scope = f"exploitant : {nom_expl}" if exploitant else "tous les exploitants"

    wb = Workbook()

    # ---------------------------------------------------------------- Lisez-moi
    ws = wb.active
    ws.title = "Lisez-moi"
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 110
    actifs = [r for r in sel if r["status"] == "actif"]
    lines = [
        ("BD finale du recouvrement", None),
        ("Généré le", today),
        ("Périmètre", scope),
        ("Fichiers compilés", f"{len(uploads)} import(s) — voir l'onglet « Imports »"),
        ("", None),
        ("Chiffres clés", None),
        ("Paiements comptés (actifs)", f"{len(actifs)} — {sum(r['recouvrement_utilise'] or 0 for r in actifs):,.0f} FCFA".replace(",", " ")),
        ("Actifs sans facture", f"{len(a['sans_facture'])} — {sum(r['recouvrement_utilise'] or 0 for r in a['sans_facture']):,.0f} FCFA".replace(",", " ")),
        ("Modifications appliquées/validées", str(sum(1 for d in a["decisions"] if d["type"] == "modification"))),
        ("Rapprochements validés", str(sum(1 for d in a["decisions"] if d["type"] == "rapprochement"))),
        ("Modifications à valider", str(len(a["modifications"]))),
        ("Rapprochements proposés", str(len(a["rapprochements"]))),
        ("Factures suspectes", str(len(a["factures_suspectes"]))),
        ("", None),
        ("Onglets", None),
        ("BD finale", "Tous les paiements jamais vus, avec leur statut, s'ils sont comptés, leur facture (y compris rapprochée), les fichiers d'où ils viennent et tout leur historique."),
        ("Sans facture", "Paiements comptés qui n'ont toujours pas de n° de facture valide (ni rapproché), triés par exploitant, avec la proposition de rapprochement éventuelle."),
        ("Rapprochements", "Paiement sans facture ↔ paiement avec facture du même exploitant et du même montant : validés et proposés."),
        ("Modifications", "Lignes corrigées d'un fichier à l'autre (facture ajoutée, payeur précisé, ventilation sur plusieurs factures, montant/date rectifiés) : ancienne valeur → nouvelle valeur."),
        ("Factures suspectes", "N° de facture mal attachés : référence de chèque, même facture pour plusieurs exploitants, facture répétée, facture changée entre deux fichiers."),
        ("Par exploitant", "Synthèse par exploitant (orthographes regroupées) : total, sans facture, rapprochés."),
        ("Imports", "Chaque fichier importé : période couverte par ses données, date de modification du fichier, nouveaux/disparus."),
        ("Historique", "Journal chronologique de tous les changements (quel paiement, quel fichier, quelle période, quel agent)."),
        ("", None),
        ("Statuts", None),
        ("Actif", "Présent dans le dernier fichier : compté dans les totaux."),
        ("À vérifier (disparu)", "Absent du dernier fichier, sans explication trouvée : non compté tant que ce n'est pas résolu."),
        ("Remplacé", "Ancienne version d'une ligne corrigée dans un fichier ultérieur : c'est la nouvelle version qui compte."),
        ("Rapproché", "Paiement sans facture reconnu comme le même paiement qu'une ligne avec facture : non compté (pas de double compte)."),
        ("Annulé (confirmé)", "Disparition confirmée par un agent avec un motif écrit : non compté."),
        ("", None),
        ("Confiance des propositions", None),
        ("Forte", "Même date + même exploitant + même montant (ou somme exacte) : appliquée automatiquement pour les modifications."),
        ("Moyenne / faible", "À valider par un agent dans l'outil (section « Rapprochement »)."),
    ]
    for i, (k, v) in enumerate(lines, start=1):
        ws.cell(row=i, column=1, value=k)
        if v is not None:
            ws.cell(row=i, column=2, value=v).alignment = Alignment(wrap_text=True, vertical="top")
        else:
            ws.cell(row=i, column=1).font = Font(bold=True, size=13 if i == 1 else 11, color="0B3D7A")

    # ---------------------------------------------------------------- BD finale
    proposals_by_old = {x["old"]["key"]: x for x in a["rapprochements"]}
    cols = [
        ("Date", 11, "date"), ("Exploitant (saisi)", 30, "text"), ("Exploitant (regroupé)", 26, "text"),
        ("Catégorie", 9, "text"), ("Libellé", 26, "text"), ("Banque", 16, "text"),
        ("N° facture (fichier)", 18, "text"), ("Facture conforme ?", 10, "text"),
        ("N° facture rapprochée", 18, "text"), ("Facture retenue", 18, "text"),
        ("Montant", 14, "money"), ("Recouvrement retenu", 14, "money"),
        ("Compté dans les totaux", 9, "text"), ("Statut", 18, "text"),
        ("Remplacé par / remplace", 30, "wrap"), ("Rapproché avec", 30, "wrap"),
        ("Anomalies / motif", 40, "wrap"), ("Motif de confirmation", 26, "wrap"),
        ("Premier fichier", 30, "text"), ("Période du premier fichier", 22, "text"),
        ("Dernier fichier", 30, "text"), ("Fichiers sources", 34, "wrap"),
        ("Historique des changements", 70, "wrap"), ("Clé technique", 30, "text"),
    ]

    def desc(k):
        r = by_key.get(k)
        if not r:
            return k
        return f"{r['date']} {r['raison_sociale']} {r['montant']:,.0f} {r['numero_facture'] or ''}".replace(",", " ").strip()

    rows = []
    for r in sel:
        fac_ret = (r["numero_facture"] if r["facture_ok"] else None) or r.get("facture_rapprochee")
        rempl = ""
        if r.get("remplace_par"):
            rempl = "Remplacé par : " + " ; ".join(desc(k) for k in r["remplace_par"].split(","))
        elif r.get("remplace_de"):
            rempl = "Remplace : " + desc(r["remplace_de"])
        rapp = ""
        if r.get("rapproche_avec"):
            rapp = desc(r["rapproche_avec"]) + (" (même paiement)" if r.get("rapprochement_mode") == "meme_paiement" else " (facture rattachée)")
        elif r["key"] in proposals_by_old:
            rapp = "PROPOSÉ : " + desc(proposals_by_old[r["key"]]["new"]["key"])
        anomalies = (r.get("review_reason") or "").strip(" |")
        if r["facture_issue"]:
            anomalies = (anomalies + " | " if anomalies else "") + "Facture : " + r["facture_issue"]
        p0 = up_period.get(r["first_seen_upload"], (None, None, None))
        rows.append([
            r["date"], r["raison_sociale"], _expl_name(r, names), r["categorie"], r["libelle"], r["banque"],
            r["numero_facture"], "Oui" if r["facture_ok"] else ("Non" if r["numero_facture"] else "Vide"),
            r.get("facture_rapprochee"), fac_ret,
            r["montant"], r["recouvrement_utilise"],
            "Oui" if r["status"] in STATUS_COUNTED else "Non", STATUS_LABELS.get(r["status"], r["status"]),
            rempl, rapp, anomalies, r.get("confirmed_motif"),
            r["first_seen_upload"], f"{p0[0] or '?'} → {p0[1] or '?'}" if p0[0] else "",
            r["last_seen_upload"], "\n".join(r["sources"]),
            storage.format_history(events.get(r["key"], [])), r["key"],
        ])
    ws = _sheet(wb, "BD finale", cols, rows, subtitle=f"BD finale du recouvrement — {scope} — générée le {today}")
    for i, r in enumerate(sel, start=ws._r0 + 1):
        if r["status"] != "actif":
            _fill_row(ws, i, len(cols), FILL_GREY)
        elif r.get("needs_review") or r["facture_issue"]:
            _fill_row(ws, i, len(cols), FILL_WARN)

    # ---------------------------------------------------------------- Sans facture
    sans = sorted(a["sans_facture"], key=lambda r: (_expl_name(r, names) or "", r["date"]))
    cols = [("Exploitant (regroupé)", 28, "text"), ("Exploitant (saisi)", 30, "text"), ("Date", 11, "date"),
            ("Libellé", 26, "text"), ("Banque", 16, "text"), ("Colonne facture", 20, "text"),
            ("Montant", 14, "money"), ("Recouvrement", 14, "money"),
            ("Proposition de rapprochement", 50, "wrap"), ("Confiance", 10, "text"),
            ("Premier fichier", 28, "text"), ("Dernier fichier", 28, "text")]
    rows = []
    for r in sans:
        p = proposals_by_old.get(r["key"])
        rows.append([_expl_name(r, names), r["raison_sociale"], r["date"], r["libelle"], r["banque"],
                     r["numero_facture"], r["montant"], r["recouvrement_utilise"],
                     (f"{p['new']['numero_facture']} — {p['new']['date']} — {p['regle']}" if p else ""),
                     p["confiance"] if p else "", r["first_seen_upload"], r["last_seen_upload"]])
    ws = _sheet(wb, "Sans facture", cols, rows,
                subtitle=f"Paiements comptés sans n° de facture valide ni rapprochement — {scope}")
    for i, r in enumerate(sans, start=ws._r0 + 1):
        p = proposals_by_old.get(r["key"])
        if p:
            ws.cell(row=i, column=10).fill = CONF_FILL[p["confiance"]]

    # ---------------------------------------------------------------- Rapprochements
    cols = [("Statut", 12, "text"), ("Confiance", 10, "text"), ("Exploitant", 28, "text"),
            ("Montant", 14, "money"),
            ("Date sans facture", 11, "date"), ("Libellé sans facture", 24, "text"),
            ("Colonne facture (sans)", 18, "text"), ("Statut ligne sans facture", 18, "text"),
            ("Date avec facture", 11, "date"), ("N° facture", 18, "text"),
            ("Écart (jours)", 8, "text"), ("Explication", 50, "wrap"),
            ("Décision", 22, "text"), ("Décidé par", 14, "text"), ("Décidé le", 18, "text"),
            ("Clé sans facture", 30, "text"), ("Clé avec facture", 30, "text")]
    rows, fills = [], []
    for d in [x for x in a["decisions"] if x["type"] == "rapprochement"]:
        s_, f_ = d["old"], d["new"]
        rows.append(["Validé", "", _expl_name(s_, names), s_["montant"], s_["date"], s_["libelle"],
                     s_["numero_facture"], STATUS_LABELS.get(s_["status"], s_["status"]),
                     f_["date"], f_["numero_facture"], rap._days(s_["date"], f_["date"]), d.get("motif") or "",
                     "Même paiement" if d.get("mode") == "meme_paiement" else "Facture rattachée",
                     d["decided_by"], (d["decided_at"] or "")[:19].replace("T", " "), s_["key"], f_["key"]])
        fills.append(FILL_OK)
    # factures ajoutées lors d'une correction de ligne = rapprochement de fait
    for d in [x for x in a["decisions"] if x["type"] == "modification"]:
        o = d["old"]
        for n in d["news"]:
            if not rap.has_valid_facture(o) and n["facture_ok"]:
                rows.append(["Validé", d.get("confiance") or "", _expl_name(n, names), n["montant"], o["date"],
                             o["libelle"], o["numero_facture"], STATUS_LABELS.get(o["status"], o["status"]),
                             n["date"], n["numero_facture"], rap._days(o["date"], n["date"]),
                             "facture ajoutée dans un fichier ultérieur : " + (d.get("nature") or ""),
                             "Ligne corrigée", d["decided_by"], (d["decided_at"] or "")[:19].replace("T", " "),
                             o["key"], n["key"]])
                fills.append(FILL_OK)
    for x in a["rapprochements"]:
        s_, f_ = x["old"], x["new"]
        rows.append(["Proposé", x["confiance"], _expl_name(s_, names), s_["montant"], s_["date"], s_["libelle"],
                     s_["numero_facture"], STATUS_LABELS.get(s_["status"], s_["status"]),
                     f_["date"], f_["numero_facture"], x["ecart_jours"],
                     x["regle"] + (f" ({x['autres_candidats']} autre(s) candidat(s) possible(s))" if x["autres_candidats"] else ""),
                     "À valider dans l'outil", "", "", s_["key"], f_["key"]])
        fills.append(CONF_FILL[x["confiance"]])
    for m in a["modifications"]:
        o = m["old"]
        for n in m["news"]:
            if not rap.has_valid_facture(o) and n["facture_ok"]:
                rows.append(["Proposé", m["confiance"], _expl_name(n, names), n["montant"], o["date"], o["libelle"],
                             o["numero_facture"], STATUS_LABELS.get(o["status"], o["status"]),
                             n["date"], n["numero_facture"], rap._days(o["date"], n["date"]),
                             f"ligne corrigée ({m['regle']}) : {m['nature']}", "À valider dans l'outil",
                             "", "", o["key"], n["key"]])
                fills.append(CONF_FILL[m["confiance"]])
    order = sorted(range(len(rows)), key=lambda i: (rows[i][2] or "", str(rows[i][4])))
    rows, fills = [rows[i] for i in order], [fills[i] for i in order]
    ws = _sheet(wb, "Rapprochements", cols, rows,
                subtitle=f"Paiements sans facture ↔ paiements avec facture (même exploitant, même montant) — {scope}")
    for i, f in enumerate(fills, start=ws._r0 + 1):
        ws.cell(row=i, column=2).fill = f
        ws.cell(row=i, column=1).fill = f

    # ---------------------------------------------------------------- Modifications
    cols = [("Statut", 12, "text"), ("Confiance", 10, "text"), ("Exploitant", 28, "text"),
            ("Nature du changement", 55, "wrap"), ("Règle", 34, "wrap"),
            ("Ancienne ligne", 45, "wrap"), ("Nouvelle(s) ligne(s)", 45, "wrap"),
            ("Ancienne version vue dans", 30, "wrap"), ("Nouvelle version vue dans", 30, "wrap"),
            ("Décidé par", 14, "text"), ("Décidé le", 18, "text")]

    def line(r):
        return (f"{r['date']} | {r['raison_sociale']} | {r['montant']:,.0f} | "
                f"fact. {r['numero_facture'] or '—'} | {r['libelle']}").replace(",", " ")

    rows, fills = [], []
    for d in [x for x in a["decisions"] if x["type"] == "modification"]:
        rows.append(["Appliqué" if d["decided_by"] == "automatique" else "Validé", d.get("confiance"),
                     _expl_name(d["new"], names), d.get("nature"), d.get("regle"), line(d["old"]),
                     "\n".join(line(n) for n in d["news"]), "\n".join(d["old"]["sources"]),
                     "\n".join(sorted({s for n in d["news"] for s in n["sources"]})),
                     d["decided_by"], (d["decided_at"] or "")[:19].replace("T", " ")])
        fills.append(FILL_OK)
    for m in a["modifications"]:
        rows.append(["Proposé", m["confiance"], _expl_name(m["new"], names), m["nature"], m["regle"],
                     line(m["old"]), "\n".join(line(n) for n in m["news"]), "\n".join(m["old"]["sources"]),
                     "\n".join(sorted({s for n in m["news"] for s in n["sources"]})), "", ""])
        fills.append(CONF_FILL.get(m["confiance"], FILL_WARN))
    ws = _sheet(wb, "Modifications", cols, rows,
                subtitle=f"Lignes corrigées d'un fichier à l'autre — {scope}")
    for i, f in enumerate(fills, start=ws._r0 + 1):
        ws.cell(row=i, column=1).fill = f
        ws.cell(row=i, column=2).fill = f

    # ---------------------------------------------------------------- Factures suspectes
    cols = [("Gravité", 11, "text"), ("Type", 32, "text"), ("N° facture", 22, "text"),
            ("Détail", 50, "wrap"), ("Paiements concernés", 80, "wrap")]
    rows = [[f["gravite"], f["type"], f["numero_facture"], f["detail"],
             "\n".join(line(r) + f" [{STATUS_LABELS.get(r['status'], r['status'])}]" for r in f["records"])]
            for f in a["factures_suspectes"]]
    ws = _sheet(wb, "Factures suspectes", cols, rows,
                subtitle=f"N° de facture mal attachés ou à vérifier — {scope}")
    for i, f in enumerate(a["factures_suspectes"], start=ws._r0 + 1):
        ws.cell(row=i, column=1).fill = FILL_BAD if f["gravite"] == "à corriger" else FILL_WARN

    # ---------------------------------------------------------------- Par exploitant
    exps = [e for e in a["exploitants"] if not exploitant or e["key"] == exploitant]
    cols = [("Exploitant", 34, "text"), ("Catégorie", 10, "text"), ("Orthographes trouvées", 60, "wrap"),
            ("Nb paiements comptés", 10, "text"), ("Total recouvré", 16, "money"),
            ("Nb sans facture", 10, "text"), ("Montant sans facture", 16, "money"),
            ("Nb rapprochés", 10, "text")]
    rows = [[e["nom"], e["categorie"], " / ".join(e["orthographes"]), e["n_paiements"], e["total"],
             e["n_sans_facture"], e["montant_sans_facture"], e["n_rapproches"]] for e in exps]
    _sheet(wb, "Par exploitant", cols, rows, subtitle="Synthèse par exploitant (orthographes regroupées)")

    # ---------------------------------------------------------------- Imports
    cols = [("Ordre", 6, "text"), ("Fichier", 40, "text"), ("Importé le", 12, "text"), ("Par", 12, "text"),
            ("Fichier modifié le", 19, "text"), ("Modifié par", 16, "text"),
            ("Données du", 11, "date"), ("Données au", 11, "date"),
            ("Lignes", 8, "text"), ("Nouveaux", 9, "text"), ("Disparus", 9, "text"),
            ("Corrections auto.", 9, "text"), ("Avertissement", 60, "wrap")]
    rows = [[i, u["filename"], u["uploaded_at"], u.get("uploaded_by"), u.get("fichier_modifie_le") or "inconnu",
             u.get("fichier_modifie_par"), u.get("periode_debut") or u.get("date_min"),
             u.get("periode_fin") or u.get("date_max"), u["n_parsed"], u["n_new"], u["n_missing_flagged"],
             u.get("n_modifications_auto"), u.get("avertissement")] for i, u in enumerate(uploads, start=1)]
    _sheet(wb, "Imports", cols, rows, subtitle="Fichiers compilés, dans l'ordre d'import, avec la période réellement couverte")

    # ---------------------------------------------------------------- Historique
    keys = {r["key"] for r in sel}
    evs = [e for e in storage.get_events(conn) if e["key"] in keys]
    cols = [("Date de l'événement", 19, "text"), ("Fichier", 34, "text"), ("Période du fichier", 22, "text"),
            ("Événement", 26, "text"), ("Paiement", 50, "wrap"), ("Détail", 70, "wrap"),
            ("Par", 14, "text")]
    rows = []
    for e in evs:
        r = by_key.get(e["key"])
        rows.append([(e.get("created_at") or "")[:19].replace("T", " "), e.get("filename"),
                     f"{e['periode_debut']} → {e['periode_fin']}" if e.get("periode_debut") else "",
                     storage.EVENT_LABELS.get(e["event"], e["event"]), line(r) if r else e["key"],
                     e.get("detail"), e.get("actor")])
    _sheet(wb, "Historique", cols, rows, subtitle=f"Journal de tous les changements — {scope}")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf, nom_expl
