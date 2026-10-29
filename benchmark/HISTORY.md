# Historique des mesures de la recherche de schéma

*Archivé le 29/10/2026 : les mesures sont mises en pause pendant la mise au point de la nouvelle forme de recherche (question découpée en morceaux de sens). Ces chiffres servent de référence.*

Base de démo Odoo 18, 12 questions de [`schema_questions.yaml`](schema_questions.yaml). Ces questions ont servi aux réglages : les chiffres sont optimistes.

| Date | Version | Embedder | Complètes | Rappel | Précision | Tables/question | Tokens | Temps |
|---|---|---|---|---|---|---|---|---|
| 28/10 | PR #9, legacy | MiniLM | 11/12 | 96 % | n.m. | n.m. | ~1043 | n.m. |
| 28/10 | PR #10, legacy | MiniLM | 12/12 | 100 % | 33 % | 5,8 | ~1012 | 89 ms |
| 28/10 | PR #10, coverage | MiniLM | 8/12 | 83 % | 53 % | 3,0 | ~388 | 69 ms |
| 28/10 | PR #11, legacy | MiniLM | 12/12 | 100 % | 33 % | 5,8 | ~1012 | 86 ms |
| 28/10 | PR #11, coverage (contexte 2) | MiniLM | 6/12 | 74 % | 55 % | 2,6 | ~290 | 70 ms |
| 28/10 | PR #11, legacy | Qwen3-Embedding-0.6B | 12/12 | 100 % | 31 % | 6,2 | ~1000 | 133–206 ms |
| 28/10 | PR #11, coverage (contexte 2) | Qwen3-Embedding-0.6B | 4/12 | 65 % | 62 % | 2,0 | ~244 | 205 ms |
| 28/10 | PR #11, coverage (contexte 0) | Qwen3-Embedding-0.6B | 6/12 | 74 % | 55 % | 2,6 | ~348 | 213 ms |

Le mode coverage a été retiré du code le 29/10.

## Effet de l'enrichissement manuel du module `account` (Qwen, legacy, CPU)

Question « Customers with the highest unpaid invoice amount, by salesperson » :

| | Avant | Après |
|---|---|---|
| Score de `account_move` | 0,544 (1er, 2e à 0,49) | **0,654** (1er, 2e à 0,50) |
| Correspondance de mots | 0,6 | 0,9 |
| Rang de `account_move` par vecteur seul | 13e (0,367) | **2e** (0,43) |
| Tables envoyées | 7 | 7 (`account_journal` sort, `account_bank_statement_line` entre) |
| Tokens | ~1716 | ~1847 |

Défauts du schéma révélés par l'enrichissement : `payment_state` évincé par la limite de 15 colonnes, notes non affichées au modèle SQL, description coupée à 160 caractères.

## Leçons
- La recherche legacy trouve toujours les bonnes tables (rappel de 100 %), mais en envoie trop (précision d'environ 30 %).
- La sélection par couverture mot par mot n'est pas viable : un mot seul ne veut rien dire pour un embedder.
- Changer d'embedder ne change rien si les fiches de tables sont pauvres.
- L'enrichissement des fiches est ce qui fait progresser le classement.
