#!/usr/bin/env python3

__package__ = "abx_pkg"

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import ClassVar, Self

from platformdirs import user_cache_path
from pydantic import Field, TypeAdapter, computed_field, model_validator

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


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "bun-cache"
try:
    bun_user_cache_path = user_cache_path(
        appname="bun",
        appauthor="abx-pkg",
        ensure_exists=True,
    )
    if os.access(bun_user_cache_path, os.W_OK):
        USER_CACHE_PATH = bun_user_cache_path
except Exception:
    pass


class BunProvider(BinProvider):
    """Bun package manager + runtime provider.

    Bun supports both global and local installs.  ``bun_prefix`` mirrors the
    ``BUN_INSTALL`` environment variable: when set, ``bun add -g`` lays out
    binaries under ``<bun_prefix>/bin`` and stores its global ``node_modules``
    under ``<bun_prefix>/install/global``.

    Security:
    - ``--ignore-scripts`` for ``postinstall_scripts=False``
    - ``--minimum-release-age=<seconds>`` for ``min_release_age`` (Bun 1.3+)
    """

    name: BinProviderName = "bun"
    INSTALLER_BIN: BinName = "bun"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "bun_prefix"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    bun_prefix: Path | None = None  # None = inherit BUN_INSTALL / ~/.bun

    cache_dir: Path = USER_CACHE_PATH
    cache_arg: str = f"--cache-dir={cache_dir}"

    bun_install_args: list[str] = []

    _CACHED_BUN_VERSION: SemVer | None = None

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        # bun 1.3+ ships --minimum-release-age
        threshold = SemVer.parse("1.3.0")
        version = self._bun_version()
        if version is None or threshold is None:
            return False
        return version >= threshold

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    def _bun_version(self) -> SemVer | None:
        if self._CACHED_BUN_VERSION is not None:
            return self._CACHED_BUN_VERSION
        if not self.INSTALLER_BIN_ABSPATH:
            return None
        try:
            proc = self.exec(
                bin_name=self.INSTALLER_BIN_ABSPATH,
                cmd=["--version"],
                quiet=True,
                timeout=self.version_timeout,
            )
            version = SemVer.parse((proc.stdout or proc.stderr).strip())
            if version:
                self._CACHED_BUN_VERSION = version
        except Exception:
            return None
        return self._CACHED_BUN_VERSION

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.bun_prefix:
            bun_bin_dir = self.bun_prefix / "bin"
            if not (os.path.isdir(bun_bin_dir) and os.access(bun_bin_dir, os.R_OK)):
                return False
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.bun_prefix

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return self.install_root / "bin" if self.install_root else None

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Resolve the bun executable, honoring ``BUN_BINARY`` for explicit overrides."""
        if self._INSTALLER_BIN_ABSPATH:
            return self._INSTALLER_BIN_ABSPATH

        manual_binary = os.environ.get("BUN_BINARY")
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
                owner_paths=(self.bun_prefix,),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_bun_prefix(self) -> Self:
        if self.bun_prefix:
            self.PATH = self._merge_PATH(self.bun_prefix / "bin")
        else:
            default_bun = (
                Path(
                    os.environ.get("BUN_INSTALL") or (Path("~").expanduser() / ".bun"),
                )
                / "bin"
            )
            self.PATH = self._merge_PATH(default_bun, PATH=self.PATH)
        return self

    def exec(
        self,
        bin_name,
        cmd=(),
        cwd: Path | str = ".",
        quiet=False,
        **kwargs,
    ):
        """Inject ``BUN_INSTALL`` so global installs land in ``bun_prefix``."""
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        if self.bun_prefix:
            self.bun_prefix.mkdir(parents=True, exist_ok=True)
            (self.bun_prefix / "bin").mkdir(parents=True, exist_ok=True)
            env["BUN_INSTALL"] = str(self.bun_prefix)
            path_entries = [entry for entry in env.get("PATH", "").split(":") if entry]
            bin_str = str(self.bun_prefix / "bin")
            if bin_str not in path_entries:
                env["PATH"] = ":".join([bin_str, *path_entries])
        return super().exec(
            bin_name=bin_name,
            cmd=cmd,
            cwd=cwd,
            quiet=quiet,
            env=env,
            **kwargs,
        )

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> None:
        """Create bun_prefix bin dir if needed."""
        if not self._ensure_writable_cache_dir(self.cache_dir):
            self.cache_arg = "--no-cache"

        if self.bun_prefix:
            (self.bun_prefix / "bin").mkdir(parents=True, exist_ok=True)
            (self.bun_prefix / "install").mkdir(parents=True, exist_ok=True)

    def _common_cli_args(
        self,
        install_args: InstallArgs,
        *,
        postinstall_scripts: bool,
        min_release_age: float | None,
    ) -> list[str]:
        explicit = [*self.bun_install_args, self.cache_arg, *install_args]
        cmd_args: list[str] = [*self.bun_install_args, self.cache_arg, "-g"]
        has_ignore_scripts = any(arg == "--ignore-scripts" for arg in explicit)
        if not postinstall_scripts and not has_ignore_scripts:
            cmd_args.append("--ignore-scripts")
        has_release_age = any(
            arg == "--minimum-release-age" or arg.startswith("--minimum-release-age=")
            for arg in explicit
        )
        if min_release_age is not None and min_release_age > 0 and not has_release_age:
            seconds = max(int(float(min_release_age) * 24 * 60 * 60), 1)
            cmd_args.append(f"--minimum-release-age={seconds}")
        return cmd_args

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
        if any(arg == "--ignore-scripts" for arg in install_args):
            postinstall_scripts = False

        cli_args = self._common_cli_args(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        proc = self.exec(
            bin_name=self._require_installer_bin(),
            cmd=["add", *cli_args, *install_args],
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
        if any(arg == "--ignore-scripts" for arg in install_args):
            postinstall_scripts = False

        cli_args = self._common_cli_args(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        installer = self._require_installer_bin()
        # `bun update -g <pkg>` is rejected by some bun versions, so we
        # fall back to `bun add -g --force <pkg>` to refresh the global store.
        proc = self.exec(
            bin_name=installer,
            cmd=["update", *cli_args, *install_args],
            timeout=timeout,
        )
        if proc.returncode != 0:
            proc = self.exec(
                bin_name=installer,
                cmd=["add", *cli_args, "--force", *install_args],
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

        proc = self.exec(
            bin_name=self._require_installer_bin(),
            cmd=["remove", *self.bun_install_args, "-g", *install_args],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", install_args, proc)
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

        if self.bun_prefix:
            candidate = self.bun_prefix / "bin" / str(bin_name)
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

        if not self.INSTALLER_BIN_ABSPATH:
            return None

        try:
            install_args = self.get_install_args(str(bin_name), **context) or [
                str(bin_name),
            ]
            main_package = install_args[0]
            if main_package[0] == "@":
                package = "@" + main_package[1:].split("@", 1)[0]
            else:
                package = main_package.split("@", 1)[0]

            global_root = (
                (self.bun_prefix / "install" / "global")
                if self.bun_prefix
                else Path(
                    os.environ.get("BUN_INSTALL") or (Path("~").expanduser() / ".bun"),
                )
                / "install"
                / "global"
            )
            package_json = global_root / "node_modules" / package / "package.json"
            if package_json.exists():
                return json.loads(package_json.read_text())["version"]
        except Exception:
            pass
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_bun.py load zx
    # ./binprovider_bun.py install zx
    # ./binprovider_bun.py get_version zx
    # ./binprovider_bun.py get_abspath zx
    result = bun = BunProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(bun, sys.argv[1])

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])

    print(result)
