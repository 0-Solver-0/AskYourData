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
   └─► LLM via API            rôle PREP et rôle SQL : Muse Spark 1.3 (API Meta), secours Muse Spark Open (Baseten)

postgres_nl2sql  :5433  pgvector/pg16       métadonnées + embeddings
postgres_odoo    :5432  postgres:16-alpine  base Odoo
odoo             :8069  odoo:18.0
```

Aucun GPU n'est nécessaire : les LLM sont appelés via API. Un GPU NVIDIA peut accélérer l'embedder local (voir « Embedder »).

## Structure

```
.
├── Dockerfile.cpu           image unique (schema_service + api_service)
├── docker-compose.yml       postgres_nl2sql + schema_service + api_service
├── docker-compose.odoo.yml  Odoo 18 + postgres_odoo (crée le réseau nl2sql_net)
├── .env.example
├── docker-compose.gpu.yml   option : GPU NVIDIA pour l'embedder
├── models/                  embedders locaux (non versionnés)
│   ├── all-MiniLM-L6-v2/
│   └── Qwen3-Embedding-0.6B/   (option)
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

# 2. Embedder local (une fois, ~90 Mo) — autres modèles : voir « Embedder »
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
2. reconnaît les **valeurs citées** dans la question (noms de pays, d'étapes, de clients… indexés à partir de la colonne `name` des petites tables : « Morocco » → `res_country`) ;
3. garde les meilleures tables, ajoute les **tables de jointure** (plus court chemin dans le graphe des clés étrangères) et les **tables de référence** citées par la question (« customer » → `sale_order.partner_id` → `res_partner`) ;
4. ne garde que les colonnes utiles (clés, colonnes citées, nom, état, dates, montants, plus proches de la question) ;
5. produit un schéma compact commenté (libellés Odoo, valeurs possibles des champs à choix, champs JSON traduisibles), sous un budget de tokens (`SCHEMA_TOKEN_BUDGET`).

Diagnostic : `POST /api/v1/schema/debug {"question": "...", "account_id": 1}` montre pour chaque table la raison du choix (`search`, `join:…`, `lookup:…`), le détail des scores, les colonnes gardées et le classement.

Contrôle sur la vraie base (sans coût) : `python benchmark/schema_search_eval.py` vérifie les tables attendues pour les questions de [`benchmark/schema_questions.yaml`](benchmark/schema_questions.yaml). Il affiche le rappel, la précision (tables utiles / tables envoyées), les tables par question, les tokens, le temps et le rang des tables attendues par vecteur seul. Historique des mesures : [`benchmark/HISTORY.md`](benchmark/HISTORY.md).

Une collection Postman est fournie : [`tools/postman/AskYourData.postman_collection.json`](tools/postman/AskYourData.postman_collection.json).

## Embedder

Le modèle qui transforme tables, colonnes et questions en vecteurs se choisit dans le `.env`, sans toucher au code (`schema_service/embedder.py`).

| Variable | Rôle |
|---|---|
| `EMBEDDER_MODEL` | dossier du modèle dans `./models/` (défaut `all-MiniLM-L6-v2`) |
| `EMBEDDER_DEVICE` | `cpu`, `cuda` ou `auto` |
| `EMBEDDER_DIM` | dimension réduite pour les modèles qui l'acceptent (Qwen3-Embedding : 32 à 1024) ; vide = dimension native |
| `EMBEDDER_PROMPT_TABLES` / `EMBEDDER_PROMPT_COLUMNS` | consignes ajoutées aux questions ; mises automatiquement pour Qwen3-Embedding, aucune pour MiniLM |

**Changer de modèle** : au démarrage, `schema_service` compare le modèle et la dimension avec ceux de l'index. S'ils diffèrent, il adapte la base et vide les vecteurs, puis `/health` indique `"reindex_required": true`. Il suffit alors de relancer `POST /api/v1/index`. Les annotations, descriptions et valeurs sont conservées.

```bash
# Exemple : Qwen3-Embedding-0.6B (~1,2 Go)
huggingface-cli download Qwen/Qwen3-Embedding-0.6B --local-dir models/Qwen3-Embedding-0.6B
# .env : EMBEDDER_MODEL=Qwen3-Embedding-0.6B
docker compose up -d --build
curl -X POST http://localhost:5000/api/v1/index -H "Content-Type: application/json" -d '{"account_id": 1}'
```

**GPU NVIDIA (option)** : PyTorch CUDA 12.8, compatible avec les RTX 50xx.
```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
curl http://localhost:5000/api/v1/health     # services.schema.embedder.device = "cuda"
```
Sans GPU, tout fonctionne sur CPU : le GPU accélère l'indexation et la recherche, il n'est pas nécessaire.

## Indexation : ce qui est retenu

- Les filtres sont dans [`config/index_filters.yaml`](config/index_filters.yaml) (monté dans le conteneur : modifier puis relancer l'indexation, sans reconstruire l'image).
- Sont exclus : les wizards Odoo (modèles transitoires), les vues QWeb, menus et tables techniques (`ir_*`, `mail_*`, `website_*`…), les tables de liaison (`*_rel`) et les tables sans modèle Odoo. Une liste blanche (`res_partner`, `res_users`…) est prioritaire.
- Les colonnes sensibles (`*password*`, `*secret*`, `*token*`…) ne sont jamais indexées.
- Les libellés Odoo (nom des modèles et des champs, aide, valeurs possibles des champs à choix) sont récupérés automatiquement.
- Une réindexation n'écrase jamais les annotations manuelles ni les descriptions.

## Recherche à partir d'une question découpée (nouvelle forme, en test)

La question est découpée en **morceaux de sens**. Le sujet est cherché parmi les fiches de tables, et chaque autre morceau parmi les colonnes des tables trouvées. Le découpage se fait **à la main** pour l'instant ; le rôle PREP (Muse Spark) le produira ensuite.

```bash
curl -X POST http://localhost:5000/api/v1/schema/structured -H "Content-Type: application/json" -d '{
  "account_id": 1,
  "question": "Customers with the highest unpaid invoice amount, by salesperson",
  "structured": {
    "subject":  "customer invoices",
    "measures": ["amount due"],
    "filters":  ["document type", "status", "payment status"],
    "group_by": ["customer", "salesperson"],
    "order":    "unpaid amount desc"
  }
}'
```

- **Sujet** : il classe les tables (consigne « tables » de l'embedder). Les 6 premières sont les candidates (`STRUCTURED_CANDIDATE_TABLES`).
- **Forme courte** : chaque morceau nomme l'attribut seul, en 1 à 3 mots, sans répéter le sujet ni citer la valeur (« status », pas « confirmed sales orders only »). Les valeurs (Morocco, posted…) et la période (this year…) restent dans la question. Une mesure de comptage laisse `measures` vide.
- **Mesures, filtres, regroupements, période** : chacun est cherché parmi les colonnes des candidates (consigne « columns »). On garde la meilleure colonne de chaque morceau, plus une seconde si elle est presque aussi bonne (`STRUCTURED_MATCHES_PER_PIECE`). Trois règles limitent le bruit :
  - **type** : une mesure prend un nombre ou un montant, une période une date, un regroupement tout sauf un nombre (`STRUCTURED_CHECK_TYPES`, activé par défaut) ;
  - **sujet d'abord** : si la table du sujet a une colonne presque aussi bonne que la meilleure (écart ≤ `STRUCTURED_SUBJECT_MARGIN`, 0,06), les autres tables sont écartées pour ce morceau ;
  - **valeurs citées** : une valeur n'ajoute sa table que si celle-ci est reliée par une clé étrangère à une table déjà retenue ; sinon elle est écartée et signalée dans le diagnostic.
  - **non trouvé** : un morceau dont la meilleure colonne est sous `STRUCTURED_MIN_SIMILARITY` (0,6) ne garde rien et est signalé ; `create_date` est autorisée pour une période (« new customers this month ») ;
  - **colonnes toujours envoyées** : une colonne marquée `always_include` par l'enrichissement (statut, type de document, actif…) part avec sa table, même si aucun morceau ne la demande ; l'étape SQL voit ainsi `sale_order.state` ou `account_move.move_type` et leurs notes ;
  - **nom toujours atteint** : une table ajoutée par une jointure, un regroupement ou une valeur, sans colonne de nom, suit sa clé vers une table qui en a (`res_users → res_partner`) ;
  - **regroupement = table** : un regroupement qui ressemble à une fiche de table (`STRUCTURED_TABLE_PIECE_MIN`, 0,6) l'ajoute avec le chemin depuis le sujet, jusqu'à 4 liens (« product category » → `product_category`).
- **Découpage par PREP** : prompt dans [`prompts/prep_structure.md`](prompts/prep_structure.md).
- **Tables gardées** : la première du sujet, celles qui portent une colonne retenue, et les tables visées par les clés étrangères retenues pour obtenir les noms. Une table sans colonne de nom suit sa clé obligatoire : `res_users → partner_id → res_partner`.
- **Schéma minimal** : clé primaire, colonnes retenues, clés de jointure et colonne de nom, avec les notes de l'enrichissement.
- **Réponse** : le schéma, plus le diagnostic (classement du sujet et colonnes trouvées pour chaque morceau).
- **Les questions découpées à la main** : 12 dans [`benchmark/structured_questions.yaml`](benchmark/structured_questions.yaml) et 10 jamais réglées dans [`benchmark/unseen_questions.yaml`](benchmark/unseen_questions.yaml), à lancer avec `python benchmark/schema_search_eval.py --file <fichier> --verbose`.

Les fiches ont été réécrites dans la même forme. Une table enrichie est décrite par son libellé, sa description et ses synonymes, sans liste de colonnes. Une colonne est décrite par son sens d'abord (libellé, synonymes, notes, aide, valeurs), et son nom technique en dernier. **Il faut réindexer une fois** après la mise à jour.

## Enrichissement par module (export / import)

Le texte d'une table, encodé en vecteur, ne contient au départ que son nom, son libellé Odoo et quelques colonnes. L'enrichissement y ajoute une description et du vocabulaire métier, à la main ou plus tard par le rôle PREP, dans le même format.

```bash
curl "http://localhost:5000/api/v1/schema/modules?account_id=1"                       # modules Odoo indexés
curl -o enrichment/account.json "http://localhost:5000/api/v1/schema/export?account_id=1&module=account"
# remplir les blocs « enrich » (tables : label, description, synonyms, domain ; colonnes : label, synonyms, notes)
curl -X POST "http://localhost:5000/api/v1/schema/import?account_id=1&dry_run=true" \
  -H "Content-Type: application/json" --data-binary @enrichment/account.json         # simulation
curl -X POST "http://localhost:5000/api/v1/schema/import?account_id=1" \
  -H "Content-Type: application/json" --data-binary @enrichment/account.json         # import + réencodage
```

- Seuls les blocs `enrich` sont appliqués : `null` = inchangé, `""` = effacé. Une table ou une colonne inconnue est signalée, jamais créée.
- Seules les tables modifiées sont réencodées, en quelques secondes. Une réindexation n'écrase jamais ces champs.
- Le module Odoo de chaque table est lu dans `ir_model_data` à l'indexation. Il faut **relancer `/index` une fois** après la mise à jour pour le renseigner.
- Mesurer l'effet : `schema_search_eval.py` affiche le **rang des tables attendues par vecteur seul** (question brute contre fiches de tables, sans mots ni règles). Des questions nouvelles sur un module se mettent dans un fichier à part (`--file benchmark/account_questions.yaml`).

Détails et règles : [`enrichment/README.md`](enrichment/README.md).

## Modèles LLM par rôle

Deux rôles, chacun avec un profil principal et un profil de secours, choisis dans `.env` :

| Rôle | Sert à | Variables |
|---|---|---|
| `prep` | Découper la question (`prompts/prep_structure.md`) | `PREP_PROVIDER`, `PREP_FALLBACK`, `PREP_REASONING` |
| `sql` | Écrire et corriger le SQL | `SQL_PROVIDER`, `SQL_FALLBACK`, `SQL_REASONING` |

- **Profils** (URL, modèle, paramètre de raisonnement, prix) : [`config/llm_providers.yaml`](config/llm_providers.yaml), versionné et **sans aucune clé**. Profils fournis : `meta_spark` (`muse-spark-1.3`, API Meta), `baseten_spark_open` (`meta-models/muse-spark-open-1026`), `local`.
- **Clés** : uniquement dans `.env` (`META_API_KEY`, `BASETEN_API_KEY`) ; jamais dans Git, les journaux ou les réponses.
- **Secours** : erreur réseau, 429 ou 5xx → un nouvel essai, puis le profil de secours ; clé absente ou refusée → secours directement.
- **Télémétrie** de chaque appel : tokens (entrée, cache, sortie, raisonnement), temps, coût calculé depuis le profil.

```bash
curl http://localhost:5000/api/v1/llm                                   # profils et présence des clés, sans appel
curl -X POST http://localhost:5000/api/v1/llm/test -H "Content-Type: application/json" \
  -d '{"role": "prep", "prompt": "Reply with the single word OK."}'     # appel réel
```

## Découpage de la question (rôle PREP)

`POST /api/v1/structure` découpe une question avec le prompt [`prompts/prep_structure.md`](prompts/prep_structure.md) (le bloc `text` est lu tel quel). Le modèle ne voit que la question, jamais le schéma.

```bash
curl -X POST http://localhost:5000/api/v1/structure -H "Content-Type: application/json" \
  -d '{"question": "Top 5 customers by revenue this year", "account_id": 1}'
```

- `status` : `ok`, `write_request`, `out_of_scope`, `ambiguous` (avec `reason`), ou `degraded` si PREP ne répond pas : la question entière sert alors de sujet.
- Avec `account_id`, la recherche structurée est lancée sur le découpage et le schéma est renvoyé (`schema`).
- Télémétrie de l'appel dans `llm` (modèle, tokens, coût) ; temps par étape dans `timings_ms`.

Mesure sur un fichier de questions, puis recherche sur le découpage obtenu :
```bash
python benchmark/prep_eval.py --file benchmark/unseen_questions.yaml
python benchmark/schema_search_eval.py --file benchmark/out/prep_unseen_questions.yaml --verbose
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

Toute branche part de `devlop` et toute PR cible `devlop` (modèle de PR fourni). La CI vérifie le lint (`ruff check .`), les tests (`pytest`, avec PostgreSQL + pgvector) et la validité des fichiers compose.

Tests en local : `pip install -r requirements-dev.txt` puis `pytest -q` (les tests d'intégration nécessitent `TEST_PG_DSN` vers un PostgreSQL avec pgvector).
