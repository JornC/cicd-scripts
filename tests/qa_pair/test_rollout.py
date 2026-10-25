import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('qa_pair', ROOT / 'qa/qa_pair.py')
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)
TARGET = 'UK-ARCHIVE-DEV'
CLUSTER = TARGET + '_FESCTR001'
REGISTRY = '123456789012.dkr.ecr.eu-west-2.amazonaws.com/archive'


def state(names=('api', 'worker')):
    items = []
    for name in names:
        arn = 'arn:aws:ecs:eu-west-2:123456789012:task-definition/uk-archive-dev_' + name + ':7'
        for kind, value in [
            ('aws_ecs_task_definition', dict(arn=arn, container_definitions=json.dumps([
                dict(name=name, image=REGISTRY + '/' + name + ':v2', environment=[dict(name='PASSWORD', value='SECRET')])]))),
            ('aws_ecs_service', dict(name=name, cluster=CLUSTER, task_definition=arn.rsplit('/', 1)[-1])),
            ('aws_appautoscaling_target', dict(service_namespace='ecs', resource_id='service/' + CLUSTER + '/' + name, min_capacity=1)),
        ]:
            items.append(dict(mode='managed', type=kind, values=value))
    return dict(format_version='1.0', values=dict(root_module=dict(child_modules=[dict(resources=items)])))


def manifest(names=('api', 'worker')):
    return qa.manifest_from_state(state(names), TARGET, 'v2', REGISTRY + '/', '12')


class Ecs:
    def __init__(self, expected):
        self.services = {}
        self.tasks = {}
        self.calls = []
        for name, service in expected['services'].items():
            self.services[name] = dict(serviceName=name, status='ACTIVE', desiredCount=1, runningCount=1, pendingCount=0,
                taskDefinition=service['task_definition'], deployments=[dict(status='PRIMARY',
                    taskDefinition=service['task_definition'], rolloutState='COMPLETED')])
            self.tasks[name] = dict(taskArn=name, taskDefinitionArn=service['task_definition'], lastStatus='RUNNING',
                desiredStatus='RUNNING', containers=[dict(name=c['name'], image=c['image'], lastStatus='RUNNING') for c in service['containers']])

    def __call__(self, operation, **arguments):
        self.calls.append((operation, arguments))
        if operation == 'list-services': return dict(serviceArns=list(self.services))
        if operation == 'describe-services': return dict(services=[self.services[n] for n in arguments['services']])
        if operation == 'list-tasks': return dict(taskArns=[arguments['service_name']])
        if operation == 'describe-tasks': return dict(tasks=[self.tasks[n] for n in arguments['tasks']])
        raise AssertionError(operation)


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.expected = manifest()
        self.ecs = Ecs(self.expected)

    def test_completed_intended_rollout(self):
        self.assertTrue(qa.rollout_ready(self.expected, self.ecs))

    def test_missing_or_unexpected_service(self):
        del self.ecs.services['worker']
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))
        self.ecs.services['unexpected'] = self.ecs.services['api']
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_zero_tasks_never_count_as_ready(self):
        self.ecs.services['api'].update(desiredCount=0, runningCount=0)
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_healthy_old_service_during_rollout(self):
        self.ecs.services['api']['deployments'].append(dict(status='ACTIVE'))
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_rollback_to_old_task_definition(self):
        self.ecs.services['api']['taskDefinition'] = self.ecs.services['api']['taskDefinition'].replace(':7', ':6')
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_task_has_old_revision_even_when_service_reports_stable(self):
        self.ecs.tasks['api']['taskDefinitionArn'] = self.ecs.tasks['api']['taskDefinitionArn'].replace(':7', ':6')
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_failed_rollout(self):
        self.ecs.services['api']['deployments'][0]['rolloutState'] = 'FAILED'
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_pending_or_missing_tasks(self):
        self.ecs.services['api']['pendingCount'] = 1
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))
        self.ecs.services['api'].update(pendingCount=0, runningCount=0)
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_unhealthy_or_stopping_task(self):
        self.ecs.tasks['api']['healthStatus'] = 'UNHEALTHY'
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))
        self.ecs.tasks['api'].update(healthStatus='HEALTHY', desiredStatus='STOPPED')
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_container_image_mismatch(self):
        self.ecs.tasks['api']['containers'][0]['image'] = REGISTRY + '/api:v1'
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_missing_container(self):
        self.ecs.tasks['api']['containers'] = []
        self.assertFalse(qa.rollout_ready(self.expected, self.ecs))

    def test_aws_reports_missing_service(self):
        def missing(operation, **arguments):
            if operation == 'describe-services': return dict(services=[], failures=[dict(reason='MISSING')])
            return self.ecs(operation, **arguments)
        self.assertFalse(qa.rollout_ready(self.expected, missing))

    def test_aws_service_batch_limit(self):
        expected = manifest(tuple('service-' + str(i) for i in range(23)))
        ecs = Ecs(expected)
        self.assertTrue(qa.rollout_ready(expected, ecs))
        self.assertEqual([10, 10, 3], [len(args['services']) for op, args in ecs.calls if op == 'describe-services'])


class ManifestTests(unittest.TestCase):
    def test_no_secrets_persisted(self):
        result = manifest()
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('PASSWORD', json.dumps(result))
        self.assertTrue(result['services']['api']['task_definition'].endswith(':7'))

    def test_wrong_image_or_registry(self):
        for image, registry in [('v1', REGISTRY), ('v2', REGISTRY + '-other')]:
            with self.assertRaises(qa.InvalidDeployment):
                qa.manifest_from_state(state(), TARGET, image, registry, '12')

    def test_wrong_cluster(self):
        with self.assertRaises(qa.InvalidDeployment):
            qa.manifest_from_state(state(), 'UK-ARCHIVE-NI-DEV', 'v2', REGISTRY, '12')

    def test_missing_task_definition_or_scaling_target(self):
        for kind in ['aws_ecs_task_definition', 'aws_appautoscaling_target']:
            value = state()
            items = value['values']['root_module']['child_modules'][0]['resources']
            items[:] = [item for item in items if item['type'] != kind]
            with self.assertRaises(qa.InvalidDeployment):
                qa.manifest_from_state(value, TARGET, 'v2', REGISTRY, '12')

    def test_unknown_schema_and_empty_state(self):
        for value in [dict(format_version='2.0'), dict(format_version='1.0')]:
            with self.assertRaises(qa.InvalidDeployment):
                qa.manifest_from_state(value, TARGET, 'v2', REGISTRY, '12')

    def test_capture_from_initialized_cloudinfra_workspace(self):
        with tempfile.TemporaryDirectory(prefix='qa state ') as tmp:
            root = Path(tmp)
            folder = root / 'environments/uk-archive-dev/eu-west-2/application_services/ecs_services/services_generator/.terragrunt-cache/a/b/.terraform'
            folder.mkdir(parents=True)
            (folder / 'terraform.tfstate').write_text('{}')
            (root / '.bin').mkdir()
            binary = root / '.bin/terraform'
            binary.write_text('#!/usr/bin/env python3\nimport json\nprint(' + repr(json.dumps(state())) + ')\n')
            binary.chmod(0o755)
            output = root / 'manifest.json'
            qa.capture(root, dict(CICD_QA_PAIR_TARGET=TARGET, DEPLOY_IMAGE_TAG='v2', AERIUS_REGISTRY_URL=REGISTRY,
                                  BUILD_NUMBER='12', CICD_QA_PAIR_MANIFEST=str(output)))
            self.assertEqual(json.loads(output.read_text()), manifest())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_missing_or_ambiguous_terraform_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(qa.InvalidDeployment):
                qa.capture(root, dict(CICD_QA_PAIR_TARGET=TARGET))
            for cache in ['a', 'b']:
                folder = root / ('environments/uk-archive-dev/eu-west-2/application_services/ecs_services/'
                                 'services_generator/.terragrunt-cache/' + cache + '/.terraform')
                folder.mkdir(parents=True)
                (folder / 'terraform.tfstate').write_text('{}')
            with self.assertRaises(qa.InvalidDeployment):
                qa.capture(root, dict(CICD_QA_PAIR_TARGET=TARGET))


class EndpointTests(unittest.TestCase):
    def test_only_deploy_destroy_can_skip_missing_peer_flags(self):
        for job, expected in [('DEPLOY-OTA-ENVIRONMENT', 0), ('QA-GENERIC', 1)]:
            env = os.environ | dict(JOB_NAME=job, DEPLOY_TERRAFORM_ACTION='destroy', SOURCE_JOB_NAME=TARGET,
                CICD_QA_PAIR_TARGET=TARGET, CICD_QA_PAIR_PEER_SETTING='CONNECT_API_HOST',
                CICD_QA_PAIR_PEER_HOST='calculator-dev.aerius.uk')
            run = subprocess.run(['bash', '-c', 'declare -A FLAG_SETTINGS; source "$1"', 'endpoint-test',
                str(ROOT / 'common/check_qa_pair_endpoint.envsh')], env=env, capture_output=True)
            self.assertEqual(run.returncode, expected, job)

    def test_effective_peer_for_both_directions_and_pairs(self):
        for region in ['', '-NI']:
            for product, key, peer in [('CALCULATOR', 'ARCHIVE_API_HOST', 'archive'), ('ARCHIVE', 'CONNECT_API_HOST', 'calculator')]:
                target = 'UK-' + product + region + '-DEV'
                host = peer + region.lower() + '-dev.aerius.uk'
                for value, expected in [(host, 0), ('wrong.aerius.uk', 1)]:
                    env = os.environ | dict(SOURCE_JOB_NAME=target, CICD_QA_PAIR_TARGET=target,
                        CICD_QA_PAIR_PEER_SETTING=key, CICD_QA_PAIR_PEER_HOST=host)
                    run = subprocess.run(['bash', '-c', 'declare -A FLAG_SETTINGS; FLAG_SETTINGS[$1]=$2; source "$3"',
                        'endpoint-test', key, value, str(ROOT / 'common/check_qa_pair_endpoint.envsh')],
                        env=env, capture_output=True)
                    self.assertEqual(run.returncode, expected, (target, value))

    def test_unprotected_flags_unchanged(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith('CICD_QA_PAIR_')}
        run = subprocess.run(['bash', str(ROOT / 'common/check_qa_pair_endpoint.envsh')], env=env)
        self.assertEqual(run.returncode, 0)


class HealthTests(unittest.TestCase):
    def test_health_requires_up_json_and_the_expected_url(self):
        host = 'archive-dev.aerius.uk'
        url = 'https://' + host + '/api/actuator/health'
        for value, response_url, status, expected in [
            ({'status': 'UP'}, url, 200, True),
            ({'status': 'DOWN'}, url, 200, False),
            ([], url, 200, False),
            ({'status': 'UP'}, url + '/login', 200, False),
            ({'status': 'UP'}, url, 503, False),
        ]:
            response = io.BytesIO(json.dumps(value).encode())
            response.url, response.status = response_url, status
            with patch.object(qa.urllib.request, 'urlopen', return_value=response):
                self.assertEqual(qa.health_ready([host]), expected)


if __name__ == '__main__':
    unittest.main()
