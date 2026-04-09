#!/usr/bin/env python3

__package__ = "abx_pkg"

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


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "uv-cache"
try:
    _user_cache = user_cache_path("uv", "abx-pkg", ensure_exists=True)
    if os.access(_user_cache, os.W_OK):
        USER_CACHE_PATH = _user_cache
except Exception:
    pass


class UvProvider(BinProvider):
    """Standalone ``uv`` package manager provider.

    Has two modes, picked based on whether ``uv_venv`` is set:

    1. **Hermetic venv mode** (``uv_venv=Path(...)``): creates a dedicated
       venv at the requested path via ``uv venv`` and installs packages
       into it via ``uv pip install --python <venv>/bin/python``, the same
       way ``PipProvider`` does when configured with ``pip_venv``. This is
       the idiomatic "install a Python library + its CLI entrypoints into
       an isolated environment" path.

    2. **Global tool mode** (``uv_venv=None``): delegates to
       ``uv tool install`` which lays out a fresh venv under
       ``UV_TOOL_DIR`` per tool and writes shims into ``UV_TOOL_BIN_DIR``.
       This is the idiomatic "install a CLI tool globally" path.

    Security:
    - ``--no-build`` for ``postinstall_scripts=False`` (wheels only).
    - ``--exclude-newer=<ISO8601>`` for ``min_release_age``.
    """

    name: BinProviderName = "uv"
    INSTALLER_BIN: BinName = "uv"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "uv_venv"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    uv_venv: Path | None = None  # None = global ``uv tool`` mode
    # Global-mode overrides (only used when uv_venv is None). Mirror
    # ``UV_TOOL_DIR`` / ``UV_TOOL_BIN_DIR`` respectively; default to uv's
    # own defaults (``~/.local/share/uv/tools`` / ``~/.local/bin``).
    uv_tool_dir: Path | None = None
    uv_tool_bin_dir: Path | None = None

    cache_dir: Path = USER_CACHE_PATH
    cache_arg: str = ""  # re-derived per-instance from cache_dir in detect_cache_arg

    uv_install_args: list[str] = []

    @model_validator(mode="after")
    def detect_cache_arg(self) -> Self:
        if not self.cache_arg:
            self.cache_arg = f"--cache-dir={self.cache_dir}"
        return self

    def supports_min_release_age(self, action) -> bool:
        return action in ("install", "update")

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.uv_venv:
            venv_python = self.uv_venv / "bin" / "python"
            if not (venv_python.is_file() and os.access(venv_python, os.X_OK)):
                return False
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.uv_venv

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        if self.uv_venv:
            return self.uv_venv / "bin"
        return self.uv_tool_bin_dir

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Resolve the uv executable, honoring ``UV_BINARY`` for explicit overrides."""
        if self._INSTALLER_BIN_ABSPATH:
            return self._INSTALLER_BIN_ABSPATH

        manual_binary = os.environ.get("UV_BINARY")
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
                owner_paths=(self.uv_venv, self.uv_tool_dir, self.uv_tool_bin_dir),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_uv_venv(self) -> Self:
        if self.uv_venv:
            self.PATH = self._merge_PATH(
                self.uv_venv / "bin",
                PATH=self.PATH,
                prepend=True,
            )
        elif self.uv_tool_bin_dir:
            self.PATH = self._merge_PATH(
                self.uv_tool_bin_dir,
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

    def exec(self, bin_name, cmd=(), cwd: Path | str = ".", quiet=False, **kwargs):
        # In global ``uv tool`` mode, inject UV_TOOL_DIR / UV_TOOL_BIN_DIR
        # when the user gave us custom values, so ``uv tool install`` lays
        # everything out under our managed dirs.
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        env.setdefault("UV_CACHE_DIR", str(self.cache_dir))
        if self.uv_venv is None:
            if self.uv_tool_dir:
                env["UV_TOOL_DIR"] = str(self.uv_tool_dir)
            if self.uv_tool_bin_dir:
                env["UV_TOOL_BIN_DIR"] = str(self.uv_tool_bin_dir)
                path_entries = [e for e in env.get("PATH", "").split(":") if e]
                bin_str = str(self.uv_tool_bin_dir)
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
        if self.uv_venv:
            self._ensure_venv()
        else:
            if self.uv_tool_dir:
                self.uv_tool_dir.mkdir(parents=True, exist_ok=True)
            if self.uv_tool_bin_dir:
                self.uv_tool_bin_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_venv(self) -> None:
        assert self.uv_venv is not None
        venv_python = self.uv_venv / "bin" / "python"
        if venv_python.is_file() and os.access(venv_python, os.X_OK):
            return
        self.uv_venv.parent.mkdir(parents=True, exist_ok=True)
        proc = self.exec(
            bin_name=self._require_installer_bin(),
            cmd=["venv", self.cache_arg, str(self.uv_venv)],
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

        if self.uv_venv:
            cmd = [
                "pip",
                "install",
                "--python",
                str(self.uv_venv / "bin" / "python"),
                self.cache_arg,
                *flags,
                *self.uv_install_args,
                *install_args,
            ]
        else:
            cmd = [
                "tool",
                "install",
                "--force",
                self.cache_arg,
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
        timeout: int | None = None,
    ) -> str:
        self.setup()
        installer_bin = self._require_installer_bin()
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

        if self.uv_venv:
            # ``--reinstall`` (in addition to ``--upgrade``) forces uv to
            # fully replace the old package's on-disk files instead of
            # just overwriting matching filenames. Without it, stale
            # compiled ``.so`` / ``.pyc`` files from the previous version
            # can shadow the newly-installed ``.py`` files (manifested as
            # ``black --version`` still reporting the old version after a
            # successful upgrade).
            cmd = [
                "pip",
                "install",
                "--python",
                str(self.uv_venv / "bin" / "python"),
                "--upgrade",
                "--reinstall",
                self.cache_arg,
                *flags,
                *self.uv_install_args,
                *install_args,
            ]
        else:
            cmd = [
                "tool",
                "install",
                "--force",
                "--reinstall",
                self.cache_arg,
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
        installer_bin = self._require_installer_bin()
        install_args = install_args or self.get_install_args(bin_name)
        # Strip version pins / extras from package specs so both
        # ``uv pip uninstall`` and ``uv tool uninstall`` get bare names.
        tool_names = [
            arg.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0]
            for arg in install_args
            if arg and not arg.startswith("-")
        ] or [bin_name]

        if self.uv_venv:
            cmd = [
                "pip",
                "uninstall",
                "--python",
                str(self.uv_venv / "bin" / "python"),
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

        # Fallback: ``uv pip show`` for venv mode.
        if self.uv_venv:
            install_args = self.get_install_args(str(bin_name)) or [str(bin_name)]
            tool_name = (
                install_args[0]
                .split("[", 1)[0]
                .split("=", 1)[0]
                .split(">", 1)[0]
                .split("<", 1)[0]
            )
            proc = self.exec(
                bin_name=self.INSTALLER_BIN_ABSPATH,
                cmd=[
                    "pip",
                    "show",
                    "--python",
                    str(self.uv_venv / "bin" / "python"),
                    tool_name,
                ],
                timeout=self.version_timeout,
                quiet=True,
            )
            if proc.returncode == 0:
                candidate = self.uv_venv / "bin" / str(bin_name)
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

        install_args = self.get_install_args(str(bin_name), **context) or [
            str(bin_name),
        ]
        main_package = install_args[0]
        tool_name = (
            main_package.split("[", 1)[0]
            .split("=", 1)[0]
            .split(">", 1)[0]
            .split("<", 1)[0]
        )

        if self.uv_venv:
            # Fallback: ``uv pip show`` for venv mode.
            proc = self.exec(
                bin_name=self.INSTALLER_BIN_ABSPATH,
                cmd=[
                    "pip",
                    "show",
                    "--python",
                    str(self.uv_venv / "bin" / "python"),
                    tool_name,
                ],
                timeout=timeout,
                quiet=True,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if line.startswith("Version: "):
                        return SemVer.parse(line.split("Version: ", 1)[1])
            return None

        # Global mode: fallback to ``uv tool list``.
        proc = self.exec(
            bin_name=self.INSTALLER_BIN_ABSPATH,
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
            if len(parts) == 2 and parts[0] == tool_name:
                return SemVer.parse(parts[1])
        return None


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
