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
)
from .semver import SemVer
from .binprovider import BinProvider, DEFAULT_ENV_PATH, remap_kwargs
from .logging import format_subprocess_output


DEFAULT_GEM_HOME = Path(os.environ.get("GEM_HOME", "~/.local/share/gem")).expanduser()


class GemProvider(BinProvider):
    name: BinProviderName = "gem"
    INSTALLER_BIN: BinName = "gem"

    PATH: PATHStr = DEFAULT_ENV_PATH

    install_root: Path | None = Field(
        default_factory=lambda: abx_pkg_install_root_default("gem"),
        validation_alias="gem_home",
    )
    bin_dir: Path | None = Field(default=None, validation_alias="gem_bindir")
    gem_install_args: list[str] = ["--no-document"]

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.bin_dir and not (
            self.bin_dir.is_dir() and os.access(self.bin_dir, os.R_OK)
        ):
            return False

        return bool(self.INSTALLER_BIN_ABSPATH)

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.install_root is None:
            self.install_root = DEFAULT_GEM_HOME
        else:
            self.install_root = self.install_root.expanduser()
        if self.bin_dir is None:
            self.bin_dir = (self.install_root / "bin").expanduser()
        else:
            self.bin_dir = self.bin_dir.expanduser()
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root, self.bin_dir),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_gem_home(self) -> Self:
        bin_dir = self.bin_dir
        assert bin_dir is not None
        if self.install_root != DEFAULT_GEM_HOME or "bin_dir" in self.model_fields_set:
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

    def _patch_generated_wrappers(self) -> None:
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None
        gem_home = str(install_root)
        gem_use_paths_line = f'Gem.use_paths("{gem_home}", ["{gem_home}"])'

        for wrapper_path in bin_dir.iterdir():
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
        installer_bin = self._require_installer_bin()
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None
        gem_home = str(install_root)

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "install",
                "--install-dir",
                gem_home,
                "--bindir",
                str(bin_dir),
                *self.gem_install_args,
                *install_args,
            ],
            env={
                **os.environ,
                "GEM_HOME": gem_home,
                "GEM_PATH": gem_home,
            },
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", install_args, proc)

        self._patch_generated_wrappers()
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
        installer_bin = self._require_installer_bin()
        install_root = self.install_root
        bin_dir = self.bin_dir
        assert install_root is not None
        assert bin_dir is not None
        gem_home = str(install_root)

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "update",
                "--install-dir",
                gem_home,
                "--bindir",
                str(bin_dir),
                *self.gem_install_args,
                *install_args,
            ],
            env={
                **os.environ,
                "GEM_HOME": gem_home,
                "GEM_PATH": gem_home,
            },
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("update", install_args, proc)

        self._patch_generated_wrappers()
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
        installer_bin = self._require_installer_bin()
        install_root = self.install_root
        assert install_root is not None
        gem_home = str(install_root)

        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "uninstall",
                "--all",
                "--executables",
                "--ignore-dependencies",
                "--force",
                "-i",
                gem_home,
                *install_args,
            ],
            env={
                **os.environ,
                "GEM_HOME": gem_home,
                "GEM_PATH": gem_home,
            },
            timeout=timeout,
        )
        if proc.returncode != 0 and "is not installed in GEM_HOME" not in proc.stderr:
            self._raise_proc_error("uninstall", install_args, proc)

        bindir = self.bin_dir
        assert bindir is not None
        for install_arg in install_args:
            (bindir / install_arg).unlink(missing_ok=True)
        (bindir / bin_name).unlink(missing_ok=True)

        return True
