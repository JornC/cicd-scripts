"""Controllable HTTP service/rollout fixture, not an ECS emulator."""
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MEMBERS = ['UK-CALCULATOR-DEV', 'UK-ARCHIVE-DEV',
           'UK-CALCULATOR-NI-DEV', 'UK-ARCHIVE-NI-DEV']
state = {name: dict(up=True, stable=True, image='v1', desired=1, running=1) for name in MEMBERS}
events = []
sessions = {}
mutex = threading.RLock()
PORTS = {name: 9101 + i for i, name in enumerate(MEMBERS)}


def peer(name):
    return MEMBERS[MEMBERS.index(name) ^ 1]


class SkeletonHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        name = self.server.environment
        with mutex:
            snapshot = dict(state[name])
        code = 200 if snapshot['up'] else 503
        body = dict(service=name, image=snapshot['image'])
        if code == 200 and self.path in ['/export', '/upload', '/summary']:
            # Calculator -> Archive, or Archive -> Calculator -> Archive.
            endpoint = '/summary' if self.path == '/upload' else '/lookup'
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{PORTS[peer(name)]}{endpoint}', timeout=5) as response:
                    body['peer'] = json.load(response)
            except urllib.error.HTTPError as exc:
                code = 502
                body['dependency_status'] = exc.code
        with mutex:
            event('http_request', service=name, path=self.path, status=code)
        data=json.dumps(body).encode()
        self.send_response(code); self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


def check_qa(test):
    with mutex:
        session = dict(sessions[test])
    target = session['target']
    path = '/export' if 'CALCULATOR' in target else '/upload'
    ok = False
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORTS[target]}{path}', timeout=10) as response:
            body=json.load(response)
        ok=True
        while body:
            ok = ok and body['image'] == session['images'][body['service']]
            body=body.get('peer')
    except urllib.error.HTTPError:
        pass
    with mutex:
        event('qa_check',test=test,passed=ok)
    return ok


def event(kind, **data):
    events.append(dict(index=len(events), time=time.time(), kind=kind, **data))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        args = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        code, body = 200, {'ok': True}
        if parsed.path == '/qa/check':
            code = 200 if check_qa(args['test']) else 503
            self.send_response(code);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')
            return
        with mutex:
            name = args.get('env')
            test = args.get('test', '')
            if parsed.path == '/state':
                body = dict(services=state, events=events, sessions=sessions)
            elif parsed.path == '/reset-events':
                events.clear(); sessions.clear()
            elif parsed.path == '/ready':
                names = args['members'].split(',')
                expected = args.get('images', '').split(',')
                ok = all(state[n]['up'] and state[n]['stable'] and state[n]['desired'] > 0
                         and state[n]['running'] == state[n]['desired']
                         and (not expected or not expected[i] or state[n]['image'] == expected[i])
                         for i, n in enumerate(names))
                code = 200 if ok else 503
            elif parsed.path == '/deploy/start':
                state[name].update(up=args.get('oldHealthy') == 'true', stable=False,
                                   desired=1, running=1, next_image=args['image'], next_revision=int(args.get('revision', '1')))
                event('deploy_start', env=name, test=test, image=args['image'])
            elif parsed.path == '/deploy/complete':
                state[name].update(up=True, stable=True, desired=1, running=1,
                                   image=state[name].get('next_image', args.get('image', 'v1')), revision=state[name].get('next_revision', 1))
                event('deploy_complete', env=name, test=test, image=state[name]['image'])
            elif parsed.path == '/outside-stop':
                state[name].update(up=False, stable=True, desired=0, running=0)
                event('outside_stop', env=name, test=test)
            elif parsed.path == '/qa/begin':
                members = args['members'].split(',')
                sessions[test] = dict(target=args['target'],members=members, images={n: state[n]['image'] for n in members})
                event('qa_begin', test=test, members=members)
            elif parsed.path == '/qa/end':
                event('qa_end', test=test)
            else:
                code = 404
            data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        host = self.headers.get('Host', '').split(':')[0]
        name = 'UK-' + host.removesuffix('.aerius.uk').upper()
        with mutex:
            ready = name in state and state[name]['up']
        payload = json.dumps({'status': 'UP' if ready else 'DOWN'}).encode()
        self.send_response(200 if ready else 503)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


health = ThreadingHTTPServer(('0.0.0.0', 443), HealthHandler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/harness/ca.pem', '/harness/key.pem')
health.socket = context.wrap_socket(health.socket, server_side=True)
threading.Thread(target=health.serve_forever, daemon=True).start()


for name, port in PORTS.items():
    server=ThreadingHTTPServer(('127.0.0.1',port),SkeletonHandler)
    server.environment=name
    threading.Thread(target=server.serve_forever,daemon=True).start()
ThreadingHTTPServer(('0.0.0.0', 9000), Handler).serve_forever()
