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
import boto
import boto.ec2
import boto.route53


region = os.environ.get('ECS_REGION', 'us-east-1')
ecs = boto.connect_ec2containerservice(
        host='ecs.{0}.amazonaws.com'.format(region))
ec2 = boto.ec2.connect_to_region(region)
route53 = boto.route53.connect_to_region(region)

logging.basicConfig(format='%(asctime)s %(message)s',
                    datefmt='%Y/%m/%d/ %I:%M:%S %p')
log = logging.info

if 'ECS_CLUSTER' in os.environ:
    cluster = os.environ['ECS_CLUSTER']
elif os.path.exists('/etc/ecs/ecs.config'):
    pat = re.compile(r'\bECS_CLUSTER\b\s*=\s*(\w*)')
    cluster = pat.findall(open('/etc/ecs/ecs.config').read())[-1]
else:
    cluster = None


def get_task_definition_arns():
    """Request all API pages needed to get Task Definition ARNS."""
    next_token = []
    arns = []
    while next_token is not None:
        detail = ecs.list_task_definitions(next_token=next_token)
        detail = detail['ListTaskDefinitionsResponse']
        detail = detail['ListTaskDefinitionsResult']
        arns.extend(detail['taskDefinitionArns'])
        next_token = detail['nextToken']
    return arns


def get_task_definition_families():
    """Ignore duplicate tasks in the same family."""
    arns = get_task_definition_arns()
    families = {}
    for arn in arns:
        match = pattern_arn.match(arn)
        if match:
            groupdict = match.groupdict()
            families[groupdict['family']] = True
    return families.keys()


def get_task_arns(family):
    """Get the ARN of running task, given the family name."""
    response = ecs.list_tasks(cluster=cluster, family=family)
    arns = response['ListTasksResponse']['ListTasksResult']['taskArns']
    if len(arns) == 0:
        return None
    return arns


def get_task_container_instance_arn(task_arn):
    """Get the ARN for the container instance a give task is running on."""
    response = ecs.describe_tasks(task_arn, cluster=cluster)
    response = response['DescribeTasksResponse']
    return response['DescribeTasksResult']['tasks'][0]['containerInstanceArn']


def get_container_instance_ec2_id(container_instance_arn):
    """Id the EC2 instance serving as the container instance."""
    detail = ecs.describe_container_instances(
        container_instances=container_instance_arn, cluster=cluster)
    detail = detail['DescribeContainerInstancesResponse']
    detail = detail['DescribeContainerInstancesResult']['containerInstances']
    return detail[0]['ec2InstanceId']


def get_ec2_interface(ec2_instance_id):
    """Get the primary interface for the given EC2 instance."""
    return ec2.get_all_instances(filters={
        'instance-id': ec2_instance_id})[0].instances[0].interfaces[0]


def get_zone_for_vpc(vpc_id):
    """Identify the Hosted Zone for the given VPC.

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


def get_service_info(family):
    info = {'vpc_id': None, "name": family[:-8], "service_ips": []}

    if family[-8:] != '-service':
        log('    (Found non-service {0})'.format(family))
        return None

    log('{0} service found'.format(family))

    service_arns = get_task_arns(family)
    if not service_arns:
        log('{0} is not running'.format(family))
    else:
        log('{0} is RUNNING'.format(family))

        for service_arn in service_arns:
            container_arn = get_task_container_instance_arn(service_arn)
            ec2_instance_id = get_container_instance_ec2_id(container_arn)
            ec2_interface = get_ec2_interface(ec2_instance_id)

            info['vpc_id'] = ec2_interface.vpc_id
            info['service_ips'].append(ec2_interface.private_ip_address)

    return info


def get_info():
    """Get all needed info about running services."""
    info = {'services': [], 'network': {'cluster': cluster}}
    families = get_task_definition_families()
    for family in families:
        service_info = get_service_info(family)
        if not service_info:
            continue
        # No need to get common network info on each loop over tasks
        if 'vpc_id' not in info['network']:
            info['network'].update(get_zone_for_vpc(service_info["vpc_id"]))
            info['network']['vpc_id'] = service_info["vpc_id"]
        info['services'].append(service_info)
    return info


def dns(zone_id, zone_name, service_name, service_ips, ttl=20):
    """Insert or update DNS record."""
    host_name = '.'.join([service_name, zone_name])

    record_set = boto.route53.record.ResourceRecordSets(route53, zone_id)
    record = record_set.add_change('UPSERT', host_name, 'A', ttl)
    for service_ip in service_ips:
        record.add_value(service_ip)
    record_set.commit()
    return record_set


def update_services(service_names=[], verbose=False):
    """Update DNS to allow discovery of properly named task definitions.

    If service_names are provided only update those services.
    Otherwise update all.
    """
    info = get_info()
    for service in info['services']:
        if (service_names and
                service['family'] not in service_names and
                service['name'] not in service_names):
            continue

        if verbose:
            log('Registering {0}.{1} as {2}'.format(
                service['name'], info['network']['zone_name'],
                ','.join(service['service_ips'])))

        dns(info['network']['zone_id'], info['network']['zone_name'],
            service['name'], service['service_ips'])


def cli():
    """Used by entry_point console_scripts."""
    parser = argparse.ArgumentParser()
    parser.add_argument('service_names', nargs='*',
                        help='list of services to start')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='supress output')
    args = parser.parse_args()
    if not args.quiet:
        logging.getLogger().setLevel(logging.INFO)
    update_services(args.service_names, True)

pattern_arn = re.compile(
    'arn:'
    '(?P<partition>[^:]+):'
    '(?P<service>[^:]+):'
    '(?P<region>[^:]*):'   # region is optional
    '(?P<account>[^:]*):'  # account is optional
    '(?P<resourcetype>[^:/]+)([:/])'
    '(?P<resource>('
        '(?P<family>[^:]+):'     # noqa
        '(?P<version>[^:]+)|.*'  # noqa
    '))')

if __name__ == '__main__':
    cli()
