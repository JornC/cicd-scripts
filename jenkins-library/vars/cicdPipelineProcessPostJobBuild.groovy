import nl.aerius.jenkinslib.util.FlagUtil
import nl.aerius.jenkinslib.util.StringUtil

import com.amazonaws.auth.AWSStaticCredentialsProvider
import com.amazonaws.auth.BasicSessionCredentials
import com.amazonaws.services.simplesystemsmanagement.AWSSimpleSystemsManagement
import com.amazonaws.services.simplesystemsmanagement.AWSSimpleSystemsManagementClientBuilder
import com.amazonaws.services.simplesystemsmanagement.model.ParameterStringFilter
import com.amazonaws.services.simplesystemsmanagement.model.DescribeParametersRequest

// this is the equivalent of the old job/postscript_*.sh script with some extra convenience wrappers
def call(Map config) {
  // Get git URL from config, as this will contain the end result instead of posssibly variables that are replaced by Jenkins.
  // The PR builder for example uses dynamic git URLs.
  // trim() is needed as the output has a new line at the end causing havoc.
  def paramGitUrl = sh(script: "git config remote.origin.url", returnStdout: true).trim()

  // For our custom temporary builds, use the proper environment name
  def paramJobName = env.JOB_NAME == 'STIKSTOFJE-DEPLOY-OTA-ENVIRONMENT' ? env.ENVIRONMENT_NAME : env.JOB_NAME

  // Jobs starting with UK-, should use the UK account by default, if account is already set, that has precedence.
  // NL account will be the default if nothing is specified.
  def paramAwsAccountName = 'NL-DEV'
  paramAwsAccountName = !env.AWS_ACCOUNT_NAME && paramJobName.toUpperCase().startsWith('UK-') ? 'UK-DEV' : (env.AWS_ACCOUNT_NAME ?: paramAwsAccountName)

  // If env.REQUESTED_BY_USER is not set and the job is triggered by a user, use this as the requester
  def paramRequestedByUser = !env.REQUESTED_BY_USER && env.BUILD_USER_ID && env.BUILD_USER_ID != 'ota-environment-deploy' ? env.BUILD_USER_ID : (env.REQUESTED_BY_USER ?: '')

  def paramFlags = ''
  // if it is a Map we should handle it as a Map that contains flags per theme
  if (config.deployFlags instanceof Map) {
    def primaryServiceTheme = env.SERVICE_THEME.tokenize(',')[0]

    // First add any dynamic flags for this environment
    paramFlags = FlagUtil.addFlag(paramFlags, getDynamicFlagsForCurrentEnvironment(paramAwsAccountName.toLowerCase(), paramJobName))
    // Then add any shared flags
    paramFlags = FlagUtil.addFlag(paramFlags, config.deployFlags.getOrDefault('SHARED', ''))
    // After add any flags specific to the primary theme
    paramFlags = FlagUtil.addFlag(paramFlags, config.deployFlags.getOrDefault(primaryServiceTheme, ''))
    // Last but not least, add any job specific flags
    paramFlags = FlagUtil.addFlag(paramFlags, env.JOB_SPECIFIC_FLAGS ?: '')
  } else {
    paramFlags = config.deployFlags
  }

  def jobParams = [
    string(name: 'SOURCE_JOB_NAME',         value: paramJobName),
    string(name: 'SOURCE_JOB_BUILD_NUMBER', value: env.BUILD_NUMBER),
    string(name: 'DEPLOY_GIT_COMMIT',       value: env.GIT_COMMIT),
    string(name: 'DEPLOY_GIT_URL',          value: paramGitUrl),
    string(name: 'AERIUS_REGISTRY_URL',     value: env.AERIUS_REGISTRY_URL),
    string(name: 'DEPLOY_IMAGE_TAG',        value: env.AERIUS_IMAGE_TAG),
  ]
  // Add optional params, if not set they will fallback to default values set in the job itself
  [
    SERVICE_TYPE      : env.SERVICE_TYPE,
    SERVICE_THEME     : env.SERVICE_THEME,
    AWS_ACCOUNT_NAME  : paramAwsAccountName,
    FLAGS             : paramFlags,
    MATTERMOST_CHANNEL: env.MATTERMOST_CHANNEL,
    REQUESTED_BY_USER : paramRequestedByUser,
    CICD_JOB_MESSAGES : getJobMessagesAndAddCurrentJobDuration('build'),
  ].each {
    key, value -> if (value) { jobParams << string(name: key, value: value) }
  }
  if (env.DRY_RUN) { jobParams << string(name: 'DEPLOY_TERRAFORM_ACTION', value: 'dry-run') }
  // if BUILD_DEPLOY_AS_PROD flag is detected, override SERVICE_TYPE to PRD
  // This doesn't mean it will deploy it to PROD, just deploy it with production configuration.
  if (FlagUtil.hasFlag(env.JOB_SPECIFIC_FLAGS ?: '', 'BUILD_DEPLOY_AS_PROD')) { jobParams << string(name: 'SERVICE_TYPE', value: 'PRD') }

  // Trigger Terraform job that will do a deploy
  build(job: 'DEPLOY-OTA-ENVIRONMENT', parameters: jobParams, wait: false)
}

def getJobMessagesAndAddCurrentJobDuration(String durationType) {
  def result = env.CICD_JOB_MESSAGES ?: ''
  if (result) {
    result += ';'
  }

  result += "jobduration ${durationType} ${StringUtil.trimSuffix(currentBuild.durationString, 'and counting')}"

  return result
}

def getDynamicFlagsForCurrentEnvironment(String profile, String environment) {
  def awsRoleAccount = env["CICD_SCRIPTS_AWS_PROFILE_" + profile.replaceAll('-', '_')] // this uses getAt() internally
  def awsRegion = profile.startsWith('uk-') ? 'eu-west-2' : 'eu-west-1'

  withAWS(roleAccount: awsRoleAccount, role: 'jenkins-nodes-qa', region: awsRegion) {
    // Filter to only get parameters for given environment
    def filter = new ParameterStringFilter()
      .withKey('Name')
      .withOption('BeginsWith')
      .withValues("/${environment.toLowerCase()}/flags/".toString())

    // The Java SDK won't get this from the environment correctly as it's pure Java, so we need
    //  to supply these explicitly
    def awsCreds = new BasicSessionCredentials(
      env.AWS_ACCESS_KEY_ID,
      env.AWS_SECRET_ACCESS_KEY,
      env.AWS_SESSION_TOKEN
    )

    def ssm = AWSSimpleSystemsManagementClientBuilder.standard()
      .withCredentials(new AWSStaticCredentialsProvider(awsCreds))
      .withRegion(env.AWS_REGION)
      .build()

    // Execute request
    def response = ssm.describeParameters(new DescribeParametersRequest().withParameterFilters(filter))

    // Collect parameters, formatting them as a flag and only collect the unique entries.
    // Sort it as well. Even though they should never overlap content wise, some wise guy might get some weird ideas
    //  and we want to be sure this code produces the same order in such a case.
    def flags = response.parameters.collect { param ->
        def parts = param.name.split('/')
        // parts[1] = environment name, parts[3] = flag name
        return "${parts[1]}/${parts[3]}".toUpperCase()
    }.sort().unique()

    def result = flags.join(',')
    if (result) {
      echo "### [cicdPipeline] - Found dynamic flags to add for current environment: ${result}"
    } else {
      echo '### [cicdPipeline] - No dynamic flags found to add for current environment'
    }

    return result
  }
}
