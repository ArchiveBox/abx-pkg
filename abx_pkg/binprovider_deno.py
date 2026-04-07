#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import subprocess
import sys
import tempfile

from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, TypeAdapter, computed_field, model_validator
from platformdirs import user_cache_path

from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    bin_abspath,
)
from .binprovider import (
    BinProvider,
    env_flag_is_true,
    remap_kwargs,
)
from .logging import format_subprocess_output, get_logger
from .semver import SemVer

logger = get_logger(__name__)


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "deno-cache"
try:
    deno_user_cache_path = user_cache_path(
        appname="deno",
        appauthor="abx-pkg",
        ensure_exists=True,
    )
    if os.access(deno_user_cache_path, os.W_OK):
        USER_CACHE_PATH = deno_user_cache_path
except Exception:
    pass


class DenoProvider(BinProvider):
    """Deno runtime + package manager provider.

    Deno installs both jsr and npm packages.  ``deno_root`` mirrors
    ``DENO_INSTALL_ROOT``: when set, ``deno install -g`` lays out
    binaries under ``<deno_root>/bin``.  ``deno_dir`` mirrors ``DENO_DIR``
    for cache isolation.

    Security:
    - npm lifecycle scripts are *opt-in* in Deno (the opposite of npm).
      ``postinstall_scripts=True`` adds ``--allow-scripts``; the default
      is to skip them.
    - ``--minimum-dependency-age=<minutes>`` for ``min_release_age`` (Deno 2.5+).
    """

    name: BinProviderName = "deno"
    INSTALLER_BIN: BinName = "deno"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "deno_root"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    deno_root: Path | None = None  # mirrors $DENO_INSTALL_ROOT, defaults to ~/.deno
    deno_dir: Path | None = None  # mirrors $DENO_DIR for cache isolation

    cache_dir: Path = USER_CACHE_PATH

    deno_install_args: list[str] = ["--allow-all"]

    deno_default_scheme: str = "npm"  # 'npm' or 'jsr'

    _CACHED_DENO_VERSION: SemVer | None = None

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        version = self._deno_version()
        # --minimum-dependency-age landed in Deno 2.5
        return bool(version and version >= SemVer((2, 5, 0)))

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @staticmethod
    def _install_args_have_option(args: InstallArgs, *options: str) -> bool:
        return any(
            arg == option or arg.startswith(f"{option}=")
            for arg in args
            for option in options
        )

    def _deno_version(self) -> SemVer | None:
        if self._CACHED_DENO_VERSION is not None:
            return self._CACHED_DENO_VERSION
        deno_abspath = self.INSTALLER_BIN_ABSPATH
        if not deno_abspath:
            return None
        try:
            proc = self.exec(
                bin_name=deno_abspath,
                cmd=["--version"],
                quiet=True,
                timeout=self.version_timeout,
            )
            output = (proc.stdout or proc.stderr).strip()
            # `deno 2.7.11 (stable, ...)`
            for token in output.split():
                version = SemVer.parse(token)
                if version:
                    self._CACHED_DENO_VERSION = version
                    return version
        except Exception:
            return None
        return None

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.deno_root:
            deno_bin_dir = self.deno_root / "bin"
            if not (os.path.isdir(deno_bin_dir) and os.access(deno_bin_dir, os.R_OK)):
                return False
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.deno_root

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return self.install_root / "bin" if self.install_root else None

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Resolve the deno executable, honoring ``DENO_BINARY`` for explicit overrides."""
        if self._INSTALLER_BIN_ABSPATH:
            return self._INSTALLER_BIN_ABSPATH

        manual_binary = os.environ.get("DENO_BINARY")
        if manual_binary and os.path.isabs(manual_binary):
            try:
                valid_abspath = TypeAdapter(HostBinPath).validate_python(
                    Path(manual_binary).resolve(),
                )
                self._INSTALLER_BIN_ABSPATH = valid_abspath
                return valid_abspath
            except Exception:
                return None

        abspath = bin_abspath(self.INSTALLER_BIN, PATH=self.PATH) or bin_abspath(
            self.INSTALLER_BIN,
        )
        if not abspath:
            return None
        valid_abspath = TypeAdapter(HostBinPath).validate_python(abspath)
        if valid_abspath:
            self._INSTALLER_BIN_ABSPATH = valid_abspath
        return valid_abspath

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.deno_root,),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_deno_root(self) -> Self:
        if self.deno_root:
            self.PATH = self._merge_PATH(self.deno_root / "bin")
        else:
            default_root = (
                Path(
                    os.environ.get("DENO_INSTALL_ROOT")
                    or (Path("~").expanduser() / ".deno"),
                )
                / "bin"
            )
            self.PATH = self._merge_PATH(default_root, PATH=self.PATH)
        return self

    @staticmethod
    def _min_release_age_minutes(min_release_age: float | None) -> int | None:
        if min_release_age is None or min_release_age <= 0:
            return None
        return max(int(float(min_release_age) * 24 * 60), 1)

    def _qualify_package(self, package: str) -> str:
        """Add an ``npm:`` / ``jsr:`` scheme prefix if the package isn't already qualified."""
        if package.startswith(("-", ".", "/")):
            return package
        if ":" in package.split("/")[0]:
            return package
        return f"{self.deno_default_scheme}:{package}"

    def _strip_scheme(self, package: str) -> str:
        for scheme in ("npm:", "jsr:", "node:", "https://", "http://"):
            if package.startswith(scheme):
                return package[len(scheme) :]
        return package

    def _deno(
        self,
        deno_cmd: list[str],
        quiet: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        deno_abspath = self._require_installer_bin()

        # Use the system trust store so jsr/npm registry TLS works on
        # hosts that ship corporate / sandboxed CA bundles.
        env.setdefault("DENO_TLS_CA_STORE", "system")

        if self.deno_root:
            self.deno_root.mkdir(parents=True, exist_ok=True)
            (self.deno_root / "bin").mkdir(parents=True, exist_ok=True)
            env["DENO_INSTALL_ROOT"] = str(self.deno_root)

        if self.deno_dir:
            self.deno_dir.mkdir(parents=True, exist_ok=True)
            env["DENO_DIR"] = str(self.deno_dir)

        return self.exec(
            bin_name=deno_abspath,
            cmd=deno_cmd,
            quiet=quiet,
            timeout=timeout,
            env=env,
        )

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> None:
        """Create deno_root bin dir if needed."""
        self._ensure_writable_cache_dir(self.cache_dir)
        if self.deno_root:
            (self.deno_root / "bin").mkdir(parents=True, exist_ok=True)

    def _build_install_command(
        self,
        *,
        bin_name: str,
        install_args: InstallArgs,
        postinstall_scripts: bool,
        min_release_age: float | None,
        force: bool = False,
    ) -> list[str]:
        explicit = list(install_args)
        cmd: list[str] = ["install", *self.deno_install_args, "-g"]

        if force and not self._install_args_have_option(explicit, "-f", "--force"):
            cmd.append("--force")

        if not self._install_args_have_option(explicit, "-n", "--name"):
            cmd.extend(["-n", bin_name])

        if postinstall_scripts and not self._install_args_have_option(
            explicit,
            "--allow-scripts",
        ):
            cmd.append("--allow-scripts")

        minutes = self._min_release_age_minutes(min_release_age)
        if minutes is not None and not self._install_args_have_option(
            explicit, "--minimum-dependency-age"
        ):
            cmd.append(f"--minimum-dependency-age={minutes}")

        for arg in install_args:
            cmd.append(self._qualify_package(arg) if arg else arg)
        return cmd

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
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
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
        effective_release_age = (
            7.0 if min_release_age is None else float(min_release_age)
        )

        self._require_installer_bin()
        cmd = self._build_install_command(
            bin_name=bin_name,
            install_args=install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=effective_release_age,
            force=True,
        )
        proc = self._deno(cmd, timeout=timeout)
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
        # ``deno install -gf`` re-installs from scratch, which is the
        # idiomatic update path for global executables.
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
        self._require_installer_bin()

        proc = self._deno(["uninstall", "-g", bin_name], timeout=timeout)
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", [bin_name], proc)
        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        **context,
    ) -> HostBinPath | None:
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass

        if not self.INSTALLER_BIN_ABSPATH:
            return None

        if self.deno_root:
            candidate = self.deno_root / "bin" / str(bin_name)
            if candidate.exists():
                return TypeAdapter(HostBinPath).validate_python(candidate)
        return None

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        **context,
    ) -> SemVer | None:
        try:
            version = self._version_from_exec(
                bin_name,
                abspath=abspath,
                timeout=timeout,
            )
            if version:
                return version
        except ValueError:
            pass
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_deno.py load cowsay
    # ./binprovider_deno.py install cowsay
    # ./binprovider_deno.py get_version cowsay
    # ./binprovider_deno.py get_abspath cowsay
    result = deno = DenoProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(deno, sys.argv[1])

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])

    print(result)
