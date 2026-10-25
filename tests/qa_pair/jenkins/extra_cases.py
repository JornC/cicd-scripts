"""Validate production entrypoints, rollout handoff and deployment record retention."""
from run import *


def named_jobs():
    for member in MEMBERS:
        finish(build('qa', member, 'named-' + member, job='QA-' + member))
    bad = build('qa', MEMBERS[0], 'wrong-named-target', job='QA-' + MEMBERS[2])
    finish(bad, 'FAILURE', 'Conflicting QA pair target')
    assert not events('qa_begin', 'wrong-named-target')


def guard():
    finish(build('qa', MEMBERS[0], 'bypass', MODE='unguarded'), 'FAILURE', 'QA was not admitted')
    assert not events('qa_begin', 'bypass')
    finish(build('qa', 'NL-CALCULATOR-DEV', 'unprotected', SMOKE='true'), contains='UNPROTECTED_QA_COMPLETED')


def wrong_account():
    for kind in ['qa', 'deploy']:
        finish(build(kind, MEMBERS[0], 'wrong-account-' + kind, AWS_ACCOUNT_NAME='NL-DEV'),
               'FAILURE', 'require the UK-DEV AWS account')
    assert not events('qa_begin') and not events('deploy_start')


def qa_action_override():
    finish(build('qa', MEMBERS[0], 'qa-action-override', DEPLOY_TERRAFORM_ACTION='destroy',
                 PEER_HOST='archive-ni-dev.aerius.uk'), 'FAILURE', 'Effective peer endpoint')
    assert not events('qa_begin', 'qa-action-override')


def metadata(path):
    number = int(path.rsplit('/', 1)[-1])
    script = f'''
def run = jenkins.model.Jenkins.get().getItem('DEPLOY-OTA-ENVIRONMENT').getBuildByNumber({number})
def vars = run.getAction(org.jenkinsci.plugins.workflow.cps.EnvActionImpl).getOverriddenEnvironment()
println(groovy.json.JsonOutput.toJson([kept: run.isKeepLog(), ready: vars.CICD_QA_PAIR_READY]))
'''
    return json.loads(request(JENKINS, '/scriptText', {'script': script}, raw=True))


def retention():
    old = build('deploy', MEMBERS[0], 'retention-old'); finish(old)
    assert metadata(old) == dict(kept=True, ready='true')
    failed = build('deploy', MEMBERS[0], 'retention-failed', FAIL='true'); finish(failed, 'FAILURE')
    assert metadata(failed) == dict(kept=True, ready='false')
    assert metadata(old)['kept']
    finish(build('qa', MEMBERS[1], 'retention-failed-qa'), 'FAILURE', 'Unreconciled deployment')
    repair = build('deploy', MEMBERS[0], 'retention-repair')
    at_gate(repair, 'qa_pair_reconcile')
    assert not events('deploy_start', 'retention-repair')
    proceed(repair, 'qa_pair_reconcile'); finish(repair)
    assert metadata(repair) == dict(kept=True, ready='true')
    assert not metadata(failed)['kept'] and not metadata(old)['kept']


def manifest_handoff():
    for mode in ['missing', 'stale']:
        d = build('deploy', MEMBERS[0], 'manifest-' + mode, MANIFEST_MODE=mode)
        finish(d, 'FAILURE', 'Could not verify the paired deployment')
        assert metadata(d) == dict(kept=True, ready='false')
        finish(build('qa', MEMBERS[1], 'manifest-qa-' + mode), 'FAILURE', 'Unreconciled deployment')
        seed([MEMBERS[0]])


if __name__ == '__main__':
    results.extend(json.loads((ROOT / 'results/report.json').read_text()))
    seed()
    for name, case in [
        ('all four named QA entrypoints and conflicting named target', named_jobs),
        ('product guard rejects wrapper bypass; unrelated QA remains unchanged', guard),
        ('wrong AWS account is rejected before QA or mutation', wrong_account),
        ('a QA action parameter cannot bypass the peer endpoint guard', qa_action_override),
        ('latest and unsafe records stay pinned until successful reconciliation', retention),
        ('missing and stale Terraform manifests leave the deployment unsafe', manifest_handoff),
    ]:
        run_case(name, case)
