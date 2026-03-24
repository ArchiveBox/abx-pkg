#!/usr/bin/env python
__package__ = "abx_pkg"

import os
import sys
import shutil
import importlib
import importlib.util
from pathlib import Path

from typing import Any

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .binprovider import BinProvider, OPERATING_SYSTEM, DEFAULT_PATH, remap_kwargs

PYINFRA_INSTALLED = importlib.util.find_spec("pyinfra") is not None


def pyinfra_package_install(
    pkg_names: InstallArgs,
    installer_module: str = "auto",
    installer_extra_kwargs: dict[str, Any] | None = None,
) -> str:
    if not PYINFRA_INSTALLED:
        raise RuntimeError(
            "Pyinfra is not installed! To fix:\n    pip install pyinfra",
        )

    pyinfra_config = importlib.import_module("pyinfra.api.config")
    pyinfra_inventory = importlib.import_module("pyinfra.api.inventory")
    pyinfra_state = importlib.import_module("pyinfra.api.state")
    pyinfra_connect = importlib.import_module("pyinfra.api.connect")
    pyinfra_operation = importlib.import_module("pyinfra.api.operation")
    pyinfra_operations = importlib.import_module("pyinfra.api.operations")
    pyinfra_exceptions = importlib.import_module("pyinfra.api.exceptions")

    Config = pyinfra_config.Config
    Inventory = pyinfra_inventory.Inventory
    State = pyinfra_state.State
    connect_all = pyinfra_connect.connect_all
    add_op = pyinfra_operation.add_op
    run_ops = pyinfra_operations.run_ops
    PyinfraError = pyinfra_exceptions.PyinfraError

    config = Config()
    inventory = Inventory((["@local"], {}))
    state = State(inventory=inventory, config=config)

    if isinstance(pkg_names, str):
        pkg_names = pkg_names.split(" ")

    connect_all(state)

    _sudo_user = None
    if installer_module == "auto":
        is_macos = OPERATING_SYSTEM == "darwin"
        if is_macos:
            installer_module = "operations.brew.packages"
            try:
                brew_abspath = shutil.which("brew")
                if brew_abspath:
                    _sudo_user = Path(brew_abspath).stat().st_uid
            except Exception:
                pass
        else:
            installer_module = "operations.server.packages"
    else:
        # TODO: non-stock pyinfra modules from other libraries?
        assert installer_module.startswith("operations.")

    try:
        module_name, operation_name = installer_module.rsplit(".", 1)
        installer_module_obj = importlib.import_module(f"pyinfra.{module_name}")
        installer_module_op = getattr(installer_module_obj, operation_name)
    except Exception as err:
        raise RuntimeError(
            f"Failed to import pyinfra installer_module {installer_module}: {err.__class__.__name__}",
        ) from err

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
    result_text = f"Installing {pkg_names} on {OPERATING_SYSTEM} using Pyinfra {installer_module} {['failed', 'succeeded'][succeeded]}\n{result.stdout}\n{result.stderr}".strip()

    if succeeded:
        return result_text

    if "Permission denied" in result_text:
        raise PermissionError(
            f"Installing {pkg_names} failed! Need to be root to use package manager (retry with sudo, or install manually)",
        )
    raise Exception(
        f"Installing {pkg_names} failed! (retry with sudo, or install manually)\n{result_text}",
    )


class PyinfraProvider(BinProvider):
    name: BinProviderName = "pyinfra"
    INSTALLER_BIN: BinName = "pyinfra"
    PATH: PATHStr = os.environ.get("PATH", DEFAULT_PATH)

    pyinfra_installer_module: str = (
        "auto"  # e.g. operations.apt.packages, operations.server.packages, etc.
    )
    pyinfra_installer_kwargs: dict[str, Any] = {}

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)

        return pyinfra_package_install(
            pkg_names=install_args,
            installer_module=self.pyinfra_installer_module,
            installer_extra_kwargs=self.pyinfra_installer_kwargs,
        )

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)

        return pyinfra_package_install(
            pkg_names=install_args,
            installer_module=self.pyinfra_installer_module,
            installer_extra_kwargs={
                **self.pyinfra_installer_kwargs,
                "latest": True,
            },
        )

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> bool:
        install_args = install_args or self.get_install_args(bin_name)

        pyinfra_package_install(
            pkg_names=install_args,
            installer_module=self.pyinfra_installer_module,
            installer_extra_kwargs={
                **self.pyinfra_installer_kwargs,
                "present": False,
            },
        )
        return True


if __name__ == "__main__":
    result = pyinfra = PyinfraProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(pyinfra, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
