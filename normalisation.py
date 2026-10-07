"""
normalisation.py — Règles communes de normalisation (exploitants, opérateurs,
numéros de facture), utilisées à la fois par l'analyse des fichiers
(parser.py) et par le rapprochement (rapprochement.py).

Le fichier source étant saisi à la main, un même exploitant apparaît sous
plusieurs orthographes ("MATRIX TELECOM" / "MATRIX TELECOMS",
"CONSULT - IT CAMEROUN" / "CONSULT IT CAMEROUN SARL", "MOBILE TELEPHONE
NETWORKS" / "MTN CAMEROON"…). Pour comparer et regrouper, on calcule une
"clé exploitant" simplifiée ; le nom saisi d'origine est toujours conservé.
"""

import re
import unicodedata

# ---------------------------------------------------------------------------
# Opérateurs (catégorie Orange / MTN / Autre)
# ---------------------------------------------------------------------------
# "MOBILE TELEPH" couvre aussi la faute de frappe "MOBILE TELEPHON ENETWORKS".
# Attention : "AFRICA MOBILE NETWORKS" et "MOBILE NETWORKS CAMEROON" sont un
# AUTRE opérateur (Africa Mobile Networks) et ne doivent pas être classés MTN.
MTN_RE = re.compile(r"\bMTN\b|MOBILE\s*TELEPH", re.I)
ORANGE_RE = re.compile(r"\bORANGE\b", re.I)


def classify_operateur(raison_sociale):
    u = strip_accents(raison_sociale or "").upper()
    if MTN_RE.search(u):
        return "MTN"
    if ORANGE_RE.search(u):
        return "Orange"
    return "Autre"


# ---------------------------------------------------------------------------
# Exploitants
# ---------------------------------------------------------------------------
# Formes juridiques / mots sans valeur distinctive, retirés avant comparaison.
_STOPWORDS = {
    "SARL", "SARLU", "SA", "SAS", "SASU", "SCS", "GIE", "ETS", "ETABLISSEMENT",
    "ETABLISSEMENTS", "STE", "STES", "LTD", "LIMITED", "INC", "PLC", "LLC",
    "CAMEROUN", "CAMEROON", "CMR", "THE", "LE", "LA", "LES", "DE", "DU", "DES",
    "ET", "AND",
}

# Alias explicites : si le nom correspond au motif, la clé est imposée.
# (À compléter au besoin par l'équipe.)
_ALIASES = [
    (MTN_RE, "MTN"),
    (re.compile(r"^ORANGE( CAMEROUN)?( SA)?$", re.I), "ORANGE"),
]

# Payeur non identifié : on ne rapproche pas ces lignes entre elles par nom.
_NON_IDENTIFIE = {"", "NI", "N I", "NON IDENTIFIE", "NON IDENTIFIEE", "INCONNU",
                  "X", "XX", "XXX", "ND", "N D"}


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", str(s))
                   if not unicodedata.combining(c))


def _base(name):
    u = strip_accents(name or "").upper()
    u = re.sub(r"[^A-Z0-9]+", " ", u)
    return re.sub(r"\s+", " ", u).strip()


def is_non_identifie(name):
    return _base(name) in _NON_IDENTIFIE


def exploitant_key(name):
    """Clé simplifiée servant à regrouper les orthographes d'un même
    exploitant. Renvoie None pour un payeur non identifié ("NI")."""
    base = _base(name)
    if base in _NON_IDENTIFIE:
        return None
    for pat, canon in _ALIASES:
        if pat.search(base):
            return canon
    words = []
    for w in base.split(" "):
        if w in _STOPWORDS:
            continue
        # pluriel simple : TELECOMS -> TELECOM, NETWORKS -> NETWORK
        if len(w) > 4 and w.endswith("S") and not w.endswith("SS"):
            w = w[:-1]
        words.append(w)
    key = " ".join(words)
    return key or base


# ---------------------------------------------------------------------------
# Numéros de facture
# ---------------------------------------------------------------------------
# Format observé : 25A-HOM/Y/309, 26A-RARN/D/144, 26A-RPIP/Y/479bis, 25A-DR/D/529…
_INVOICE_RE = re.compile(r"^\s*\d{2}\s*[A-Z]\s*-?\s*[A-Z]{1,8}\s*/\s*[A-Z]\s*/?\s*\d+", re.I)
_CHEQUE_RE = re.compile(r"\bCH[EQ]|\bCHQ|CHEQUE|CHÈQUE", re.I)


def normalize_facture(f):
    return re.sub(r"\s+", "", strip_accents(f or "").upper())


def looks_like_invoice(f):
    return bool(f) and bool(_INVOICE_RE.match(strip_accents(f)))


def facture_issue(f):
    """Renvoie None si la colonne facture est vide ou conforme, sinon une
    explication courte (référence de chèque, texte libre…)."""
    if not f or not str(f).strip():
        return None
    if looks_like_invoice(f):
        return None
    if _CHEQUE_RE.search(strip_accents(f)):
        return "référence de chèque saisie à la place du n° de facture"
    return "référence non conforme au format des factures (ex. 26A-XXX/Y/123)"
