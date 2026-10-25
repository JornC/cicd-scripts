#!/usr/bin/env python3
"""Run the actual shared library with skeleton services on an isolated Jenkins."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--work-dir', type=Path, required=True)
parser.add_argument('--keep', action='store_true', help='Keep only these test containers for debugging')
args = parser.parse_args()
root = args.work_dir.resolve()
source = Path(__file__).resolve().parent
repo = source.parents[2]
network = 'qa-pair-implementation'
containers = ['qa-pair-implementation-fixture', 'qa-pair-implementation-jenkins']
image = 'qa-pair-implementation:jenkins-2.555.3'


def run(*command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


for kind, name in [('network', network)] + [('container', name) for name in containers]:
    if subprocess.run(['docker', kind, 'inspect', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise SystemExit('Refusing to reuse existing ' + name)
root.mkdir(mode=0o755)
for path in source.iterdir():
    if path.is_file(): shutil.copyfile(path, root / path.name)
(root / 'results').mkdir()
(root / 'bin').mkdir()
shutil.copyfile(root / 'fake_aws.py', root / 'bin/aws')
(root / 'bin/aws').chmod(0o755)
implementation = root / 'implementation'
implementation.mkdir()
for name in ['qa', 'common', 'jenkins-library']:
    shutil.copytree(repo / name, implementation / name, ignore=shutil.ignore_patterns('__pycache__'))
(root / 'results/source-hashes.json').write_text(json.dumps({str(p.relative_to(implementation)):
    hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(implementation.rglob('*')) if p.is_file()}, indent=2))
(implementation / 'job').mkdir()
for name in ['get_build_name.sh', 'notify_mattermost_message.sh']:
    path = implementation / 'job' / name
    path.write_text('#!/bin/sh\nprintf "local fixture"\n')
    path.chmod(0o755)
library = root / 'library-source'
shutil.copytree(implementation / 'jenkins-library', library)
stubs = {
    'buildName': 'def call(Object value) { echo "TEST_BUILD_NAME: ${value}" }',
    'withBuildUser': 'def call(Closure body) { body() }',
    'mattermostSend': 'def call(Map config) { echo "TEST_NOTIFICATION_CAPTURED" }',
    'cicdGetTestStatusMessage': 'def call(boolean publish) { return "" }',
    'cicdPipelineProcessPreJobWebhooks': 'def call(Object... args) { echo "TEST_PRE_WEBHOOK_CAPTURED" }',
    'cicdPipelineProcessPostJobWebhooks': 'def call(Object... args) { echo "TEST_POST_WEBHOOK_CAPTURED" }',
    'cicdPipelineProcessPostJobBuild': 'def call(Map config) { echo "TEST_BUILD_POST_CAPTURED" }'
}
for name, body in stubs.items(): (library / 'vars' / (name + '.groovy')).write_text(body + '\n')
run('git', 'init', '-b', 'main', str(library), stdout=subprocess.DEVNULL)
run('git', '-C', str(library), 'add', '.')
run('git', '-C', str(library), '-c', 'user.name=qa-pair-fixture', '-c', 'user.email=fixture@invalid',
    'commit', '-m', 'local shared library fixture', stdout=subprocess.DEVNULL)
run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
    '-keyout', str(root / 'key.pem'), '-out', str(root / 'ca.pem'), '-subj', '/CN=*.aerius.uk',
    '-addext', 'subjectAltName=DNS:*.aerius.uk', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
run('docker', 'build', '-t', image, str(source))
created = []
created_network = False


def driver(script, *arguments):
    run('docker', 'run', '--rm', '--network', network, '--user', f'{os.getuid()}:{os.getgid()}',
        '-e', 'PYTHONDONTWRITEBYTECODE=1',
        '-e', 'REPRO_JENKINS=http://qa-pair-implementation-jenkins:8080',
        '-e', 'REPRO_FIXTURE=http://fixture:9000',
        '--mount', f'type=bind,src={root},dst=/harness',
        'python:3.12-slim', 'python', '-u', '/harness/' + script, *arguments)


try:
    run('docker', 'network', 'create', '--internal', network)
    created_network = True
    aliases = ['--network-alias', 'fixture']
    for host in ['calculator-dev', 'archive-dev', 'calculator-ni-dev', 'archive-ni-dev']:
        aliases += ['--network-alias', host + '.aerius.uk']
    run('docker', 'run', '-d', '--name', containers[0], '--network', network, *aliases,
        '--mount', f'type=bind,src={root},dst=/harness,readonly',
        'python:3.12-slim', 'python', '/harness/fixture.py')
    created.append(containers[0])
    run('docker', 'run', '-d', '--name', containers[1], '--network', network,
        '-e', 'JAVA_OPTS=-Djenkins.install.runSetupWizard=false -Dhudson.plugins.git.GitSCM.ALLOW_LOCAL_CHECKOUT=true',
        '--mount', f'type=bind,src={root},dst=/harness,readonly',
        '--mount', f'type=bind,src={implementation},dst=/implementation,readonly',
        '--mount', f'type=bind,src={root}/init.groovy,dst=/usr/share/jenkins/ref/init.groovy.d/01-test.groovy,readonly', image)
    created.append(containers[1])
    driver('run.py')
    driver('edge_cases.py')
    driver('extra_cases.py')
    for mode in ['graceful', 'hard']:
        driver('restart_case.py', 'prepare')
        if mode == 'hard':
            run('docker', 'kill', '--signal', 'KILL', containers[1])
            run('docker', 'start', containers[1])
        else:
            run('docker', 'restart', '--timeout', '15', containers[1])
        driver('restart_case.py', 'verify', mode + '-restart')
    report = json.loads((root / 'results/report.json').read_text())
    assert all(case['passed'] for case in report)
    print(f'PASS: {len(report)} production-library integration cases', flush=True)
finally:
    if not args.keep:
        for name in reversed(created):
            subprocess.run(['docker', 'rm', '--force', '--volumes', name], check=False)
        if created_network:
            subprocess.run(['docker', 'network', 'rm', network], check=False)
    print('Local evidence:', root / 'results', flush=True)
