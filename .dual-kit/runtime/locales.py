"""Parité de JSON et placeholders simples {nom}, locales explicitement fournies.
Pas de prise en charge complète ICU/pluriels ; revue linguistique distincte.
"""
import argparse
import json
from pathlib import Path
import re


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('clé JSON dupliquée')
        result[key] = value
    return result


def flatten(obj, prefix=''):
    if not isinstance(obj, dict):
        raise ValueError('objet JSON requis')
    out = {}
    for key, value in obj.items():
        # Éviter collisions entre clé contenant un point et namespace imbriqué.
        if '.' in key:
            raise ValueError('clé contenant un point non supportée')
        name = f'{prefix}.{key}' if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, name))
        elif isinstance(value, str) and value.strip():
            out[name] = value
        else:
            raise ValueError('chaîne vide ou valeur non textuelle')
    return out


def check(paths):
    if not paths:
        return {'status': 'NOT_RUN', 'reason': 'aucune locale configurée'}
    entries = [flatten(json.loads(Path(p).read_text(encoding='utf-8'), object_pairs_hook=pairs)) for p in paths]
    base = entries[0]; failures = []
    for index, entry in enumerate(entries[1:], 1):
        if set(entry) != set(base):
            failures.append({'locale_index': index, 'reason': 'clés différentes'})
        for key in set(entry) & set(base):
            if set(re.findall(r'\{([A-Za-z_][A-Za-z0-9_]*)\}', entry[key])) != set(
                    re.findall(r'\{([A-Za-z_][A-Za-z0-9_]*)\}', base[key])):
                failures.append({'locale_index': index, 'reason': 'placeholders différents'})
    return {'status': 'FAIL' if failures else 'PASS', 'failures': failures,
            'locales': len(paths), 'linguistic_review': 'NOT_RUN'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('files', nargs='*'); a = p.parse_args()
    try:
        result = check(a.files)
    except (OSError, ValueError):
        result = {'status': 'FAIL', 'reason': 'ressource absente ou invalide'}
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(1 if result['status'] == 'FAIL' else 0)
