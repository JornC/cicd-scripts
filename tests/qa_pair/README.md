# QA pair validation

Run adapter and shell handoff tests from the repository root with Python 3.9+ and Bash:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/qa_pair -p 'test_*.py' -v
```

These exercise the actual Python rollout adapter, endpoint guard, flag reader and Terraform driver with deterministic Terraform/SSM/ECS responses. They check wrong revisions/images, rollback, zero/missing/unhealthy tasks, API batching, missing state, private manifests with no copied secrets, NI flag overrides and capture only after successful apply. No AWS credentials are used.

Run the actual shared library on an isolated Jenkins controller with Docker, Git, OpenSSL and Python 3.9+:

```sh
python3 -u tests/qa_pair/jenkins/run-local.py --work-dir /tmp/qa-pair-validation
```

The directory must not already exist. The launcher builds the pinned Jenkins image, creates its own internal Docker network, and runs skeleton Calculator/Archive services for GB and NI. It binds no host ports and mounts no host credentials or Docker socket. Test hostname aliases resolve only inside that network. Image/plugin downloads happen before the controller runs. The harness refuses to reuse an existing container/network with its names and cleans up the resources it creates. `--keep` retains those resources for debugging; the work directory and evidence remain either way.

The skeletons expose Calculator export → Archive lookup and Archive upload → Calculator summary → Archive lookup. Controlled gates guarantee overlap in the baseline, making all four original races reproducible. Protected runs cover peer and own redeployments, both acquisition orders, independent pairs, stopped peers, rollout timeouts, failures/aborts, stale requests, endpoint mismatch, destroy, bootstrap, record retention and missing/stale manifests. Two-executor runs detect agent starvation. Graceful restart and forced process termination exercise persistent locks and unsafe records. A deliberate outside shutdown demonstrates the remaining limit of Jenkins-only coordination.

The controller loads a Git snapshot of the production shared library. It uses the actual `cicdPipeline`, its Declarative body, the pair wrapper and utility, admission guard, verification step and Python adapter. Notifications/build display helpers are stubbed; no messages are sent. Terraform capture and AWS reads consume controlled service state. The separate shell tests exercise the actual Terraform driver. This does not execute real Terraform resources or the application test suites, validate production IAM, or reproduce every production plugin/agent configuration.

Evidence under `<work-dir>/results` includes the case report, ordered skeleton events, Jenkins logs, installed plugin versions and SHA-256 hashes of the implementation snapshot. Source hashes let a reviewer check which implementation was tested. This reproduction is local test infrastructure and must not be exposed as a production Jenkins controller.
