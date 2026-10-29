# AskYourData — Architecture v3 (Meta API)

> **Statut :** décisions prises le 27 oct 2026, complétées le 29 oct (D8 à D13). Indexation, recherche de schéma, enrichissement et embedder sont codés ; la structuration de la question, la génération SQL et l'exécution restent à faire.
> Le brief d'origine est archivé dans `documentation/archive/PROJECT_BRIEF.md`.
> Conventions : `[À VÉRIFIER]` = hypothèse à confirmer ; `[ESTIMATION]` = ordre de grandeur à mesurer.

---

## 1. Décisions

| # | Décision | Conséquence |
|---|---|---|
| D1 | **Plus de service de traduction.** Les questions sont posées en anglais. | Suppression de `rewrite_service` (Qwen2.5-3B). |
| D2 | **Aucun LLM en local.** Tous les LLM passent par des API. | Suppression de `model_service` (SQLCoder-7B-2), du `Dockerfile` GPU et du dossier `models/` (LLM). Plus besoin de GPU pour lancer le projet. |
| D3 | **On garde Odoo, PostgreSQL Odoo et PostgreSQL + pgvector.** | Trois conteneurs de données inchangés. |
| D4 | **Deux rôles LLM, chacun à sa place.** Rôle `PREP` (préparation) : enrichissement des tables et structuration des questions, par défaut **Muse Spark sur l'API Meta** (secours : Muse Spark Open sur Baseten). Rôle `SQL` : génération et correction du SQL, par défaut **Muse Spark sur l'API Meta**. | Le SQL ne reçoit qu'un contexte réduit et précis préparé en amont. Les deux rôles restent séparés pour pouvoir changer de modèle indépendamment. |
| D5 | **Tout ce qui peut être fait en code est fait en code**, avant tout appel d'API. | Cache, filtrage, recherche vectorielle, jointures, validation et formatage restent locaux. |
| D6 | **Fournisseurs interchangeables par rôle.** Chaque rôle choisit un profil de fournisseur par configuration, avec un profil de secours (§ 7). | Bascule vers un autre hébergeur ou un serveur local en changeant une ligne de configuration, sans toucher au code. |
| D7 | **Embedder local conservé** (CPU, petit modèle). | Recherche de schéma gratuite et rapide, sans dépendance réseau. |
| D8 | **Embedder paramétrable** (`EMBEDDER_MODEL`, `EMBEDDER_DEVICE`, `EMBEDDER_DIM`) : MiniLM par défaut, Qwen3-Embedding-0.6B en option, GPU NVIDIA facultatif (`docker-compose.gpu.yml`). | Changer de modèle vide les vecteurs et demande une réindexation ; le reste de l'index est conservé. |
| D9 | **Enrichissement par module Odoo**, en export / import JSON (`/api/v1/schema/export`, `/import`), rempli à la main, par Muse Code ou par `PREP`. | Testé sur `account` : la table des factures passe de la 13e à la 2e place en recherche par vecteur seul. C'est l'enrichissement des fiches, pas le choix de l'embedder, qui fait progresser la recherche. |
| D10 | **Abandon de la sélection par couverture mot par mot** (« mode coverage », PR #10–#11, retiré le 29/10). | Un mot isolé ne veut rien dire pour un embedder. Mesures archivées dans `benchmark/HISTORY.md`. |
| D11 | **Nouvelle forme de recherche** : la question est découpée en morceaux de sens (sujet, mesure, filtres, regroupements, période), chacun cherché au bon endroit (tables ou colonnes), contre des fiches rédigées dans la même forme. Validée d'abord **à la main**, puis produite par `PREP` (§ 5.1). | La recherche actuelle (« legacy », règles et synonymes) reste en place comme référence jusqu'à ce que la nouvelle forme fasse mieux. |
| D12 | **Mesures mises en pause** pendant la mise au point de la nouvelle forme. | Historique : `benchmark/HISTORY.md`. |
| D13 | **Forme courte et règles de la recherche structurée** : morceaux réduits à l'attribut (sans sujet ni valeur), colonne compatible avec le rôle, table du sujet d'abord, valeur citée seulement si sa table est reliée, morceau faible signalé « non trouvé », regroupement qui désigne une table. Découpage par PREP : `prompts/prep_structure.md`. | Le bruit restant venait de colonnes prises hors du sujet ; l'enrichissement seul ne le corrigeait plus. Réglages : `STRUCTURED_CHECK_TYPES`, `STRUCTURED_SUBJECT_MARGIN`, `STRUCTURED_MIN_SIMILARITY`, `STRUCTURED_TABLE_PIECE_MIN`. |

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
│  • client LLM par rôle ─ PREP ─► Meta API : Muse Spark (déf.)       │
│                        └ SQL ──► Meta API : Muse Spark              │
│  • validation SQL, exécution, correction, formatage, télémétrie     │
└───────────────┬─────────────────────────────────────────────────────┘
                │ HTTP
┌───────────────▼──────────────┐    ┌─────────────────────────────────┐
│ schema_service :8002 (CPU)   │───►│ postgres_nl2sql :5433           │
│ embedder local + indexation  │    │ pgvector/pg16 : métadonnées,    │
│ + enrichissement (PREP)      │    │ descriptions, embeddings        │
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
| Description, synonymes, domaine, colonnes clés de chaque table | **`PREP`** (Muse Spark Open) | Indexation (une fois) | faible |
| Embeddings des tables et colonnes | Embedder local | Indexation | 0 |
| Structuration de la question | **`PREP`** (Muse Spark Open) | Chaque question | faible |
| Recherche des tables et colonnes | pgvector + embedder local | Chaque question | 0 |
| Chemins de jointure, schéma compact | Code | Chaque question | 0 |
| Génération et correction du SQL | **`SQL`** (Muse Spark) | Chaque question | principal poste |
| Validation, exécution, formatage, résumé | Code | Chaque question | 0 |

**Principe de sûreté :** aucun modèle ne voit de données réelles (lignes de résultat). Le modèle `PREP` voit la structure des tables et la question ; le modèle `SQL` voit la question et un schéma réduit. Aucun chiffre de la réponse n'est écrit par un modèle.

**Pourquoi deux rôles séparés :** Muse Spark Open est une version distincte de Muse Spark (même base que Spark 1.3, post-entraînement de sécurité supplémentaire, probablement servie en 4 bits sur Baseten `[À VÉRIFIER]`), un peu en retrait sur les évaluations publiques. Cet écart ne pèse pas sur la préparation (tâches simples et cadrées) mais compte pour le SQL : le modèle le plus fort est donc réservé au rôle `SQL`.

**Prix identiques :** Muse Spark 1.3 sur l'API Meta et Muse Spark Open sur Baseten coûtent tous deux 1,25 $ (entrée), 0,15 $ (entrée en cache) et 4,25 $ (sortie) par million de tokens. Utiliser Baseten pour `PREP` ne ferait donc rien économiser. **Choix retenu (27 oct) : `PREP` utilise aussi Muse Spark sur l'API Meta** (modèle plus fort, coût de l'enrichissement estimé à 1–1,50 $ pour ~200 tables). Muse Spark Open sur Baseten reste le secours de chaque rôle et pourra reprendre `PREP` pour préserver les crédits Meta si les crédits Baseten sont attribués.

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

### 3.4 Enrichissement par le modèle `PREP` (une fois)
Modèle par défaut : Muse Spark 1.3 sur l'API Meta (secours : Muse Spark Open sur Baseten), **raisonnement réglé sur « low »** (le défaut de Muse Spark Open est « xhigh », coûteux en tokens et en temps).

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
- Seules les **colonnes clés** sont décrites par le modèle ; les autres gardent le libellé Odoo.
- **Contrôles en code** : JSON valide, domaine connu, chaque colonne de `key_columns` existe dans la table (sinon rejetée). Échec → 1 nouvel essai → table marquée « non enrichie » (elle reste indexée avec les libellés Odoo).
- Les règles d'usage éventuellement proposées par le modèle (ex. « filtrer `state='posted'` ») ne sont **pas** injectées dans le prompt `SQL` sans relecture humaine.

**Performance** `[ESTIMATION]` : quelques secondes par appel ; avec ~10 appels en parallèle, quelques minutes pour toutes les tables retenues. Limite réelle : débit du fournisseur (Baseten annonce 2 M tokens/min pour les crédits du hackathon) → parallélisme réglable, nouvel essai automatique sur erreur 429. Traitement par priorité : modules de la démo (ventes, facturation, stock, contacts) d'abord. Le système fonctionne déjà avec les seuls libellés Odoo pendant l'enrichissement.

**Coût** `[ESTIMATION]` : ~200 tables × ~2 000 tokens ≈ 0,4 M tokens en entrée et ~0,06 M en sortie. Au tarif Baseten de Muse Spark Open (1,25 $ / 4,25 $ par million de tokens en entrée / sortie), de l'ordre de 1 $ hors tokens de raisonnement.

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
  ├─ 1. PREP (Muse Spark Open) : structuration        → JSON
  │        status ≠ ok (écriture, hors sujet, ambiguë) → réponse sans appel SQL
  ├─ 1b. Cache (demande structurée)                    → réponse sans appel SQL
  │
  ├─ 2. pgvector : 1 recherche par terme de recherche  → fusion + seuil de similarité
  │        aucune table au-dessus du seuil             → « je ne sais pas »
  │
  ├─ 3. Code : chemins de jointure (FK), PK/FK, élagage des colonnes,
  │            règles métier Odoo, DDL compact, budget de tokens
  │
  ├─ 4. SQL (Muse Spark) : question d'origine + demande structurée + schéma réduit → SQL
  │
  ├─ 5. Code : validation (AST, schéma, tables interdites, LIMIT, EXPLAIN)
  ├─ 6. Exécution read-only sur postgres_odoo
  ├─ 7. Si erreur : SQL (Muse Spark) avec l'erreur exacte (max 2 fois)
  ├─ 8. Formatage et résumé par gabarit (code)
  └─ 9. Télémétrie (latence par étape, tokens, coût, modèles)
```

### 5.1 Étape 1 — Structuration par le modèle `PREP`
Le modèle `PREP` (Muse Spark par défaut) reçoit **uniquement la question** (pas de schéma). Il ne peut donc pas inventer de table : il produit des termes métier, et ce sont pgvector et le code qui choisissent les vraies tables.

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
- Raisonnement réglé sur « low », sortie plafonnée (~300 tokens). Les tokens de raisonnement éventuellement renvoyés sont ignorés pour l'extraction mais comptés dans la télémétrie.
- Avantages : une recherche par sujet (meilleur rappel sur les questions multi-tables), demande explicite pour le modèle `SQL`, cache plus efficace (formulations différentes → même structure), refus précoce.
- **Mode dégradé** : si le fournisseur `PREP` (et son secours) ne répond pas, pgvector est interrogé avec la question brute et le pipeline continue.
- Désactivable (`QUESTION_STRUCTURING=off`) pour la mesure comparative.

### 5.2 Étape 2 — Recherche de schéma
- Une recherche pgvector par entrée de `search_queries` ; fusion des résultats (meilleur score par table) ; présélection de ~10 à 20 tables `[à calibrer]`.
- Seuil minimal de similarité : en dessous, refus explicite sans appeler le modèle `SQL`.
- Colonnes : recherche sur les embeddings stockés ; PK et FK toujours conservées.

### 5.3 Étape 3 — Réduction du schéma (code)
- **Expansion par clés étrangères** : ajout des tables nécessaires pour relier les tables retenues (chemin le plus court, profondeur 1–2).
- **Élagage des colonnes** : PK, FK utiles, colonnes pertinentes, colonnes métier essentielles (`state`, dates, montants, `company_id`).
- **Format compact** : DDL minimal + description courte de chaque table en commentaire.
- **Règles métier Odoo vérifiées à la main** (ex. factures validées = `account_move.state = 'posted'`, champs traduisibles en JSON `name->>'en_US'`) `[À VÉRIFIER sur la base de démo]`.
- **Budget de tokens** configurable ; taille réelle journalisée.

### 5.4 Étape 4 — Génération par le modèle `SQL` (Muse Spark)
- Message système fixe (réutilisable par le cache de prompt de l'API si disponible `[À VÉRIFIER]`) : rôle, règles de sortie, règles Odoo, contraintes (lecture seule, une seule instruction).
- Message utilisateur : question d'origine (au cas où la structuration aurait perdu un détail) + demande structurée + schéma réduit.
- Sortie : `<sql>…</sql>` ou `<cannot_answer>raison</cannot_answer>`.
- Peu d'exemples, plafond de tokens en sortie, température minimale.

### 5.5 Étapes 5 à 7 — Validation, exécution, correction
- **Validation** : sqlglot (dialecte postgres), une seule instruction `SELECT`/`WITH`, existence des tables et colonnes, **liste de tables interdites** (`res_users`, `ir_config_parameter`, tables d'authentification), `LIMIT` ajouté si absent, `EXPLAIN` avec seuil de coût.
- **Exécution** avec un **rôle PostgreSQL dédié en lecture seule** (`default_transaction_read_only = on`, `statement_timeout`), et non l'utilisateur `odoo`.
- **Correction** : la question, le schéma, le SQL fautif et l'erreur exacte (tronquée, sans lignes de résultat) sont renvoyés au modèle `SQL`. 2 tentatives maximum, puis échec explicite. Une erreur « table/colonne inexistante » peut élargir le schéma.

### 5.6 Étape 8 — Formatage
100 % code : JSON, tableau, CSV/XLSX, graphique (si le temps le permet). Résumé en une phrase par **gabarit**, sans appel de modèle. Toujours affichés : le SQL exécuté et les tables utilisées.

---

## 6. Optimisations locales avant tout appel d'API

| Levier | Effet |
|---|---|
| Cache (question brute puis demande structurée) | Zéro appel pour les questions déjà posées |
| Rapports prédéfinis paramétrés (CA par mois, top clients…) | Zéro appel pour les demandes fréquentes |
| Refus précoce (`status` de la structuration, seuil pgvector) | Pas d'appel au modèle `SQL` pour les questions sans réponse |
| Filtrage des tables + schéma réduit + budget | Moins de tokens envoyés au modèle `SQL` |
| Règles Odoo dans le prompt | SQL juste plus souvent du premier coup → moins de corrections |
| Validation locale avant correction | Le modèle `SQL` n'est rappelé qu'avec une erreur précise |
| Formatage et résumé en code | Zéro token, zéro chiffre inventé |
| Message système fixe | Profite du cache d'entrée des fournisseurs (ex. 0,15 $ au lieu de 1,25 $ par million de tokens sur Baseten) |
| Enrichissement une seule fois + export versionné | Coût d'indexation payé une seule fois |

---

## 7. Fournisseurs interchangeables par rôle

### 7.1 Deux rôles, configurés séparément
| Rôle | Utilisé pour | Profil par défaut | Secours |
|---|---|---|---|
| `PREP` | Enrichissement des tables (§ 3.4) + structuration des questions (§ 5.1) | `meta_spark` — Muse Spark sur l'API Meta | `baseten_spark_open` (si crédits Baseten) |
| `SQL` | Génération et correction du SQL (§ 5.4–5.5) | `meta_spark` — Muse Spark sur l'API Meta | `baseten_spark_open` |

Les rôles sont nommés d'après ce qu'ils font, pas d'après un modèle : changer de modèle ne change ni le code ni les noms de variables.

### 7.2 Profils de fournisseurs
Chaque fournisseur est décrit une seule fois dans un fichier versionné, `config/llm_providers.yaml` (sans aucune clé) :

```yaml
meta_spark:
  base_url: <URL de l'API Meta>          # [À VÉRIFIER dans la documentation]
  model: <identifiant API de Muse Spark 1.3>  # nom affiché « Muse Spark 1.3 » ; identifiant exact [À VÉRIFIER]
  api_key_env: META_API_KEY
  context_tokens: 1000000
  reasoning: {param: <à vérifier>, value: low}
  price_per_mtok: {input: 1.25, cached_input: 0.15, output: 4.25}

baseten_spark_open:
  base_url: https://inference.baseten.co/v1
  model: meta-models/muse-spark-open-1026     # identifiant imposé pour le hackathon
  api_key_env: BASETEN_API_KEY
  reasoning: {param: <à vérifier>, value: low} # défaut du modèle : xhigh
  price_per_mtok: {input: 1.25, cached_input: 0.15, output: 4.25}

fireworks_glimmer:
  base_url: <URL Fireworks, compatible OpenAI>
  model: accounts/fireworks/models/muse-glimmer-30b
  api_key_env: FIREWORKS_API_KEY
  price_per_mtok: {input: 0.35, output: 1.50}  # [À VÉRIFIER, prix relevé chez un tiers]

local:
  base_url: http://<hôte>:8000/v1        # vLLM, llama.cpp server, Ollama…
  model: <modèle servi>
  api_key_env: LOCAL_API_KEY
  price_per_mtok: {input: 0, output: 0}
```

Le `.env` ne fait que **choisir** un profil par rôle et fournir les clés :
```
PREP_PROVIDER=meta_spark
PREP_FALLBACK=baseten_spark_open
SQL_PROVIDER=meta_spark
SQL_FALLBACK=baseten_spark_open
```

Basculer vers un autre hébergeur ou vers un serveur local = changer une ligne. Ajouter un fournisseur = ajouter un profil.

### 7.3 Client LLM unique
- **Une seule interface**, par exemple `complete(role, messages) → {text, tokens_in, tokens_out, tokens_reasoning, model, provider, latency_ms, cost_usd}`.
- Implémentation par défaut au **format chat completions compatible OpenAI**, que comprennent Baseten, Fireworks et les serveurs locaux courants. Si l'API Meta a un format propre `[À VÉRIFIER au test d'accès]`, un adaptateur dédié implémente la même interface.
- **Les particularités de chaque fournisseur restent dans le profil** : nom du paramètre de raisonnement, sortie de raisonnement à ignorer, prix. Le code du pipeline ne connaît que les rôles.
- **Extraction tolérante** de la réponse : contenu entre `<sql>…</sql>`, ou premier objet JSON valide, quel que soit le texte autour.
- **Secours** : sur erreur réseau, 429 ou 5xx, 1 nouvel essai sur le profil principal, puis bascule sur le profil de secours du rôle ; le profil effectivement utilisé est renvoyé dans la réponse et la télémétrie.
- **Coût calculé automatiquement** à partir des tokens et du prix du profil.

### 7.4 Embedder
Même principe : interface `embed(texts)`, modèle choisi par `EMBEDDING_MODEL` / `EMBEDDING_DIM` (§ 4). Changer d'embedder impose une réindexation (la taille des vecteurs change) ; changer de LLM n'en impose aucune.

### 7.5 Limites de la bascule
- **Les prompts sont réglés pour un modèle donné.** Après tout changement de profil, relancer le benchmark ; si nécessaire, le profil peut porter une variante de prompt.
- **Mode local** : Muse Spark Open (816 B paramètres, 42 B actifs) et Muse Glimmer quantifié en 4 bits (~20 Go, pour GPU de 24 à 32 Go) ne tiennent pas sur la machine de développement actuelle (11,6 Go). Un profil `local` vise un modèle plus petit ou une autre machine.
- **Licence Muse Spark Open** : mention « Built with Muse Spark Open » à afficher de façon visible (README et interface de démo).

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
    "models": { "prep": "provider/model", "sql": "provider/model" },
    "timings_ms": { "cache": 0, "structuring": 0, "schema": 0, "generation": 0, "validation": 0, "execution": 0, "formatting": 0, "total": 0 },
    "tokens": { "prep": { "prompt": 0, "completion": 0, "reasoning": 0 }, "sql": { "prompt": 0, "completion": 0, "reasoning": 0 } },
    "cost_usd": { "prep": 0, "sql": 0, "total": 0 },
    "error": null
  }
  ```
- `POST /api/v1/translate` : conservé pour compatibilité (renvoie le SQL sans l'exécuter) ; le champ `question_rewritten` devient la `request` produite par le modèle `PREP`.
- `POST /api/v1/index` : ajoute les options `enrich` (bool) et `force` (bool).
- `/api/v1/health` : vérifie `schema_service`, les deux bases et la joignabilité des fournisseurs des rôles `PREP` et `SQL` (sans consommer de tokens si possible) ; indique le profil actif de chaque rôle.
- `/api/v1/annotate/*` : inchangés.

---

## 9. Configuration (`.env.example`, à compléter)

```
# Bases
NL2SQL_DB_PASSWORD=
ODOO_DB_PASSWORD=
EXEC_DB_USER=askyourdata          # rôle read-only
EXEC_DB_PASSWORD=

# Choix des fournisseurs par rôle (profils définis dans config/llm_providers.yaml)
PREP_PROVIDER=meta_spark
PREP_FALLBACK=baseten_spark_open
SQL_PROVIDER=meta_spark
SQL_FALLBACK=baseten_spark_open

# Clés API (une par fournisseur utilisé)
META_API_KEY=
BASETEN_API_KEY=
FIREWORKS_API_KEY=
LOCAL_API_KEY=

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

Jeu de questions : `benchmark/questions.yaml`, 20+ questions en anglais, SQL de référence écrites à la main et exécutées sur la base de démo.

### 11.1 Mesure principale : l'apport du rôle `PREP`
Mêmes questions, même base, mêmes réglages ; **le SQL est toujours écrit par le modèle `SQL` (Muse Spark, API Meta)**. Seule la préparation change :

| Test | Préparation | Génération du SQL |
|---|---|---|
| 1. Référence | Aucune : question brute → pgvector sur noms de tables et libellés Odoo | Muse Spark |
| 2. + Enrichissement | Tables décrites par le modèle `PREP` | Muse Spark |
| 3. Pipeline complet | Enrichissement + structuration de la question par le modèle `PREP` | Muse Spark |

Comparer 1 → 2 → 3 isole ce que rapporte chaque apport. Si la structuration ne gagne rien, elle est désactivée (`QUESTION_STRUCTURING=off`) pour gagner en latence. L'enrichissement est fait une fois avant les tests 2 et 3 ; son coût n'est pas compté par question.

### 11.2 Mesures complémentaires
| Comparaison | But |
|---|---|
| MiniLM vs Contriever | Choisir l'embedder |
| Schéma complet vs schéma réduit | Tokens et exactitude du modèle `SQL` |
| Optionnel : `SQL_PROVIDER=baseten_spark_open` | Valider le secours pour la génération SQL (si les crédits le permettent) |

### 11.3 Métriques
- **Rappel des tables** : les tables de la SQL de référence arrivent-elles jusqu'au modèle `SQL` ?
- **Exactitude** : le SQL généré renvoie les mêmes lignes que la référence (noms de colonnes ignorés, ordre ignoré sauf classement, tolérance sur les décimaux).
- **Latence** par étape (médiane, p95) et totale.
- **Tokens et coût** par rôle et par question : tokens `PREP` ajoutés, tokens `SQL` économisés grâce au schéma réduit.
- **Taux de « je ne sais pas »** vs **taux de mauvaise réponse** (pire cas).

Chaque passage consomme des crédits : mesurer le coût réel du premier passage, puis limiter le nombre de passages complets. Rapport : `benchmark/results/<date>_<profils>.md`, avec les profils et versions de modèles utilisés.

---

## 12. Risques

| Risque | Impact | Parade |
|---|---|---|
| API Meta inaccessible depuis le Maroc, quota ou format inattendu | Bloquant pour la génération | Test d'accès immédiat ; `SQL_FALLBACK=baseten_spark_open` (même interface) |
| Panne ou lenteur d'une API pendant la démo | Démo impossible | Profils de secours par rôle, cache des questions de démo, vidéo enregistrée tôt, mode dégradé sans structuration |
| Deux fournisseurs = deux points de panne | Disponibilité | Secours automatique par rôle ; structuration désactivable |
| Budget (150 $ Meta ; 100 $ Baseten et 25 $ Fireworks demandés, attribution non garantie) | Arrêt du service | Coût calculé par rôle et par requête ; réserve de 20 % ; prix réels mesurés au premier appel |
| Raisonnement « xhigh » par défaut sur Muse Spark Open | Latence et coût multipliés | Raisonnement réglé sur « low » dans le profil ; tokens de raisonnement suivis |
| Version de modèle modifiée côté fournisseur | Benchmark non reproductible | Figer la version si possible ; la noter dans chaque rapport |
| La structuration perd un détail | Mauvais SQL | La question d'origine est toujours transmise au modèle `SQL` |
| Table utile non retenue | Jointure impossible | Expansion FK ; élargissement du schéma sur erreur « inexistant » |
| Latence de deux appels LLM à la suite | Temps de réponse | `PREP` en raisonnement bas et sortie courte ; mesure par étape |
| Confidentialité | Schéma et question envoyés à des tiers | Base de démo uniquement ; aucune ligne de résultat envoyée ; vérifier les conditions d'utilisation des API |
| Contriever plus lent que MiniLM | Latence de recherche | Choix par le benchmark |

---

## 13. Points ouverts

- API Meta (Muse Spark 1.3) : accès depuis le Maroc, URL et identifiant API exact du modèle, format (compatible OpenAI ?), paramètre de raisonnement, sortie structurée, limites de débit. Prix connu : 1,25 $ / 0,15 $ (cache) / 4,25 $ par million de tokens ; contexte 1 M tokens.
- Baseten : nom du paramètre de raisonnement, précision servie (4 bits ?), crédits attribués.
- Fireworks : crédits attribués (secours `PREP`), prix réel de Muse Glimmer.
- Odoo 18 : champ `transient` et tables `ir_model*` à confirmer ; modules installés dans la base de démo.
- Nombre réel de tables après filtrage.
- Règles officielles du hackathon (soumission, modèles autorisés).

---

## 14. Suites

Une fois ce document validé :
1. ~~Aligner `documentation/PROJECT_BRIEF.md`~~ : archivé le 29/10 (`documentation/archive/`).
2. Découper le travail en PR à partir de `devlop`, dans l'ordre : nettoyage du repo → client LLM par rôle et profils de fournisseurs → filtrage et indexation → enrichissement → structuration de la question → validation et exécution → réduction du schéma → formatage → benchmark.
