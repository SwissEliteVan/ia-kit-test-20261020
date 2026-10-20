"""Adaptateur de boîte de réception vers un dispatcher de session autorisé.

Ne réveille PAS une IA à lui seul. Le dispatcher (ici la session principale)
observe request.json, déclenche réellement B et écrit la réponse. Sa confiance
repose sur les ACL du contrôle et sur le dispatcher, sans signature cryptographique.
En absence de dispatcher : timeout, jamais APPROVED fictif.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mailbox', required=True)
    p.add_argument('--timeout', type=int, default=240)
    a = p.parse_args()
    body = json.load(sys.stdin)
    key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    mailbox = Path(a.mailbox).resolve()/key
    mailbox.mkdir(parents=True, exist_ok=True)
    request = mailbox/'request.json'
    try:
        with request.open('x', encoding='utf-8') as f: json.dump(body, f)
    except FileExistsError:
        if json.loads(request.read_text(encoding='utf-8')) != body:
            raise SystemExit('mailbox collision')
    deadline = time.monotonic()+a.timeout
    response = mailbox/'response.json'
    while time.monotonic() < deadline:
        if response.exists():
            result = json.loads(response.read_text(encoding='utf-8'))
            if result.get('head') != body['head']: raise SystemExit('stale response')
            print(json.dumps(result)); return
        time.sleep(0.2)
    raise SystemExit('session dispatcher unavailable or timed out')


if __name__ == '__main__': main()
