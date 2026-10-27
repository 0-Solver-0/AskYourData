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

# 4. Sur http://localhost:8069, créer la base nommée "odoo"
#    avec "Load demonstration data" coché

# 5. Services AskYourData
docker compose up -d --build

# 6. Vérifier l'état
curl http://localhost:5000/api/v1/health
```

> Le script `postgres/init/01_init.sql` n'est exécuté qu'à la **création** du volume `nl2sql_postgres_data`. Si ce volume existe déjà sans les tables `nl2sql_*`, le recréer (`docker compose down` puis `docker volume rm nl2sql_postgres_data`) ou exécuter le script à la main.

## Première utilisation

```bash
# Indexer les tables Odoo (une fois)
curl -X POST http://localhost:5000/api/v1/index \
  -H "Content-Type: application/json" \
  -d '{"account_id": 1}'

# Question → schéma réduit (sql = null tant que la génération n'est pas branchée)
curl -X POST http://localhost:5000/api/v1/translate \
  -H "Content-Type: application/json" \
  -d '{"question": "Who are the top 5 customers by revenue?", "account_id": 1}'
```

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

Toute branche part de `devlop` et toute PR cible `devlop` (modèle de PR fourni). La CI vérifie le lint (`ruff check .`) et la validité des fichiers compose.
