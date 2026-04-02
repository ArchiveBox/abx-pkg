#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import sys
import json
import tempfile
import subprocess

from pathlib import Path
from typing import ClassVar, Self

from pydantic import model_validator, TypeAdapter, computed_field
from platformdirs import user_cache_path

from .base_types import (
    BinProviderName,
    PATHStr,
    BinName,
    InstallArgs,
    HostBinPath,
    bin_abspath,
)
from .semver import SemVer
from .binprovider import BinProvider, remap_kwargs
from .logging import format_subprocess_output, get_logger, log_subprocess_error

logger = get_logger(__name__)

# Cache these values globally because they never change at runtime
_CACHED_GLOBAL_NPM_PREFIX: tuple[str, Path] | None = None
_CACHED_HOME_DIR: Path = Path("~").expanduser().absolute()


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "npm-cache"
try:
    npm_user_cache_path = user_cache_path(
        appname="npm",
        appauthor="abx-pkg",
        ensure_exists=True,
    )
    if os.access(npm_user_cache_path, os.W_OK):
        USER_CACHE_PATH = npm_user_cache_path
except Exception:
    pass


class NpmProvider(BinProvider):
    name: BinProviderName = "npm"
    INSTALLER_BIN: BinName = "npm"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "npm_prefix"

    PATH: PATHStr = ""

    npm_prefix: Path | None = None  # None = -g global, otherwise it's a path

    cache_dir: Path = USER_CACHE_PATH
    cache_arg: str = f"--cache={cache_dir}"

    npm_install_args: list[str] = [
        "--force",
        "--no-audit",
        "--no-fund",
        "--loglevel=error",
    ]

    _CACHED_LOCAL_NPM_PREFIX: Path | None = None

    def supports_min_release_age(self, action) -> bool:
        return action in ("install", "update")

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @computed_field
    @property
    def is_valid(self) -> bool:
        """False if npm_prefix is not created yet or if npm binary is not found in PATH"""
        if self.npm_prefix:
            npm_bin_dir = self.npm_prefix / "node_modules" / ".bin"
            npm_bin_dir_exists = os.path.isdir(npm_bin_dir) and os.access(
                npm_bin_dir,
                os.R_OK,
            )
            if not npm_bin_dir_exists:
                return False

        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.npm_prefix

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return (
            self.install_root / "node_modules" / ".bin" if self.install_root else None
        )

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Resolve the package manager executable used for npm operations.

        Prefer a real `npm` binary when both `npm` and `pnpm` are available so
        the default behavior matches the provider name. `pnpm` remains a
        supported fallback on hosts that do not ship `npm`.
        """
        if self._INSTALLER_BIN_ABSPATH:
            return self._INSTALLER_BIN_ABSPATH

        manual_binary = os.environ.get("NPM_BINARY")
        if manual_binary and os.path.isabs(manual_binary):
            try:
                valid_abspath = TypeAdapter(HostBinPath).validate_python(
                    Path(manual_binary).resolve(),
                )
                self._INSTALLER_BIN_ABSPATH = valid_abspath
                return valid_abspath
            except Exception:
                return None

        abspath = (
            bin_abspath(self.INSTALLER_BIN, PATH=self.PATH)
            or bin_abspath(self.INSTALLER_BIN)
            or bin_abspath("pnpm", PATH=self.PATH)
            or bin_abspath("pnpm")
        )
        if not abspath:
            return None

        valid_abspath = TypeAdapter(HostBinPath).validate_python(abspath)
        if valid_abspath:
            self._INSTALLER_BIN_ABSPATH = valid_abspath
        return valid_abspath

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        """Detect the user (UID) to run as when executing npm."""
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.npm_prefix,),
                preserve_root=True,
            )

        return self

    @model_validator(mode="after")
    def load_PATH_from_npm_prefix(self) -> Self:
        self.PATH = self._load_PATH()
        return self

    def _load_PATH(self) -> str:
        PATH = self.PATH
        global _CACHED_GLOBAL_NPM_PREFIX

        if self.npm_prefix:
            return self._merge_PATH(self.npm_prefix / "node_modules/.bin")

        npm_bin_dirs: set[Path] = set()

        npm_abspath = self.INSTALLER_BIN_ABSPATH
        if npm_abspath:
            using_pnpm = Path(npm_abspath).name == "pnpm"
            # find all local and global npm PATHs
            npm_local_dir = self._CACHED_LOCAL_NPM_PREFIX or (
                Path(self._npm(["bin"], quiet=True).stdout.strip()).parent.parent
                if using_pnpm
                else Path(self._npm(["prefix"], quiet=True).stdout.strip())
            )
            self._CACHED_LOCAL_NPM_PREFIX = npm_local_dir

            # start at npm_local_dir and walk up to $HOME (or /), finding all npm bin dirs along the way
            search_dir = npm_local_dir
            stop_if_reached = [str(Path("/")), str(_CACHED_HOME_DIR)]
            num_hops, max_hops = 0, 6
            while num_hops < max_hops and str(search_dir) not in stop_if_reached:
                try:
                    npm_bin_dirs.add(list(search_dir.glob("node_modules/.bin"))[0])
                    break
                except (IndexError, OSError, Exception):
                    # could happen because we dont have permission to access the parent dir, or it's been moved, or many other weird edge cases...
                    pass
                search_dir = search_dir.parent
                num_hops += 1

            cached_bin, cached_dir = _CACHED_GLOBAL_NPM_PREFIX or ("", Path("/"))
            npm_global_dir = (
                cached_dir if cached_bin == Path(npm_abspath).name else None
            )
            npm_global_dir = npm_global_dir or (
                Path(self._npm(["bin", "-g"], quiet=True).stdout.strip())
                if using_pnpm
                else Path(self._npm(["prefix", "-g"], quiet=True).stdout.strip())
                / "bin"
            )
            _CACHED_GLOBAL_NPM_PREFIX = (Path(npm_abspath).name, npm_global_dir)
            npm_bin_dirs.add(npm_global_dir)

        return self._merge_PATH(*sorted(npm_bin_dirs), PATH=PATH)

    def _write_pnpm_workspace_config(self, min_release_age: float = 7.0) -> None:
        """Write/update pnpm-workspace.yaml with minimumReleaseAge if pnpm is the backend.

        Called before every install/update/uninstall so the config always
        reflects the current Binary.min_release_age value.  When the age is
        ``0`` (disabled), the ``minimumReleaseAge`` key is *removed* from the
        file so pnpm reverts to its default behavior.

        pnpm's minimumReleaseAge is config-only (no CLI flag).  The value is
        in **minutes**, converted from days.  The file is written into the
        directory pnpm operates from (npm_prefix when set, otherwise the
        pnpm home / cache dir).
        """
        npm_abspath = self.INSTALLER_BIN_ABSPATH
        if not npm_abspath or Path(npm_abspath).name != "pnpm":
            return

        days = min_release_age

        config_dir = self.npm_prefix or self.cache_dir / "pnpm-home"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "pnpm-workspace.yaml"

        # Preserve any existing content and only update/remove minimumReleaseAge
        try:
            existing = config_path.read_text()
        except FileNotFoundError:
            existing = ""

        key = "minimumReleaseAge:"

        if days <= 0:
            # Remove the key from the config if present
            if key in existing:
                lines = [
                    line
                    for line in existing.splitlines()
                    if not line.strip().startswith(key)
                ]
                content = "\n".join(lines).strip()
                if content:
                    config_path.write_text(content + "\n")
                elif config_path.exists():
                    config_path.write_text("")
                logger.debug("Removed minimumReleaseAge from %s", config_path)
            return

        minutes = int(days * 24 * 60)
        new_line = f"minimumReleaseAge: {minutes}"
        if key in existing:
            # Replace existing value
            lines = [
                new_line if line.strip().startswith(key) else line
                for line in existing.splitlines()
            ]
            config_path.write_text("\n".join(lines) + "\n")
        else:
            # Append to file
            config_path.write_text(
                existing.rstrip("\n") + f"\n{new_line}\n"
                if existing
                else f"{new_line}\n",
            )

        logger.debug("Wrote %s with minimumReleaseAge=%d", config_path, minutes)

    def _coerce_min_release_age(
        self,
        min_release_age: float | None,
        install_args: InstallArgs,
    ) -> float:
        explicit_min_release_age = self._get_option_value(
            install_args,
            "--min-release-age",
        )
        if explicit_min_release_age is None:
            return 7.0 if min_release_age is None else min_release_age
        try:
            return float(explicit_min_release_age)
        except ValueError as err:
            raise ValueError(
                f"{self.__class__.__name__} got invalid --min-release-age value: {explicit_min_release_age!r}",
            ) from err

    def _npm(
        self,
        npm_cmd: list[str],
        quiet: bool = False,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        global _CACHED_GLOBAL_NPM_PREFIX
        env = os.environ.copy()

        npm_abspath = self.INSTALLER_BIN_ABSPATH
        if not npm_abspath:
            raise Exception(
                f"{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        # `pnpm` is close enough to npm for the operations we use, but its CLI
        # shape differs enough that we normalize subcommands and flags in one
        # place instead of duplicating that branching in install/update/etc.
        subcommand, *npm_args = npm_cmd
        cmd = npm_cmd
        if Path(npm_abspath).name == "pnpm":
            pnpm_home = Path(
                env.get("PNPM_HOME")
                or (
                    self.npm_prefix / "node_modules/.bin"
                    if self.npm_prefix
                    else self.cache_dir / "pnpm-home"
                ),
            )
            pnpm_home.mkdir(parents=True, exist_ok=True)
            env["PNPM_HOME"] = str(pnpm_home)
            path_entries = [entry for entry in env.get("PATH", "").split(":") if entry]
            if str(pnpm_home) not in path_entries:
                env["PATH"] = ":".join([str(pnpm_home), *path_entries])
            cmd = [
                {
                    "install": "add",
                    "show": "view",
                    "uninstall": "remove",
                    "list": "ls",
                }.get(subcommand, subcommand),
                *(
                    f"--dir={arg.split('=', 1)[-1]}"
                    if arg.startswith("--prefix=")
                    else f"--store-dir={arg.split('=', 1)[-1]}"
                    if arg.startswith("--cache=")
                    else arg
                    for arg in npm_args
                    if arg not in ("--force", "--no-audit", "--no-fund")
                    and not arg.startswith("--min-release-age")
                ),
            ]

        return self.exec(
            bin_name=npm_abspath,
            cmd=cmd,
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
        """create npm install prefix and node_modules_dir if needed"""
        if not self.PATH or not self._CACHED_LOCAL_NPM_PREFIX:
            self.PATH = self._load_PATH()

        if not self._ensure_writable_cache_dir(self.cache_dir):
            self.cache_arg = "--no-cache"

        if self.npm_prefix:
            (self.npm_prefix / "node_modules/.bin").mkdir(parents=True, exist_ok=True)

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
        min_release_age = self._coerce_min_release_age(min_release_age, install_args)
        self._write_pnpm_workspace_config(min_release_age=min_release_age)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__} install method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        if min_version:
            # npm uses pkg@>=1.2.3 syntax for version constraints
            install_args = [
                f"{arg}@>={min_version}"
                if arg
                and not arg.startswith(("-", ".", "/"))
                and ":" not in arg.split("/")[0]
                and "@" not in arg.split("/")[-1]
                else arg
                for arg in install_args
            ]

        min_release_age_days = f"{min_release_age:g}"
        explicit_npm_args = [*self.npm_install_args, self.cache_arg, *install_args]
        npm_cmd_args = [
            *self.npm_install_args,
            self.cache_arg,
            *(
                ["--ignore-scripts"]
                if (
                    not postinstall_scripts
                    and not self._args_have_option(
                        explicit_npm_args,
                        "--ignore-scripts",
                    )
                )
                else []
            ),
            *(
                [f"--min-release-age={min_release_age_days}"]
                if min_release_age > 0
                and not self._args_have_option(explicit_npm_args, "--min-release-age")
                else []
            ),
        ]
        if self.npm_prefix:
            npm_cmd_args.append(f"--prefix={self.npm_prefix}")
        else:
            npm_cmd_args.append("--global")

        proc = self._npm(
            [
                "install",
                *npm_cmd_args,
                *install_args,
            ],
            timeout=timeout,
        )

        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} install",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__}: install got returncode {proc.returncode} while installing {install_args}: {install_args}\n{format_subprocess_output(proc.stdout, proc.stderr)}".strip(),
            )

        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

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
        min_release_age = self._coerce_min_release_age(min_release_age, install_args)
        self._write_pnpm_workspace_config(min_release_age=min_release_age)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__} update method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

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

        min_release_age_days = f"{min_release_age:g}"
        explicit_update_args = [*self.npm_install_args, self.cache_arg, *install_args]
        update_args = [
            *self.npm_install_args,
            self.cache_arg,
            *(
                ["--ignore-scripts"]
                if (
                    not postinstall_scripts
                    and not self._args_have_option(
                        explicit_update_args,
                        "--ignore-scripts",
                    )
                )
                else []
            ),
            *(
                [f"--min-release-age={min_release_age_days}"]
                if min_release_age > 0
                and not self._args_have_option(
                    explicit_update_args,
                    "--min-release-age",
                )
                else []
            ),
        ]
        if self.npm_prefix:
            update_args.append(f"--prefix={self.npm_prefix}")
        else:
            update_args.append("--global")

        proc = self._npm(
            ["update", *update_args, *install_args],
            timeout=timeout,
        )

        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} update",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__}: update got returncode {proc.returncode} while updating {install_args}: {install_args}\n{format_subprocess_output(proc.stdout, proc.stderr)}".strip(),
            )

        return (proc.stderr.strip() + "\n" + proc.stdout.strip()).strip()

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
        min_release_age = self._coerce_min_release_age(min_release_age, install_args)
        self._write_pnpm_workspace_config(min_release_age=min_release_age)
        if not self.INSTALLER_BIN_ABSPATH:
            raise Exception(
                f"{self.__class__.__name__} uninstall method is not available on this host ({self.INSTALLER_BIN} not found in $PATH)",
            )

        explicit_uninstall_args = [
            *self.npm_install_args,
            self.cache_arg,
            *install_args,
        ]
        uninstall_args = [
            *self.npm_install_args,
            self.cache_arg,
            *(
                ["--ignore-scripts"]
                if (
                    not postinstall_scripts
                    and not self._args_have_option(
                        explicit_uninstall_args,
                        "--ignore-scripts",
                    )
                )
                else []
            ),
        ]
        if self.npm_prefix:
            uninstall_args.append(f"--prefix={self.npm_prefix}")
        else:
            uninstall_args.append("--global")

        proc = self._npm(
            ["uninstall", *uninstall_args, *install_args],
            timeout=timeout,
        )

        if proc.returncode != 0:
            log_subprocess_error(
                logger,
                f"{self.__class__.__name__} uninstall",
                proc.stdout,
                proc.stderr,
            )
            raise Exception(
                f"{self.__class__.__name__}: uninstall got returncode {proc.returncode} while uninstalling {install_args}: {install_args}\n{format_subprocess_output(proc.stdout, proc.stderr)}".strip(),
            )

        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        **context,
    ) -> HostBinPath | None:
        # print(self.__class__.__name__, 'on_get_abspath', bin_name)

        # try searching for the bin_name in BinProvider.PATH first (fastest)
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass

        if not self.INSTALLER_BIN_ABSPATH:
            return None

        # fallback to using npm show to get alternate binary names based on the package, then try to find those in BinProvider.PATH
        try:
            install_args = self.get_install_args(str(bin_name)) or [str(bin_name)]
            main_package = install_args[
                0
            ]  # assume first package in list is the main one
            package_info = json.loads(
                self._npm(
                    ["show", "--json", main_package, "bin"],
                    timeout=self.version_timeout,
                    quiet=True,
                ).stdout.strip(),
            )
            # { ...
            #   "version": "2.2.3",
            #   "bin": {
            #     "mercury-parser": "cli.js",
            #     "postlight-parser": "cli.js"
            #   },
            #   ...
            # }
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

        # fallback to using npm list to get the installed package version
        try:
            install_args = self.get_install_args(str(bin_name), **context) or [
                str(bin_name),
            ]
            main_package = install_args[
                0
            ]  # assume first package in list is the main one

            # remove the package version if it exists "@postslight/parser@^1.2.3" -> "@postlight/parser"
            if main_package[0] == "@":
                package = "@" + main_package[1:].split("@", 1)[0]
            else:
                package = main_package.split("@", 1)[0]

            # npm list --depth=0 --json --prefix=<prefix> "@postlight/parser"
            # (dont use 'npm info @postlight/parser version', it shows *any* available version, not installed version)
            json_output = self._npm(
                [
                    "list",
                    f"--prefix={self.npm_prefix}" if self.npm_prefix else "--global",
                    "--depth=0",
                    "--json",
                    package,
                ],
                timeout=timeout,
                quiet=True,
            ).stdout.strip()
            # {
            #   "name": "lib",
            #   "dependencies": {
            #     "@postlight/parser": {
            #       "version": "2.2.3",
            #       "overridden": false
            #     }
            #   }
            # }
            package_listing = json.loads(json_output)
            if isinstance(package_listing, list):
                package_listing = package_listing[0] if package_listing else {}
            return package_listing["dependencies"][package]["version"]
        except Exception:
            pass

        try:
            assert package
            root_args = (
                ["root", f"--prefix={self.npm_prefix}"]
                if self.npm_prefix
                else ["root", "--global"]
            )
            modules_dir = Path(
                self._npm(
                    root_args,
                    timeout=timeout,
                    quiet=True,
                ).stdout.strip(),
            )
            version_str = json.loads(
                (modules_dir / package / "package.json").read_text(),
            )["version"]
            return version_str
        except Exception:
            raise
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_npm.py load @postlight/parser
    # ./binprovider_npm.py install @postlight/parser
    # ./binprovider_npm.py get_version @postlight/parser
    # ./binprovider_npm.py get_abspath @postlight/parser
    result = npm = NpmProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(npm, sys.argv[1])  # e.g. install

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])  # e.g. install ffmpeg

    print(result)
