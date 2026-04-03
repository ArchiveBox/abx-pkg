#!/usr/bin/env python
__package__ = "abx_pkg"

import os
import sys
import json
import shutil
import logging as py_logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .semver import SemVer
from .binprovider import BinProvider, OPERATING_SYSTEM, DEFAULT_PATH, remap_kwargs
from .logging import get_logger, log_subprocess_output

logger = get_logger(__name__)


ANSIBLE_INSTALLED = shutil.which("ansible-playbook") is not None


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


def ansible_package_install(
    pkg_names: str | InstallArgs,
    playbook_template=ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE,
    installer_module="auto",
    state="present",
    quiet=True,
    module_extra_kwargs: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> str:
    if not ANSIBLE_INSTALLED:
        raise RuntimeError(
            "Ansible is not installed! To fix:\n    pip install ansible",
        )

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
    with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
        ansible_home = Path(temp_dir) / "tmp"
        ansible_home.mkdir(exist_ok=True)

        playbook_path = Path(temp_dir) / "install_playbook.yml"
        playbook_path.write_text(playbook)

        env = os.environ.copy()
        env["ANSIBLE_INVENTORY_UNPARSED_WARNING"] = "False"
        env["ANSIBLE_LOCALHOST_WARNING"] = "False"
        env["ANSIBLE_HOME"] = str(ansible_home)
        env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
        env["TMPDIR"] = "/tmp"
        env["PATH"] = ":".join(
            [str(Path(sys.executable).parent), env.get("PATH", "")],
        ).strip(":")
        ansible_playbook = (
            shutil.which("ansible-playbook", path=env["PATH"]) or "ansible-playbook"
        )
        cmd = [
            ansible_playbook,
            "-i",
            "localhost,",
            "-c",
            "local",
            str(playbook_path),
        ]
        proc = None
        if (
            OPERATING_SYSTEM != "darwin"
            and installer_module != "community.general.homebrew"
        ):
            sudo_bin = shutil.which("sudo", path=env["PATH"]) or shutil.which("sudo")
            if os.geteuid() != 0 and sudo_bin:
                sudo_proc = subprocess.run(
                    [
                        sudo_bin,
                        "-n",
                        "--preserve-env=PATH,HOME,LOGNAME,USER,TMPDIR,ANSIBLE_INVENTORY_UNPARSED_WARNING,ANSIBLE_LOCALHOST_WARNING,ANSIBLE_HOME,ANSIBLE_PYTHON_INTERPRETER",
                        "--",
                        *cmd,
                    ],
                    capture_output=True,
                    text=True,
                    cwd=temp_dir,
                    env=env,
                    timeout=timeout,
                )
                if sudo_proc.returncode == 0:
                    proc = sudo_proc
                else:
                    log_subprocess_output(
                        logger,
                        "ansible sudo exec",
                        sudo_proc.stdout,
                        sudo_proc.stderr,
                        level=py_logging.DEBUG,
                    )
        if proc is None:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=temp_dir,
                env=env,
                timeout=timeout,
            )
        succeeded = proc.returncode == 0
        result_text = f"Installing {pkg_names} on {OPERATING_SYSTEM} using Ansible {installer_module} {['failed', 'succeeded'][succeeded]}:{proc.stdout}\n{proc.stderr}".strip()

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
        timeout: int | None = None,
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
            timeout=timeout,
        )

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        timeout: int | None = None,
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
                timeout=timeout,
            )
        return ansible_package_install(
            pkg_names=install_args,
            quiet=True,
            playbook_template=self.ansible_playbook_template,
            installer_module=self.ansible_installer_module,
            state="latest",
            timeout=timeout,
        )

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        timeout: int | None = None,
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
                timeout=timeout,
            )
        else:
            ansible_package_install(
                pkg_names=install_args,
                quiet=True,
                playbook_template=self.ansible_playbook_template,
                installer_module=self.ansible_installer_module,
                state="absent",
                timeout=timeout,
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
