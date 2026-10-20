# Synchronisation et publication Git contrôlées

Le module opérationnel est [gitflow.py](../runtime/gitflow.py), avec le contrat
[GIT-AUTOMATIQUE.md](../automatisation/GIT-AUTOMATIQUE.md). Il complète les simples
helpers de lecture preflight/publication_check du noyau initial.

Avant intervention : racine/remote/branche validés, état local propre, fetch,
HEAD et distant comparés. Fast-forward seulement si permis. Avance inconnue,
divergence ou conflit : arrêt sans écrasement, nettoyage ou résolution implicite.

Après A : scope, tests exécutés, stabilité des fichiers, commit identifiable,
nouveau fetch/recheck puis push de HEAD vers la ref configurée. Droits commit et
push distincts, politique de branche protégée vérifiée, pas de force-push.
Le SHA réellement publié est relu depuis le remote et transmis à B ; son verdict
est lié au candidat et aux preuves. Les corrections peuvent boucler sans messager
humain quand les wrappers/dispatcher sont réellement connectés.

Main-only : aucune branche/PR créée, deux clones sur la branche autorisée,
réservation séquentielle et revue post-commit/post-push clairement annoncée.
Ce mode ne prétend pas fournir une revue avant publication du code sur la branche.
Une CI post-push ne protège pas rétroactivement le commit. Le module n'effectue
ni merge de branches, ni déploiement ; une politique PR existante doit être
respectée en choisissant la branche de travail autorisée.

Une SQLite privée et commune sérialise les participants ; elle ne verrouille pas
les éditeurs externes ni un deuxième contrôleur avec une autre base. Les rechecks
et le rejet Git normal des pushes concurrents restent nécessaires. Après erreur,
consulter la phase et les commandes, conserver le travail. Une réconciliation
d'effet interrompu exige vérification des processus et SHA ; jamais de timeout
considéré comme permission de voler le lock ou réécrire l'historique.
