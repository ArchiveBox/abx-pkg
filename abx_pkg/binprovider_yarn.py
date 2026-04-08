#!/usr/bin/env python3

__package__ = "abx_pkg"

import json
import os
import re
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


USER_CACHE_PATH = Path(tempfile.gettempdir()) / "yarn-cache"
try:
    yarn_user_cache_path = user_cache_path(
        appname="yarn",
        appauthor="abx-pkg",
        ensure_exists=True,
    )
    if os.access(yarn_user_cache_path, os.W_OK):
        USER_CACHE_PATH = yarn_user_cache_path
except Exception:
    pass


_DEFAULT_YARN_ROOT = (
    Path(os.environ.get("ABX_PKG_YARN_ROOT") or "~/.cache/abx-pkg/yarn")
    .expanduser()
    .absolute()
)


class YarnProvider(BinProvider):
    """Yarn package manager provider (Yarn 4+ / Berry).

    Yarn 4 is workspace-based: every install happens inside a project
    directory containing a ``package.json`` and ``.yarnrc.yml``.  This
    provider auto-initializes a managed workspace under ``yarn_prefix``
    on first use, configures ``nodeLinker: node-modules`` so binaries
    end up in ``<yarn_prefix>/node_modules/.bin``, and writes the
    ``npmMinimalAgeGate`` security setting from ``min_release_age``.

    Yarn classic (1.x) does not support ``npmMinimalAgeGate``; on those
    hosts ``supports_min_release_age`` returns ``False`` and the runtime
    falls back to a plain install while logging a warning.
    """

    name: BinProviderName = "yarn"
    INSTALLER_BIN: BinName = "yarn"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "yarn_prefix"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(
        default_factory=lambda: float(os.environ.get("ABX_PKG_MIN_RELEASE_AGE", "7")),
        repr=False,
    )

    yarn_prefix: Path | None = None  # workspace dir; defaults to managed cache dir

    cache_dir: Path = USER_CACHE_PATH

    yarn_install_args: list[str] = []

    _CACHED_YARN_VERSION: SemVer | None = None

    def supports_min_release_age(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        # npmMinimalAgeGate landed in Yarn 4.10
        threshold = SemVer.parse("4.10.0")
        version = self._yarn_version()
        if version is None or threshold is None:
            return False
        return version >= threshold

    def supports_postinstall_disable(self, action) -> bool:
        if action not in ("install", "update"):
            return False
        # Yarn 2+ supports the enableScripts setting and --mode skip-build
        threshold = SemVer.parse("2.0.0")
        version = self._yarn_version()
        if version is None or threshold is None:
            return False
        return version >= threshold

    @computed_field
    @property
    def is_valid(self) -> bool:
        if self.yarn_prefix:
            yarn_bin_dir = self.yarn_prefix / "node_modules" / ".bin"
            if not (os.path.isdir(yarn_bin_dir) and os.access(yarn_bin_dir, os.R_OK)):
                return False
        return bool(self.INSTALLER_BIN_ABSPATH)

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.yarn_prefix

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        return (
            self.install_root / "node_modules" / ".bin" if self.install_root else None
        )

    @computed_field
    @property
    def INSTALLER_BIN_ABSPATH(self) -> HostBinPath | None:
        """Resolve the yarn executable, honoring ``YARN_BINARY`` for explicit overrides."""
        if self._INSTALLER_BIN_ABSPATH:
            return self._INSTALLER_BIN_ABSPATH

        manual_binary = os.environ.get("YARN_BINARY")
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
                owner_paths=(self.yarn_prefix,),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_yarn_prefix(self) -> Self:
        prefix = self.yarn_prefix or _DEFAULT_YARN_ROOT
        bin_dir = prefix / "node_modules" / ".bin"
        self.PATH = self._merge_PATH(bin_dir)
        return self

    def _yarn_version(self) -> SemVer | None:
        if self._CACHED_YARN_VERSION is not None:
            return self._CACHED_YARN_VERSION
        if not self.INSTALLER_BIN_ABSPATH:
            return None
        try:
            proc = self.exec(
                bin_name=self.INSTALLER_BIN_ABSPATH,
                cmd=["--version"],
                quiet=True,
                timeout=self.version_timeout,
            )
            output = (proc.stdout or proc.stderr).strip()
            match = re.search(r"\d+\.\d+\.\d+", output)
            if not match:
                return None
            self._CACHED_YARN_VERSION = SemVer.parse(match.group(0))
        except Exception:
            return None
        return self._CACHED_YARN_VERSION

    def _ensure_workspace_initialized(self) -> Path:
        prefix = self.yarn_prefix or _DEFAULT_YARN_ROOT
        prefix.mkdir(parents=True, exist_ok=True)
        package_json = prefix / "package.json"
        if not package_json.exists():
            # Note: do NOT write a ``packageManager`` field here. Yarn 1.22
            # treats it as an opt-in to corepack and refuses to install if
            # the running yarn version doesn't match.
            package_json.write_text(
                json.dumps(
                    {
                        "name": "abx-pkg-yarn-workspace",
                        "version": "0.0.0",
                        "private": True,
                    },
                    indent=2,
                )
                + "\n",
            )

        version = self._yarn_version()
        berry_threshold = SemVer.parse("2.0.0")
        if (
            version is not None
            and berry_threshold is not None
            and version >= berry_threshold
        ):
            yarnrc = prefix / ".yarnrc.yml"
            if not yarnrc.exists():
                yarnrc.write_text("nodeLinker: node-modules\n")
            else:
                content = yarnrc.read_text()
                if "nodeLinker:" not in content:
                    yarnrc.write_text(
                        content.rstrip("\n") + "\nnodeLinker: node-modules\n",
                    )
        return prefix

    def _write_yarnrc_security(
        self,
        *,
        min_release_age: float | None,
        postinstall_scripts: bool,
    ) -> None:
        version = self._yarn_version()
        berry_threshold = SemVer.parse("2.0.0")
        if version is None or berry_threshold is None or version < berry_threshold:
            return  # yarn classic uses .yarnrc, not .yarnrc.yml — skip

        prefix = self._ensure_workspace_initialized()
        yarnrc = prefix / ".yarnrc.yml"
        try:
            existing = yarnrc.read_text()
        except FileNotFoundError:
            existing = ""

        kept_lines: list[str] = [
            line
            for line in existing.splitlines()
            if not line.strip().startswith(("npmMinimalAgeGate:", "enableScripts:"))
        ]

        age_gate_threshold = SemVer.parse("4.10.0")
        if (
            min_release_age is not None
            and min_release_age > 0
            and age_gate_threshold is not None
            and version >= age_gate_threshold
        ):
            # Yarn 4 npmMinimalAgeGate accepts duration strings like "7d".
            if min_release_age >= 1 and float(min_release_age).is_integer():
                duration = f"{int(min_release_age)}d"
            else:
                duration = f"{max(int(min_release_age * 24 * 60), 1)}m"
            kept_lines.append(f"npmMinimalAgeGate: {duration}")
        if postinstall_scripts is False:
            kept_lines.append("enableScripts: false")

        new_content = "\n".join(kept_lines)
        if new_content and not new_content.endswith("\n"):
            new_content += "\n"
        yarnrc.write_text(new_content)

    def exec(
        self,
        bin_name,
        cmd=(),
        cwd: Path | str = ".",
        quiet=False,
        **kwargs,
    ):
        """Inject yarn workspace env vars and default cwd to the workspace dir."""
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        env.setdefault("YARN_ENABLE_TELEMETRY", "0")
        env.setdefault("YARN_ENABLE_GLOBAL_CACHE", "1")
        env.setdefault("YARN_GLOBAL_FOLDER", str(self.cache_dir))
        env.setdefault("YARN_CACHE_FOLDER", str(self.cache_dir / "v6"))
        if cwd == ".":
            workspace = self.yarn_prefix or _DEFAULT_YARN_ROOT
            workspace.mkdir(parents=True, exist_ok=True)
            cwd = workspace
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
        """Initialize the yarn workspace if needed."""
        self._ensure_writable_cache_dir(self.cache_dir)
        self._ensure_workspace_initialized()

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
        # Preserve ``None`` so the base class's "unsupported on this host"
        # fallback is honored end-to-end (no npmMinimalAgeGate on older Yarn).
        self._write_yarnrc_security(
            min_release_age=min_release_age,
            postinstall_scripts=postinstall_scripts,
        )

        version = self._yarn_version()
        berry_threshold = SemVer.parse("2.0.0")
        cli_args: list[str] = list(self.yarn_install_args)
        if (
            version is not None
            and berry_threshold is not None
            and version >= berry_threshold
            and not postinstall_scripts
        ):
            cli_args.extend(["--mode", "skip-build"])

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
        # Preserve ``None`` so the base class's "unsupported on this host"
        # fallback is honored end-to-end (no npmMinimalAgeGate on older Yarn).
        self._write_yarnrc_security(
            min_release_age=min_release_age,
            postinstall_scripts=postinstall_scripts,
        )

        installer = self._require_installer_bin()
        version = self._yarn_version()
        berry_threshold = SemVer.parse("2.0.0")
        if (
            version is not None
            and berry_threshold is not None
            and version >= berry_threshold
        ):
            cli_args: list[str] = list(self.yarn_install_args)
            if not postinstall_scripts:
                cli_args.extend(["--mode", "skip-build"])
            proc = self.exec(
                bin_name=installer,
                cmd=["up", *cli_args, *install_args],
                timeout=timeout,
            )
        else:
            proc = self.exec(
                bin_name=installer,
                cmd=["upgrade", *self.yarn_install_args, *install_args],
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
            cmd=["remove", *self.yarn_install_args, *install_args],
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

        prefix = self.yarn_prefix or _DEFAULT_YARN_ROOT
        candidate = prefix / "node_modules" / ".bin" / str(bin_name)
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

        prefix = self.yarn_prefix or _DEFAULT_YARN_ROOT
        try:
            install_args = self.get_install_args(str(bin_name), **context) or [
                str(bin_name),
            ]
            main_package = install_args[0]
            if main_package[0] == "@":
                package = "@" + main_package[1:].split("@", 1)[0]
            else:
                package = main_package.split("@", 1)[0]
            package_json = prefix / "node_modules" / package / "package.json"
            if package_json.exists():
                return json.loads(package_json.read_text())["version"]
        except Exception:
            pass
        return None


if __name__ == "__main__":
    # Usage:
    # ./binprovider_yarn.py load zx
    # ./binprovider_yarn.py install zx
    # ./binprovider_yarn.py get_version zx
    # ./binprovider_yarn.py get_abspath zx
    result = yarn = YarnProvider()
    func = None

    if len(sys.argv) > 1:
        result = func = getattr(yarn, sys.argv[1])

    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])

    print(result)
