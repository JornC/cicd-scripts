import jenkins.model.Jenkins
import hudson.security.AuthorizationStrategy
import hudson.security.SecurityRealm
import hudson.slaves.EnvironmentVariablesNodeProperty
import org.jenkinsci.plugins.workflow.libs.GlobalLibraries
import org.jenkinsci.plugins.workflow.libs.LibraryConfiguration
import org.jenkinsci.plugins.workflow.libs.SCMSourceRetriever
import jenkins.plugins.git.GitSCMSource

def j = Jenkins.get()
j.setSecurityRealm(SecurityRealm.NO_AUTHENTICATION)
j.setAuthorizationStrategy(AuthorizationStrategy.UNSECURED)
j.setNumExecutors(12)
j.setQuietPeriod(0)
j.setCrumbIssuer(null)
j.globalNodeProperties.replace(new EnvironmentVariablesNodeProperty([
  new EnvironmentVariablesNodeProperty.Entry('CICD_SCRIPTS_DIR', '/implementation'),
  new EnvironmentVariablesNodeProperty.Entry('SSL_CERT_FILE', '/harness/ca.pem'),
  new EnvironmentVariablesNodeProperty.Entry('PATH+QA_PAIR', '/harness/bin')
]))
def library = new LibraryConfiguration('cicd-scripts', new SCMSourceRetriever(new GitSCMSource('/harness/library-source')))
library.setDefaultVersion('main')
library.setImplicit(false)
GlobalLibraries.get().setLibraries([library])
j.save()
