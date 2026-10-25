import nl.aerius.jenkinslib.util.QaPairUtil

def call(Map config = [:], Closure body) {
  def context = QaPairUtil.context([
    JOB_NAME: env.JOB_NAME, SOURCE_JOB_NAME: env.SOURCE_JOB_NAME,
    ENVIRONMENT_NAME: env.ENVIRONMENT_NAME, DEPLOY_TERRAFORM_ACTION: env.DEPLOY_TERRAFORM_ACTION,
    AWS_ACCOUNT_NAME: env.AWS_ACCOUNT_NAME
  ])
  if (context == null) { body(null); return }
  def pairEnvironment = [
    "CICD_QA_PAIR_TARGET=${context.target}", "CICD_QA_PAIR_RESOURCE=${context.resource}",
    "CICD_QA_PAIR_PEER_HOST=${context.peerHost}", "CICD_QA_PAIR_PEER_SETTING=${context.peerSetting}",
    "CICD_QA_PAIR_HEALTH_HOSTS=${context.hosts.join(',')}"
  ]
  withEnv(pairEnvironment) {
    if (context.kind == 'deploy') {
      lock(resource: context.resource) {
        def previous = QaPairUtil.latestDeployment(context.target)
        if (previous && previous.number > currentBuild.number) error('A newer deployment has entered; refusing stale deployment')
        if (!previous || (!previous.ready && !(previous.action == 'destroy' && previous.result == 'SUCCESS'))) {
          timeout(time: 60, unit: 'MINUTES') {
            input(id: 'qa_pair_reconcile', message: previous ?
              "Confirm deployment ${previous.number} for ${context.target} can no longer mutate the environment before reconciliation" :
              "Bootstrap ${context.target}: confirm earlier unprotected deployments can no longer mutate the environment")
          }
        }
        if (!currentBuild.keepLog) {
          currentBuild.keepLog = true
          env.CICD_QA_PAIR_OWNS_RETENTION = 'true'
        }
        env.CICD_QA_PAIR_ENTERED = 'v1'
        env.CICD_QA_PAIR_READY = 'false'
        env.CICD_QA_PAIR_VERIFIED = 'false'
        body(context)
        if (currentBuild.currentResult in ['FAILURE', 'ABORTED']) error('Paired deployment did not complete successfully')
        if (context.action == 'apply') {
          if (env.CICD_QA_PAIR_VERIFIED != 'true') error('Paired deployment did not verify its Terraform rollout')
          env.CICD_QA_PAIR_READY = 'true'
        }
        QaPairUtil.releasePreviousRecords(context.target, currentBuild.number)
      }
      return
    }

    timeout(time: config.qaPairTimeoutSeconds ?: 10800, unit: 'SECONDS') {
      def completed = false
      while (!completed) {
        lock(resource: context.resource) {
          def states = context.members.collectEntries { member -> [(member): QaPairUtil.latestDeployment(member)] }
          String failedTarget = context.members.find { member -> states[member] && !states[member].ready && !states[member].building }
          if (failedTarget) error("Unreconciled deployment for ${failedTarget}")
          if (states.every { member, state -> state?.ready }) {
            if (!env.DEPLOY_IMAGE_TAG || states[context.target].image != env.DEPLOY_IMAGE_TAG) {
              error('QA targets a missing or superseded deployment image')
            }
            def ready = false
            node(config.agentLabel == 'any' ? '' : (config.agentLabel ?: '')) {
              def status = sh(returnStatus: true, script: '''#!/usr/bin/env bash
                set -e
                python3 "${CICD_SCRIPTS_DIR}/qa/qa_pair.py" health
              ''')
              if (!(status in [0, 3])) error('Could not check QA pair health; inspect the readiness check above')
              ready = status == 0
            }
            if (ready) {
              withEnv(["CICD_QA_PAIR_ADMITTED=${context.resource}:${context.target}"]) { body(context) }
              completed = true
            }
          }
          if (!completed) echo "PAIR_NOT_READY: releasing ${context.resource} and agent so deployments can proceed"
        }
        if (!completed) sleep(time: config.qaPairPollSeconds ?: 15, unit: 'SECONDS')
      }
    }
  }
}
