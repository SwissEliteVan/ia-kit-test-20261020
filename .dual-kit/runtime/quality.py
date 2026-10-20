"""Détecter sans exécuter de commandes issues du projet. Sortie JSON."""
import argparse
import json
from pathlib import Path
import hashlib
import os
import subprocess
import tempfile


def run_approved(root, argv, timeout=120):
    """L'opérateur a déjà vérifié la commande et isolé son environnement.

    Ne jamais remplir argv à partir d'une issue. Aucun log brut n'est publié.
    """
    if not isinstance(argv, list) or not argv or not 1 <= timeout <= 3600:
        raise ValueError('commande ou délai invalide')
    with tempfile.TemporaryFile() as log:
        try:
            p = subprocess.run(argv, cwd=root, stdout=log, stderr=log,
                               timeout=timeout, shell=False)
            log.seek(0)
            digest = hashlib.file_digest(log, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(log.read()).hexdigest()
            return {'command': argv, 'exit': p.returncode,
                    'status': 'PASS' if p.returncode == 0 else 'FAIL', 'log_sha256': digest}
        except FileNotFoundError:
            return {'command': argv, 'exit': None, 'status': 'BLOCKED', 'reason': 'outil absent'}
        except subprocess.TimeoutExpired:
            return {'command': argv, 'exit': None, 'status': 'BLOCKED', 'reason': 'timeout'}


MARKERS = {'Node': ['package.json'], 'Python': ['pyproject.toml', 'requirements.txt', 'setup.py'],
           'PHP': ['composer.json'], 'Java': ['pom.xml', 'build.gradle', 'build.gradle.kts'],
           'Go': ['go.mod'], 'Rust': ['Cargo.toml'], 'Mobile': ['pubspec.yaml', 'Package.swift'],
           'Desktop': ['Cargo.toml', '*.csproj', '*.sln', 'CMakeLists.txt']}


def detect(root):
    root = Path(root)
    kinds = [k for k, marks in MARKERS.items() if any(list(root.glob(m)) for m in marks)]
    if not kinds:
        kinds = ['Documentaire' if list(root.glob('*.md')) else 'Inconnu']
    scripts = {}
    if (root/'package.json').is_file():
        scripts = json.loads((root/'package.json').read_text(encoding='utf-8')).get('scripts', {})
    return {'types': kinds, 'node_scripts_present': sorted(scripts),
            'status': 'NOT_RUN', 'reason': 'détection seulement ; commandes à approuver',
            'conditional': {'all': ['tests', 'secrets', 'licences', 'dépendances'],
                            'web_ui': ['a11y', 'SEO si public', 'navigateur', 'mobile'],
                            'translations': ['clés', 'placeholders', 'revue linguistique'],
                            'media': ['droits', 'intégrité', 'budget'],
                            'database': ['migration sur copie', 'restauration']}}


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('root'); a = p.parse_args()
    print(json.dumps(detect(a.root), ensure_ascii=False))
