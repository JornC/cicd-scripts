import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_rollout import ROOT


class FlagReaderTests(unittest.TestCase):
    def test_resolved_flags_for_both_pairs_including_ni_overrides(self):
        with tempfile.TemporaryDirectory(prefix='qa-pair-flags-') as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'common', root / 'common')
            checkout = root / 'common/git_checkout_efficient.sh'
            checkout.write_text('#!/bin/sh\nexit 0\n')
            checkout.chmod(0o755)
            aws = root / 'aws'
            aws.write_text('''#!/usr/bin/env python3
import sys
a = sys.argv
if 'describe-parameters' in a:
    path = a[a.index('--filter') + 1].split('Values=', 1)[1]
    print(path + ('archive_api_host' if '/api/' in path else 'connect_api_host'))
elif 'get-parameters' in a:
    for path in a[a.index('--names') + 1:]:
        print(path + '\\t' + path.split('/')[1][3:] + '.aerius.uk')
else:
    raise SystemExit(1)
''')
            aws.chmod(0o755)
            for product, key, peer, flag in [
                ('CALCULATOR', 'ARCHIVE_API_HOST', 'ARCHIVE', 'API'),
                ('ARCHIVE', 'CONNECT_API_HOST', 'CALCULATOR', 'CONNECT_API'),
            ]:
                for region in ['', '-NI']:
                    target = 'UK-' + product + region + '-DEV'
                    host = (peer + region + '-DEV').lower() + '.aerius.uk'
                    flags = 'UK-' + peer + '-DEV/' + flag
                    if region:
                        flags += ',UK-' + peer + region + '-DEV/' + flag
                    env = os.environ | dict(CICD_SCRIPTS_DIR=str(root), PATH=str(root) + ':' + os.environ['PATH'],
                        FLAGS=flags, AWS_ACCOUNT_NAME='UK-DEV', SOURCE_JOB_NAME=target, JOB_NAME='QA-' + target,
                        DEPLOY_TERRAFORM_ACTION='apply', PRODUCT_NAME='test', CICD_QA_PAIR_TARGET=target,
                        CICD_QA_PAIR_PEER_SETTING=key, CICD_QA_PAIR_PEER_HOST=host)
                    with self.subTest(target=target):
                        result = subprocess.run(['bash', '-c', 'set -e; source "$1"; test "${FLAG_SETTINGS[$2]}" = "$3"',
                            'flags-test', str(root / 'common/read_job_flags.envsh'), key, host],
                            env=env, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
