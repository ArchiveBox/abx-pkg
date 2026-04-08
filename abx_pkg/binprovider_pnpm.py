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
from .binprovider import BinProvider, env_flag_is_true, remap_kwargs
from .logging import format_subprocess_output
from .semver import SemVer


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "pnpm-cache"
try:
    _user_cache = user_cache_path("pnpm", "abx-pkg", ensure_exists=True)
    if os.access(_user_cache, os.W_OK):
        USER_CACHE_PATH = _user_cache
except Exception:
    pass


class PnpmProvider(BinProvider):
    """Standalone pnpm package manager provider.

    Behaves like ``NpmProvider`` but always shells out to ``pnpm`` directly,
    with no auto-switching to ``npm``.  ``minimumReleaseAge`` is enforced via
    ``--config.minimumReleaseAge=<minutes>`` (pnpm 10.16+).
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
    cache_arg: str = ""  # re-derived per-instance from cache_dir in detect_cache_arg

    pnpm_install_args: list[str] = ["--loglevel=error"]

    @model_validator(mode="after")
    def detect_cache_arg(self) -> Self:
        # Re-derive cache_arg from the instance's cache_dir so that passing
        # ``cache_dir=Path(...)`` at construction time actually takes effect
        # (instead of silently inheriting the module-level default).
        if not self.cache_arg or "--store-dir=" in self.cache_arg:
            self.cache_arg = f"--store-dir={self.cache_dir}"
        return self

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        threshold = SemVer.parse("10.16.0")
        installer = self.INSTALLER_BINARY
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

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

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.pnpm_prefix:
            bin_dir = self.pnpm_prefix / "node_modules" / ".bin"
            if not (bin_dir.is_dir() and os.access(bin_dir, os.R_OK)):
                return False
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.pnpm_prefix

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return self.pnpm_prefix / "node_modules" / ".bin" if self.pnpm_prefix else None

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
        if self.pnpm_prefix:
            self.PATH = self._merge_PATH(self.pnpm_prefix / "node_modules" / ".bin")
        else:
            # In global mode, pnpm puts shims under PNPM_HOME (from env, or
            # ``<cache_dir>/pnpm-home`` — the same fallback exec() uses).
            pnpm_home = os.environ.get("PNPM_HOME") or str(
                self.cache_dir / "pnpm-home",
            )
            self.PATH = self._merge_PATH(pnpm_home, PATH=self.PATH)
        return self

    def exec(self, bin_name, cmd=(), cwd: Path | str = ".", quiet=False, **kwargs):
        # pnpm REQUIRES PNPM_HOME on PATH for global installs to work.
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        pnpm_home = Path(
            env.get("PNPM_HOME")
            or (
                self.pnpm_prefix / "node_modules" / ".bin"
                if self.pnpm_prefix
                else self.cache_dir / "pnpm-home"
            ),
        )
        pnpm_home.mkdir(parents=True, exist_ok=True)
        env["PNPM_HOME"] = str(pnpm_home)
        path_entries = [e for e in env.get("PATH", "").split(":") if e]
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
        if not self._ensure_writable_cache_dir(self.cache_dir):
            self.cache_arg = "--no-cache"
        if self.pnpm_prefix:
            (self.pnpm_prefix / "node_modules" / ".bin").mkdir(
                parents=True,
                exist_ok=True,
            )

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
        installer_bin = self._require_installer_bin()
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
        if any(
            arg == "--ignore-scripts"
            for arg in (*self.pnpm_install_args, *install_args)
        ):
            postinstall_scripts = False

        cmd: list[str] = ["add", *self.pnpm_install_args, self.cache_arg]
        if not postinstall_scripts:
            cmd.append("--ignore-scripts")
        else:
            # pnpm 10+ blocks ALL postinstall scripts unless explicitly allowed.
            cmd.append("--config.dangerouslyAllowAllBuilds=true")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not any(
                arg == "--config.minimumReleaseAge"
                or arg.startswith("--config.minimumReleaseAge=")
                for arg in (*self.pnpm_install_args, *install_args)
            )
        ):
            cmd.append(
                f"--config.minimumReleaseAge={max(int(min_release_age * 24 * 60), 1)}",
            )
        cmd.append(f"--dir={self.pnpm_prefix}" if self.pnpm_prefix else "--global")
        cmd.extend(install_args)

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
        timeout: int | None = None,
    ) -> str:
        self.setup()
        installer_bin = self._require_installer_bin()
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
        if any(
            arg == "--ignore-scripts"
            for arg in (*self.pnpm_install_args, *install_args)
        ):
            postinstall_scripts = False

        cmd: list[str] = ["update", *self.pnpm_install_args, self.cache_arg]
        if not postinstall_scripts:
            cmd.append("--ignore-scripts")
        else:
            cmd.append("--config.dangerouslyAllowAllBuilds=true")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not any(
                arg == "--config.minimumReleaseAge"
                or arg.startswith("--config.minimumReleaseAge=")
                for arg in (*self.pnpm_install_args, *install_args)
            )
        ):
            cmd.append(
                f"--config.minimumReleaseAge={max(int(min_release_age * 24 * 60), 1)}",
            )
        cmd.append(f"--dir={self.pnpm_prefix}" if self.pnpm_prefix else "--global")
        cmd.extend(install_args)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
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
        installer_bin = self._require_installer_bin()
        install_args = install_args or self.get_install_args(bin_name)

        # pnpm remove rejects --ignore-scripts and --config.minimumReleaseAge,
        # so don't pass either even if they were set as provider defaults.
        cmd: list[str] = ["remove", *self.pnpm_install_args, self.cache_arg]
        cmd.append(f"--dir={self.pnpm_prefix}" if self.pnpm_prefix else "--global")
        cmd.extend(install_args)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
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

        # Fallback: ask `pnpm view` for the package's bin entries and look
        # them up by name in our PATH.
        try:
            install_args = self.get_install_args(str(bin_name)) or [str(bin_name)]
            package_info = json.loads(
                self.exec(
                    bin_name=self.INSTALLER_BIN_ABSPATH,
                    cmd=["view", "--json", install_args[0], "bin"],
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

        # Fallback: ask `pnpm ls --json` for the installed version of the
        # main package, and finally fall back to reading its package.json.
        install_args = self.get_install_args(str(bin_name), **context) or [
            str(bin_name),
        ]
        main_package = install_args[0]
        package = (
            "@" + main_package[1:].split("@", 1)[0]
            if main_package.startswith("@")
            else main_package.split("@", 1)[0]
        )
        try:
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
            listing = json.loads(json_output)
            if isinstance(listing, list):
                listing = listing[0] if listing else {}
            return listing["dependencies"][package]["version"]
        except Exception:
            pass

        try:
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
    result = pnpm = PnpmProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(pnpm, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)
