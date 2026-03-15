#!/usr/bin/env python3
__package__ = "abx_pkg"

import os
import shutil

from pathlib import Path
from typing import Optional, List

from pydantic import model_validator, TypeAdapter, computed_field
from typing_extensions import Self

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs, HostBinPath, bin_abspath
from .binprovider import BinProvider, DEFAULT_ENV_PATH, remap_kwargs


DEFAULT_NIX_PROFILE = Path(os.environ.get("ABX_PKG_NIX_PROFILE", "~/.nix-profile")).expanduser()
DEFAULT_NIX_BIN_DIR = Path("/nix/var/nix/profiles/default/bin")


class NixProvider(BinProvider):
    name: BinProviderName = "nix"
    INSTALLER_BIN: BinName = "nix"

    PATH: PATHStr = ""

    nix_profile: Path = DEFAULT_NIX_PROFILE
    nix_state_dir: Optional[Path] = None
    nix_install_args: List[str] = [
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

        abspath = bin_abspath(self.INSTALLER_BIN, PATH=f"{DEFAULT_NIX_BIN_DIR}:{DEFAULT_ENV_PATH}") or bin_abspath(DEFAULT_NIX_BIN_DIR / "nix")
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

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(owner_paths=(self.nix_profile.parent,), preserve_root=True)

        return self

    @model_validator(mode="after")
    def load_PATH_from_nix_profile(self) -> Self:
        profile_bin_dir = str(self.nix_profile / "bin")
        if profile_bin_dir not in self.PATH:
            self.PATH = TypeAdapter(PATHStr).validate_python(":".join([*self.PATH.split(":"), profile_bin_dir]))
        return self

    def setup(self) -> None:
        self.nix_profile.parent.mkdir(parents=True, exist_ok=True)
        if self.nix_profile.exists() and self.nix_profile.is_dir() and not self.nix_profile.is_symlink():
            shutil.rmtree(self.nix_profile)
        if self.nix_state_dir:
            self.nix_state_dir.mkdir(parents=True, exist_ok=True)

    def _nix_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.nix_state_dir:
            env["XDG_STATE_HOME"] = str(self.nix_state_dir)
            env["XDG_CACHE_HOME"] = str(self.nix_state_dir / "cache")
        return env

    def _profile_element_name(self, bin_name: str, install_args: Optional[InstallArgs] = None) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        install_target = str(install_args[0]) if install_args else bin_name
        element = install_target.split("#", 1)[-1].split("^", 1)[0]
        return element or bin_name

    def default_install_args_handler(self, bin_name: BinName, **context) -> InstallArgs:
        return [f"nixpkgs#{bin_name}"]

    @remap_kwargs({'packages': 'install_args'})
    def default_install_handler(self, bin_name: str, install_args: Optional[InstallArgs] = None, **context) -> str:
        self.setup()

        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f"{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)")

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["profile", "install", *self.nix_install_args, "--profile", str(self.nix_profile), *install_args],
            env=self._nix_env(),
        )
        if proc.returncode != 0:
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f"{self.__class__.__name__}: install got returncode {proc.returncode} while installing {install_args}: {install_args}")

        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

    @remap_kwargs({'packages': 'install_args'})
    def default_update_handler(self, bin_name: str, install_args: Optional[InstallArgs] = None, **context) -> str:
        profile_element = self._profile_element_name(bin_name, install_args=install_args)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f"{self.__class__.__name__} update method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)")

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["profile", "upgrade", *self.nix_install_args, "--profile", str(self.nix_profile), profile_element],
            env=self._nix_env(),
        )
        if proc.returncode != 0:
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f"{self.__class__.__name__}: update got returncode {proc.returncode} while updating {profile_element}")

        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

    @remap_kwargs({'packages': 'install_args'})
    def default_uninstall_handler(self, bin_name: str, install_args: Optional[InstallArgs] = None, **context) -> bool:
        profile_element = self._profile_element_name(bin_name, install_args=install_args)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(f"{self.__class__.__name__} uninstall method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)")

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["profile", "remove", *self.nix_install_args, "--profile", str(self.nix_profile), profile_element],
            env=self._nix_env(),
        )
        if proc.returncode not in (0, 1):
            print(proc.stdout.strip())
            print(proc.stderr.strip())
            raise Exception(f"{self.__class__.__name__}: uninstall got returncode {proc.returncode} while uninstalling {profile_element}")

        if self.nix_profile.is_symlink() or self.nix_profile.exists():
            try:
                self.nix_profile.unlink()
            except OSError:
                pass
        (self.nix_profile / "bin").mkdir(parents=True, exist_ok=True)
        return True
