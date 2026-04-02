#!/usr/bin/env python3
__package__ = "abx_pkg"

import os

from pathlib import Path

from pydantic import TypeAdapter, model_validator, computed_field
from typing import ClassVar, Self

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs, HostBinPath
from .semver import SemVer
from .binprovider import BinProvider, DEFAULT_ENV_PATH, remap_kwargs
from .logging import get_logger, log_subprocess_error

logger = get_logger(__name__)


DEFAULT_GOPATH = Path(os.environ.get("GOPATH", "~/go")).expanduser()


class GoGetProvider(BinProvider):
    name: BinProviderName = "goget"
    INSTALLER_BIN: BinName = "go"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "gopath"
    BIN_DIR_FIELD: ClassVar[str | None] = "gobin"

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

    @computed_field
    @property
    def install_root(self) -> Path:
        return self.gopath

    @computed_field
    @property
    def bin_dir(self) -> Path:
        return (self.gobin or (self.install_root / "bin")).expanduser()

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
        if self.gobin or "gopath" in self.model_fields_set:
            self.PATH = self._merge_PATH(self.bin_dir)
        else:
            self.PATH = self._merge_PATH(self.bin_dir, PATH=self.PATH)
        return self

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> None:
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True, exist_ok=True)

    def _go_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GOPATH"] = str(self.install_root)
        env["GOBIN"] = str(self.bin_dir)
        return env

    def default_install_args_handler(self, bin_name: BinName, **context) -> InstallArgs:
        bin_name_str = str(bin_name)
        if not (
            bin_name_str.startswith(("./", "../"))
            or ("/" in bin_name_str and "." in bin_name_str.split("/", 1)[0])
        ):
            raise ValueError(
                f"{self.__class__.__name__} requires install_args with a full Go module path for {bin_name!r}, e.g. overrides={{'{bin_name}': {{'install_args': ['example.com/module/cmd/{bin_name}@latest']}}}}",
            )
        return [f"{bin_name}@latest"]

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

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["install", *self.go_install_args, *install_args],
            env=self._go_env(),
            timeout=timeout,
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
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        timeout: int | None = None,
    ) -> str:
        return self.default_install_handler(
            bin_name=bin_name,
            install_args=install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
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
        abspath = self.get_abspath(bin_name, quiet=True, nocache=True)
        if not abspath:
            return True

        Path(abspath).unlink(missing_ok=True)
        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        **context,
    ) -> HostBinPath | None:
        bin_name_str = str(bin_name)
        abspath = super().default_abspath_handler(bin_name, **context)
        if abspath:
            return TypeAdapter(HostBinPath).validate_python(abspath)

        install_args = list(
            context.get("install_args") or self.get_install_args(bin_name_str),
        )
        install_target = install_args[0] if install_args else bin_name_str
        candidate_name = (
            Path(
                str(install_target).split("@", 1)[0].rstrip("/"),
            ).name
            or bin_name_str
        )
        if candidate_name == bin_name_str:
            return None
        candidate_abspath = super().default_abspath_handler(candidate_name, **context)
        if candidate_abspath is None:
            return None
        return TypeAdapter(HostBinPath).validate_python(candidate_abspath)

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        **context,
    ) -> SemVer | None:
        abspath = abspath or self.get_abspath(bin_name, quiet=True)
        if not abspath or not self.INSTALLER_BIN_ABSPATH:
            return None

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["version", "-m", abspath],
            env=self._go_env(),
            timeout=timeout,
            quiet=True,
        )
        if proc.returncode == 0:
            for line in proc.stdout.strip().splitlines():
                if line.startswith("mod\t"):
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        return parts[2].lstrip("v")

        version = self._version_from_exec(
            bin_name,
            abspath=abspath,
            timeout=timeout,
        )
        return version
