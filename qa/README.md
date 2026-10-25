# GB and NI Calculator / Archive coordination

The Calculator export tests call Archive. Archive upload tests call Calculator, which can call Archive again. Keeping Archive running does not prevent either suite from encountering a redeployment. The shared pipeline therefore gives each pair one Jenkins lock, held by both QA suites and by deployments of either member.

| Lock | Members | Peer hosts |
| --- | --- | --- |
| `qa-pair:uk-gb` | `UK-CALCULATOR-DEV`, `UK-ARCHIVE-DEV` | `calculator-dev.aerius.uk`, `archive-dev.aerius.uk` |
| `qa-pair:uk-ni` | `UK-CALCULATOR-NI-DEV`, `UK-ARCHIVE-NI-DEV` | `calculator-ni-dev.aerius.uk`, `archive-ni-dev.aerius.uk` |

The mapping lives in `jenkins-library/src/nl/aerius/jenkinslib/util/QaPairUtil.groovy`. It covers the root-level `DEPLOY-OTA-ENVIRONMENT` job and `QA-*` jobs, including `QA-GENERIC`. Targets come from `SOURCE_JOB_NAME`, `ENVIRONMENT_NAME`, and the named QA job suffix. Conflicting targets and accounts other than `UK-DEV` fail before work starts. Other environments retain the existing pipeline behavior.

## Pipeline behavior

`cicdPipeline` calls `cicdWithQaPair` before entering the existing Declarative pipeline, now in `cicdPipelineBody`. Waiting for the pair lock does not allocate an agent. The existing checkout, QA reports, notifications and post actions remain in that pipeline for admitted runs. An admission failure is visible in Jenkins but does not enter that pipeline's reporting/notification stages. Calculator and Archive each call `cicdQaPairRequireProtection()` at the start of their QA body to detect a missing admission wrapper.

For apply/destroy, the lock surrounds the entire pipeline, including Terraform, verification and post actions. Before the body runs, the deployment records `CICD_QA_PAIR_ENTERED=v1` and `CICD_QA_PAIR_READY=false` in Jenkins build metadata. Apply becomes ready only after strict verification and successful pipeline completion. A failure or abort leaves an unsafe record. Destroy remains unready. Dry-run does not mutate or acquire the lock.

For QA, the wrapper acquires the lock and reads the latest entered deployment for each member. A completed unsafe record fails QA immediately. Missing records or unhealthy endpoints cause QA to release the lock and its short-lived readiness agent before waiting again. Once both members are ready, the requested `DEPLOY_IMAGE_TAG` must match the target's latest deployment and both HTTPS health endpoints must return `200` with JSON `status: UP`. QA then holds the lock for its entire existing suite. GB and NI can proceed independently; QA suites within one pair are serialized.

The flag reader checks the effective peer after resolving all flags, including NI overrides: Calculator must use the pair's `ARCHIVE_API_HOST`, and Archive its `CONNECT_API_HOST`. A wrong endpoint fails before tests or Terraform mutation. The runtime connection settings and test assertions are unchanged.

## Rollout proof

After a successful Terraform apply, `terraform/run_v1.0.sh` calls `qa_pair.py capture` in the same Cloud-Infra workspace. It finds the initialized `services_generator` module and runs the workspace's `.bin/terraform show -json`. It extracts expected ECS service names, task-definition ARNs, container images and minimum capacity. Raw Terraform state, including secrets, stays in memory; only these selected fields are written to a private manifest outside the temporary Cloud-Infra workspace.

The gate binds that manifest to the current environment, image and Jenkins build number. Through the existing `uk-dev` AWS profile in `eu-west-2`, it checks:

- The exact expected service set exists in the target cluster.
- Every service is active, has positive desired capacity at or above its Terraform minimum, and has all desired tasks running with none pending.
- Only the intended primary deployment remains; a reported rollout state must be `COMPLETED`.
- Every running task uses the expected task-definition ARN and container images. Essential containers must be running; unhealthy tasks or essential containers are rejected.
- The deployed service's HTTPS health endpoint is up.

A healthy old deployment, an ECS rollback or an empty cluster cannot satisfy this gate. Missing/stale manifests and AWS permission/tool errors fail immediately; an incomplete rollout waits up to the rollout timeout. The existing asynchronous QA trigger remains asynchronous, so a child job cannot deadlock its deployment's lock.

Terraform's JSON output can expose sensitive values; the filtered handoff is deliberate. See [Terraform show](https://developer.hashicorp.com/terraform/cli/commands/show). ECS requests use the documented [10-service](https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-services.html) and [100-task](https://docs.aws.amazon.com/cli/latest/reference/ecs/describe-tasks.html) batch limits.

## Deployment records and recovery

The latest entered record for each environment is authoritative, even when a previous deployment succeeded. Builds are retained with Jenkins `keepLog`; old pins owned by this mechanism are released only after a successful newer apply/destroy. Existing retention pins are left alone. Failed attempts retain the previous records until reconciliation succeeds. Do not delete the latest records or manually remove their retention pins.

The first protected deployment for each environment presents a bootstrap input before any mutation. A later deployment following an unsafe record presents the same reconciliation input. Confirm it only after establishing that earlier Terraform processes and external rollouts can no longer change that environment. This input is a human assertion about external activity, not an automated cancellation mechanism. It holds the pair lock without an agent, and expires after one hour. The subsequent successful apply must still pass the complete rollout gate.

A build delayed before acquiring the lock cannot overwrite a newer entered deployment: its lower deployment build number is rejected. A stale QA image is also rejected rather than reported as a passing test of a different deployment.

## Adoption

1. Make the updated `cicd-scripts` available to both the trusted shared library and agent-side scripts. They must use the matching version. Existing jobs already call this wrapper; the change requires no new job configuration, bot change or Cloud-Infra source change.
2. Merge the Calculator and Archive QA guard calls after the shared-library helper is available. No application test is removed or skipped.
3. Let earlier unprotected pipelines finish, or stop them and verify their external work has ended. Confirm bootstrap for a controlled deployment of each of the four environments. Later normal deployments need no input.
4. Check the first real rollout capture and gate on each member before relying on nightly results. The local reproduction exercises the control flow and adapters, not the production AWS account or agent images.

Runtime prerequisites are the existing Lockable Resources and Pipeline Input steps, a trusted shared library able to read/retain Jenkins runs, Python 3.7+ on deployment and QA agents, AWS CLI v2 on the deployment agent, and working HTTPS trust/DNS. The `uk-dev` deployment profile needs `ecs:ListServices`, `ecs:DescribeServices`, `ecs:ListTasks`, and `ecs:DescribeTasks`, plus its existing Terraform state access. The capture expects the current Cloud-Infra workspace layout with one initialized `services_generator` module and `.bin/terraform`. These assumptions fail closed if unmet; local skeleton tests do not verify live IAM, agent tools or the Cloud-Infra runner.

| Shared-library option | Default | Scope |
| --- | --- | --- |
| `qaPairTimeoutSeconds` | 10800 | Total protected QA duration, including waiting and execution |
| `qaPairPollSeconds` | 15 | Delay after releasing an unready pair |
| `qaPairRolloutTimeoutSeconds` | 1800 | Strict verification after the existing deployment body |

The first rollout should also check that the total QA timeout fits normal suite duration and queueing. Readiness checks can use an agent briefly; waiting between attempts does not reserve one.

## Assurance boundary

This removes deployment/test overlap when both jobs run on the same Jenkins controller through this wrapper. It preserves the real cross-service E2E tests and their assertions. It does not remove the applications' runtime dependency or require both services to come from the same nightly build.

An AWS console action, another controller, an unwrapped job or a scheduling bot can still stop/change a service without taking the Jenkins lock. Before QA, missing availability prevents admission; during QA, an outside shutdown still fails the tests. Both members must remain available for the test duration. A permanently running GB Archive can still redeploy, which is covered when that redeployment uses the protected job. Bot code was not available for verification; this change makes no claim to control it.

The lock also does not prevent spontaneous crashes, autoscaling actions, database resets, credential/licence failures or other test defects. Retained Jenkins metadata is part of the coordination state and must survive normal controller maintenance. Controller restart and forced-kill tests verify lock/record persistence in the local reproduction; restoring an old backup or deleting metadata requires draining/reconciling outstanding work again.

Reproduction instructions and the exact test boundaries are in [tests/qa_pair/README.md](../tests/qa_pair/README.md).
