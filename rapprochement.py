"""
rapprochement.py — Moteur de rapprochement et de suivi des modifications.

Objectif : quand on compile les fichiers hebdomadaires successifs, faire
ressortir automatiquement ce qui a changé et ce qui a été rapproché.

1) MODIFICATIONS ENTRE FICHIERS
   Un paiement présent dans un fichier mais absent d'un autre ("disparu") a
   très souvent été simplement CORRIGÉ par l'autre direction : n° de facture
   ajouté, nom du payeur précisé ("NI" -> "SCB", "NTI AKO/REASY" -> "REASY
   CAMEROUN SARL"), date ou montant rectifié… La ligne corrigée apparaît alors
   comme un "nouveau" paiement. On apparie les deux :
     - le paiement disparu (ancienne version)
     - un paiement actif qui n'a JAMAIS figuré dans le même fichier que lui
       (nouvelle version)
   par ordre de confiance :
     forte   : même date, même montant, même exploitant
     forte   : même date, même exploitant, plusieurs nouvelles lignes dont la
               somme = ancien montant (paiement ventilé sur plusieurs factures)
     moyenne : même date, même montant, payeur différent (ou "NI")
     moyenne : même exploitant, même montant, date différente (<= 45 jours)
     faible  : même date, même exploitant, montant différent
   Seules les modifications de confiance forte sont appliquées automatiquement
   (elles ne changent pas les totaux : l'ancienne version n'est déjà plus
   comptée). Les autres sont proposées à la validation d'un agent.

2) RAPPROCHEMENT FACTURE
   Un exploitant a un paiement SANS n° de facture, et un paiement AVEC n° de
   facture du même montant (en général dans un fichier ultérieur). On propose
   le rapprochement :
     forte   : le paiement sans facture a disparu des fichiers récents
     moyenne : paiement avec facture daté après le paiement sans facture
     faible  : paiement avec facture daté avant (jusqu'à 7 jours)
   Une colonne "facture" contenant une référence de chèque ou un texte libre
   est traitée comme "sans facture" (facture mal attachée).

3) FACTURES SUSPECTES (mal attachées)
   - référence non conforme (chèque, texte libre) dans la colonne facture ;
   - même n° de facture attaché à des exploitants différents ;
   - même n° de facture sur plusieurs paiements du même exploitant
     (souvent un paiement échelonné — à confirmer) ;
   - n° de facture modifié d'un fichier à l'autre.

Ce module ne touche pas à la base : il reçoit des listes de dict et renvoie
des propositions. Les décisions sont enregistrées par storage.py.
"""

from collections import Counter, defaultdict
from itertools import combinations
from datetime import date

from normalisation import (
    exploitant_key, is_non_identifie, looks_like_invoice, facture_issue,
    normalize_facture,
)

DIFF_FIELDS = [
    ("date", "Date"),
    ("raison_sociale", "Exploitant"),
    ("montant", "Montant"),
    ("recouvrement_utilise", "Recouvrement"),
    ("numero_facture", "N° facture"),
    ("libelle", "Libellé"),
    ("banque", "Banque"),
]

CONF_ORDER = {"forte": 0, "moyenne": 1, "faible": 2}


def _d(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _days(a, b):
    da, db = _d(a), _d(b)
    if da is None or db is None:
        return 9999
    return (db - da).days


def has_valid_facture(r):
    return looks_like_invoice(r.get("numero_facture") or "")


def enrich(records):
    """Ajoute à chaque enregistrement : exploitant_key, ni (non identifié),
    facture_ok, facture_issue. Modifie les dict en place et les renvoie."""
    for r in records:
        r["exploitant_key"] = exploitant_key(r.get("raison_sociale"))
        r["ni"] = is_non_identifie(r.get("raison_sociale"))
        r["facture_ok"] = has_valid_facture(r)
        r["facture_issue"] = facture_issue(r.get("numero_facture"))
        if not isinstance(r.get("sources"), (list, set, tuple)):
            r["sources"] = []
    return records


def diff_records(old, new):
    out = []
    for field, label in DIFF_FIELDS:
        a, b = old.get(field), new.get(field)
        if isinstance(a, float) or isinstance(b, float):
            if (a or 0) == (b or 0):
                continue
        elif (a or "") == (b or ""):
            continue
        out.append({"champ": label, "avant": a, "apres": b})
    return out


def describe_diff(diff):
    labels = []
    for d in diff:
        if d["champ"] == "N° facture":
            if not d["avant"]:
                labels.append(f"facture ajoutée ({d['apres']})")
            elif not d["apres"]:
                labels.append(f"facture retirée ({d['avant']})")
            else:
                labels.append(f"facture modifiée ({d['avant']} → {d['apres']})")
        elif d["champ"] == "Exploitant":
            labels.append(f"payeur modifié ({d['avant']} → {d['apres']})")
        elif d["champ"] in ("Montant", "Recouvrement"):
            labels.append(f"{d['champ'].lower()} modifié ({d['avant']:,.0f} → {d['apres']:,.0f})".replace(",", " "))
        elif d["champ"] == "Date":
            labels.append(f"date modifiée ({d['avant']} → {d['apres']})")
        else:
            labels.append(f"{d['champ'].lower()} modifié")
    return "; ".join(labels) if labels else "aucune différence visible"


def _same_expl(a, b):
    return a["exploitant_key"] is not None and a["exploitant_key"] == b["exploitant_key"]


# ---------------------------------------------------------------------------
# 1) Modifications entre fichiers
# ---------------------------------------------------------------------------
def propose_modifications(records, rejected_pairs=frozenset(), used_new=frozenset()):
    """records : liste enrichie (enrich). Renvoie une liste de propositions
    {old, news[], new, confiance, regle, diff, nature}. "news" contient
    plusieurs lignes quand un paiement a été ventilé sur plusieurs factures."""
    olds = [r for r in records if r["status"] == "a_verifier_disparu"]
    news = [r for r in records if r["status"] == "actif" and r["key"] not in used_new]
    if not olds or not news:
        return []

    def compatible(o, n):
        # la nouvelle version ne doit jamais avoir coexisté avec l'ancienne
        return (n["key"] not in taken_new and (o["key"], n["key"]) not in rejected_pairs
                and not (set(o["sources"]) & set(n["sources"])))

    def make(o, ns, conf, label):
        if len(ns) == 1:
            diff = diff_records(o, ns[0])
            nature = describe_diff(diff)
        else:
            diff = [{"champ": "Ventilation", "avant": o["montant"],
                     "apres": " + ".join(f"{n['montant']:,.0f}".replace(",", " ") for n in ns)}]
            facs = [n["numero_facture"] for n in ns if n["numero_facture"]]
            nature = (f"paiement ventilé en {len(ns)} lignes "
                      f"({diff[0]['apres']} = {o['montant']:,.0f})".replace(",", " "))
            if facs:
                nature += " ; factures : " + ", ".join(facs)
        return {"type": "modification", "old": o, "news": ns, "new": ns[0],
                "confiance": conf, "regle": label, "diff": diff, "nature": nature}

    rules = [
        ("forte", "même date, même montant, même exploitant",
         lambda o, n: o["date"] == n["date"] and o["montant"] == n["montant"] and _same_expl(o, n)),
        ("ventilation", None, None),
        ("moyenne", "même date et même montant, payeur différent",
         lambda o, n: o["date"] == n["date"] and o["montant"] == n["montant"]),
        ("moyenne", "même exploitant et même montant, date différente",
         lambda o, n: _same_expl(o, n) and o["montant"] == n["montant"] and abs(_days(o["date"], n["date"])) <= 45),
        ("faible", "même date et même exploitant, montant différent",
         lambda o, n: o["date"] == n["date"] and _same_expl(o, n)),
    ]

    taken_old, taken_new = set(), set()
    out = []
    for conf, label, rule in rules:
        if conf == "ventilation":
            # un paiement disparu remplacé par plusieurs lignes du même jour,
            # même exploitant, dont la somme est exactement l'ancien montant
            for o in olds:
                if o["key"] in taken_old:
                    continue
                pool = [n for n in news if compatible(o, n) and n["date"] == o["date"]
                        and (_same_expl(o, n) or o["ni"]) and n["montant"] < o["montant"]][:12]
                found = None
                for size in (2, 3, 4):
                    for combo in combinations(pool, size):
                        if abs(sum(n["montant"] for n in combo) - o["montant"]) < 0.5:
                            found = combo
                            break
                    if found:
                        break
                if found:
                    taken_old.add(o["key"])
                    taken_new.update(n["key"] for n in found)
                    out.append(make(o, list(found), "forte",
                                    "même date, même exploitant, somme des nouvelles lignes = ancien montant"))
            continue
        cands = []
        for o in olds:
            if o["key"] in taken_old:
                continue
            for n in news:
                if compatible(o, n) and rule(o, n):
                    cands.append((abs(_days(o["date"], n["date"])), o["key"], n["key"], o, n))
        cands.sort(key=lambda c: (c[0], c[1], c[2]))
        for _, ok, nk, o, n in cands:
            if ok in taken_old or nk in taken_new:
                continue
            taken_old.add(ok)
            taken_new.add(nk)
            out.append(make(o, [n], conf, label))
    return out


# ---------------------------------------------------------------------------
# 2) Rapprochement paiement sans facture <-> paiement avec facture
# ---------------------------------------------------------------------------
def propose_rapprochements(records, rejected_pairs=frozenset(), used_from=frozenset(),
                           used_to=frozenset(), exclude_from=frozenset()):
    sans = [r for r in records
            if r["status"] in ("actif", "a_verifier_disparu")
            and not r["facture_ok"] and not r.get("facture_rapprochee")
            and r["exploitant_key"] is not None
            and r["key"] not in used_from and r["key"] not in exclude_from]
    avec = [r for r in records
            if r["status"] == "actif" and r["facture_ok"] and r["key"] not in used_to]
    by_k = defaultdict(list)
    for f in avec:
        by_k[(f["exploitant_key"], f["montant"])].append(f)

    cands = []
    for s in sans:
        for f in by_k.get((s["exploitant_key"], s["montant"]), []):
            if (s["key"], f["key"]) in rejected_pairs:
                continue
            dd = _days(s["date"], f["date"])
            if dd < -7 or dd > 120:
                continue
            if s["status"] == "a_verifier_disparu":
                conf, why = "forte", "le paiement sans facture a disparu des fichiers récents ; même exploitant, même montant"
            elif dd >= 0:
                conf, why = "moyenne", f"même exploitant, même montant ; facture apparue {dd} jour(s) après"
            else:
                conf, why = "faible", f"même exploitant, même montant ; paiement avec facture daté {-dd} jour(s) AVANT"
            cands.append((CONF_ORDER[conf], abs(dd), s["key"], f["key"], conf, why, s, f))
    cands.sort(key=lambda c: c[:4])

    taken_s, taken_f = set(), set()
    out = []
    for _, dd, sk, fk, conf, why, s, f in cands:
        if sk in taken_s or fk in taken_f:
            continue
        taken_s.add(sk)
        taken_f.add(fk)
        # nb d'autres candidats possibles : utile pour juger de l'ambiguïté
        n_alt = len(by_k.get((s["exploitant_key"], s["montant"]), [])) - 1
        out.append({
            "type": "rapprochement",
            "old": s, "new": f,
            "confiance": conf, "regle": why,
            "ecart_jours": _days(s["date"], f["date"]),
            "autres_candidats": max(n_alt, 0),
        })
    return out


# ---------------------------------------------------------------------------
# 3) Factures suspectes
# ---------------------------------------------------------------------------
def factures_suspectes(records, modifications=()):
    out = []
    actifs = [r for r in records if r["status"] == "actif"]

    for r in actifs:
        if r["facture_issue"]:
            out.append({
                "gravite": "à corriger",
                "type": "Référence non conforme",
                "numero_facture": r["numero_facture"],
                "detail": r["facture_issue"],
                "records": [r],
            })

    groups = defaultdict(list)
    for r in actifs:
        if r["facture_ok"]:
            groups[normalize_facture(r["numero_facture"])].append(r)
    for fac, rs in groups.items():
        if len(rs) < 2:
            continue
        keys = {r["exploitant_key"] for r in rs}
        if len(keys) > 1:
            noms = sorted({r["raison_sociale"] for r in rs})
            out.append({
                "gravite": "à corriger",
                "type": "Même facture, exploitants différents",
                "numero_facture": rs[0]["numero_facture"],
                "detail": "attachée à : " + " / ".join(noms),
                "records": rs,
            })
        else:
            montants = Counter(r["montant"] for r in rs)
            out.append({
                "gravite": "à vérifier",
                "type": "Même facture sur plusieurs paiements",
                "numero_facture": rs[0]["numero_facture"],
                "detail": f"{len(rs)} paiements de {rs[0]['raison_sociale']} "
                          f"({', '.join(f'{int(m):,}'.replace(',', ' ') for m in montants)} FCFA) — "
                          "paiement échelonné ou doublon ?",
                "records": rs,
            })

    for m in modifications:
        if len(m.get("news", [m["new"]])) != 1:
            continue
        a = m["old"].get("numero_facture") or ""
        b = m["new"].get("numero_facture") or ""
        if a and b and normalize_facture(a) != normalize_facture(b):
            out.append({
                "gravite": "à vérifier",
                "type": "Facture modifiée entre deux fichiers",
                "numero_facture": f"{a} → {b}",
                "detail": f"{m['old']['raison_sociale']} — {m['old']['date']}",
                "records": [m["old"], m["new"]],
            })

    order = {"à corriger": 0, "à vérifier": 1}
    out.sort(key=lambda x: (order[x["gravite"]], x["type"], str(x["numero_facture"])))
    return out


# ---------------------------------------------------------------------------
# Exploitants (regroupement des orthographes)
# ---------------------------------------------------------------------------
def exploitants(records):
    groups = defaultdict(list)
    for r in records:
        groups[r["exploitant_key"] or "__NI__"].append(r)
    out = []
    for k, rs in groups.items():
        noms = Counter(r["raison_sociale"] for r in rs if r["raison_sociale"])
        actifs = [r for r in rs if r["status"] == "actif"]
        sans = [r for r in actifs if not r["facture_ok"] and not r.get("facture_rapprochee")]
        out.append({
            "key": k,
            "nom": "Non identifié (NI)" if k == "__NI__" else (noms.most_common(1)[0][0] if noms else k),
            "orthographes": sorted(noms),
            "n_paiements": len(actifs),
            "total": sum(r["recouvrement_utilise"] or 0 for r in actifs),
            "n_sans_facture": len(sans),
            "montant_sans_facture": sum(r["recouvrement_utilise"] or 0 for r in sans),
            "n_rapproches": sum(1 for r in rs if r.get("facture_rapprochee")),
            "categorie": Counter(r["categorie"] for r in rs).most_common(1)[0][0],
        })
    out.sort(key=lambda e: -e["total"])
    return out


def matches_exploitant(r, key):
    if not key:
        return True
    if key == "__NI__":
        return r["exploitant_key"] is None
    return r["exploitant_key"] == key
