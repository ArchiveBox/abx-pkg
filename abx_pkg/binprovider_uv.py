#!/usr/bin/env python3
__package__ = "abx_pkg"
import os
import shutil
import re
import sys
import tempfile
from pathlib import Path
from typing import Self
from platformdirs import user_cache_path
from pydantic import Field, TypeAdapter, computed_field, model_validator
from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abx_pkg_install_root_default,
    bin_abspath,
)
from .binprovider import BinProvider, env_flag_is_true, remap_kwargs
from .logging import format_subprocess_output
from .semver import SemVer

USER_CACHE_PATH = Path(tempfile.gettempdir()) / "uv-cache"
try:
    _user_cache = user_cache_path("uv", "abx-pkg", ensure_exists=True)
    if os.access(_user_cache, os.W_OK):
        USER_CACHE_PATH = _user_cache
except Exception:
    pass


class UvProvider(BinProvider):
    """Standalone ``uv`` package manager provider.
    Has two modes, picked based on whether ``install_root`` is set:
    1. **Hermetic venv mode** (``install_root=Path(...)``): creates a dedicated
       venv at the requested path via ``uv venv`` and installs packages
       into it via ``uv pip install --python <venv>/bin/python``, the same
       way ``PipProvider`` does when configured with ``install_root``. This is
       the idiomatic "install a Python library + its CLI entrypoints into
       an isolated environment" path.
    2. **Global tool mode** (``install_root=None``): delegates to
       ``uv tool install`` which lays out a fresh venv under
       ``UV_TOOL_DIR`` per tool and writes shims into ``UV_TOOL_BIN_DIR``.
       This is the idiomatic "install a CLI tool globally" path.
    Security:
    - ``--no-build`` for ``postinstall_scripts=False`` (wheels only).
    - ``--exclude-newer=<ISO8601>`` for ``min_release_age``.
    """

    name: BinProviderName = "uv"
    INSTALLER_BIN: BinName = "uv"
    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )
    # None = global ``uv tool`` mode, otherwise a managed venv path.
    # Default: ABX_PKG_UV_ROOT > ABX_PKG_LIB_DIR/uv > None.
    install_root: Path | None = Field(
        default_factory=lambda: abx_pkg_install_root_default("uv"),
        validation_alias="uv_venv",
    )
    # Global-mode overrides (only used when install_root is None). Mirror
    # ``UV_TOOL_DIR`` / ``UV_TOOL_BIN_DIR`` respectively; default to uv's
    # own defaults (``~/.local/share/uv/tools`` / ``~/.local/bin``).
    uv_tool_dir: Path | None = None
    bin_dir: Path | None = Field(default=None, validation_alias="uv_tool_bin_dir")
    cache_dir: Path = USER_CACHE_PATH
    cache_arg: str = ""  # re-derived per-instance from cache_dir in detect_cache_arg
    uv_install_args: list[str] = []

    @model_validator(mode="after")
    def detect_cache_arg(self) -> Self:
        if not self.cache_arg:
            self.cache_arg = f"--cache-dir={self.cache_dir}"
        return self

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        env: dict[str, str] = {"UV_ACTIVE": "1"}
        if not self.install_root:
            return env
        env["VIRTUAL_ENV"] = str(self.install_root)
        for sp in sorted(
            (self.install_root / "lib").glob("python*/site-packages"),
        ):
            env["PYTHONPATH"] = ":" + str(sp)
            break
        return env

    def supports_min_release_age(self, action) -> bool:
        return action in ("install", "update")

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.install_root:
            venv_python = self.install_root / "bin" / "python"
            if not (venv_python.is_file() and os.access(venv_python, os.X_OK)):
                return False
        return bool(
            bin_abspath(self.INSTALLER_BIN, PATH=self.PATH)
            or bin_abspath(self.INSTALLER_BIN),
        )

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "bin"
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(self.install_root, self.uv_tool_dir, self.bin_dir),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_uv_venv(self) -> Self:
        if self.install_root:
            bin_dir = self.bin_dir
            assert bin_dir is not None
            self.PATH = self._merge_PATH(
                bin_dir,
                PATH=self.PATH,
                prepend=True,
            )
        elif self.bin_dir:
            self.PATH = self._merge_PATH(
                self.bin_dir,
                PATH=self.PATH,
                prepend=True,
            )
        else:
            default_bin = Path(
                os.environ.get("UV_TOOL_BIN_DIR")
                or (Path("~").expanduser() / ".local" / "bin"),
            )
            self.PATH = self._merge_PATH(default_bin, PATH=self.PATH, prepend=True)
        return self

    def exec(
        self,
        bin_name,
        cmd=(),
        cwd: Path | str = ".",
        quiet=False,
        should_log_command: bool = True,
        **kwargs,
    ):
        # In global ``uv tool`` mode, inject UV_TOOL_DIR / UV_TOOL_BIN_DIR
        # when the user gave us custom values, so ``uv tool install`` lays
        # everything out under our managed dirs.
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        env.setdefault("UV_CACHE_DIR", str(self.cache_dir))
        if self.install_root is None:
            if self.uv_tool_dir:
                env["UV_TOOL_DIR"] = str(self.uv_tool_dir)
            if self.bin_dir:
                env["UV_TOOL_BIN_DIR"] = str(self.bin_dir)
                path_entries = [e for e in env.get("PATH", "").split(":") if e]
                bin_str = str(self.bin_dir)
                if bin_str not in path_entries:
                    env["PATH"] = ":".join([bin_str, *path_entries])
        return super().exec(
            bin_name=bin_name,
            cmd=cmd,
            cwd=cwd,
            quiet=quiet,
            should_log_command=should_log_command,
            env=env,
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
        if not self._ensure_writable_cache_dir(self.cache_dir):
            self.cache_arg = "--no-cache"
        if self.install_root:
            self._ensure_venv(no_cache=no_cache)
        else:
            if self.uv_tool_dir:
                self.uv_tool_dir.mkdir(parents=True, exist_ok=True)
            if self.bin_dir:
                self.bin_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_venv(self, *, no_cache: bool = False) -> None:
        assert self.install_root is not None
        venv_python = self.install_root / "bin" / "python"
        if venv_python.is_file() and os.access(venv_python, os.X_OK):
            return
        self.install_root.parent.mkdir(parents=True, exist_ok=True)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        proc = self.exec(
            bin_name=installer_bin,
            cmd=[
                "venv",
                "--no-cache" if no_cache else self.cache_arg,
                str(self.install_root),
            ],
            quiet=True,
            timeout=self.install_timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", ["uv venv"], proc)

    @staticmethod
    def _release_age_cutoff(min_release_age: float | None) -> str | None:
        if min_release_age is None or min_release_age <= 0:
            return None
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) - timedelta(days=min_release_age)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )

    def _pip_flags(
        self,
        *,
        install_args: InstallArgs,
        postinstall_scripts: bool,
        min_release_age: float | None,
    ) -> list[str]:
        """Build the shared ``uv pip`` security flags list."""
        combined = (*self.uv_install_args, *install_args)
        flags: list[str] = []
        if not postinstall_scripts and not any(
            arg == "--no-build" or arg.startswith("--no-build=") for arg in combined
        ):
            flags.append("--no-build")
        cutoff = self._release_age_cutoff(min_release_age)
        if cutoff and not any(
            arg == "--exclude-newer" or arg.startswith("--exclude-newer=")
            for arg in combined
        ):
            flags.append(f"--exclude-newer={cutoff}")
        return flags

    @staticmethod
    def _package_name_from_install_arg(install_arg: str) -> str | None:
        if not install_arg or install_arg.startswith("-"):
            return None
        if "://" in install_arg:
            return None
        if install_arg.startswith((".", "/", "~")):
            return None
        package_name = re.split(r"[<>=!~;]", install_arg, maxsplit=1)[0]
        package_name = package_name.split("[", 1)[0].strip()
        return package_name or None

    def _package_name_for_bin(self, bin_name: BinName, **context) -> str:
        install_args = self.get_install_args(str(bin_name), **context) or [
            str(bin_name),
        ]
        for install_arg in install_args:
            package_name = self._package_name_from_install_arg(install_arg)
            if package_name:
                return package_name
        return str(bin_name)

    def _version_from_uv_metadata(
        self,
        package_name: str,
        timeout: int | None = None,
    ) -> SemVer | None:
        try:
            uv_abspath = self.INSTALLER_BINARY().loaded_abspath
            assert uv_abspath
        except Exception:
            return None
        if self.install_root:
            proc = self.exec(
                bin_name=uv_abspath,
                cmd=[
                    "pip",
                    "show",
                    "--python",
                    str(self.install_root / "bin" / "python"),
                    package_name,
                ],
                timeout=timeout,
                quiet=True,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if line.startswith("Version: "):
                        return SemVer.parse(line.split("Version: ", 1)[1])
            return None
        proc = self.exec(
            bin_name=uv_abspath,
            cmd=["tool", "list"],
            timeout=timeout,
            quiet=True,
        )
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("-"):
                continue
            parts = line.split(" v", 1)
            if len(parts) == 2 and parts[0] == package_name:
                return SemVer.parse(parts[1])
        return None

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
                f"{arg}>={min_version}"
                if arg
                and not arg.startswith("-")
                and not any(c in arg for c in ">=<!=~")
                else arg
                for arg in install_args
            ]
        flags = self._pip_flags(
            install_args=install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        cache_arg = "--no-cache" if no_cache else self.cache_arg
        if self.install_root:
            # ``--compile-bytecode`` tells uv to compile ``.pyc`` files at
            # install time, overwriting any stale bytecode that Python may
            # have previously auto-generated for an older version of the
            # same package (wheel-provided source mtimes can collide with
            # existing ``.pyc`` headers and defeat Python's mtime-based
            # invalidation). See ``default_update_handler`` for context.
            cmd = [
                "pip",
                "install",
                "--python",
                str(self.install_root / "bin" / "python"),
                "--compile-bytecode",
                cache_arg,
                *flags,
                *self.uv_install_args,
                *install_args,
            ]
        else:
            cmd = [
                "tool",
                "install",
                "--force",
                cache_arg,
                *flags,
                *self.uv_install_args,
                *install_args,
            ]
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
        self.setup(no_cache=no_cache)
        installer_bin = self.INSTALLER_BINARY(no_cache=no_cache).loaded_abspath
        assert installer_bin
        postinstall_scripts = bool(postinstall_scripts)
        install_args = install_args or self.get_install_args(bin_name)
        if min_version:
            install_args = [
                f"{arg}>={min_version}"
                if arg
                and not arg.startswith("-")
                and not any(c in arg for c in ">=<!=~")
                else arg
                for arg in install_args
            ]
        flags = self._pip_flags(
            install_args=install_args,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        cache_arg = "--no-cache" if no_cache else self.cache_arg
        if self.install_root:
            # Do an explicit uninstall + install cycle instead of
            # ``uv pip install --upgrade --reinstall`` so the venv's
            # site-packages is fully repopulated from scratch (uv's
            # in-place upgrade path can leave stale files otherwise).
            # ``--compile-bytecode`` forces uv to write fresh ``.pyc``
            # files at install time, which overwrites any stale bytecode
            # Python auto-generated earlier (wheel-provided source mtimes
            # can collide with existing ``.pyc`` headers and defeat
            # Python's mtime-based invalidation).
            tool_names = [
                arg.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0]
                for arg in install_args
                if arg and not arg.startswith("-")
            ] or [bin_name]
            uninstall_proc = self.exec(
                bin_name=installer_bin,
                cmd=[
                    "pip",
                    "uninstall",
                    "--python",
                    str(self.install_root / "bin" / "python"),
                    *tool_names,
                ],
                timeout=timeout,
            )
            # Treat "no packages to uninstall" as a no-op success.
            if uninstall_proc.returncode != 0 and "No packages to uninstall" not in (
                uninstall_proc.stderr or ""
            ):
                self._raise_proc_error("update", tool_names, uninstall_proc)
            # Belt-and-suspenders: ``--compile-bytecode`` below makes uv
            # rewrite ``.pyc`` files at install time, but on older uv
            # releases (and on some wheel layouts where the source mtime
            # is preserved across versions) the rewrite can be skipped if
            # uv decides the ``.pyc`` is "already up to date" against the
            # newly-written source. Wipe every ``__pycache__`` under the
            # venv's site-packages between the uninstall and the install
            # so Python is forced to recompile from the freshly-written
            # source. Targeted, not the whole venv.
            for site_packages in (self.install_root / "lib").glob(
                "python*/site-packages",
            ):
                for pycache_dir in site_packages.rglob("__pycache__"):
                    shutil.rmtree(pycache_dir, ignore_errors=True)
            cmd = [
                "pip",
                "install",
                "--python",
                str(self.install_root / "bin" / "python"),
                "--compile-bytecode",
                cache_arg,
                *flags,
                *self.uv_install_args,
                *install_args,
            ]
        else:
            # ``uv tool install --force`` creates a fresh per-tool venv each
            # time, so there's no stale-compiled-artifact hazard.
            cmd = [
                "tool",
                "install",
                "--force",
                cache_arg,
                *flags,
                *self.uv_install_args,
                *install_args,
            ]
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
        installer_bin = self.INSTALLER_BINARY().loaded_abspath
        assert installer_bin
        install_args = install_args or self.get_install_args(bin_name)
        # Strip version pins / extras from package specs so both
        # ``uv pip uninstall`` and ``uv tool uninstall`` get bare names.
        tool_names = [
            arg.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0]
            for arg in install_args
            if arg and not arg.startswith("-")
        ] or [bin_name]
        if self.install_root:
            cmd = [
                "pip",
                "uninstall",
                "--python",
                str(self.install_root / "bin" / "python"),
                *tool_names,
            ]
        else:
            cmd = ["tool", "uninstall", *tool_names]
        proc = self.exec(bin_name=installer_bin, cmd=cmd, timeout=timeout)
        if proc.returncode != 0:
            self._raise_proc_error("uninstall", tool_names, proc)
        return True

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        try:
            abspath = super().default_abspath_handler(bin_name, **context)
            if abspath:
                return TypeAdapter(HostBinPath).validate_python(abspath)
        except Exception:
            pass
        try:
            installer_binary = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            return None
        # Fallback: ``uv pip show`` for venv mode.
        if self.install_root:
            tool_name = self._package_name_for_bin(str(bin_name), **context)
            assert installer_binary.loaded_abspath
            proc = self.exec(
                bin_name=installer_binary.loaded_abspath,
                cmd=[
                    "pip",
                    "show",
                    "--python",
                    str(self.install_root / "bin" / "python"),
                    tool_name,
                ],
                timeout=self.version_timeout,
                quiet=True,
            )
            if proc.returncode == 0:
                candidate = self.install_root / "bin" / str(bin_name)
                if candidate.exists():
                    return TypeAdapter(HostBinPath).validate_python(candidate)
        return None

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
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
        tool_name = self._package_name_for_bin(str(bin_name), **context)
        return self._version_from_uv_metadata(tool_name, timeout=timeout)


if __name__ == "__main__":
    # Usage:
    # ./binprovider_uv.py load black
    # ./binprovider_uv.py install black
    result = uv = UvProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(uv, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)
