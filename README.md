# Registre du Recouvrement — version interne

Application interne (serveur + page web) qui reconstitue une **base cumulative**
des paiements de recouvrement à partir des fichiers Excel hebdomadaires reçus,
même quand ces fichiers sont mal structurés (libellés de banques mal
orthographiés, dates mal saisies, ordre des lignes différent d'une semaine à
l'autre). Elle permet d'obtenir à tout moment :

- le montant total recouvré sur une période donnée (X à Y) ;
- la répartition par opérateur : Orange, MTN, Autres ;
- le total de l'exercice ;
- la répartition par banque ;
- un panneau "À vérifier" listant les anomalies de saisie et les paiements
  disparus d'un fichier à l'autre (à confirmer avec un motif écrit avant
  d'être exclus).

Toute la logique d'analyse est en Python, côté serveur, dans `parser.py`. Le
navigateur ne fait qu'afficher les résultats et envoyer les fichiers — il n'y a
qu'une seule version de la logique à maintenir (contrairement à un prototype
100% navigateur qui devrait dupliquer cette logique en JavaScript).

## Installation

Il faut Python 3.9 ou plus récent, installé sur le poste ou serveur qui hébergera
l'outil sur le réseau interne.

```bash
cd recouvrement_app
python -m venv venv
source venv/bin/activate      # sous Windows : venv\Scripts\activate
pip install -r requirements.txt
```

## Lancement

```bash
python app.py
```

Le serveur démarre sur le port 5000 et écoute sur toutes les interfaces
réseau. Vos collègues, depuis n'importe quel poste du même réseau interne,
ouvrent simplement dans leur navigateur :

```
http://<adresse-ip-du-poste-serveur>:5000
```

(Sur le poste serveur lui-même, `http://localhost:5000` fonctionne aussi.)

Au tout premier lancement, un compte **administrateur** est créé
automatiquement avec un mot de passe aléatoire, écrit une seule fois dans
`admin_initial_password.txt` à côté de la base — voir la section
« Authentification et rôles » ci-dessous pour la marche à suivre.

Pour changer le port ou l'emplacement de la base de données :

```bash
# Linux / macOS
RECOUVREMENT_PORT=8080 RECOUVREMENT_DB=/chemin/vers/recouvrement.db python app.py

# Windows (PowerShell)
$env:RECOUVREMENT_PORT="8080"; $env:RECOUVREMENT_DB="C:\data\recouvrement.db"; python app.py
```

## Utiliser un nom plutôt que l'adresse IP (ex. « registre-recouvrement »)

Par défaut, vos collègues doivent taper l'adresse IP du poste serveur
(`http://192.168.x.x:5000`), ce qui change si cette adresse change. Trois
façons d'avoir un nom fixe à la place — à faire une seule fois, par
quelqu'un ayant accès au réseau :

1. **Le plus simple : nom mDNS (`.local`)**. Renommez le poste ou serveur
   qui héberge l'outil (ex. `registre-recouvrement`), puis vos collègues
   utilisent `http://registre-recouvrement.local:5000`. Cela fonctionne
   sans rien configurer sur Windows 10+ et macOS ; sous Linux, il faut que
   le service `avahi-daemon` tourne (`sudo apt install avahi-daemon` si
   absent). Fonctionne uniquement si tout le monde est sur le même réseau
   local (pas de VPN inter-sites).
2. **Le plus fiable pour beaucoup de collègues / plusieurs sites** : demandez
   à la personne qui gère le réseau (ou le contrôleur de domaine / la box
   internet) d'ajouter une entrée DNS interne : `registre-recouvrement` →
   l'adresse IP du serveur. Ensuite, tout le monde utilise simplement
   `http://registre-recouvrement:5000`, quel que soit son poste.
3. **Sans accès au DNS/réseau** : sur chaque poste collègue, ajoutez une
   ligne dans le fichier `hosts` (`C:\Windows\System32\drivers\etc\hosts`
   sous Windows, `/etc/hosts` sous macOS/Linux) :
   ```
   192.168.x.x    registre-recouvrement
   ```
   Simple mais à refaire sur chaque poste, et à corriger si l'IP du
   serveur change — pensez à réserver une IP fixe pour ce poste dans les
   paramètres de votre routeur/box (réservation DHCP) pour éviter ce
   problème.

Pour supprimer complètement le `:5000` de l'adresse (`http://registre-recouvrement/`
tout court), lancez l'outil sur le port 80 : `RECOUVREMENT_PORT=80 python app.py`
(sous Linux/macOS, le port 80 demande généralement les droits administrateur,
donc `sudo`) — ou placez l'outil derrière un reverse proxy (nginx, IIS…) qui
gère aussi le HTTPS, recommandé si l'outil doit un jour être accessible
au-delà du strict réseau interne.

## Où sont les données

Tout l'historique est stocké dans **un seul fichier** : `recouvrement.db`
(créé automatiquement au premier lancement, à côté de `app.py`). C'est le
fichier à sauvegarder régulièrement — une simple copie suffit, aucune base de
données séparée à installer.

Conseil : placez ce dossier sur un poste ou serveur qui reste allumé et
accessible sur le réseau interne, et programmez une copie automatique
régulière de `recouvrement.db` vers un emplacement de sauvegarde (lecteur
réseau partagé, etc.).

## Authentification et rôles

L'outil est protégé par un système de comptes avec deux rôles :

- **Administrateur** : peut tout faire — importer, consulter, exporter,
  confirmer des annulations, **gérer les comptes utilisateurs**, et
  **réinitialiser complètement la base de données** (tout effacer pour
  pouvoir tout reconstituer proprement, ex. en cas d'erreur de fond dans
  l'historique) ;
- **Utilisateur** : peut importer, consulter, exporter et confirmer une
  annulation — mais ne peut ni gérer les comptes, ni réinitialiser la base.

### Première connexion

Au tout premier lancement de `python app.py`, un compte `admin` est créé
automatiquement avec un mot de passe aléatoire, écrit **une seule fois** dans
`admin_initial_password.txt` (à côté de `recouvrement.db`). Marche à suivre :

1. Ouvrez `admin_initial_password.txt` et notez le mot de passe.
2. Connectez-vous sur `http://<adresse-du-serveur>:5000/login` avec
   `admin` / ce mot de passe.
3. Dans la section « Administration » en bas de page, créez un compte
   nommément pour chaque collègue (rôle Utilisateur, ou Administrateur pour
   les personnes qui doivent pouvoir réinitialiser la base).
4. **Supprimez `admin_initial_password.txt`** — il ne doit pas rester en
   clair sur le serveur une fois la première connexion effectuée.

### Gestion des comptes

Réservée aux administrateurs, dans la section « Administration » de la page
principale : création de compte (nom, mot de passe de 8 caractères minimum,
rôle), réinitialisation du mot de passe d'un compte, suppression d'un
compte. Le dernier compte administrateur restant ne peut pas être supprimé,
pour éviter de se retrouver sans accès à l'administration.

### Réinitialiser la base de données

Dans la « Zone sensible » de la section Administration. Cette action efface
**définitivement** tous les paiements et tout le journal des imports — utile
si l'historique doit être reconstruit proprement depuis le début (par
exemple après la découverte d'une erreur de fond dans les premiers imports).
Les comptes utilisateurs et le journal d'audit, eux, sont conservés.

Trois confirmations sont exigées avant l'effacement, pour éviter tout clic
accidentel :
1. un motif écrit expliquant pourquoi ;
2. taper exactement la phrase `REINITIALISER LA BASE` ;
3. ressaisir son propre mot de passe.

**Avant de réinitialiser, faites une copie de `recouvrement.db`** si vous
n'êtes pas certain·e de pouvoir réimporter tous les fichiers Excel
nécessaires pour reconstituer l'historique — l'action est irréversible.

### Journal d'audit

Toujours dans la section Administration : connexions, créations/suppressions
de comptes, réinitialisations de mot de passe, imports de fichiers,
confirmations d'annulation, et bien sûr la réinitialisation de la base,
avec l'auteur et la date de chaque action.

### Limites de sécurité à connaître

Cet outil est conçu pour un usage **interne, sur un réseau de confiance**
(bureau, VPN d'entreprise) :
- pas de HTTPS intégré — si le serveur est exposé au-delà du réseau interne,
  placez-le derrière un reverse proxy (nginx, IIS…) qui gère le HTTPS ;
- pas de protection CSRF dédiée — raison de plus pour ne pas exposer l'outil
  directement sur Internet ;
- les sessions expirent après 12 heures d'inactivité (cookie signé, non
  modifiable sans connaître la clé secrète stockée localement dans
  `.secret_key`).

## Utilisation au quotidien

1. Chaque semaine, à réception du nouveau fichier Excel de l'autre direction,
   un agent le glisse-dépose sur la page d'accueil de l'outil.
2. L'outil compare le fichier à tout l'historique et affiche un résumé :
   combien de paiements sont nouveaux, combien étaient déjà connus, combien
   de paiements précédemment actifs sont absents de ce nouvel extrait (donc
   signalés "à vérifier"), et combien de lignes ont une anomalie de saisie.
3. Le panneau "À vérifier" liste ces cas. Pour un paiement disparu que l'on a
   vérifié et dont on comprend l'explication (ex. correction du nom du
   payeur), un agent clique "Confirmer l'annulation" et **doit** saisir un
   motif écrit — ce motif reste enregistré de façon permanente avec le
   paiement concerné, à des fins d'audit.
4. Les KPI (total, Orange/MTN/Autres, par banque) se recalculent
   automatiquement selon la période sélectionnée (boutons rapides ou dates
   personnalisées).

## Pourquoi "Tout l'historique" et "Exercice en cours" affichent des totaux différents

Les totaux se basent sur la **date réelle du paiement** (celle indiquée dans le
fichier source), jamais sur la date d'import. "Exercice en cours" filtre donc sur
l'année civile en cours (1er janvier - 31 décembre), tandis que "Tout l'historique"
additionne tous les paiements actifs, quelle que soit leur date — y compris ceux
dont la date est manifestement erronée (une ligne datée par erreur en 2025 ou en
2029, par exemple). Ces dates suspectes sont précisément ce que le panneau
"À vérifier" signale (`needs_review`) ; c'est normal et volontaire de les voir
apparaître dans l'historique complet mais disparaître d'un filtre par exercice.

## Reconstituer un montant "à date" pour une période passée

Cas typique : le rapport du 10 au 20 juin annonçait 20 millions ; celui du 21 au
30 juin annonce 25 millions ; mais en réalité, seuls 3 millions ont été
véritablement recouvrés entre le 21 et le 30 juin, et 2 millions supplémentaires
concernant la période du 10 au 20 juin n'ont été révélés que dans le second
fichier (paiement tardif, correction de l'autre direction, etc.).

L'outil est conçu justement pour ce cas : comme chaque paiement est rattaché à sa
**date réelle** et non à la date de réception du fichier qui l'a révélé, il suffit
de resélectionner la période "10 au 20 juin" dans l'outil *après* l'import du
second fichier pour voir apparaître les 2 millions supplémentaires — automatiquement
rattachés à la bonne période, sans double comptage (la clé unique empêche qu'un
paiement déjà compté ne soit compté deux fois).

Point important : un total pour une période passée n'est donc pas figé, il peut
augmenter si un import ultérieur révèle un paiement tardif pour cette période.
C'est le comportement voulu — mieux vaut toujours régénérer un chiffre à présenter
(bouton "Exporter en Excel") au moment de la présentation, plutôt que de réutiliser
un total noté plusieurs semaines auparavant.

## Le panneau "À vérifier"

Il n'affiche **pas tous les paiements** — uniquement ceux qui nécessitent une
vérification humaine :
- anomalies de saisie détectées automatiquement (date mal tapée, colonne
  "Recouvrement" vide, ligne datée hors du bloc banque/mois où elle se trouve…) ;
- paiements actifs lors d'un import précédent mais absents du dernier import
  reçu — en attente de confirmation ("Confirmer l'annulation", avec motif écrit
  obligatoire) avant d'être définitivement exclus des totaux.

Tous les paiements (y compris ceux sans anomalie) se trouvent dans le tableau
"Détail des paiements" plus bas sur la page.

## Voir le détail derrière un chiffre (Total / Orange / MTN / Autres)

Les quatre tuiles en haut de page sont cliquables : cliquer sur « Orange »,
par exemple, filtre immédiatement le tableau « Détail des paiements »
ci-dessous sur cette catégorie (pour la période actuellement sélectionnée)
et vous y amène directement — vous y voyez l'exploitant, le libellé, la
banque, le n° de facture (si disponible), le montant et le statut de chaque
paiement qui compose ce total. Le bouton « Exporter en Excel » juste
au-dessus du tableau reprend exactement le même filtre.

## Exporter en Excel

Deux boutons "Exporter en Excel" génèrent un classeur `.xlsx` à la volée :
- dans le panneau "À vérifier" : exporte uniquement les anomalies et paiements
  en attente, pour investigation ou transmission ;
- dans "Détail des paiements" : exporte l'intégralité des paiements correspondant
  aux filtres actuellement affichés (période, catégorie, banque, recherche) —
  contrairement au tableau à l'écran, l'export n'est pas limité à 2000 lignes.

## Logo et couleurs (charte ART)

La page utilise une palette bleue par défaut (voir les variables `--ink` et
`--accent` en haut de `static/style.css`) ; ajustez ces deux couleurs si vous avez
les codes hexadécimaux exacts de la charte graphique de l'ART.

Pour afficher le vrai logo de l'ART dans le bandeau : déposez votre fichier logo
(de bonne qualité, format `.png` avec fond transparent de préférence) sous
`static/logo.png`. Tant qu'aucun fichier n'est présent, un badge de repli "ART"
s'affiche automatiquement à la place — aucune autre modification n'est nécessaire.

## Si le fichier source change de format

Le parsing suppose que les colonnes suivent l'ordre habituel du fichier reçu
(B = date, C = libellé, D = raison sociale, F = montant, G = montant
recouvré, L = numéro de facture) sur une feuille nommée "TRAIT R" (ou, à
défaut, la première feuille du classeur). Si l'autre direction change
significativement la structure du fichier, il faut adapter `parser.py` en
conséquence — c'est le seul fichier qui contient la logique de lecture.

## Fichiers du projet

```
recouvrement_app/
  app.py                      serveur Flask (routes web + API + authentification)
  auth.py                     comptes utilisateurs et rôles (admin / user)
  parser.py                   lecture et fiabilisation des fichiers Excel (logique métier)
  storage.py                  base cumulative (SQLite) : fusion, statuts, requêtes, audit
  xlsx_export.py               génération des exports Excel (.xlsx) à la volée
  requirements.txt            dépendances Python
  pages/
    index.html                page principale de l'application (protégée par connexion)
    login.html                page de connexion (publique)
  static/
    app.js                    logique côté navigateur (appels à l'API, affichage)
    style.css                 mise en forme (palette bleue — voir "Logo et couleurs")
    logo.png                  (à ajouter vous-même — voir "Logo et couleurs")
  recouvrement.db             (créé automatiquement — c'est votre base de données)
  .secret_key                 (créé automatiquement — clé de signature des sessions, à garder secrète)
  admin_initial_password.txt  (créé automatiquement au 1er lancement — à supprimer après usage)
```

Notes sur `pages/` vs `static/` : seul `static/` est servi directement par le
serveur web (feuilles de style, script, logo) ; `pages/index.html` n'est
jamais accessible sans être connecté, et `pages/login.html` uniquement via
`/login`. C'est ce qui empêche de contourner la connexion en devinant
une adresse directe vers la page principale.
