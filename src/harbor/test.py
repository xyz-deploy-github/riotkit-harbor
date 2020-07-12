import os
import sys
import unittest
import subprocess
import time
import yaml
from io import StringIO
from typing import Dict
from copy import deepcopy
from argparse import ArgumentParser
from rkd.contract import ExecutionContext
from rkd.context import ApplicationContext
from rkd.executor import OneByOneTaskExecutor
from rkd.inputoutput import IO
from rkd.inputoutput import BufferedSystemIO
from rkd.syntax import TaskDeclaration
from .tasks.base import HarborBaseTask
from .service import ServiceDeclaration
from .driver import ComposeDriver
from .cached_loader import CachedLoader
from dotenv import dotenv_values

HARBOR_MODULE_PATH = os.path.dirname(os.path.realpath(__file__))
ENV_SIMPLE_PATH = os.path.dirname(os.path.realpath(__file__)) + '/../../test/testdata/env_simple'
CURRENT_TEST_ENV_PATH = os.path.dirname(os.path.realpath(__file__)) + '/../../test/testdata/current_test_env'
TEST_PROJECT_NAME = 'env_simple'


class TestTask(HarborBaseTask):
    is_dev_env = True

    def get_name(self) -> str:
        return ':test'

    def get_group_name(self) -> str:
        return ''

    def execute(self, context: ExecutionContext) -> bool:
        return True

    def run(self, context: ExecutionContext) -> bool:
        return True

    def configure_argparse(self, parser: ArgumentParser):
        pass


def create_mocked_task(io: IO) -> TestTask:
    task = TestTask()
    ctx = ApplicationContext([], [], '')
    ctx.io = io

    task.internal_inject_dependencies(
        io=io,
        ctx=ctx,
        executor=OneByOneTaskExecutor(ctx=ctx)
    )

    return task


class BaseHarborTestClass(unittest.TestCase):
    _stderr_bckp = None
    _stdout_bckp = None

    def setUp(self) -> None:
        print('')
        print('==================================================================================================' +
              '=====================================')
        print('Test name: ' + self.__class__.__name__ + ' :: ' + self._testMethodName)
        print('----------')
        print('')

        CachedLoader.clear()   # avoid keeping the state between tests

        os.chdir(HARBOR_MODULE_PATH)
        self.recreate_structure()
        self.setup_environment()
        self.remove_all_containers()
        self._stderr_bckp = sys.stderr
        self._stdout_bckp = sys.stdout

    def tearDown(self) -> None:
        if sys.stderr != self._stderr_bckp or sys.stdout != self._stdout_bckp:
            print('!!! Test ' + self.id() + ' is not cleaning up stdout/stderr')

        self._restore_streams()

    @classmethod
    def mock_compose(cls, content: dict):
        content['version'] = '3.4'

        with open(CURRENT_TEST_ENV_PATH + '/apps/conf/mocked.yaml', 'wb') as f:
            f.write(yaml.dump(content).encode('utf-8'))

    def _restore_streams(self):
        sys.stderr = self._stderr_bckp
        sys.stdout = self._stdout_bckp

    @classmethod
    def recreate_structure(cls):
        """Within each class recreate the project structure, as it could be changed by tests itself"""

        subprocess.check_call(['rm', '-rf', CURRENT_TEST_ENV_PATH])
        subprocess.check_call(['cp', '-pr', ENV_SIMPLE_PATH, CURRENT_TEST_ENV_PATH])

        # copy from base structure - as we test eg. things like default configurations, NGINX template
        for directory in ['containers', 'data', 'hooks.d', 'apps/www-data']:
            subprocess.check_call('rm -rf %s/%s' % (ENV_SIMPLE_PATH, directory), shell=True)
            subprocess.check_call('cp -pr %s/project/%s %s/%s' % (
                HARBOR_MODULE_PATH, directory, CURRENT_TEST_ENV_PATH, directory
            ), shell=True)

        cls.mock_compose({'services': {}})

    @classmethod
    def get_test_env_subdirectory(cls, subdir_name: str):
        directory = CURRENT_TEST_ENV_PATH + '/' + subdir_name

        if not os.path.isdir(directory):
            subprocess.check_call(['mkdir', '-p', directory])

        return os.path.realpath(directory)

    @classmethod
    def remove_all_containers(cls):
        try:
            subprocess.check_output("docker rm -f -v $(docker ps -a --format '{{ .Names }}' | grep " + TEST_PROJECT_NAME + ")",
                                    shell=True, stderr=subprocess.STDOUT)

        except subprocess.CalledProcessError as e:
            # no containers found - it's OK
            if "requires at least 1 argument" in str(e.output):
                return

            raise e

    @classmethod
    def setup_environment(cls):
        os.environ.update(dotenv_values(CURRENT_TEST_ENV_PATH + '/.env'))
        os.environ['APPS_PATH'] = CURRENT_TEST_ENV_PATH + '/apps'
        os.environ['RKD_PATH'] = cls.get_test_env_subdirectory('') + ':' + HARBOR_MODULE_PATH + '/internal'

        os.chdir(CURRENT_TEST_ENV_PATH)

    def _get_prepared_compose_driver(self, args: dict = {}, env: dict = {}) -> ComposeDriver:
        merged_env = deepcopy(os.environ)
        merged_env.update(env)

        task = create_mocked_task(BufferedSystemIO())
        declaration = TaskDeclaration(task)
        ctx = ExecutionContext(declaration, args=args, env=merged_env)

        return ComposeDriver(task, ctx, TEST_PROJECT_NAME)

    def execute_task(self, task: HarborBaseTask, args: dict = {}, env: dict = {}) -> str:
        ctx = ApplicationContext([], [], '')
        ctx.io = BufferedSystemIO()

        task.internal_inject_dependencies(
            io=ctx.io,
            ctx=ctx,
            executor=OneByOneTaskExecutor(ctx=ctx)
        )

        merged_env = deepcopy(os.environ)
        merged_env.update(env)

        r_io = IO()
        str_io = StringIO()

        with r_io.capture_descriptors(enable_standard_out=True, stream=str_io):
            result = task.execute(ExecutionContext(
                TaskDeclaration(task),
                args=args,
                env=merged_env
            ))

        return ctx.io.get_value() + "\n" + str_io.getvalue() + "\nTASK_EXIT_RESULT=" + str(result)

    @staticmethod
    def prepare_service_discovery(driver: ComposeDriver):
        driver.up(ServiceDeclaration('gateway', {}), capture=True, force_recreate=True)
        driver.up(ServiceDeclaration('gateway_proxy_gen', {}), capture=True, force_recreate=True)
        driver.up(ServiceDeclaration('website', {}), capture=True)

    def prepare_example_service(self, name: str, uses_service_discovery: bool = False) -> ComposeDriver:
        drv = self._get_prepared_compose_driver()

        # prepare
        drv.rm(ServiceDeclaration(name, {}))
        drv.up(ServiceDeclaration(name, {}))

        if uses_service_discovery:
            # give service discovery some time
            # @todo: This can be improved possibly
            time.sleep(5)

        return drv

    def get_containers_state(self, driver: ComposeDriver) -> Dict[str, bool]:
        running_rows = driver.scope.sh('docker ps -a --format "{{ .Names }}|{{ .Status }}"', capture=True).split("\n")
        containers = {}

        for container_row in running_rows:
            try:
                name, status = container_row.split('|')
            except ValueError:
                continue

            if name.startswith(driver.project_name + '_'):
                containers[name] = 'Up' in status

        return containers

    def get_locally_pulled_docker_images(self) -> list:
        images = subprocess.check_output(['docker', 'images', '--format', '{{ .Repository }}:{{ .Tag }}'])\
            .decode('utf-8')\
            .split("\n")

        return images

    def exec_in_container(self, container_name: str, cmd: list) -> str:
        return subprocess.check_output(
            ['docker', 'exec', '-i', container_name] + cmd,
            stderr=subprocess.STDOUT
        ).decode('utf-8')

    def fetch_page_content(self, host: str):
        return self.exec_in_container(TEST_PROJECT_NAME + '_gateway_1', ['curl', '-s', '-vv', '--header',
                                                                         'Host: %s' % host, 'http://127.0.0.1'])

    def assertContainerIsNotRunning(self, service_name: str, driver: ComposeDriver):
        container_name_without_instance_num = driver.project_name + '_' + service_name + '_'

        for name, state in self.get_containers_state(driver).items():
            if name.startswith(container_name_without_instance_num) and state is True:
                self.fail('"%s" is running, but should not' % name)

    def assertLocalRegistryHasImage(self, image_name):
        self.assertIn(image_name, self.get_locally_pulled_docker_images(),
                      msg='Expected that "docker images" will contain image "%s"' % image_name)

    def assertLocalRegistryHasNoPulledImage(self, image_name):
        self.assertNotIn(image_name, self.get_locally_pulled_docker_images(),
                         msg='Expected that "docker images" will not contain image "%s"' % image_name)
