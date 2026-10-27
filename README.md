# AskYourData

Parler avec ses données : une question en anglais sur Odoo 18, une réponse tirée directement de la base, sans écrire de SQL.

Architecture complète et décisions : [`documentation/ARCHITECTURE.md`](documentation/ARCHITECTURE.md).

> **État : en construction.** La recherche de schéma fonctionne ; la génération SQL (Muse Spark via API) et l'exécution sont en cours de branchement. `/api/v1/translate` renvoie pour l'instant le schéma réduit, avec `sql: null`.

## Architecture

```
question (EN)
   │
   ▼
api_service      :5000  Flask + Gunicorn   orchestre le pipeline
   │
   ├─► schema_service :8002  embedder local (CPU) + pgvector   tables, colonnes, relations
   └─► LLM via API (à venir)  rôle PREP : Muse Spark Open (Baseten) · rôle SQL : Muse Spark (API Meta)

postgres_nl2sql  :5433  pgvector/pg16       métadonnées + embeddings
postgres_odoo    :5432  postgres:16-alpine  base Odoo
odoo             :8069  odoo:18.0
```

Aucun GPU n'est nécessaire : les LLM sont appelés via API.

## Structure

```
.
├── Dockerfile.cpu           image unique (schema_service + api_service)
├── docker-compose.yml       postgres_nl2sql + schema_service + api_service
├── docker-compose.odoo.yml  Odoo 18 + postgres_odoo (crée le réseau nl2sql_net)
├── .env.example
├── models/                  embedder local (non versionné)
│   └── all-MiniLM-L6-v2/
├── schema_service/server.py
├── api_service/app.py
├── postgres/init/01_init.sql
├── odoo/
│   ├── config/odoo.conf
│   └── addons/
└── documentation/
```

## Démarrage

```bash
# 1. Variables d'environnement
cp .env.example .env

# 2. Embedder local (une fois, ~90 Mo)
pip install -U "huggingface_hub[cli]"
huggingface-cli download sentence-transformers/all-MiniLM-L6-v2 --local-dir models/all-MiniLM-L6-v2

# 3. Odoo en premier (crée aussi le réseau nl2sql_net)
docker compose -f docker-compose.odoo.yml up -d

# 4. Sur http://localhost:8069, créer une base avec "Load demonstration data" coché,
#    puis reporter son nom dans .env : SOURCE_DB_NAME=<nom de la base>

# 5. Services AskYourData
docker compose up -d --build

# 6. Vérifier l'état
curl http://localhost:5000/api/v1/health
```

> La connexion à la base source (compte par défaut, `account_id = 1`) vient des variables `SOURCE_DB_*` du `.env` et est réappliquée à chaque démarrage de `schema_service` : pour changer de base, modifier le `.env` puis `docker compose up -d`.
>
> Le schéma de la base NL2SQL (`schema_service/schema.sql`) est appliqué par `schema_service` à chaque démarrage : il est idempotent et met aussi à niveau une base existante. Aucune manipulation du volume n'est nécessaire.

## Première utilisation

```bash
# 1. Vérifier les filtres sans rien écrire : tables retenues / exclues avec la raison
curl -X POST http://localhost:5000/api/v1/index \
  -H "Content-Type: application/json" \
  -d '{"account_id": 1, "dry_run": true}'

# 2. Indexer les tables Odoo (structure, libellés Odoo, embeddings)
curl -X POST http://localhost:5000/api/v1/index \
  -H "Content-Type: application/json" \
  -d '{"account_id": 1}'

# 3. Relire l'index : volume de chaque table retenue, tables exclues et raisons
curl "http://localhost:5000/api/v1/index/report?account_id=1"

# Question → schéma réduit (sql = null tant que la génération n'est pas branchée)
curl -X POST http://localhost:5000/api/v1/translate \
  -H "Content-Type: application/json" \
  -d '{"question": "Who are the top 5 customers by revenue?", "account_id": 1}'
```

## Recherche de schéma : comment les tables sont choisies

Pour chaque question, `schema_service` (sans LLM, quelques millisecondes) :
1. note chaque table : similarité des vecteurs (table et meilleure colonne), correspondance de mots avec les libellés Odoo et le vocabulaire métier de [`config/search_synonyms.yaml`](config/search_synonyms.yaml), petit bonus pour les tables remplies et malus pour les tables vides ;
2. garde les meilleures tables, ajoute les **tables de jointure** (plus court chemin dans le graphe des clés étrangères) et les **tables de référence** citées par la question (« customer » → `sale_order.partner_id` → `res_partner`) ;
3. ne garde que les colonnes utiles (clés, colonnes citées, nom, état, dates, montants, plus proches de la question) ;
4. produit un schéma compact commenté (libellés Odoo, valeurs possibles des champs à choix, champs JSON traduisibles), sous un budget de tokens (`SCHEMA_TOKEN_BUDGET`).

Diagnostic : `POST /api/v1/schema/debug {"question": "...", "account_id": 1}` montre pour chaque table la raison du choix (`search`, `join:…`, `lookup:…`), le détail des scores, les colonnes gardées et le classement.

Contrôle sur la vraie base (sans coût) : `python benchmark/schema_search_eval.py` vérifie les tables attendues pour les questions de [`benchmark/schema_questions.yaml`](benchmark/schema_questions.yaml).

Une collection Postman est fournie : [`tools/postman/AskYourData.postman_collection.json`](tools/postman/AskYourData.postman_collection.json).

## Indexation : ce qui est retenu

- Les filtres sont dans [`config/index_filters.yaml`](config/index_filters.yaml) (monté dans le conteneur : modifier puis relancer l'indexation, sans reconstruire l'image).
- Sont exclus : les wizards Odoo (modèles transitoires), les vues QWeb, menus et tables techniques (`ir_*`, `mail_*`, `website_*`…), les tables de liaison (`*_rel`) et les tables sans modèle Odoo. Une liste blanche (`res_partner`, `res_users`…) est prioritaire.
- Les colonnes sensibles (`*password*`, `*secret*`, `*token*`…) ne sont jamais indexées.
- Les libellés Odoo (nom des modèles et des champs, aide, valeurs possibles des champs à choix) sont récupérés automatiquement.
- Une réindexation n'écrase jamais les annotations manuelles ni les descriptions.

## Annotations

```bash
# Annoter une table
curl -X POST http://localhost:5000/api/v1/annotate/table \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": 1,
    "table_name": "sale_order",
    "label":      "Sales orders",
    "synonyms":   "orders, sales, transactions",
    "domain":     "sales"
  }'

# Annoter une colonne
curl -X POST http://localhost:5000/api/v1/annotate/column \
  -H "Content-Type: application/json" \
  -d '{
    "account_id":  1,
    "table_name":  "sale_order",
    "column_name": "amount_total",
    "label":       "Total amount",
    "synonyms":    "total, amount, revenue",
    "display":     true
  }'
```

## Contribuer

Toute branche part de `devlop` et toute PR cible `devlop` (modèle de PR fourni). La CI vérifie le lint (`ruff check .`), les tests (`pytest`, avec PostgreSQL + pgvector) et la validité des fichiers compose.

Tests en local : `pip install -r requirements-dev.txt` puis `pytest -q` (les tests d'intégration nécessitent `TEST_PG_DSN` vers un PostgreSQL avec pgvector).
