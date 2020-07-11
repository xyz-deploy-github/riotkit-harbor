import os
import subprocess
import pkg_resources
from abc import ABC
from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import StrictUndefined
from jinja2.exceptions import UndefinedError
from typing import Dict
from typing import Tuple
from argparse import ArgumentParser
from rkd.contract import ExecutionContext
from rkd.yaml_parser import YamlFileLoader
from .base import HarborBaseTask
from ..formatting import development_formatting

HARBOR_ROOT = os.path.dirname(os.path.realpath(__file__)) + '/../deployment/files'


class BaseDeploymentTask(HarborBaseTask, ABC):
    ansible_dir: str = '.rkd/deployment'
    _config: dict

    def _silent_mkdir(self, path: str):
        try:
            os.mkdir(path)
        except FileExistsError:
            pass

    def get_config(self) -> dict:
        try:
            self._config
        except AttributeError:
            # try .yml, then .yaml
            try:
                self._config = YamlFileLoader(self._ctx.directories).load_from_file(
                    'deployment.yml',
                    'org.riotkit.harbor/deployment/v1'
                )
            except FileNotFoundError as e:
                self._config = YamlFileLoader(self._ctx.directories).load_from_file(
                    'deployment.yaml',
                    'org.riotkit.harbor/deployment/v1'
                )

        return self._config

    def get_harbor_version(self) -> str:
        try:
            return pkg_resources.get_distribution("harbor").version
        except:
            return 'dev'

    def _verify_synced_version(self, abs_ansible_dir: str):
        """Verifies last synchronization - displays warning if Harbor version was changed after last
        files synchronization"""

        if not os.path.isfile(abs_ansible_dir + '/.synced'):
            return

        with open(abs_ansible_dir + '/.synced', 'rb') as f:
            synced_version = f.read().decode('utf-8').strip()
            actual_version = self.get_harbor_version()

            if synced_version != actual_version:
                self.io().warn('Ansible deployment in .rkd/deployment is not up-to-date. We recommend to update' +
                               ' from %s to %s' % (synced_version, actual_version))

    def _write_synced_version(self, abs_ansible_dir: str):
        with open(abs_ansible_dir + '/.synced', 'wb') as f:
            f.write(self.get_harbor_version().encode('utf-8'))

    def role_is_installed_and_configured(self) -> bool:
        return os.path.isfile(self.ansible_dir + '/.synced')

    def install_and_configure_role(self, force_update: bool = False) -> bool:
        """Install an Ansible role from galaxy, and configure playbook, inventory, all the needed things"""

        abs_ansible_dir = os.path.realpath(self.ansible_dir)
        should_update = force_update or not os.path.isfile(abs_ansible_dir + '/.synced')

        self.io().info('Checking role installation...')
        self._silent_mkdir(abs_ansible_dir)
        self._verify_synced_version(abs_ansible_dir)

        if not self._synchronize_structure_from_template(abs_ansible_dir, only_jinja_templates=True):
            self.io().error_msg('Cannot synchronize templates')
            return False

        if should_update:
            self.io().info('Role will be updated')

            if not self._synchronize_structure_from_template(abs_ansible_dir):
                self.io().error_msg('Cannot synchronize structure')
                return False

            self.io().debug('Downloading fresh role...')
            subprocess.check_call([
                'ansible-galaxy',
                'install', '-r', self.ansible_dir + '/requirements.yml',
                '-p', self.ansible_dir + '/roles/',
                '--force'
            ])

            self._write_synced_version(abs_ansible_dir)

        return True

    def _synchronize_structure_from_template(self, abs_ansible_dir: str, only_jinja_templates: bool = False) -> bool:
        self.io().debug(
            'Synchronizing structure from template (only_jinja_templates=' + str(only_jinja_templates) + ')')

        # synchronize directory structure
        for root, subdirs, files in os.walk(HARBOR_ROOT):
            relative_root = root[len(HARBOR_ROOT) + 1:]

            self._silent_mkdir(abs_ansible_dir + '/' + relative_root)

            for file in files:
                if only_jinja_templates and not file.endswith('.j2'):
                    continue

                abs_src_file_path = root + '/' + file
                abs_dest_file_path = abs_ansible_dir + '/' + relative_root + '/' + file

                if not self._copy_file(abs_src_file_path, abs_dest_file_path):
                    self.io().error('Cannot process file %s' % abs_dest_file_path)
                    return False

        return True

    def _copy_file(self, abs_src_file_path: str, abs_dest_file_path: str):
        """Copies a file from template directory - supports jinja2 files rendering on-the-fly"""

        if abs_dest_file_path.endswith('.j2'):
            abs_dest_file_path = abs_dest_file_path[:-3]

            with open(abs_src_file_path, 'rb') as f:
                tpl = Environment(loader=FileSystemLoader(['./', './rkd/deployment']), undefined=StrictUndefined)\
                        .from_string(f.read().decode('utf-8'))

            try:
                variables = self._prepare_variables()

                with open(abs_dest_file_path, 'wb') as f:
                    f.write(tpl.render(**variables).encode('utf-8'))
            except UndefinedError as e:
                self.io().error(str(e) + " - required in " + abs_src_file_path + ", please define it in deployment.yml")
                return False

            return True

        subprocess.check_call(['cp', '-p', abs_src_file_path, abs_dest_file_path])
        self.io().debug('Created ' + abs_dest_file_path)
        return True

    def _prepare_variables(self):
        variables = {}
        variables.update(os.environ)
        variables.update(self.get_config())

        if 'git_url' not in variables:
            variables['git_url'] = subprocess\
                .check_output(['git', 'config', '--get', 'remote.origin.url']).decode('utf-8')\
                .replace('\n', '')\
                .strip()

        if 'git_secret_url' not in variables:
            variables['git_secret_url'] = variables['git_url'].replace('\n', '')

        return variables


class UpdateFilesTask(BaseDeploymentTask):
    """Update an Ansible role and required configuration files.
    Warning: Overwrites existing files, but does not remove custom files in '.rkd/deployment' directory"""

    def get_name(self) -> str:
        return ':update'

    def get_group_name(self) -> str:
        return ':harbor:deployment:files'

    def format_task_name(self, name) -> str:
        return development_formatting(name)

    def run(self, context: ExecutionContext) -> bool:
        return self.install_and_configure_role(force_update=True)


class DeploymentTask(BaseDeploymentTask):
    """Deploys your project from GIT to a PRODUCTION server

    All changes needs to be COMMITED and PUSHED to GIT server, the task does not copy local files.

    The deployment task can be extended by environment variables and switches to make possible any customizations
    such as custom playbook, custom role or a custom inventory. The environment variables from .env are considered.

    Example usage:
        # deploy services matching profile "gateway", use password stored in .vault-apssword for Ansible Vault
        harbor :deployment:apply -V .vault-password --profile=gateway

        # deploy from different branch
        harbor :deployment:apply --branch production_fix_1

        # use SSH-AGENT & key-based authentication by specifying path to private key
        harbor :deployment:apply --git-key=~/.ssh/id_rsa
    """

    def get_name(self) -> str:
        return ':apply'

    def get_group_name(self) -> str:
        return ':harbor:deployment'

    def format_task_name(self, name) -> str:
        return development_formatting(name)

    def get_declared_envs(self) -> Dict[str, str]:
        envs = super(DeploymentTask, self).get_declared_envs()
        envs['PLAYBOOK'] = 'harbor.playbook.yml'
        envs['INVENTORY'] = 'harbor.inventory.cfg'
        envs['GIT_KEY'] = ''

        return envs

    def configure_argparse(self, parser: ArgumentParser):
        parser.add_argument('--playbook', '-p', help='Playbook name', default='harbor.playbook.yml')
        parser.add_argument('--git-key', '-k', help='Path to private key for a git repository eg. ~/.ssh/id_rsa',
                            default='')
        parser.add_argument('--inventory', '-i', help='Inventory filename', default='harbor.inventory.cfg')
        parser.add_argument('--debug', '-d', action='store_true', help='Set increased logging for Ansible output')
        parser.add_argument('--vault-passwords', '-V', help='Vault passwords separated by "||" eg. 123||456',
                            default='')
        parser.add_argument('--branch', '-b', help='Git branch to deploy from', default='master')
        parser.add_argument('--profile', help='Harbor profile to filter out services that needs to be deployed',
                            default='')

    def run(self, context: ExecutionContext) -> bool:
        playbook_name = context.get_arg_or_env('--playbook')
        inventory_name = context.get_arg_or_env('--inventory')
        git_private_key_path = context.get_arg_or_env('--git-key')
        branch = context.get_arg('--branch')
        profile = context.get_arg('--profile')
        debug = context.get_arg('--debug')
        vault_passwords = context.get_arg('--vault-passwords').split('||') \
            if context.get_arg('--vault-passwords') else []

        if not self.role_is_installed_and_configured():
            self.io().error_msg('Deployment unconfigured. Use `harbor :deployment:role:update` first')
            return False

        self.install_and_configure_role(force_update=False)
        pwd_backup = os.getcwd()
        os.chdir(self.ansible_dir)
        pid = None

        try:
            command = ''
            opts = ''

            if git_private_key_path:
                sock, pid = self.spawn_ssh_agent()
                command += 'export SSH_AUTH_SOCK=%s; export SSH_AGENT_PID=%i; ssh-add %s; sleep 5; ' % \
                           (sock, pid, git_private_key_path)

            if debug:
                opts += ' -vv '

            opts += ' -e git_branch="%s" ' % branch
            opts += ' -e harbor_deployment_profile="%s" ' % profile

            if vault_passwords:
                num = 0
                for passwd in vault_passwords:
                    num = num + 1

                    if os.path.isfile('../../' + passwd):
                        opts += ' --vault-password-file="%s" ' % ('../../' + passwd)
                    else:
                        opts += ' --vault-id="%i@%s" ' % (num, passwd)

            command += 'ansible-playbook ./%s -i %s %s' % (
                playbook_name,
                inventory_name,
                opts
            )

            self.sh(command)
        finally:
            os.chdir(pwd_backup)

            if pid:
                self.kill_ssh_agent(pid)

        return True

    def spawn_ssh_agent(self) -> Tuple[str, int]:
        out = subprocess.check_output('eval $(ssh-agent -s);echo "|${SSH_AUTH_SOCK}|${SSH_AGENT_PID}";', shell=True).decode('utf-8')
        parts = out.split('|')
        sock = parts[1]
        pid = int(parts[2].strip())

        self.io().debug('Spawned ssh-agent - sock=%s, pid=%i' % (sock, pid))

        return sock, pid

    def kill_ssh_agent(self, pid: int):
        self.io().debug('Clean up - killing ssh-agent at PID=%i' % pid)
        subprocess.check_call(['kill', str(pid)])


class CreateExampleDeploymentFileTask(HarborBaseTask):
    """Create a example deployment.yml file"""

    def get_group_name(self) -> str:
        return ':harbor:deployment'

    def get_name(self) -> str:
        return ':create-example'

    def format_task_name(self, name) -> str:
        return development_formatting(name)

    def run(self, context: ExecutionContext) -> bool:
        if os.path.isfile('./deployment.yml') or os.path.isfile('./deployment.yaml'):
            self.io().error_msg('deployment.yml or deployment.yaml already exists')
            return False

        subprocess.check_call(['cp', HARBOR_ROOT + '/../examples/deployment.yml', './deployment.yml'])
        self.io().success_msg('File "deployment.yml" created.')
        self.io().print_line()
        self.io().info_msg('The example is initially adjusted to work with Vagrant test virtual machine.')
        self.io().info_msg(' - `harbor :deployment:vagrant -c "up --provision"` to bring machine up')
        self.io().info_msg(' - `harbor :deployment:apply --git-key=~/.ssh/id_rsa` to perform a test deployment')

        return True


class ManageVagrantTask(BaseDeploymentTask):
    """Controls a test virtual machine using Vagrant"""

    def get_group_name(self) -> str:
        return ':harbor:deployment'

    def get_name(self) -> str:
        return ':vagrant'

    def format_task_name(self, name) -> str:
        return development_formatting(name)

    def configure_argparse(self, parser: ArgumentParser):
        parser.add_argument('--cmd', '-c', required=True, help='Vagrant commandline')

    def run(self, context: ExecutionContext) -> bool:
        cmd = context.get_arg('--cmd')

        try:
            subprocess.check_call('cd %s && vagrant %s' % (self.ansible_dir, cmd), shell=True)

        except subprocess.CalledProcessError:
            return False

        return True

