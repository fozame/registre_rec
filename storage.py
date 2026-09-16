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

Si une clé revient dans un import ultérieur (quel que soit son statut précédent),
elle est automatiquement réactivée en "actif" et un historique de la réapparition
est ajouté à review_reason — rien n'est jamais perdu silencieusement.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

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


def init_db(db_path):
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


@contextmanager
def connect(db_path):
    conn = get_conn(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def merge_upload(conn, filename, sheet_name, uploaded_at, uploaded_by, df):
    """Fusionne les lignes fraîchement analysées (df, sortie de parser.parse_file)
    dans la base cumulative. Reproduit la logique validée sur les 2 premiers
    fichiers réels (voir build_ledger.py dans les archives du projet)."""
    cur = conn.cursor()

    cur.execute("SELECT key FROM records WHERE status = 'actif'")
    active_keys = {row["key"] for row in cur.fetchall()}
    keys_this_upload = set(df["key"]) if len(df) else set()

    n_missing = 0
    for k in active_keys - keys_this_upload:
        cur.execute("SELECT review_reason FROM records WHERE key = ?", (k,))
        existing_reason = (cur.fetchone()["review_reason"] or "")
        new_reason = existing_reason + f" | absent de l'extrait du {uploaded_at}"
        cur.execute(
            "UPDATE records SET status = 'a_verifier_disparu', needs_review = 1, "
            "review_reason = ? WHERE key = ?",
            (new_reason, k),
        )
        n_missing += 1

    n_new = 0
    n_reconfirmed = 0
    for _, row in df.iterrows():
        k = row["key"]
        cur.execute("SELECT status, review_reason, sources FROM records WHERE key = ?", (k,))
        existing = cur.fetchone()
        if existing:
            sources = json.loads(existing["sources"] or "[]")
            if filename not in sources:
                sources.append(filename)
            new_status = existing["status"]
            new_reason = existing["review_reason"] or ""
            if new_status != "actif":
                new_status = "actif"
                new_reason += " | réapparu, statut réactivé automatiquement"
            cur.execute(
                "UPDATE records SET last_seen_upload = ?, last_seen_date = ?, "
                "sources = ?, status = ?, review_reason = ? WHERE key = ?",
                (filename, uploaded_at, json.dumps(sources, ensure_ascii=False),
                 new_status, new_reason, k),
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
            n_new += 1

    date_min = df["date"].min().isoformat() if len(df) else None
    date_max = df["date"].max().isoformat() if len(df) else None
    needs_review_count = int(df["needs_review"].sum()) if len(df) else 0

    cur.execute(
        """INSERT INTO uploads (
            filename, sheet_name, uploaded_at, uploaded_by, n_parsed, n_new,
            n_reconfirmed, n_missing_flagged, date_min, date_max,
            needs_review_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            filename, sheet_name, uploaded_at, uploaded_by, len(df), n_new,
            n_reconfirmed, n_missing, date_min, date_max, needs_review_count, _now(),
        ),
    )
    conn.commit()

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
                 categorie=None, banque=None, search=None, limit=2000):
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
    limit_sql = ""
    if limit is not None:
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
