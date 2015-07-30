#!/usr/bin/env python
"""
A toolkit for identifying and advertising service resources.

Uses a specific naming convention for the Task Definition of services.  If you
name the Task Definition ending with "-service", no configuration is needed.
This also requires that you not use that naming convention for task definitions
that are not services.

For example:
    A Task Definition with the family name of 'cache-service' will have its
    hosting Container Instance's internal ip added to a Route53 private Zone as
    cache.local and other machines on the same subnet can address it that way.
"""

import argparse
import logging
import os
import re
import json
import boto
import boto.ec2
import boto.route53
import requests
from etcd.client import Client
from time import sleep


region = os.environ.get('ECS_REGION', 'us-east-1')
ecs = boto.connect_ec2containerservice(
        host='ecs.{0}.amazonaws.com'.format(region))
ec2 = boto.ec2.connect_to_region(region)
route53 = boto.route53.connect_to_region(region)

logging.basicConfig(format='%(asctime)s %(message)s',
                    datefmt='%Y/%m/%d/ %I:%M:%S %p')

if 'ECS_CLUSTER' in os.environ:
    cluster = os.environ['ECS_CLUSTER']
elif os.path.exists('/etc/ecs/ecs.config'):
    pat = re.compile(r'\bECS_CLUSTER\b\s*=\s*(\w*)')
    cluster = pat.findall(open('/etc/ecs/ecs.config').read())[-1]
else:
    cluster = None


def get_task_arns(family):
    """
    Get the ARN of running task, given the family name.
    """

    response = ecs.list_tasks(cluster=cluster, family=family)
    arns = response['ListTasksResponse']['ListTasksResult']['taskArns']
    if len(arns) == 0:
        return None
    return arns



def get_ec2_interface(container_instance_arn):
    """
    Get the ec2 interface from an container instance ARN. 
    """

    response = ecs.describe_container_instances(container_instance_arn, cluster=cluster)
    ec2_instance_id = response['DescribeContainerInstancesResponse'] \
        ['DescribeContainerInstancesResult']['containerInstances'] \
        [0]['ec2InstanceId']

    response = ec2.get_all_instances(filters={'instance-id': ec2_instance_id})
    return response[0].instances[0].interfaces[0]


def get_zone_for_vpc(vpc_id):
    """
    Identify the Hosted Zone for the given VPC.

    Assumes a 1 to 1 relationship.

    NOTE: There is an existing bug.
    https://github.com/boto/boto/issues/3061
    When that changes, I expect to have to search ['VPCs'] as a list of
    dictionaries rather than a dictionary.  This has the unfortunate side
    effect of not working for Hosted Zones that are associated with more than
    one VPC. (But, why would you expect internal DNS for 2 different private
    networks to be the same anyway?)
    """

    response = route53.get_all_hosted_zones()['ListHostedZonesResponse']
    for zone in response['HostedZones']:
        zone_id = zone['Id'].split('/')[-1]
        detail = route53.get_hosted_zone(zone_id)['GetHostedZoneResponse']
        try:
            if detail['VPCs']['VPC']['VPCId'] == vpc_id:
                return {'zone_id': zone_id, 'zone_name': zone['Name']}
        except KeyError:
            pass


def get_service_info(service_name):
    info = {
        "name": service_name,
        "tasks": []
    }

    if service_name[-8:] == '-service':
        info['name'] = service_name[:-8]

    task_arns = get_task_arns(service_name)
    if not task_arns:
        logging.info('{0} is NOT RUNNING'.format(service_name))
        return None
    else:
        logging.info('{0} is RUNNING'.format(service_name))

        data = ecs.describe_tasks(task_arns, cluster=cluster)
        tasks = data['DescribeTasksResponse']['DescribeTasksResult']['tasks']
        for task in tasks:
            interface = get_ec2_interface(task['containerInstanceArn'])
            task_info = {
                'ip': interface.private_ip_address,
                'ports': {}
            }

            for container in task['containers']:
                for port in container['networkBindings']:
                    if port['protocol'] == 'tcp':
                        task_info['ports'][port['containerPort']] = port['hostPort']

            info['tasks'].append(task_info)
            info['vpc_id'] = interface.vpc_id

    return info


def update_dns(zone_id, zone_name, service_name, service_ips, ttl=20):
    """
    Insert or update DNS record.
    """

    host_name = '.'.join([service_name, zone_name])
    record_set = boto.route53.record.ResourceRecordSets(route53, zone_id)
    record = record_set.add_change('UPSERT', host_name, 'A', ttl)
    for service_ip in service_ips:
        record.add_value(service_ip)
    record_set.commit()
    return record_set


def update_service(service_name, method, prefix):
    """
    Update DNS to allow discovery of properly named task definitions.

    """

    info = get_service_info(service_name)
    if not info:
        return None

    if method == 'dns':
        network = get_zone_for_vpc(info["vpc_id"])
        ips = [t['ip'] for t in info['tasks']]

        logging.info('Registering {0}.{1} as {2}'.format(
                     info['name'], network['zone_name'], ','.join(ips)))

        update_dns(network['zone_id'], network['zone_name'],
                   info['name'], ips)
    elif method == 'etcd':
        data = json.dumps(info['tasks'])
        logging.info('Registering {0} as {1}'.format(
                     info['name'], data))

        host = requests.get("http://169.254.169.254/latest/meta-data/local-ipv4").content

        client = Client(host=host, port=4001)
        key = '/' + '/'.join([i for i in ['tasks', prefix, info['name']] if i])
        client.node.set(key, data)


def main():
    """
    Main function that handles running the command.
    """

    parser = argparse.ArgumentParser()
    parser.add_argument('service_name', nargs=1,
                        help='list of services to start')
    parser.add_argument('method', nargs=1,
                        help='method of registering service')
    parser.add_argument('-p', '--prefix', action='store', default=False,
                        help='prefix when saving to etcd')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='suppress output')
    parser.add_argument('-r', '--rerun', action='store_true',
                        help='run again after a 60 second pause')
    args = parser.parse_args()

    if not args.quiet:
        logging.getLogger().setLevel(logging.INFO)

    update_service(args.service_name[0], args.method[0], args.prefix)
    if args.rerun:
        sleep(60)
        update_service(args.service_name[0], args.method[0], args.prefix)


if __name__ == '__main__':
    main()
