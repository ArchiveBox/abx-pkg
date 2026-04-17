#!/usr/bin/env python3
__package__ = "abx_pkg"

import os
import shutil

from pathlib import Path

from pydantic import model_validator, TypeAdapter, computed_field
from typing import ClassVar, Self

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


# Ultimate fallback when neither the constructor arg nor
# ``ABX_PKG_NIX_ROOT`` nor ``ABX_PKG_LIB_DIR`` is set.
DEFAULT_NIX_PROFILE = Path("~/.nix-profile").expanduser()
DEFAULT_NIX_BIN_DIR = Path("/nix/var/nix/profiles/default/bin")


class NixProvider(BinProvider):
    name: BinProviderName = "nix"
    INSTALLER_BIN: BinName = "nix"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "nix_profile"

    PATH: PATHStr = ""

    # Default: ABX_PKG_NIX_ROOT > ABX_PKG_LIB_DIR/nix > ~/.nix-profile.
    nix_profile: Path = abx_pkg_install_root_default("nix") or DEFAULT_NIX_PROFILE
    nix_state_dir: Path | None = None
    nix_install_args: list[str] = [
        "--extra-experimental-features",
        "nix-command",
        "--extra-experimental-features",
        "flakes",
    ]

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        if self._INSTALLER_BIN_ABSPATH:
            return self._INSTALLER_BIN_ABSPATH

        abspath = bin_abspath(
            self.INSTALLER_BIN,
            PATH=f"{DEFAULT_NIX_BIN_DIR}:{DEFAULT_ENV_PATH}",
        ) or bin_abspath(DEFAULT_NIX_BIN_DIR / "nix")
        if not abspath:
            return None

        valid_abspath = TypeAdapter(HostBinPath).validate_python(abspath)
        self._INSTALLER_BIN_ABSPATH = valid_abspath
        return valid_abspath

    @computed_field
    @property
    def is_valid(self) -> bool:
        profile_bin_dir = self.nix_profile / "bin"
        if profile_bin_dir.exists() and not os.access(profile_bin_dir, os.R_OK):
            return False

        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path:
        return self.nix_profile

    @computed_field
    @property
    def bin_dir(self) -> Path:
        return self.nix_profile / "bin"

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.nix_profile.parent,),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_nix_profile(self) -> Self:
        self.PATH = self._merge_PATH(
            self.nix_profile / "bin",
            PATH=self.PATH,
            prepend=True,
        )
        return self

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> None:
        self.nix_profile.parent.mkdir(parents=True, exist_ok=True)
        if (
            self.nix_profile.exists()
            and self.nix_profile.is_dir()
            and not self.nix_profile.is_symlink()
        ):
            shutil.rmtree(self.nix_profile)
        if self.nix_state_dir:
            self.nix_state_dir.mkdir(parents=True, exist_ok=True)

    def _nix_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.nix_state_dir:
            env["XDG_STATE_HOME"] = str(self.nix_state_dir)
            env["XDG_CACHE_HOME"] = str(self.nix_state_dir / "cache")
        return env

    def _profile_element_name(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        install_target = str(install_args[0]) if install_args else bin_name
        element = install_target.split("#", 1)[-1].split("^", 1)[0]
        return element or bin_name

    def default_install_args_handler(self, bin_name: BinName, **context) -> InstallArgs:
        return [f"nixpkgs#{bin_name}"]

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
        installer_bin = self._require_installer_bin()

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "profile",
                "add",
                *self.nix_install_args,
                "--profile",
                str(self.nix_profile),
                *install_args,
            ],
            env=self._nix_env(),
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
        profile_element = self._profile_element_name(
            bin_name,
            install_args=install_args,
        )
        installer_bin = self._require_installer_bin()

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "profile",
                "upgrade",
                *self.nix_install_args,
                "--profile",
                str(self.nix_profile),
                profile_element,
            ],
            env=self._nix_env(),
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("update", profile_element, proc)

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
        profile_element = self._profile_element_name(
            bin_name,
            install_args=install_args,
        )
        installer_bin = self._require_installer_bin()

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "profile",
                "remove",
                *self.nix_install_args,
                "--profile",
                str(self.nix_profile),
                profile_element,
            ],
            env=self._nix_env(),
            timeout=timeout,
        )
        if proc.returncode not in (0, 1):
            self._raise_proc_error("uninstall", profile_element, proc)

        return True
