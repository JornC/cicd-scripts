def call(Map config = [:]) {
  stage('Verify paired environment rollout') {
    timeout(time: config.qaPairRolloutTimeoutSeconds ?: 1800, unit: 'SECONDS') {
      waitUntil(initialRecurrencePeriod: 10000, quiet: true) {
        def status = sh(returnStatus: true, script: '''#!/usr/bin/env bash
          set -e
          python3 "${CICD_SCRIPTS_DIR}/qa/qa_pair.py" verify
        ''')
        if (!(status in [0, 3])) error('Could not verify the paired deployment; inspect the rollout check above')
        status == 0
      }
    }
    env.CICD_QA_PAIR_VERIFIED = 'true'
  }
}
