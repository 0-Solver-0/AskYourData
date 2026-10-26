# NL2SQL v2

Pipeline multi-agent NL → SQL sur Odoo 18.

## Architecture

```
Question utilisateur (français)
        │
        ▼
rewrite_service  :8003  Qwen2.5 3B   GPU ~3 Go   traduit FR→EN
        │
        ▼
schema_service   :8002  all-MiniLM   CPU         sélectionne tables + colonnes + relations
        │
        ▼
model_service    :8001  SQLCoder 7B  GPU ~7 Go   génère le SQL
        │
        ▼
api_service      :5000  Flask        CPU         orchestre + retourne résultat
```

## Structure

```
nl2sql/
├── Dockerfile              ← GPU  (model_service + rewrite_service)
├── Dockerfile.cpu          ← CPU  (schema_service + api_service)
├── docker-compose.yml      ← services NL2SQL + postgres_nl2sql
├── docker-compose.odoo.yml ← Odoo 18 + postgres_odoo
├── .env.example
├── models/
│   ├── sqlcoder-7b-2/
│   ├── qwen2.5-3b-instruct/
│   └── all-MiniLM-L6-v2/
├── model_service/
│   └── server.py
├── rewrite_service/
│   └── server.py
├── schema_service/
│   └── server.py
├── api_service/
│   └── app.py
├── postgres/
│   └── init/
│       └── 01_init.sql
└── odoo/
    ├── config/odoo.conf
    └── addons/
```

## Démarrage

```bash
# 1. Copier les variables d'environnement
cp .env.example .env

# 2. Démarrer Odoo en premier
docker compose -f docker-compose.odoo.yml up -d

# 3. Attendre qu'Odoo soit prêt, puis aller sur http://localhost:8069
#    Créer la base avec "Load demonstration data" coché

# 4. Démarrer NL2SQL
docker compose up -d

# 5. Vérifier l'état
curl http://localhost:5000/api/v1/health
```

## Première utilisation

```bash
# Indexer les tables Odoo (à faire une seule fois)
curl -X POST http://localhost:5000/api/v1/index \
  -H "Content-Type: application/json" \
  -d '{"account_id": 1}'

# Traduire une question
curl -X POST http://localhost:5000/api/v1/translate \
  -H "Content-Type: application/json" \
  -d '{"question": "Quels sont les 5 meilleurs clients ?", "account_id": 1}'
```

## Annotations

```bash
# Annoter une table
curl -X POST http://localhost:5000/api/v1/annotate/table \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": 1,
    "table_name": "sale_order",
    "label":      "Commandes clients",
    "synonyms":   "commandes, orders, ventes, transactions",
    "domain":     "commercial"
  }'

# Annoter une colonne
curl -X POST http://localhost:5000/api/v1/annotate/column \
  -H "Content-Type: application/json" \
  -d '{
    "account_id":  1,
    "table_name":  "sale_order",
    "column_name": "amount_total",
    "label":       "Montant total",
    "synonyms":    "total, montant, panier, chiffre d affaires",
    "display":     true
  }'
```

## VRAM

```
SQLCoder 7B   float16   7.0 Go
Qwen2.5 3B    float16   3.0 Go
─────────────────────────────
Total                  10.0 Go / 11.6 Go  ✅
```
