# Enrichissement des tables

Fichiers d'enrichissement validés (métadonnées de la base de démo uniquement : ni données, ni secrets).
Format : celui produit par `GET /api/v1/schema/export`, avec les blocs `enrich` remplis.

```bash
# 1. Modules disponibles
curl "http://localhost:5000/api/v1/schema/modules?account_id=1"

# 2. Export d'un module
curl -o enrichment/account.json "http://localhost:5000/api/v1/schema/export?account_id=1&module=account"

# 3. Remplir les blocs « enrich » (tables : label, description, synonyms, domain ;
#    colonnes : label, synonyms, notes, always_include = true pour une colonne à toujours envoyer)
#    null = inchangé, "" = effacer

# 4. Simulation : liste des changements, rien n'est écrit
curl -X POST "http://localhost:5000/api/v1/schema/import?account_id=1&dry_run=true" \
  -H "Content-Type: application/json" --data-binary @enrichment/account.json

# 5. Import : applique et réencode les seules tables modifiées
curl -X POST "http://localhost:5000/api/v1/schema/import?account_id=1" \
  -H "Content-Type: application/json" --data-binary @enrichment/account.json
```

Règles :
- décrire ce que contient la table, de façon générale, sans viser une question de benchmark ;
- une réindexation (`/index`) n'écrase jamais les champs importés ;
- réimporter le même fichier ne change rien (aucun champ modifié) ;
- `always_include` (statut, type de document, actif…) ne demande pas de réencoder la table.
