import groovy.json.JsonSlurperClassic

import jenkins.scm.api.SCMSource

// Returns the labels of the pull request under check that are test flags (like TEST_SOMETHING_ENABLE), comma separated.
def call() {
  def github = getGitHubSource()
  def response

  withCredentials([usernamePassword(credentialsId: github.credentialsId, usernameVariable: 'GITHUB_APP', passwordVariable: 'GITHUB_TOKEN')]) {
    response = httpRequest(
      url: "${github.apiUri}/repos/${github.repoOwner}/${github.repository}/pulls/${env.CHANGE_ID}",
      quiet: true,
      customHeaders: [
        [name: 'Authorization', value: "Bearer ${GITHUB_TOKEN}", maskValue: true]
      ]
    )
  }

  return flagsFromLabels(response.content)
}

// The checkout uses an SSH key, so take the GitHub App credentials the multibranch job scans with
@NonCPS
def getGitHubSource() {
  def source = SCMSource.SourceByItem.findSource(currentBuild.rawBuild.parent)

  return [
    apiUri        : source.apiUri,
    repoOwner     : source.repoOwner,
    repository    : source.repository,
    credentialsId : source.credentialsId,
  ]
}

@NonCPS
def flagsFromLabels(String pullRequestJson) {
  return new JsonSlurperClassic().parseText(pullRequestJson).labels*.name.findAll { it ==~ /TEST_[A-Z0-9]+(_[A-Z0-9]+)*/ }.join(',')
}
