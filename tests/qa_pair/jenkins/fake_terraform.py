#!/usr/bin/env python3
"""Emit the intended Terraform state for a skeleton deployment."""
import json
import os

target = os.environ['CICD_QA_PAIR_TARGET']
cluster = target + '_FESCTR001'
registry = os.environ['AERIUS_REGISTRY_URL'].rstrip('/')
items = []
for name in ['api', 'worker']:
    arn = 'arn:aws:ecs:eu-west-2:123456789012:task-definition/' + target.lower() + '_' + name + ':' + os.environ['BUILD_NUMBER']
    for kind, value in [
        ('aws_ecs_task_definition', dict(arn=arn, container_definitions=json.dumps([
            dict(name=name, image=registry + '/' + name + ':' + os.environ['DEPLOY_IMAGE_TAG'])]))),
        ('aws_ecs_service', dict(name=name, cluster=cluster, task_definition=arn)),
        ('aws_appautoscaling_target', dict(service_namespace='ecs', resource_id='service/' + cluster + '/' + name, min_capacity=1)),
    ]:
        items.append(dict(mode='managed', type=kind, values=value))
print(json.dumps(dict(format_version='1.0', values=dict(root_module=dict(resources=items)))))
