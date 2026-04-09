#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import sys
import json
import tempfile

from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, model_validator, TypeAdapter, computed_field
from platformdirs import user_cache_path

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
from .binprovider import (
    BinProvider,
    env_flag_is_true,
    remap_kwargs,
)
from .logging import format_subprocess_output

# Cache these values globally because they never change at runtime
_CACHED_GLOBAL_NPM_PREFIX: Path | None = None
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
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    # None = -g global, otherwise it's a path.
    # Default: ABX_PKG_NPM_ROOT > ABX_PKG_LIB_DIR/npm > None.
    npm_prefix: Path | None = abx_pkg_install_root_default("npm")

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
        if action not in ("install", "update"):
            return False

        npm_abspath = self.INSTALLER_BIN_ABSPATH
        if not npm_abspath:
            return False

        # npm 11+ supports ``--min-release-age``. Probe ``npm install --help``
        # rather than version-sniffing because the flag was backported to
        # several 10.x releases and the exact version varies by distro.
        proc = self.exec(
            bin_name=npm_abspath,
            cmd=["install", "--help"],
            quiet=True,
            timeout=self.version_timeout,
        )
        help_text = "\n".join(
            part.strip() for part in (proc.stdout, proc.stderr) if part.strip()
        )
        return proc.returncode == 0 and "--min-release-age" in help_text

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @staticmethod
    def _install_args_have_option(args: InstallArgs, *options: str) -> bool:
        return any(
            arg == option or arg.startswith(f"{option}=")
            for arg in args
            for option in options
        )

    @staticmethod
    def _install_arg_value(args: InstallArgs, *options: str) -> str | None:
        for idx, arg in enumerate(args):
            for option in options:
                if arg == option and idx + 1 < len(args):
                    return args[idx + 1]
                if arg.startswith(f"{option}="):
                    return arg.split("=", 1)[1]
        return None

    def _resolve_security_constraints(
        self,
        install_args: InstallArgs,
        *,
        postinstall_scripts: bool,
        min_release_age: float | None,
    ) -> tuple[bool, float]:
        effective_postinstall_scripts = postinstall_scripts
        if self._install_args_have_option(install_args, "--ignore-scripts"):
            effective_postinstall_scripts = False

        explicit_min_release_age = self._install_arg_value(
            install_args,
            "--min-release-age",
        )
        if explicit_min_release_age is not None:
            try:
                effective_min_release_age = float(explicit_min_release_age)
            except ValueError as err:
                raise ValueError(
                    f"{self.__class__.__name__} got invalid --min-release-age value: {explicit_min_release_age!r}",
                ) from err
        else:
            effective_min_release_age = (
                7.0 if min_release_age is None else min_release_age
            )

        return effective_postinstall_scripts, effective_min_release_age

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
        """Resolve the npm executable, honoring ``NPM_BINARY`` for explicit overrides."""
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

        npm_abspath = self.INSTALLER_BIN_ABSPATH
        if not npm_abspath:
            return PATH

        npm_bin_dirs: set[Path] = set()

        # find all local and global npm PATHs
        npm_local_dir = self._CACHED_LOCAL_NPM_PREFIX or Path(
            self.exec(
                bin_name=npm_abspath,
                cmd=["prefix"],
                quiet=True,
            ).stdout.strip(),
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

        npm_global_dir = _CACHED_GLOBAL_NPM_PREFIX or (
            Path(
                self.exec(
                    bin_name=npm_abspath,
                    cmd=["prefix", "-g"],
                    quiet=True,
                ).stdout.strip(),
            )
            / "bin"
        )
        _CACHED_GLOBAL_NPM_PREFIX = npm_global_dir
        npm_bin_dirs.add(npm_global_dir)

        return self._merge_PATH(*sorted(npm_bin_dirs), PATH=PATH)

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

    def _build_mutation_args(
        self,
        install_args: InstallArgs,
        *,
        postinstall_scripts: bool,
        min_release_age: float,
    ) -> list[str]:
        """Shared ``install``/``update`` CLI args (security flags + prefix)."""
        explicit_args = [*self.npm_install_args, self.cache_arg, *install_args]
        min_release_age_days = f"{min_release_age:g}"
        extra: list[str] = []
        if not postinstall_scripts and not self._install_args_have_option(
            explicit_args,
            "--ignore-scripts",
        ):
            extra.append("--ignore-scripts")
        if min_release_age > 0 and not self._install_args_have_option(
            explicit_args,
            "--min-release-age",
        ):
            extra.append(f"--min-release-age={min_release_age_days}")

        mutation_args = [
            *self.npm_install_args,
            self.cache_arg,
            *extra,
        ]
        if self.npm_prefix:
            mutation_args.append(f"--prefix={self.npm_prefix}")
        else:
            mutation_args.append("--global")
        return mutation_args

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
        self.setup()
        npm_abspath = self._require_installer_bin()
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
        postinstall_scripts, min_release_age = self._resolve_security_constraints(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )

        mutation_args = self._build_mutation_args(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        proc = self.exec(
            bin_name=npm_abspath,
            cmd=["install", *mutation_args, *install_args],
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
        self.setup()
        npm_abspath = self._require_installer_bin()
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
        postinstall_scripts, min_release_age = self._resolve_security_constraints(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )

        mutation_args = self._build_mutation_args(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        proc = self.exec(
            bin_name=npm_abspath,
            cmd=["update", *mutation_args, *install_args],
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
        npm_abspath = self._require_installer_bin()
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        install_args = install_args or self.get_install_args(bin_name)
        postinstall_scripts, _ = self._resolve_security_constraints(
            install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )

        explicit_args = [*self.npm_install_args, self.cache_arg, *install_args]
        uninstall_args = [
            *self.npm_install_args,
            self.cache_arg,
            *(
                ["--ignore-scripts"]
                if (
                    not postinstall_scripts
                    and not self._install_args_have_option(
                        explicit_args,
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

        proc = self.exec(
            bin_name=npm_abspath,
            cmd=["uninstall", *uninstall_args, *install_args],
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
        # print(self.__class__.__name__, 'on_get_abspath', bin_name)

        # try searching for the bin_name in BinProvider.PATH first (fastest)
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass

        npm_abspath = self.INSTALLER_BIN_ABSPATH
        if not npm_abspath:
            return None

        # fallback to using npm show to get alternate binary names based on the package, then try to find those in BinProvider.PATH
        try:
            install_args = self.get_install_args(str(bin_name)) or [str(bin_name)]
            main_package = install_args[
                0
            ]  # assume first package in list is the main one
            package_info = json.loads(
                self.exec(
                    bin_name=npm_abspath,
                    cmd=["show", "--json", main_package, "bin"],
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

        npm_abspath = self.INSTALLER_BIN_ABSPATH
        if not npm_abspath:
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
            json_output = self.exec(
                bin_name=npm_abspath,
                cmd=[
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
                self.exec(
                    bin_name=npm_abspath,
                    cmd=root_args,
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
