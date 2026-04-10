#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar, Self
from collections.abc import Iterable

from pydantic import Field, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    DEFAULT_LIB_DIR,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abx_pkg_install_root_default,
)
from .binary import Binary
from .binprovider import BinProvider, EnvProvider, env_flag_is_true, remap_kwargs
from .binprovider_npm import NpmProvider
from .logging import format_subprocess_output, get_logger, log_subprocess_output
from .semver import SemVer

logger = get_logger(__name__)

CLAUDE_SANDBOX_NO_PROXY = (
    "localhost,127.0.0.1,169.254.169.254,metadata.google.internal,"
    ".svc.cluster.local,.local"
)

# Puppeteer's provider bootstraps @puppeteer/browsers via a local npm
# prefix and creates symlinks in bin_dir — it genuinely needs a managed
# directory even when no explicit root is set.
DEFAULT_PUPPETEER_ROOT = DEFAULT_LIB_DIR / "puppeteer"


class PuppeteerProvider(BinProvider):
    name: BinProviderName = "puppeteer"
    INSTALLER_BIN: BinName = "puppeteer-browsers"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "puppeteer_root"
    BIN_DIR_FIELD: ClassVar[str | None] = "browser_bin_dir"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(default=None, repr=False)

    # Default: ABX_PKG_PUPPETEER_ROOT > ABX_PKG_LIB_DIR/puppeteer > None.
    puppeteer_root: Path | None = abx_pkg_install_root_default("puppeteer")
    browser_bin_dir: Path | None = None
    browser_cache_dir: Path | None = None

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @computed_field
    @property
    def install_root(self) -> Path | None:
        if self.puppeteer_root:
            return self.puppeteer_root
        if self.browser_bin_dir:
            return self.browser_bin_dir.parent
        if self.browser_cache_dir:
            return self.browser_cache_dir.parent
        return None

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        if self.browser_bin_dir:
            return self.browser_bin_dir
        return self.install_root / "bin" if self.install_root else None

    @computed_field
    @property
    def cache_dir(self) -> Path | None:
        if self.browser_cache_dir:
            return self.browser_cache_dir
        return self.install_root / "cache" if self.install_root else None

    @computed_field
    @property
    def npm_prefix(self) -> Path:
        return self.install_root / "npm"

    @computed_field
    @property
    def is_valid(self) -> bool:
        return bool(self.INSTALLER_BIN_ABSPATH)

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(
                    self.install_root,
                    self.bin_dir,
                    self.cache_dir,
                    self.npm_prefix,
                ),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_root(self) -> Self:
        self.PATH = self._merge_PATH(
            self.bin_dir,
            self.npm_prefix / "node_modules" / ".bin",
            PATH=self.PATH,
            prepend=True,
        )
        return self

    def _cli_binary(
        self,
        *,
        postinstall_scripts: bool,
        min_release_age: float,
    ) -> Binary:
        cli_provider = NpmProvider(
            npm_prefix=self.npm_prefix,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        return Binary(
            name="puppeteer-browsers",
            binproviders=[cli_provider],
            overrides={"npm": {"install_args": ["@puppeteer/browsers"]}},
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        ).load_or_install()

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version=None,
    ) -> None:
        postinstall_scripts = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        postinstall_scripts = (
            False if postinstall_scripts is None else postinstall_scripts
        )
        min_release_age = 0 if min_release_age is None else min_release_age

        self.install_root.mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        cli_binary = self._cli_binary(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        self._INSTALLER_BIN_ABSPATH = cli_binary.abspath
        self._INSTALLER_BINARY = cli_binary
        self.PATH = self._merge_PATH(
            self.bin_dir,
            self.npm_prefix / "node_modules" / ".bin",
            PATH="",
            prepend=True,
        )

    def _browser_name(
        self,
        bin_name: str,
        install_args: Iterable[str],
    ) -> str:
        for arg in install_args:
            arg_str = str(arg)
            if arg_str.startswith("-"):
                continue
            return arg_str.split("@", 1)[0]
        return bin_name

    def _normalize_install_args(self, install_args: Iterable[str]) -> list[str]:
        normalized: list[str] = []
        skip_next = False
        for arg in install_args:
            arg_str = str(arg)
            if skip_next:
                skip_next = False
                continue
            if arg_str == "--path":
                skip_next = True
                continue
            if arg_str.startswith("--path="):
                continue
            normalized.append(arg_str)
        normalized.append(f"--path={self.cache_dir}")
        return normalized

    def _list_installed_browsers(self) -> list[tuple[str, str, Path]]:
        installer_bin = self.INSTALLER_BIN_ABSPATH
        if not installer_bin:
            return []
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["list", f"--path={self.cache_dir}"],
            cwd=self.install_root,
            quiet=True,
            timeout=self.version_timeout,
            env={**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)},
        )
        if proc.returncode != 0:
            return []

        matches: list[tuple[str, str, Path]] = []
        pattern = re.compile(
            r"^(?P<browser>[^@\s]+)@(?P<version>\S+)(?:\s+\([^)]+\))?\s+(?P<path>.+)$",
        )
        for line in proc.stdout.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            matches.append(
                (
                    match.group("browser"),
                    match.group("version"),
                    Path(match.group("path")),
                ),
            )
        return matches

    def _parse_installed_browser_path(
        self,
        output: str,
        browser_name: str,
    ) -> Path | None:
        pattern = re.compile(
            r"^(?P<browser>[^@\s]+)@(?P<version>\S+)(?:\s+\([^)]+\))?\s+(?P<path>.+)$",
            re.MULTILINE,
        )
        matches = [
            (
                match.group("version"),
                Path(match.group("path")),
            )
            for match in pattern.finditer(output or "")
            if match.group("browser") == browser_name
        ]
        parsed_matches = [
            (parsed_version, path)
            for version, path in matches
            if (parsed_version := SemVer.parse(version)) is not None
        ]
        if parsed_matches:
            return max(parsed_matches, key=lambda item: item[0])[1]
        if len(matches) == 1:
            return matches[0][1]
        return None

    def _resolve_installed_browser_path(
        self,
        bin_name: str,
        install_args: Iterable[str] | None = None,
    ) -> Path | None:
        browser_name = self._browser_name(bin_name, install_args or [bin_name])
        candidates = [
            (version, path)
            for candidate_browser, version, path in self._list_installed_browsers()
            if candidate_browser == browser_name
        ]
        parsed_candidates = [
            (parsed_version, path)
            for version, path in candidates
            if (parsed_version := SemVer.parse(version)) is not None
        ]
        if parsed_candidates:
            return max(parsed_candidates, key=lambda item: item[0])[1]
        if len(candidates) == 1:
            return candidates[0][1]
        return None

    def _symlink_path(self, bin_name: str) -> Path:
        return self.bin_dir / bin_name

    def _refresh_symlink(self, bin_name: str, target: Path) -> Path:
        link_path = self._symlink_path(bin_name)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink(missing_ok=True)
        if os.name == "posix" and ".app/Contents/MacOS/" in str(target):
            link_path.write_text(
                f'#!/bin/sh\nexec {shlex.quote(str(target))} "$@"\n',
                encoding="utf-8",
            )
            link_path.chmod(0o755)
            return link_path
        link_path.symlink_to(target)
        return link_path

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        **context,
    ) -> HostBinPath | None:
        link_path = self._symlink_path(str(bin_name))
        if link_path.exists() and os.access(link_path, os.X_OK):
            return link_path

        resolved = self._resolve_installed_browser_path(str(bin_name))
        if not resolved or not resolved.exists():
            return None
        try:
            return self._refresh_symlink(str(bin_name), resolved)
        except OSError:
            return resolved

    def _cleanup_partial_browser_cache(
        self,
        install_output: str,
        browser_name: str,
    ) -> bool:
        targets: set[Path] = set()
        browser_cache_dir = self.cache_dir / browser_name

        missing_dir_match = re.search(
            r"browser folder \(([^)]+)\) exists but the executable",
            install_output,
        )
        if missing_dir_match:
            targets.add(Path(missing_dir_match.group(1)))

        missing_zip_match = re.search(r"open '([^']+\.zip)'", install_output)
        if missing_zip_match:
            targets.add(Path(missing_zip_match.group(1)))

        build_id_match = re.search(
            rf"All providers failed for {re.escape(browser_name)} (\S+)",
            install_output,
        )
        if build_id_match and browser_cache_dir.exists():
            build_id = build_id_match.group(1)
            targets.update(browser_cache_dir.glob(f"*{build_id}*"))

        removed_any = False
        resolved_cache = self.cache_dir.resolve(strict=False)
        for target in targets:
            resolved_target = target.resolve(strict=False)
            if not (
                resolved_target == resolved_cache
                or resolved_cache in resolved_target.parents
            ):
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                removed_any = True
            elif target.exists():
                target.unlink(missing_ok=True)
                removed_any = True
        return removed_any

    def _should_repair_cli_install(self, output: str) -> bool:
        lowered = (output or "").lower()
        return (
            "this.shim.parser.camelcase is not a function" in lowered
            or "yargs/build/lib/command.js" in lowered
        )

    def _get_install_failure_hint(self, install_output: str) -> str | None:
        lowered = (install_output or "").lower()
        if (
            "storage.googleapis.com" in lowered
            and "getaddrinfo" in lowered
            and "eai_again" in lowered
        ):
            return (
                "Puppeteer failed to download a browser from storage.googleapis.com. "
                "Override NO_PROXY/no_proxy to remove .googleapis.com and .google.com. "
                f'Example NO_PROXY="{CLAUDE_SANDBOX_NO_PROXY}"'
            )
        return None

    def _has_sudo(self) -> bool:
        try:
            return (
                Binary(
                    name="sudo",
                    binproviders=[
                        EnvProvider(postinstall_scripts=True, min_release_age=0),
                    ],
                    postinstall_scripts=True,
                    min_release_age=0,
                ).load(nocache=True)
                is not None
            )
        except Exception:
            return False

    def _run_install_with_sudo(
        self,
        install_args: list[str],
    ) -> subprocess.CompletedProcess[str] | None:
        installer_bin = self.INSTALLER_BIN_ABSPATH
        if not installer_bin:
            return None
        sudo_binary = Binary(
            name="sudo",
            binproviders=[
                EnvProvider(postinstall_scripts=True, min_release_age=0),
            ],
            postinstall_scripts=True,
            min_release_age=0,
        ).load(nocache=True)
        if sudo_binary is None or sudo_binary.loaded_abspath is None:
            return None

        proc = self.exec(
            bin_name=sudo_binary.loaded_abspath,
            cmd=["-E", str(installer_bin), "install", *install_args],
            cwd=self.install_root,
            timeout=self.install_timeout,
            env={**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)},
        )
        if proc.returncode == 0 and self.cache_dir.exists():
            uid = os.getuid()
            gid = os.getgid()
            chown_proc = self.exec(
                bin_name=sudo_binary.loaded_abspath,
                cmd=["chown", "-R", f"{uid}:{gid}", str(self.cache_dir)],
                cwd=self.install_root,
                timeout=30,
                quiet=True,
            )
            if chown_proc.returncode != 0:
                log_subprocess_output(
                    logger,
                    f"{self.__class__.__name__} sudo chown",
                    chown_proc.stdout,
                    chown_proc.stderr,
                )
        return proc

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        **context,
    ) -> str:
        install_args = list(install_args or self.get_install_args(bin_name))
        browser_name = self._browser_name(bin_name, install_args)
        normalized_install_args = self._normalize_install_args(install_args)

        if self.dry_run:
            return f"DRY_RUN would install {browser_name} via @puppeteer/browsers"

        installer_bin = self._require_installer_bin()
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["install", *normalized_install_args],
            cwd=self.install_root,
            timeout=timeout if timeout is not None else self.install_timeout,
            env={**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)},
        )

        install_output = f"{proc.stdout}\n{proc.stderr}"
        if (
            proc.returncode != 0
            and "--install-deps" in normalized_install_args
            and "requires root privileges" in install_output
            and os.geteuid() != 0
            and self._has_sudo()
        ):
            sudo_proc = self._run_install_with_sudo(normalized_install_args)
            if sudo_proc is not None:
                proc = sudo_proc
                install_output = f"{proc.stdout}\n{proc.stderr}"

        if proc.returncode != 0 and self._should_repair_cli_install(install_output):
            cli_binary = self._cli_binary(postinstall_scripts=True, min_release_age=0)
            self._INSTALLER_BIN_ABSPATH = cli_binary.abspath
            self._INSTALLER_BINARY = cli_binary
            proc = self.exec(
                bin_name=self._require_installer_bin(),
                cmd=["install", *normalized_install_args],
                cwd=self.install_root,
                timeout=timeout if timeout is not None else self.install_timeout,
                env={**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)},
            )
            install_output = f"{proc.stdout}\n{proc.stderr}"

        if proc.returncode != 0 and self._cleanup_partial_browser_cache(
            install_output,
            browser_name,
        ):
            proc = self.exec(
                bin_name=self._require_installer_bin(),
                cmd=["install", *normalized_install_args],
                cwd=self.install_root,
                timeout=timeout if timeout is not None else self.install_timeout,
                env={**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)},
            )
            install_output = f"{proc.stdout}\n{proc.stderr}"

        if proc.returncode != 0:
            install_hint = self._get_install_failure_hint(install_output)
            if install_hint:
                raise RuntimeError(install_hint) from None
            self._raise_proc_error("install", bin_name, proc)

        installed_path = self._parse_installed_browser_path(
            install_output,
            browser_name,
        )
        installed_path = installed_path or self._resolve_installed_browser_path(
            bin_name,
            install_args,
        )
        if not installed_path or not installed_path.exists():
            raise FileNotFoundError(
                f"{self.__class__.__name__} could not resolve installed browser path for {bin_name}",
            )

        self._refresh_symlink(bin_name, installed_path)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        **context,
    ) -> str:
        return self.default_install_handler(
            bin_name,
            install_args=install_args,
            timeout=timeout,
            **context,
        )

    @remap_kwargs({"packages": "install_args"})
    def default_uninstall_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        **context,
    ) -> bool:
        install_args = list(install_args or self.get_install_args(bin_name))
        browser_name = self._browser_name(bin_name, install_args)
        self._symlink_path(bin_name).unlink(missing_ok=True)
        browser_dir = self.cache_dir / browser_name
        if browser_dir.exists():
            shutil.rmtree(browser_dir, ignore_errors=True)
        return True
