package nl.aerius.jenkinslib.util

import com.cloudbees.groovy.cps.NonCPS
import hudson.model.ParametersAction
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.cps.EnvActionImpl

class QaPairUtil implements Serializable {
  static final String DEPLOY_JOB = 'DEPLOY-OTA-ENVIRONMENT'
  static final Map PAIRS = [
    'uk-gb': ['UK-CALCULATOR-DEV', 'UK-ARCHIVE-DEV'],
    'uk-ni': ['UK-CALCULATOR-NI-DEV', 'UK-ARCHIVE-NI-DEV']
  ]
  static final Map HOSTS = [
    'UK-CALCULATOR-DEV': 'calculator-dev.aerius.uk',
    'UK-ARCHIVE-DEV': 'archive-dev.aerius.uk',
    'UK-CALCULATOR-NI-DEV': 'calculator-ni-dev.aerius.uk',
    'UK-ARCHIVE-NI-DEV': 'archive-ni-dev.aerius.uk'
  ]

  @NonCPS
  static Map context(Map values) {
    String job = values.JOB_NAME ?: ''
    String kind = job == DEPLOY_JOB ? 'deploy' : (job.startsWith('QA-') ? 'qa' : '')
    if (!kind) return null
    List targets = [values.SOURCE_JOB_NAME, values.ENVIRONMENT_NAME]
    if (kind == 'qa' && job != 'QA-GENERIC') targets.add(job.substring(3))
    targets = targets.findAll { it?.trim() }.collect { it.trim().toUpperCase(Locale.ROOT) }.unique()
    if (!targets.any { HOSTS.containsKey(it) }) return null
    if (targets.size() != 1) throw new IllegalArgumentException('Conflicting QA pair target environments')
    if (values.AWS_ACCOUNT_NAME != 'UK-DEV') throw new IllegalArgumentException('Protected QA pairs require the UK-DEV AWS account')
    String target = targets[0]
    String name = PAIRS.keySet().find { PAIRS[it].contains(target) }
    List members = PAIRS[name]
    String peer = members.find { it != target }
    String action = values.DEPLOY_TERRAFORM_ACTION ?: 'apply'
    if (kind == 'deploy' && !(action in ['apply', 'destroy', 'dry-run'])) {
      throw new IllegalArgumentException('Unsupported deployment action for a protected QA pair')
    }
    if (kind == 'deploy' && action == 'dry-run') return null
    return [name: name, resource: "qa-pair:${name}".toString(), kind: kind, target: target,
      members: members, peerHost: HOSTS[peer], hosts: members.collect { HOSTS[it] }, action: action,
      peerSetting: target.contains('CALCULATOR') ? 'ARCHIVE_API_HOST' : 'CONNECT_API_HOST']
  }

  @NonCPS
  static Map latestDeployment(String target) {
    def job = Jenkins.get().getItemByFullName(DEPLOY_JOB)
    if (job == null) return null
    for (def run : job.getBuilds()) {
      def parameters = run.getAction(ParametersAction)
      if (parameters?.getParameter('SOURCE_JOB_NAME')?.value?.trim()?.toUpperCase(Locale.ROOT) != target) continue
      def variables = run.getAction(EnvActionImpl)?.getOverriddenEnvironment() ?: [:]
      if (variables.CICD_QA_PAIR_ENTERED != 'v1') continue
      return [number: run.number, ready: variables.CICD_QA_PAIR_READY == 'true' && !(run.result?.toString() in ['FAILURE', 'ABORTED']),
        building: run.isBuilding(), result: run.result?.toString(),
        action: parameters?.getParameter('DEPLOY_TERRAFORM_ACTION')?.value ?: 'apply',
        image: parameters?.getParameter('DEPLOY_IMAGE_TAG')?.value]
    }
    return null
  }

  @NonCPS
  static void releasePreviousRecords(String target, int currentNumber) {
    def job = Jenkins.get().getItemByFullName(DEPLOY_JOB)
    for (def run : job.getBuilds()) {
      if (run.number >= currentNumber || run.isBuilding()) continue
      def parameters = run.getAction(ParametersAction)
      def variables = run.getAction(EnvActionImpl)?.getOverriddenEnvironment() ?: [:]
      if (parameters?.getParameter('SOURCE_JOB_NAME')?.value?.trim()?.toUpperCase(Locale.ROOT) == target &&
          variables.CICD_QA_PAIR_OWNS_RETENTION == 'true') {
        run.keepLog(false)
      }
    }
  }
}
