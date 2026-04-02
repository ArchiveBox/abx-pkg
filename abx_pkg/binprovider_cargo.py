#!/usr/bin/env python3
__package__ = "abx_pkg"

import os

from pathlib import Path

from pydantic import model_validator, TypeAdapter, computed_field
from typing import Self

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .semver import SemVer
from .binprovider import BinProvider, remap_kwargs
from .logging import format_subprocess_output, get_logger, log_subprocess_error

logger = get_logger(__name__)


DEFAULT_CARGO_HOME = Path(os.environ.get("CARGO_HOME", "~/.cargo")).expanduser()


class CargoProvider(BinProvider):
    name: BinProviderName = "cargo"
    INSTALLER_BIN: BinName = "cargo"

    PATH: PATHStr = ""

    cargo_root: Path | None = None
    cargo_home: Path = DEFAULT_CARGO_HOME
    cargo_install_args: list[str] = ["--locked"]

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
            self.euid = self.detect_euid(
                owner_paths=(self.cargo_root, self.cargo_home),
                preserve_root=True,
            )

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

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> None:
        self.cargo_home.mkdir(parents=True, exist_ok=True)
        self._cargo_target_dir().mkdir(parents=True, exist_ok=True)
        if self.cargo_root:
            (self.cargo_root / "bin").mkdir(parents=True, exist_ok=True)

    def _cargo_target_dir(self) -> Path:
        return (self.cargo_root or self.cargo_home) / "target"

    def _cargo_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CARGO_HOME"] = str(self.cargo_home)
        env["CARGO_TARGET_DIR"] = str(self._cargo_target_dir())
        if self.cargo_root:
            env["CARGO_INSTALL_ROOT"] = str(self.cargo_root)
        return env

    def _cargo_install_args(self) -> list[str]:
        install_args = [*self.cargo_install_args]
        if self.cargo_root:
            install_args.extend(["--root", str(self.cargo_root)])
        return install_args

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> str:
        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
        )

        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        version_args = ["--version", f">={min_version}"] if min_version else []

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["install", *self._cargo_install_args(), *version_args, *install_args],
            env=self._cargo_env(),
        )
        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} install",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__}: install got returncode {proc.returncode} while installing {install_args}: {install_args}\n{format_subprocess_output(proc.stdout, proc.stderr)}".strip(),
            )

        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> str:
        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
        )

        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__} update method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        version_args = ["--version", f">={min_version}"] if min_version else []

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=[
                "install",
                "--force",
                *self._cargo_install_args(),
                *version_args,
                *install_args,
            ],
            env=self._cargo_env(),
        )
        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} update",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__}: update got returncode {proc.returncode} while updating {install_args}: {install_args}\n{format_subprocess_output(proc.stdout, proc.stderr)}".strip(),
            )

        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

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
                f"{self.__class__.__name__} uninstall method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["uninstall", *self._cargo_install_args(), *install_args],
            env=self._cargo_env(),
        )
        if proc.returncode != 0 and "did not match any packages" not in proc.stderr:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} uninstall",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__}: uninstall got returncode {proc.returncode} while uninstalling {install_args}: {install_args}\n{format_subprocess_output(proc.stdout, proc.stderr)}".strip(),
            )

        return True
