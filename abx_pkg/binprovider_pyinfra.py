#!/usr/bin/env python
__package__ = 'abx_pkg'

import os
import sys
import shutil
from pathlib import Path

from typing import Optional, Dict, Any

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .binprovider import BinProvider, OPERATING_SYSTEM, DEFAULT_PATH

PYINFRA_INSTALLED = False
PYINFRA_IMPORT_ERROR = None
try:
    # from pyinfra import host
    from pyinfra import operations                           # noqa: F401
    from pyinfra.api import Config, Inventory, State
    from pyinfra.api.connect import connect_all
    from pyinfra.api.operation import add_op
    from pyinfra.api.operations import run_ops
    from pyinfra.api.exceptions import PyinfraError

    PYINFRA_INSTALLED = True
except ImportError as err:
    PYINFRA_IMPORT_ERROR = err
    pass




def pyinfra_package_install(pkg_names: InstallArgs, installer_module: str = "auto", installer_extra_kwargs: Optional[Dict[str, Any]] = None) -> str:
    if not PYINFRA_INSTALLED:
        raise RuntimeError("Pyinfra is not installed! To fix:\n    pip install pyinfra") from PYINFRA_IMPORT_ERROR

    config = Config()
    inventory = Inventory((["@local"], {}))
    state = State(inventory=inventory, config=config)

    if isinstance(pkg_names, str):
        pkg_names = pkg_names.split(' ')   # type: ignore

    connect_all(state)
    
    _sudo_user = None
    if installer_module == 'auto':
        is_macos = OPERATING_SYSTEM == "darwin"
        if is_macos:
            installer_module = 'operations.brew.packages'
            try:
                _sudo_user = Path(shutil.which('brew')).stat().st_uid
            except Exception:
                pass
        else:
            installer_module = 'operations.server.packages'
    else:
        # TODO: non-stock pyinfra modules from other libraries?
        assert installer_module.startswith('operations.')
    
    try:
        installer_module_op = eval(installer_module)
    except Exception as err:
        raise RuntimeError(f'Failed to import pyinfra installer_module {installer_module}: {err.__class__.__name__}') from err
    
    result = add_op(
        state,
        installer_module_op,
        name=f"Install system packages: {pkg_names}",
        packages=pkg_names,
        _sudo_user=_sudo_user,
        **(installer_extra_kwargs or {}),
    )

    succeeded = False
    try:
        run_ops(state)
        succeeded = True
    except PyinfraError:
        succeeded = False
        
    result = result[state.inventory.hosts["@local"]]
    result_text = f'Installing {pkg_names} on {OPERATING_SYSTEM} using Pyinfra {installer_module} {["failed", "succeeded"][succeeded]}\n{result.stdout}\n{result.stderr}'.strip()
    
    if succeeded:
        return result_text

    if "Permission denied" in result_text:
        raise PermissionError(
            f"Installing {pkg_names} failed! Need to be root to use package manager (retry with sudo, or install manually)"
        )
    raise Exception(f"Installing {pkg_names} failed! (retry with sudo, or install manually)\n{result_text}")
        


class PyinfraProvider(BinProvider):
    name: BinProviderName = "pyinfra"
    INSTALLER_BIN: BinName = "pyinfra"
    PATH: PATHStr = os.environ.get("PATH", DEFAULT_PATH)

    pyinfra_installer_module: str = 'auto'   # e.g. operations.apt.packages, operations.server.packages, etc.
    pyinfra_installer_kwargs: Dict[str, Any] = {}


    def default_install_handler(self, bin_name: str, packages: Optional[InstallArgs] = None, **context) -> str:
        packages = packages or self.get_packages(bin_name)

        return pyinfra_package_install(
            pkg_names=packages,
            installer_module=self.pyinfra_installer_module,
            installer_extra_kwargs=self.pyinfra_installer_kwargs,
        )


if __name__ == "__main__":
    result = pyinfra = PyinfraProvider()
    
    if len(sys.argv) > 1:
        result = func = getattr(pyinfra, sys.argv[1])   # e.g. install

    if len(sys.argv) > 2:
        result = func(sys.argv[2])             # e.g. install ffmpeg
    
    print(result)
