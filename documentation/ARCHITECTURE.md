# AskYourData — Architecture v3 (Meta API)

> **Statut :** décisions prises le 27 oct 2026. Ce document décrit l'architecture cible ; aucun code n'est encore modifié.
> Il remplace les sections « Architecture » de `documentation/PROJECT_BRIEF.md` et du `README.md`, qui seront alignées dans une PR séparée.
> Conventions : `[À VÉRIFIER]` = hypothèse à confirmer ; `[ESTIMATION]` = ordre de grandeur à mesurer.

---

## 1. Décisions

| # | Décision | Conséquence |
|---|---|---|
| D1 | **Plus de service de traduction.** Les questions sont posées en anglais. | Suppression de `rewrite_service` (Qwen2.5-3B). |
| D2 | **Aucun LLM en local.** Tous les LLM passent par des API. | Suppression de `model_service` (SQLCoder-7B-2), du `Dockerfile` GPU et du dossier `models/` (LLM). Plus besoin de GPU pour lancer le projet. |
| D3 | **On garde Odoo, PostgreSQL Odoo et PostgreSQL + pgvector.** | Trois conteneurs de données inchangés. |
| D4 | **Deux modèles Meta, chacun à sa place.** Muse Glimmer 30B (Fireworks) pour comprendre et décrire ; Muse Spark (Meta Model API) pour écrire le SQL. | Glimmer, plus petit et moins cher, prépare le travail ; Spark ne reçoit qu'un contexte réduit et précis. |
| D5 | **Tout ce qui peut être fait en code est fait en code**, avant tout appel d'API. | Cache, filtrage, recherche vectorielle, jointures, validation et formatage restent locaux. |
| D6 | **Fournisseurs interchangeables.** Un LLM se choisit par configuration (URL, modèle, clé), sans changer le code. | Retour possible vers un serveur local ou un autre hébergeur. |
| D7 | **Embedder local conservé** (CPU, petit modèle). | Recherche de schéma gratuite et rapide, sans dépendance réseau. |

---

## 2. Vue d'ensemble

### 2.1 Conteneurs (5)

```
┌──────────────┐    ┌──────────────────┐
│ odoo :8069   │───►│ postgres_odoo    │◄──────────────────────────┐
│ (odoo:18.0)  │    │ :5432 (pg16)     │  exécution SQL read-only  │
└──────────────┘    └──────────────────┘                           │
                                                                   │
┌────────────────────────────── api_service :5000 ─────────────────┴──┐
│ Orchestrateur (Flask + Gunicorn, CPU)                               │
│  • client LLM générique ─────► Fireworks  : Muse Glimmer 30B        │
│                         └────► Meta API   : Muse Spark              │
│  • validation SQL, exécution, correction, formatage, télémétrie     │
└───────────────┬─────────────────────────────────────────────────────┘
                │ HTTP
┌───────────────▼──────────────┐    ┌─────────────────────────────────┐
│ schema_service :8002 (CPU)   │───►│ postgres_nl2sql :5433           │
│ embedder local + indexation  │    │ pgvector/pg16 : métadonnées,    │
│ + enrichissement (Glimmer)   │    │ descriptions, embeddings        │
└──────────────────────────────┘    └─────────────────────────────────┘
```

| Conteneur | Rôle | Image |
|---|---|---|
| `odoo` | ERP source (démo) | `odoo:18.0` |
| `postgres_odoo` | Base Odoo | `postgres:16-alpine` |
| `postgres_nl2sql` | Métadonnées + vecteurs | `pgvector/pgvector:pg16` |
| `schema_service` | Indexation, enrichissement, recherche de schéma | `Dockerfile.cpu` |
| `api_service` | Orchestration du pipeline | `Dockerfile.cpu` |

`model_service` n'est pas remplacé par un autre conteneur : l'appel aux API se fait par un module du `api_service`. Un service qui ne ferait que relayer un appel HTTP ajouterait un saut réseau sans rien apporter.

`schema_service` reste séparé pour charger l'embedder **une seule fois** (et non dans chacun des workers Gunicorn).

### 2.2 Rôle de chaque composant

| Étape | Qui | Quand | Coût |
|---|---|---|---|
| Filtrage des tables, libellés Odoo, comptage des lignes | Code | Indexation | 0 |
| Description, synonymes, domaine, colonnes clés de chaque table | **Muse Glimmer** | Indexation (une fois) | faible |
| Embeddings des tables et colonnes | Embedder local | Indexation | 0 |
| Structuration de la question | **Muse Glimmer** | Chaque question | faible |
| Recherche des tables et colonnes | pgvector + embedder local | Chaque question | 0 |
| Chemins de jointure, schéma compact | Code | Chaque question | 0 |
| Génération et correction du SQL | **Muse Spark** | Chaque question | principal poste |
| Validation, exécution, formatage, résumé | Code | Chaque question | 0 |

**Principe de sûreté :** aucun modèle ne voit de données réelles (lignes de résultat). Glimmer voit la structure des tables et la question ; Spark voit la question et un schéma réduit. Aucun chiffre de la réponse n'est écrit par un modèle.

---

## 3. Pipeline d'indexation (une fois, puis incrémental)

Déclenché par `POST /api/v1/index {account_id}`. Ne s'exécute jamais pendant une question.

### 3.1 Lecture de la structure (code)
- Tables, colonnes, types, clés primaires et étrangères depuis le catalogue PostgreSQL de la base Odoo.
- Corriger au passage le bug actuel : une réindexation ne doit **jamais** effacer `description` (aujourd'hui `DO UPDATE SET description = EXCLUDED.description` avec `NULL`).

### 3.2 Filtrage des tables (code)
Objectif : n'indexer que les tables qui portent des données métier.

1. **Wizards : par les métadonnées Odoo.** Exclure les modèles transitoires (`ir_model.transient = TRUE`) `[À VÉRIFIER sur Odoo 18]`. Cela couvre tous les wizards, même ceux dont le nom ne contient pas « wizard ».
2. **Liste de motifs**, dans un fichier de configuration (modifiable sans toucher au code) :

| Catégorie | Exemples | Motif |
|---|---|---|
| Vues QWeb, menus, actions, technique | `ir_ui_view`, `ir_ui_menu`, `ir_act_window`, `ir_attachment`, `ir_config_parameter` | `ir_*` |
| Menus et pages du site web | `website_menu`, `website_page` | `website_*` (si installé) |
| Wizards non détectés | `*_wizard` | `*_wizard`, `*_wizard_*` |
| Messagerie, notifications | `mail_*`, `bus_*`, `sms_*`, `snailmail_*` | préfixes |
| Interface web, imports | `web_*`, `base_import_*` | préfixes |
| Services techniques | `iap_*`, `auth_*`, `digest_*`, `onboarding_*`, `spreadsheet_*` | préfixes |
| Tables de liaison many2many | `…_rel` | suffixe `_rel` |

3. **Liste blanche prioritaire** : `res_partner`, `res_company`, `res_currency`, `res_country`… ne sont jamais exclues (on n'exclut pas `res_*` en bloc).
4. **Tables vides** de la base de démo : indexées mais non prioritaires pour l'enrichissement.
5. **Journal** : nombre de tables retenues / exclues, avec la raison de chaque exclusion, pour relecture.

Résultat attendu : de plusieurs centaines de tables à environ 150–250 `[ESTIMATION, à mesurer]`.

> Ne pas confondre avec la **liste de tables interdites** (sécurité, § 5.5) : `res_users` n'est pas du bruit mais reste interdite dans le SQL ; `ir_ui_menu` est du bruit mais n'est pas dangereuse.

### 3.3 Libellés Odoo (code, gratuit)
Pour chaque table retenue, récupérer ce qu'Odoo sait déjà `[À VÉRIFIER : noms exacts et stockage JSON traduisible en v18]` :

| Information | Source | Exemple (`account_move`) |
|---|---|---|
| Modèle Odoo | `ir_model.model` | `account.move` |
| Libellé de la table | `ir_model.name` | « Journal Entry » |
| Libellé des colonnes | `ir_model_fields.field_description` | `amount_residual` → « Amount Due » |
| Aide d'un champ | `ir_model_fields.help` | si présent |
| Valeurs des champs à choix | `ir_model_fields_selection` | `move_type` : out_invoice, out_refund, in_invoice… |
| Relations | clés étrangères | `partner_id` → `res_partner` |
| Volume | nombre de lignes | ~N |

### 3.4 Enrichissement par Muse Glimmer (une fois)
**Un appel par table**, avec une **fiche de structure** : la table, ses colonnes, leurs libellés Odoo, les valeurs des champs à choix et le voisinage (tables pointées et tables qui pointent vers elle, avec leurs libellés). **Aucune donnée réelle n'est envoyée.**

On n'envoie pas tout le schéma en un seul appel : trop volumineux pour la fenêtre de contexte, moins précis, et un échec ferait tout recommencer.

Exemple de fiche :
```
Table: sale_order_line   (Odoo model: sale.order.line — "Sales Order Line")
Rows: ~N
Columns:
  order_id        integer  FK → sale_order ("Sales Order")    "Order Reference"
  product_id      integer  FK → product_product ("Product")   "Product"
  product_uom_qty numeric                                     "Quantity"
  price_subtotal  numeric                                     "Subtotal"
  state           varchar  values: draft, sent, sale, cancel  "Order Status"
Referenced by: stock_move.sale_line_id, …
```

Sortie attendue (JSON strict) :
```json
{
  "label": "Journal entries, invoices and credit notes",
  "description": "Accounting documents: customer invoices, vendor bills, credit notes and miscellaneous entries; the kind is given by move_type. Stores amounts, due amount and payment status.",
  "synonyms": ["invoice", "bill", "credit note", "refund", "receivable"],
  "domain": "accounting",
  "key_columns": {
    "move_type": "document kind",
    "amount_residual": "amount still due",
    "payment_state": "paid / partially paid / not paid"
  }
}
```

- `domain` dans une liste fermée : `sales`, `accounting`, `inventory`, `purchase`, `hr`, `contacts`, `project`, `other`.
- Seules les **colonnes clés** sont décrites par Glimmer ; les autres gardent le libellé Odoo.
- **Contrôles en code** : JSON valide, domaine connu, chaque colonne de `key_columns` existe dans la table (sinon rejetée). Échec → 1 nouvel essai → table marquée « non enrichie » (elle reste indexée avec les libellés Odoo).
- Les règles d'usage éventuellement proposées par Glimmer (ex. « filtrer `state='posted'` ») ne sont **pas** injectées dans le prompt de Spark sans relecture humaine.

**Performance** `[ESTIMATION]` : quelques secondes par appel ; avec ~10 appels en parallèle, quelques minutes pour toutes les tables retenues. Limite réelle : débit du serverless Fireworks `[À VÉRIFIER]` → parallélisme réglable, nouvel essai automatique sur erreur 429. Traitement par priorité : modules de la démo (ventes, facturation, stock, contacts) d'abord. Le système fonctionne déjà avec les seuls libellés Odoo pendant l'enrichissement.

**Coût** `[ESTIMATION]` : ~200 tables × ~2 000 tokens ≈ 0,4 M tokens en entrée, soit moins de 1 $.

### 3.5 Stockage et embeddings
- Colonnes existantes de `nl2sql_tables` et `nl2sql_columns` : `label`, `description`, `synonyms`, `domain`.
- Ajouts au schéma : `enriched BOOLEAN`, `enriched_by` (modèle et version), `enriched_at`, et une indication de source pour que **les annotations manuelles (`/annotate`) restent prioritaires** et ne soient jamais écrasées.
- Embedding d'une table = nom + libellé + description + synonymes + domaine + libellés des colonnes clés.
- **Embeddings de colonnes stockés** à l'indexation (aujourd'hui recalculés à chaque question).
- Réindexation incrémentale : seules les tables nouvelles ou modifiées, ou `force=true`.

### 3.6 Export versionné de l'enrichissement
Les descriptions validées sont exportées dans un fichier JSON versionné dans le repo (métadonnées de la base de démo uniquement, ni données ni secrets). Une nouvelle installation (celle du jury) le charge au démarrage : **zéro appel, zéro attente, zéro coût**.

### 3.7 Relecture humaine
Échantillon prioritaire : les ~30 tables utilisées par le benchmark et la démo. Corrections via `/annotate`, qui devient prioritaire.

---

## 4. Embedder local

| Option | Éditeur | Taille | Dimensions | Remarque |
|---|---|---|---|---|
| `all-MiniLM-L6-v2` | Microsoft / sentence-transformers | ~22 M | 384 | Actuel, très rapide |
| `facebook/contriever-msmarco` | **Meta** (FAIR) | ~110 M | 768 | Chaîne IA 100 % Meta ; mean pooling à ajouter ; vecteurs plus grands, réindexation nécessaire |
| MetaEncoder | Meta | 29,8 B | — | Trop gros pour la machine cible |

- L'embedder est **configurable** (`EMBEDDING_MODEL`, `EMBEDDING_DIM`).
- Choix final par le benchmark : rappel des tables (les bonnes tables sont-elles dans la présélection ?) et latence.
- Image `Dockerfile.cpu` : installer la roue **CPU** de PyTorch (dépendance de sentence-transformers) pour éviter une image lourde.

---

## 5. Pipeline d'une question

```
question (EN)
  │
  ├─ 0. Cache (question normalisée)                    → réponse sans appel
  │
  ├─ 1. Muse Glimmer : structuration (1 appel court)    → JSON
  │        status ≠ ok (écriture, hors sujet, ambiguë) → réponse sans Spark
  ├─ 1b. Cache (demande structurée)                    → réponse sans Spark
  │
  ├─ 2. pgvector : 1 recherche par terme de recherche  → fusion + seuil de similarité
  │        aucune table au-dessus du seuil             → « je ne sais pas »
  │
  ├─ 3. Code : chemins de jointure (FK), PK/FK, élagage des colonnes,
  │            règles métier Odoo, DDL compact, budget de tokens
  │
  ├─ 4. Muse Spark : question d'origine + demande structurée + schéma réduit → SQL
  │
  ├─ 5. Code : validation (AST, schéma, tables interdites, LIMIT, EXPLAIN)
  ├─ 6. Exécution read-only sur postgres_odoo
  ├─ 7. Si erreur : Muse Spark avec l'erreur exacte (max 2 fois)
  ├─ 8. Formatage et résumé par gabarit (code)
  └─ 9. Télémétrie (latence par étape, tokens, coût, modèles)
```

### 5.1 Étape 1 — Structuration par Muse Glimmer
Glimmer reçoit **uniquement la question** (pas de schéma). Il ne peut donc pas inventer de table : il produit des termes métier, et ce sont pgvector et le code qui choisissent les vraies tables.

```json
{
  "request": "List customers with the highest unpaid invoice amount, grouped by salesperson.",
  "intent": "ranking",
  "metrics": ["unpaid invoice amount"],
  "entities": ["customer", "customer invoice", "salesperson"],
  "dimensions": ["salesperson"],
  "filters": ["invoice not fully paid"],
  "period": null,
  "sort_limit": "top 10 by unpaid amount desc",
  "search_queries": ["customers partners", "customer invoices payment status residual amount", "salesperson user"],
  "status": "ok"
}
```

- `status` : `ok` | `write_request` | `out_of_scope` | `ambiguous`.
- Raisonnement au niveau bas, sortie plafonnée (~300 tokens), température minimale.
- Avantages : une recherche par sujet (meilleur rappel sur les questions multi-tables), demande explicite pour Spark, cache plus efficace (formulations différentes → même structure), refus précoce.
- **Mode dégradé** : si Fireworks ne répond pas, pgvector est interrogé avec la question brute et le pipeline continue.
- Désactivable (`QUESTION_STRUCTURING=off`) pour la mesure comparative.

### 5.2 Étape 2 — Recherche de schéma
- Une recherche pgvector par entrée de `search_queries` ; fusion des résultats (meilleur score par table) ; présélection de ~10 à 20 tables `[à calibrer]`.
- Seuil minimal de similarité : en dessous, refus explicite sans appeler Spark.
- Colonnes : recherche sur les embeddings stockés ; PK et FK toujours conservées.

### 5.3 Étape 3 — Réduction du schéma (code)
- **Expansion par clés étrangères** : ajout des tables nécessaires pour relier les tables retenues (chemin le plus court, profondeur 1–2).
- **Élagage des colonnes** : PK, FK utiles, colonnes pertinentes, colonnes métier essentielles (`state`, dates, montants, `company_id`).
- **Format compact** : DDL minimal + description courte de chaque table en commentaire.
- **Règles métier Odoo vérifiées à la main** (ex. factures validées = `account_move.state = 'posted'`, champs traduisibles en JSON `name->>'en_US'`) `[À VÉRIFIER sur la base de démo]`.
- **Budget de tokens** configurable ; taille réelle journalisée.

### 5.4 Étape 4 — Génération par Muse Spark
- Message système fixe (réutilisable par le cache de prompt de l'API si disponible `[À VÉRIFIER]`) : rôle, règles de sortie, règles Odoo, contraintes (lecture seule, une seule instruction).
- Message utilisateur : question d'origine (au cas où Glimmer aurait perdu un détail) + demande structurée + schéma réduit.
- Sortie : `<sql>…</sql>` ou `<cannot_answer>raison</cannot_answer>`.
- Peu d'exemples, plafond de tokens en sortie, température minimale.

### 5.5 Étapes 5 à 7 — Validation, exécution, correction
- **Validation** : sqlglot (dialecte postgres), une seule instruction `SELECT`/`WITH`, existence des tables et colonnes, **liste de tables interdites** (`res_users`, `ir_config_parameter`, tables d'authentification), `LIMIT` ajouté si absent, `EXPLAIN` avec seuil de coût.
- **Exécution** avec un **rôle PostgreSQL dédié en lecture seule** (`default_transaction_read_only = on`, `statement_timeout`), et non l'utilisateur `odoo`.
- **Correction** : la question, le schéma, le SQL fautif et l'erreur exacte (tronquée, sans lignes de résultat) sont renvoyés à Spark. 2 tentatives maximum, puis échec explicite. Une erreur « table/colonne inexistante » peut élargir le schéma.

### 5.6 Étape 8 — Formatage
100 % code : JSON, tableau, CSV/XLSX, graphique (si le temps le permet). Résumé en une phrase par **gabarit**, sans appel de modèle. Toujours affichés : le SQL exécuté et les tables utilisées.

---

## 6. Optimisations locales avant tout appel d'API

| Levier | Effet |
|---|---|
| Cache (question brute puis demande structurée) | Zéro appel pour les questions déjà posées |
| Rapports prédéfinis paramétrés (CA par mois, top clients…) | Zéro appel pour les demandes fréquentes |
| Refus précoce (Glimmer `status`, seuil pgvector) | Pas d'appel à Spark pour les questions sans réponse |
| Filtrage des tables + schéma réduit + budget | Moins de tokens envoyés à Spark |
| Règles Odoo dans le prompt | SQL juste plus souvent du premier coup → moins de corrections |
| Validation locale avant correction | Spark n'est rappelé qu'avec une erreur précise |
| Formatage et résumé en code | Zéro token, zéro chiffre inventé |
| Enrichissement une seule fois + export versionné | Coût d'indexation payé une seule fois |

---

## 7. Fournisseurs interchangeables

### 7.1 Client LLM unique
Une interface, deux usages, configurés indépendamment :

| Usage | Variables | Défaut |
|---|---|---|
| Structuration + enrichissement | `GLIMMER_BASE_URL`, `GLIMMER_MODEL`, `GLIMMER_API_KEY` | Fireworks, `accounts/fireworks/models/muse-glimmer-30b` |
| Génération SQL | `SPARK_BASE_URL`, `SPARK_MODEL`, `SPARK_API_KEY` | Meta Model API, Muse Spark `[nom exact À VÉRIFIER]` |

Si les API sont compatibles avec le format OpenAI `[À VÉRIFIER pour l'API Meta]`, un seul client suffit ; basculer revient à changer ces variables :
- Muse Spark via l'API Meta (défaut) ;
- Muse Glimmer sur Fireworks (défaut pour Glimmer ; plan B pour la génération SQL) ;
- Muse Glimmer sur Baseten (secours ; déploiement GPU dédié facturé au temps `[À VÉRIFIER]`, arrêt automatique obligatoire) ;
- serveur local (vLLM, llama.cpp, Ollama), lancé hors du compose par défaut.

Si l'API Meta a un format propre, un adaptateur dédié implémente la même interface.

Chaque réponse du client renvoie : texte, tokens (entrée/sortie), modèle, latence.

### 7.2 Embedder
Même principe : interface `embed(text)`, modèle choisi par variable d'environnement (§ 4).

### 7.3 Limite du mode local
Muse Glimmer quantifié en 4 bits (~20 Go) vise des GPU de 24 à 32 Go : il ne tient pas sur la machine de développement actuelle (11,6 Go). Un mode local passerait par un modèle plus petit ou une autre machine.

---

## 8. Contrats d'API

- `POST /api/v1/ask` (nouveau) :
  ```json
  { "question": "...", "account_id": 1, "output": "json|table|csv|xlsx", "debug": false }
  ```
  ```json
  {
    "status": "ok|error|clarification_needed|cannot_answer|refused",
    "structured_request": { },
    "answer": { "columns": [], "rows": [], "summary": "..." },
    "sql": "...",
    "tables_used": [],
    "attempts": 1,
    "models": { "structuring": "...", "generation": "..." },
    "timings_ms": { "cache": 0, "structuring": 0, "schema": 0, "generation": 0, "validation": 0, "execution": 0, "formatting": 0, "total": 0 },
    "tokens": { "structuring": { "prompt": 0, "completion": 0 }, "generation": { "prompt": 0, "completion": 0 } },
    "error": null
  }
  ```
- `POST /api/v1/translate` : conservé pour compatibilité (renvoie le SQL sans l'exécuter) ; le champ `question_rewritten` devient la `request` produite par Glimmer.
- `POST /api/v1/index` : ajoute les options `enrich` (bool) et `force` (bool).
- `/api/v1/health` : vérifie `schema_service`, les deux bases et la joignabilité des deux API (sans consommer de tokens si possible).
- `/api/v1/annotate/*` : inchangés.

---

## 9. Configuration (`.env.example`, à compléter)

```
# Bases
NL2SQL_DB_PASSWORD=
ODOO_DB_PASSWORD=
EXEC_DB_USER=askyourdata          # rôle read-only
EXEC_DB_PASSWORD=

# Muse Spark (génération SQL)
SPARK_BASE_URL=
SPARK_MODEL=
SPARK_API_KEY=

# Muse Glimmer (structuration + enrichissement)
GLIMMER_BASE_URL=
GLIMMER_MODEL=accounts/fireworks/models/muse-glimmer-30b
GLIMMER_API_KEY=

# Pipeline
QUESTION_STRUCTURING=on
EMBEDDING_MODEL=all-MiniLM-L6-v2
EMBEDDING_DIM=384
SCHEMA_TOKEN_BUDGET=
MAX_CORRECTIONS=2
ENRICH_CONCURRENCY=10
```

Clés uniquement dans `.env` (déjà ignoré par Git), jamais dans le code ni dans les logs.

---

## 10. Nettoyage du repo

| Élément | Action |
|---|---|
| `rewrite_service/` | Supprimer |
| `model_service/` | Supprimer (le client LLM rejoint `api_service`) |
| `Dockerfile` (GPU, CUDA, PyTorch nightly) | Supprimer |
| Services `model_service`, `rewrite_service` dans `docker-compose.yml` | Supprimer ; retirer les `depends_on` correspondants de `api_service` |
| Dossier `models/` (LLM) | Ne garder que l'embedder, ou le télécharger au build ; ajouter `models/`, `*.gguf`, `*.safetensors`, `*.bin`, `logs/` au `.gitignore` |
| `odoo.conf` à la racine | Supprimer (doublon non monté) |
| `01_init.sql` à la racine | Déplacer vers `postgres/init/` : `docker-compose.yml` monte `./postgres/init`, le script n'est donc pas exécuté aujourd'hui |
| `postgres_odoo` | Ajouter `POSTGRES_USER` / `POSTGRES_PASSWORD` (sans mot de passe, l'image refuse de démarrer) |
| Réseau `nl2sql_net` | Le déclarer `external: true` dans l'un des deux fichiers compose |
| Timeout Gunicorn | L'aligner sur la somme des délais des appels internes |

---

## 11. Benchmark

Mesures à produire sur le jeu de questions (`benchmark/questions.yaml`, 20+ questions en anglais, SQL de référence exécutées sur la base de démo) :

| Comparaison | But |
|---|---|
| Question brute vs demande structurée par Glimmer (pgvector) | Prouver l'intérêt de l'étape Glimmer (rappel des tables, latence) |
| MiniLM vs Contriever | Choisir l'embedder |
| Avec vs sans enrichissement | Mesurer le gain des descriptions |
| Schéma complet vs schéma réduit (Spark) | Tokens et exactitude |
| Muse Spark vs Muse Glimmer pour la génération SQL | Exactitude, latence, coût ; valider le plan B |

Métriques : exactitude par comparaison des résultats, rappel du schéma, latence par étape (médiane, p95), tokens et coût par question, taux de « je ne sais pas » vs taux de mauvaise réponse. Chaque passage coûte des crédits : estimer le coût d'un passage dès le premier, et limiter le nombre de passages complets.

---

## 12. Risques

| Risque | Impact | Parade |
|---|---|---|
| API Meta inaccessible depuis le Maroc, quota ou format inattendu | Bloquant pour la génération | Test d'accès immédiat ; plan B : Glimmer sur Fireworks pour la génération SQL (même interface) |
| Panne ou lenteur d'une API pendant la démo | Démo impossible | Cache des questions de démo, vidéo enregistrée tôt, mode dégradé sans Glimmer |
| Deux fournisseurs = deux points de panne | Disponibilité | Structuration désactivable ; bascule de fournisseur par configuration |
| Budget (150 $ Meta, 25 $ Fireworks, 100 $ Baseten) | Arrêt du service | Suivi des tokens et du coût par requête ; réserve de 20 % ; prix réels mesurés au premier appel `[À VÉRIFIER : crédits Fireworks/Baseten bien attribués]` |
| Version de modèle modifiée côté fournisseur | Benchmark non reproductible | Figer la version si possible ; la noter dans chaque rapport |
| Glimmer perd un détail en reformulant | Mauvais SQL | La question d'origine est toujours transmise à Spark |
| Table utile non retenue | Jointure impossible | Expansion FK ; élargissement du schéma sur erreur « inexistant » |
| Latence de deux appels LLM à la suite | Temps de réponse | Glimmer en raisonnement bas et sortie courte ; mesure par étape |
| Confidentialité | Schéma et question envoyés à des tiers | Base de démo uniquement ; aucune ligne de résultat envoyée ; vérifier les conditions d'utilisation des API |
| Contriever plus lent que MiniLM | Latence de recherche | Choix par le benchmark |

---

## 13. Points ouverts

- API Meta (Muse Spark) : accès depuis le Maroc, nom exact du modèle, format (compatible OpenAI ?), sortie structurée, cache de prompt, limites de débit, prix.
- Fireworks : limites du serverless pour Muse Glimmer, prix réel, crédits attribués.
- Odoo 18 : champ `transient` et tables `ir_model*` à confirmer ; modules installés dans la base de démo.
- Nombre réel de tables après filtrage.
- Règles officielles du hackathon (soumission, modèles autorisés).

---

## 14. Suites

Une fois ce document validé :
1. Aligner `documentation/PROJECT_BRIEF.md` (sections architecture, benchmark, planning) et le `README.md`.
2. Découper le travail en PR à partir de `devlop`, dans l'ordre : nettoyage du repo → client LLM (Spark + Glimmer) → filtrage et indexation → enrichissement → structuration de la question → validation et exécution → réduction du schéma → formatage → benchmark.
