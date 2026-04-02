#!/usr/bin/env python
__package__ = "abx_pkg"

import os
import sys
import json
import shutil
import tempfile
import importlib
import importlib.util
from pathlib import Path
from typing import Any

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .semver import SemVer
from .binprovider import BinProvider, OPERATING_SYSTEM, DEFAULT_PATH, remap_kwargs


ANSIBLE_INSTALLED = importlib.util.find_spec("ansible_runner") is not None


ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE = """
---
- name: Install system packages
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Install system packages
      {installer_module}:
        name: "{{{{item}}}}"
        state: {state}
{module_extra_yaml}
      loop: {pkg_names}
"""


def render_ansible_module_extra_yaml(
    module_extra_kwargs: dict[str, Any] | None = None,
) -> str:
    if not module_extra_kwargs:
        return ""

    return "".join(
        f"        {key}: {json.dumps(value)}\n"
        for key, value in module_extra_kwargs.items()
    ).rstrip("\n")


def get_homebrew_search_path() -> str | None:
    brew_abspath = shutil.which("brew", path=DEFAULT_PATH) or shutil.which("brew")
    if not brew_abspath:
        return None
    return str(Path(brew_abspath).parent)


def get_ansible_bin_dir() -> str:
    return str(Path(sys.executable).parent)


def ansible_package_install(
    pkg_names: str | InstallArgs,
    playbook_template=ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE,
    installer_module="auto",
    state="present",
    quiet=True,
    module_extra_kwargs: dict[str, Any] | None = None,
) -> str:
    if not ANSIBLE_INSTALLED:
        raise RuntimeError(
            "Ansible is not installed! To fix:\n    pip install ansible ansible-runner",
        )

    ansible_runner = importlib.import_module("ansible_runner")
    Runner = ansible_runner.Runner
    RunnerConfig = ansible_runner.RunnerConfig

    if isinstance(pkg_names, str):
        pkg_names = pkg_names.split(" ")
    else:
        pkg_names = list(pkg_names)

    if installer_module == "community.general.homebrew":
        homebrew_path = get_homebrew_search_path()
        module_extra_kwargs = {
            **({"path": homebrew_path} if homebrew_path else {}),
            **(module_extra_kwargs or {}),
        }

    module_extra_yaml = render_ansible_module_extra_yaml(module_extra_kwargs)

    if installer_module == "auto":
        if OPERATING_SYSTEM == "darwin":
            # macOS: Use homebrew
            playbook = playbook_template.format(
                pkg_names=pkg_names,
                state=state,
                installer_module="community.general.homebrew",
                module_extra_yaml=module_extra_yaml,
            )
        else:
            # Linux: Use Ansible catchall that autodetects apt/yum/pkg/nix/etc.
            playbook = playbook_template.format(
                pkg_names=pkg_names,
                state=state,
                installer_module="ansible.builtin.package",
                module_extra_yaml=module_extra_yaml,
            )
    else:
        # Custom installer module
        playbook = playbook_template.format(
            pkg_names=pkg_names,
            state=state,
            installer_module=installer_module,
            module_extra_yaml=module_extra_yaml,
        )

    # create a temporary directory using the context manager
    with tempfile.TemporaryDirectory() as temp_dir:
        ansible_home = Path(temp_dir) / "tmp"
        ansible_home.mkdir(exist_ok=True)

        playbook_path = Path(temp_dir) / "install_playbook.yml"
        playbook_path.write_text(playbook)

        # run the playbook using ansible-runner
        old_env = os.environ.copy()
        try:
            os.environ["ANSIBLE_INVENTORY_UNPARSED_WARNING"] = "False"
            os.environ["ANSIBLE_LOCALHOST_WARNING"] = "False"
            os.environ["ANSIBLE_HOME"] = str(ansible_home)
            os.environ["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
            os.environ["PATH"] = ":".join(
                [get_ansible_bin_dir(), old_env.get("PATH", "")],
            ).strip(":")
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
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        succeeded = r.status == "successful"
        stdout_handle = r.stdout
        stderr_handle = r.stderr
        stdout = stdout_handle.read() if stdout_handle else ""
        stderr = stderr_handle.read() if stderr_handle else ""
        if stdout_handle:
            stdout_handle.close()
        if stderr_handle:
            stderr_handle.close()
        result_text = f"Installing {pkg_names} on {OPERATING_SYSTEM} using Ansible {installer_module} {['failed', 'succeeded'][succeeded]}:{stdout}\n{stderr}".strip()

        # check for success/failure
        if succeeded:
            return result_text
        else:
            if "Permission denied" in result_text:
                raise PermissionError(
                    f"Installing {pkg_names} failed! Need to be root to use package manager (retry with sudo, or install manually)",
                )
            raise Exception(
                f"Installing {pkg_names} failed! (retry with sudo, or install manually)\n{result_text}",
            )


class AnsibleProvider(BinProvider):
    name: BinProviderName = "ansible"
    INSTALLER_BIN: BinName = "ansible"
    PATH: PATHStr = os.environ.get("PATH", DEFAULT_PATH)

    ansible_installer_module: str = (
        "auto"  # e.g. community.general.homebrew, ansible.builtin.apt, etc.
    )
    ansible_playbook_template: str = ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE

    def get_ansible_module_extra_kwargs(self) -> dict[str, Any]:
        if self.ansible_installer_module == "community.general.homebrew":
            homebrew_path = get_homebrew_search_path()
            if homebrew_path:
                return {"path": homebrew_path}
        return {}

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)

        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}",
            )

        module_extra_kwargs = self.get_ansible_module_extra_kwargs()

        return ansible_package_install(
            pkg_names=install_args,
            quiet=True,
            playbook_template=self.ansible_playbook_template,
            installer_module=self.ansible_installer_module,
            module_extra_kwargs=module_extra_kwargs or None,
        )

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)

        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}",
            )

        module_extra_kwargs = self.get_ansible_module_extra_kwargs()
        if module_extra_kwargs:
            return ansible_package_install(
                pkg_names=install_args,
                quiet=True,
                playbook_template=self.ansible_playbook_template,
                installer_module=self.ansible_installer_module,
                state="latest",
                module_extra_kwargs=module_extra_kwargs,
            )
        return ansible_package_install(
            pkg_names=install_args,
            quiet=True,
            playbook_template=self.ansible_playbook_template,
            installer_module=self.ansible_installer_module,
            state="latest",
        )

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> bool:
        install_args = install_args or self.get_install_args(bin_name)

        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}",
            )

        module_extra_kwargs = self.get_ansible_module_extra_kwargs()
        if module_extra_kwargs:
            ansible_package_install(
                pkg_names=install_args,
                quiet=True,
                playbook_template=self.ansible_playbook_template,
                installer_module=self.ansible_installer_module,
                state="absent",
                module_extra_kwargs=module_extra_kwargs,
            )
        else:
            ansible_package_install(
                pkg_names=install_args,
                quiet=True,
                playbook_template=self.ansible_playbook_template,
                installer_module=self.ansible_installer_module,
                state="absent",
            )
        return True


if __name__ == "__main__":
    result = ansible = AnsibleProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(ansible, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
