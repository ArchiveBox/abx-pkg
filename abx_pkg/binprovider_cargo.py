#!/usr/bin/env python3
__package__ = "abx_pkg"

import os

from pathlib import Path

from pydantic import Field, model_validator, computed_field
from typing import Self

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    abx_pkg_install_root_default,
    bin_abspath,
)
from .semver import SemVer
from .binprovider import BinProvider, remap_kwargs
from .logging import format_subprocess_output


DEFAULT_CARGO_HOME = Path(os.environ.get("CARGO_HOME", "~/.cargo")).expanduser()


class CargoProvider(BinProvider):
    name: BinProviderName = "cargo"
    INSTALLER_BIN: BinName = "cargo"

    PATH: PATHStr = ""

    cargo_home: Path = DEFAULT_CARGO_HOME
    install_root: Path | None = Field(
        default_factory=lambda: abx_pkg_install_root_default("cargo"),
        validation_alias="cargo_root",
    )
    bin_dir: Path | None = None
    cargo_install_args: list[str] = ["--locked"]

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        env: dict[str, str] = {
            "CARGO_HOME": str(self.cargo_home),
            "CARGO_TARGET_DIR": str(self.install_root / "target"),
        }
        if self.install_root != self.cargo_home:
            env["CARGO_INSTALL_ROOT"] = str(self.install_root)
        return env

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.install_root and self.install_root != self.cargo_home:
            cargo_bin_dir = self.install_root / "bin"
            if not (cargo_bin_dir.is_dir() and os.access(cargo_bin_dir, os.R_OK)):
                return False

        return bool(
            bin_abspath(self.INSTALLER_BIN, PATH=self.PATH)
            or bin_abspath(self.INSTALLER_BIN),
        )

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.install_root is None:
            self.install_root = self.cargo_home
        if self.bin_dir is None:
            self.bin_dir = self.install_root / "bin"
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root, self.cargo_home),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_cargo_root(self) -> Self:
        cargo_bin_dirs = [self.cargo_home / "bin"]
        install_root = self.install_root
        assert install_root is not None
        if install_root != self.cargo_home:
            cargo_bin_dirs.insert(0, install_root / "bin")
        self.PATH = self._merge_PATH(*cargo_bin_dirs, PATH=self.PATH, prepend=True)
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
        assert install_root is not None
        self.cargo_home.mkdir(parents=True, exist_ok=True)
        (install_root / "target").mkdir(parents=True, exist_ok=True)
        if install_root != self.cargo_home:
            bin_dir = self.bin_dir
            assert bin_dir is not None
            bin_dir.mkdir(parents=True, exist_ok=True)

    def _cargo_package_specs(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
    ) -> list[str]:
        install_args = list(install_args or self.get_install_args(bin_name))
        options_with_values = {
            "--version",
            "--git",
            "--branch",
            "--tag",
            "--rev",
            "--path",
            "--root",
            "--index",
            "--registry",
            "--bin",
            "--example",
            "--profile",
            "--target",
            "--target-dir",
            "--config",
            "-j",
            "--jobs",
            "-Z",
        }
        package_specs: list[str] = []
        skip_next = False
        for arg in install_args:
            if skip_next:
                skip_next = False
                continue
            if arg in options_with_values:
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            package_specs.append(arg)
        return package_specs or [bin_name]

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
        if min_version and not any(arg.startswith("--version") for arg in install_args):
            install_args = ["--version", f">={min_version}", *install_args]
        installer_bin = self.INSTALLER_BINARY().loaded_abspath
        assert installer_bin

        cargo_install_args = [*self.cargo_install_args]
        if self.install_root != self.cargo_home:
            cargo_install_args.extend(["--root", str(self.install_root)])

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["install", *cargo_install_args, *install_args],
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
        self.setup(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
        )

        install_args = install_args or self.get_install_args(bin_name)
        if min_version and not any(arg.startswith("--version") for arg in install_args):
            install_args = ["--version", f">={min_version}", *install_args]
        installer_bin = self.INSTALLER_BINARY().loaded_abspath
        assert installer_bin

        cargo_install_args = [*self.cargo_install_args]
        if self.install_root != self.cargo_home:
            cargo_install_args.extend(["--root", str(self.install_root)])

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "install",
                "--force",
                *cargo_install_args,
                *install_args,
            ],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("update", install_args, proc)
        return format_subprocess_output(proc.stdout, proc.stderr)

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
        package_specs = self._cargo_package_specs(
            bin_name,
            install_args=install_args,
        )
        installer_bin = self.INSTALLER_BINARY().loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "uninstall",
                *(
                    ["--root", str(self.install_root)]
                    if self.install_root is not None
                    and self.install_root != self.cargo_home
                    else []
                ),
                *package_specs,
            ],
            timeout=timeout,
        )
        if proc.returncode != 0 and "did not match any packages" not in proc.stderr:
            self._raise_proc_error("uninstall", package_specs, proc)

        return True
