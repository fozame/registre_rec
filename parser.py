"""
parser.py — Lecture et fiabilisation des fichiers Excel hebdomadaires de recouvrement.

C'est la SEULE implémentation de la logique d'analyse (contrairement à la version
prototype sur claude.ai, qui devait dupliquer cette logique en JavaScript côté
navigateur). Ici, tout le parsing se fait sur le serveur, une seule fois, en Python.

Hypothèses sur le fichier source (feuille "TRAIT R" ou équivalente) :
  colonne B : date de l'opération (ou, par erreur de saisie, le libellé d'un bloc
              banque/mois, ou une date tapée en texte et mal formée)
  colonne C : libellé de l'opération
  colonne D : raison sociale / payeur
  colonne F : montant
  colonne G : montant recouvré (peut être vide -> traité comme 0, et signalé)
  colonne L : numéro de facture

Le fichier étant rempli à la main chaque semaine par une autre direction, il contient
des libellés de blocs mal orthographiés ("BANQUE ATLANNTIQUE"), des dates tapées en
texte et mal formées ("28/072026"), des lignes dont l'année ne correspond pas au bloc
dans lequel elles se trouvent, etc. Toutes ces anomalies sont détectées et signalées
(needs_review / review_reason) plutôt que silencieusement ignorées ou mal comptées.
"""

import re
from datetime import datetime, date

import openpyxl
import pandas as pd

# ---------------------------------------------------------------------------
# Normalisation des noms de banque (tolère les fautes de frappe courantes)
# ---------------------------------------------------------------------------
BANK_ALIASES = [
    (re.compile(r"ATLAN", re.I), "Banque Atlantique"),
    (re.compile(r"ACCESS", re.I), "Access Bank"),
    (re.compile(r"\bSCBC?\b", re.I), "SCB Cameroun"),
    (re.compile(r"BICEC", re.I), "BICEC"),
    (re.compile(r"ECOBANK", re.I), "Ecobank"),
    (re.compile(r"\bUBA\b", re.I), "UBA"),
]

MONTHS = {
    "JANVIER": 1, "FEVRIER": 2, "FÉVRIER": 2, "MARS": 3, "AVRIL": 4, "MAI": 5,
    "JUIN": 6, "JUILLET": 7, "AOUT": 8, "AOÛT": 8, "SEPTEMBRE": 9,
    "OCTOBRE": 10, "NOVEMBRE": 11, "DECEMBRE": 12, "DÉCEMBRE": 12,
}

DEFAULT_SHEET_NAMES = ("TRAIT R",)


def normalize_bank(label):
    if not label:
        return "Banque non identifiée"
    for pat, canon in BANK_ALIASES:
        if pat.search(label):
            return canon
    return label.strip().title()


def looks_like_label(text):
    """True si ce texte ressemble à un libellé de bloc (banque/mois) plutôt qu'à
    une date mal saisie : présence d'au moins 3 lettres consécutives."""
    return bool(re.search(r"[A-Za-zÀ-ÿ]{3,}", text))


def parse_block_period(label):
    """Extrait (année, mois) d'un libellé de bloc du type 'BICEC (MAI 2026)'."""
    if not label:
        return None
    u = label.upper()
    m = re.search(r"([A-ZÉÛÎÔ]+)\s*(\d{4})", u)
    if not m:
        return None
    month_word, year_s = m.group(1), m.group(2)
    month = MONTHS.get(month_word)
    if not month:
        return None
    return int(year_s), month


def try_parse_garbled_date(text, fallback_date):
    """Reconstruction au mieux d'une date mal tapée ('2701/2026', '17/041/2026',
    '28/072026'). Renvoie (date, ok) — ok=False si la reconstruction a échoué et
    que la date de la ligne précédente a été reprise à la place."""
    digits = re.sub(r"\D", "", text)
    m = re.search(r"(\d{4})$", digits)
    year = None
    rest = digits
    if m:
        year = int(m.group(1))
        rest = digits[:-4]
    if year and 2020 <= year <= 2030 and len(rest) in (3, 4):
        rest = rest.zfill(4)
        day = int(rest[:2])
        month = int(rest[2:4])
        try:
            if 1 <= month <= 12 and 1 <= day <= 31:
                return date(year, month, day), True
        except ValueError:
            pass
    return fallback_date, False


def normalize_text(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def classify_operateur(raison_sociale):
    u = raison_sociale.upper()
    if "ORANGE" in u:
        return "Orange"
    if "MTN" in u:
        return "MTN"
    return "Autre"


def _segment(s, maxlen):
    s = re.sub(r"[^A-Z0-9]+", "-", str(s).upper()).strip("-")
    return s[:maxlen] if s else "X"


def make_base_key(d, raison, montant, facture, libelle):
    """Clé stable identifiant un paiement, indépendamment de sa position dans le
    fichier. Deux fichiers hebdomadaires qui contiennent la même opération
    produiront la même clé, ce qui permet la détection cumulative
    (nouveau / déjà vu / disparu)."""
    parts = [
        _segment(d.isoformat(), 10),
        _segment(raison, 30),
        _segment(int(round(montant)), 15),
        _segment(facture, 20),
        _segment(libelle, 30),
    ]
    return "_".join(parts)


def _pick_sheet(wb, sheet):
    if sheet:
        if sheet in wb.sheetnames:
            return sheet
        for name in wb.sheetnames:
            if name.strip().upper() == sheet.strip().upper():
                return name
        raise ValueError(
            f"La feuille '{sheet}' est introuvable dans ce classeur. "
            f"Feuilles disponibles : {', '.join(wb.sheetnames)}"
        )
    for candidate in DEFAULT_SHEET_NAMES:
        for name in wb.sheetnames:
            if name.strip().upper() == candidate.upper():
                return name
    # dernier recours : la première feuille du classeur
    return wb.sheetnames[0]


def parse_file(path, sheet=None):
    """Analyse un fichier Excel hebdomadaire et renvoie un DataFrame pandas avec
    une ligne par paiement détecté (colonnes : date, libelle, raison_sociale,
    montant, recouvrement_declare, recouvrement_utilise, banque, block_label_raw,
    categorie, numero_facture, needs_review, review_reason, key)."""
    wb = openpyxl.load_workbook(path, data_only=True)
    sheet_name = _pick_sheet(wb, sheet)
    ws = wb[sheet_name]

    rows = []
    current_label = None
    current_bank = "Banque non identifiée"
    last_valid_date = date.today()

    for r in range(1, ws.max_row + 1):
        b = ws.cell(row=r, column=2).value
        c = ws.cell(row=r, column=3).value
        d_ = ws.cell(row=r, column=4).value
        f = ws.cell(row=r, column=6).value
        g = ws.cell(row=r, column=7).value
        l = ws.cell(row=r, column=12).value

        row_date = None
        needs_review = False
        review_reason = None

        if isinstance(b, str):
            bs = b.strip()
            if bs == "":
                continue
            if bs.upper() in ("DATE", "DATES"):
                continue  # ligne d'en-tête répétée
            if bs.upper().startswith("TOTAL"):
                continue  # ligne de sous-total
            if looks_like_label(bs):
                current_label = bs
                current_bank = normalize_bank(bs)
                continue
            else:
                row_date, ok = try_parse_garbled_date(bs, last_valid_date)
                needs_review = True
                review_reason = f"date saisie en texte non reconnue ('{bs}'), " + (
                    "reconstituée automatiquement — à vérifier" if ok else
                    "reprise de la date de la ligne précédente — À VÉRIFIER MANUELLEMENT")
        elif isinstance(b, datetime):
            row_date = b.date()
            last_valid_date = row_date
        else:
            continue  # ligne vide / non pertinente

        if row_date is None:
            continue
        if row_date.year < 2020 or row_date.year > 2027:
            needs_review = True
            review_reason = (review_reason or "") + f" | date suspecte ({row_date.isoformat()}), hors plage attendue"
        block_period = parse_block_period(current_label)
        if block_period and (row_date.year, row_date.month) != block_period:
            needs_review = True
            review_reason = (review_reason or "") + (
                f" | date incohérente avec le bloc ('{current_label}' attend "
                f"{block_period[1]}/{block_period[0]}, ligne datée {row_date.isoformat()})")

        montant = f if isinstance(f, (int, float)) else None
        recouvrement = g if isinstance(g, (int, float)) else None
        if montant is None:
            continue  # pas une ligne de transaction réelle

        if recouvrement is None:
            needs_review = True
            review_reason = (review_reason or "") + " | colonne Recouvrement vide"
            recouvrement_used = 0
        else:
            recouvrement_used = recouvrement

        raison = normalize_text(d_)
        libelle = normalize_text(c)
        facture = normalize_text(l)

        rows.append({
            "date": row_date,
            "libelle": libelle,
            "raison_sociale": raison,
            "montant": float(montant),
            "recouvrement_declare": None if recouvrement is None else float(recouvrement),
            "recouvrement_utilise": float(recouvrement_used),
            "banque": current_bank,
            "block_label_raw": current_label,
            "categorie": classify_operateur(raison),
            "numero_facture": facture,
            "needs_review": needs_review,
            "review_reason": review_reason,
        })

    df = pd.DataFrame(rows, columns=[
        "date", "libelle", "raison_sociale", "montant", "recouvrement_declare",
        "recouvrement_utilise", "banque", "block_label_raw", "categorie",
        "numero_facture", "needs_review", "review_reason",
    ])
    if len(df):
        df["base_key"] = df.apply(
            lambda row: make_base_key(
                row["date"], row["raison_sociale"], row["montant"],
                row["numero_facture"], row["libelle"]),
            axis=1,
        )
        occ = {}
        keys = []
        for bk in df["base_key"]:
            occ[bk] = occ.get(bk, 0) + 1
            keys.append(f"{bk}-{occ[bk]}")
        df["key"] = keys
        df = df.drop(columns=["base_key"])
    return df, sheet_name
