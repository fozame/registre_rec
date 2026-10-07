"""
storage.py — Persistance de la base cumulative dans un fichier SQLite unique.

Un seul fichier (recouvrement.db, créé automatiquement à côté de app.py) contient
tout l'historique. C'est ce fichier qu'il faut sauvegarder régulièrement (une simple
copie suffit — SQLite est un format de fichier unique, pas un serveur séparé).

Statuts possibles pour un enregistrement (records.status) :
  actif               -> paiement confirmé, compté dans les totaux
  a_verifier_disparu  -> présent dans un import précédent, absent du dernier import :
                         à vérifier avant d'être exclu définitivement
  annule_confirme     -> un agent a examiné la disparition et confirmé, avec un motif
                         écrit obligatoire, qu'il ne s'agit pas d'un paiement perdu
                         (ex. correction du nom du payeur par l'autre direction)
  remplace            -> ancienne version d'un paiement corrigé dans un fichier
                         ultérieur (facture ajoutée, payeur précisé, paiement ventilé
                         sur plusieurs factures…). Non compté : c'est la nouvelle
                         version (remplace_par) qui compte.
  rapproche           -> paiement sans facture rapproché avec un paiement avec
                         facture du même exploitant et du même montant, et reconnu
                         comme étant le MÊME paiement. Non compté (pas de double compte).

Si une clé revient dans un import ultérieur, elle est réactivée en "actif" et un
historique de la réapparition est ajouté — rien n'est jamais perdu silencieusement.
Exception : une ancienne version déjà "remplacée"/"rapprochée" qui réapparaît en
même temps que sa nouvelle version n'est pas recomptée (cas d'un fichier ancien
réimporté).

Chaque changement est tracé dans record_events (quel paiement, quel fichier,
quelle période, quel agent) et chaque décision de rapprochement dans liens.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, date, timedelta

import rapprochement as rap
from normalisation import classify_operateur

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    key                   TEXT PRIMARY KEY,
    date                  TEXT NOT NULL,
    libelle               TEXT,
    raison_sociale        TEXT,
    montant               REAL,
    recouvrement_declare  REAL,
    recouvrement_utilise  REAL,
    banque                TEXT,
    block_label_raw       TEXT,
    categorie             TEXT,
    numero_facture        TEXT,
    needs_review          INTEGER NOT NULL DEFAULT 0,
    review_reason         TEXT,
    status                TEXT NOT NULL DEFAULT 'actif',
    first_seen_upload     TEXT,
    first_seen_date       TEXT,
    last_seen_upload      TEXT,
    last_seen_date        TEXT,
    sources               TEXT,
    confirmed_motif       TEXT,
    confirmed_by          TEXT,
    confirmed_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_records_date ON records(date);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
CREATE INDEX IF NOT EXISTS idx_records_categorie ON records(categorie);
CREATE INDEX IF NOT EXISTS idx_records_banque ON records(banque);

CREATE TABLE IF NOT EXISTS uploads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    filename            TEXT NOT NULL,
    sheet_name          TEXT,
    uploaded_at         TEXT NOT NULL,
    uploaded_by         TEXT,
    n_parsed            INTEGER,
    n_new               INTEGER,
    n_reconfirmed       INTEGER,
    n_missing_flagged   INTEGER,
    date_min            TEXT,
    date_max            TEXT,
    needs_review_count  INTEGER,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    created_at      TEXT NOT NULL,
    created_by      TEXT
);

CREATE TABLE IF NOT EXISTS record_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    upload_id   INTEGER,
    filename    TEXT,
    event       TEXT NOT NULL,
    detail      TEXT,
    actor       TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_key ON record_events(key);
CREATE INDEX IF NOT EXISTS idx_events_upload ON record_events(upload_id);

CREATE TABLE IF NOT EXISTS liens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL CHECK (type IN ('modification', 'rapprochement')),
    key_from    TEXT NOT NULL,
    keys_to     TEXT NOT NULL,
    confiance   TEXT,
    regle       TEXT,
    nature      TEXT,
    diff        TEXT,
    statut      TEXT NOT NULL CHECK (statut IN ('valide', 'rejete', 'annule')),
    mode        TEXT,
    prev_status TEXT,
    decided_by  TEXT,
    decided_at  TEXT,
    motif       TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liens_from ON liens(key_from);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
"""


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Colonnes ajoutées après la première version : ajoutées automatiquement aux
# bases existantes au démarrage (aucune donnée n'est perdue).
_NEW_COLUMNS = {
    "records": [
        ("remplace_par", "TEXT"),        # clé(s) de la nouvelle version (séparées par des virgules)
        ("remplace_de", "TEXT"),         # clé de l'ancienne version que cette ligne remplace
        ("facture_rapprochee", "TEXT"),  # n° de facture retrouvé par rapprochement
        ("rapproche_avec", "TEXT"),      # clé du paiement avec facture
        ("rapprochement_mode", "TEXT"),  # meme_paiement | facture_seule
    ],
    "uploads": [
        ("fichier_modifie_le", "TEXT"),
        ("fichier_modifie_par", "TEXT"),
        ("fichier_cree_le", "TEXT"),
        ("periode_debut", "TEXT"),
        ("periode_fin", "TEXT"),
        ("avertissement", "TEXT"),
        ("n_modifications_auto", "INTEGER"),
    ],
}


def init_db(db_path):
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    for table, cols in _NEW_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    conn.commit()
    _recompute_categories(conn)
    _backfill_events(conn)
    auto_apply_modifications(conn)
    conn.close()


def _recompute_categories(conn):
    """Réapplique la règle Orange/MTN/Autre à toute la base (ex. "MOBILE
    TELEPHONE NETWORKS" désormais classé MTN)."""
    rows = conn.execute("SELECT key, raison_sociale, categorie FROM records").fetchall()
    for r in rows:
        cat = classify_operateur(r["raison_sociale"])
        if cat != r["categorie"]:
            conn.execute("UPDATE records SET categorie = ? WHERE key = ?", (cat, r["key"]))
    conn.commit()


def _backfill_events(conn):
    """Base créée avant l'historique détaillé : reconstitue un historique
    minimal (première apparition, disparition, annulation) à partir des
    informations déjà stockées."""
    if conn.execute("SELECT COUNT(*) FROM record_events").fetchone()[0]:
        return
    uploads = conn.execute("SELECT id, filename, uploaded_at FROM uploads ORDER BY id").fetchall()
    first_id = {}
    for u in uploads:
        first_id.setdefault(u["filename"], u["id"])
    last_upload = uploads[-1] if uploads else None
    now = _now()
    for r in conn.execute("SELECT * FROM records").fetchall():
        conn.execute(
            "INSERT INTO record_events (key, upload_id, filename, event, detail, actor, created_at) "
            "VALUES (?, ?, ?, 'nouveau', ?, 'reconstitution', ?)",
            (r["key"], first_id.get(r["first_seen_upload"]), r["first_seen_upload"],
             f"première apparition (import du {r['first_seen_date']}) — historique reconstitué", now),
        )
        if r["status"] == "a_verifier_disparu" and last_upload:
            conn.execute(
                "INSERT INTO record_events (key, upload_id, filename, event, detail, actor, created_at) "
                "VALUES (?, ?, ?, 'disparu', ?, 'reconstitution', ?)",
                (r["key"], last_upload["id"], last_upload["filename"],
                 f"absent du fichier {last_upload['filename']} — dernier fichier le contenant : "
                 f"{r['last_seen_upload']}", now),
            )
        if r["status"] == "annule_confirme":
            conn.execute(
                "INSERT INTO record_events (key, upload_id, filename, event, detail, actor, created_at) "
                "VALUES (?, NULL, NULL, 'annule_confirme', ?, ?, ?)",
                (r["key"], r["confirmed_motif"], r["confirmed_by"], r["confirmed_at"] or now),
            )
    conn.commit()


def add_event(conn, key, event, detail=None, actor=None, upload_id=None, filename=None):
    conn.execute(
        "INSERT INTO record_events (key, upload_id, filename, event, detail, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (key, upload_id, filename, event, detail, actor, _now()),
    )


@contextmanager
def connect(db_path):
    conn = get_conn(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _plausible_period(df):
    """Période couverte par le fichier, en ignorant les dates manifestement
    mal saisies (ex. 2029, 2025 isolé) : 2e et 98e centiles des dates."""
    if not len(df):
        return None, None
    ds = sorted(df["date"])
    lo = ds[int(len(ds) * 0.02)]
    hi = ds[min(len(ds) - 1, int(len(ds) * 0.98))]
    return lo.isoformat(), hi.isoformat()


def _order_warning(conn, filename, file_meta, periode_fin):
    """Avertit si le fichier importé paraît plus ancien que le dernier importé
    (les paiements absents seraient alors signalés "disparus" à tort), ou s'il
    s'agit du même fichier déjà importé."""
    last = conn.execute(
        "SELECT filename, fichier_modifie_le, periode_fin FROM uploads ORDER BY id DESC LIMIT 1"
    ).fetchone()
    msgs = []
    mod = (file_meta or {}).get("modifie_le")
    if mod:
        same = conn.execute(
            "SELECT uploaded_at FROM uploads WHERE filename = ? AND fichier_modifie_le = ?",
            (filename, mod),
        ).fetchone()
        if same:
            msgs.append(f"Ce fichier (même nom, même date de modification {mod}) a déjà été "
                        f"importé le {same['uploaded_at']}.")
    if last:
        if mod and last["fichier_modifie_le"] and mod < last["fichier_modifie_le"]:
            msgs.append(
                f"Ce fichier a été modifié le {mod}, AVANT le dernier fichier importé "
                f"({last['filename']}, modifié le {last['fichier_modifie_le']}). "
                "S'il est plus ancien, les paiements récents absents de ce fichier sont "
                "signalés « disparus » à tort : réimportez ensuite le fichier le plus récent.")
        elif periode_fin and last["periode_fin"] and periode_fin < last["periode_fin"]:
            msgs.append(
                f"Les données de ce fichier s'arrêtent au {periode_fin}, avant celles du dernier "
                f"fichier importé ({last['filename']}, jusqu'au {last['periode_fin']}). "
                "Vérifiez qu'il ne s'agit pas d'un fichier plus ancien.")
    return " ".join(msgs) or None


def merge_upload(conn, filename, sheet_name, uploaded_at, uploaded_by, df, file_meta=None):
    """Fusionne les lignes fraîchement analysées (df, sortie de parser.parse_file)
    dans la base cumulative, trace chaque changement (record_events) puis
    applique les modifications détectées avec une confiance forte."""
    cur = conn.cursor()
    file_meta = file_meta or {}
    periode_debut, periode_fin = _plausible_period(df)
    warning = _order_warning(conn, filename, file_meta, periode_fin)

    cur.execute(
        """INSERT INTO uploads (filename, sheet_name, uploaded_at, uploaded_by, created_at,
               fichier_modifie_le, fichier_modifie_par, fichier_cree_le,
               periode_debut, periode_fin, avertissement)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (filename, sheet_name, uploaded_at, uploaded_by, _now(),
         file_meta.get("modifie_le"), file_meta.get("modifie_par"), file_meta.get("cree_le"),
         periode_debut, periode_fin, warning),
    )
    upload_id = cur.lastrowid
    label = f"{filename} (données du {periode_debut} au {periode_fin})"

    def ev(key, event, detail):
        add_event(conn, key, event, detail, actor=uploaded_by, upload_id=upload_id, filename=filename)

    cur.execute("SELECT key FROM records WHERE status = 'actif'")
    active_keys = {row["key"] for row in cur.fetchall()}
    keys_this_upload = set(df["key"]) if len(df) else set()

    n_missing = 0
    for k in active_keys - keys_this_upload:
        cur.execute("SELECT review_reason FROM records WHERE key = ?", (k,))
        existing_reason = (cur.fetchone()["review_reason"] or "")
        new_reason = existing_reason + f" | absent du fichier {filename} importé le {uploaded_at}"
        cur.execute(
            "UPDATE records SET status = 'a_verifier_disparu', needs_review = 1, "
            "review_reason = ? WHERE key = ?",
            (new_reason, k),
        )
        ev(k, "disparu", f"absent de {label}")
        n_missing += 1

    n_new = 0
    n_reconfirmed = 0
    for _, row in df.iterrows():
        k = row["key"]
        cur.execute("SELECT * FROM records WHERE key = ?", (k,))
        existing = cur.fetchone()
        if existing:
            sources = json.loads(existing["sources"] or "[]")
            if filename not in sources:
                sources.append(filename)
            new_status = existing["status"]
            new_reason = existing["review_reason"] or ""
            if new_status in ("remplace", "rapproche"):
                # ancienne version qui réapparaît : on ne la recompte que si sa
                # nouvelle version est absente de ce même fichier
                linked = (existing["remplace_par"] or existing["rapproche_avec"] or "").split(",")
                linked = [x for x in linked if x]
                if linked and not any(x in keys_this_upload for x in linked):
                    new_status = "actif"
                    new_reason += f" | réapparu dans {filename} sans sa version corrigée : réactivé"
                    conn.execute(
                        "UPDATE liens SET statut = 'annule', motif = ? WHERE key_from = ? AND statut = 'valide'",
                        (f"ancienne version réapparue dans {filename}", k),
                    )
                    conn.execute(
                        "UPDATE records SET remplace_de = NULL WHERE remplace_de = ?", (k,))
                    conn.execute(
                        "UPDATE records SET remplace_par = NULL, facture_rapprochee = NULL, "
                        "rapproche_avec = NULL, rapprochement_mode = NULL WHERE key = ?", (k,))
                    ev(k, "reapparu", f"réapparu dans {label} sans sa version corrigée — lien annulé, paiement réactivé")
                else:
                    ev(k, "reapparu", f"réapparu dans {label} avec sa version corrigée — non recompté")
            elif new_status != "actif":
                new_status = "actif"
                new_reason += f" | réapparu dans {filename}, statut réactivé automatiquement"
                ev(k, "reapparu", f"réapparu dans {label} — réactivé (statut précédent : {existing['status']})")
            cur.execute(
                "UPDATE records SET last_seen_upload = ?, last_seen_date = ?, "
                "sources = ?, status = ?, review_reason = ?, "
                "needs_review = CASE WHEN ? = 'actif' AND status = 'a_verifier_disparu' "
                "               THEN (review_reason LIKE '%date%' OR review_reason LIKE '%Recouvrement vide%') "
                "               ELSE needs_review END "
                "WHERE key = ?",
                (filename, uploaded_at, json.dumps(sources, ensure_ascii=False),
                 new_status, new_reason, new_status, k),
            )
            n_reconfirmed += 1
        else:
            cur.execute(
                """INSERT INTO records (
                    key, date, libelle, raison_sociale, montant,
                    recouvrement_declare, recouvrement_utilise, banque,
                    block_label_raw, categorie, numero_facture,
                    needs_review, review_reason, status,
                    first_seen_upload, first_seen_date,
                    last_seen_upload, last_seen_date, sources
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'actif', ?, ?, ?, ?, ?)""",
                (
                    k, row["date"].isoformat(), row["libelle"], row["raison_sociale"],
                    row["montant"], row["recouvrement_declare"], row["recouvrement_utilise"],
                    row["banque"], row["block_label_raw"], row["categorie"], row["numero_facture"],
                    int(bool(row["needs_review"])), row["review_reason"],
                    filename, uploaded_at, filename, uploaded_at,
                    json.dumps([filename], ensure_ascii=False),
                ),
            )
            ev(k, "nouveau", f"apparu dans {label}")
            n_new += 1

    conn.commit()
    n_auto = auto_apply_modifications(conn, upload_id=upload_id, filename=filename)

    date_min = df["date"].min().isoformat() if len(df) else None
    date_max = df["date"].max().isoformat() if len(df) else None
    needs_review_count = int(df["needs_review"].sum()) if len(df) else 0

    cur.execute(
        """UPDATE uploads SET n_parsed = ?, n_new = ?, n_reconfirmed = ?, n_missing_flagged = ?,
               date_min = ?, date_max = ?, needs_review_count = ?, n_modifications_auto = ?
           WHERE id = ?""",
        (len(df), n_new, n_reconfirmed, n_missing, date_min, date_max,
         needs_review_count, n_auto, upload_id),
    )
    conn.commit()

    analyse = analyse_rapprochement(conn)
    return {
        "filename": filename,
        "sheet_name": sheet_name,
        "uploaded_at": uploaded_at,
        "n_parsed": len(df),
        "n_new": n_new,
        "n_reconfirmed": n_reconfirmed,
        "n_missing_flagged": n_missing,
        "needs_review_count": needs_review_count,
        "date_min": date_min,
        "date_max": date_max,
        "periode_debut": periode_debut,
        "periode_fin": periode_fin,
        "fichier_modifie_le": file_meta.get("modifie_le"),
        "n_modifications_auto": n_auto,
        "n_modifications_a_valider": len(analyse["modifications"]),
        "n_rapprochements_proposes": len(analyse["rapprochements"]),
        "avertissement": warning,
    }


def confirm_cancellation(conn, key, motif, confirmed_by):
    cur = conn.cursor()
    cur.execute("SELECT status FROM records WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"Aucun enregistrement avec la clé '{key}'")
    if not motif or not motif.strip():
        raise ValueError("Un motif est obligatoire pour confirmer une annulation.")
    # Le motif écrit fait foi : le cas est résolu et sort du panneau "à vérifier"
    # (il reste consultable dans le détail des paiements, statut "Annulé (confirmé)").
    cur.execute(
        "UPDATE records SET status = 'annule_confirme', needs_review = 0, "
        "confirmed_motif = ?, confirmed_by = ?, confirmed_at = ? WHERE key = ?",
        (motif.strip(), confirmed_by or "non renseigné", _now(), key),
    )
    add_event(conn, key, "annule_confirme", motif.strip(), actor=confirmed_by)
    conn.commit()


def get_uploads(conn):
    cur = conn.execute("SELECT * FROM uploads ORDER BY id DESC")
    return [dict(r) for r in cur.fetchall()]


def get_summary(conn, start=None, end=None):
    where = ["status = 'actif'"]
    params = []
    if start:
        where.append("date >= ?")
        params.append(start)
    if end:
        where.append("date <= ?")
        params.append(end)
    where_sql = " AND ".join(where)

    total_row = conn.execute(
        f"SELECT COALESCE(SUM(recouvrement_utilise),0) AS total, COUNT(*) AS n "
        f"FROM records WHERE {where_sql}", params,
    ).fetchone()

    by_categorie = {
        r["categorie"]: r["total"]
        for r in conn.execute(
            f"SELECT categorie, COALESCE(SUM(recouvrement_utilise),0) AS total "
            f"FROM records WHERE {where_sql} GROUP BY categorie", params,
        ).fetchall()
    }
    for cat in ("Orange", "MTN", "Autre"):
        by_categorie.setdefault(cat, 0)

    by_banque = [
        {"banque": r["banque"], "total": r["total"]}
        for r in conn.execute(
            f"SELECT banque, COALESCE(SUM(recouvrement_utilise),0) AS total "
            f"FROM records WHERE {where_sql} GROUP BY banque ORDER BY total DESC", params,
        ).fetchall()
    ]

    return {
        "total": total_row["total"],
        "count": total_row["n"],
        "by_categorie": by_categorie,
        "by_banque": by_banque,
        "start": start,
        "end": end,
    }


def get_records(conn, start=None, end=None, status=None, needs_review=None,
                 categorie=None, banque=None, search=None, limit=2000,
                 exploitant=None, sans_facture=False):
    """limit=None renvoie tout (utilisé pour les exports Excel, où l'on veut
    l'intégralité des lignes filtrées plutôt qu'un aperçu plafonné)."""
    where = []
    params = []
    if start:
        where.append("date >= ?")
        params.append(start)
    if end:
        where.append("date <= ?")
        params.append(end)
    if status:
        where.append("status = ?")
        params.append(status)
    if needs_review is not None:
        where.append("needs_review = ?")
        params.append(1 if needs_review else 0)
    if categorie:
        where.append("categorie = ?")
        params.append(categorie)
    if banque:
        where.append("banque = ?")
        params.append(banque)
    if search:
        where.append("(raison_sociale LIKE ? OR libelle LIKE ? OR numero_facture LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    post_filter = bool(exploitant or sans_facture)
    limit_sql = ""
    if limit is not None and not post_filter:
        limit_sql = " LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM records {where_sql} ORDER BY date DESC{limit_sql}", params,
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["needs_review"] = bool(d["needs_review"])
        d["sources"] = json.loads(d["sources"] or "[]")
        out.append(d)
    rap.enrich(out)
    if exploitant:
        out = [d for d in out if rap.matches_exploitant(d, exploitant)]
    if sans_facture:
        out = [d for d in out if not d["facture_ok"] and not d.get("facture_rapprochee")]
    if limit is not None:
        out = out[:limit]
    return out


def get_anomalies(conn):
    """Enregistrements qui nécessitent l'attention d'un agent : anomalies de
    saisie détectées (needs_review) et paiements disparus en attente de
    confirmation (a_verifier_disparu)."""
    rows = conn.execute(
        "SELECT * FROM records WHERE needs_review = 1 OR status = 'a_verifier_disparu' "
        "ORDER BY status = 'a_verifier_disparu' DESC, date DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["needs_review"] = bool(d["needs_review"])
        d["sources"] = json.loads(d["sources"] or "[]")
        out.append(d)
    return out


def get_banks(conn):
    rows = conn.execute(
        "SELECT DISTINCT banque FROM records WHERE banque IS NOT NULL ORDER BY banque"
    ).fetchall()
    return [r["banque"] for r in rows]


# ---------------------------------------------------------------------------
# Réinitialisation complète (admin uniquement — voir auth.py / app.py pour le
# contrôle d'accès) : vide la base cumulative pour repartir de zéro et
# reconstituer l'historique en réimportant les fichiers depuis le début.
# Les comptes utilisateurs et le journal d'audit sont conservés.
# ---------------------------------------------------------------------------
def reset_all_data(conn):
    cur = conn.cursor()
    cur.execute("DELETE FROM records")
    cur.execute("DELETE FROM uploads")
    cur.execute("DELETE FROM record_events")
    cur.execute("DELETE FROM liens")
    conn.commit()


def log_action(conn, actor, action, detail=None):
    conn.execute(
        "INSERT INTO audit_log (actor, action, detail, created_at) VALUES (?, ?, ?, ?)",
        (actor, action, detail, _now()),
    )
    conn.commit()


def get_audit_log(conn, limit=200):
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Historique détaillé (qui / quoi / quel fichier / quelle période)
# ---------------------------------------------------------------------------
EVENT_LABELS = {
    "nouveau": "Apparu",
    "disparu": "Disparu du fichier",
    "reapparu": "Réapparu",
    "annule_confirme": "Annulation confirmée",
    "modifie": "Corrigé dans un fichier ultérieur",
    "remplace": "Remplacé par une version corrigée",
    "rapproche": "Rapproché (facture)",
    "lien_annule": "Décision annulée",
}


def get_events(conn, keys=None, upload_id=None, limit=None):
    where, params = [], []
    if upload_id is not None:
        where.append("e.upload_id = ?")
        params.append(upload_id)
    if keys is not None:
        keys = list(keys)
        if not keys:
            return []
        where.append(f"e.key IN ({','.join('?' * len(keys))})")
        params.extend(keys)
    sql = ("SELECT e.*, u.periode_debut, u.periode_fin, u.fichier_modifie_le, "
           "u.uploaded_at AS upload_date FROM record_events e "
           "LEFT JOIN uploads u ON u.id = e.upload_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def events_by_key(conn):
    out = {}
    for e in get_events(conn):
        out.setdefault(e["key"], []).append(e)
    return out


def format_history(events):
    parts = []
    for e in events:
        when = (e.get("upload_date") or (e.get("created_at") or "")[:10])
        parts.append(f"[{when}] {EVENT_LABELS.get(e['event'], e['event'])}"
                     + (f" : {e['detail']}" if e.get("detail") else "")
                     + (f" (par {e['actor']})" if e.get("actor") and e["actor"] not in ("reconstitution",) else ""))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Rapprochement et modifications
# ---------------------------------------------------------------------------
def _all_records(conn):
    out = []
    for r in conn.execute("SELECT * FROM records").fetchall():
        d = dict(r)
        d["needs_review"] = bool(d["needs_review"])
        d["sources"] = json.loads(d["sources"] or "[]")
        out.append(d)
    return rap.enrich(out)


def _decisions(conn):
    rejected = {"modification": set(), "rapprochement": set()}
    for l in conn.execute("SELECT type, key_from, keys_to FROM liens WHERE statut = 'rejete'"):
        for kt in l["keys_to"].split(","):
            rejected[l["type"]].add((l["key_from"], kt))
    return rejected


def _apply_modification(conn, old_key, new_keys, conf, regle, nature, diff, actor,
                        motif=None, upload_id=None, filename=None):
    old = conn.execute("SELECT status FROM records WHERE key = ?", (old_key,)).fetchone()
    if old is None:
        raise KeyError(f"Aucun enregistrement avec la clé '{old_key}'")
    for nk in new_keys:
        if conn.execute("SELECT 1 FROM records WHERE key = ?", (nk,)).fetchone() is None:
            raise KeyError(f"Aucun enregistrement avec la clé '{nk}'")
    conn.execute(
        """INSERT INTO liens (type, key_from, keys_to, confiance, regle, nature, diff, statut,
               prev_status, decided_by, decided_at, motif, created_at)
           VALUES ('modification', ?, ?, ?, ?, ?, ?, 'valide', ?, ?, ?, ?, ?)""",
        (old_key, ",".join(new_keys), conf, regle, nature,
         json.dumps(diff, ensure_ascii=False, default=str), old["status"], actor, _now(), motif, _now()),
    )
    conn.execute(
        "UPDATE records SET status = 'remplace', needs_review = 0, remplace_par = ? WHERE key = ?",
        (",".join(new_keys), old_key),
    )
    for nk in new_keys:
        conn.execute("UPDATE records SET remplace_de = ? WHERE key = ?", (old_key, nk))
        add_event(conn, nk, "modifie", f"nouvelle version de {old_key} : {nature}",
                  actor=actor, upload_id=upload_id, filename=filename)
    add_event(conn, old_key, "remplace",
              f"remplacé par {', '.join(new_keys)} : {nature} (confiance {conf} — {regle})",
              actor=actor, upload_id=upload_id, filename=filename)


def auto_apply_modifications(conn, upload_id=None, filename=None):
    """Applique automatiquement les modifications de confiance FORTE (même
    date, même exploitant, même montant ou somme exacte). Elles ne changent
    pas les totaux : l'ancienne version n'était déjà plus comptée."""
    records = _all_records(conn)
    if not any(r["status"] == "a_verifier_disparu" for r in records):
        return 0
    rejected = _decisions(conn)
    used_new = {r["key"] for r in records if r.get("remplace_de")}
    n = 0
    for m in rap.propose_modifications(records, rejected["modification"], used_new):
        if m["confiance"] != "forte":
            continue
        _apply_modification(conn, m["old"]["key"], [x["key"] for x in m["news"]],
                            m["confiance"], m["regle"], m["nature"], m["diff"],
                            actor="automatique", upload_id=upload_id, filename=filename)
        n += 1
    conn.commit()
    return n


def analyse_rapprochement(conn, exploitant=None):
    """Propositions en attente + décisions déjà prises + factures suspectes."""
    records = _all_records(conn)
    by_key = {r["key"]: r for r in records}
    rejected = _decisions(conn)
    used_new = {r["key"] for r in records if r.get("remplace_de")}
    mods = rap.propose_modifications(records, rejected["modification"], used_new)
    pending_old = {m["old"]["key"] for m in mods}
    used_to = {r["rapproche_avec"] for r in records if r.get("rapproche_avec")}
    raps = rap.propose_rapprochements(records, rejected["rapprochement"],
                                      used_to=used_to, exclude_from=pending_old)

    decided = []
    for l in conn.execute("SELECT * FROM liens WHERE statut = 'valide' ORDER BY id").fetchall():
        l = dict(l)
        old = by_key.get(l["key_from"])
        news = [by_key[k] for k in l["keys_to"].split(",") if k in by_key]
        if old is None or not news:
            continue
        decided.append({**l, "old": old, "news": news, "new": news[0],
                        "diff": json.loads(l["diff"] or "[]")})

    # modifications validées où le n° de facture a changé -> suspect aussi
    suspects = rap.factures_suspectes(records, mods + [d for d in decided if d["type"] == "modification"])

    def keep(item):
        if not exploitant:
            return True
        rs = [item["old"]] + item.get("news", [item.get("new")]) if "old" in item else item["records"]
        return any(rap.matches_exploitant(r, exploitant) for r in rs if r)

    sans = [r for r in records if r["status"] == "actif" and not r["facture_ok"]
            and not r.get("facture_rapprochee")]
    return {
        "modifications": [m for m in mods if keep(m)],
        "rapprochements": [x for x in raps if keep(x)],
        "decisions": [d for d in decided if keep(d)],
        "factures_suspectes": [f for f in suspects if keep(f)],
        "sans_facture": [r for r in sans if rap.matches_exploitant(r, exploitant)],
        "exploitants": rap.exploitants(records),
    }


def valider_lien(conn, type_, key_from, keys_to, actor, mode=None, motif=None):
    if isinstance(keys_to, str):
        keys_to = [k for k in keys_to.split(",") if k]
    if not keys_to:
        raise ValueError("Aucune ligne cible.")
    records = {r["key"]: r for r in _all_records(conn)}
    old = records.get(key_from)
    if old is None or any(k not in records for k in keys_to):
        raise KeyError("Paiement introuvable (la base a peut-être changé : rechargez la page).")

    if type_ == "modification":
        if old["status"] != "a_verifier_disparu":
            raise ValueError("Ce paiement n'est plus en attente (déjà traité ?).")
        news = [records[k] for k in keys_to]
        if len(news) == 1:
            diff = rap.diff_records(old, news[0])
            nature = rap.describe_diff(diff)
        else:
            diff = [{"champ": "Ventilation", "avant": old["montant"],
                     "apres": " + ".join(str(int(n["montant"])) for n in news)}]
            nature = f"paiement ventilé en {len(news)} lignes"
        _apply_modification(conn, key_from, keys_to, "validée", "validation manuelle",
                            nature, diff, actor, motif=motif)
    elif type_ == "rapprochement":
        if len(keys_to) != 1:
            raise ValueError("Un rapprochement porte sur un seul paiement avec facture.")
        f = records[keys_to[0]]
        if old.get("facture_rapprochee"):
            raise ValueError("Ce paiement est déjà rapproché.")
        if mode not in ("meme_paiement", "facture_seule"):
            raise ValueError("Mode de rapprochement invalide.")
        new_status = "rapproche" if mode == "meme_paiement" else old["status"]
        conn.execute(
            """INSERT INTO liens (type, key_from, keys_to, confiance, regle, nature, statut, mode,
                   prev_status, decided_by, decided_at, motif, created_at)
               VALUES ('rapprochement', ?, ?, 'validée', 'validation manuelle', ?, 'valide', ?, ?, ?, ?, ?, ?)""",
            (key_from, f["key"], f"facture {f['numero_facture']} rattachée", mode,
             old["status"], actor, _now(), motif, _now()),
        )
        conn.execute(
            "UPDATE records SET facture_rapprochee = ?, rapproche_avec = ?, rapprochement_mode = ?, "
            "status = ?, needs_review = CASE WHEN ? = 'rapproche' THEN 0 ELSE needs_review END "
            "WHERE key = ?",
            (f["numero_facture"], f["key"], mode, new_status, new_status, key_from),
        )
        txt = ("même paiement : la ligne sans facture n'est plus comptée"
               if mode == "meme_paiement" else "facture rattachée, les deux lignes restent comptées")
        add_event(conn, key_from, "rapproche",
                  f"rapproché avec la facture {f['numero_facture']} ({f['date']}, {f['raison_sociale']}) — {txt}"
                  + (f" — motif : {motif}" if motif else ""), actor=actor)
        add_event(conn, f["key"], "rapproche",
                  f"sa facture {f['numero_facture']} a été rattachée au paiement sans facture du {old['date']}",
                  actor=actor)
    else:
        raise ValueError("Type de lien inconnu.")
    conn.commit()


def rejeter_lien(conn, type_, key_from, keys_to, actor, motif=None):
    if isinstance(keys_to, list):
        keys_to = ",".join(keys_to)
    conn.execute(
        """INSERT INTO liens (type, key_from, keys_to, statut, decided_by, decided_at, motif, created_at)
           VALUES (?, ?, ?, 'rejete', ?, ?, ?, ?)""",
        (type_, key_from, keys_to, actor, _now(), motif, _now()),
    )
    conn.commit()


def annuler_lien(conn, lien_id, actor, motif=None):
    l = conn.execute("SELECT * FROM liens WHERE id = ?", (lien_id,)).fetchone()
    if l is None or l["statut"] != "valide":
        raise KeyError("Décision introuvable ou déjà annulée.")
    prev = l["prev_status"] or "actif"
    if l["type"] == "modification":
        conn.execute(
            "UPDATE records SET status = ?, remplace_par = NULL, "
            "needs_review = CASE WHEN ? = 'a_verifier_disparu' THEN 1 ELSE needs_review END WHERE key = ?",
            (prev, prev, l["key_from"]),
        )
        for k in l["keys_to"].split(","):
            conn.execute("UPDATE records SET remplace_de = NULL WHERE key = ?", (k,))
    else:
        conn.execute(
            "UPDATE records SET status = ?, facture_rapprochee = NULL, rapproche_avec = NULL, "
            "rapprochement_mode = NULL, "
            "needs_review = CASE WHEN ? = 'a_verifier_disparu' THEN 1 ELSE needs_review END WHERE key = ?",
            (prev, prev, l["key_from"]),
        )
    conn.execute("UPDATE liens SET statut = 'annule', motif = COALESCE(?, motif) WHERE id = ?",
                 (motif, lien_id))
    if l["type"] == "modification":
        # une correction annulée est considérée comme rejetée, pour ne pas être
        # réappliquée automatiquement au prochain import. Un rapprochement
        # annulé, lui, redevient simplement une proposition.
        rejeter_lien(conn, l["type"], l["key_from"], l["keys_to"], actor,
                     f"décision n°{lien_id} annulée" + (f" : {motif}" if motif else ""))
    add_event(conn, l["key_from"], "lien_annule",
              f"{l['type']} n°{lien_id} annulé" + (f" : {motif}" if motif else ""), actor=actor)
    conn.commit()
