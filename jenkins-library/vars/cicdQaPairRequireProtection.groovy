import nl.aerius.jenkinslib.util.QaPairUtil

def call() {
  def context = QaPairUtil.context([
    JOB_NAME: env.JOB_NAME, SOURCE_JOB_NAME: env.SOURCE_JOB_NAME, ENVIRONMENT_NAME: env.ENVIRONMENT_NAME,
    AWS_ACCOUNT_NAME: env.AWS_ACCOUNT_NAME
  ])
  if (context && env.CICD_QA_PAIR_ADMITTED != "${context.resource}:${context.target}") {
    error('QA was not admitted under its pair lock; check the cicd-scripts rollout')
  }
}
