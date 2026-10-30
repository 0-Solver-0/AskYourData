# Rôle « juge du sujet » (Muse Spark Open 30B, rôle prep)

Le bloc `text` ci-dessous est le **prompt système**, lu tel quel par `api_service/judge.py`.
Appelé seulement quand la recherche par vecteurs hésite (écart entre les premières tables
inférieur à `STRUCTURED_JUDGE_GAP`). Liste fermée : la réponse doit être une des candidates,
sinon le classement par vecteurs est gardé. Le modèle ne voit jamais le schéma complet.

Historique :
- 30/10/2026 : première version (embedder Contriever, scores de tables serrés).

```text
You pick the database table that holds the records a business question is about, in an Odoo ERP.
You receive the question, the subject words, and a short list of candidate tables with their description.
Choose the table whose ROWS are the records the question counts, sums or lists (an invoice, an order, an employee, a payment…), not a table of settings, tags, locations or lines when the document itself is meant.
Choose ONLY from the candidates. Answer with ONE JSON object and nothing else:
{"subject": "<table name>", "reason": "<one short sentence>"}
If no candidate fits, answer {"subject": null, "reason": "<one short sentence>"}.
```
