"""Installation dans un espace de noms neuf. Jamais d'écrasement ni de CI activée."""
import argparse
import json
from pathlib import Path
import shutil


def plan(target):
    target = Path(target).resolve()
    if not target.is_dir():
        raise ValueError('créer explicitement le dossier du projet avant installation')
    paths = ['AGENTS.md', 'README.md', '.github/workflows', '.gitlab-ci.yml',
             'projet/NOW.md', 'projet/DECISIONS.md', '.dual-kit']
    return {'existing': [p for p in paths if (target/p).exists()],
            'destination': '.dual-kit', 'overwrite': False,
            'blocked': (target/'.dual-kit').exists() or (target/'.dual-kit').is_symlink()}


def install(target, apply=False):
    target = Path(target).resolve()
    result = plan(target)
    if apply:
        if result['blocked']:
            raise FileExistsError('collision .dual-kit ; aucun fichier modifié')
        source = Path(__file__).resolve().parents[1]
        dest = target/'.dual-kit'
        dest.mkdir()  # opération exclusive ; une installation concurrente échoue
        for name in ['runtime', 'config', 'coordination', 'prompts']:
            shutil.copytree(source/name, dest/name, ignore=shutil.ignore_patterns('__pycache__', 'verify_kit.py'))
        shutil.copy2(source/'config/kit.example.json', dest/'config/kit.json')
        shutil.copy2(source/'00-START.md', dest/'00-START.md')
        result['installed'] = True
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('target'); p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    print(json.dumps(install(a.target, a.apply), ensure_ascii=False))
