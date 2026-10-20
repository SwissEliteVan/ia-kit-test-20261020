"""Adaptateurs HTTP officiels, sans SDK ni outils exécutables fournis aux modèles.

Les clés sont uniquement des variables d'environnement ; aucun abonnement de chat
n'est utilisé. La facturation reste celle du fournisseur choisi par l'opérateur.
"""
import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import time
import threading
from datetime import date
from urllib import error, parse, request

from gitflow import Blocked, dump


class ProviderError(Blocked):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.retry_after = retry_after


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError('API_REDIRECT_REFUSED')


class Adapter:
    base = None
    path = None
    def __init__(self, config, test_mode=False):
        self.c = config
        self.test = test_mode
        self.url = self.base
        if config.get('test_endpoint'):
            url = parse.urlsplit(config['test_endpoint'])
            try: local = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError: local = False
            if not test_mode or url.scheme != 'http' or not local or url.username or url.query or url.path:
                raise Blocked('UNTRUSTED_API_ENDPOINT')
            self.url = config['test_endpoint'].rstrip('/')
        if not config.get('model') or not isinstance(config['model'], str):
            raise Blocked('MODEL_NOT_CONFIGURED')
        if self.test and not config.get('test_endpoint'):
            raise Blocked('TEST_MODE_REQUIRES_LOOPBACK_ENDPOINT')
        if not self.test:
            name = config.get('auth_env', '')
            if not re.fullmatch('[A-Z][A-Z0-9_]*', name): raise Blocked('AUTH_ENV_NOT_CONFIGURED')
            if not os.environ.get(name): raise Blocked('API_KEY_ABSENT')

    def headers(self):
        h = {'Content-Type': 'application/json'}
        if not self.test: h['Authorization'] = 'Bearer '+os.environ[self.c['auth_env']]
        return h

    def http(self, path, body=None):
        # Le timeout socket seul ne borne pas une réponse envoyée au compte-gouttes.
        # Le thread ne touche jamais SQLite ; un résultat tardif reste incertain.
        result = queue.Queue(maxsize=1)
        def work():
            try: result.put((True, self._http(path, body)))
            except Exception as e: result.put((False, e))
        threading.Thread(target=work, daemon=True).start()
        try: ok, value = result.get(timeout=self.c['timeout_seconds'])
        except queue.Empty: raise ProviderError('API_OUTCOME_UNCERTAIN') from None
        if not ok: raise value
        return value

    def _http(self, path, body=None):
        raw = None if body is None else dump(body).encode()
        req = request.Request(self.url+path, data=raw, headers=self.headers(), method='GET' if body is None else 'POST')
        opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=self.c['timeout_seconds']) as response:
                content = response.read(1024*1024+1)
            if len(content) > 1024*1024: raise ProviderError('API_RESPONSE_TOO_LARGE')
            return json.loads(content)
        except error.HTTPError as e:
            code = {401: 'AUTH_REJECTED', 403: 'AUTH_FORBIDDEN', 429: 'RATE_LIMIT',
                    503: 'API_UNAVAILABLE'}.get(e.code, 'API_HTTP_ERROR')
            try: delay = min(30, max(0, int(e.headers.get('Retry-After', '1'))))
            except ValueError: delay = 1
            raise ProviderError(code, delay) from None
        except (error.URLError, TimeoutError, socket.timeout, ConnectionError):
            raise ProviderError('API_OUTCOME_UNCERTAIN') from None
        except (ValueError, UnicodeError):
            raise ProviderError('API_INVALID_RESPONSE') from None

    def probe(self):
        data = self.http('/v1/models' if self.c['provider'] != 'deepseek' else '/models')
        ids = [entry.get('id') for entry in data.get('data', [])]
        if self.c['model'] not in ids: raise Blocked('MODEL_NOT_AVAILABLE_TO_CREDENTIAL')
        return {'authentication': 'PASS', 'model_listed': True,
                'inference_capability': 'NOT_RUN', 'provider': self.c['provider']}


class OpenAI(Adapter):
    base = 'https://api.openai.com'
    path = '/v1/responses'
    def body(self, system, user):
        return {'model': self.c['model'], 'instructions': system, 'input': user,
                'max_output_tokens': self.c['max_output_tokens'], 'store': False}

    def unpack(self, data):
        if data.get('status') != 'completed': raise ProviderError('API_INCOMPLETE_RESULT')
        text = ''.join(c.get('text', '') for item in data.get('output', [])
                       if item.get('type') == 'message' for c in item.get('content', [])
                       if c.get('type') == 'output_text')
        u = data['usage']
        return text, u['input_tokens'], u['output_tokens'], data.get('id')


class Anthropic(Adapter):
    base = 'https://api.anthropic.com'
    path = '/v1/messages'
    def headers(self):
        h = {'Content-Type': 'application/json', 'anthropic-version': '2023-06-01'}
        if not self.test: h['x-api-key'] = os.environ[self.c['auth_env']]
        return h

    def body(self, system, user):
        return {'model': self.c['model'], 'system': system,
                'messages': [{'role': 'user', 'content': user}],
                'max_tokens': self.c['max_output_tokens']}

    def unpack(self, data):
        if data.get('stop_reason') != 'end_turn': raise ProviderError('API_INCOMPLETE_RESULT')
        text = ''.join(c['text'] for c in data['content'] if c['type'] == 'text')
        u = data['usage']
        # Aucun cache_control envoyé : une surcharge inattendue bloque le budget.
        if u.get('cache_creation_input_tokens', 0): raise ProviderError('UNEXPECTED_CACHE_CHARGE')
        return text, u['input_tokens']+u.get('cache_read_input_tokens', 0), u['output_tokens'], data.get('id')


class DeepSeek(Adapter):
    base = 'https://api.deepseek.com'
    path = '/chat/completions'
    def body(self, system, user):
        return {'model': self.c['model'], 'messages': [{'role': 'system', 'content': system},
                {'role': 'user', 'content': user}], 'max_tokens': self.c['max_output_tokens'],
                'stream': False, 'thinking': {'type': 'disabled'}}

    def unpack(self, data):
        choice = data['choices'][0]
        if choice.get('finish_reason') != 'stop': raise ProviderError('API_INCOMPLETE_RESULT')
        return choice['message']['content'], data['usage']['prompt_tokens'], data['usage']['completion_tokens'], data.get('id')


ADAPTERS = {'openai': OpenAI, 'anthropic': Anthropic, 'deepseek': DeepSeek}


def register(name, cls):
    """Extension par du code opérateur de confiance, jamais par un événement."""
    if name in ADAPTERS or not issubclass(cls, Adapter): raise ValueError('adapter registration rejected')
    ADAPTERS[name] = cls


class Client:
    def __init__(self, store, config, stopped=lambda: False):
        self.s, self.c, self.stopped = store, config, stopped
        store.db.executescript('''CREATE TABLE IF NOT EXISTS api_calls(
          id TEXT PRIMARY KEY, task TEXT, role TEXT, fingerprint TEXT, status TEXT,
          reserved INTEGER, charged INTEGER, result TEXT, meta TEXT, next_at REAL);
        ''')

    def profile(self, role):
        p = self.c['providers'][role]
        if p.get('provider') not in ADAPTERS: raise Blocked('PROVIDER_NOT_REGISTERED')
        for k in ['context_tokens', 'max_output_tokens', 'input_micro_usd_per_million', 'output_micro_usd_per_million', 'timeout_seconds']:
            if type(p.get(k)) is not int or p[k] <= 0: raise Blocked('MODEL_LIMITS_OR_PRICES_NOT_CONFIGURED')
        if p['max_output_tokens'] > p['context_tokens'] or p['timeout_seconds'] > 120:
            raise Blocked('INVALID_MODEL_LIMITS')
        if not self.c.get('test_mode'):
            try: age = (date.today()-date.fromisoformat(p['pricing_verified_at'])).days
            except (ValueError, KeyError): raise Blocked('PRICING_VERIFICATION_REQUIRED')
            if not 0 <= age <= 7 or p.get('limits_verified') is not True:
                raise Blocked('PRICING_OR_LIMITS_STALE')
        return p

    def granted(self):
        grant = self.c['spending']
        if (grant.get('enabled') is not True or not grant.get('authorization')
                or self.c.get('data_egress_authorized') is not True):
            raise Blocked('API_SPENDING_OR_DATA_EGRESS_NOT_AUTHORIZED')
        for field in ['total_micro_usd', 'task_micro_usd', 'role_micro_usd', 'max_calls', 'max_attempts']:
            if type(grant.get(field)) is not int or grant[field] <= 0:
                raise Blocked('API_BUDGET_NOT_CONFIGURED')
        if grant['max_attempts'] > 3: raise Blocked('API_RETRIES_TOO_HIGH')
        return grant

    def reserve(self, key, task, role, fingerprint, cap):
        g = self.granted()
        with self.s.transaction():
            if self.stopped(): raise Blocked('EMERGENCY_STOP')
            row = self.s.db.execute('SELECT * FROM api_calls WHERE id=?', (key,)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint: raise Blocked('API_EVENT_PAYLOAD_CHANGED')
                if row['status'] == 'DONE': return json.loads(row['result'])
                if row['status'] == 'REJECTED': raise ProviderError('KNOWN_REJECTION', max(0, row['next_at']-time.time()))
                raise Blocked('API_OUTCOME_UNCERTAIN_RECONCILE_REQUIRED')
            if self.s.db.execute("SELECT 1 FROM api_calls WHERE status IN ('STARTED','UNCERTAIN','FROZEN')").fetchone():
                raise Blocked('UNSETTLED_API_CALL_BLOCKS_SPENDING')
            rows = self.s.db.execute('SELECT task,role,charged FROM api_calls').fetchall()
            if (len(rows) >= g['max_calls'] or sum(r['charged'] for r in rows)+cap > g['total_micro_usd']
                    or sum(r['charged'] for r in rows if r['task'] == task)+cap > g['task_micro_usd']
                    or sum(r['charged'] for r in rows if r['role'] == role)+cap > g['role_micro_usd']):
                raise Blocked('API_BUDGET_EXHAUSTED')
            self.s.db.execute('INSERT INTO api_calls VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (key, task, role, fingerprint, 'STARTED', cap, cap, None, '{}', 0))
        return None

    def complete(self, key, result, p, reserved):
        it, ot = result['input_tokens'], result['output_tokens']
        if (type(it) is not int or type(ot) is not int or min(it, ot) < 0
                or it > p['context_tokens'] or ot > p['max_output_tokens']):
            self.s.db.execute("UPDATE api_calls SET status='FROZEN' WHERE id=?", (key,))
            raise Blocked('API_USAGE_EXCEEDS_VERIFIED_LIMITS')
        charge = (it*p['input_micro_usd_per_million']+ot*p['output_micro_usd_per_million']+999999)//1000000
        if charge > reserved: raise Blocked('API_COST_CEILING_BREACHED')
        with self.s.transaction():
            self.s.db.execute("UPDATE api_calls SET status='DONE',charged=?,result=?,meta=? WHERE id=?",
                             (charge, dump(result), dump({k: v for k, v in result.items() if k != 'value'}), key))

    def call(self, task, role, step, system, context):
        p = self.profile(role); grant = self.granted()
        adapter = ADAPTERS[p['provider']](p, self.c.get('test_mode', False))
        user = dump(context)
        if len(user.encode())+len(system.encode()) > self.c['max_context_bytes']:
            raise Blocked('CONTEXT_TOO_LARGE')
        # Réserve tout le contexte vérifié et le maximum de sortie, pas une
        # estimation optimiste du tokenizer. Aucune fonction/outillage facturé.
        cap = (p['context_tokens']*p['input_micro_usd_per_million']+
               p['max_output_tokens']*p['output_micro_usd_per_million']+999999)//1000000
        fingerprint = hashlib.sha256(dump([p, system, context]).encode()).hexdigest()
        for attempt in range(grant['max_attempts']):
            key = f'{task}:{step}:{role}:{attempt}'
            try:
                cached = self.reserve(key, task, role, fingerprint, cap)
            except ProviderError as e:
                if e.retry_after: time.sleep(min(e.retry_after, 30))
                continue
            if cached is not None: return cached
            try:
                data = adapter.http(adapter.path, adapter.body(system, user))
                text, it, ot, provider_id = adapter.unpack(data)
                secret = os.environ.get(p.get('auth_env', ''), '')
                if secret and secret in text: raise ProviderError('SECRET_IN_PROVIDER_OUTPUT')
                value = json.loads(text)
                if not isinstance(value, dict): raise ProviderError('API_SCHEMA_INVALID')
                if not self.c.get('test_mode') and not isinstance(provider_id, str):
                    raise ProviderError('REAL_PROVIDER_RECEIPT_ABSENT')
                result = {'value': value, 'input_tokens': it, 'output_tokens': ot,
                          'provider_id': provider_id, 'provider': p['provider'],
                          'model': p['model'], 'evidence': 'LOCAL_HTTP_FIXTURE' if adapter.test else 'REAL_PROVIDER_API'}
                self.complete(key, result, p, cap)
                return result
            except (ProviderError, ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
                known = isinstance(e, ProviderError) and e.code in {'RATE_LIMIT', 'API_UNAVAILABLE', 'AUTH_REJECTED', 'AUTH_FORBIDDEN', 'API_HTTP_ERROR'}
                state = 'REJECTED' if known else 'UNCERTAIN'
                delay = e.retry_after if isinstance(e, ProviderError) else 0
                self.s.db.execute("UPDATE api_calls SET status=?,meta=?,next_at=? WHERE id=? AND status='STARTED'",
                                 (state, dump({'reason': e.code if isinstance(e, ProviderError) else 'API_PROTOCOL_INVALID'}), time.time()+delay, key))
                if known and e.code in {'RATE_LIMIT', 'API_UNAVAILABLE'}:
                    if delay: time.sleep(min(delay, 30))
                    continue
                raise Blocked(e.code if isinstance(e, ProviderError) else 'API_PROTOCOL_INVALID') from None
        raise Blocked('API_RETRY_LIMIT')

    def doctor(self, probe=False):
        output = {}
        for role in ['A', 'B']:
            p = self.c['providers'][role]
            output[role] = {'provider': p.get('provider'), 'key_present': bool(os.environ.get(p.get('auth_env', ''))),
                            'authentication': 'NOT_RUN', 'inference_capability': 'NOT_RUN'}
            if probe:
                if not self.c.get('allow_auth_probe'): raise Blocked('AUTH_PROBE_NOT_AUTHORIZED')
                self.granted()
                output[role].update(ADAPTERS[p['provider']](self.profile(role), self.c.get('test_mode', False)).probe())
        return output
