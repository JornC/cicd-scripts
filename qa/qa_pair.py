#!/usr/bin/env python3
"""Capture Terraform's intended ECS deployment and check it before admitting QA."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


class InvalidDeployment(Exception):
    pass


def require(condition, message):
    if not condition:
        raise InvalidDeployment(message)


def resources(module):
    yield from module.get('resources', [])
    for child in module.get('child_modules', []):
        yield from resources(child)


def task_revision(value):
    return value.rsplit('/', 1)[-1]


def manifest_from_state(state, environment, image, registry, build):
    require(state.get('format_version', '').split('.')[0] == '1', 'Unsupported Terraform JSON format')
    require(environment and image and registry and build, 'Deployment identity is incomplete')
    require(state.get('values', {}).get('root_module'), 'Terraform state has no resource values')
    values = list(resources(state['values']['root_module']))
    cluster = environment + '_FESCTR001'
    definitions = {}
    minimums = {}
    for resource in values:
        value = resource.get('values') or {}
        if resource.get('mode') != 'managed':
            continue
        if resource['type'] == 'aws_ecs_task_definition':
            definitions[task_revision(value['arn'])] = value
        if resource['type'] == 'aws_appautoscaling_target' and value.get('service_namespace') == 'ecs':
            prefix = 'service/' + cluster + '/'
            if value.get('resource_id', '').startswith(prefix):
                minimums[value['resource_id'][len(prefix):]] = value['min_capacity']
    services = {}
    application_images = 0
    for resource in values:
        if resource.get('mode') != 'managed' or resource.get('type') != 'aws_ecs_service':
            continue
        value = resource['values']
        require(value['cluster'].rsplit('/', 1)[-1] == cluster, 'Terraform service targets a different cluster')
        name = value['name']
        definition = definitions.get(task_revision(value['task_definition']))
        require(definition is not None, 'Terraform service has no matching task definition')
        require(name not in services and name in minimums, 'Duplicate service or missing scaling target')
        containers = []
        for container in json.loads(definition['container_definitions']):
            container_image = container['image']
            if container_image.startswith(registry.rstrip('/') + '/'):
                require(container_image.endswith(':' + image), 'Terraform contains a different application image version')
                application_images += 1
            containers.append({'name': container['name'], 'image': container_image,
                               'essential': container.get('essential', True)})
        require(containers, 'Terraform task definition has no containers')
        services[name] = {'task_definition': definition['arn'], 'minimum': max(1, int(minimums[name])),
                          'containers': containers}
    require(services and application_images, 'Terraform state has no services or expected application images')
    return {'schema': 1, 'environment': environment, 'cluster': cluster, 'image': image,
            'build': str(build), 'services': services}


def capture(root, environ):
    environment = environ['CICD_QA_PAIR_TARGET']
    region = 'eu-west-2'
    directory = root / 'environments' / environment.lower() / region / 'application_services/ecs_services/services_generator'
    candidates = list(directory.rglob('.terraform/terraform.tfstate'))
    require(len(candidates) == 1, 'Expected one initialized Terraform services_generator directory')
    binary = root / '.bin/terraform'
    require(binary.is_file(), 'Cloud-Infra Terraform executable is missing')
    result = subprocess.run([str(binary), '-chdir=' + str(candidates[0].parent.parent), 'show', '-json'],
                            capture_output=True, text=True, check=False, timeout=120)
    require(result.returncode == 0, 'Could not read Terraform state for rollout verification')
    # State can contain secrets. Keep it in memory and persist only the selected deployment fields.
    manifest = manifest_from_state(json.loads(result.stdout), environment, environ['DEPLOY_IMAGE_TAG'],
                                   environ['AERIUS_REGISTRY_URL'], environ['BUILD_NUMBER'])
    path = Path(environ['CICD_QA_PAIR_MANIFEST'])
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.qa-pair-', delete=False) as handle:
        json.dump(manifest, handle)
        temporary = Path(handle.name)
    temporary.replace(path)
    print('Captured expected task definitions for', len(manifest['services']), 'services')


class Aws:
    def __init__(self, account):
        require(account == 'UK-DEV', 'Protected QA pairs require AWS_ACCOUNT_NAME=UK-DEV')
        self.command = ['aws', '--profile', account.lower(), '--region', 'eu-west-2',
                        '--output', 'json', '--no-cli-pager', '--cli-connect-timeout', '10', '--cli-read-timeout', '30']

    def __call__(self, operation, **arguments):
        command = self.command + ['ecs', operation]
        for name, value in arguments.items():
            command += ['--' + name.replace('_', '-')] + (value if isinstance(value, list) else [value])
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
        require(result.returncode == 0, 'AWS ECS ' + operation + ' failed; rollout verification cannot continue')
        return json.loads(result.stdout)


def batches(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def rollout_ready(manifest, aws):
    cluster = manifest['cluster']
    expected = manifest['services']
    listed = aws('list-services', cluster=cluster)['serviceArns']
    if {arn.rsplit('/', 1)[-1] for arn in listed} != set(expected):
        return False
    for names in batches(sorted(expected), 10):
        response = aws('describe-services', cluster=cluster, services=names)
        if response.get('failures') or {service['serviceName'] for service in response['services']} != set(names):
            return False
        for service in response['services']:
            intended = expected[service['serviceName']]
            desired = service['desiredCount']
            if (service['status'] != 'ACTIVE' or desired < intended['minimum'] or
                    service['runningCount'] != desired or service['pendingCount'] != 0 or
                    service['taskDefinition'] != intended['task_definition']):
                return False
            deployments = service['deployments']
            if (len(deployments) != 1 or deployments[0]['status'] != 'PRIMARY' or
                    deployments[0]['taskDefinition'] != intended['task_definition'] or
                    deployments[0].get('rolloutState', 'COMPLETED') != 'COMPLETED'):
                return False
            tasks = aws('list-tasks', cluster=cluster, service_name=service['serviceName'],
                        desired_status='RUNNING')['taskArns']
            if len(tasks) != desired:
                return False
            for identifiers in batches(tasks, 100):
                response = aws('describe-tasks', cluster=cluster, tasks=identifiers)
                if response.get('failures') or {task['taskArn'] for task in response['tasks']} != set(identifiers):
                    return False
                for task in response['tasks']:
                    if (task['taskDefinitionArn'] != intended['task_definition'] or task['lastStatus'] != 'RUNNING' or
                            task['desiredStatus'] != 'RUNNING' or task.get('healthStatus') == 'UNHEALTHY'):
                        return False
                    containers = {container['name']: container for container in task['containers']}
                    for container in intended['containers']:
                        actual = containers.get(container['name'])
                        if (not actual or actual['image'] != container['image'] or
                                (container['essential'] and (actual['lastStatus'] != 'RUNNING' or
                                 actual.get('healthStatus') == 'UNHEALTHY'))):
                            return False
    return True


def health_ready(hosts):
    for host in hosts:
        require(host and '/' not in host and ':' not in host, 'Invalid QA pair health hostname')
        url = 'https://' + host + '/api/actuator/health'
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                health = json.load(response)
                if (response.status != 200 or response.url != url or
                        not isinstance(health, dict) or health.get('status') != 'UP'):
                    return False
        except (urllib.error.URLError, TimeoutError, ValueError):
            return False
    return bool(hosts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['capture', 'verify', 'health'])
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    environ = os.environ
    try:
        if args.action == 'capture':
            require(args.root is not None, 'Cloud-Infra workspace is required')
            capture(args.root.resolve(), environ)
            return 0
        if args.action == 'health':
            ready = health_ready(environ['CICD_QA_PAIR_HEALTH_HOSTS'].split(','))
        else:
            manifest = json.loads(Path(environ['CICD_QA_PAIR_MANIFEST']).read_text())
            require(manifest['schema'] == 1 and manifest['environment'] == environ['CICD_QA_PAIR_TARGET'] and
                    manifest['image'] == environ['DEPLOY_IMAGE_TAG'] and manifest['build'] == environ['BUILD_NUMBER'],
                    'Missing, stale or conflicting rollout manifest')
            require(manifest['cluster'] == environ['CICD_QA_PAIR_TARGET'] + '_FESCTR001' and manifest['services'],
                    'Rollout manifest has a conflicting cluster or no services')
            ready = rollout_ready(manifest, Aws(environ['AWS_ACCOUNT_NAME']))
            if ready:
                host = environ['CICD_QA_PAIR_TARGET'].lower()[3:] + '.aerius.uk'
                ready = health_ready([host])
        if not ready:
            print('QA pair readiness check is pending', file=sys.stderr)
        return 0 if ready else 3
    except (InvalidDeployment, KeyError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        # Do not print AWS responses, Terraform state or subprocess output here.
        print('QA pair verification failed:', str(error) if isinstance(error, InvalidDeployment) else type(error).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
