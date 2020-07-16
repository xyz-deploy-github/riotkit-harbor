import os
from rkd.contract import ExecutionContext
from rkd.standardlib import CreateStructureTask

HARBOR_PATH = os.path.dirname(os.path.realpath(__file__)) + '/..'


class CreateHarborStructureTask(CreateStructureTask):
    """Create a new Harbor project structure"""

    def get_group_name(self) -> str:
        return ':harbor:create'

    def get_name(self) -> str:
        return ':project'

    def on_startup(self, ctx: ExecutionContext) -> None:
        self.io().info_msg('Creating Harbor structure...')

    def on_files_copy(self, ctx: ExecutionContext) -> None:
        """Copies files, if project was not created yet"""

        self.sh('cp -prfT %s/project ./' % HARBOR_PATH)

    def on_requirements_txt_write(self, ctx: ExecutionContext) -> None:
        """Apply Ansible to requirements.txt"""

        self.rkd([':file:line-in-file',
                  'requirements.txt',
                  '--regexp="ansible(.*)"',
                  '--insert="ansible>=2.8"'
                  ])

    def print_success_msg(self, ctx: ExecutionContext) -> None:
        super().print_success_msg(ctx)
        self.io().print_line()
        self.io().success_msg("Harbor successfully installed on bootstrapped RKD project, enjoy - " +
                              "RiotKit tech collective.")
