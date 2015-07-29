#!/usr/bin/env python

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
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='suppress output')
    args = parser.parse_args()

    if not args.quiet:
        logging.getLogger().setLevel(logging.INFO)

    host = requests.get("http://169.254.169.254/latest/meta-data/local-ipv4").content
    with open(args.template[0], 'r') as f:
        template_content = f.read()

    while True:
        key = '/tasks/{0}'.format(args.key[0])
        logging.info('Waiting for key change: {0}'.format(key))

        client = Client(host=host, port=4001)
        try:
            data = json.loads(client.node.wait(key).node.value)
        except EtcdWaitFaultException:
            pass
        else:
            logging.info('Writing template: {0}'.format(args.destination[0]))
            template = Template(template_content)
            result = template.render(tasks=data)
            with open(args.destination[0], 'w') as f:
                f.write(result)

            logging.info('Running command: {0}'.format(' '.join(args.command)))
            call(args.command)


if __name__ == '__main__':
    main()

