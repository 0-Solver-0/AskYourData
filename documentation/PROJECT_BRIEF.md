# AskYourData — Brief projet (Meta Global AI Developer Hackathon)

> Ce document sert de contexte permanent pour le développement du projet.
> Il peut être placé à la racine du repo sous le nom `CLAUDE.md`, ou collé comme instructions d'un projet dédié.
> Il distingue toujours **ce qui existe**, **ce qui est à vérifier** et **ce qui est à construire**.

---

## 0. Instructions pour l'assistant qui travaille dans ce repo

1. **Commencer par un inventaire.** Avant toute modification, lire l'arborescence et le code, puis confirmer ou corriger la section 3 « État actuel ». Ce document a été rédigé sans accès au repo : certains détails (noms d'endpoints, ports, structure des dossiers) sont marqués `[À VÉRIFIER]`.
2. **Ne jamais inventer.** Ni fonctionnalité, ni chiffre, ni nom de table Odoo. Si un point ne peut pas être vérifié, le dire.
3. **Une PR = un objectif.** Petites PR, faciles à relire, liées à un élément du planning (section 8).
4. **Ne pas changer un contrat d'API entre services sans le signaler** dans la description de la PR.
5. **Mesurer.** Toute PR qui touche le pipeline (recherche de schéma, prompt, génération, validation) doit relancer le benchmark et reporter l'impact sur l'exactitude, la latence et les tokens.
6. **Sécurité des données.** Uniquement la base Odoo de démonstration. Aucune donnée client, aucun secret, aucun poids de modèle dans Git.

---

## 1. Contexte

- **Événement :** Meta Global AI Developer Hackathon (Meta × Cerebral Valley), en ligne.
- **Track :** Indie developer — jugé sur la créativité, l'ambition technique et le fait que ça fonctionne.
- **Thème :** Build for Your World.
- **Exigence :** construire avec les modèles Meta — série Muse Spark via la Meta Model API, et/ou modèles open-weights de Meta.
- **Crédits disponibles :** 150 $ Meta Model API ; selon sélection, Baseten (100 $), Modal (100 $), Fireworks (25 $) pour les modèles open-weights Meta.
- **Dates clés (heure du Maroc, UTC+0) :**
  - Ateliers et office hours Discord : 27 oct → 4 nov
  - **Soumission : 5 nov à 02h00** (4 nov 21h00 ET). Objectif interne : **soumettre le 4 nov avant 18h00**.
  - Jury : 4 → 8 nov ; résultats : 9 nov.
- **Règles officielles :** publiées au lancement. `[À VÉRIFIER]` format de soumission (vidéo de démo ? repo public ? description ?) et éventuelles restrictions sur l'usage d'autres modèles.

---

## 2. Vision fonctionnelle

### Problème
Dans une entreprise, chaque rapport ou suivi de KPI passe par un développeur ou un data analyst : trouver les données, écrire la requête, mettre en forme. Les utilisateurs métier attendent, et la plupart des questions ne sont jamais posées.

### Solution
**AskYourData — parler avec ses données.** L'utilisateur pose une question en langage naturel et reçoit la réponse directement depuis la base : données brutes ou rapport prêt à l'emploi, sans écrire de SQL.

**Première cible : Odoo ERP (v18 Community, base de démo).** L'approche est conçue pour s'étendre ensuite à d'autres systèmes et bases de données.

### Trois exigences, par ordre de priorité
1. **Exactitude, sans hallucination :** aucune table ou colonne inventée, aucun chiffre écrit par le modèle. Si le système ne sait pas, il le dit.
2. **Temps de réponse.**
3. **Coût en tokens,** maîtrisé par l'architecture (schéma réduit, pas d'appel LLM inutile).

### Utilisateurs cibles
Responsable financier, responsable des ventes, responsable des opérations, dirigeant de PME : connaissent leurs données métier, ne connaissent pas le SQL ni le schéma Odoo.

### Types de questions (par valeur croissante)
| Catégorie | Exemple |
|---|---|
| Lookup simple | « Combien de clients avons-nous au Maroc ? » |
| Agrégation | « Chiffre d'affaires facturé par mois en 2025 » |
| Comparaison de périodes | « Évolution du CA mois par mois, en % » |
| Classement par groupe | « Top 3 produits par catégorie en quantité vendue » |
| Multi-tables | « Clients avec le plus gros montant impayé, par commercial » |

Les comparaisons, classements et requêtes multi-tables sont le cœur de la démo : ce sont les rapports qui demandent aujourd'hui un développeur.

### Sorties
- **Données brutes** (JSON).
- **Tableau** lisible, **export CSV/Excel**.
- **Graphique** simple pour les séries temporelles et classements (si le temps le permet).
- **Résumé en une phrase** pour les résultats à valeur unique — les chiffres viennent du résultat, jamais du modèle.
- **Toujours visible :** la requête SQL exécutée et les tables utilisées (traçabilité).

### Comportements attendus
- Question ambiguë → l'agent demande une précision plutôt que de deviner (stretch goal ; au minimum, documenter l'hypothèse prise dans la réponse).
- Échec après les tentatives de correction → message clair « je n'ai pas pu répondre », avec la raison.
- Langue : anglais en priorité ; français en stretch goal.

---

## 3. État actuel (POC local)

> Source : description fournie par l'auteur. `[À VÉRIFIER]` contre le code avant de commencer.

### model_service — port 8001
- Flask + **llama-cpp-python**, modèle **Qwen3-4B Text2SQL Q8_0 (GGUF)**.
- GPU : RTX 5070 Ti Laptop (Blackwell, **sm_120**). llama-cpp-python compilé depuis les sources (CUDA 13.0, `CMAKE_CUDA_ARCHITECTURES=120`, correctif stub `libcuda.so` au build), dans un Dockerfile multi-stage.
- Endpoint : `POST /generate {question, schema} → {sql, elapsed}`.
- Prompt au format ChatML avec 3 exemples few-shot (CTE, `RANK() OVER`, `LAG()`, `MODE() WITHIN GROUP`).
- Mesuré : **2,77 s – 5,40 s** de génération sur requêtes complexes.

### schema_service — port 8002
- Flask + **pgvector** + **all-MiniLM-L6-v2** (384 dimensions, CPU).
- **426 tables** Odoo v18 indexées après filtrage (wizards, `mail_`, `ir_`, `bus_`, `web_`).
- Embedding d'une table = description + noms de colonnes (champs calculés exclus via `pg_attribute.attgenerated`) + tables cibles des clés étrangères.
- **4 624 colonnes** et **2 252 relations FK** indexées séparément.
- Index **ivfflat** (cosinus). Latence de recherche mesurée : **~7 ms**.
- Descriptions enrichies par LLM protégées par un flag `enriched=TRUE` (non écrasées à la réindexation) ; paramètre `force=True` pour forcer.
- **42 / 426** tables enrichies. Après enrichissement, des tables absentes du top 10 ont atteint des scores de 0,57 à 0,78.
- Endpoints : `[À VÉRIFIER]` noms, paramètres, format de réponse.

### postgres_nl2sql — port 5433
- Image pgvector / PostgreSQL 16, stocke les embeddings.

### api_service
- `[À VÉRIFIER]` existe dans une version antérieure (Flask + Gunicorn, port 5000, endpoint `/api/v1/translate`) qui appelait seulement model_service avec un schéma fourni.

### Base Odoo
- Odoo v18 Community avec données de démo. `[À VÉRIFIER]` hôte, port, nom de la base, modules installés (Ventes, Facturation, Inventaire, RH, Contacts ?).

### Ce qui n'existe pas encore
- Orchestration de bout en bout (api_service → schema_service → model_service).
- Validation et exécution du SQL sur Odoo, retour des résultats.
- Intégration d'un modèle Meta.
- Formatage des sorties.
- Benchmark et mesure de l'exactitude.
- Gestion du français.

---

## 4. Architecture cible

```
            ┌──────────────────────── api_service (orchestrateur) ────────────────────────┐
question ─► │ 1. Normalisation (FR→EN : stretch)                                          │
            │ 2. Recherche de schéma ──────────────► schema_service (pgvector, ~7 ms)     │
            │ 3. Construction du schéma réduit (tables + colonnes + chemins de jointure)  │
            │ 4. Génération SQL ───────────────────► model_service (backends multiples)   │
            │ 5. Validation (AST, schéma, EXPLAIN)                                        │
            │ 6. Exécution read-only ──────────────► PostgreSQL Odoo                      │
            │ 7. Correction (max 2 tentatives, avec l'erreur exacte)                      │
            │ 8. Formatage déterministe + résumé vérifié                                  │
            │ 9. Télémétrie (latence par étape, tokens)                                   │
            └─────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 api_service — orchestrateur
- Endpoint principal proposé : `POST /api/v1/ask`
  ```json
  { "question": "...", "output": "json|table|csv|xlsx|chart", "debug": false }
  ```
- Réponse proposée :
  ```json
  {
    "status": "ok|error|clarification_needed",
    "answer": { "columns": [], "rows": [], "summary": "..." },
    "sql": "...",
    "tables_used": [],
    "attempts": 1,
    "timings_ms": { "schema": 0, "generation": 0, "validation": 0, "execution": 0, "formatting": 0, "total": 0 },
    "tokens": { "prompt": 0, "completion": 0 },
    "error": null
  }
  ```
- Garder l'ancien endpoint s'il existe, pour compatibilité.

### 4.2 Recherche et réduction du schéma
- Top-k tables via schema_service (k à calibrer, point de départ 5 à 10).
- **Expansion par clés étrangères :** ajouter les tables nécessaires pour joindre les tables retenues (chemin le plus court dans le graphe FK, profondeur limitée à 1–2).
- **Élagage des colonnes :** garder clés primaires, clés étrangères utiles, colonnes pertinentes selon la recherche de colonnes, et colonnes métier essentielles (`state`, `date`, montants, `company_id`).
- **Format compact** dans le prompt : DDL minimal + description courte de la table + commentaires sur les colonnes ambiguës.
- **Budget de tokens** configurable pour le schéma ; journaliser la taille réelle envoyée.

### 4.3 Règles métier Odoo (indices injectés dans le prompt)
> `[À VÉRIFIER]` chaque règle sur la base de démo avant de l'utiliser.

- Factures clients : `account_move` avec `move_type IN ('out_invoice','out_refund')` et `state = 'posted'`. Pour un CA net des avoirs, utiliser les montants signés (`amount_untaxed_signed`, `amount_total_signed`).
- Impayés : `payment_state` et `amount_residual` sur `account_move`.
- Commandes confirmées : `sale_order.state = 'sale'` (vérifier les états disponibles en v18).
- Champs traduisibles stockés en JSONB (ex. `product_template.name`) : extraire avec `name->>'en_US'`.
- Multi-société : filtrer ou grouper par `company_id` si plusieurs sociétés.
- Exclure les documents en brouillon et annulés sauf demande explicite.

Ces règles alimenteront à terme une **couche sémantique** (définitions de métriques réutilisables : « chiffre d'affaires », « impayé », « client actif »).

### 4.4 model_service — backends multiples
- Interface inchangée : `POST /generate {question, schema} → {sql, elapsed}` ; ajouter `tokens` dans la réponse.
- Sélection du backend par variable d'environnement, par exemple `MODEL_BACKEND=local_llamacpp|meta_model_api|openweights_hosted`.
  - `local_llamacpp` : existant (Qwen3-4B), puis un modèle open-weights Meta qui tient dans ~11 Go de VRAM → **mode privé** (les données ne quittent pas la machine).
  - `meta_model_api` : **Muse Spark** via la Meta Model API → **backend principal du hackathon**. `[À VÉRIFIER]` format de l'API, nom exact du modèle, accès depuis le Maroc (la preview publique de juillet visait les développeurs américains).
  - `openweights_hosted` : modèle open-weights Meta via Baseten ou Fireworks, pour comparaison dans le benchmark.
- Clés API dans `.env` uniquement, jamais dans le code.
- Le prompt doit demander **uniquement le SQL**, dans des balises faciles à extraire.

### 4.5 Validation avant exécution
1. **Parsing AST** (sqlglot, dialecte postgres) : une seule instruction, `SELECT` ou `WITH … SELECT` uniquement.
2. **Vérification du schéma :** toutes les tables et colonnes existent (catalogue PostgreSQL ou index du schema_service).
3. **Liste de tables interdites :** `res_users`, tables `ir_config_parameter`, tables d'authentification, etc.
4. **Ajout automatique d'un `LIMIT`** si absent (ex. 1 000 lignes).
5. **`EXPLAIN`** : erreurs de type ou de jointure détectées sans exécuter ; rejet si le coût estimé dépasse un seuil.

### 4.6 Exécution
- **Rôle PostgreSQL dédié en lecture seule** :
  ```sql
  CREATE ROLE askyourdata LOGIN PASSWORD '...';
  GRANT CONNECT ON DATABASE <odoo_db> TO askyourdata;
  GRANT USAGE ON SCHEMA public TO askyourdata;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO askyourdata;
  REVOKE SELECT ON res_users, ir_config_parameter FROM askyourdata;
  ALTER ROLE askyourdata SET default_transaction_read_only = on;
  ALTER ROLE askyourdata SET statement_timeout = '15s';
  ```
- Plafond de lignes retournées, sérialisation propre des décimaux et des dates.

### 4.7 Boucle de correction
- En cas d'échec de validation ou d'exécution : renvoyer au modèle la question, le schéma réduit, le SQL fautif et **l'erreur exacte** (ex. colonne inexistante + colonnes disponibles).
- **Maximum 2 tentatives.** Ensuite, réponse d'échec explicite.

### 4.8 Formatage
- 100 % déterministe (code) : tableau, CSV, XLSX (openpyxl), graphique (stretch).
- Résumé en langage naturel uniquement pour un résultat à une valeur ou très court ; **vérification automatique** que chaque nombre du résumé apparaît dans le résultat, sinon on n'affiche pas le résumé.

### 4.9 Télémétrie
- Pour chaque requête : latence par étape, tokens prompt/complétion, backend utilisé, nombre de tentatives, statut.
- Écriture dans un fichier JSONL (ex. `logs/requests.jsonl`) exploité par le benchmark.

### 4.10 Enrichissement du schéma
- Générer les descriptions des **384 tables restantes** avec **Muse Spark** (et non un modèle concurrent), en lot.
- Entrée par table : nom, colonnes, FK, quelques valeurs d'exemple si utile. Sortie : description métier courte (1–3 phrases).
- Stockage avec `enriched=TRUE`, puis réindexation. Relecture manuelle d'un échantillon.

### 4.11 Stretch goals
- **Français :** modèle d'embeddings multilingue, ou réécriture FR→EN par le modèle Meta avant la recherche.
- **Index HNSW** à la place d'ivfflat.
- **Cache de requêtes paramétrées** pour les rapports récurrents (zéro appel LLM).
- **Routeur :** questions simples vers un petit modèle, complexes vers Muse Spark.
- **Question de clarification** pour les métriques ambiguës.
- **Interface web minimale** pour la démo.

---

## 5. Benchmark (preuve)

### Jeu de questions
- Fichier `benchmark/questions.yaml`, **20+ questions** sur 4 modules : comptabilité, ventes, RH, inventaire.
- Majorité de questions « rapport » : comparaisons de périodes, classements par groupe, multi-tables.
- Format :
  ```yaml
  - id: acc_001
    module: accounting
    category: ranking        # lookup | aggregation | period_comparison | ranking | multi_table
    question: "Top 10 customers by unpaid invoice amount"
    reference_sql: |
      SELECT ...
    notes: "Posted customer invoices only"
  ```
- Chaque `reference_sql` est écrite à la main et **exécutée sur la base de démo** pour vérifier qu'elle renvoie un résultat non vide et correct.

### Métriques
- **Execution-match accuracy :** le SQL généré renvoie les mêmes lignes que la référence.
  - Comparaison indépendante des noms de colonnes ; ordre ignoré sauf si la référence a un `ORDER BY` ; tolérance numérique sur les décimaux.
- **Latence par étape** (médiane et p95) et totale.
- **Tokens par question** (prompt et complétion).
- **Taux d'échec explicite** (le système dit « je ne sais pas ») vs **taux de mauvaise réponse** (pire cas).

### Comparaisons à présenter
1. Schéma complet dans le prompt vs schéma réduit.
2. Début du hackathon (Qwen3, 42 tables enrichies) vs fin (Muse Spark, enrichissement complet, pipeline complet).
3. Optionnel : Muse Spark vs open-weights Meta hébergé vs local.

### Livrable
- Script `benchmark/run.py` → rapport Markdown `benchmark/results/<date>_<backend>.md` + JSON brut.

---

## 6. Exigences non fonctionnelles

- **Sécurité :** lecture seule, timeout, plafond de lignes, tables sensibles exclues, aucun secret dans Git.
- **Confidentialité :** base de démo uniquement ; mode local disponible pour les données sensibles.
- **Reproductibilité :** `docker compose up` démarre tout ; `.env.example` documente toutes les variables.
- **Observabilité :** `/health` sur chaque service ; logs structurés.
- **Portabilité :** rien de spécifique à Odoo dans l'orchestrateur ; les règles Odoo sont isolées (filtres d'indexation, règles métier, liste de tables interdites).

---

## 7. Workflow Git

- **Branche `main` protégée**, toujours fonctionnelle. Aucun commit direct.
- **Une branche par PR :** `feat/…`, `fix/…`, `bench/…`, `docs/…`, `chore/…`.
- **Commits** au format conventionnel : `feat(api): add /ask orchestration`.
- **Template de PR** (`.github/pull_request_template.md`) :
  ```markdown
  ## Quoi
  ## Pourquoi (élément du planning)
  ## Comment c'est testé
  ## Impact benchmark (exactitude / latence / tokens) — si le pipeline est touché
  ## Changement de contrat d'API ? oui / non
  ```
- **CI minimale (GitHub Actions) :** `ruff` (lint) + `pytest` (tests unitaires sans GPU ni base réelle : parsing, validation, formatage, comparaison de résultats).
- **`.gitignore` :** `models/`, `*.gguf`, `.env`, `logs/`, dumps de base, données clients.
- **Definition of Done :** code relu, tests passent, `docker compose up` fonctionne, README mis à jour si nécessaire, benchmark relancé si le pipeline est touché.

---

## 8. Planning par PR (heure du Maroc)

| Jour | Date | PR / tâche | Résultat attendu |
|---|---|---|---|
| J0 | 26 oct (soir) | `chore/repo-setup` : inventaire du code, `.gitignore`, `.env.example`, template de PR, CI | Repo propre, section 3 confirmée |
| J1 | 27 oct | `bench/questions` : 20+ questions + SQL de référence vérifiées | Jeu de test prêt |
| J1 | 27 oct | `feat/api-orchestration` : `/ask` enchaîne schema_service → model_service (Qwen actuel) | Première réponse SQL de bout en bout |
| J2 | 28 oct | `feat/sql-validation-execution` : validation, rôle read-only, exécution, boucle de correction | Réponses avec données réelles |
| J2 | 28 oct | `bench/harness` + **benchmark n°1 (référence)** | Chiffres de départ |
| J3 | 29 oct | `feat/model-backends` : abstraction + backend Muse Spark ; `feat/telemetry` : latence et tokens | Muse Spark branché, mesures automatiques |
| J4 | 30 oct | `feat/schema-reduction` : expansion FK, élagage des colonnes, budget de tokens, règles Odoo | Prompts plus petits, meilleure exactitude |
| J5 | 31 oct | `feat/schema-enrichment` : 384 descriptions par Muse Spark, réindexation | 426 / 426 tables enrichies |
| J6 | 1 nov | `feat/output-formatting` : tableau, CSV/XLSX, résumé vérifié ; **benchmark n°2** | Réponses lisibles, progression mesurée |
| J7 | 2 nov | Analyse des erreurs du benchmark, corrections de prompt et de règles ; interface de démo minimale | Exactitude améliorée, démo utilisable |
| J8 | 3 nov | Stretch goals selon le temps (français, HNSW, comparaison open-weights) ; **benchmark final** | Chiffres finaux |
| J9 | 4 nov | **Gel du code à 12h00.** README, vidéo de démo, rapport du benchmark, **soumission avant 18h00** | Projet soumis (limite réelle : 5 nov 02h00) |

**Règles de priorité :** si un jour prend du retard, les stretch goals (J8) sautent en premier, puis le graphique et l'interface. Le benchmark, la validation et l'intégration Meta ne sautent jamais.

---

## 9. Démo et soumission

- **Scénario de démo (2–3 min) :** une question simple → une comparaison de périodes → un classement multi-tables → une question piège (table inexistante, demande d'écriture) correctement refusée → le tableau du benchmark avant/après.
- **Toujours montrer** le SQL exécuté et les tables utilisées.
- **README :** problème, architecture (schéma section 4), installation, exemples, résultats du benchmark, limites connues.
- `[À VÉRIFIER]` exigences exactes de soumission dans les règles officielles.

---

## 10. Points ouverts

- Accès à la Meta Model API depuis le Maroc et format exact de l'API Muse Spark.
- Choix du modèle open-weights Meta pour le mode local (doit tenir dans ~11 Go de VRAM).
- Endpoints exacts du schema_service et état réel de l'api_service.
- Modules Odoo installés dans la base de démo et validité des règles métier de la section 4.3.
- Règles officielles du hackathon (soumission, modèles autorisés).
