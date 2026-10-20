"""Transport Git opérateur : configuration de confiance, aucun texte d'issue exécuté.

Le répertoire de contrôle et ses credentials ne doivent pas être modifiables par
les agents. Aucun merge de branches, reset, clean, rebase ou push forcé.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid


def dump(obj):
    return json.dumps(obj, ensure_ascii=True, sort_keys=True)


class Blocked(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class GitFlow:
    def __init__(self, config):
        self.c = config
        required = ['approved', 'author_root', 'reviewer_root', 'control_dir',
                    'remote', 'remote_url', 'branch', 'scope', 'checks',
                    'allow_fetch', 'allow_ff', 'allow_commit', 'allow_push',
                    'branch_policy_verified', 'authorization', 'protected_branches',
                    'protected_push_authorized', 'timeout_seconds', 'max_rounds',
                    'hooks_policy', 'agents']
        if any(k not in config for k in required) or config['approved'] is not True:
            raise Blocked('CONFIG_NOT_APPROVED')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', config['remote']):
            raise Blocked('INVALID_REMOTE_NAME')
        if (not isinstance(config['branch'], str) or config['branch'].startswith('-')
                or not config['authorization'] or not config['scope'] or not config['checks']
                or type(config['timeout_seconds']) is not int or not 1 <= config['timeout_seconds'] <= 3600
                or type(config['max_rounds']) is not int or not 1 <= config['max_rounds'] <= 4):
            raise Blocked('INVALID_CONFIG')
        if config['hooks_policy'] not in {'require-review', 'approved-existing'}:
            raise Blocked('INVALID_HOOK_POLICY')
        for p in config['scope']:
            if not isinstance(p, str) or not p or Path(p).is_absolute() or p == '.' or '..' in Path(p).parts or '\\' in p or ':' in p or any(part.lower() == '.git' for part in Path(p).parts):
                raise Blocked('INVALID_SCOPE')
        self.roots = {role: Path(config[key]).resolve() for role, key in
                      [('A', 'author_root'), ('B', 'reviewer_root')]}
        self.control = Path(config['control_dir']).resolve()
        if self.roots['A'] == self.roots['B'] or any(self.control.is_relative_to(root) for root in self.roots.values()):
            raise Blocked('CONTROL_MUST_BE_OUTSIDE_SEPARATE_WORKSPACES')
        self.control.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.control/'gitflow.sqlite', timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY, fingerprint TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS reservations(resource TEXT PRIMARY KEY, cycle TEXT);
        CREATE TABLE IF NOT EXISTS workers(resource TEXT PRIMARY KEY, cycle TEXT, owner TEXT);
        CREATE TABLE IF NOT EXISTS commands(seq INTEGER PRIMARY KEY, cycle TEXT, role TEXT,
          argv TEXT, exit_code INTEGER, stdout_sha256 TEXT, stderr_sha256 TEXT, sha TEXT, at REAL);
        ''')
        self.request = 'inspection'

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def stopped(self):
        return (self.control/'STOP').exists()

    def env(self):
        # Credentials via gestionnaire Git/agent SSH approuvé, pas via URL/token.
        keep = {'PATH', 'SystemRoot', 'WINDIR', 'HOME', 'USERPROFILE', 'APPDATA',
                'LOCALAPPDATA', 'TEMP', 'TMP', 'SSH_AUTH_SOCK', 'XDG_CONFIG_HOME'}
        env = {k: v for k, v in os.environ.items() if k in keep}
        env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never', GIT_PAGER='cat')
        return env

    def command(self, role, argv, git=True, data=None, allowed=(0,)):
        if self.stopped():
            raise Blocked('EMERGENCY_STOP')
        overrides = ['-c', 'protocol.ext.allow=never', '-c', 'push.followTags=false',
                     '-c', 'push.recurseSubmodules=no', '-c', 'submodule.recurse=false',
                     '-c', 'fetch.pruneTags=false', '-c', f"remote.{self.c['remote']}.pruneTags=false"]
        args = ['git', *overrides, '-C', str(self.roots[role]), *argv] if git else argv
        code, out, err = -1, b'', b''
        try:
            result = subprocess.run(args, input=data, capture_output=True, env=self.env(), cwd=self.roots[role],
                                    timeout=self.c['timeout_seconds'], shell=False)
            code, out, err = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            code = -2
        except OSError:
            code = -3
        sha = out.decode('ascii', 'ignore').strip()
        if not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', sha):
            sha = None
        # Pas de stdout/stderr bruts : URL avec credentials, patch et PII exclus.
        logged = list(argv) if git else ['APPROVED_ADAPTER_OR_CHECK', hashlib.sha256(dump(argv).encode()).hexdigest()]
        if '--' in logged:
            at = logged.index('--')
            logged[at+1:] = ['<paths-redacted>']
        self.db.execute('INSERT INTO commands VALUES(NULL,?,?,?,?,?,?,?,?)',
                        (self.request, role, dump(logged), code, hashlib.sha256(out).hexdigest(),
                         hashlib.sha256(err).hexdigest(), sha, time.time()))
        if code not in allowed:
            if code == -2: raise Blocked('COMMAND_TIMEOUT')
            if code == -3: raise Blocked('COMMAND_UNAVAILABLE')
            if git and argv[0] == 'push': raise Blocked('PUSH_REJECTED_OR_TRANSPORT_FAILED')
            if git and argv[0] in {'fetch', 'ls-remote'}: raise Blocked('FETCH_OR_CONNECTION_FAILED')
            raise Blocked('GIT_COMMAND_FAILED' if git else 'CHECK_OR_ADAPTER_FAILED')
        return out

    def g(self, role, *args, allowed=(0,)):
        return self.command(role, list(args), allowed=allowed).decode('utf-8', 'surrogateescape').strip()

    def validate_repo(self, role):
        root = self.roots[role]
        if not root.is_dir() or Path(self.g(role, 'rev-parse', '--show-toplevel')).resolve() != root:
            raise Blocked('REPOSITORY_MISMATCH')
        self.g(role, 'check-ref-format', '--branch', self.c['branch'])
        if self.g(role, 'branch', '--show-current') != self.c['branch']:
            raise Blocked('BRANCH_MISMATCH_OR_DETACHED_HEAD')
        remote = self.c['remote']
        urls = self.g(role, 'remote', 'get-url', '--all', remote).splitlines()
        pushurls = self.g(role, 'remote', 'get-url', '--push', '--all', remote).splitlines()
        if urls != [self.c['remote_url']] or pushurls != urls:
            raise Blocked('REMOTE_URL_MISMATCH')
        if self.g(role, 'config', '--bool', '--get', f'remote.{remote}.mirror', allowed=(0, 1)) == 'true':
            raise Blocked('MIRROR_REMOTE_NOT_AUTHORIZED')
        url = urls[0]
        if (re.search(r'(?i)^https?://[^/]*@', url) or '?' in url or '\n' in url
                or url.startswith('-') or url.startswith('ext::')):
            raise Blocked('UNSAFE_REMOTE_URL')
        # Ne jamais laisser un fetch configuré écrire dans refs/heads ou ailleurs.
        refspecs = self.g(role, 'config', '--get-all', f'remote.{remote}.fetch', allowed=(0, 1)).splitlines()
        if not refspecs or any(':' not in r or not r.split(':', 1)[1].startswith(f'refs/remotes/{remote}/') for r in refspecs):
            raise Blocked('UNSAFE_FETCH_REFSPEC')
        if self.c['hooks_policy'] == 'require-review':
            hookpath = self.g(role, 'config', '--get', 'core.hooksPath', allowed=(0, 1))
            hooks = Path(self.g(role, 'rev-parse', '--git-path', 'hooks'))
            if not hooks.is_absolute(): hooks = root/hooks
            if hookpath or (hooks.exists() and any(p.is_file() and not p.name.endswith('.sample') for p in hooks.iterdir())):
                raise Blocked('HOOKS_REQUIRE_REVIEW')

    def clean(self, role):
        if self.command(role, ['status', '--porcelain=v1', '-z', '--untracked-files=all']):
            raise Blocked('DIRTY_WORKSPACE')

    def remote_ref(self):
        return f"refs/remotes/{self.c['remote']}/{self.c['branch']}"

    def fetch(self, role):
        if self.c['allow_fetch'] is not True:
            raise Blocked('FETCH_NOT_AUTHORIZED')
        self.g(role, 'fetch', '--prune', self.c['remote'])

    def synchronize(self, role, expected=None):
        self.validate_repo(role)
        self.clean(role)
        self.fetch(role)
        local = self.g(role, 'rev-parse', 'HEAD')
        remote = self.g(role, 'rev-parse', self.remote_ref())
        if local != remote:
            if self.g(role, 'merge-base', local, remote) != local:
                raise Blocked('LOCAL_AHEAD_OR_DIVERGED')
            if self.c['allow_ff'] is not True:
                raise Blocked('FAST_FORWARD_NOT_AUTHORIZED')
            self.clean(role)
            self.g(role, 'merge', '--ff-only', self.remote_ref())
        self.clean(role)
        head = self.g(role, 'rev-parse', 'HEAD')
        if head != remote or (expected and head != expected):
            raise Blocked('STALE_REMOTE_OR_CANDIDATE')
        return head

    def load(self, request):
        row = self.db.execute('SELECT * FROM cycles WHERE id=?', (request,)).fetchone()
        if not row:
            raise Blocked('UNKNOWN_CYCLE')
        if row['fingerprint'] != hashlib.sha256(dump(self.c).encode()).hexdigest():
            raise Blocked('CYCLE_CONFIG_CHANGED')
        return json.loads(row['body'])

    def save(self, body):
        self.db.execute('UPDATE cycles SET body=? WHERE id=?', (dump(body), self.request))

    @contextlib.contextmanager
    def worker(self, request):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', request):
            raise Blocked('INVALID_REQUEST_ID')
        self.request = request
        resources = sorted(str(p) for p in self.roots.values())
        owner = f'{os.getpid()}:{uuid.uuid4().hex}'
        try:
            with self.tx():
                for resource in resources:
                    old = self.db.execute('SELECT cycle FROM reservations WHERE resource=?', (resource,)).fetchone()
                    if old and old['cycle'] != request:
                        raise Blocked('WORKSPACE_RESERVED')
                    self.db.execute('INSERT OR IGNORE INTO reservations VALUES(?,?)', (resource, request))
                    self.db.execute('INSERT INTO workers VALUES(?,?,?)', (resource, request, owner))
        except sqlite3.IntegrityError:
            raise Blocked('WORKER_ACTIVE_OR_CRASH_REQUIRES_RECONCILIATION')
        try:
            yield
        except Blocked as error:
            row = self.db.execute('SELECT body FROM cycles WHERE id=?', (request,)).fetchone()
            if row:
                body = json.loads(row[0]); body['blocked'] = error.code
                if body['phase'] == 'PUSHING' and error.code == 'COMMAND_TIMEOUT':
                    body['transport_uncertain'] = True
                self.save(body)
            raise
        finally:
            with self.tx():
                self.db.execute('DELETE FROM workers WHERE owner=?', (owner,))
                row = self.db.execute('SELECT body FROM cycles WHERE id=?', (request,)).fetchone()
                if row and json.loads(row[0])['phase'] == 'APPROVED':
                    # Couvre aussi APPROVED observé après une course entre le
                    # précontrôle idempotent et l'acquisition du worker.
                    self.db.execute('DELETE FROM reservations WHERE cycle=?', (request,))

    def begin(self, request, task):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', task):
            raise Blocked('INVALID_TASK_ID')
        if self.db.execute('SELECT 1 FROM cycles WHERE id=?', (request,)).fetchone():
            previous = self.load(request)
            if previous['task'] != task: raise Blocked('REQUEST_REUSED')
            if previous['phase'] == 'APPROVED': return previous
        with self.worker(request):
            exists = self.db.execute('SELECT 1 FROM cycles WHERE id=?', (request,)).fetchone()
            if exists:
                body = self.load(request)
                if body['task'] != task: raise Blocked('REQUEST_REUSED')
                if body['phase'] == 'A_READY': self.synchronize('A', body['base'])
                return body
            base = self.synchronize('A')
            body = {'task': task, 'phase': 'A_READY', 'base': base, 'head': base,
                    'round': 1, 'published': [], 'reviews': [], 'checks': []}
            self.db.execute('INSERT INTO cycles VALUES(?,?,?)',
                            (request, hashlib.sha256(dump(self.c).encode()).hexdigest(), dump(body)))
            return body

    def changed(self):
        raw = self.command('A', ['status', '--porcelain=v1', '-z', '--untracked-files=all'])
        fields = raw.split(b'\0'); paths = set(); i = 0
        while i < len(fields) and fields[i]:
            field = fields[i]; i += 1
            if len(field) < 4 or b'U' in field[:2] or field[:2] in {b'AA', b'DD'}:
                raise Blocked('UNMERGED_INDEX')
            paths.add(os.fsdecode(field[3:]))
            if b'R' in field[:2] or b'C' in field[:2]:
                if i >= len(fields) or not fields[i]: raise Blocked('INVALID_RENAME')
                paths.add(os.fsdecode(fields[i])); i += 1
        for path in paths:
            if not any(path == p or path.startswith(p.rstrip('/')+'/') for p in self.c['scope']):
                raise Blocked('OUT_OF_SCOPE_CHANGES')
            if not (self.roots['A']/path).resolve().is_relative_to(self.roots['A']):
                raise Blocked('PATH_ESCAPES_WORKSPACE')
        return sorted(paths)

    def snapshot(self, paths):
        h = hashlib.sha256()
        for path in paths:
            p = self.roots['A']/path
            h.update(os.fsencode(path)); h.update(b'\0')
            if p.is_symlink(): h.update(b'link'+os.fsencode(os.readlink(p)))
            elif p.is_file(): h.update(b'file'+p.read_bytes()+str(p.stat().st_mode).encode())
            elif not p.exists(): h.update(b'deleted')
            else: raise Blocked('SUBMODULE_OR_DIRECTORY_CHANGE_REQUIRES_REVIEW')
        return h.hexdigest()

    def checks(self, role):
        results = []
        for argv in self.c['checks']:
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                raise Blocked('INVALID_CHECK_COMMAND')
            self.command(role, argv, git=False)
            results.append({'command_sha256': hashlib.sha256(dump(argv).encode()).hexdigest(), 'exit': 0, 'status': 'PASS'})
        return results

    def push(self, body):
        if (self.c['allow_push'] is not True or self.c['branch_policy_verified'] is not True
                or (self.c['branch'] in self.c['protected_branches'] and self.c['protected_push_authorized'] is not True)):
            raise Blocked('PUSH_NOT_AUTHORIZED_FOR_BRANCH')
        self.validate_repo('A'); self.clean('A'); self.fetch('A')
        remote = self.g('A', 'rev-parse', self.remote_ref())
        if self.g('A', 'rev-parse', 'HEAD') != body['head']:
            raise Blocked('HEAD_CHANGED')
        if remote == body['head']:
            # Coupure après réception possible : aucun nouveau commit ou push.
            pass
        elif remote == body['base']:
            if body.get('transport_uncertain'):
                raise Blocked('VERIFY_TRANSPORT_STOPPED_BEFORE_RETRY')
            body['phase'] = 'PUSHING'; self.save(body)
            self.g('A', 'push', self.c['remote'], f"HEAD:refs/heads/{self.c['branch']}")
        else:
            raise Blocked('REMOTE_ADVANCED_STOP_BEFORE_PUSH')
        # Vérification indépendante de la ref distante, même si le push dit exit0.
        advertised = self.g('A', 'ls-remote', '--exit-code', self.c['remote'], f"refs/heads/{self.c['branch']}").split()
        if not advertised or advertised[0] != body['head']:
            raise Blocked('PUBLISHED_SHA_NOT_CONFIRMED')
        if body['head'] not in body['published']: body['published'].append(body['head'])
        body['phase'] = 'REVIEW_PENDING'; body.pop('blocked', None)
        body.pop('transport_uncertain', None); self.save(body)

    def review_snapshot(self, head, base):
        if not all(re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', value) for value in [head, base]):
            raise Blocked('INVALID_SHA')
        self.synchronize('B', head)
        if self.g('B', 'merge-base', base, head) != base:
            raise Blocked('REVIEW_BASE_NOT_ANCESTOR')
        diff = self.command('B', ['diff', '--binary', base, head, '--'])
        checks = self.checks('B')
        self.clean('B')
        if self.g('B', 'rev-parse', 'HEAD') != head:
            raise Blocked('REVIEW_HEAD_CHANGED')
        return {'head': head, 'base': base, 'diff_sha256': hashlib.sha256(diff).hexdigest(), 'checks': checks}

    def adapter(self, role, payload):
        agent = self.c['agents'].get(role, {})
        if not agent.get('enabled') or not agent.get('argv'):
            raise Blocked('AGENT_UNAVAILABLE')
        if agent.get('trusted_execution') is not True:
            raise Blocked('AGENT_ISOLATION_AND_BUDGET_NOT_VERIFIED')
        argv = agent['argv']
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise Blocked('INVALID_AGENT_COMMAND')
        raw = self.command(role, argv, git=False, data=dump(payload).encode())
        if len(raw) > 65536: raise Blocked('AGENT_RESULT_TOO_LARGE')
        try: result = json.loads(raw)
        except (ValueError, UnicodeError): raise Blocked('INVALID_AGENT_RESULT')
        if not isinstance(result, dict): raise Blocked('INVALID_AGENT_RESULT')
        return result

    def review(self, body):
        b = self.c['agents'].get('B', {})
        if not b.get('enabled') or not b.get('argv'):
            body['phase'] = 'WAITING_AGENT'; self.save(body); return body
        if b.get('identity') == self.c['agents'].get('A', {}).get('identity'):
            raise Blocked('INDEPENDENT_REVIEW_REQUIRED')
        snapshot = self.review_snapshot(body['head'], body['base'])
        payload = dict(snapshot, task=body['task'], request=self.request, round=body['round'])
        body['phase'] = 'REVIEW_RUNNING'; self.save(body)
        result = self.adapter('B', payload)
        if (result.get('head') != body['head'] or result.get('verdict') not in {'APPROVED', 'FIX_REQUIRED'}
                or not isinstance(result.get('proof'), str) or not 1 <= len(result['proof']) <= 4000):
            raise Blocked('REVIEW_RESULT_NOT_BOUND_TO_CANDIDATE')
        self.clean('B')
        if self.g('B', 'rev-parse', 'HEAD') != body['head']:
            raise Blocked('REVIEW_HEAD_CHANGED')
        body['reviews'].append(dict(result, reviewer=b['identity'], adapter_kind=b.get('kind', 'unspecified'),
                                    snapshot=snapshot))
        body['phase'] = result['verdict']; body.pop('blocked', None); self.save(body)
        if body['phase'] == 'APPROVED':
            self.db.execute('DELETE FROM reservations WHERE cycle=?', (self.request,))
        return body

    def publish(self, request):
        previous = self.load(request)
        if previous['phase'] == 'APPROVED': return previous
        with self.worker(request):
            body = self.load(request)
            if body['phase'] == 'APPROVED': return body
            if body['phase'] in {'COMMITTING', 'REVIEW_RUNNING', 'AUTHOR_RUNNING'}:
                raise Blocked('INTERRUPTED_SIDE_EFFECT_REQUIRES_RECONCILIATION')
            if body['phase'] == 'A_READY':
                if self.c['allow_commit'] is not True: raise Blocked('COMMIT_NOT_AUTHORIZED')
                self.validate_repo('A')
                if self.g('A', 'rev-parse', 'HEAD') != body['base']: raise Blocked('HEAD_CHANGED')
                paths = self.changed()
                if not paths: raise Blocked('NO_AUTHORIZED_CHANGES')
                digest = self.snapshot(paths)
                body['checks'] = self.checks('A')
                if paths != self.changed() or digest != self.snapshot(paths):
                    raise Blocked('FILES_CHANGED_DURING_TESTS')
                self.fetch('A')
                if self.g('A', 'rev-parse', self.remote_ref()) != body['base']:
                    raise Blocked('REMOTE_ADVANCED_STOP_BEFORE_COMMIT')
                if self.g('A', 'rev-parse', 'HEAD') != body['base']:
                    raise Blocked('HEAD_CHANGED_DURING_TESTS')
                self.g('A', 'add', '--', *paths)
                if self.changed() != paths or self.snapshot(paths) != digest:
                    raise Blocked('FILES_CHANGED_DURING_STAGING')
                self.g('A', 'diff', '--quiet')
                if self.g('A', 'rev-parse', 'HEAD') != body['base']:
                    raise Blocked('HEAD_CHANGED_DURING_STAGING')
                tree = self.g('A', 'write-tree')
                body.update(phase='COMMITTING', planned_tree=tree); self.save(body)
                self.g('A', 'commit', '-m', f"dual({body['task']}): iteration {body['round']}")
                head = self.g('A', 'rev-parse', 'HEAD')
                if self.g('A', 'rev-parse', 'HEAD^{tree}') != tree:
                    raise Blocked('COMMITTED_TREE_DIFFERS_FROM_TESTED_TREE')
                if self.g('A', 'rev-parse', 'HEAD^') != body['base']:
                    raise Blocked('COMMITTED_PARENT_CHANGED')
                body.update(head=head, phase='COMMITTED'); self.save(body)
                self.clean('A')
            if body['phase'] in {'COMMITTED', 'PUSHING'}: self.push(body)
            if body['phase'] in {'REVIEW_PENDING', 'WAITING_AGENT'}: return self.review(body)
            return body

    def next_round(self, request):
        with self.worker(request):
            body = self.load(request)
            if body['phase'] != 'FIX_REQUIRED': raise Blocked('CORRECTION_NOT_REQUESTED')
            if body['round'] >= self.c['max_rounds']: raise Blocked('MAX_REVIEW_ROUNDS')
            base = self.synchronize('A', body['head'])
            body.update(base=base, phase='A_READY', round=body['round']+1, checks=[])
            self.save(body)
            return body

    def reconcile(self, request, expected_head, workers_stopped=False):
        """Action opérateur explicite après vérification externe de l'arrêt.

        Ne supprime aucun fichier ni changement et ne relance aucun agent.
        L'assertion workers_stopped n'est pas une preuve d'arrêt de processus.
        """
        if workers_stopped is not True:
            raise Blocked('VERIFY_WORKERS_STOPPED_BEFORE_RECONCILING')
        self.request = request
        body = self.load(request); before = dump(body)
        self.validate_repo('A')
        head = self.g('A', 'rev-parse', 'HEAD')
        if head != expected_head: raise Blocked('RECONCILIATION_SHA_MISMATCH')
        phase = body['phase']
        if phase == 'AUTHOR_RUNNING':
            if head != body['base']: raise Blocked('AGENT_COMMITTED_UNEXPECTEDLY')
            body['phase'] = 'A_READY'
        elif phase == 'COMMITTING':
            if head == body['base']:
                body['phase'] = 'A_READY'
            elif (self.g('A', 'rev-parse', 'HEAD^') == body['base']
                  and self.g('A', 'rev-parse', 'HEAD^{tree}') == body['planned_tree']):
                self.clean('A'); body.update(head=head, phase='COMMITTED')
            else: raise Blocked('COMMIT_OUTCOME_NOT_PROVEN')
        elif phase == 'REVIEW_RUNNING':
            self.clean('A')
            if head != body['head']: raise Blocked('REVIEW_HEAD_CHANGED')
            self.fetch('A')
            if self.g('A', 'rev-parse', self.remote_ref()) != head: raise Blocked('STALE_REMOTE_OR_CANDIDATE')
            body['phase'] = 'REVIEW_PENDING'
        elif phase == 'PUSHING':
            self.clean('A')
            if head != body['head']: raise Blocked('HEAD_CHANGED')
            self.fetch('A')
            remote = self.g('A', 'rev-parse', self.remote_ref())
            if remote not in {body['base'], head}: raise Blocked('STALE_REMOTE_OR_CANDIDATE')
            body['phase'] = 'COMMITTED'
        else: raise Blocked('NO_UNCERTAIN_SIDE_EFFECT_TO_RECONCILE')
        body.pop('blocked', None)
        body.pop('transport_uncertain', None)
        with self.tx():
            if dump(self.load(request)) != before: raise Blocked('CYCLE_CHANGED_DURING_RECONCILIATION')
            self.db.execute('DELETE FROM workers WHERE cycle=?', (request,))
            self.save(body)
        return body

    def cycle(self, request, task):
        body = self.begin(request, task)
        if body['phase'] in {'COMMITTING', 'REVIEW_RUNNING', 'AUTHOR_RUNNING'}:
            raise Blocked('INTERRUPTED_SIDE_EFFECT_REQUIRES_RECONCILIATION')
        if body['phase'] in {'COMMITTED', 'PUSHING', 'REVIEW_PENDING', 'WAITING_AGENT'}:
            body = self.publish(request)
        while body['phase'] in {'A_READY', 'FIX_REQUIRED'}:
            if body['phase'] == 'FIX_REQUIRED': body = self.next_round(request)
            with self.worker(request):
                # Avant CHAQUE intervention A, même si le cycle est repris.
                if self.synchronize('A', body['base']) != body['base']: raise Blocked('STALE_BASE')
                payload = dict(body, request=request)
                body['phase'] = 'AUTHOR_RUNNING'; self.save(body)
                self.adapter('A', payload)
                if self.g('A', 'rev-parse', 'HEAD') != body['base']: raise Blocked('AGENT_COMMITTED_UNEXPECTEDLY')
                body['phase'] = 'A_READY'; self.save(body)
            body = self.publish(request)
        return body

    def export(self, path):
        rows = [dict(row) for row in self.db.execute('SELECT * FROM commands ORDER BY seq')]
        Path(path).write_text(dump(rows), encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ['begin', 'cycle']:
        c = sub.add_parser(name); c.add_argument('request'); c.add_argument('task')
    for name in ['publish', 'next-round', 'status']:
        c = sub.add_parser(name); c.add_argument('request')
    c = sub.add_parser('reconcile'); c.add_argument('request'); c.add_argument('--head', required=True)
    c.add_argument('--workers-stopped', action='store_true')
    c = sub.add_parser('review-check'); c.add_argument('--head', required=True); c.add_argument('--base', required=True)
    c.add_argument('--request', required=True)
    c = sub.add_parser('export'); c.add_argument('path')
    a = p.parse_args(); flow = None
    try:
        config = json.loads(Path(a.config).read_text(encoding='utf-8'))
        flow = GitFlow(config)
        if a.command == 'begin': result = flow.begin(a.request, a.task)
        elif a.command == 'cycle': result = flow.cycle(a.request, a.task)
        elif a.command == 'publish': result = flow.publish(a.request)
        elif a.command == 'next-round': result = flow.next_round(a.request)
        elif a.command == 'reconcile': result = flow.reconcile(a.request, a.head, a.workers_stopped)
        elif a.command == 'status': result = flow.load(a.request)
        elif a.command == 'review-check':
            flow.request = a.request
            result = flow.review_snapshot(a.head, a.base)
        else: flow.export(a.path); result = {'status': 'EXPORTED'}
        print(dump(result))
    except Blocked as error:
        print(dump({'status': 'BLOCKED', 'reason': error.code})); return 2
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print(dump({'status': 'BLOCKED', 'reason': 'CONFIG_OR_STORAGE_ERROR'})); return 2
    finally:
        if flow: flow.close()
    return 0


if __name__ == '__main__': raise SystemExit(main())
