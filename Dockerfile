# Registre du Recouvrement — image Docker
#
# Construit un environnement Python complet et isolé : plus besoin que le
# serveur hôte ait python3-venv, pip, ou une version particulière de Python
# déjà installés — tout est embarqué dans l'image.
#
# Construire :   docker compose build
# Lancer :       docker compose up -d
# (voir la section "Déploiement avec Docker" du README pour le détail)

FROM python:3.11-slim

WORKDIR /app

# Étape séparée pour profiter du cache Docker : les dépendances ne sont
# réinstallées que si requirements.txt change, pas à chaque modification
# du code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# La base de données, la clé de session et le mot de passe admin initial
# vivent tous dans /data (dérivé de RECOUVREMENT_DB — voir app.py) : c'est
# ce dossier qu'il faut monter en volume pour ne jamais perdre l'historique
# entre deux redéploiements de l'image (voir docker-compose.yml).
ENV RECOUVREMENT_DB=/data/recouvrement.db
ENV RECOUVREMENT_PORT=5000

EXPOSE 5000

CMD ["python", "app.py"]
