@Library('cicd-scripts') _

def target = params.SOURCE_JOB_NAME
def groups = [
  'UK-CALCULATOR-DEV': ['UK-CALCULATOR-DEV', 'UK-ARCHIVE-DEV'],
  'UK-ARCHIVE-DEV': ['UK-CALCULATOR-DEV', 'UK-ARCHIVE-DEV'],
  'UK-CALCULATOR-NI-DEV': ['UK-CALCULATOR-NI-DEV', 'UK-ARCHIVE-NI-DEV'],
  'UK-ARCHIVE-NI-DEV': ['UK-CALCULATOR-NI-DEV', 'UK-ARCHIVE-NI-DEV']
]
if (params.PRE_HOLD == 'true') input(id: 'before_lock', message: 'Delayed before pair acquisition')
def body = {
  if (params.KIND == 'qa') {
    if (params.MODE != 'old') cicdQaPairRequireProtection()
    if (params.SMOKE == 'true') { echo 'UNPROTECTED_QA_COMPLETED'; return }
    sh '''#!/usr/bin/env bash
      declare -A FLAG_SETTINGS
      if [[ -n "${CICD_QA_PAIR_TARGET:-}" ]]; then
        FLAG_SETTINGS[${CICD_QA_PAIR_PEER_SETTING}]="${PEER_HOST:-${CICD_QA_PAIR_PEER_HOST}}"
      fi
      source "${CICD_SCRIPTS_DIR}/common/check_qa_pair_endpoint.envsh"
    '''
    sh "curl -fsS 'http://fixture:9000/qa/begin?test=${params.TEST_ID}&target=${target}&members=${groups[target].join(',')}'"
    try {
      if (params.HOLD == 'true') input(id: 'qa_gate', message: 'QA is active')
      sh "curl -fsS 'http://fixture:9000/qa/check?test=${params.TEST_ID}'"
    } finally {
      sh "curl -fsS 'http://fixture:9000/qa/end?test=${params.TEST_ID}'"
    }
  } else {
    sh '''#!/usr/bin/env bash
      declare -A FLAG_SETTINGS
      if [[ -n "${CICD_QA_PAIR_TARGET:-}" ]]; then
        FLAG_SETTINGS[${CICD_QA_PAIR_PEER_SETTING}]="${PEER_HOST:-${CICD_QA_PAIR_PEER_HOST}}"
      fi
      source "${CICD_SCRIPTS_DIR}/common/check_qa_pair_endpoint.envsh"
    '''
    if (params.DEPLOY_TERRAFORM_ACTION == 'destroy') {
      sh "curl -fsS 'http://fixture:9000/outside-stop?env=${target}&test=${params.TEST_ID}'"
      return
    }
    sh "curl -fsS 'http://fixture:9000/deploy/start?env=${target}&image=${params.DEPLOY_IMAGE_TAG}&test=${params.TEST_ID}&oldHealthy=${params.OLD_HEALTHY}&revision=${env.BUILD_NUMBER}'"
    if (params.HOLD == 'true') input(id: 'deploy_gate', message: 'Deployment is in progress')
    if (params.COMPLETE == 'true') sh "curl -fsS 'http://fixture:9000/deploy/complete?env=${target}&test=${params.TEST_ID}'"
    if (params.FAIL == 'true') error('Injected failure after mutation')
    if (params.MODE != 'old') {
      sh '''#!/usr/bin/env bash
        set -e
        mkdir -p .bin "environments/${CICD_QA_PAIR_TARGET,,}/eu-west-2/application_services/ecs_services/services_generator/.terragrunt-cache/a/b/.terraform"
        echo '{}' > "environments/${CICD_QA_PAIR_TARGET,,}/eu-west-2/application_services/ecs_services/services_generator/.terragrunt-cache/a/b/.terraform/terraform.tfstate"
        cp /harness/fake_terraform.py .bin/terraform
        chmod +x .bin/terraform
        python3 "${CICD_SCRIPTS_DIR}/qa/qa_pair.py" capture --root "${WORKSPACE}"
        if [[ "$MANIFEST_MODE" == 'missing' ]]; then rm "$CICD_QA_PAIR_MANIFEST"; fi
        if [[ "$MANIFEST_MODE" == 'stale' ]]; then
          python3 - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ['CICD_QA_PAIR_MANIFEST'])
value = json.loads(path.read_text())
value['build'] = 'stale'
path.write_text(json.dumps(value))
PY
        fi
      '''
    }
    if (params.TRIGGER_QA == 'true') {
      build(job: 'QA-GENERIC', parameters: [
        string(name: 'KIND', value: 'qa'), string(name: 'SOURCE_JOB_NAME', value: target),
        string(name: 'DEPLOY_IMAGE_TAG', value: params.DEPLOY_IMAGE_TAG),
        string(name: 'TEST_ID', value: params.TEST_ID + '-child')
      ], wait: false)
    }
  }
}
if (params.MODE in ['old', 'unguarded']) {
  node { body() }
} else {
  cicdPipeline([
    agentLabel: 'any', qaPairPollSeconds: 1,
    qaPairTimeoutSeconds: params.QA_SECONDS.toInteger(),
    qaPairRolloutTimeoutSeconds: params.ROLLOUT_SECONDS.toInteger()
  ], body)
}
