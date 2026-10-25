import nl.aerius.jenkinslib.util.BuildUtil
import nl.aerius.jenkinslib.util.FlagUtil

def call(Map config, Map qaPair, Closure body) {
  def agentLabel = config.agentLabel ?: 'any'

  def jobIsPrChecker = env.JOB_NAME.toUpperCase().startsWith('PULLREQUESTCHECKER-')
  def jobIsQA        = env.JOB_NAME.toUpperCase().startsWith('QA-')
  def jobIsDeploy    = env.JOB_NAME == 'DEPLOY-OTA-ENVIRONMENT'
  def jobIsBuild     = !jobIsQA && !jobIsPrChecker && !jobIsDeploy

  def jobIsImageBuildAcc = env.JOB_NAME == 'IMAGE-BUILD-ACC'
  def jobIsQAGeneric     = env.JOB_NAME == 'QA-GENERIC'

  pipeline {
    // Use agent label from config if provided
    agent {
      label agentLabel == 'any' ? '' : agentLabel
    }

    options {
      // We do not support this.. Almost always restarting from a specific stage will cause issues.
      disableRestartFromStage()
    }

    stages {
      stage('PipelineWrapper') {
        steps {
          script {
            // Set build name
            buildName(sh(script: "${env.CICD_SCRIPTS_DIR}/job/get_build_name.sh", returnStdout: true))

            // Process pre job webhooks and if web hooks are not working, mark job as unstable to signal this (not crashing on purpose).
            // At this time the webhooks used are just to help make stuff more user friendly, shouldn't be the end of the world if it crashes.
            catchError(buildResult: 'UNSTABLE', stageResult: 'UNSTABLE') {
              cicdPipelineProcessPreJobWebhooks(jobIsBuild, jobIsDeploy, jobIsPrChecker, jobIsQA)
            }

            // Keep a map of ENVs we want to wrap the body with. Mostly for convenience and help clean up
            //  the Jenskinsfiles that make use of this pipeline.
            // What we wrap can be found below.
            def wrapperEnvs = [:]
            if (qaPair.kind == 'deploy' && qaPair.action == 'apply') {
              wrapperEnvs['CICD_QA_PAIR_MANIFEST'] = "${pwd()}/.qa-pair-${env.BUILD_NUMBER}.json"
            }

            // Allow user to supply global environment configuration
            if (config.environment) {
              config.environment.each {
                key, value -> wrapperEnvs[key] = value
              }
            }
            // Also a script version, we do this so it runs in this context where a node is allocated
            // Without it will crash.. hard..
            if (config.environmentScripts) {
              config.environmentScripts.each {
                key, value -> wrapperEnvs[key] = sh(script: value, returnStdout: true)
              }
            }

            // For QA jobs also handle qaFlags block (similarly to deployFlags)
            if (jobIsQA && config.qaFlags && config.qaFlags instanceof Map) {
              def primaryServiceTheme = env.SERVICE_THEME.tokenize(',')[0]
              def qaFlags = ''

              // First use any flags already supplied to the job
              qaFlags = env.FLAGS ?: ''
              // Then add any shared flags
              qaFlags = FlagUtil.addFlag(qaFlags, config.qaFlags.getOrDefault('SHARED', ''))
              // After add any flags specific to the primary theme
              qaFlags = FlagUtil.addFlag(qaFlags, config.qaFlags.getOrDefault(primaryServiceTheme, ''))

              wrapperEnvs['FLAGS'] = qaFlags
            }

            // QA-GENERIC can run any branch, but Sonar stores every upload as the main branch analysis.
            // Main is analysed by the nightly QA jobs, so skip the Sonar goal in every Maven invocation here.
            if (jobIsQAGeneric) {
              echo '### [cicdPipeline] - QA-GENERIC detected, skipping the Sonar upload'
              wrapperEnvs['MAVEN_ARGS'] = "${env.MAVEN_ARGS ?: ''} -Dsonar.skip=true".trim()
            }

            // Set Docker registry environment vars as last step if it's a build job.
            // Needed when the scripts in 'docker/images/' are used in the pipeline, which should always be the case for a build job.
            // Apart from convenience, this also makes sure that if these - for whatever arbitrary reason - get
            //  renamed or needs more vars, we can fix it for all pipelines by changing it here.
            if (jobIsBuild) {
              echo '### [cicdPipeline] - build job detected, setting Docker Registry vars'
              withCredentials([string(credentialsId: 'DOCKER_REGISTRY_HOSTNAME', variable: 'DOCKER_REGISTRY_HOSTNAME')]) {
                if (jobIsImageBuildAcc && env.OVERRIDE_AERIUS_REGISTRY_PATH) {
                  echo "### [cicdPipeline] - Overriding registry path with: ${env.OVERRIDE_AERIUS_REGISTRY_PATH}"
                  wrapperEnvs['AERIUS_REGISTRY_PATH'] = env.OVERRIDE_AERIUS_REGISTRY_PATH
                } else {
                  def AERIUS_REGISTRY_PATH = sh(script: "${env.CICD_SCRIPTS_DIR}/docker/get_registry_path.sh", returnStdout: true)
                  wrapperEnvs['AERIUS_REGISTRY_PATH'] = AERIUS_REGISTRY_PATH
                }
                wrapperEnvs['AERIUS_REGISTRY_URL']    = "${DOCKER_REGISTRY_HOSTNAME}/${wrapperEnvs['AERIUS_REGISTRY_PATH']}/"

                if (jobIsImageBuildAcc && env.OVERRIDE_AERIUS_IMAGE_TAG) {
                  echo "### [cicdPipeline] - Overriding image tag with: ${env.OVERRIDE_AERIUS_IMAGE_TAG}"
                  wrapperEnvs['AERIUS_IMAGE_TAG']     = env.OVERRIDE_AERIUS_IMAGE_TAG
                } else {
                  wrapperEnvs['AERIUS_IMAGE_TAG']     = sh(script: "${env.CICD_SCRIPTS_DIR}/docker/get_image_tag.sh", returnStdout: true)
                }
              }
            }

            // Run the actual body wrapped by the final global environment list we crafted
            withEnv(wrapperEnvs.collect{key, value -> "${key}=${value}"}) {
              body()
              if (qaPair.kind == 'deploy' && qaPair.action == 'apply') {
                cicdQaPairVerifyDeployment(config)
              }

              try {
                // Process any post jobs stuff
                if (jobIsBuild) {
                  cicdPipelineProcessPostJobBuild(config)
                }
              } catch (err) {
                echo "### [cicdPipeline] - Crashed in post-job actions"
                // First crash wins!
                if (!env.CICD_CRASHED_STAGE) {
                  env.CICD_CRASHED_STAGE = 'cicdPipeline: Post-Job actions'
                }
                throw err
              }
            }
          }
        }
      }
    }

    post {
      always {
        script {
          echo "### [cicdPipeline] - Finished. Current Status: ${currentBuild.currentResult}"

          def flakyJobResultIfAny = cicdPipelineFlakyJob(currentBuild)
          def restartLimitReached = false
          // If this is a flaky job, do check if similar previous jobs failed as well..
          // If 3 consecutive jobs for this environment crashed (checking the last 10 jobs), stop retrying
          if (flakyJobResultIfAny != null) {
            int countPreviousJobs = 0
            int amountRestarted = 0

            def currentEnvironment = BuildUtil.getEnvironment(currentBuild)
            def previousBuild = currentBuild.previousBuild
            while (previousBuild != null && countPreviousJobs < 10) {
              def previousEnvironment = BuildUtil.getEnvironment(previousBuild)

              if (currentEnvironment == previousEnvironment) {
                if (previousBuild.result == 'FAILURE') {
                  amountRestarted++
                } else {
                  break
                }
              }

              previousBuild = previousBuild.previousBuild
              countPreviousJobs++
            }

            if (amountRestarted > 1) {
              echo "### [cicdPipeline] - Flaky job restarted ${amountRestarted} times.. Time to give up.."
              // Restarted enough times already, don't do that again
              restartLimitReached = true
            } else {
              echo "### [cicdPipeline] - Flaky job restarted ${amountRestarted} times.."
            }
          }

          // If QA job, we want to collect some reports based on what is requested by the pipeline
          if (jobIsQA) {
            cicdPipelineProcessQAReports(config)
          }

          // Always notify, except when the specific exclusions below apply.. this is rather extensive so I'll make it extra verbose
          def notify = true
          // Do not notify ...
          if (
            // for PR check jobs
            jobIsPrChecker
            // for build jobs
            || jobIsBuild
            // for deploy jobs if no one requested it specifically (nightlies for example - QA job will do the actual notifying)
            || (jobIsDeploy && !env.REQUESTED_BY_USER)
          ) {
            notify = false
          }

          // Force notify if a build didn't finish successfully, excluding PR check jobs
          if (!jobIsPrChecker && currentBuild.currentResult != 'SUCCESS') {
            notify = true
          }

          if (notify) {
            withBuildUser {
              // Append test status if it's a QA job
              def testStatusMessage = ''
              if (jobIsQA) {
                testStatusMessage = cicdGetTestStatusMessage(!jobIsQAGeneric)
                if (testStatusMessage != '') {
                  testStatusMessage = "\n\n```\n${testStatusMessage}\n```"
                }
              }

              def jobTypeString = ''
              jobTypeString = jobIsBuild  ? 'build' : jobTypeString
              jobTypeString = jobIsQA     ? 'QA'    : jobTypeString
              jobTypeString = jobIsDeploy ? (env.DEPLOY_TERRAFORM_ACTION == 'destroy' ? 'destroy' : 'deploy') : jobTypeString

              def jobFlakynessMessage = ''
              jobFlakynessMessage = flakyJobResultIfAny != null ? "\n\n:snowflake: Job is flaky and will be restarted, found: `${flakyJobResultIfAny}`" : jobFlakynessMessage
              jobFlakynessMessage = restartLimitReached ? '\n\n:snowflake: Job is flaky but reached the retry limit.' : jobFlakynessMessage

              Map messageColors = [SUCCESS: 'good', FAILURE: 'danger']

              mattermostSend(
                channel: (env.MATTERMOST_CHANNEL ? "#${env.MATTERMOST_CHANNEL}" : null),
                color: flakyJobResultIfAny && !restartLimitReached ? '#D3D3D3' : messageColors.getOrDefault(currentBuild.result, 'warning'),
                message: sh(script: """${CICD_SCRIPTS_DIR}/job/notify_mattermost_message.sh "${currentBuild.result}" "${currentBuild.durationString}" "${jobTypeString}" """, returnStdout: true) + testStatusMessage + jobFlakynessMessage
              )
            }
          }

          // Restart job if it's flaky and the limit is not reached yet
          if (flakyJobResultIfAny && !restartLimitReached) {
            echo '### [cicdPipeline] - Flaky job detected.. Restarting..'
            def rebuildParams = []

            params.each { key, value ->
                // Just use a string param for simplicity, which works for most pipeline parameters.
                rebuildParams.add(string(name: key, value: value.toString()))
            }

            build(
              job: env.JOB_NAME,
              parameters: rebuildParams,
              wait: false
            )
          }

          // Process post job webhooks and if web hooks are not working, mark job as unstable to signal this (not crashing on purpose).
          // At this time the webhooks used are just to help make stuff more user friendly, shouldn't be the end of the world if it crashes.
          catchError(buildResult: 'UNSTABLE', stageResult: 'UNSTABLE') {
            cicdPipelineProcessPostJobWebhooks(jobIsBuild, jobIsDeploy, jobIsPrChecker, jobIsQA)
          }
        }
      }
    }
  }
}
