"""
app.py — Serveur interne du Registre du Recouvrement.

Lancement :
    pip install -r requirements.txt
    python app.py

Par défaut, le serveur écoute sur toutes les interfaces réseau, port 5000 :
les collègues sur le même réseau interne accèdent à l'outil via
http://<adresse-ip-de-ce-poste>:5000

La base de données (recouvrement.db) est créée automatiquement au premier
lancement, à côté de ce fichier. C'est le seul fichier à sauvegarder pour
conserver tout l'historique — une simple copie suffit.

Au tout premier lancement, un compte administrateur est créé automatiquement
avec un mot de passe aléatoire, écrit une seule fois dans
admin_initial_password.txt (à côté de la base) — voir ce fichier, ou le
README, pour la procédure de première connexion.

Variables d'environnement optionnelles :
    RECOUVREMENT_DB    chemin du fichier SQLite (défaut : recouvrement.db)
    RECOUVREMENT_PORT  port d'écoute (défaut : 5000)
"""

import os
import secrets as secrets_mod
import tempfile
from datetime import date, timedelta
from functools import wraps

from flask import (
    Flask, jsonify, request, send_from_directory, send_file,
    session, redirect,
)
from werkzeug.security import check_password_hash

import auth
import storage
import xlsx_export
from parser import parse_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("RECOUVREMENT_DB", os.path.join(BASE_DIR, "recouvrement.db"))
STATIC_DIR = os.path.join(BASE_DIR, "static")
PAGES_DIR = os.path.join(BASE_DIR, "pages")
SECRET_KEY_PATH = os.path.join(os.path.dirname(DB_PATH), ".secret_key")
ADMIN_PASSWORD_NOTICE_PATH = os.path.join(os.path.dirname(DB_PATH), "admin_initial_password.txt")

# static_url_path="/static" (et non "") : le dossier static/ ne sert QUE des
# fichiers publics (CSS/JS/logo). La page authentifiée (pages/index.html)
# n'est JAMAIS servie automatiquement par Flask — elle passe uniquement par
# la route "/" ci-dessous, protégée par @login_required. Si index.html vivait
# dans static/ avec static_url_path="", n'importe qui pourrait contourner
# l'authentification en demandant directement /index.html.
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 Mo par fichier importé
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True


def _load_or_create_secret_key():
    """La clé de signature des sessions doit rester stable entre deux
    redémarrages du serveur (sinon tout le monde est déconnecté à chaque
    redémarrage) : elle est générée une fois puis conservée dans un petit
    fichier local à côté de la base de données."""
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
            if key:
                return key
    key = secrets_mod.token_hex(32)
    with open(SECRET_KEY_PATH, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


app.secret_key = _load_or_create_secret_key()

storage.init_db(DB_PATH)

with storage.connect(DB_PATH) as _conn:
    _bootstrap_password = auth.ensure_bootstrap_admin(_conn)
if _bootstrap_password:
    with open(ADMIN_PASSWORD_NOTICE_PATH, "w", encoding="utf-8") as fh:
        fh.write(
            "Compte administrateur créé automatiquement au premier lancement.\n\n"
            "Nom d'utilisateur : admin\n"
            f"Mot de passe      : {_bootstrap_password}\n\n"
            "Connectez-vous une première fois avec ces identifiants, créez vos "
            "propres comptes nommément (Administration > Comptes utilisateurs), "
            "puis SUPPRIMEZ ce fichier — il n'est plus nécessaire et ne doit pas "
            "rester en clair sur le serveur.\n"
        )


# ---------------------------------------------------------------------------
# Authentification — décorateurs
# ---------------------------------------------------------------------------
def _is_api_request():
    return request.path.startswith("/api/")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if _is_api_request():
                return jsonify(error="Authentification requise."), 401
            return redirect("/login?next=" + request.path)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if _is_api_request():
                return jsonify(error="Authentification requise."), 401
            return redirect("/login?next=" + request.path)
        if session.get("role") != "admin":
            return jsonify(error="Réservé aux administrateurs."), 403
        return view(*args, **kwargs)
    return wrapped


def current_actor():
    return session.get("username") or "inconnu"


# ---------------------------------------------------------------------------
# Connexion / déconnexion
# ---------------------------------------------------------------------------
@app.route("/login")
def login_page():
    if session.get("user_id"):
        return redirect("/")
    return send_from_directory(PAGES_DIR, "login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    with storage.connect(DB_PATH) as conn:
        user = auth.verify_user(conn, username, password)
        if user is None:
            return jsonify(error="Nom d'utilisateur ou mot de passe incorrect."), 401
        session.permanent = True
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        storage.log_action(conn, user["username"], "connexion")
    return jsonify(ok=True, username=user["username"], role=user["role"])


@app.route("/api/logout", methods=["POST"])
def api_logout():
    if session.get("username"):
        with storage.connect(DB_PATH) as conn:
            storage.log_action(conn, session["username"], "déconnexion")
    session.clear()
    return jsonify(ok=True)


@app.route("/api/me")
@login_required
def api_me():
    return jsonify(username=session.get("username"), role=session.get("role"))


# ---------------------------------------------------------------------------
# Page principale (protégée)
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    return send_from_directory(PAGES_DIR, "index.html")


# ---------------------------------------------------------------------------
# Import d'un nouveau fichier hebdomadaire
# ---------------------------------------------------------------------------
@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    if "file" not in request.files:
        return jsonify(error="Aucun fichier reçu."), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify(error="Aucun fichier sélectionné."), 400
    if not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify(error="Seuls les fichiers .xlsx / .xlsm sont acceptés."), 400

    sheet = request.form.get("sheet") or None
    uploaded_by = request.form.get("uploaded_by") or session.get("username")
    uploaded_at = request.form.get("uploaded_at") or date.today().isoformat()

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        df, sheet_name = parse_file(tmp_path, sheet=sheet)
    except Exception as exc:  # feuille manquante, fichier corrompu, etc.
        return jsonify(error=f"Impossible d'analyser ce fichier : {exc}"), 400
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    if len(df) == 0:
        return jsonify(error=(
            "Aucune ligne de paiement n'a été détectée dans ce fichier "
            f"(feuille '{sheet_name}'). Vérifiez qu'il s'agit bien du bon fichier."
        )), 400

    with storage.connect(DB_PATH) as conn:
        stats = storage.merge_upload(conn, f.filename, sheet_name, uploaded_at, uploaded_by, df)
        storage.log_action(conn, current_actor(), "import fichier", f.filename)

    return jsonify(stats)


# ---------------------------------------------------------------------------
# Totaux pour une période (KPI + répartition Orange/MTN/Autres + par banque)
# ---------------------------------------------------------------------------
@app.route("/api/summary")
@login_required
def api_summary():
    start = request.args.get("start") or None
    end = request.args.get("end") or None
    with storage.connect(DB_PATH) as conn:
        return jsonify(storage.get_summary(conn, start=start, end=end))


# ---------------------------------------------------------------------------
# Liste détaillée des paiements (filtres optionnels)
# ---------------------------------------------------------------------------
@app.route("/api/records")
@login_required
def api_records():
    with storage.connect(DB_PATH) as conn:
        rows = storage.get_records(
            conn,
            start=request.args.get("start") or None,
            end=request.args.get("end") or None,
            status=request.args.get("status") or None,
            needs_review=(
                request.args.get("needs_review") == "1"
                if "needs_review" in request.args else None
            ),
            categorie=request.args.get("categorie") or None,
            banque=request.args.get("banque") or None,
            search=request.args.get("search") or None,
        )
    return jsonify(rows)


# ---------------------------------------------------------------------------
# Panneau "à vérifier" : anomalies de saisie + paiements disparus
# ---------------------------------------------------------------------------
@app.route("/api/anomalies")
@login_required
def api_anomalies():
    with storage.connect(DB_PATH) as conn:
        return jsonify(storage.get_anomalies(conn))


@app.route("/api/export/anomalies.xlsx")
@login_required
def api_export_anomalies():
    with storage.connect(DB_PATH) as conn:
        rows = storage.get_anomalies(conn)
    buf = xlsx_export.build_records_workbook(
        rows, title="A verifier",
        subtitle="Anomalies de saisie et paiements disparus en attente de confirmation "
                  f"— export du {date.today().isoformat()}",
    )
    return send_file(
        buf, as_attachment=True,
        download_name=xlsx_export.export_filename("a_verifier"),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/export/records.xlsx")
@login_required
def api_export_records():
    with storage.connect(DB_PATH) as conn:
        rows = storage.get_records(
            conn,
            start=request.args.get("start") or None,
            end=request.args.get("end") or None,
            status=request.args.get("status") or None,
            categorie=request.args.get("categorie") or None,
            banque=request.args.get("banque") or None,
            search=request.args.get("search") or None,
            limit=None,
        )
    start = request.args.get("start") or "début"
    end = request.args.get("end") or "aujourd'hui"
    buf = xlsx_export.build_records_workbook(
        rows, title="Paiements",
        subtitle=f"Détail des paiements du {start} au {end} — export du {date.today().isoformat()}",
    )
    return send_file(
        buf, as_attachment=True,
        download_name=xlsx_export.export_filename("paiements"),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/confirm-cancel", methods=["POST"])
@login_required
def api_confirm_cancel():
    body = request.get_json(silent=True) or {}
    key = body.get("key")
    motif = body.get("motif")
    confirmed_by = body.get("confirmed_by") or session.get("username")
    if not key:
        return jsonify(error="Clé de paiement manquante."), 400
    try:
        with storage.connect(DB_PATH) as conn:
            storage.confirm_cancellation(conn, key, motif, confirmed_by)
            storage.log_action(conn, current_actor(), "confirmation annulation", key)
    except KeyError as exc:
        return jsonify(error=str(exc)), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Journal des imports + liste des banques connues (pour les filtres)
# ---------------------------------------------------------------------------
@app.route("/api/uploads")
@login_required
def api_uploads():
    with storage.connect(DB_PATH) as conn:
        return jsonify(storage.get_uploads(conn))


@app.route("/api/banks")
@login_required
def api_banks():
    with storage.connect(DB_PATH) as conn:
        return jsonify(storage.get_banks(conn))


# ---------------------------------------------------------------------------
# Administration (admin uniquement) : comptes utilisateurs, journal d'audit,
# réinitialisation complète de la base.
# ---------------------------------------------------------------------------
@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_admin_list_users():
    with storage.connect(DB_PATH) as conn:
        return jsonify(auth.list_users(conn))


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def api_admin_create_user():
    body = request.get_json(silent=True) or {}
    try:
        with storage.connect(DB_PATH) as conn:
            auth.create_user(
                conn, body.get("username"), body.get("password"),
                body.get("role"), created_by=current_actor(),
            )
            storage.log_action(conn, current_actor(), "création compte", body.get("username"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_user(user_id):
    with storage.connect(DB_PATH) as conn:
        target = auth.get_user(conn, user_id)
        try:
            auth.delete_user(conn, user_id)
        except KeyError as exc:
            return jsonify(error=str(exc)), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        storage.log_action(
            conn, current_actor(), "suppression compte",
            target["username"] if target else str(user_id),
        )
    # Un administrateur qui vient de supprimer son propre compte perd sa session.
    if session.get("user_id") == user_id:
        session.clear()
    return jsonify(ok=True)


@app.route("/api/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def api_admin_reset_password(user_id):
    body = request.get_json(silent=True) or {}
    try:
        with storage.connect(DB_PATH) as conn:
            auth.reset_password(conn, user_id, body.get("password"))
            target = auth.get_user(conn, user_id)
            storage.log_action(
                conn, current_actor(), "réinitialisation mot de passe",
                target["username"] if target else str(user_id),
            )
    except KeyError as exc:
        return jsonify(error=str(exc)), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


RESET_CONFIRMATION_PHRASE = "REINITIALISER LA BASE"


@app.route("/api/admin/reset-database", methods=["POST"])
@admin_required
def api_admin_reset_database():
    body = request.get_json(silent=True) or {}
    motif = (body.get("motif") or "").strip()
    phrase = (body.get("phrase") or "").strip()
    password = body.get("password") or ""

    if not motif:
        return jsonify(error="Un motif écrit est obligatoire."), 400
    if phrase != RESET_CONFIRMATION_PHRASE:
        return jsonify(error=f"Phrase de confirmation incorrecte. Tapez exactement : {RESET_CONFIRMATION_PHRASE}"), 400

    with storage.connect(DB_PATH) as conn:
        # Triple vérification avant une action irréversible : rôle admin (déjà
        # garanti par le décorateur), phrase tapée à l'identique, ET mot de
        # passe du compte actuellement connecté ressaisi ici.
        user = auth.get_user(conn, session["user_id"])
        if user is None or not check_password_hash(user["password_hash"], password):
            return jsonify(error="Mot de passe incorrect."), 401

        storage.reset_all_data(conn)
        storage.log_action(conn, current_actor(), "RÉINITIALISATION BASE DE DONNÉES", motif)

    return jsonify(ok=True)


@app.route("/api/admin/audit-log")
@admin_required
def api_admin_audit_log():
    with storage.connect(DB_PATH) as conn:
        return jsonify(storage.get_audit_log(conn))


if __name__ == "__main__":
    port = int(os.environ.get("RECOUVREMENT_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
