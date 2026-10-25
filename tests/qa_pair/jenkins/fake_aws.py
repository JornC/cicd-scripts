#!/usr/bin/env python3
"""AWS CLI boundary fixture; never connects to AWS."""
import json
import sys
import urllib.request

arguments = sys.argv[1:]
operation = arguments[arguments.index('ecs') + 1]
options = {}
key = None
for value in arguments[arguments.index('ecs') + 2:]:
    if value.startswith('--'):
        key = value[2:]
        options[key] = []
    else:
        options[key].append(value)
cluster = options['cluster'][0]
target = cluster.removesuffix('_FESCTR001')
with urllib.request.urlopen('http://fixture:9000/state') as response:
    state = json.load(response)['services'][target]
prefix = 'arn:aws:ecs:eu-west-2:123456789012:'
registry = '123456789012.dkr.ecr.eu-west-2.amazonaws.com/app'
services = ['api', 'worker']


def definition(name, revision):
    return prefix + 'task-definition/' + target.lower() + '_' + name + ':' + str(revision)


if operation == 'list-services':
    result = dict(serviceArns=[prefix + 'service/' + cluster + '/' + name for name in services])
elif operation == 'describe-services':
    entries = []
    for name in options['services']:
        revision = state.get('revision', 1) if state['stable'] else state.get('next_revision', 1)
        rollout = [dict(status='PRIMARY', taskDefinition=definition(name, revision),
                        rolloutState='COMPLETED' if state['stable'] else 'IN_PROGRESS')]
        entries.append(dict(serviceName=name, status='ACTIVE', desiredCount=state['desired'],
            runningCount=state['running'] if state['up'] else 0, pendingCount=0,
            taskDefinition=definition(name, revision), deployments=rollout))
    result = dict(services=entries)
elif operation == 'list-tasks':
    result = dict(taskArns=[prefix + 'task/' + cluster + '/' + options['service-name'][0]] if state['up'] else [])
elif operation == 'describe-tasks':
    entries = []
    for task in options['tasks']:
        name = task.rsplit('/', 1)[-1]
        entries.append(dict(taskArn=task, taskDefinitionArn=definition(name, state.get('revision', 1)),
            lastStatus='RUNNING', desiredStatus='RUNNING',
            containers=[dict(name=name, image=registry + '/' + name + ':' + state['image'], lastStatus='RUNNING')]))
    result = dict(tasks=entries)
else:
    raise SystemExit('Unsupported fixture operation: ' + operation)
print(json.dumps(result))
