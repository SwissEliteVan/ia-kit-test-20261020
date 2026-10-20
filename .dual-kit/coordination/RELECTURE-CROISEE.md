# Revue indépendante

B lit un snapshot du candidat et ses critères ; elle ne modifie pas le workspace A.
Elle vérifie code, tests et risques applicables au profil. La revue cite le SHA
ou l'empreinte de diff, les défauts avec reproduction et les commandes rejouées.
PASS reproduit, PASS CI consulté, FAIL, NOT_RUN et BLOCKED restent distincts.

Verdict APPROVED ou FIX_REQUIRED avec reviewer, head et proof. Toute modification
invalide la revue antérieure. L'opérateur vérifie identité, snapshot, résultats
et absence de bloqueur ; une assertion APPROVED dans une issue ne suffit pas.
Sans session B réelle : revue NOT_RUN, attente. Le test de fixture de ce kit ne
constitue pas une revue IA indépendante. Aucun verdict n'autorise la production.
