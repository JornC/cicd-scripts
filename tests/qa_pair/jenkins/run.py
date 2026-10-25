"""Drive only the localhost reproduction; never contacts production Jenkins."""
import base64
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).parent
JENKINS = os.environ.get('REPRO_JENKINS', 'http://qa-pair-implementation-jenkins:8080')
FIXTURE = os.environ.get('REPRO_FIXTURE', 'http://fixture:9000')
assert JENKINS == 'http://qa-pair-implementation-jenkins:8080'
assert FIXTURE == 'http://fixture:9000'
MEMBERS = ['UK-CALCULATOR-DEV', 'UK-ARCHIVE-DEV', 'UK-CALCULATOR-NI-DEV', 'UK-ARCHIVE-NI-DEV']
DEFAULTS = dict(KIND='qa', SOURCE_JOB_NAME=MEMBERS[0], DEPLOY_IMAGE_TAG='v1', TEST_ID='unnamed',
                MODE='fixed', HOLD='false', COMPLETE='true', FAIL='false', TRIGGER_QA='false',
                OLD_HEALTHY='false', PEER_HOST='', PRE_HOLD='false', MANIFEST_MODE='', SMOKE='false',
                DEPLOY_TERRAFORM_ACTION='apply', ROLLOUT_SECONDS='45', QA_SECONDS='90', AWS_ACCOUNT_NAME='UK-DEV', AERIUS_REGISTRY_URL='123456789012.dkr.ecr.eu-west-2.amazonaws.com/app', SOURCE_JOB_BUILD_NUMBER='1', ENVIRONMENT_NAME='')
results = []


def request(base, path, data=None, raw=False, timeout=20):
    payload = urllib.parse.urlencode(data).encode() if data is not None else None
    with urllib.request.urlopen(urllib.request.Request(base + path, data=payload), timeout=timeout) as r:
        b = r.read()
        return b.decode() if raw else (json.loads(b) if b else {})


def wait(fn, description, seconds=60):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result: return result
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
        time.sleep(0.25)
    raise AssertionError(f'Timed out: {description}; last={last}')


def fixture(path, **params):
    return request(FIXTURE, path + '?' + urllib.parse.urlencode(params))


def events(kind=None, test=None):
    es = fixture('/state')['events']
    return [e for e in es if (kind is None or e['kind'] == kind) and (test is None or e.get('test') == test)]


def install():
    wait(lambda: request(JENKINS, '/api/json'), 'Jenkins startup', 90)
    script = (ROOT / 'jobs.groovy').read_text()
    encoded = base64.b64encode(script.encode()).decode()
    definitions = ','.join('new hudson.model.StringParameterDefinition(%s,%s)' % (json.dumps(k), json.dumps(v))
                           for k, v in DEFAULTS.items())
    groovy = f'''
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition
import org.jenkinsci.plugins.scriptsecurity.scripts.ScriptApproval
import org.jenkinsci.plugins.scriptsecurity.scripts.languages.GroovyLanguage
def script = new String(Base64.decoder.decode('{encoded}'), 'UTF-8')
ScriptApproval.get().preapprove(script, GroovyLanguage.get())
for (name in ['QA-GENERIC','DEPLOY-OTA-ENVIRONMENT'] + ['QA-UK-CALCULATOR-DEV', 'QA-UK-ARCHIVE-DEV', 'QA-UK-CALCULATOR-NI-DEV', 'QA-UK-ARCHIVE-NI-DEV']) {{
  def job = Jenkins.get().getItem(name) ?: Jenkins.get().createProject(WorkflowJob, name)
  while (job.getProperty(hudson.model.ParametersDefinitionProperty) != null) {{
    job.removeProperty(hudson.model.ParametersDefinitionProperty)
  }}
  job.addProperty(new hudson.model.ParametersDefinitionProperty([{definitions}]))
  job.setDefinition(new CpsFlowDefinition(script, false))
  job.save()
}}
println('INSTALLED')
'''
    output = request(JENKINS, '/scriptText', {'script': groovy}, raw=True)
    assert 'INSTALLED' in output, output
    plugins = request(JENKINS, '/pluginManager/api/json?tree=plugins[shortName,version]')
    (ROOT / 'results/plugins.json').write_text(json.dumps(plugins, indent=2))


def build(kind, target, test, job=None, **params):
    name = job or ('DEPLOY-OTA-ENVIRONMENT' if kind == 'deploy' else 'QA-GENERIC')
    number = request(JENKINS, f'/job/{name}/api/json?tree=nextBuildNumber')['nextBuildNumber']
    parameters = DEFAULTS | dict(KIND=kind, SOURCE_JOB_NAME=target, TEST_ID=test) | {k: str(v) for k, v in params.items()}
    request(JENKINS, f'/job/{name}/buildWithParameters', parameters, raw=True)
    path = f'/job/{name}/{number}'
    wait(lambda: request(JENKINS, path + '/api/json'), 'build starts ' + path)
    return path


def log(path):
    return request(JENKINS, path + '/consoleText', raw=True)


def at_gate(path, gate):
    # The input action itself is present before Jenkins writes its console message.
    gate = gate[0].upper() + gate[1:]
    return wait(lambda: request(JENKINS, path + '/input/' + gate + '/', raw=True), 'input gate ' + path)


def proceed(path, gate):
    gate = gate[0].upper() + gate[1:]
    request(JENKINS, path + '/input/' + gate + '/proceedEmpty', {}, raw=True)


def finish(path, expected='SUCCESS', contains=None):
    status = wait(lambda: (lambda s: s if not s['building'] else None)(request(JENKINS, path + '/api/json?tree=result,building')),
                  'build completes ' + path, 100)
    text = log(path)
    (ROOT / 'results' / (path.replace('/job/', '').replace('/', '-') + '.log')).write_text(text)
    assert status['result'] == expected, (path, status, text[-3500:])
    if contains: assert contains in text, (path, contains, text[-2500:])
    return status


def queued(path):
    wait(lambda: 'is locked' in log(path) or 'waiting for' in log(path).lower(), 'job waits for pair ' + path)


def seed(targets=MEMBERS):
    for target in targets:
        p = build('deploy', target, 'seed-' + target)
        def ready():
            status=request(JENKINS,p+'/api/json?tree=building')
            if not status['building']: return True
            try:
                request(JENKINS,p+'/input/Qa_pair_reconcile/',raw=True)
                proceed(p,'qa_pair_reconcile')
            except urllib.error.HTTPError as e:
                if e.code!=404: raise
            return False
        wait(ready,'seed/reconcile controlled fixture '+target)
        finish(p)


def run_case(name, fn):
    fixture('/reset-events')
    start = time.monotonic()
    try:
        fn()
        result = dict(name=name, passed=True, seconds=round(time.monotonic()-start, 2), events=events())
    except Exception as exc:
        result = dict(name=name, passed=False, seconds=round(time.monotonic()-start, 2), error=str(exc), events=events())
        results.append(result)
        (ROOT/'results/report.json').write_text(json.dumps(results, indent=2))
        print(json.dumps({k:v for k,v in result.items() if k!='events'}), flush=True)
        raise
    results.append(result)
    (ROOT/'results/report.json').write_text(json.dumps(results, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='events'}), flush=True)


def overlap(mode, qa_target, deploy_target, order='qa-first'):
    label = f'{mode}-{qa_target}-{deploy_target}-{order}'
    if order == 'qa-first':
        q = build('qa', qa_target, label, MODE=mode, HOLD='true')
        at_gate(q, 'qa_gate')
        d = build('deploy', deploy_target, label+'-deploy', MODE=mode, HOLD='true')
        if mode == 'old':
            at_gate(d, 'deploy_gate')
            proceed(q, 'qa_gate'); finish(q, 'FAILURE')
        else:
            queued(d)
            assert not events('deploy_start', label+'-deploy')
            proceed(q, 'qa_gate'); finish(q)
            at_gate(d, 'deploy_gate')
            assert events('qa_end', label)[0]['index'] < events('deploy_start', label+'-deploy')[0]['index']
        proceed(d, 'deploy_gate'); finish(d)
    else:
        d = build('deploy', deploy_target, label+'-deploy', HOLD='true')
        at_gate(d, 'deploy_gate')
        q = build('qa', qa_target, label, HOLD='true')
        queued(q); assert not events('qa_begin', label)
        proceed(d, 'deploy_gate'); finish(d)
        at_gate(q, 'qa_gate'); proceed(q, 'qa_gate'); finish(q)


def main():
    install()
    def bootstrap():
        q = build('qa', MEMBERS[0], 'missing-history')
        wait(lambda: 'PAIR_NOT_READY' in log(q), 'QA waits for deployment records')
        d = build('deploy', MEMBERS[0], 'bootstrap')
        at_gate(d, 'qa_pair_reconcile')
        assert not events('qa_begin', 'missing-history') and not events('deploy_start', 'bootstrap')
        proceed(d, 'qa_pair_reconcile'); finish(d)
        assert not events('qa_begin', 'missing-history')
        request(JENKINS, q + '/stop', {}, raw=True); finish(q, 'ABORTED')
    run_case('missing history blocks QA and requires explicit bootstrap', bootstrap)
    seed()
    for qa, peer in [(MEMBERS[0],MEMBERS[1]),(MEMBERS[1],MEMBERS[0]),
                     (MEMBERS[2],MEMBERS[3]),(MEMBERS[3],MEMBERS[2])]:
        run_case('baseline race '+qa, lambda q=qa,p=peer: overlap('old', q, p))
    seed()
    for qa, peer in [(MEMBERS[0],MEMBERS[1]),(MEMBERS[1],MEMBERS[0]),
                     (MEMBERS[2],MEMBERS[3]),(MEMBERS[3],MEMBERS[2])]:
        for order in ['qa-first','deploy-first']:
            run_case('protected '+qa+' '+order, lambda q=qa,p=peer,o=order: overlap('fixed',q,p,o))
        run_case('own redeploy '+qa, lambda q=qa: overlap('fixed',q,q))
    print('BASE_MATRIX_COMPLETE', flush=True)


if __name__ == '__main__': main()
