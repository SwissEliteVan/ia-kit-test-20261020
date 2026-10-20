"""Validation d'une livraison webhook en mémoire ; ne déclenche aucune commande.

Le transport doit borner le corps HTTP et vérifier l'autorisation courante via
l'API du fournisseur AVANT appel. Les doublons sont traités par Store.transition.
"""
import hashlib
import hmac
import json

EVENTS = {'issues', 'issue_comment', 'pull_request', 'check_run', 'workflow_dispatch'}


def validate(raw, signature, secret, delivery, kind, repository, allowed, permission):
    if not secret or len(raw) > 65536 or not delivery or len(delivery) > 200:
        raise ValueError('livraison invalide')
    expected = 'sha256=' + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError('signature invalide')
    data = json.loads(raw)
    sender = data.get('sender', {})
    if (kind not in EVENTS or data.get('repository', {}).get('full_name') != repository
            or sender.get('login') not in allowed or sender.get('type') != 'User'
            or permission not in {'write', 'maintain', 'admin'}):
        raise ValueError('événement ou auteur non autorisé')
    # Ne convertir jamais un commentaire libre en transition APPROVED/CLOSED.
    return {'event': delivery, 'kind': kind, 'repository': repository,
            'actor': sender['login'], 'status': 'RECEIVED_UNTRUSTED',
            'body_sha256': hashlib.sha256(raw).hexdigest()}
