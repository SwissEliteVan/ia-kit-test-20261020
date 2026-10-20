"""Noyau local du kit. Python 3.10+, bibliothèque standard uniquement.

Le plan de contrôle doit être inaccessible en écriture aux agents exécutés.
SQLite sur disque local partagé par tous les workers ; pas de SQLite sur NFS.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time


STATES = {
    'OPEN': {'CLAIMED'}, 'CLAIMED': {'PLANNED'},
    'PLANNED': {'IMPLEMENTED'}, 'IMPLEMENTED': {'TESTED', 'TEST_FAILED'},
    'TEST_FAILED': {'PLANNED'}, 'TESTED': {'REVIEW_REQUESTED'},
    'REVIEW_REQUESTED': {'APPROVED', 'FIX_REQUIRED'},
    'FIX_REQUIRED': {'PLANNED'}, 'APPROVED': {'RELEASED'},
    'RELEASED': {'CLOSED'}, 'CLOSED': set(),
}
HOLDS = {'WAITING_AGENT', 'AGENT_UNAVAILABLE', 'TRIGGER_FAILED', 'TIMED_OUT',
         'CONFLICT', 'STALE', 'HUMAN_REQUIRED', 'INTERRUPTED'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def git(root, *args):
    p = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                       text=True, timeout=20, check=True)
    return p.stdout.strip()


def preflight(root, base):
    if git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('CONFLICT: modifications locales présentes')
    if git(root, 'rev-parse', 'HEAD') != base:
        raise ValueError('STALE: HEAD différent de la base')
    return base


def publication_check(root, base, candidate, remote_ref=None):
    """Lecture seule. Le fetch est effectué séparément par le transport autorisé."""
    preflight(root, candidate)
    if git(root, 'merge-base', base, candidate) != base:
        raise ValueError('CONFLICT: candidat non descendant de la base')
    if remote_ref and git(root, 'rev-parse', remote_ref) != base:
        raise ValueError('STALE: référence distante modifiée ; fetch récent requis')
    return candidate


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, state TEXT NOT NULL, revision INTEGER NOT NULL,
          body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, task TEXT NOT NULL, request TEXT NOT NULL,
          result TEXT NOT NULL, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS active(
          slot INTEGER PRIMARY KEY CHECK(slot=1), task TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS switches(name TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS runs(
          id TEXT PRIMARY KEY, task TEXT NOT NULL, role TEXT NOT NULL,
          status TEXT NOT NULL, receipt TEXT NOT NULL);
        ''')

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()

    def task(self, task):
        r = self.db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        if not r:
            raise ValueError('tâche inconnue')
        return dict(r, body=json.loads(r['body']))

    def stopped(self):
        return self.db.execute("SELECT 1 FROM switches WHERE name='stop' AND value='1'").fetchone() is not None

    def stop(self, enabled=True):
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO switches VALUES('stop',?)", ('1' if enabled else '0',))

    def create(self, task, base, scope, acceptance):
        if not task or not acceptance or not scope or len(acceptance) > 4000 or len(scope) > 100:
            raise ValueError('id, périmètre et acceptation requis')
        for p in scope:
            if Path(p).is_absolute() or '..' in Path(p).parts or '\\' in p or ':' in p:
                raise ValueError('périmètre relatif invalide')
        with self.transaction():
            if self.stopped():
                raise ValueError('STOP')
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?)',
                            (task, 'OPEN', 0, encode({'base': base, 'scope': scope,
                             'acceptance': acceptance, 'checks': [], 'attempts': 0})))
        return self.task(task)

    def transition(self, event, task, revision, target, actor, evidence=None):
        evidence = evidence or {}
        request = encode([task, revision, target, actor, evidence])
        with self.transaction():
            old = self.db.execute('SELECT * FROM events WHERE id=?', (event,)).fetchone()
            if old:
                if old['request'] != request:
                    raise ValueError('identifiant événement réutilisé avec autre contenu')
                return json.loads(old['result'])
            if self.stopped():
                raise ValueError('STOP')
            t = self.task(task)
            if t['revision'] != revision:
                raise ValueError('révision périmée')
            state, b = t['state'], t['body']
            if state == 'CLOSED':
                raise ValueError('tâche close immuable')
            if state in HOLDS:
                if target != b.get('resume') or actor != 'operator':
                    raise ValueError('reprise explicite opérateur requise après réconciliation')
                b.pop('resume')
            elif target in HOLDS:
                b['resume'] = state
            elif target not in STATES.get(state, set()):
                raise ValueError('transition interdite')
            if target == 'CLAIMED' and state == 'OPEN':
                self.db.execute('INSERT INTO active VALUES(1,?)', (task,))
                b['author'] = actor
            if target == 'TESTED':
                checks = evidence.get('checks', [])
                if not checks or any(c.get('status') != 'PASS' or c.get('exit') != 0
                                     or not c.get('command') for c in checks):
                    raise ValueError('tests PASS avec commande et sortie requis')
                if not evidence.get('head'):
                    raise ValueError('empreinte du candidat requise')
                b.update(checks=checks, head=evidence['head'])
            if target == 'TEST_FAILED' and evidence.get('head'):
                b['head'] = evidence['head']
            if target in {'APPROVED', 'FIX_REQUIRED'}:
                if actor == b.get('author') or actor != evidence.get('reviewer'):
                    raise ValueError('revue indépendante requise')
                if evidence.get('head') != b.get('head') or not evidence.get('proof'):
                    raise ValueError('revue périmée ou sans preuve')
                b['review'] = evidence
            if target == 'PLANNED' and state in {'FIX_REQUIRED', 'TEST_FAILED'}:
                b['attempts'] += 1
                if b['attempts'] > 3:
                    raise ValueError('limite corrections atteinte : escalade')
                # La base historique reste traçable ; la correction part du
                # candidat effectivement testé, pas du premier commit du ticket.
                b['work_base'] = b.get('head', b.get('work_base', b['base']))
                b.pop('review', None)
                b.pop('head', None)
                b['checks'] = []
            if target == 'RELEASED':
                if actor != 'operator' or not evidence.get('verified') or evidence.get('head') != b.get('head'):
                    raise ValueError('vérification du candidat requise')
            if target == 'CLOSED':
                if actor != 'operator' or not evidence.get('acceptance_verified'):
                    raise ValueError('acceptation non vérifiée')
                self.db.execute('DELETE FROM active WHERE task=?', (task,))
            result = dict(id=task, state=target, revision=revision+1, body=b)
            self.db.execute('UPDATE tasks SET state=?,revision=?,body=? WHERE id=?',
                            (target, revision+1, encode(b), task))
            self.db.execute('INSERT INTO events VALUES(?,?,?,?,?)',
                            (event, task, request, encode(result), time.time()))
            return result

    def run(self, task, role, config, root, request=None):
        """Adaptateur de processus configuré par l'opérateur, jamais par une issue.

        Reçu de processus seulement : ne valide ni tests, ni revue, ni tâche.
        Le wrapper doit fournir isolation OS, plafond fournisseur et résultat JSON.
        """
        agent = config['agents'][role]
        if config['automation']['level'] != 3 or not agent.get('enabled'):
            raise ValueError('AGENT_UNAVAILABLE')
        if not agent.get('isolation_verified') or not agent.get('provider_cap_verified'):
            raise ValueError('HUMAN_REQUIRED: isolation et plafond fournisseur non vérifiés')
        if role not in {'A', 'B'} or not isinstance(agent.get('argv'), list) or not agent['argv']:
            raise ValueError('adaptateur invalide')
        timeout = config['automation']['timeout_seconds']
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise ValueError('timeout invalide')
        with self.transaction():
            if self.stopped():
                raise ValueError('STOP')
            t = self.task(task)
            request = request if request is not None else f"revision:{t['revision']}"
            runid = hashlib.sha256(encode([task, role, request]).encode()).hexdigest()
            fingerprint = hashlib.sha256(encode(agent).encode()).hexdigest()
            previous = self.db.execute('SELECT status,receipt FROM runs WHERE id=?', (runid,)).fetchone()
            if previous:
                receipt = json.loads(previous['receipt'])
                if receipt.get('fingerprint') != fingerprint:
                    raise ValueError('clé de run réutilisée avec autre configuration')
                return dict(run=runid, **dict(receipt, status=previous['status']))
            if t['state'] != ('PLANNED' if role == 'A' else 'REVIEW_REQUESTED'):
                raise ValueError('état incompatible avec le rôle')
            if role == 'B' and config['agents']['A']['identity'] == agent['identity']:
                raise ValueError('identités de revue identiques')
            active = self.db.execute('SELECT task FROM active WHERE slot=1').fetchone()
            if not active or active['task'] != task:
                raise ValueError('réservation absente')
            if self.db.execute("SELECT 1 FROM runs WHERE status='RUNNING'").fetchone():
                raise ValueError('autre exécution active ou interrompue à réconcilier')
            count = self.db.execute('SELECT count(*) FROM runs WHERE task=?', (task,)).fetchone()[0]
            # Chaque essai réserve le plafond maximal ; jamais de remboursement automatique.
            cap = agent['max_cost_units']
            spent = sum(json.loads(r[0])['reserved'] for r in self.db.execute(
                'SELECT receipt FROM runs WHERE task=?', (task,)))
            if cap <= 0 or count >= config['automation']['max_runs'] or spent+cap > config['automation']['task_cost_units']:
                raise ValueError('BUDGET_EXHAUSTED')
            base = t['body'].get('head', t['body']['base']) if role == 'B' else t['body'].get('work_base', t['body']['base'])
            preflight(root, base)
            receipt = {'reserved': cap, 'identity': agent['identity'], 'base': base,
                       'started': time.time(), 'role': role, 'fingerprint': fingerprint}
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',
                            (runid, task, role, 'RUNNING', encode(receipt)))
        # Le wrapper reçoit des données JSON sur stdin ; aucune interpolation shell.
        status = 'TRIGGER_FAILED'
        try:
            env = {k: v for k, v in os.environ.items()
                   if k in {'PATH', 'SystemRoot', 'TEMP', 'TMP', 'HOME', 'USERPROFILE'}}
            with open(os.devnull, 'w') as sink, tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as payload:
                payload.write(encode({'task': t, 'run': runid, 'role': role,
                                      'untrusted_data': True}))
                payload.seek(0)
                proc = subprocess.Popen(agent['argv'], cwd=root, env=env, stdin=payload,
                                        stdout=sink, stderr=sink, text=True, shell=False)
                deadline = time.monotonic()+timeout
                while proc.poll() is None:
                    if self.stopped() or time.monotonic() >= deadline:
                        proc.kill()
                        proc.wait(timeout=5)
                        status = 'INTERRUPTED' if self.stopped() else 'TIMED_OUT'
                        break
                    time.sleep(0.1)
                else:
                    receipt['exit'] = proc.returncode
                    status = 'COMPLETED' if proc.returncode == 0 else 'TRIGGER_FAILED'
        except OSError:
            status = 'TRIGGER_FAILED'
        receipt.update(ended=time.time(), status=status)
        # Arrêter les descendants et les appels distants incombe au wrapper isolé.
        with self.transaction():
            self.db.execute('UPDATE runs SET status=?,receipt=? WHERE id=?',
                            (status, encode(receipt), runid))
        return dict(run=runid, **receipt)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default='.dual-state/queue.sqlite')
    sub = p.add_subparsers(dest='command', required=True)
    c = sub.add_parser('create')
    c.add_argument('id'); c.add_argument('--base', required=True)
    c.add_argument('--scope', nargs='+', required=True); c.add_argument('--acceptance', required=True)
    c = sub.add_parser('show'); c.add_argument('id')
    c = sub.add_parser('event'); c.add_argument('file')
    c = sub.add_parser('run'); c.add_argument('id'); c.add_argument('role', choices=['A', 'B'])
    c.add_argument('--config', required=True); c.add_argument('--root', required=True)
    c.add_argument('--request', help='ID stable de livraison ; défaut lié à la révision ; retry explicite = nouvel ID')
    sub.add_parser('stop'); sub.add_parser('resume')
    a = p.parse_args(); s = Store(a.db)
    try:
        if a.command == 'create': result = s.create(a.id, a.base, a.scope, a.acceptance)
        elif a.command == 'show': result = s.task(a.id)
        elif a.command == 'event': result = s.transition(**json.loads(Path(a.file).read_text(encoding='utf-8')))
        elif a.command == 'run': result = s.run(a.id, a.role, json.loads(Path(a.config).read_text(encoding='utf-8')), Path(a.root).resolve(), a.request)
        else: s.stop(a.command == 'stop'); result = {'stopped': s.stopped()}
        print(encode(result))
    except (ValueError, sqlite3.Error, subprocess.SubprocessError) as e:
        print(encode({'status': 'BLOCKED', 'reason': str(e)})); raise SystemExit(2)
    finally:
        s.close()


if __name__ == '__main__':
    main()
