import json
from pathlib import Path
import time
from run import *


def stop(path):
    request(JENKINS, path+'/stop', {}, raw=True)


def recover(target):
    p = build('deploy', target, 'reconcile-'+target)
    at_gate(p, 'qa_pair_reconcile')
    proceed(p, 'qa_pair_reconcile')
    finish(p)


def independence():
    q = build('qa', MEMBERS[0], 'independent-gb', HOLD='true')
    at_gate(q, 'qa_gate')
    finish(build('deploy', MEMBERS[3], 'independent-ni-deploy'))
    finish(build('qa', MEMBERS[2], 'independent-ni-qa'))
    assert request(JENKINS,q+'/api/json?tree=building')['building']
    proceed(q,'qa_gate'); finish(q)


def stopped_peer():
    fixture('/outside-stop', env=MEMBERS[1])
    q=build('qa',MEMBERS[0],'cold-start')
    wait(lambda:'PAIR_NOT_READY' in log(q),'QA releases pair')
    assert not events('qa_begin','cold-start')
    finish(build('deploy',MEMBERS[1],'cold-start-deploy'))
    finish(q)


def lost_readiness():
    fixture('/outside-stop',env=MEMBERS[1])
    q=build('qa',MEMBERS[0],'readiness-timeout',QA_SECONDS='4')
    finish(q,'ABORTED')
    assert not events('qa_begin','readiness-timeout')
    seed([MEMBERS[1]])


def rollout_gate():
    d=build('deploy',MEMBERS[0],'slow-rollout',COMPLETE='false',OLD_HEALTHY='true',TRIGGER_QA='true')
    wait(lambda:'Scheduling project: QA-GENERIC' in log(d),'child QA scheduled')
    wait(lambda:'waitUntil' in log(d),'strict completion gate')
    assert fixture('/state')['services'][MEMBERS[0]]['up']
    assert not fixture('/state')['services'][MEMBERS[0]]['stable']
    time.sleep(1)
    assert not events('qa_begin','slow-rollout-child')
    fixture('/deploy/complete',env=MEMBERS[0],test='slow-rollout')
    finish(d)
    wait(lambda:events('qa_end','slow-rollout-child'),'child QA completes')
    assert events('deploy_complete','slow-rollout')[0]['index'] < events('qa_begin','slow-rollout-child')[0]['index']


def failed_rollout():
    d=build('deploy',MEMBERS[0],'rollout-timeout',COMPLETE='false',OLD_HEALTHY='true',ROLLOUT_SECONDS='4')
    finish(d,'ABORTED')
    # ECS completes after Jenkins has abandoned the wait; old HTTP health alone was not enough.
    fixture('/deploy/complete',env=MEMBERS[0],test='late-completion')
    finish(build('qa',MEMBERS[1],'after-timeout'),'FAILURE','Unreconciled deployment')
    assert not events('qa_begin','after-timeout')
    recover(MEMBERS[0])
    finish(build('qa',MEMBERS[1],'after-timeout-recovery'))


def failed_apply():
    d=build('deploy',MEMBERS[1],'failed-apply',FAIL='true')
    finish(d,'FAILURE')
    finish(build('qa',MEMBERS[0],'after-failed-apply'),'FAILURE','Unreconciled deployment')
    # Automatic subsequent deployment is held before it can mutate anything.
    recovery=build('deploy',MEMBERS[1],'repair-failed-apply')
    at_gate(recovery,'qa_pair_reconcile')
    assert not events('deploy_start','repair-failed-apply')
    proceed(recovery,'qa_pair_reconcile');finish(recovery)
    finish(build('qa',MEMBERS[0],'after-failed-apply-repair'))


def aborted_apply():
    d=build('deploy',MEMBERS[1],'aborted-apply',HOLD='true')
    at_gate(d,'deploy_gate');stop(d);finish(d,'ABORTED')
    fixture('/deploy/complete',env=MEMBERS[1],test='orphaned-rollout-completes')
    finish(build('qa',MEMBERS[0],'after-abort'),'FAILURE','Unreconciled deployment')
    recover(MEMBERS[1])
    finish(build('qa',MEMBERS[0],'after-abort-recovery'))


def abort_qa():
    q=build('qa',MEMBERS[0],'abort-qa',HOLD='true')
    at_gate(q,'qa_gate')
    d=build('deploy',MEMBERS[1],'deploy-after-aborted-qa')
    queued(d);stop(q);finish(q,'ABORTED');finish(d)


def endpoint_guard():
    q=build('qa',MEMBERS[2],'wrong-peer',PEER_HOST='archive-dev.aerius.uk')
    finish(q,'FAILURE','Effective peer endpoint')
    d=build('deploy',MEMBERS[3],'wrong-peer-deploy',PEER_HOST='calculator-dev.aerius.uk')
    finish(d,'FAILURE','Effective peer endpoint')
    assert not events('qa_begin','wrong-peer') and not events('deploy_start','wrong-peer-deploy')
    # The pipeline entered before reading flags, so even this early failure needs reconciliation.
    recover(MEMBERS[3])


def ambiguous_target():
    q=build('qa',MEMBERS[2],'ambiguous',ENVIRONMENT_NAME=MEMBERS[0])
    finish(q,'FAILURE','Conflicting QA pair target')
    assert not events('qa_begin','ambiguous')


def stale_qa():
    finish(build('deploy',MEMBERS[0],'new-image',DEPLOY_IMAGE_TAG='v2'))
    finish(build('qa',MEMBERS[0],'stale-qa'),'FAILURE','superseded deployment')
    assert not events('qa_begin','stale-qa')
    finish(build('qa',MEMBERS[0],'current-qa',DEPLOY_IMAGE_TAG='v2'))
    seed([MEMBERS[0]])


def stale_deploy():
    old=build('deploy',MEMBERS[0],'older-run',PRE_HOLD='true',DEPLOY_IMAGE_TAG='old')
    at_gate(old,'before_lock')
    finish(build('deploy',MEMBERS[0],'newer-run',DEPLOY_IMAGE_TAG='v2'))
    proceed(old,'before_lock');finish(old,'FAILURE','refusing stale deployment')
    assert not events('deploy_start','older-run')
    seed([MEMBERS[0]])


def outside_shutdown():
    q=build('qa',MEMBERS[0],'outside-bypass',HOLD='true')
    at_gate(q,'qa_gate')
    fixture('/outside-stop',env=MEMBERS[1],test='bypassed-lock')
    proceed(q,'qa_gate');finish(q,'FAILURE')
    assert events('qa_check','outside-bypass')[0]['passed'] is False
    seed([MEMBERS[1]])


def destroy_pair_member():
    q=build('qa',MEMBERS[0],'before-destroy',HOLD='true');at_gate(q,'qa_gate')
    d=build('deploy',MEMBERS[1],'destroy',DEPLOY_TERRAFORM_ACTION='destroy');queued(d)
    assert not events('outside_stop','destroy')
    proceed(q,'qa_gate');finish(q);finish(d)
    finish(build('qa',MEMBERS[0],'after-destroy'),'FAILURE','Unreconciled deployment')
    seed([MEMBERS[1]]);finish(build('qa',MEMBERS[0],'after-recreate'))


def limited_executors():
    request(JENKINS,'/scriptText',{'script':'jenkins.model.Jenkins.get().setNumExecutors(2)'},raw=True)
    try:
        for member in [MEMBERS[1],MEMBERS[3]]:fixture('/outside-stop',env=member)
        a=build('qa',MEMBERS[0],'capacity-gb')
        wait(lambda:'PAIR_NOT_READY' in log(a),'GB waits without agent')
        b=build('qa',MEMBERS[2],'capacity-ni')
        wait(lambda:'PAIR_NOT_READY' in log(b),'NI waits without agent')
        d=build('deploy',MEMBERS[1],'capacity-deploy-gb')
        e=build('deploy',MEMBERS[3],'capacity-deploy-ni')
        finish(a);finish(b);finish(d);finish(e)
    finally:
        request(JENKINS,'/scriptText',{'script':'jenkins.model.Jenkins.get().setNumExecutors(12)'},raw=True)


if __name__=='__main__':
    install()
    results.extend(json.loads((ROOT/'results/report.json').read_text()))
    for name,fn in [
        ('GB and NI progress independently',independence),
        ('stopped peer does not deadlock its startup',stopped_peer),
        ('unavailable peer times out visibly',lost_readiness),
        ('healthy old API does not bypass rollout gate; async child proceeds',rollout_gate),
        ('rollout timeout blocks QA until explicit reconciliation',failed_rollout),
        ('failed apply blocks QA and automatic mutation until reconciliation',failed_apply),
        ('aborted apply remains unsafe after its external rollout completes',aborted_apply),
        ('QA abort releases pair for queued deployment',abort_qa),
        ('cross-pair effective endpoints rejected before work',endpoint_guard),
        ('conflicting environment identifiers rejected',ambiguous_target),
        ('superseded QA cannot silently test newer deployment',stale_qa),
        ('out-of-order older deployment cannot overwrite newer one',stale_deploy),
        ('destroy waits for QA and prevents admission until recreation',destroy_pair_member),
        ('waiting outside agents permits both pairs to start with two executors',limited_executors),
        ('limitation demonstrated: outside shutdown bypasses mutex',outside_shutdown),
    ]: run_case(name,fn)
    print('EDGE_CASES_COMPLETE',flush=True)
