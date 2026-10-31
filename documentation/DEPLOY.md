# Déploiement du test public (AWS EC2)

Objectif : ouvrir l'interface à des testeurs (jury, réseau LinkedIn). Chaque personne reçoit une **clé
personnelle**, avec un quota de questions. **Rien ne se fait sans clé valide**, et les routes
d'administration ne sont pas accessibles depuis Internet.

Rien n'est réindexé en production : on **exporte** la base NL2SQL (index, enrichissement, vecteurs) et
la base Odoo de démo, puis on les restaure sur le serveur.

## 1. Ce qui tourne sur le serveur

| Conteneur | Rôle | Exposé |
|---|---|---|
| `askyourdata_caddy` | HTTPS (Let's Encrypt), liste blanche des routes | **80, 443** |
| `askyourdata_api` | interface, questions, clés | `127.0.0.1:5000` (tunnel SSH seulement) |
| `nl2sql_schema` | recherche de schéma, DRAMA sur CPU | non |
| `nl2sql_postgres` | index, enrichissement, clés, journal des questions | non |
| `odoo_postgres` | base Odoo de démo (lue en lecture seule) | non |

L'application Odoo (port 8069) **n'est pas lancée** : seule sa base est nécessaire. Les modèles de
langage sont appelés à distance (API Meta, Baseten) : **aucun GPU**.

Routes publiques (tout le reste répond 404) : `/`, `/v2`, `/static/*`, `/robots.txt`,
`/api/v1/ask`, `/api/v1/transcribe`, `/api/v1/access/me`, `/api/v1/health` (réduit).
La liste est appliquée deux fois : par Caddy (`deploy/Caddyfile`) et par l'API (`PUBLIC_MODE`).

## 2. Créer l'instance
- Type **`m7i.large`** (2 vCPU, 8 Go) ; `m7i.xlarge` pour plus de marge. Pas de `t3`
  (crédits CPU épuisés pendant les pics).
- Ubuntu 24.04 LTS, disque **30 Go gp3**, une **Elastic IP**.
- Groupe de sécurité :
  - 22 (SSH) : **ta seule adresse IP** ;
  - 80 et 443 : tout le monde.
  - Rien d'autre.
- Nom de domaine : le tien, ou sans domaine **`<ip-avec-tirets>.sslip.io`**
  (ex. `3-120-45-67.sslip.io` pour 3.120.45.67). **HTTPS est obligatoire** : sans lui, le navigateur
  refuse le micro.

## 3. Préparer le serveur
```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
git clone https://github.com/0-Solver-0/AskYourData.git && cd AskYourData && git checkout main
```
Dépôt privé : utiliser un jeton GitHub en lecture seule, ou une clé de déploiement.

## 4. Copier ce qui n'est pas dans Git (depuis ton PC)
```powershell
scp .env ubuntu@<ip>:~/AskYourData/.env
scp -r models/drama-base ubuntu@<ip>:~/AskYourData/models/
```
Dans le `.env` du serveur :
- `SITE_DOMAIN=<domaine>` ;
- mots de passe **forts** pour `NL2SQL_DB_PASSWORD`, `ODOO_DB_PASSWORD`, `EXEC_DB_PASSWORD` ;
- `ACCESS_DEFAULT_QUOTA=20`, `ACCESS_GLOBAL_BUDGET_USD=20`, `ACCESS_MIN_INTERVAL_S=5` ;
- **le même modèle d'embedding qu'en local** : `EMBEDDER_MODEL=drama-base`, `EMBEDDER_BACKEND=drama`,
  `EMBEDDER_TRUST_REMOTE_CODE=true`.

Piège principal : si le modèle diffère, `schema_service` vide les vecteurs au démarrage et demande une
réindexation.

## 5. Exporter les deux bases (sur ton PC, PowerShell)
Ne pas utiliser `>` dans PowerShell : il abîme les fichiers binaires. Écrire dans le conteneur puis copier.
```powershell
docker exec odoo_postgres pg_dump -U odoo -Fc -f /tmp/odoo.dump Meta_Global_AI_Hackathon
docker cp odoo_postgres:/tmp/odoo.dump .\odoo.dump
docker exec nl2sql_postgres pg_dump -U nl2sql -Fc -f /tmp/nl2sql.dump nl2sql
docker cp nl2sql_postgres:/tmp/nl2sql.dump .\nl2sql.dump
scp odoo.dump nl2sql.dump ubuntu@<ip>:~/AskYourData/
```

## 6. Restaurer sur le serveur
```bash
cd ~/AskYourData
# Base Odoo (crée aussi le réseau nl2sql_net) — conteneur PostgreSQL seul, sans port publié
docker compose -f docker-compose.odoo.yml -f docker-compose.odoo.prod.yml up -d postgres_odoo
docker cp odoo.dump odoo_postgres:/tmp/odoo.dump
docker exec odoo_postgres createdb -U odoo Meta_Global_AI_Hackathon
docker exec odoo_postgres pg_restore -U odoo -d Meta_Global_AI_Hackathon --no-owner --no-privileges /tmp/odoo.dump

# Base NL2SQL (index, enrichissement, vecteurs)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgres_nl2sql
docker cp nl2sql.dump nl2sql_postgres:/tmp/nl2sql.dump
docker exec nl2sql_postgres pg_restore -U nl2sql -d nl2sql --clean --if-exists --no-owner /tmp/nl2sql.dump

# Rôle en lecture seule sur la base Odoo (les rôles ne sont pas dans le dump)
docker run --rm --network nl2sql_net -v $PWD:/w -w /w python:3.11-slim sh -c \
  "pip install -q psycopg2-binary && python tools/create_readonly_role.py"
```

## 7. Démarrer et vérifier
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker exec askyourdata_api python -c "import requests; print(requests.get('http://localhost:5000/api/v1/health', timeout=30).text)"
```
À vérifier dans la réponse :
- `"reindex_required": false` (sinon le modèle d'embedding diffère : revoir l'étape 4) ;
- `execution.status = ok` ;
- `voice.recording`.

Ensuite :
- `https://<domaine>/` doit demander une clé ;
- `https://<domaine>/api/v1/index` doit répondre 404.

## 8. Clés d'accès
```bash
docker exec askyourdata_api python tools/keys.py create --name "Jury Meta" --quota 50
docker exec askyourdata_api python tools/keys.py create --name "Karim (LinkedIn)"      # 20 questions
docker exec askyourdata_api python tools/keys.py list
docker exec askyourdata_api python tools/keys.py quota  --name "Karim (LinkedIn)" --quota 30
docker exec askyourdata_api python tools/keys.py revoke --name "Karim (LinkedIn)"
docker exec askyourdata_api python tools/keys.py forget --name "Karim (LinkedIn)"      # efface ses questions
docker exec askyourdata_api python tools/keys.py export > questions.json              # sans les noms
```
La clé s'affiche **une seule fois**, à sa création : seule son empreinte est enregistrée.

## 9. Tableau de bord (tunnel SSH, jamais public)
```powershell
ssh -L 5000:127.0.0.1:5000 ubuntu@<ip>
```
Puis ouvrir `http://localhost:5000/admin` sur ton PC. On y trouve :
- les personnes actives (5 dernières minutes) ;
- les questions et transcriptions ;
- le coût estimé face au plafond global ;
- l'état de chaque clé ;
- les dernières questions (question posée, question reformulée, statut, coût, temps).

## 10. Limites et protections
- **Quota par clé** : 20 questions par défaut (50 pour le jury), voix et texte confondus.
- **Plafond global** (`ACCESS_GLOBAL_BUDGET_USD`) : une fois atteint, tout est bloqué avec un message.
  Le coût est **estimé** à partir des prix de `config/llm_providers.yaml`.
- **Débit** : 1 question toutes les 5 s par clé.
- **Clés fausses** : après 5 essais, l'adresse est bloquée 15 minutes. Ce compteur est en mémoire, par
  processus, et ne s'écrit nulle part.
- Taille des requêtes limitée : question de 500 caractères, requête de 2 Mo (audio de 20 s).
- `robots.txt` « Disallow » et en-tête `X-Robots-Tag: noindex` : le site n'est pas indexé.
- Données conservées : questions et coûts dans `usage_log` (base NL2SQL), pour améliorer le système.
  Les testeurs en sont prévenus à la saisie de la clé. Pas d'audio, pas de lignes de résultat, pas
  d'adresse IP. Effacement sur demande avec `keys.py forget`.

## 11. Sauvegarde, mise à jour, arrêt
- **Sauvegarde** de la base NL2SQL (clés et questions), par exemple chaque nuit avec `cron` :
  `docker exec nl2sql_postgres pg_dump -U nl2sql -Fc -f /tmp/nl2sql.dump nl2sql && docker cp nl2sql_postgres:/tmp/nl2sql.dump ~/backups/nl2sql-$(date +%F).dump`.
  Un instantané EBS couvre tout le disque.
- **Mise à jour** : `git pull` sur `main`, puis `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.
- **Couper le test** : `docker stop askyourdata_caddy` (le site devient injoignable) ; ou
  `ACCESS_GLOBAL_BUDGET_USD=0` puis redémarrage de `api_service` (message « budget atteint »).
