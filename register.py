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
import boto3
from time import sleep

from botocore.exceptions import ClientError


def find(service_name, container_name, cluster='default', private=True):
    """
    finds the IP address of the currently running tasks of the service.
    :return None, if no tasks are running, or [list of IP addresses]
    """
    client = boto3.client('ecs')
    cluster = os.environ.get('CLUSTER', cluster)
    ipaddresses = []
    try:
        response = client.list_tasks(
            cluster=cluster, serviceName=service_name, desiredStatus='RUNNING'
        )
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise ValueError(response['ResponseMetadata'])

        response = client.describe_tasks(cluster=cluster,
                                         tasks=response['taskArns'])
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise ValueError(response['ResponseMetadata'])

        container_instance_arns = []
        for task in response['tasks']:
            for container in task['containers']:
                if container['name'] != container_name and \
                        not container['name'].startswith(container_name):
                    continue
                if container['lastStatus'] != 'RUNNING':
                    continue
                container_instance_arns.append(task['containerInstanceArn'])

        response = client.describe_container_instances(
            cluster=cluster, containerInstances=container_instance_arns
        )
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise ValueError(response['ResponseMetadata'])

        ec2 = boto3.resource('ec2')
        for instance in response['containerInstances']:
            inst = ec2.Instance(instance['ec2InstanceId'])
            if private and inst.private_ip_address is not None:
                ipaddresses.append(inst.private_ip_address)
            elif inst.public_ip_address is not None:
                ipaddresses.append(inst.public_ip_address)

    except (IndexError, ClientError):
        msg = 'Service {0} Container Name: {1} not found in Cluster {2}'.format(
            service_name, container_name, cluster
        )
        raise Exception(msg)
    return ipaddresses


def register(service_name, container_name, dns_entry,
             cluster='default', private=True):
    """
    register creates `A` records for a given a service, and
    (running) container, by using find in either private or public DNS entries
    in Route 53
    """
    route53 = boto3.client('route53')
    response = route53.list_hosted_zones(MaxItems='50')
    if response['ResponseMetadata']['HTTPStatusCode'] != 200:
        raise ValueError(response['ResponseMetadata'])


    # Resource Records from Route53 are period terminated. So
    # blah.threatanalytics.io shows up as blah.threatanalytics.io.
    if not dns_entry.endswith('.'):
        dns_entry += '.'

    zone_name = '.'.join(dns_entry.split('.')[1:])
    zone = get_zone(response['HostedZones'], zone_name, private)

    if zone is None:
        raise AttributeError('No hosted zone for {0}, searched: {1}'.format(
            dns_entry, zone_name
        ))

    response = route53.list_resource_record_sets(
        HostedZoneId=zone['Id'],
        StartRecordName=dns_entry,
        StartRecordType='A'
    )
    if response['ResponseMetadata']['HTTPStatusCode'] != 200:
        raise ValueError(response['ResponseMetadata'])

    existing = []

    for recordset in response['ResourceRecordSets']:
        print('Comparing {0} and {1}'.format(recordset['Name'], dns_entry))
        if recordset['Name'] == dns_entry:  # should hit on first
            for rs in recordset['ResourceRecords']:
                existing.append(rs['Value'])
            break

    new = find(service_name, container_name, cluster)
    if set(existing) != set(new):  # set resource records
        resource_record = {
            'Name': dns_entry,
            'ResourceRecords': [{'Value': address} for address in new],
            'TTL': 20,
            'Type': 'A'
        }
        response = route53.change_resource_record_sets(
            HostedZoneId=zone['Id'],
            ChangeBatch={
                'Comment': 'updated by ecs register container',
                'Changes': [
                    {
                        'Action': 'UPSERT',
                        'ResourceRecordSet': resource_record
                    }
                ]
            }
        )

    return (response['ResponseMetadata']['HTTPStatusCode'],
            response['ResponseMetadata'])


def register_cname(dns_entry, target, private=True):
    route53 = boto3.client('route53')
    response = route53.list_hosted_zones(MaxItems='50')
    if response['ResponseMetadata']['HTTPStatusCode'] != 200:
        raise ValueError(response['ResponseMetadata'])

    # Resource Records from Route53 are period terminated. So
    # blah.threatanalytics.io shows up as blah.threatanalytics.io.
    if not dns_entry.endswith('.'):
        dns_entry += '.'

    zone_name = '.'.join(dns_entry.split('.')[1:])
    zone = get_zone(response['HostedZones'], zone_name, private)

    if zone is None:
        raise AttributeError('No hosted zone for {0}, searched: {1}'.format(
            dns_entry, zone_name
        ))

    response = route53.list_resource_record_sets(
        HostedZoneId=zone['Id'],
        StartRecordName=dns_entry,
        StartRecordType='CNAME'
    )
    if response['ResponseMetadata']['HTTPStatusCode'] != 200:
        raise ValueError(response['ResponseMetadata'])

    for recordset in response['ResourceRecordSets']:
        print('Comparing {0} and {1}'.format(recordset['Name'], dns_entry))
        if recordset['Name'] == dns_entry:  # should hit on first
            for rs in recordset['ResourceRecords']:
                if rs['Value'] == target:
                    return None # CNAME is already in desired state
            break

    resource_record = {
        'Name': dns_entry,
        'ResourceRecords': [{'Value': target}],
        'TTL': 20,
        'Type': 'CNAME'
    }

    response = route53.change_resource_record_sets(
        HostedZoneId=zone['Id'],
        ChangeBatch={
            'Comment': 'updated by ecs register container',
            'Changes': [
                {
                    'Action': 'UPSERT',
                    'ResourceRecordSet': resource_record
                }
            ]
        }
    )

    return (response['ResponseMetadata']['HTTPStatusCode'],
            response['ResponseMetadata'])


def get_zone(zones, zone_name, private):
    # for dns_entry 'elasticsearch.internal.advancedthreatanalytics.com'
    # extract internal.advancedthreatanalytics.com and see if we have a
    # zone for that
    for zone in zones:
        if zone['Name'] != zone_name:
            continue
        elif zone['Config']['PrivateZone'] == private:
            return zone

    return None


def update_service(family, cluster, container, dns, cname=None):
    """
    Update DNS to allow discovery of properly named task definitions.
    """

    ips = find(family, container, cluster=cluster)

    if not ips:
        return None

    print 'Registering {0}:{1} in cluster {2} as {3}'.format(
        family, container, cluster, dns
    )
    register(family, container, dns, cluster=cluster)

    if cname:
        print 'Registering {0} as CNAME for {1}'.format(cname, dns)
        register_cname(dns_entry=cname, target=dns)


def main():
    """
    Main function that handles running the command.

    register    --service=portalapi \
                --
                --dns=somethingelse.threatanalytics.io \
                --cluster=ATAProduction \
                --cname=somethingelse.internal.ata.com \
                --comment=This is some comment
    """

    parser = argparse.ArgumentParser()
    parser.add_argument('--family', dest='family',
                        help='service family to register')
    parser.add_argument('--dns', dest='dns',
                        help='fqdn to register this service to')
    parser.add_argument('--cluster', dest='cluster',
                        help='cluster in which the service is deployed')
    parser.add_argument('--cname', dest='cname', default='',
                        help='cname to register the service to in the private'
                             ' network')
    parser.add_argument('--container', dest='container',
                        help='name of the docker container we are registering')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='suppress output')
    parser.add_argument('-r', '--rerun', action='store_true',
                        help='run again after a 60 second pause')
    args = parser.parse_args()

    if not args.quiet:
        logging.getLogger().setLevel(logging.INFO)

    update_service(family=args.family,
                   container=args.container,
                   cluster=args.cluster,
                   dns=args.dns,
                   cname=args.cname)
    if args.rerun:
        sleep(60)
        update_service(family=args.family,
                       container=args.container,
                       cluster=args.cluster,
                       dns=args.dns,
                       cname=args.cname)


if __name__ == '__main__':
    main()
