#!/usr/bin/env python3
__package__ = "abx_pkg"

import os

from pathlib import Path

from pydantic import model_validator, TypeAdapter, computed_field
from typing import Self

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs, HostBinPath
from .semver import SemVer
from .binprovider import BinProvider, DEFAULT_ENV_PATH, remap_kwargs
from .logging import get_logger, log_subprocess_error

logger = get_logger(__name__)


DEFAULT_GOPATH = Path(os.environ.get("GOPATH", "~/go")).expanduser()


class GoGetProvider(BinProvider):
    name: BinProviderName = "go_get"
    INSTALLER_BIN: BinName = "go"

    PATH: PATHStr = DEFAULT_ENV_PATH

    gobin: Path | None = None
    gopath: Path = DEFAULT_GOPATH
    go_install_args: list[str] = []

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.gobin and not (self.gobin.is_dir() and os.access(self.gobin, os.R_OK)):
            return False

        return bool(self.INSTALLER_BIN_ABSPATH)

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.gobin, self.gopath),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_go_env(self) -> Self:
        bin_dir = self._gobin()
        if self.gobin:
            self.PATH = TypeAdapter(PATHStr).validate_python(str(bin_dir))
        elif str(bin_dir) not in self.PATH:
            self.PATH = TypeAdapter(PATHStr).validate_python(
                ":".join([*self.PATH.split(":"), str(bin_dir)]),
            )
        return self

    def _gobin(self) -> Path:
        return (self.gobin or (self.gopath / "bin")).expanduser()

    def setup(self) -> None:
        self.gopath.mkdir(parents=True, exist_ok=True)
        self._gobin().mkdir(parents=True, exist_ok=True)

    def _go_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GOPATH"] = str(self.gopath)
        env["GOBIN"] = str(self._gobin())
        return env

    def default_install_args_handler(self, bin_name: BinName, **context) -> InstallArgs:
        return [f"{bin_name}@latest"]

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> str:
        self.setup()

        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["install", *self.go_install_args, *install_args],
            env=self._go_env(),
        )
        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} install",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__}: install got returncode {proc.returncode} while installing {install_args}: {install_args}",
            )

        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> str:
        return self.default_install_handler(
            bin_name=bin_name,
            install_args=install_args,
            **context,
        )

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> bool:
        abspath = self.get_abspath(bin_name, quiet=True, nocache=True)
        if not abspath:
            return True

        Path(abspath).unlink(missing_ok=True)
        return True

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        **context,
    ) -> SemVer | None:
        abspath = abspath or self.get_abspath(bin_name, quiet=True)
        if not abspath or not self.INSTALLER_BIN_ABSPATH:
            return None

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["version", "-m", abspath],
            env=self._go_env(),
            timeout=self._version_timeout,
            quiet=True,
        )
        if proc.returncode == 0:
            for line in proc.stdout.strip().splitlines():
                if line.startswith("mod\t"):
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        version = SemVer.parse(parts[2].lstrip("v"))
                        if version:
                            return version

        return super().default_version_handler(bin_name, abspath=abspath, **context)
