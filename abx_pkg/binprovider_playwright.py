#!/usr/bin/env python3

__package__ = "abx_pkg"

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import ClassVar, Self
from collections.abc import Iterable

from pydantic import Field, computed_field, model_validator

from .base_types import BinName, BinProviderName, HostBinPath, InstallArgs, PATHStr
from .binary import Binary
from .binprovider import BinProvider, EnvProvider, remap_kwargs
from .binprovider_npm import NpmProvider
from .logging import format_subprocess_output, get_logger, log_subprocess_output
from .semver import SemVer

logger = get_logger(__name__)


DEFAULT_PLAYWRIGHT_ROOT = Path(
    os.environ.get("ABX_PKG_PLAYWRIGHT_ROOT", "~/.cache/abx-pkg/playwright"),
).expanduser()


# Playwright's Node.js API exposes canonical browser types. Branded channels
# (chrome, msedge, etc.) live under ``chromium``.
PLAYWRIGHT_BROWSER_TYPES = ("chromium", "firefox", "webkit")


class PlaywrightProvider(BinProvider):
    """Playwright browser installer provider.

    Installs Chromium / Firefox / WebKit (and headless shells) into a
    managed ``browser_cache_dir`` (``PLAYWRIGHT_BROWSERS_PATH``) via
    ``playwright install --with-deps``, then symlinks each browser's
    executable into ``browser_bin_dir`` for direct ``load()`` access.

    ``--with-deps`` installs system dependencies for browsers and requires
    root on Linux. This provider tries the install with ``sudo -E`` first
    when running as a non-root user, and falls back to running without
    ``sudo`` if the sudo attempt fails; both error outputs are surfaced
    together if both attempts fail.
    """

    name: BinProviderName = "playwright"
    INSTALLER_BIN: BinName = "playwright"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "playwright_root"
    BIN_DIR_FIELD: ClassVar[str | None] = "browser_bin_dir"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(default=None, repr=False)
    min_release_age: float | None = Field(default=None, repr=False)

    playwright_root: Path | None = None
    browser_bin_dir: Path | None = None
    browser_cache_dir: Path | None = None  # mirrors PLAYWRIGHT_BROWSERS_PATH

    # Flags unconditionally prepended to every ``playwright install`` call.
    # Users can override by passing their own value, but the default leaves
    # ``--with-deps`` in place since that is the main reason to use this
    # provider over raw ``npm install playwright``.
    playwright_install_args: list[str] = ["--with-deps"]

    def supports_min_release_age(self, action) -> bool:
        return False

    def supports_postinstall_disable(self, action) -> bool:
        return False

    @computed_field
    @property
    def install_root(self) -> Path:
        if self.playwright_root:
            return self.playwright_root
        if self.browser_bin_dir:
            return self.browser_bin_dir.parent
        if self.browser_cache_dir:
            return self.browser_cache_dir.parent
        return DEFAULT_PLAYWRIGHT_ROOT

    @computed_field
    @property
    def bin_dir(self) -> Path:
        return self.browser_bin_dir or (self.install_root / "bin")

    @computed_field
    @property
    def cache_dir(self) -> Path:
        return self.browser_cache_dir or (self.install_root / "cache")

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

    def _cli_binary(self) -> Binary:
        cli_provider = NpmProvider(
            npm_prefix=self.npm_prefix,
            postinstall_scripts=True,
            min_release_age=0,
        )
        return Binary(
            name="playwright",
            binproviders=[cli_provider],
            overrides={"npm": {"install_args": ["playwright"]}},
            postinstall_scripts=True,
            min_release_age=0,
        ).load_or_install()

    def _playwright_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.cache_dir)
        # playwright's CLI defers to node; make sure our managed
        # ``npm_prefix/node_modules`` is resolvable by the shim.
        node_modules = self.npm_prefix / "node_modules"
        if node_modules.is_dir():
            existing = env.get("NODE_PATH", "")
            env["NODE_PATH"] = (
                f"{node_modules}:{existing}" if existing else str(node_modules)
            )
        return env

    def setup(
        self,
        *,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
    ) -> None:
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        cli_binary = self._cli_binary()
        self._INSTALLER_BIN_ABSPATH = cli_binary.abspath
        self._INSTALLER_BINARY = cli_binary
        self.PATH = self._merge_PATH(
            self.bin_dir,
            self.npm_prefix / "node_modules" / ".bin",
            PATH="",
            prepend=True,
        )

    # --------------------------------------------------------------
    # install-args / browser-name helpers

    def _browser_name(
        self,
        bin_name: str,
        install_args: Iterable[str],
    ) -> str:
        """Pick the canonical browser name from an ``install_args`` list.

        ``install_args`` arrives looking like ``["chromium"]`` or
        ``["--no-shell", "firefox"]``; skip flags and return the first
        positional argument, or fall back to ``bin_name``.
        """
        for arg in install_args:
            arg_str = str(arg)
            if arg_str.startswith("-"):
                continue
            return arg_str
        return bin_name

    def _browser_type(self, browser_name: str) -> str:
        """Map branded channels onto their underlying ``playwright`` browser type.

        ``chrome`` / ``chrome-beta`` / ``msedge`` / ``chromium-headless-shell``
        all live inside the ``chromium`` type's ``executablePath()``.
        """
        lowered = browser_name.lower()
        if lowered in PLAYWRIGHT_BROWSER_TYPES:
            return lowered
        if lowered.startswith("chromium") or lowered in (
            "chrome",
            "chrome-beta",
            "chrome-dev",
            "chrome-canary",
            "msedge",
            "msedge-beta",
            "msedge-dev",
            "msedge-canary",
        ):
            return "chromium"
        if lowered.startswith("firefox"):
            return "firefox"
        if lowered.startswith("webkit"):
            return "webkit"
        return lowered

    # --------------------------------------------------------------
    # sudo-then-fallback runner

    def _sudo_abspath(self) -> Path | None:
        try:
            sudo_binary = Binary(
                name="sudo",
                binproviders=[
                    EnvProvider(postinstall_scripts=True, min_release_age=0),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            ).load(nocache=True)
        except Exception:
            return None
        if sudo_binary is None or sudo_binary.loaded_abspath is None:
            return None
        return sudo_binary.loaded_abspath

    def _run_playwright_install(
        self,
        install_args: list[str],
        timeout: int | None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``playwright install <args>``, trying sudo first on non-root hosts.

        ``playwright install --with-deps`` requires root to apt-get install
        system libs. Follow the shared sudo pattern: if we're not already
        root and sudo is available, try ``sudo -E playwright install ...``
        first; if that fails (or isn't available), fall back to running
        playwright directly. If *both* attempts fail, merge both error
        outputs into the final returned CompletedProcess so the caller
        sees why sudo failed AND why the unprivileged retry failed.
        """
        installer_bin = self._require_installer_bin()
        env = self._playwright_env()
        cmd_args = ["install", *install_args]
        resolved_timeout = timeout if timeout is not None else self.install_timeout

        sudo_failure_output: str | None = None
        if os.geteuid() != 0:
            sudo_abspath = self._sudo_abspath()
            if sudo_abspath is not None:
                sudo_proc = self.exec(
                    bin_name=sudo_abspath,
                    cmd=[
                        "-E",
                        "-n",
                        "--",
                        str(installer_bin),
                        *cmd_args,
                    ],
                    cwd=self.install_root,
                    timeout=resolved_timeout,
                    env=env,
                )
                if sudo_proc.returncode == 0:
                    self._chown_cache_dir_to_current_user(sudo_abspath)
                    return sudo_proc
                log_subprocess_output(
                    logger,
                    f"{self.__class__.__name__} sudo install attempt",
                    sudo_proc.stdout,
                    sudo_proc.stderr,
                )
                sudo_failure_output = format_subprocess_output(
                    sudo_proc.stdout,
                    sudo_proc.stderr,
                )

        proc = self.exec(
            bin_name=installer_bin,
            cmd=cmd_args,
            cwd=self.install_root,
            timeout=resolved_timeout,
            env=env,
        )
        if proc.returncode != 0 and sudo_failure_output:
            merged_stderr = "\n".join(
                part
                for part in (
                    proc.stderr,
                    f"Previous sudo attempt failed:\n{sudo_failure_output}",
                )
                if part
            )
            return subprocess.CompletedProcess(
                proc.args,
                proc.returncode,
                proc.stdout,
                merged_stderr,
            )
        return proc

    def _chown_cache_dir_to_current_user(self, sudo_abspath: Path) -> None:
        if os.geteuid() == 0 or not self.cache_dir.exists():
            return
        uid = os.getuid()
        gid = os.getgid()
        chown_proc = self.exec(
            bin_name=sudo_abspath,
            cmd=["-n", "chown", "-R", f"{uid}:{gid}", str(self.cache_dir)],
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

    # --------------------------------------------------------------
    # executable path resolution (via playwright's Node.js API)

    def _playwright_executable_path(self, browser_type: str) -> Path | None:
        """Ask playwright-core for the browser's ``executablePath()``.

        This matches exactly what ``playwright.<browser>.launch()`` would
        use, so we stay consistent with upstream across OSes / builds /
        headless-shell variants without hardcoding path patterns.
        """
        cli_binary = getattr(self, "_INSTALLER_BINARY", None)
        if cli_binary is None or cli_binary.loaded_abspath is None:
            return None
        node_modules = self.npm_prefix / "node_modules"
        if not (node_modules / "playwright").is_dir():
            return None

        node_binary = Binary(
            name="node",
            binproviders=[EnvProvider(postinstall_scripts=True, min_release_age=0)],
            postinstall_scripts=True,
            min_release_age=0,
        ).load(nocache=True)
        if node_binary is None or node_binary.loaded_abspath is None:
            return None

        script = (
            "const pw = require(process.argv[1]);"
            "const type = process.argv[2];"
            "const bt = pw[type];"
            "if (!bt) { process.exit(2); }"
            "try { process.stdout.write(JSON.stringify({path: bt.executablePath()})); }"
            "catch (err) { process.stdout.write(JSON.stringify({error: String(err)})); process.exit(3); }"
        )
        proc = self.exec(
            bin_name=node_binary.loaded_abspath,
            cmd=[
                "-e",
                script,
                str(node_modules / "playwright"),
                browser_type,
            ],
            cwd=self.install_root,
            timeout=self.version_timeout,
            env=self._playwright_env(),
            quiet=True,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            payload = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            return None
        path_str = payload.get("path")
        if not path_str:
            return None
        path = Path(path_str)
        return path if path.exists() else None

    def _symlink_path(self, bin_name: str) -> Path:
        return self.bin_dir / bin_name

    def _refresh_symlink(self, bin_name: str, target: Path) -> Path:
        link_path = self._symlink_path(bin_name)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink(missing_ok=True)
        # On macOS the executable is buried inside a ``.app`` bundle, so a
        # plain symlink to the inner binary works but some consumers prefer
        # a tiny shell shim (matching PuppeteerProvider).
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

        install_args = (
            context.get("install_args")
            or self.get_install_args(str(bin_name))
            or [str(bin_name)]
        )
        browser_type = self._browser_type(
            self._browser_name(str(bin_name), install_args),
        )
        resolved = self._playwright_executable_path(browser_type)
        if not resolved:
            return None
        try:
            return self._refresh_symlink(str(bin_name), resolved)
        except OSError:
            return resolved

    # --------------------------------------------------------------
    # action handlers

    def _normalize_install_args(self, install_args: Iterable[str]) -> list[str]:
        """Prepend ``playwright_install_args`` (``--with-deps`` by default).

        Keep the user's args intact; just make sure the configured
        provider-level defaults lead. Callers may override by including
        an explicit ``--with-deps`` / ``--no-with-deps`` or passing
        ``playwright_install_args=[]``.
        """
        normalized: list[str] = [str(arg) for arg in self.playwright_install_args]
        for arg in install_args:
            arg_str = str(arg)
            if arg_str in normalized:
                continue
            normalized.append(arg_str)
        return normalized

    @remap_kwargs({"packages": "install_args"})
    def default_install_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        **context,
    ) -> str:
        self.setup()
        install_args = list(install_args or self.get_install_args(bin_name))
        browser_name = self._browser_name(bin_name, install_args)
        normalized_install_args = self._normalize_install_args(install_args)

        if self.dry_run:
            return (
                f"DRY_RUN would run: playwright install "
                f"{' '.join(normalized_install_args)}"
            )

        proc = self._run_playwright_install(normalized_install_args, timeout)
        if proc.returncode != 0:
            self._raise_proc_error("install", bin_name, proc)

        browser_type = self._browser_type(browser_name)
        executable_path = self._playwright_executable_path(browser_type)
        if not executable_path or not executable_path.exists():
            raise FileNotFoundError(
                f"{self.__class__.__name__} could not resolve installed browser "
                f"path for {bin_name} (browser_type={browser_type}, "
                f"cache_dir={self.cache_dir})",
            )

        self._refresh_symlink(bin_name, executable_path)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        **context,
    ) -> str:
        # ``playwright install --force`` re-downloads an already-present
        # browser. Inject it alongside the user args so the existing
        # browser directory gets refreshed in place.
        merged_args = list(install_args or self.get_install_args(bin_name))
        if "--force" not in merged_args:
            merged_args = ["--force", *merged_args]
        return self.default_install_handler(
            bin_name,
            install_args=merged_args,
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
        browser_type = self._browser_type(browser_name)

        # Drop the symlink first so ``load()`` stops seeing the tool even if
        # the browser dir removal partially fails.
        self._symlink_path(bin_name).unlink(missing_ok=True)

        # Remove every ``<browser>-<version>`` dir that matches the
        # resolved browser type. ``playwright uninstall`` only removes
        # *unused* browsers, so do the directory cleanup ourselves.
        if not self.cache_dir.is_dir():
            return True

        removed_any = False
        prefix_matches = {
            "chromium": ("chromium-", "chromium_headless_shell-", "ffmpeg-"),
            "firefox": ("firefox-",),
            "webkit": ("webkit-",),
        }.get(browser_type, (f"{browser_type}-",))

        for entry in self.cache_dir.iterdir():
            if not entry.is_dir():
                continue
            if any(entry.name.startswith(prefix) for prefix in prefix_matches):
                shutil.rmtree(entry, ignore_errors=True)
                removed_any = True
        return removed_any or True


if __name__ == "__main__":
    # Usage:
    #   ./binprovider_playwright.py load chromium
    #   ./binprovider_playwright.py install chromium
    result = playwright_provider = PlaywrightProvider()
    func = None
    if len(sys.argv) > 1:
        result = func = getattr(playwright_provider, sys.argv[1])
    if len(sys.argv) > 2 and callable(func):
        result = func(sys.argv[2])
    print(result)
