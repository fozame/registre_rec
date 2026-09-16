"""
auth.py — Comptes utilisateurs et rôles.

Deux rôles :
  admin  peut tout faire, y compris gérer les comptes et RÉINITIALISER
         complètement la base (tout effacer pour pouvoir tout reconstituer
         proprement en réimportant les fichiers depuis le début).
  user   peut importer des fichiers, consulter, exporter, et confirmer une
         annulation — mais ne peut ni gérer les comptes ni réinitialiser
         la base.

Les mots de passe ne sont jamais stockés en clair : seul leur hash
(werkzeug.security, algorithme scrypt/pbkdf2 selon la version installée)
est conservé dans la table `users`.
"""

import secrets
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LENGTH = 8
VALID_ROLES = ("admin", "user")


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_bootstrap_admin(conn):
    """Au tout premier lancement (aucun utilisateur en base), crée un compte
    admin avec un mot de passe aléatoire, et l'écrit une seule fois dans
    admin_initial_password.txt à côté de la base — à consulter, utiliser pour
    la première connexion, puis à supprimer."""
    count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if count > 0:
        return None
    password = secrets.token_urlsafe(9)  # ~12 caractères lisibles
    create_user(conn, "admin", password, "admin", created_by="bootstrap")
    return password


def create_user(conn, username, password, role, created_by=None):
    username = (username or "").strip()
    if not username:
        raise ValueError("Le nom d'utilisateur est obligatoire.")
    if role not in VALID_ROLES:
        raise ValueError("Rôle invalide.")
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} caractères.")
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        raise ValueError(f"Le nom d'utilisateur '{username}' existe déjà.")
    conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, generate_password_hash(password), role, _now(), created_by),
    )
    conn.commit()


def verify_user(conn, username, password):
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
    ).fetchone()
    if row is None:
        return None
    if not check_password_hash(row["password_hash"], password or ""):
        return None
    return dict(row)


def get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def list_users(conn):
    rows = conn.execute(
        "SELECT id, username, role, created_at, created_by FROM users ORDER BY username"
    ).fetchall()
    return [dict(r) for r in rows]


def count_admins(conn):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"
    ).fetchone()["n"]


def delete_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise KeyError("Utilisateur introuvable.")
    if row["role"] == "admin" and count_admins(conn) <= 1:
        raise ValueError("Impossible de supprimer le dernier compte administrateur.")
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()


def reset_password(conn, user_id, new_password):
    if not new_password or len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} caractères.")
    row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise KeyError("Utilisateur introuvable.")
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user_id),
    )
    conn.commit()
