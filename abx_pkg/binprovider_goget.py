#!/usr/bin/env python3
__package__ = "abx_pkg"

import os

from pathlib import Path

from pydantic import Field, TypeAdapter, model_validator, computed_field
from typing import Self

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    abx_pkg_install_root_default,
    bin_abspath,
)
from .semver import SemVer
from .binprovider import BinProvider, DEFAULT_ENV_PATH, remap_kwargs
from .logging import format_subprocess_output


DEFAULT_GOPATH = Path(os.environ.get("GOPATH", "~/go")).expanduser()


class GoGetProvider(BinProvider):
    name: BinProviderName = "goget"
    INSTALLER_BIN: BinName = "go"

    PATH: PATHStr = DEFAULT_ENV_PATH

    install_root: Path | None = Field(
        default_factory=lambda: abx_pkg_install_root_default("goget"),
        validation_alias="gopath",
    )
    bin_dir: Path | None = Field(default=None, validation_alias="gobin")
    go_install_args: list[str] = []

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        env: dict[str, str] = {"GOPATH": str(self.install_root)}
        if self.bin_dir:
            env["GOBIN"] = str(self.bin_dir)
        return env

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.bin_dir and not (
            self.bin_dir.is_dir() and os.access(self.bin_dir, os.R_OK)
        ):
            return False

        return bool(
            bin_abspath(self.INSTALLER_BIN, PATH=self.PATH)
            or bin_abspath(self.INSTALLER_BIN),
        )

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.install_root is None:
            self.install_root = DEFAULT_GOPATH
        if self.bin_dir is None:
            self.bin_dir = (self.install_root / "bin").expanduser()
        else:
            self.bin_dir = self.bin_dir.expanduser()
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.bin_dir, self.install_root),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_go_env(self) -> Self:
        bin_dir = self.bin_dir
        assert bin_dir is not None
        if self.install_root != DEFAULT_GOPATH or "bin_dir" in self.model_fields_set:
            self.PATH = self._merge_PATH(bin_dir)
        else:
            self.PATH = self._merge_PATH(bin_dir, PATH=self.PATH)
        return self

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None
        install_root.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)

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
        installer_bin = self.INSTALLER_BINARY().loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["install", *self.go_install_args, *install_args],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", install_args, proc)

        return format_subprocess_output(proc.stdout, proc.stderr)

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
        abspath = self.get_abspath(bin_name, quiet=True, no_cache=True)
        if not abspath:
            return True

        Path(abspath).unlink(missing_ok=True)
        # Also remove the short binary name (e.g. "shfmt" for
        # "mvdan.cc/sh/v3/cmd/shfmt") from bin_dir.
        if self.bin_dir:
            install_args = self.get_install_args(str(bin_name))
            install_target = install_args[0] if install_args else str(bin_name)
            short_name = Path(str(install_target).split("@", 1)[0].rstrip("/")).name
            if short_name and short_name != str(bin_name):
                (self.bin_dir / short_name).unlink(missing_ok=True)
        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
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
        candidate_abspath = bin_abspath(candidate_name, PATH=str(self.bin_dir))
        if candidate_abspath is None:
            return None
        direct_abspath = TypeAdapter(HostBinPath).validate_python(candidate_abspath)
        bin_dir = self.bin_dir
        assert bin_dir is not None
        link_path = bin_dir / str(bin_name)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink(missing_ok=True)
        link_path.symlink_to(direct_abspath)
        return TypeAdapter(HostBinPath).validate_python(link_path)

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> SemVer | None:
        abspath = abspath or self.get_abspath(bin_name, quiet=True)
        if not abspath:
            return None
        try:
            installer_abspath = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
            assert installer_abspath
        except Exception:
            return None

        proc = self.exec(
            bin_name=installer_abspath,
            cmd=["version", "-m", abspath],
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
