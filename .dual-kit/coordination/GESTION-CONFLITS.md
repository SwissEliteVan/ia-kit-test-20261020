# Conflits, interruptions et reprises

Un commentaire CLAIM n'est pas atomique. Niveau 1 : une seule session auteur,
relecture de la source commune, STOP si ambiguïté. Niveaux outillés : le slot
transactionnel unique sérialise les tâches utilisant la même base SQLite.
Il ne protège pas contre un éditeur externe ou une deuxième base indépendante.

CONFLICT/STALE : conserver le travail, base et patch ; inspecter les écarts et
réserver de nouveau après accord sur la stratégie. Aucune suppression automatique.
TIMED_OUT/TRIGGER_FAILED/INTERRUPTED : consulter les reçus, arrêter les descendants
et appels distants via le wrapper, puis vérifier qu'aucun worker n'agit encore.
Le crash laisse RUNNING et bloque tout nouveau run jusqu'À réconciliation.

Aucune reprise automatique aveugle après effet externe inconnu : une API sans clé
idempotente peut avoir démarré malgré un timeout. Rechercher par run_id avant retry.
B absente : WAITING_AGENT. Tests en échec : TEST_FAILED. Décision sensible :
HUMAN_REQUIRED. L'absence de réponse ne constitue jamais une approbation.
Après trois corrections infructueuses : escalade avec preuve et décision minimale.
