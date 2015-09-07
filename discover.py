#!/usr/bin/env python

import os
import argparse
import logging
import json
import requests

import boto
import boto.ec2

from socket import gethostbyname
from boto.ec2.address import Address
from etcd.client import Client
from etcd.exceptions import EtcdWaitFaultException
from httplib import IncompleteRead
from jinja2 import Template
from subprocess import call


logging.basicConfig(format='%(asctime)s %(message)s',
                    datefmt='%Y/%m/%d/ %I:%M:%S %p')

region = os.environ.get('ECS_REGION', 'us-east-1')
ec2 = boto.ec2.connect_to_region(region)


def domain2localip(domain):
    public_ip = gethostbyname(domain)
    eip = ec2.get_all_addresses(addresses=[public_ip, ])[0]
    return eip.private_ip_address


def isassociated(domain):
    public_ip = gethostbyname(domain)
    instance_id = requests.get("http://169.254.169.254/latest/meta-data/instance-id").content
    addresses = ec2.get_all_addresses(addresses=[public_ip, ],
                                      filters={"instance-id": instance_id})
    return len(addresses) > 0


def generate_template(template, destination, command, **kwargs):
    with open(template, 'r') as f:
        template_content = f.read()

    kwargs['domain2LocalIP'] = domain2localip
    kwargs['isAssociated'] = isassociated

    logging.info('Writing template: {0}'.format(destination))
    template = Template(template_content)
    result = template.render(**kwargs)
    with open(destination, 'w') as f:
        f.write(result)

    logging.info('Running command: {0}'.format(' '.join(command)))
    call(command)


def main():
    """
    Main function that handles running the command.
    """

    parser = argparse.ArgumentParser()
    parser.add_argument('key', nargs=1,
                        help='etcd key')
    parser.add_argument('template', nargs=1,
                        help='template to render')
    parser.add_argument('destination', nargs=1,
                        help='template testination')
    parser.add_argument('command', nargs='*',
                        help='command to run after generating template')
    parser.add_argument('-e', '--etcd', action='store', default=None,
                        help='etcd host to connect to')
    parser.add_argument('-r', '--recursive', action='store_true',
                        help='prefix when saving to etcd')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='suppress output')
    args = parser.parse_args()

    if not args.quiet:
        logging.getLogger().setLevel(logging.INFO)

    if args.etcd:
        host = gethostbyname(args.etcd)
    else:
        host = requests.get("http://169.254.169.254/latest/meta-data/local-ipv4").content

    if not os.path.isfile(args.template[0]):
        raise Exception('Template does not exist: {0}'.format(args.template[0]))

    client = Client(host=host, port=4001)
    key = '/tasks/{0}'.format(args.key[0])

    logging.info('Checking key: {0}'.format(key))
    if args.recursive:
        response = client.directory.list(key)
        services = [{'service': n.key.split('/')[-1], 'tasks': json.loads(n.value)}
                    for n in response.node.children]
        generate_template(args.template[0], args.destination[0], args.command,
                          services=services)
    else:
        response = client.node.get(key)
        tasks = json.loads(response.node.value)
        generate_template(args.template[0], args.destination[0], args.command,
                          tasks=tasks)

    while True:
        logging.info('Waiting for key change: {0}'.format(key))

        if args.recursive:
            try:
                client.directory.wait(key, recursive=True)
                response = client.directory.list(key)
            except (EtcdWaitFaultException, IncompleteRead):
                pass
            else:
                services = [{'service': n.key.split('/')[-1], 'tasks': json.loads(n.value)}
                            for n in response.node.children]
                generate_template(args.template[0], args.destination[0], args.command,
                                  services=services)
        else:
            try:
                response = client.node.wait(key)
            except (EtcdWaitFaultException, IncompleteRead):
                pass
            else:
                tasks = json.loads(response.node.value)
                generate_template(args.template[0], args.destination[0], args.command,
                                  tasks=tasks)


if __name__ == '__main__':
    main()
