#!/usr/bin/env python3

__package__ = "abxpkg"

import os
import sys
from pathlib import Path
from typing import Self

from platformdirs import user_cache_path
from pydantic import Field, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    InstallArgs,
    PATHStr,
    abxpkg_install_root_default,
    bin_abspath,
)
from .binprovider import BinProvider, env_flag_is_true, remap_kwargs
from .logging import format_subprocess_output
from .semver import SemVer


USER_CACHE_PATH = user_cache_path("deno", "abxpkg")


class DenoProvider(BinProvider):
    """Deno runtime + package manager provider.

    ``deno_root`` mirrors ``DENO_INSTALL_ROOT``: when set, ``deno install -g``
    lays out binaries under ``<deno_root>/bin``. ``deno_dir`` mirrors
    ``DENO_DIR`` for cache isolation.

    Security:
    - npm lifecycle scripts are *opt-in* in Deno (the opposite of npm).
      ``postinstall_scripts=True`` adds ``--allow-scripts``; the default
      is to skip them.
    - ``--minimum-dependency-age=<minutes>`` for ``min_release_age`` (Deno 2.5+).
    """

    name: BinProviderName = "deno"
    INSTALLER_BIN: BinName = "deno"

    PATH: PATHStr = ""  # Starts empty; setup_PATH() lazily uses install_root/bin_dir only, or DENO_INSTALL_ROOT/~/.deno/bin in ambient mode.
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABXPKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABXPKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # Mirrors $DENO_INSTALL_ROOT, defaults to ~/.deno when None.
    # Default: ABXPKG_DENO_ROOT > ABXPKG_LIB_DIR/deno > None.
    install_root: Path | None = Field(
        default_factory=lambda: abxpkg_install_root_default("deno"),
        validation_alias="deno_root",
    )
    bin_dir: Path | None = None
    deno_dir: Path | None = None  # mirrors $DENO_DIR for cache isolation

    cache_dir: Path = USER_CACHE_PATH

    deno_install_args: list[str] = ["--allow-all"]

    deno_default_scheme: str = "npm"  # 'npm' or 'jsr'

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        env: dict[str, str] = {"DENO_TLS_CA_STORE": "system"}
        if self.install_root:
            env["DENO_INSTALL_ROOT"] = str(self.install_root)
        if self.deno_dir:
            env["DENO_DIR"] = str(self.deno_dir)
        return env

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        threshold = SemVer.parse("2.5.0")
        try:
            installer = self.INSTALLER_BINARY()
        except Exception:
            return False
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

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
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "bin"
        return self

    def setup_PATH(self) -> None:
        """Populate PATH on first use from install_root/bin_dir, or DENO_INSTALL_ROOT/~/.deno/bin in ambient mode."""
        if self.bin_dir:
            self.PATH = self._merge_PATH(self.bin_dir)
        else:
            default_root = (
                Path(
                    os.environ.get("DENO_INSTALL_ROOT")
                    or (Path("~").expanduser() / ".deno"),
                )
                / "bin"
            )
            self.PATH = self._merge_PATH(default_root, PATH=self.PATH)
        super().setup_PATH()

    def exec(
        self,
        bin_name,
        cmd=(),
        cwd: Path | str = ".",
        quiet=False,
        should_log_command: bool = True,
        **kwargs,
    ):
        # Ensure install_root/deno_dir exist before deno uses them.
        if self.install_root:
            self.install_root.mkdir(parents=True, exist_ok=True)
            assert self.bin_dir is not None
            self.bin_dir.mkdir(parents=True, exist_ok=True)
        if self.deno_dir:
            self.deno_dir.mkdir(parents=True, exist_ok=True)
        return super().exec(
            bin_name=bin_name,
            cmd=cmd,
            cwd=cwd,
            quiet=quiet,
            should_log_command=should_log_command,
            **kwargs,
        )

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        no_cache: bool = False,
    ) -> None:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root,),
                preserve_root=True,
            )
        self._ensure_writable_cache_dir(self.cache_dir)
        if self.bin_dir:
            self.bin_dir.mkdir(parents=True, exist_ok=True)

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
        self.setup(no_cache=no_cache)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        postinstall_scripts = bool(postinstall_scripts)
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}@>={min_version}"
                if arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
                and "@" not in arg.split("/")[-1]
                else arg
                for arg in install_args
            ]

        cmd: list[str] = ["install"]
        if no_cache:
            cmd.append("--reload")
        cmd.extend([*self.deno_install_args, "-g"])
        if not any(arg in ("-f", "--force") for arg in install_args):
            cmd.append("--force")
        if not any(
            arg in ("-n", "--name") or arg.startswith("--name=") for arg in install_args
        ):
            cmd.extend(["-n", bin_name])
        if postinstall_scripts and not any(
            arg == "--allow-scripts" or arg.startswith("--allow-scripts=")
            for arg in install_args
        ):
            cmd.append("--allow-scripts")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not any(
                arg == "--minimum-dependency-age"
                or arg.startswith("--minimum-dependency-age=")
                for arg in install_args
            )
        ):
            cmd.append(
                f"--minimum-dependency-age={max(int(min_release_age * 24 * 60), 1)}",
            )
        # Auto-prefix bare names with the default scheme (npm: or jsr:).
        for arg in install_args:
            if (
                arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
            ):
                cmd.append(f"{self.deno_default_scheme}:{arg}")
            else:
                cmd.append(arg)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
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
        # ``deno install -gf`` re-installs from scratch, which is the
        # idiomatic update path for global executables.
        return self.default_install_handler(
            bin_name=bin_name,
            install_args=install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            min_version=min_version,
            no_cache=no_cache,
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
        installer_bin = self.INSTALLER_BINARY().loaded_abspath
        assert installer_bin
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["uninstall", "-g", bin_name],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", [bin_name], proc)
        return True


if __name__ == "__main__":
    # Usage:
    # ./binprovider_deno.py load cowsay
    # ./binprovider_deno.py install cowsay
    result = deno = DenoProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(deno, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)
