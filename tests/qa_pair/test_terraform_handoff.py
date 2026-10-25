import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_rollout import ROOT, REGISTRY, TARGET, state


class TerraformHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qa-pair-terraform-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cicd = self.root / 'cicd'
        self.workspace = self.root / 'cloudinfra'
        self.workspace.mkdir()
        for folder in ['terraform', 'prepare', 'common', 'qa']:
            shutil.copytree(ROOT / folder, self.cicd / folder, ignore=shutil.ignore_patterns('__pycache__'))
        self.script(self.cicd / 'common/git_checkout_efficient.sh', '#!/bin/sh\nexit 0\n')
        self.script(self.root / 'bin/aws', '''#!/usr/bin/env bash
if [[ "$*" == *describe-parameters* ]]; then
  echo /uk-calculator-dev/flags/connect_api/connect_api_host
else
  printf '/uk-calculator-dev/flags/connect_api/connect_api_host\\t%s\\n' "$FIXTURE_PEER"
fi
''')
        self.script(self.workspace / 'scripts/prepare-modules.sh', '#!/bin/sh\nexit 0\n')
        self.script(self.workspace / 'scripts/aws-account-name-to-id.sh', '#!/bin/sh\necho 123456789012\n')
        self.script(self.workspace / 'scripts/do-terragrunt-action.sh', '''#!/usr/bin/env bash
set -e
echo "$TERRAFORM_ACTION" >> "$EVENT_LOG"
if [[ "$TERRAFORM_ACTION" == 'apply' && "$FIXTURE_FAIL" == 'true' ]]; then exit 1; fi
mkdir -p .bin "environments/uk-archive-dev/eu-west-2/application_services/ecs_services/services_generator/.terragrunt-cache/a/b/.terraform"
echo '{}' > environments/uk-archive-dev/eu-west-2/application_services/ecs_services/services_generator/.terragrunt-cache/a/b/.terraform/terraform.tfstate
cp "$FIXTURE_TERRAFORM" .bin/terraform
''')
        self.script(self.root / 'terraform', '''#!/usr/bin/env python3
import os
from pathlib import Path
with Path(os.environ['EVENT_LOG']).open('a') as f: f.write('show\\n')
print(''' + repr(json.dumps(state())) + ''')
''')
        self.events = self.root / 'events'
        self.output = self.root / 'manifest.json'
        self.env = os.environ | dict(
            PATH=str(self.root / 'bin') + ':' + os.environ['PATH'],
            CICD_SCRIPTS_DIR=str(self.cicd), WORKSPACE=str(self.workspace),
            SOURCE_JOB_NAME=TARGET, SOURCE_JOB_BUILD_NUMBER='1', BUILD_NUMBER='12',
            DEPLOY_GIT_COMMIT='test-commit', DEPLOY_GIT_URL='git@github.com:aerius/archive-service.git',
            AERIUS_REGISTRY_URL=REGISTRY, DEPLOY_IMAGE_TAG='v2', SERVICE_TYPE='DEV', SERVICE_THEME='UK',
            AWS_ACCOUNT_NAME='UK-DEV', DEPLOY_TERRAFORM_ACTION='apply', FLAGS='UK-CALCULATOR-DEV/CONNECT_API',
            CICD_QA_PAIR_TARGET=TARGET, CICD_QA_PAIR_PEER_SETTING='CONNECT_API_HOST',
            CICD_QA_PAIR_PEER_HOST='calculator-dev.aerius.uk', CICD_QA_PAIR_MANIFEST=str(self.output),
            FIXTURE_PEER='calculator-dev.aerius.uk', FIXTURE_FAIL='false', EVENT_LOG=str(self.events),
            FIXTURE_TERRAFORM=str(self.root / 'terraform'))

    def script(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def execute(self, **changes):
        return subprocess.run(['bash', str(self.cicd / 'terraform/run_v1.0.sh')],
            cwd=self.workspace, env=self.env | changes, capture_output=True, text=True)

    def test_capture_runs_after_successful_apply(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events.read_text().splitlines(), ['init', 'apply', 'show'])
        self.assertEqual(json.loads(self.output.read_text())['image'], 'v2')

    def test_failed_apply_never_creates_ready_manifest(self):
        result = self.execute(FIXTURE_FAIL='true')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events.read_text().splitlines(), ['init', 'apply'])
        self.assertFalse(self.output.exists())

    def test_wrong_peer_is_rejected_before_terraform(self):
        result = self.execute(FIXTURE_PEER='calculator-ni-dev.aerius.uk')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Effective peer endpoint', result.stderr)
        self.assertFalse(self.events.exists())

    def test_dry_run_does_not_capture_or_mutate(self):
        result = self.execute(DEPLOY_TERRAFORM_ACTION='dry-run', CICD_QA_PAIR_TARGET='')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.events.exists())
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
