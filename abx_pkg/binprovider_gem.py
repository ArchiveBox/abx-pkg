#!/usr/bin/env python3
__package__ = "abx_pkg"

import os

from pathlib import Path

from pydantic import model_validator, TypeAdapter, computed_field
from typing import Self

from .base_types import BinProviderName, PATHStr, BinName, InstallArgs
from .binprovider import BinProvider, DEFAULT_ENV_PATH, remap_kwargs
from .logging import get_logger, log_subprocess_error

logger = get_logger(__name__)


DEFAULT_GEM_HOME = Path(os.environ.get("GEM_HOME", "~/.local/share/gem")).expanduser()


class GemProvider(BinProvider):
    name: BinProviderName = "gem"
    INSTALLER_BIN: BinName = "gem"

    PATH: PATHStr = DEFAULT_ENV_PATH

    gem_home: Path | None = None
    gem_bindir: Path | None = None
    gem_install_args: list[str] = ["--no-document"]

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.gem_bindir and not (
            self.gem_bindir.is_dir() and os.access(self.gem_bindir, os.R_OK)
        ):
            return False

        return bool(self.INSTALLER_BIN_ABSPATH)

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.gem_home, self.gem_bindir),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_gem_home(self) -> Self:
        bindir = self._bindir()
        if self.gem_home or self.gem_bindir:
            self.PATH = TypeAdapter(PATHStr).validate_python(str(bindir))
        elif str(bindir) not in self.PATH:
            self.PATH = TypeAdapter(PATHStr).validate_python(
                ":".join([*self.PATH.split(":"), str(bindir)]),
            )
        return self

    def _gem_home(self) -> Path:
        return (self.gem_home or DEFAULT_GEM_HOME).expanduser()

    def _bindir(self) -> Path:
        return (self.gem_bindir or (self._gem_home() / "bin")).expanduser()

    def setup(self) -> None:
        self._gem_home().mkdir(parents=True, exist_ok=True)
        self._bindir().mkdir(parents=True, exist_ok=True)

    def _gem_install_args(self) -> list[str]:
        return [
            "--install-dir",
            str(self._gem_home()),
            "--bindir",
            str(self._bindir()),
            *self.gem_install_args,
        ]

    def _gem_scope_args(self) -> list[str]:
        return [
            "-i",
            str(self._gem_home()),
        ]

    def _gem_env(self) -> dict[str, str]:
        env = os.environ.copy()
        gem_home = str(self._gem_home())
        env["GEM_HOME"] = gem_home
        env["GEM_PATH"] = gem_home
        return env

    def _patch_generated_wrappers(self) -> None:
        gem_home = str(self._gem_home())
        gem_use_paths_line = f'Gem.use_paths("{gem_home}", ["{gem_home}"])'

        for wrapper_path in self._bindir().iterdir():
            if not wrapper_path.is_file():
                continue

            wrapper_text = wrapper_path.read_text(encoding="utf-8")
            if (
                gem_use_paths_line in wrapper_text
                or "Gem.activate_bin_path" not in wrapper_text
            ):
                continue

            if "require 'rubygems'" in wrapper_text:
                wrapper_text = wrapper_text.replace(
                    "require 'rubygems'",
                    f"require 'rubygems'\n{gem_use_paths_line}",
                    1,
                )
            else:
                wrapper_lines = wrapper_text.splitlines()
                insert_at = (
                    1 if wrapper_lines and wrapper_lines[0].startswith("#!") else 0
                )
                wrapper_lines[insert_at:insert_at] = [gem_use_paths_line, ""]
                wrapper_text = "\n".join(wrapper_lines) + "\n"

            wrapper_path.write_text(wrapper_text, encoding="utf-8")

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

        min_version = context.get("min_version")
        version_args = ["--version", f">={min_version}"] if min_version else []

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["install", *self._gem_install_args(), *version_args, *install_args],
            env=self._gem_env(),
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

        self._patch_generated_wrappers()
        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> str:
        self.setup()

        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__} update method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        min_version = context.get("min_version")
        version_args = ["--version", f">={min_version}"] if min_version else []

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=["update", *self._gem_install_args(), *version_args, *install_args],
            env=self._gem_env(),
        )
        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} update",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__}: update got returncode {proc.returncode} while updating {install_args}: {install_args}",
            )

        self._patch_generated_wrappers()
        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> bool:
        install_args = install_args or self.get_install_args(bin_name)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__} uninstall method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
            cmd=[
                "uninstall",
                "--all",
                "--executables",
                "--ignore-dependencies",
                "--force",
                *self._gem_scope_args(),
                *install_args,
            ],
            env=self._gem_env(),
        )
        if proc.returncode != 0 and "is not installed in GEM_HOME" not in proc.stderr:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} uninstall",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__}: uninstall got returncode {proc.returncode} while uninstalling {install_args}: {install_args}",
            )

        bindir = self._bindir()
        for install_arg in install_args:
            (bindir / install_arg).unlink(missing_ok=True)
        (bindir / bin_name).unlink(missing_ok=True)

        return True
