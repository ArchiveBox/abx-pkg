#!/usr/bin/env python3
__package__ = "abx_pkg"

import os

from pathlib import Path
from typing import Optional, List

from pydantic import model_validator, TypeAdapter, computed_field
from typing_extensions import Self

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .binprovider import BinProvider, remap_kwargs


DEFAULT_CARGO_HOME = Path(os.environ.get("CARGO_HOME", "~/.cargo")).expanduser()


class CargoProvider(BinProvider):
    name: BinProviderName = "cargo"
    INSTALLER_BIN: BinName = "cargo"

    PATH: PATHStr = ""

    cargo_root: Optional[Path] = None
    cargo_home: Path = DEFAULT_CARGO_HOME
    cargo_install_args: List[str] = ["--locked"]

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.cargo_root:
            cargo_bin_dir = self.cargo_root / "bin"
            if not (cargo_bin_dir.is_dir() and os.access(cargo_bin_dir, os.R_OK)):
                return False

        return bool(self.INSTALLER_BIN_ABSPATH)

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(owner_paths=(self.cargo_root, self.cargo_home), preserve_root=True)

        return self

    @model_validator(mode="after")
    def load_PATH_from_cargo_root(self) -> Self:
        cargo_bin_dirs: list[str] = []

        if self.cargo_root:
            cargo_bin_dirs.append(str(self.cargo_root / "bin"))

        cargo_bin_dirs.append(str(self.cargo_home / "bin"))

        PATH = self.PATH
        for bin_dir in cargo_bin_dirs:
            if bin_dir not in PATH:
                PATH = ":".join([*PATH.split(":"), bin_dir])

        self.PATH = TypeAdapter(PATHStr).validate_python(PATH)
        return self

    def setup(self) -> None:
        self.cargo_home.mkdir(parents=True, exist_ok=True)
        if self.cargo_root:
            (self.cargo_root / "bin").mkdir(parents=True, exist_ok=True)

    def _cargo_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CARGO_HOME"] = str(self.cargo_home)
        if self.cargo_root:
            env["CARGO_INSTALL_ROOT"] = str(self.cargo_root)
        return env

    def _cargo_install_args(self) -> list[str]:
        install_args = [*self.cargo_install_args]
        if self.cargo_root:
            install_args.extend(["--root", str(self.cargo_root)])
        return install_args

    @remap_kwargs({'packages': 'install_args'})
    def default_install_handler(self, bin_name: str, install_args: Optional[InstallArgs] = None, **context) -> str:
        self.setup()

        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f"{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)")

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["install", *self._cargo_install_args(), *install_args],
            env=self._cargo_env(),
        )
        if proc.returncode != 0:
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f"{self.__class__.__name__}: install got returncode {proc.returncode} while installing {install_args}: {install_args}")

        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

    @remap_kwargs({'packages': 'install_args'})
    def default_update_handler(self, bin_name: str, install_args: Optional[InstallArgs] = None, **context) -> str:
        self.setup()

        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f"{self.__class__.__name__} update method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)")

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["install", "--force", *self._cargo_install_args(), *install_args],
            env=self._cargo_env(),
        )
        if proc.returncode != 0:
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f"{self.__class__.__name__}: update got returncode {proc.returncode} while updating {install_args}: {install_args}")

        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

    @remap_kwargs({'packages': 'install_args'})
    def default_uninstall_handler(self, bin_name: str, install_args: Optional[InstallArgs] = None, **context) -> bool:
        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f"{self.__class__.__name__} uninstall method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)")

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["uninstall", *self._cargo_install_args(), *install_args],
            env=self._cargo_env(),
        )
        if proc.returncode != 0:
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f"{self.__class__.__name__}: uninstall got returncode {proc.returncode} while uninstalling {install_args}: {install_args}")

        return True
