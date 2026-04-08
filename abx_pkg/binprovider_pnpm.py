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


_CACHED_HOME_DIR: Path = Path("~").expanduser().absolute()


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "pnpm-cache"
try:
    pnpm_user_cache_path = user_cache_path(
        appname="pnpm",
        appauthor="abx-pkg",
        ensure_exists=True,
    )
    if os.access(pnpm_user_cache_path, os.W_OK):
        USER_CACHE_PATH = pnpm_user_cache_path
except Exception:
    pass


class PnpmProvider(BinProvider):
    """Standalone pnpm package manager provider.

    Behaves like ``NpmProvider`` but always shells out to ``pnpm`` directly,
    with no auto-switching to ``npm``.  ``minimumReleaseAge`` is enforced
    via ``--config.minimumReleaseAge=<minutes>`` (pnpm 10.16+).
    """

    name: BinProviderName = "pnpm"
    INSTALLER_BIN: BinName = "pnpm"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "pnpm_prefix"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    pnpm_prefix: Path | None = None  # None = -g global, otherwise it's a path

    cache_dir: Path = USER_CACHE_PATH
    cache_arg: str = f"--store-dir={cache_dir}"

    pnpm_install_args: list[str] = ["--loglevel=error"]

    _CACHED_LOCAL_PNPM_PREFIX: Path | None = None
    _CACHED_PNPM_VERSION: SemVer | None = None

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        # pnpm 10.16+ ships minimumReleaseAge support
        threshold = SemVer.parse("10.16.0")
        version = self._pnpm_version()
        if version is None or threshold is None:
            return False
        return version >= threshold

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    def _pnpm_version(self) -> SemVer | None:
        if self._CACHED_PNPM_VERSION is not None:
            return self._CACHED_PNPM_VERSION
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
                self._CACHED_PNPM_VERSION = version
        except Exception:
            return None
        return self._CACHED_PNPM_VERSION

    @computed_field
    @property
    def is_valid(self) -> bool:
        """False if pnpm_prefix is not created yet or if pnpm binary is not found in PATH"""
        if self.pnpm_prefix:
            pnpm_bin_dir = self.pnpm_prefix / "node_modules" / ".bin"
            if not (os.path.isdir(pnpm_bin_dir) and os.access(pnpm_bin_dir, os.R_OK)):
                return False
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.pnpm_prefix

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return (
            self.install_root / "node_modules" / ".bin" if self.install_root else None
        )

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Resolve the pnpm executable, honoring ``PNPM_BINARY`` for explicit overrides."""
        if self._INSTALLER_BIN_ABSPATH:
            return self._INSTALLER_BIN_ABSPATH

        manual_binary = os.environ.get("PNPM_BINARY")
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
                owner_paths=(self.pnpm_prefix,),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_pnpm_prefix(self) -> Self:
        self.PATH = self._load_PATH()
        return self

    def _load_PATH(self) -> str:
        PATH = self.PATH

        if self.pnpm_prefix:
            return self._merge_PATH(self.pnpm_prefix / "node_modules/.bin")

        pnpm_bin_dirs: set[Path] = set()
        pnpm_abspath = self.INSTALLER_BIN_ABSPATH
        if pnpm_abspath:
            try:
                local_root = (
                    self._CACHED_LOCAL_PNPM_PREFIX
                    or Path(
                        self.exec(
                            bin_name=pnpm_abspath,
                            cmd=["bin"],
                            quiet=True,
                            timeout=self.version_timeout,
                        ).stdout.strip(),
                    ).parent.parent
                )
                self._CACHED_LOCAL_PNPM_PREFIX = local_root

                search_dir = local_root
                stop_if_reached = [str(Path("/")), str(_CACHED_HOME_DIR)]
                num_hops, max_hops = 0, 6
                while num_hops < max_hops and str(search_dir) not in stop_if_reached:
                    try:
                        pnpm_bin_dirs.add(
                            list(search_dir.glob("node_modules/.bin"))[0],
                        )
                        break
                    except (IndexError, OSError, Exception):
                        pass
                    search_dir = search_dir.parent
                    num_hops += 1
            except Exception:
                pass

            try:
                global_bin = Path(
                    self.exec(
                        bin_name=pnpm_abspath,
                        cmd=["bin", "-g"],
                        quiet=True,
                        timeout=self.version_timeout,
                    ).stdout.strip(),
                )
                if str(global_bin):
                    pnpm_bin_dirs.add(global_bin)
            except Exception:
                pass

        return self._merge_PATH(*sorted(pnpm_bin_dirs), PATH=PATH)

    def exec(
        self,
        bin_name,
        cmd=(),
        cwd: Path | str = ".",
        quiet=False,
        **kwargs,
    ):
        """Inject ``PNPM_HOME`` (required by pnpm for global installs) before delegating."""
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        pnpm_home = Path(
            env.get("PNPM_HOME")
            or (
                self.pnpm_prefix / "node_modules/.bin"
                if self.pnpm_prefix
                else self.cache_dir / "pnpm-home"
            ),
        )
        pnpm_home.mkdir(parents=True, exist_ok=True)
        env["PNPM_HOME"] = str(pnpm_home)
        path_entries = [entry for entry in env.get("PATH", "").split(":") if entry]
        if str(pnpm_home) not in path_entries:
            env["PATH"] = ":".join([str(pnpm_home), *path_entries])
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
        """create pnpm install prefix and node_modules_dir if needed"""
        if not self.PATH or not self._CACHED_LOCAL_PNPM_PREFIX:
            self.PATH = self._load_PATH()

        if not self._ensure_writable_cache_dir(self.cache_dir):
            self.cache_arg = "--no-cache"

        if self.pnpm_prefix:
            (self.pnpm_prefix / "node_modules/.bin").mkdir(parents=True, exist_ok=True)

    def _common_cli_args(
        self,
        install_args: InstallArgs,
        *,
        postinstall_scripts: bool,
        min_release_age: float | None,
        for_remove: bool = False,
    ) -> list[str]:
        """Build the shared list of pnpm CLI flags shared by add/update/remove.

        ``pnpm remove`` rejects ``--ignore-scripts`` and ``--config.minimumReleaseAge``,
        so set ``for_remove=True`` to skip both.
        """
        explicit = [*self.pnpm_install_args, self.cache_arg, *install_args]
        cmd_args: list[str] = [*self.pnpm_install_args, self.cache_arg]
        if not for_remove:
            has_ignore_scripts = any(arg == "--ignore-scripts" for arg in explicit)
            if not postinstall_scripts and not has_ignore_scripts:
                cmd_args.append("--ignore-scripts")
            # pnpm 10+ blocks ALL postinstall scripts unless explicitly allow-listed,
            # so opt in via dangerouslyAllowAllBuilds when scripts are requested.
            # Match both ``--flag value`` (space-separated) and ``--flag=value`` forms.
            if postinstall_scripts and not any(
                arg == "--config.dangerouslyAllowAllBuilds"
                or arg.startswith("--config.dangerouslyAllowAllBuilds=")
                for arg in explicit
            ):
                cmd_args.append("--config.dangerouslyAllowAllBuilds=true")
            has_release_age = any(
                arg
                in (
                    "--config.minimumReleaseAge",
                    "--config.minimum-release-age",
                )
                or arg.startswith(
                    (
                        "--config.minimumReleaseAge=",
                        "--config.minimum-release-age=",
                    ),
                )
                for arg in explicit
            )
            if (
                min_release_age is not None
                and min_release_age > 0
                and not has_release_age
            ):
                minutes = max(int(float(min_release_age) * 24 * 60), 1)
                cmd_args.append(f"--config.minimumReleaseAge={minutes}")
        if self.pnpm_prefix:
            cmd_args.append(f"--dir={self.pnpm_prefix}")
        else:
            cmd_args.append("--global")
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
        proc = self.exec(
            bin_name=self._require_installer_bin(),
            cmd=["update", *cli_args, *install_args],
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
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        install_args = install_args or self.get_install_args(bin_name)
        if any(arg == "--ignore-scripts" for arg in install_args):
            postinstall_scripts = False

        cli_args = self._common_cli_args(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=None,
            for_remove=True,
        )
        proc = self.exec(
            bin_name=self._require_installer_bin(),
            cmd=["remove", *cli_args, *install_args],
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

        try:
            install_args = self.get_install_args(str(bin_name)) or [str(bin_name)]
            main_package = install_args[0]
            package_info = json.loads(
                self.exec(
                    bin_name=self.INSTALLER_BIN_ABSPATH,
                    cmd=["view", "--json", main_package, "bin"],
                    timeout=self.version_timeout,
                    quiet=True,
                ).stdout.strip(),
            )
            alt_bin_names = (
                package_info.get("bin", package_info)
                if isinstance(package_info, dict)
                else {}
            ).keys()
            for alt_bin_name in alt_bin_names:
                abspath = bin_abspath(alt_bin_name, PATH=self.PATH)
                if abspath:
                    return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass
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

        package = None
        try:
            install_args = self.get_install_args(str(bin_name), **context) or [
                str(bin_name),
            ]
            main_package = install_args[0]
            if main_package[0] == "@":
                package = "@" + main_package[1:].split("@", 1)[0]
            else:
                package = main_package.split("@", 1)[0]

            json_output = self.exec(
                bin_name=self.INSTALLER_BIN_ABSPATH,
                cmd=[
                    "ls",
                    f"--dir={self.pnpm_prefix}" if self.pnpm_prefix else "--global",
                    "--depth=0",
                    "--json",
                    package,
                ],
                timeout=timeout,
                quiet=True,
            ).stdout.strip()
            package_listing = json.loads(json_output)
            if isinstance(package_listing, list):
                package_listing = package_listing[0] if package_listing else {}
            return package_listing["dependencies"][package]["version"]
        except Exception:
            pass

        try:
            assert package
            modules_dir = Path(
                self.exec(
                    bin_name=self.INSTALLER_BIN_ABSPATH,
                    cmd=(
                        ["root", f"--dir={self.pnpm_prefix}"]
                        if self.pnpm_prefix
                        else ["root", "--global"]
                    ),
                    timeout=timeout,
                    quiet=True,
                ).stdout.strip(),
            )
            return json.loads((modules_dir / package / "package.json").read_text())[
                "version"
            ]
        except Exception:
            return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_pnpm.py load zx
    # ./binprovider_pnpm.py install zx
    # ./binprovider_pnpm.py get_version zx
    # ./binprovider_pnpm.py get_abspath zx
    result = pnpm = PnpmProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(pnpm, sys.argv[1])

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])

    print(result)
