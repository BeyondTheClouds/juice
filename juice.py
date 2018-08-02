#!/usr/bin/env python

"""Tool to test the performances of MariaDB, Galera and CockroachDB with
OpenStack on Grid'5000

Usage:
    juice [-h | --help] [-v | --version] <command> [<args>...]

Options:
    -h --help      Show this help
    -v --version   Show version number

Commands:
    deploy         Claim resources from g5k and configure them
    openstack      Add OpenStack Keystone to the deployment
    rally          Benchmark the Openstack
    stress         Launch sysbench tests (after a deployment)
    emulate        Emulate network using tc
    backup         Backup the environment
    destroy        Destroy all the running dockers (not the resources)
    info           Show information of the actual deployment
    help           Show this help

Run 'juice COMMAND --help' for more information on a command
"""

import os
import logging
import sys
import pprint
import yaml
import json
import operator
import pickle

from docopt import docopt
from enoslib.api import (generate_inventory, emulate_network,
                         validate_network)
from enoslib.task import enostask, _save_env

from utils import (JUICE_PATH, ANSIBLE_PATH, SYMLINK_NAME, doc,
                   doc_lookup, run_ansible, g5k_deploy)

logging.basicConfig(level=logging.DEBUG)

tc = {
    "enable": True,
    "default_delay": "0ms",
    "default_rate": "10gbit",
    "constraints": [{
        "src": "database",
        "dst": "database",
        "delay": "0ms",
        "rate": "10gbit",
        "loss": "0",
        "network": "database_network",
    }],
    "groups": ['database'],
}

######################################################################
## SCAFFOLDING
######################################################################


@doc()
@enostask()
def deploy(conf, provider='g5k', force_deployment=False, xp_name=None,
           tags=['provide', 'inventory', 'scaffold'], env=None,
           **kwargs):
    """
usage: juice deploy [--conf CONFIG_PATH] [--provider PROVIDER]
                    [--force-deployment]
                    [--xp-name NAME] [--tags TAGS...]

Claim resources from PROVIDER and configure them.

Options:
  --conf CONFIG_PATH    Path to the configuration file describing the
                        deployment [default: ./conf.yaml]
  --provider PROVIDER   Provider to target [default: g5k]
  --force-deployment    Force provider to redo the deployment
  --xp-name NAME        NAME of the folder generated by juice for this
                        new deployment.
  --tags TAGS           Only run tasks relative to the specific tags
                        [default: provide inventory scaffold]
    """
    # Read the configuration
    config = {}

    if isinstance(conf, str):
        # Get the config object from a yaml file
        with open(conf) as f:
            config = yaml.load(f)
    elif isinstance(conf, dict):
        # Get the config object from a dict
        config = conf
    else:
        # Data format error
        raise Exception(
            ('conf is type {!r} while it should be ',
             'a yaml file or a dict').format(type(conf)))

    env['db'] = config.get('database', 'cockroachdb')
    env['monitoring'] = config.get('monitoring', True)
    env['config'] = config

    # Claim resources on Grid'5000
    if 'provide' in tags:
        if provider == 'g5k' and 'g5k' in config:
            env['provider'] = 'g5k'
            updated_env = g5k_deploy(config['g5k'], env=xp_name,
                                     force_deploy=force_deployment)
            env.update(updated_env)
        else:
            raise Exception(
                ('The provider {!r} is not supported or ',
                 'it lacks a configuration').format(provider))

    # Generate the Ansible inventory file
    if 'inventory' in tags:
        env['inventory'] = os.path.join(env['resultdir'], 'hosts')
        generate_inventory(env['roles'] , env['networks'],
                           env['inventory'], check_networks=True)
        _save_env(env)


    # Deploy the resources, requires both g5k and inventory executions
    if 'scaffold' in tags:
        run_ansible('scaffolding.yml', extra_vars={
            'registry':    config['registry'],
            'db':          env['db'],
            'monitoring':  env['monitoring'],
            'enos_action': 'deploy'
            })

@doc()
@enostask()
def backup(backup_dir='current/backup', env=None, **kwargs):
    """
usage: juice backup [--backup-dir DIRECTORY]

Backup the experiment
  --backup-dir DIRECTORY  Backup directory [default: current/backup]
    """
    backup_dir = os.path.abspath(backup_dir)
    os.path.isdir(backup_dir) or os.mkdir(backup_dir)

    extra_vars = {
        "enos_action": "backup",
        "db": env['db'],
        "backup_dir": backup_dir,
        "monitoring": env['monitoring'],
        "rally_nodes": env.get('rally_nodes', [])
    }
    run_ansible('scaffolding.yml', extra_vars=extra_vars)
    run_ansible('openstack.yml', extra_vars=extra_vars)
    run_ansible('rally.yml', extra_vars=extra_vars)


@doc()
@enostask()
def destroy(env=None, hard=False, **kwargs):
    """
usage: juice destroy

Destroy all the running dockers (not destroying the resources), requires g5k
and inventory executions
    """
    extra_vars = {}
    # Call destroy on each component
    extra_vars.update({
        'monitoring': env.get('monitoring', True),
        "db": env.get('db', 'cockroachdb'),
        "rally_nodes": env.get('rally_nodes', []),
        "enos_action": "destroy"
    })
    run_ansible('scaffolding.yml', extra_vars=extra_vars)
    run_ansible('openstack.yml', extra_vars=extra_vars)
    run_ansible('rally.yml', extra_vars=extra_vars)


######################################################################
## Scaffolding ++
######################################################################

@doc()
@enostask()
def openstack(env=None, **kwargs):
    """
usage: juice openstack

Launch OpenStack.
    """
    # Generate inventory
    extra_vars = {
        "db": env['db'],
    }
    # use deploy of each role
    extra_vars.update({"enos_action": "deploy"})
    run_ansible('openstack.yml', extra_vars=extra_vars)


######################################################################
## Stress
######################################################################


@doc()
@enostask()
def stress(env=None, **kwargs):
    """
usage: juice stress

Launch sysbench tests.
    """
    # Generate inventory
    extra_vars = {
        "registry": env["config"]["registry"],
        "db": env.get('db', 'cockroachdb'),
        "enos_action": "stress"
    }
    # use deploy of each role
    run_ansible('stress.yml', extra_vars=extra_vars)


@doc()
@enostask()
def rally(files, directory, high, env=None, **kwargs):
    """
usage: juice rally [--files FILE... | --directory DIRECTORY] [--high]

Benchmark the Openstack

  --files FILE           Files to use for rally scenarios (name must be a path
from rally scenarios folder).
  --directory DIRECTORY  Directory that contains rally scenarios. [default:
keystone]
  --high                 Use high mode or not
    """
    logging.info("Launching rally using scenarios: %s" % (', '.join(files)))
    logging.info("Launching rally using all scenarios in %s directory.",
                 directory)

    database_nodes = [host.address for role, hosts in env['roles'].items()
                                   if role.startswith('database')
                                   for host in hosts]

    # In high mode: runs rally in all database nodes, in light mode:
    # runs rally on one database node. In light mode, we pick the
    # second database node (ie, `database_node[1]`) to not run rally
    # on the same node than the one that contains mariadb.
    rally_nodes = database_nodes if high else database_nodes[1]
    env['rally_nodes'] = rally_nodes

    extra_vars = {
        "rally_nodes": rally_nodes
    }
    if files:
        extra_vars.update({"rally_files": files})
    else:
        extra_vars.update({"rally_directory": directory})

    # use deploy of each role
    extra_vars.update({"enos_action": "deploy"})
    run_ansible('rally.yml', extra_vars=extra_vars)


######################################################################
## Other
######################################################################


@doc(tc)
@enostask()
def emulate(tc=tc, env=None, **kwargs):
    """
usage: juice emulate

Emulate network using: {0}
    """
    inventory = env["inventory"]
    roles = env["roles"]
    logging.info("Emulates using constraints: %s" % tc)
    emulate_network(roles, inventory, tc)
    env["latency"] = tc['constraints'][0]['delay']


@doc()
@enostask()
def validate(env=None, **kwargs):
    """
usage: juice validate

Validate network. Doesn't work for now since there is no flent installed
    """
    inventory = env["inventory"]
    roles = env["roles"]
    validate_network(roles, inventory)


@doc(SYMLINK_NAME)
@enostask()
def info(env, out, **kwargs):
    """
usage: enos info [-e ENV|--env=ENV] [--out=FORMAT]

Show information of the `ENV` deployment.

Options:
  -e ENV --env=ENV         Path to the environment directory. You should use
                           this option when you want to link a
                           specific experiment [default: {0}].
  --out FORMAT             Output the result in either json, pickle or
                           yaml format.
    """
    if not out:
        pprint.pprint(env)
    elif out == 'json':
        print(json.dumps(env, default=operator.attrgetter('__dict__')))
    elif out == 'pickle':
        print(pickle.dumps(env))
    elif out == 'yaml':
        print(yaml.dump(env))
    else:
        print("--out doesn't suppport %s output format" % out)
        print(info.__doc__)

@doc()
def help(**kwargs):
    """
usage: juice help

Show the help message
    """
    print(__doc__)


if __name__ == '__main__':
    args = docopt(__doc__,
                  version='juice version 1.0.0',
                  options_first=True)

    argv = [args['<command>']] + args['<args>']

    doc_lookup(args['<command>'], argv)
