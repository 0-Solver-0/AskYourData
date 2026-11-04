# AskYourData

Parler avec ses données : une question en anglais sur Odoo 18, une réponse tirée directement de la base, sans écrire de SQL.

Architecture complète et décisions : [`documentation/ARCHITECTURE.md`](ARCHITECTURE.md).

> **État : en construction.** La chaîne complète fonctionne : découpage (PREP), recherche de schéma, génération SQL, vérification, exécution en lecture seule et réponse (`/api/v1/ask`). `/api/v1/translate` renvoie le SQL sans l'exécuter.

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
├── models/                  embedder local (non versionné)
│   └── contriever-msmarco/  modèle ouvert Meta AI
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

# 2. Embedder local, modèle ouvert de Meta (une fois, ~440 Mo) — voir « Modèles utilisés »
pip install -U "huggingface_hub[cli]"
huggingface-cli download facebook/contriever-msmarco --local-dir models/contriever-msmarco

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
1. note chaque table : similarité des vecteurs (table et meilleure colonne), correspondance de mots avec les libellés Odoo et le vocabulaire métier de [`config/search_synonyms.yaml`](../config/search_synonyms.yaml), petit bonus pour les tables remplies et malus pour les tables vides ;
2. reconnaît les **valeurs citées** dans la question (noms de pays, d'étapes, de clients… indexés à partir de la colonne `name` des petites tables : « Morocco » → `res_country`) ;
3. garde les meilleures tables, ajoute les **tables de jointure** (plus court chemin dans le graphe des clés étrangères) et les **tables de référence** citées par la question (« customer » → `sale_order.partner_id` → `res_partner`) ;
4. ne garde que les colonnes utiles (clés, colonnes citées, nom, état, dates, montants, plus proches de la question) ;
5. produit un schéma compact commenté (libellés Odoo, valeurs possibles des champs à choix, champs JSON traduisibles), sous un budget de tokens (`SCHEMA_TOKEN_BUDGET`).

Diagnostic : `POST /api/v1/schema/debug {"question": "...", "account_id": 1}` montre pour chaque table la raison du choix (`search`, `join:…`, `lookup:…`), le détail des scores, les colonnes gardées et le classement.

Contrôle sur la vraie base (sans coût) : `python benchmark/schema_search_eval.py` vérifie les tables attendues pour les questions de [`benchmark/schema_questions.yaml`](../benchmark/schema_questions.yaml). Il affiche le rappel, la précision (tables utiles / tables envoyées), les tables par question, les tokens, le temps et le rang des tables attendues par vecteur seul. Historique des mesures : [`benchmark/HISTORY.md`](../benchmark/HISTORY.md).

Une collection Postman est fournie : [`tools/postman/AskYourData.postman_collection.json`](../tools/postman/AskYourData.postman_collection.json).

## Modèles utilisés

Règle du hackathon : **tout modèle appelé à l'exécution est un modèle Meta**.

| Rôle | Modèle | Éditeur | Où il tourne |
|---|---|---|---|
| Découpage de la question (PREP) | `meta-models/muse-spark-open-1026` (Muse Spark Open 30B) | Meta | API Baseten (`config/llm_providers.yaml`, profil `baseten_spark_open`) |
| Choix de la table du sujet quand la recherche hésite (juge) | `meta-models/muse-spark-open-1026` | Meta | API Baseten (même profil que PREP) ; seulement si l'écart entre les 2 premières tables est sous `STRUCTURED_JUDGE_GAP` (0,05), réponse limitée aux 8 candidates |
| Génération et correction du SQL | `muse-spark-1.3` (Muse Spark 1.3) | Meta | API Meta (`meta_spark`) |
| Question dictée → texte | `muse-voice-transcribe-1.0` (Muse Voice Transcribe) | Meta | API Meta (`meta_voice`) |
| Recherche des tables et colonnes (embeddings) | `facebook/contriever-msmarco` (Contriever) | **Meta AI, modèle ouvert** | **en local**, sur CPU, dans le conteneur `schema_service` (`./models/contriever-msmarco`) |
| ou, en option : recherche des tables et colonnes | `facebook/drama-base` (DRAMA, dérivé de Llama 3.2, CC-BY-NC 4.0) | **Meta AI, modèle ouvert** | **en local**, sur CPU, dans `schema_service` (`./models/drama-base`, `EMBEDDER_BACKEND=drama`) |

Sans modèle d'IA : la vérification du SQL (sqlglot), l'exécution (PostgreSQL en lecture seule), le résumé de la réponse (gabarit dans le code). La lecture à voix haute éventuelle utilise la synthèse vocale du navigateur (Web Speech API), pas un modèle.

## Embedder

Le modèle qui transforme tables, colonnes et questions en vecteurs se choisit dans le `.env`, sans toucher au code (`schema_service/embedder.py`). Par défaut : **Contriever** (`facebook/contriever-msmarco`, Meta AI, 768 dimensions, regroupement par moyenne).

| Variable | Rôle |
|---|---|
| `EMBEDDER_MODEL` | dossier du modèle dans `./models/` (défaut `contriever-msmarco`) |
| `EMBEDDER_DEVICE` | `cpu`, `cuda` ou `auto` |
| `EMBEDDER_DIM` | dimension réduite pour les modèles qui l'acceptent ; vide = dimension native |
| `EMBEDDER_PROMPT_TABLES` / `EMBEDDER_PROMPT_COLUMNS` | consignes ajoutées aux questions (aucune par défaut) |
| `STRUCTURED_MIN_SIMILARITY`, `STRUCTURED_TABLE_PIECE_MIN`, `STRUCTURED_SUBJECT_MARGIN` | seuils de la recherche, **propres à l'échelle de l'embedder** |

**Changer de modèle** : au démarrage, `schema_service` compare le modèle et la dimension avec ceux de l'index. S'ils diffèrent, il adapte la base et vide les vecteurs, puis `/health` indique `"reindex_required": true`. Il suffit alors de relancer `POST /api/v1/index`. Les annotations, descriptions et valeurs sont conservées. Ensuite, **recalibrer les seuils** : `tools/calibrate_search.py` pose les questions de benchmark, montre les scores des bonnes et des mauvaises correspondances et propose des valeurs.

```bash
docker compose up -d --build schema_service
curl -X POST http://localhost:5000/api/v1/index -H "Content-Type: application/json" -d '{"account_id": 1}'
python tools/calibrate_search.py --url http://localhost:5000      # puis seuils dans .env
docker compose up -d schema_service
```

**DRAMA (option)** : `facebook/drama-base` (Meta AI, 2025, 0,1B paramètres hors vocabulaire, construit à partir de Llama 3.2) classe mieux les correspondances courtes que Contriever (ex. « lead source » → `crm_lead.source_id` et non `lead_mining_request_id`). Il s'utilise par son API native (`encode_queries` / `encode_documents`, préfixe « Query: » ajouté par le modèle) et exécute le code Python livré avec le modèle, d'où l'option explicite :

```bash
huggingface-cli download facebook/drama-base --local-dir models/drama-base
# .env : EMBEDDER_MODEL=drama-base  EMBEDDER_BACKEND=drama  EMBEDDER_TRUST_REMOTE_CODE=true
docker compose up -d --build schema_service    # puis POST /api/v1/index et tools/calibrate_search.py
```
Ses scores sont plus bas (≈ 0,5 pour une bonne correspondance) : recalibrer les seuils `STRUCTURED_*`.

**GPU NVIDIA (option)** : inutile pour Contriever (110M paramètres), qui tourne bien sur CPU.

## Indexation : ce qui est retenu

- Les filtres sont dans [`config/index_filters.yaml`](../config/index_filters.yaml) (monté dans le conteneur : modifier puis relancer l'indexation, sans reconstruire l'image).
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
- **Découpage par PREP** : prompt dans [`prompts/prep_structure.md`](../prompts/prep_structure.md).
- **Tables gardées** : la première du sujet, celles qui portent une colonne retenue, et les tables visées par les clés étrangères retenues pour obtenir les noms. Une table sans colonne de nom suit sa clé obligatoire : `res_users → partner_id → res_partner`.
- **Schéma minimal** : clé primaire, colonnes retenues, clés de jointure et colonne de nom, avec les notes de l'enrichissement.
- **Réponse** : le schéma, plus le diagnostic (classement du sujet et colonnes trouvées pour chaque morceau).
- **Les questions découpées à la main** : 12 dans [`benchmark/structured_questions.yaml`](../benchmark/structured_questions.yaml) et 10 jamais réglées dans [`benchmark/unseen_questions.yaml`](../benchmark/unseen_questions.yaml), à lancer avec `python benchmark/schema_search_eval.py --file <fichier> --verbose`.

- **Entités** : PREP indique à quoi appartient un morceau quand ce n'est pas le sujet (`"entities": {"vendor": "vendor", "city": "customer"}`). L'entité est cherchée parmi les fiches de tables (seuil `STRUCTURED_TABLE_PIECE_MIN`) : parmi les 3 meilleures, celle que le sujet référence par une clé étrangère, sinon la première (« vendor » → `res_partner` plutôt que `product_supplierinfo`). Un morceau qui nomme l'entité (« vendor ») prend la **clé étrangère du sujet vers sa table** (`account_move.partner_id → res_partner`), même si une colonne d'une autre table ressemble davantage au mot ; sans clé directe, la table de l'entité est ajoutée avec son chemin. Un attribut (« city » du client) est cherché **d'abord dans la table de l'entité**, puis partout en secours.

**Plusieurs vecteurs par table et par colonne.** Une fiche qui mêle plusieurs sens (« Customer or vendor », sept types de documents…) donne un vecteur moyen, battu par une carte plus étroite d'une autre table. Une table ou une colonne enrichie est donc encodée en plusieurs textes courts : **son libellé, puis un texte par synonyme** (table `nl2sql_vectors`). Son score est le meilleur d'entre eux, et le diagnostic indique le texte gagnant (`via`). La description, les notes, l'aide Odoo et les valeurs ne sont **pas** encodées : elles vont au modèle SQL, dans le DDL. Sans libellé ni synonymes, la fiche complète d'avant sert de secours. Conséquences : chaque synonyme doit être précis et propre à sa table ; les scores montent (texte court contre requête courte), donc **recalibrer les seuils** ; **réindexer une fois** après la mise à jour (`/health` indique `reindex_required` tant que les vecteurs multiples manquent ; d'ici là, la recherche utilise l'ancien vecteur unique).

Tester un mot à la main, sans lancer toute la chaîne :
```bash
python tools/probe_search.py "vendor" "payment journal" --limit 5   # tables et colonnes, score et texte gagnant
```

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

Détails et règles : [`enrichment/README.md`](../enrichment/README.md).

**Enrichissement par Muse Spark Open 30B** : [`tools/enrich_with_spark.py`](../tools/enrich_with_spark.py) envoie chaque table d'un module au rôle PREP (30B) avec les règles de [`prompts/enrich_schema.md`](../prompts/enrich_schema.md) : synonymes au singulier, propres à la table, sans mots de colonnes ; dates prévue / réelle ; notes pour les valeurs vides et les pièges. Il écrit un fichier par module dans `enrichment/spark/` (ignoré par Git), **à relire avant import**, puis liste les synonymes partagés entre tables. Rien n'est écrit dans la base. Coût estimé : environ 2 $ pour les 118 tables métier.
```bash
docker run --rm --network nl2sql_net --env-file .env -v ${PWD}:/w -w /w askyourdata-api_service \
  python tools/enrich_with_spark.py --module sale --module purchase --url http://askyourdata_api:5000
python tools/enrich_with_spark.py --check enrichment/spark/*.json      # doublons seulement
```

## Modèles LLM par rôle

Deux rôles, chacun avec un profil principal et un profil de secours, choisis dans `.env` :

| Rôle | Sert à | Variables |
|---|---|---|
| `prep` | Découper la question (`prompts/prep_structure.md`) | `PREP_PROVIDER`, `PREP_FALLBACK`, `PREP_REASONING` |
| `sql` | Écrire et corriger le SQL | `SQL_PROVIDER`, `SQL_FALLBACK`, `SQL_REASONING` |

- **Profils** (URL, modèle, paramètre de raisonnement, prix) : [`config/llm_providers.yaml`](../config/llm_providers.yaml), versionné et **sans aucune clé**. Profils fournis : `meta_spark` (`muse-spark-1.3`, API Meta), `baseten_spark_open` (`meta-models/muse-spark-open-1026`), `local`.
- **Clés** : uniquement dans `.env` (`META_API_KEY`, `BASETEN_API_KEY`) ; jamais dans Git, les journaux ou les réponses.
- **Secours** : erreur réseau, 429 ou 5xx → un nouvel essai, puis le profil de secours ; clé absente ou refusée → secours directement.
- **Télémétrie** de chaque appel : tokens (entrée, cache, sortie, raisonnement), temps, coût calculé depuis le profil.

```bash
curl http://localhost:5000/api/v1/llm                                   # profils et présence des clés, sans appel
curl -X POST http://localhost:5000/api/v1/llm/test -H "Content-Type: application/json" \
  -d '{"role": "prep", "prompt": "Reply with the single word OK."}'     # appel réel
```

## Découpage de la question (rôle PREP)

`POST /api/v1/structure` découpe une question avec le prompt [`prompts/prep_structure.md`](../prompts/prep_structure.md) (le bloc `text` est lu tel quel). Le modèle ne voit que la question, jamais le schéma.

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

## Génération SQL (rôle SQL, sans exécution)

`POST /api/v1/translate` enchaîne le découpage (PREP), la recherche structurée et la génération avec le prompt [`prompts/sql_generate.md`](../prompts/sql_generate.md). Le modèle répond par :
- `<sql>` : la requête (statut `sql`) ;
- `<missing>` : ce qui manque au schéma, **en mots métier** ; le code lance alors une recherche complémentaire (`POST /select/extend` : colonnes des tables retenues et de leurs voisines, puis tables reliées), complète le schéma sans rien retirer et redemande **une seule fois** le SQL. Rien de trouvé, ou encore un manque : `cannot_answer` (« Data not available: … ») ;
- `<cannot_answer>` : la donnée n'existe pas (statut `cannot_answer`).

Les refus de PREP (`write_request`, `out_of_scope`, `ambiguous`) arrêtent la chaîne avant la recherche. La réponse donne le découpage, les tables, le schéma envoyé, les modèles utilisés, les tokens, le coût et le temps par étape. `translate` n'exécute pas le SQL : voir `ask` ci-dessous.

```bash
curl -X POST http://localhost:5000/api/v1/translate -H "Content-Type: application/json" \
  -d '{"question": "Top 5 customers by revenue this year", "account_id": 1}'
python benchmark/sql_eval.py --file benchmark/unseen_questions.yaml      # SQL à relire : benchmark/out/sql_*.md
python tools/try_question.py "Top 5 customers by revenue this year"      # une question, étape par étape
```

## Question → réponse (vérification et exécution)

`POST /api/v1/ask` fait tout `translate`, puis :
1. **Vérification** par le code ([`api_service/validate.py`](../api_service/validate.py), sqlglot) : une seule instruction `SELECT`/`WITH`, aucune écriture (CTE compris), pas de `SELECT INTO`, `FOR UPDATE`, `SELECT *` ni de fonction système (`pg_*`, `query_to_xml`…) ; **uniquement les tables et colonnes du schéma envoyé** ; aucune table ou colonne sensible ([`api_service/sensitive.py`](../api_service/sensitive.py)) ; `LIMIT 1000` ajouté s'il manque.
2. **Exécution** ([`api_service/executor.py`](../api_service/executor.py)) avec un rôle PostgreSQL dédié en lecture seule, transaction en lecture seule, `statement_timeout` (`EXEC_TIMEOUT_MS`), `EXPLAIN` d'abord (coût maximal `EXEC_MAX_COST`).
3. **Correction** : requête refusée ou erreur de la base → le modèle SQL reçoit sa requête et l'erreur exacte, et corrige (**2 fois au plus**), puis échec explicite.
4. **Réponse** construite par le code à partir des lignes ([`api_service/answer.py`](../api_service/answer.py)) : colonnes, lignes, résumé par gabarit. **Aucun chiffre écrit par un modèle.**

Rôle en lecture seule (une fois, puis après l'installation d'un module Odoo) : choisir `EXEC_DB_PASSWORD` dans `.env`, puis

```powershell
docker run --rm --network nl2sql_net -v ${PWD}:/w -w /w python:3.11-slim sh -c "pip install -q psycopg2-binary && python tools/create_readonly_role.py"
```

Le script crée le rôle `EXEC_DB_USER`, masque les tables et colonnes sensibles (`res_users.password`, `ir_config_parameter`…) et vérifie : lecture permise, mot de passe refusé, écriture refusée. `/api/v1/health` indique `execution.status = ok`.

```bash
curl -X POST http://localhost:5000/api/v1/ask -H "Content-Type: application/json" \
  -d '{"question": "Top 5 customers by revenue this year", "account_id": 1}'
python benchmark/sql_eval.py --file benchmark/unseen_questions.yaml --execute   # résultats : benchmark/out/ask_*.md
python tools/try_question.py "Top 5 customers by revenue this year" --execute
```

## Interface web (question écrite ou dictée)

Ouvrir **http://localhost:5000/** : zone de question, exemples, réponse en tableau (export CSV), résumé, et pour chaque réponse la **trace** (temps par étape, tokens, coût, modèles) et le détail (découpage, SQL exécuté, tables, données demandées par le modèle). Les refus (« donnée absente », « lecture seule ») sont affichés tels quels, sans chiffre.

**Voix** : bouton micro (cliquer pour parler, recliquer pour envoyer). Le navigateur convertit l'enregistrement en WAV mono 16 kHz, `POST /api/v1/transcribe` le transmet à **Muse Voice Transcribe** (`muse-voice-transcribe-1.0`, API Meta, même clé `META_API_KEY`), le texte entendu s'affiche puis part automatiquement dans la même chaîne qu'une question écrite. Réglages dans `.env` (obligatoires, sans valeur cachée dans le code) : `VOICE_PROVIDER=meta_voice` (profil de `config/llm_providers.yaml` : modèle, adresse, prix) et `VOICE_LANGUAGES=English,French`. Le micro demande une page ouverte en `localhost` ou en https.

**Arrêt automatique du micro** (les deux interfaces, sans modèle : volume mesuré par le navigateur) : 2 s de silence après la parole arrêtent et envoient la question ; au bout de 20 s l'enregistrement s'arrête et part ; sans aucun son pendant 5 s, il s'arrête **sans envoi** (rien n'est transcrit ni facturé). Réglages dans `.env` : `VOICE_MAX_SECONDS`, `VOICE_SILENCE_MS`, `VOICE_NO_SPEECH_MS` ; lus par l'interface via `/health` (`voice.recording`), qui signale une valeur absente ou invalide (`defaults`).

**Question reformulée** : dans le même appel, PREP renvoie la question corrigée et clarifiée (erreurs de transcription, mots en double, mot vague → terme métier habituel ; rien d'ajouté). Elle est affichée « Understood as: … », relue à voix haute après une question dictée, et sert de question principale à la recherche et au modèle SQL ; la question telle que posée lui est aussi transmise.

**Version conversation (proposition) : http://localhost:5000/v2**. Même API, autre présentation : une page d'accueil centrée (salutation, saisie en pilule, exemples), puis un fil de questions et réponses avec la saisie fixée en bas. Elle s'adapte au téléphone. La version classique reste sur `/`.
- **Réponse lue à voix haute** par la **synthèse vocale du navigateur** (`speechSynthesis`, voix du système, aucun modèle tiers). Le texte lu est le résumé de la base, précédé de l'hypothèse retenue s'il y en a une ; sans réponse, la page lit ce qui manque (« This data is not in the database. Data not available: … »). Aucun chiffre n'est écrit par un modèle.
- Lecture **automatique** pour une question dictée, ou pour toutes si le bouton haut-parleur de la saisie est activé (choix gardé dans le navigateur). Bouton **Listen** sous chaque réponse.

**Test public (production)** : chaque testeur reçoit une clé personnelle (20 questions, voix et texte ; 50 pour le jury), créée par `tools/keys.py` ; rien ne se fait sans clé valide. Un plafond global en dollars (`ACCESS_GLOBAL_BUDGET_USD`) bloque tout s'il est atteint. Les questions sont journalisées (base NL2SQL, sans audio ni adresse IP) et visibles dans un tableau de bord `/admin`, accessible seulement par tunnel SSH. `docker-compose.prod.yml` ajoute Caddy (HTTPS automatique, nécessaire au micro) et ne laisse passer que l'interface et les routes de question : index, import, export et annotations répondent 404 depuis Internet (`PUBLIC_MODE`). Guide pas à pas : [`documentation/DEPLOY.md`](DEPLOY.md).

**Taux de bonnes réponses** : [`benchmark/unseen_reference.yaml`](../benchmark/unseen_reference.yaml) donne, pour chaque question, un SQL de référence écrit à la main et le choix métier retenu. [`benchmark/answer_eval.py`](../benchmark/answer_eval.py) exécute la référence (même rôle en lecture seule), pose la question à `/api/v1/ask` et compare les valeurs : mêmes lignes, colonnes en plus permises, nombres arrondis à 2 décimales, ordre compté pour les classements.

```powershell
docker run --rm --network nl2sql_net -v ${PWD}:/w -w /w python:3.11-slim sh -c "pip install -q requests pyyaml psycopg2-binary sqlglot==26.17.1 && python benchmark/answer_eval.py --url http://askyourdata_api:5000 --references-only"
docker run --rm --network nl2sql_net -v ${PWD}:/w -w /w python:3.11-slim sh -c "pip install -q requests pyyaml psycopg2-binary sqlglot==26.17.1 && python benchmark/answer_eval.py --url http://askyourdata_api:5000 --runs 3"
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
