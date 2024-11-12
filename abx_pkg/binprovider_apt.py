
#!/usr/bin/env python
__package__ = "abx_pkg"

import sys
import time
import shutil
from typing import Optional

from pydantic import model_validator, TypeAdapter

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .binprovider import BinProvider

_LAST_UPDATE_CHECK = None
UPDATE_CHECK_INTERVAL = 60 * 60 * 24  # 1 day


class AptProvider(BinProvider):
    name: BinProviderName = "apt"
    INSTALLER_BIN: BinName = "apt-get"

    PATH: PATHStr = ""
    
    euid: Optional[int] = 0     # always run apt as root

    @model_validator(mode="after")
    def load_PATH_from_dpkg_install_location(self):
        dpkg_abspath = shutil.which("dpkg")
        if (not self.INSTALLER_BIN_ABSPATH) or not dpkg_abspath or not self.is_valid:
            # package manager is not available on this host
            # self.PATH: PATHStr = ''
            # self.INSTALLER_BIN_ABSPATH = None
            return self

        PATH = self.PATH
        dpkg_install_dirs = self.exec(bin_name=dpkg_abspath, cmd=["-L", "bash"], quiet=True).stdout.strip().split("\n")
        dpkg_bin_dirs = [path for path in dpkg_install_dirs if path.endswith("/bin")]
        for bin_dir in dpkg_bin_dirs:
            if str(bin_dir) not in PATH:
                PATH = ":".join([str(bin_dir), *PATH.split(":")])
        self.PATH = TypeAdapter(PATHStr).validate_python(PATH)
        return self

    def default_install_handler(self, bin_name: BinName, packages: Optional[InstallArgs] = None, **context) -> str:
        global _LAST_UPDATE_CHECK

        packages = packages or self.get_packages(bin_name)

        if not (self.INSTALLER_BIN_ABSPATH and shutil.which("dpkg")):
            raise Exception(f"{self.__class__.__name__}.INSTALLER_BIN is not available on this host: {self.INSTALLER_BIN}")

        # print(f'[*] {self.__class__.__name__}: Installing {bin_name}: {self.INSTALLER_BIN} install {packages}')

        # Attempt 1: Try installing with Pyinfra
        from .binprovider_pyinfra import PYINFRA_INSTALLED, pyinfra_package_install

        if PYINFRA_INSTALLED:
            return pyinfra_package_install([bin_name], installer_module="operations.apt.packages")

        # Attempt 2: Try installing with Ansible
        from .binprovider_ansible import ANSIBLE_INSTALLED, ansible_package_install

        if ANSIBLE_INSTALLED:
            return ansible_package_install([bin_name], installer_module="ansible.builtin.apt")

        # Attempt 3: Fallback to installing manually by calling apt in shell
        if not _LAST_UPDATE_CHECK or (time.time() - _LAST_UPDATE_CHECK) > UPDATE_CHECK_INTERVAL:
            # only update if we haven't checked in the last day
            self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=["update", "-qq"])
            _LAST_UPDATE_CHECK = time.time()

        proc = self.exec(bin_name=self.INSTALLER_BIN_ABSPATH, cmd=["install", "-y", "-qq", "--no-install-recommends", *packages])
        if proc.returncode != 0:
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f"{self.__class__.__name__} install got returncode {proc.returncode} while installing {packages}: {packages}")

            return proc.stderr.strip() + "\n" + proc.stdout.strip()
        return f"Installed {packages} succesfully."


if __name__ == "__main__":
    result = apt = AptProvider()

    if len(sys.argv) > 1:
        result = func = getattr(apt, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2:
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
