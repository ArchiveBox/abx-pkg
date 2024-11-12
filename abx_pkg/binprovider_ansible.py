#!/usr/bin/env python
__package__ = 'abx_pkg'

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .binprovider import BinProvider, OPERATING_SYSTEM, DEFAULT_PATH


ANSIBLE_INSTALLED = False
ANSIBLE_IMPORT_ERROR = None
try:
    from ansible_runner import Runner, RunnerConfig
    ANSIBLE_INSTALLED = True
except ImportError as err:
    ANSIBLE_IMPORT_ERROR = err


ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE = """
---
- name: Install system packages
  hosts: localhost
  gather_facts: false
  tasks:
    - name: 'Install system packages: {pkg_names}'
      {installer_module}:
        name: "{{{{item}}}}"
        state: {state}
      loop: {pkg_names}
"""


def ansible_package_install(pkg_names: str | InstallArgs, playbook_template=ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE, installer_module='auto', state='present', quiet=True) -> str:
    if not ANSIBLE_INSTALLED:
        raise RuntimeError("Ansible is not installed! To fix:\n    pip install ansible ansible-runner") from ANSIBLE_IMPORT_ERROR

    if isinstance(pkg_names, str):
        pkg_names = pkg_names.split(' ')
    else:
        pkg_names = list(pkg_names)

    if installer_module == "auto":
        if OPERATING_SYSTEM == 'darwin':
            # macOS: Use homebrew
            playbook = playbook_template.format(pkg_names=pkg_names, state=state, installer_module="community.general.homebrew")
        else:
            # Linux: Use Ansible catchall that autodetects apt/yum/pkg/nix/etc.
            playbook = playbook_template.format(pkg_names=pkg_names, state=state, installer_module="ansible.builtin.package")
    else:
        # Custom installer module
        playbook = playbook_template.format(pkg_names=pkg_names, state=state, installer_module="ansible.builtin.package")


    # create a temporary directory using the context manager
    with tempfile.TemporaryDirectory() as temp_dir:
        ansible_home = Path(temp_dir) / 'tmp'
        ansible_home.mkdir(exist_ok=True)
        
        playbook_path = Path(temp_dir) / 'install_playbook.yml'
        playbook_path.write_text(playbook)

        # run the playbook using ansible-runner
        os.environ["ANSIBLE_INVENTORY_UNPARSED_WARNING"] = "False"
        os.environ["ANSIBLE_LOCALHOST_WARNING"] = "False"
        os.environ["ANSIBLE_HOME"] = str(ansible_home)
        rc = RunnerConfig(
            private_data_dir=temp_dir,
            playbook=str(playbook_path),
            rotate_artifacts=50000,
            host_pattern="localhost",
            quiet=quiet,
        )
        rc.prepare()
        r = Runner(config=rc)
        r.run()
        succeeded = r.status == "successful"
        result_text = f'Installing {pkg_names} on {OPERATING_SYSTEM} using Ansible {installer_module} {["failed", "succeeded"][succeeded]}:{r.stdout.read()}\n{r.stderr.read()}'.strip()
        
        # check for succes/failure
        if succeeded:
            return result_text
        else:
            if "Permission denied" in result_text:
                raise PermissionError(
                    f"Installing {pkg_names} failed! Need to be root to use package manager (retry with sudo, or install manually)"
                )
            raise Exception(f"Installing {pkg_names} failed! (retry with sudo, or install manually)\n{result_text}")


class AnsibleProvider(BinProvider):
    name: BinProviderName = "ansible"
    INSTALLER_BIN: BinName = "ansible"
    PATH: PATHStr = os.environ.get("PATH", DEFAULT_PATH)
    
    ansible_installer_module: str = 'auto'  # e.g. community.general.homebrew, ansible.builtin.apt, etc.
    ansible_playbook_template: str = ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE


    def default_install_handler(self, bin_name: str, packages: Optional[InstallArgs] = None, **context) -> str:
        packages = packages or self.get_packages(bin_name)

        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}")

        return ansible_package_install(
            pkg_names=packages,
            quiet=True,
            playbook_template=self.ansible_playbook_template,
            installer_module=self.ansible_installer_module,
        )


if __name__ == "__main__":
    result = ansible = AnsibleProvider()

    if len(sys.argv) > 1:
        result = func = getattr(ansible, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2:
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
