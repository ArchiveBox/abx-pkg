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
    ABX_PKG_LIB_DIR,
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


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "bun-cache"
try:
    _user_cache = user_cache_path("bun", "abx-pkg", ensure_exists=True)
    if os.access(_user_cache, os.W_OK):
        USER_CACHE_PATH = _user_cache
except Exception:
    pass


class BunProvider(BinProvider):
    """Bun package manager + runtime provider.

    ``bun_prefix`` mirrors the ``BUN_INSTALL`` environment variable: when
    set, ``bun add -g`` lays out binaries under ``<bun_prefix>/bin`` and
    stores its global ``node_modules`` under ``<bun_prefix>/install/global``.

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

    bun_prefix: Path | None = (
        (ABX_PKG_LIB_DIR / "bun") if ABX_PKG_LIB_DIR else None
    )  # None = inherit BUN_INSTALL / ~/.bun

    cache_dir: Path = USER_CACHE_PATH
    cache_arg: str = ""  # re-derived per-instance from cache_dir in detect_cache_arg

    bun_install_args: list[str] = []

    @model_validator(mode="after")
    def detect_cache_arg(self) -> Self:
        # Re-derive cache_arg from the instance's cache_dir so that passing
        # ``cache_dir=Path(...)`` at construction time actually takes effect
        # (instead of silently inheriting the module-level default). An
        # explicit ``cache_arg=...`` override is respected verbatim.
        if not self.cache_arg:
            self.cache_arg = f"--cache-dir={self.cache_dir}"
        return self

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        threshold = SemVer.parse("1.3.0")
        installer = self.INSTALLER_BINARY
        version = installer.loaded_version if installer else None
        return bool(version and threshold and version >= threshold)

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.bun_prefix:
            bin_dir = self.bun_prefix / "bin"
            if not (bin_dir.is_dir() and os.access(bin_dir, os.R_OK)):
                return False
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.bun_prefix

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return self.bun_prefix / "bin" if self.bun_prefix else None

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
                Path(os.environ.get("BUN_INSTALL") or (Path("~").expanduser() / ".bun"))
                / "bin"
            )
            self.PATH = self._merge_PATH(default_bun, PATH=self.PATH)
        return self

    def exec(self, bin_name, cmd=(), cwd: Path | str = ".", quiet=False, **kwargs):
        # Inject BUN_INSTALL so global installs land in bun_prefix.
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        if self.bun_prefix:
            self.bun_prefix.mkdir(parents=True, exist_ok=True)
            (self.bun_prefix / "bin").mkdir(parents=True, exist_ok=True)
            env["BUN_INSTALL"] = str(self.bun_prefix)
            path_entries = [e for e in env.get("PATH", "").split(":") if e]
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
        if not self._ensure_writable_cache_dir(self.cache_dir):
            self.cache_arg = "--no-cache"
        if self.bun_prefix:
            (self.bun_prefix / "bin").mkdir(parents=True, exist_ok=True)
            (self.bun_prefix / "install").mkdir(parents=True, exist_ok=True)

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
            arg == "--ignore-scripts" for arg in (*self.bun_install_args, *install_args)
        ):
            postinstall_scripts = False

        cmd: list[str] = ["add", *self.bun_install_args, self.cache_arg, "-g"]
        if not postinstall_scripts:
            cmd.append("--ignore-scripts")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not any(
                arg == "--minimum-release-age"
                or arg.startswith("--minimum-release-age=")
                for arg in (*self.bun_install_args, *install_args)
            )
        ):
            cmd.append(
                f"--minimum-release-age={max(int(min_release_age * 24 * 60 * 60), 1)}",
            )
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
            arg == "--ignore-scripts" for arg in (*self.bun_install_args, *install_args)
        ):
            postinstall_scripts = False

        cmd: list[str] = ["update", *self.bun_install_args, self.cache_arg, "-g"]
        if not postinstall_scripts:
            cmd.append("--ignore-scripts")
        if (
            min_release_age is not None
            and min_release_age > 0
            and not any(
                arg == "--minimum-release-age"
                or arg.startswith("--minimum-release-age=")
                for arg in (*self.bun_install_args, *install_args)
            )
        ):
            cmd.append(
                f"--minimum-release-age={max(int(min_release_age * 24 * 60 * 60), 1)}",
            )
        cmd.extend(install_args)

        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
        if proc.returncode != 0:
            # `bun update -g <pkg>` is rejected by some bun versions; fall
            # back to `bun add -g --force <pkg>` to refresh the global store.
            cmd = ["add", *self.bun_install_args, self.cache_arg, "-g", "--force"]
            if not postinstall_scripts:
                cmd.append("--ignore-scripts")
            if (
                min_release_age is not None
                and min_release_age > 0
                and not any(
                    arg == "--minimum-release-age"
                    or arg.startswith("--minimum-release-age=")
                    for arg in (*self.bun_install_args, *install_args)
                )
            ):
                cmd.append(
                    f"--minimum-release-age={max(int(min_release_age * 24 * 60 * 60), 1)}",
                )
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

        proc = self.exec(
            bin_name=installer_bin,
            cmd=["remove", *self.bun_install_args, "-g", *install_args],
            timeout=timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", install_args, proc)
        return True

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath=None,
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

        # Fallback: read the package.json from bun's global node_modules.
        install_args = self.get_install_args(str(bin_name), **context) or [
            str(bin_name),
        ]
        main_package = install_args[0]
        package = (
            "@" + main_package[1:].split("@", 1)[0]
            if main_package.startswith("@")
            else main_package.split("@", 1)[0]
        )
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
            try:
                return json.loads(package_json.read_text())["version"]
            except Exception:
                return None
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_bun.py load zx
    # ./binprovider_bun.py install zx
    result = bun = BunProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(bun, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)
