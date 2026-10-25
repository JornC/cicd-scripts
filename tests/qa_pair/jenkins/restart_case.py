import json
import sys
import time
from run import *

pending_file=ROOT/'results/restart-pending.json'

if sys.argv[1]=='prepare':
    install();seed();fixture('/reset-events')
    q=build('qa',MEMBERS[0],'restart-qa',HOLD='true',QA_SECONDS='600');at_gate(q,'qa_gate')
    d=build('deploy',MEMBERS[1],'restart-waiting-deploy');queued(d)
    failed=build('deploy',MEMBERS[3],'restart-aborted',HOLD='true');at_gate(failed,'deploy_gate')
    request(JENKINS,failed+'/stop',{},raw=True);finish(failed,'ABORTED')
    fixture('/deploy/complete',env=MEMBERS[3],test='restart-late-rollout')
    pending_file.write_text(json.dumps(dict(qa=q,deploy=d,aborted=failed)))
    print('READY_FOR_CONTROLLER_RESTART',flush=True)
else:
    p=json.loads(pending_file.read_text())
    wait(lambda:request(JENKINS,'/api/json'),'controller returns',90)
    at_gate(p['qa'],'qa_gate')
    bad=build('qa',MEMBERS[2],'restart-unreconciled')
    finish(bad,'FAILURE','Unreconciled deployment')
    repair=build('deploy',MEMBERS[3],'restart-repair')
    at_gate(repair,'qa_pair_reconcile');proceed(repair,'qa_pair_reconcile');finish(repair)
    assert not events('deploy_start','restart-waiting-deploy')
    proceed(p['qa'],'qa_gate');finish(p['qa']);finish(p['deploy'])
    assert events('qa_end','restart-qa')[0]['index'] < events('deploy_start','restart-waiting-deploy')[0]['index']
    assert not events('qa_begin','restart-unreconciled')
    report=json.loads((ROOT/'results/report.json').read_text())
    result=dict(name=sys.argv[2]+' preserves held lock and unsafe deployment journal',passed=True,events=events())
    report.append(result);(ROOT/'results/report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='events'}),flush=True)
