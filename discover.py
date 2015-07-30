#!/usr/bin/env python

import os
import argparse
import logging
import json
import requests

from etcd.client import Client
from etcd.exceptions import EtcdWaitFaultException
from jinja2 import Template
from subprocess import call


logging.basicConfig(format='%(asctime)s %(message)s',
                    datefmt='%Y/%m/%d/ %I:%M:%S %p')


def generate_template(template, destination, command, **kwargs):
    with open(template, 'r') as f:
        template_content = f.read()

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
    parser.add_argument('-r', '--recursive', action='store_true',
                        help='prefix when saving to etcd')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='suppress output')
    args = parser.parse_args()

    if not args.quiet:
        logging.getLogger().setLevel(logging.INFO)

    host = requests.get("http://169.254.169.254/latest/meta-data/local-ipv4").content
    if not os.path.isfile(args.template[0]):
        raise Exception('Template does not exist: {0}'.format(args.template[0]))

    while True:
        key = '/tasks/{0}'.format(args.key[0])
        logging.info('Waiting for key change: {0}'.format(key))

        client = Client(host=host, port=4001)
        if args.recursive:
            try:
                client.directory.wait(key, recursive=True)
                response = client.directory.list(key)
            except EtcdWaitFaultException:
                pass
            else:
                services = [{'service': n.key.split('/')[-1], 'tasks': json.loads(n.value)}
                            for n in response.node.children]
                generate_template(args.template[0], args.destination[0], args.command,
                                  services=services)
        else:
            try:
                response = client.node.wait(key)
            except EtcdWaitFaultException:
                pass
            else:
                tasks = json.loads(response.node.value)
                generate_template(args.template[0], args.destination[0], args.command,
                                  tasks=tasks)


if __name__ == '__main__':
    main()

