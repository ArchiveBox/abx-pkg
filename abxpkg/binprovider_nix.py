#!/usr/bin/env python3
__package__ = "abxpkg"

import os
import shutil

from pathlib import Path

from pydantic import Field, model_validator, computed_field
from typing import Self

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    abxpkg_install_root_default,
    bin_abspath,
)
from .semver import SemVer
from .binprovider import BinProvider, DEFAULT_ENV_PATH, log_method_call, remap_kwargs
from .logging import format_command, format_subprocess_output, get_logger

logger = get_logger(__name__)


# Ultimate fallback when neither the constructor arg nor
# ``ABXPKG_NIX_ROOT`` nor ``ABXPKG_LIB_DIR`` is set.
DEFAULT_NIX_PROFILE = Path("~/.nix-profile").expanduser()
DEFAULT_NIX_BIN_DIR = Path("/nix/var/nix/profiles/default/bin")


class NixProvider(BinProvider):
    name: BinProviderName = "nix"
    _log_emoji = "❄️"
    INSTALLER_BIN: BinName = "nix"

    PATH: PATHStr = (
        ""  # Starts empty; setup_PATH() lazily replaces it with install_root/bin only.
    )

    install_root: Path | None = Field(
        default_factory=lambda: (
            abxpkg_install_root_default("nix") or DEFAULT_NIX_PROFILE
        ),
        validation_alias="nix_profile",
    )
    bin_dir: Path | None = None

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        env: dict[str, str] = {
            "LD_LIBRARY_PATH": ":" + str(self.install_root / "lib"),
        }
        return env

    @computed_field
    @property
    def is_valid(self) -> bool:
        install_root = self.install_root
        assert install_root is not None
        profile_bin_dir = install_root / "bin"
        if profile_bin_dir.exists() and not os.access(profile_bin_dir, os.R_OK):
            return False

        return bool(
            bin_abspath(
                self.INSTALLER_BIN,
                PATH=f"{DEFAULT_NIX_BIN_DIR}:{DEFAULT_ENV_PATH}",
            )
            or bin_abspath(self.INSTALLER_BIN),
        )

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Fill in the active Nix profile bin dir from the resolved install_root."""
        install_root = self.install_root
        assert install_root is not None
        if self.bin_dir is None:
            self.bin_dir = install_root / "bin"

        return self

    def setup_PATH(self, no_cache: bool = False) -> None:
        """Populate PATH on first use from install_root/bin only."""
        install_root = self.install_root
        assert install_root is not None
        self.PATH = self._merge_PATH(
            install_root / "bin",
            PATH=self.PATH,
            prepend=True,
        )
        super().setup_PATH(no_cache=no_cache)

    @log_method_call()
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
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(install_root.parent,),
                preserve_root=True,
            )
        install_root.parent.mkdir(parents=True, exist_ok=True)
        if (
            install_root.exists()
            and install_root.is_dir()
            and not install_root.is_symlink()
        ):
            logger.info("$ %s", format_command(["rm", "-rf", str(install_root)]))
            shutil.rmtree(install_root)

    def _profile_element_name(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
    ) -> str:
        """Map install_args to the Nix profile element name used by upgrade/remove."""
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
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "profile",
                "install",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                str(self.install_root),
                *install_args,
            ],
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
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> str:
        profile_element = self._profile_element_name(
            bin_name,
            install_args=install_args,
        )
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "profile",
                "upgrade",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                str(self.install_root),
                profile_element,
            ],
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
        no_cache: bool = False,
        timeout: int | None = None,
    ) -> bool:
        profile_element = self._profile_element_name(
            bin_name,
            install_args=install_args,
        )
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "profile",
                "remove",
                "--extra-experimental-features",
                "nix-command",
                "--extra-experimental-features",
                "flakes",
                "--profile",
                str(self.install_root),
                profile_element,
            ],
            timeout=timeout,
        )
        if proc.returncode not in (0, 1):
            self._raise_proc_error("uninstall", profile_element, proc)

        return True
