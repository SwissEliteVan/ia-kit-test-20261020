# Enveloppe unique de transition v2

Les anciens formats textuels discordants ne sont plus interprétés comme commandes.
Le JSON ci-dessous est un exemple de fichier donné à `dual.py event` par le
contrôle opérateur authentifié. Les issues ne peuvent pas invoquer cette CLI.

```json
{"event":"delivery-unique-001","task":"TASK-001","revision":0,"target":"CLAIMED","actor":"A","evidence":{}}
```

Création : `dual.py create TASK-001 --base SHA --scope src/module --acceptance CRITERE`.
La tâche conserve ID, base, scope, acceptation, auteur, état et révision.
TESTED : evidence.head et checks [{command, exit:0, status:PASS}].
APPROVED/FIX_REQUIRED : reviewer diffèrent de author, head exact et proof.
RELEASED : actor=operator, verified=true après contrôle Git et head exact.
CLOSED : actor=operator, acceptance_verified=true après lecture des critères.
Ces booléens viennent du vérificateur de confiance, jamais d'un message de l'agent.

Identifiant événement stable par livraison ; retries avec même charge utile
retournent le même résultat sans dupliquer. Autre contenu avec même ID : rejet.
Les références de preuves ne contiennent ni secrets ni données client.

Déclenchement de processus : --request doit être l’identifiant stable de livraison
pour un transport externe. Un doublon explicite reste dédupliqué après changement
de révision. Sans cet argument, la clé est liée à la révision courante. Retry
intentionnel : nouvel ID après réconciliation, dans les budgets restants.
