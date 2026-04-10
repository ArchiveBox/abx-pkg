#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Self
from collections.abc import Iterable

from pydantic import Field, PrivateAttr, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
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


class PuppeteerProvider(BinProvider):
    name: BinProviderName = "puppeteer"
    INSTALLER_BIN: BinName = "puppeteer-browsers"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(
        default_factory=lambda: env_flag_is_true("ABX_PKG_POSTINSTALL_SCRIPTS"),
        repr=False,
    )
    min_release_age: float | None = Field(default=None, repr=False)

    # Default: ABX_PKG_PUPPETEER_ROOT > ABX_PKG_LIB_DIR/puppeteer > None.
    install_root: Path | None = Field(
        default_factory=lambda: abx_pkg_install_root_default("puppeteer"),
        validation_alias="puppeteer_root",
    )
    bin_dir: Path | None = None
    browser_cache_dir: Path | None = None
    _SUDO_BINARY: Binary | None = PrivateAttr(default=None)

    def supports_postinstall_disable(self, action) -> bool:
        return action in ("install", "update")

    @computed_field
    @property
    def cache_dir(self) -> Path | None:
        if self.browser_cache_dir is not None:
            return self.browser_cache_dir
        if self.install_root is not None:
            return self.install_root / "cache"
        return None

    @computed_field
    @property
    def is_valid(self) -> bool:
        return bool(self.INSTALLER_BIN_ABSPATH)

    @model_validator(mode="after")
    def detect_euid_to_use(self) -> Self:
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "bin"
        if self.euid is None:
            self.euid = self.detect_euid(
                owner_paths=(
                    self.install_root,
                    self.bin_dir,
                    self.cache_dir,
                    self.install_root / "npm"
                    if self.install_root is not None
                    else None,
                ),
                preserve_root=True,
            )
        return self

    @model_validator(mode="after")
    def load_PATH_from_root(self) -> Self:
        path_entries: list[Path] = []
        if self.bin_dir is not None:
            path_entries.append(self.bin_dir)
        if self.install_root is not None:
            path_entries.append(self.install_root / "npm" / "node_modules" / ".bin")
        if path_entries:
            self.PATH = self._merge_PATH(
                *path_entries,
                PATH=self.PATH,
                prepend=True,
            )
        return self

    def _cli_binary(
        self,
        *,
        postinstall_scripts: bool,
        min_release_age: float,
        no_cache: bool = False,
    ) -> Binary:
        cli_provider = NpmProvider(
            install_root=self.install_root / "npm"
            if self.install_root is not None
            else None,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        )
        return Binary(
            name="puppeteer-browsers",
            binproviders=[cli_provider],
            overrides={"npm": {"install_args": ["@puppeteer/browsers"]}},
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
        ).install(no_cache=no_cache)

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version=None,
        no_cache: bool = False,
    ) -> None:
        if (
            not no_cache
            and self._INSTALLER_BINARY is not None
            and self._INSTALLER_BINARY.loaded_abspath is not None
        ):
            self._INSTALLER_BIN_ABSPATH = self._INSTALLER_BINARY.loaded_abspath
            path_entries: list[Path] = []
            if self.bin_dir is not None:
                path_entries.append(self.bin_dir)
            if self.install_root is not None:
                path_entries.append(self.install_root / "npm" / "node_modules" / ".bin")
            if path_entries:
                self.PATH = self._merge_PATH(
                    *path_entries,
                    PATH="",
                    prepend=True,
                )
            return
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

        if self.install_root is not None:
            self.install_root.mkdir(parents=True, exist_ok=True)
        if self.bin_dir is not None:
            self.bin_dir.mkdir(parents=True, exist_ok=True)
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        cli_binary = self._cli_binary(
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            no_cache=no_cache,
        )
        self._INSTALLER_BIN_ABSPATH = cli_binary.abspath
        self._INSTALLER_BINARY = cli_binary
        path_entries: list[Path] = []
        if self.bin_dir is not None:
            path_entries.append(self.bin_dir)
        if self.install_root is not None:
            path_entries.append(self.install_root / "npm" / "node_modules" / ".bin")
        if path_entries:
            self.PATH = self._merge_PATH(
                *path_entries,
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
        if self.cache_dir is not None:
            normalized.append(f"--path={self.cache_dir}")
        return normalized

    def _list_installed_browsers(self) -> list[tuple[str, str, Path]]:
        installer_bin = self.INSTALLER_BIN_ABSPATH
        if not installer_bin:
            return []
        cmd = ["list"]
        if self.cache_dir is not None:
            cmd.append(f"--path={self.cache_dir}")
        proc = self.exec(
            bin_name=installer_bin,
            cmd=cmd,
            cwd=self.install_root or ".",
            quiet=True,
            timeout=self.version_timeout,
            env=(
                {**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)}
                if self.cache_dir is not None
                else None
            ),
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

    def _refresh_symlink(self, bin_name: str, target: Path) -> Path:
        bin_dir = self.bin_dir
        assert bin_dir is not None
        link_path = bin_dir / bin_name
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
        if str(bin_name) == self.INSTALLER_BIN:
            return self.INSTALLER_BIN_ABSPATH
        bin_dir = self.bin_dir
        assert bin_dir is not None
        link_path = bin_dir / str(bin_name)
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
        if self.cache_dir is None:
            return False
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
            return self._sudo_binary() is not None
        except Exception:
            return False

    def _sudo_binary(self, *, no_cache: bool = False) -> Binary | None:
        sudo_binary = None if no_cache else self._SUDO_BINARY
        if sudo_binary is None:
            sudo_binary = Binary(
                name="sudo",
                binproviders=[
                    EnvProvider(postinstall_scripts=True, min_release_age=0),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            ).load(no_cache=no_cache)
            if not no_cache and sudo_binary is not None:
                self._SUDO_BINARY = sudo_binary
        return sudo_binary

    def _run_install_with_sudo(
        self,
        install_args: list[str],
    ) -> subprocess.CompletedProcess[str] | None:
        installer_bin = self.INSTALLER_BIN_ABSPATH
        if not installer_bin:
            return None
        sudo_binary = self._sudo_binary()
        if sudo_binary is None or sudo_binary.loaded_abspath is None:
            return None

        proc = self.exec(
            bin_name=sudo_binary.loaded_abspath,
            cmd=["-E", str(installer_bin), "install", *install_args],
            cwd=self.install_root or ".",
            timeout=self.install_timeout,
            env=(
                {**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)}
                if self.cache_dir is not None
                else None
            ),
        )
        if (
            proc.returncode == 0
            and self.cache_dir is not None
            and self.cache_dir.exists()
        ):
            uid = os.getuid()
            gid = os.getgid()
            chown_proc = self.exec(
                bin_name=sudo_binary.loaded_abspath,
                cmd=["chown", "-R", f"{uid}:{gid}", str(self.cache_dir)],
                cwd=self.install_root or ".",
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
        no_cache: bool = False,
        **context,
    ) -> str:
        self.setup(no_cache=no_cache)
        install_args = list(install_args or self.get_install_args(bin_name))
        browser_name = self._browser_name(bin_name, install_args)
        normalized_install_args = self._normalize_install_args(install_args)

        if self.dry_run:
            return f"DRY_RUN would install {browser_name} via @puppeteer/browsers"

        installer_bin = self._require_installer_bin()
        proc = self.exec(
            bin_name=installer_bin,
            cmd=["install", *normalized_install_args],
            cwd=self.install_root or ".",
            timeout=timeout if timeout is not None else self.install_timeout,
            env=(
                {**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)}
                if self.cache_dir is not None
                else None
            ),
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
                cwd=self.install_root or ".",
                timeout=timeout if timeout is not None else self.install_timeout,
                env=(
                    {**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)}
                    if self.cache_dir is not None
                    else None
                ),
            )
            install_output = f"{proc.stdout}\n{proc.stderr}"

        if proc.returncode != 0 and self._cleanup_partial_browser_cache(
            install_output,
            browser_name,
        ):
            proc = self.exec(
                bin_name=self._require_installer_bin(),
                cmd=["install", *normalized_install_args],
                cwd=self.install_root or ".",
                timeout=timeout if timeout is not None else self.install_timeout,
                env=(
                    {**os.environ, "PUPPETEER_CACHE_DIR": str(self.cache_dir)}
                    if self.cache_dir is not None
                    else None
                ),
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

        if self.bin_dir is not None:
            self._refresh_symlink(bin_name, installed_path)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        no_cache: bool = False,
        **context,
    ) -> str:
        return self.default_install_handler(
            bin_name,
            install_args=install_args,
            timeout=timeout,
            no_cache=no_cache,
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
        if self.bin_dir is not None:
            (self.bin_dir / bin_name).unlink(missing_ok=True)
        if self.cache_dir is not None:
            browser_dir = self.cache_dir / browser_name
            if browser_dir.exists():
                shutil.rmtree(browser_dir, ignore_errors=True)
        return True
