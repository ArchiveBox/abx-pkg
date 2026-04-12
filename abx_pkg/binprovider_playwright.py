#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import shlex
import shutil
import sys
import platform
from pathlib import Path
from typing import Self

from pydantic import Field, PrivateAttr, TypeAdapter, computed_field, model_validator

from .base_types import (
    BinName,
    BinProviderName,
    HostBinPath,
    InstallArgs,
    PATHStr,
    abx_pkg_install_root_default,
    bin_abspath,
)
from .binary import Binary
from .binprovider import BinProvider, EnvProvider, remap_kwargs
from .binprovider_npm import NpmProvider
from .logging import format_subprocess_output, get_logger
from .semver import SemVer

logger = get_logger(__name__)


class PlaywrightProvider(BinProvider):
    """Playwright browser installer provider.

    Drives ``playwright install --with-deps <install_args>`` against the
    ``playwright`` npm package. When ``playwright_root`` is set it
    doubles as the abx-pkg install root AND ``PLAYWRIGHT_BROWSERS_PATH``:
    browsers land inside it (``chromium-<build>/`` etc.), a dedicated
    npm prefix is nested under it, and each requested browser is
    surfaced from ``bin_dir`` so ``load(bin_name)`` finds it directly.
    When ``playwright_root`` is left unset, playwright picks its own
    default browsers path, the npm CLI bootstraps against the host's
    npm default, and ``load()`` returns the resolved ``executablePath()``
    directly without creating any managed symlinks.

    ``--with-deps`` installs system packages and requires root on
    Linux, so ``euid`` defaults to ``0``: the base ``BinProvider.exec``
    machinery routes every subprocess through ``sudo -n -- ...`` first
    on non-root hosts, falls back to running without sudo if that
    fails, and merges both stderr outputs if both attempts fail. On
    root hosts it just runs directly.
    """

    name: BinProviderName = "playwright"
    INSTALLER_BIN: BinName = "playwright"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(default=None, repr=False)
    min_release_age: float | None = Field(default=None, repr=False)

    # ``playwright_root`` is both the abx-pkg install root and the
    # ``PLAYWRIGHT_BROWSERS_PATH`` we export to the CLI. Leave unset to
    # let playwright use its own OS-default browsers path.
    # Default: ABX_PKG_PLAYWRIGHT_ROOT > ABX_PKG_LIB_DIR/playwright > None.
    install_root: Path | None = Field(
        default_factory=lambda: abx_pkg_install_root_default("playwright"),
        validation_alias="playwright_root",
    )
    bin_dir: Path | None = None

    # Only Linux needs the sudo-first execution path for
    # ``playwright install --with-deps``. On macOS and elsewhere,
    # run as the normal user by default.
    euid: int | None = 0 if platform.system().lower() == "linux" else None
    _NODE_BINARY: Binary | None = PrivateAttr(default=None)

    @computed_field
    @property
    def ENV(self) -> "dict[str, str]":
        if not self.install_root:
            return {}
        return {"PLAYWRIGHT_BROWSERS_PATH": str(self.install_root)}

    def supports_min_release_age(self, action) -> bool:
        return False

    def supports_postinstall_disable(self, action) -> bool:
        return False

    @model_validator(mode="after")
    def load_PATH_from_root(self) -> Self:
        if self.bin_dir is None and self.install_root is not None:
            self.bin_dir = self.install_root / "bin"
        path_entries: list[Path] = []
        if self.bin_dir is not None:
            path_entries.append(self.bin_dir)
        # In hermetic mode (install_root outside LIB_DIR), add our own
        # npm bin dir.  In managed mode, NpmProvider handles its own PATH.
        lib_dir = os.environ.get("ABX_PKG_LIB_DIR")
        hermetic = self.install_root is not None and (
            not lib_dir
            or not str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        )
        if hermetic and self.install_root is not None:
            path_entries.append(self.install_root / "npm" / "node_modules" / ".bin")
        if path_entries:
            self.PATH = self._merge_PATH(
                *path_entries,
                PATH=self.PATH,
                prepend=True,
            )
        return self

    def exec(
        self,
        bin_name,
        cmd=(),
        cwd: Path | str | None = None,
        quiet=False,
        should_log_command: bool = True,
        **kwargs,
    ):
        # ``euid=0`` routes every subprocess through the base class's
        # ``sudo -n -- ...`` fallback on non-root hosts so
        # ``--with-deps`` can apt-get install browser system libs.
        # ``sudo`` strips most env vars by default (``env_reset`` in
        # sudoers), so simply setting ``env["PLAYWRIGHT_BROWSERS_PATH"]``
        # would be silently dropped before reaching the child. Wrap the
        # whole command with ``/usr/bin/env KEY=VAL -- <cmd>`` instead:
        # ``env`` is a trusted utility that sudo executes happily, and
        # the assignments are CLI args (not env vars) so sudo's filter
        # never sees them. ``env`` then sets the vars and execs the
        # real command. Works identically when sudo isn't involved
        # (root host or already-elevated). The first command token
        # must be an absolute path because sudo's secure_path may not
        # contain our managed bin dir.
        env = (kwargs.pop("env", None) or os.environ.copy()).copy()
        env_assignments: list[str] = []
        if self.install_root is not None:
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.install_root)
            env_assignments.append(
                f"PLAYWRIGHT_BROWSERS_PATH={self.install_root}",
            )
        needs_sudo_env_wrapper = os.geteuid() != 0 and self.EUID != os.geteuid()
        if env_assignments and needs_sudo_env_wrapper:
            resolved_bin = bin_name
            if not os.path.isabs(str(bin_name)):
                resolved_bin = bin_abspath(str(bin_name), PATH=self.PATH) or bin_name
            # POSIX ``env``: first non-assignment positional arg is the
            # utility to exec; no ``--`` separator (older coreutils
            # don't support it).
            cmd = [*env_assignments, str(resolved_bin), *cmd]
            bin_name = "/usr/bin/env"
        cwd_candidates: list[Path | str | None] = [
            cwd,
            self.install_root,
            Path.cwd(),
        ]
        resolved_cwd = next(
            (str(candidate) for candidate in cwd_candidates if candidate is not None),
            ".",
        )
        return super().exec(
            bin_name=bin_name,
            cmd=cmd,
            cwd=resolved_cwd,
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
        if self.install_root is not None:
            self.install_root.mkdir(parents=True, exist_ok=True)
        if self.bin_dir is not None:
            self.bin_dir.mkdir(parents=True, exist_ok=True)
        lib_dir = os.environ.get("ABX_PKG_LIB_DIR")
        if (
            self.install_root is not None
            and lib_dir
            and str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        ):
            npm_install_root = Path(lib_dir) / "npm"
        elif self.install_root is not None:
            npm_install_root = self.install_root / "npm"
        else:
            npm_install_root = None
        expected_playwright_module = (
            npm_install_root / "node_modules" / "playwright"
            if npm_install_root is not None
            else None
        )
        try:
            cached = self.INSTALLER_BINARY(no_cache=no_cache)
        except Exception:
            cached = None
        if (
            cached
            and cached.loaded_abspath
            and (
                expected_playwright_module is None
                or expected_playwright_module.is_dir()
            )
        ):
            path_entries: list[Path] = []
            if self.bin_dir is not None:
                path_entries.append(self.bin_dir)
            if self.install_root is not None and (
                not lib_dir
                or not str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
            ):
                path_entries.append(
                    self.install_root / "npm" / "node_modules" / ".bin",
                )
            if path_entries:
                self.PATH = self._merge_PATH(
                    *path_entries,
                    PATH="",
                    prepend=True,
                )
            return
        # Bootstrap the ``playwright`` npm package (which ships the CLI
        # and its ``playwright-core`` peer). Nest it under
        # ``playwright_root`` when one is pinned; otherwise leave
        # ``npm_prefix`` unset so ``NpmProvider`` falls back to the
        # host's own npm default.
        #
        # Security flags are propagated to the bootstrap install so
        # ``--min-release-age`` / ``--postinstall-scripts`` apply to the
        # ``playwright`` npm package too, not just the browser download.
        effective_postinstall = (
            self.postinstall_scripts
            if postinstall_scripts is None
            else postinstall_scripts
        )
        effective_min_release_age = (
            self.min_release_age if min_release_age is None else min_release_age
        )
        # playwright's postinstall script downloads browsers — it must
        # run for the CLI to work. Default to True only when the caller
        # didn't express a preference.
        if effective_postinstall is None:
            effective_postinstall = True
        effective_min_release_age = effective_min_release_age or 0

        # Determine where to install the playwright npm package.
        # Hermetic: install_root/npm
        # Managed LIB_DIR: LIB_DIR/npm (shared with NpmProvider)
        # Global: no install_root (NpmProvider picks its own default)
        if (
            self.install_root is not None
            and lib_dir
            and str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        ):
            npm_install_root = Path(lib_dir) / "npm"
        cli_provider = NpmProvider(
            install_root=npm_install_root,
            postinstall_scripts=effective_postinstall,
            min_release_age=effective_min_release_age,
        )
        cli = Binary(
            name="playwright",
            binproviders=[cli_provider],
            overrides={"npm": {"install_args": ["playwright"]}},
            postinstall_scripts=effective_postinstall,
            min_release_age=effective_min_release_age,
        ).install(no_cache=no_cache)
        path_entries: list[Path] = []
        if self.bin_dir is not None:
            path_entries.append(self.bin_dir)
        if npm_install_root is not None:
            path_entries.append(npm_install_root / "node_modules" / ".bin")
        if path_entries:
            self.PATH = self._merge_PATH(
                *path_entries,
                PATH="",
                prepend=True,
            )
        loaded_cli = self.load(self.INSTALLER_BIN, quiet=True, no_cache=True)
        self._INSTALLER_BINARY = (
            loaded_cli if loaded_cli is not None else cli
        )  # bootstrap: seed cache after npm install

    def _playwright_browser_path(
        self,
        bin_name: str,
        *,
        no_cache: bool = False,
    ) -> Path | None:
        """Return ``playwright[bin_name].executablePath()`` via node.

        Delegates to ``playwright-core`` so we stay consistent with
        upstream layout across OSes and builds without hardcoding
        browser-specific path patterns. When ``npm_prefix`` is pinned
        we ``require()`` the absolute ``<prefix>/node_modules/playwright``
        path so the managed install wins; otherwise we let node's own
        module resolution find whichever ``playwright`` the host ships.
        """
        # Find the playwright npm module to call executablePath().
        # Hermetic: install_root/npm/node_modules/playwright
        # Managed LIB_DIR: LIB_DIR/npm/node_modules/playwright
        # Global: let node's require() find it
        lib_dir = os.environ.get("ABX_PKG_LIB_DIR")
        hermetic = self.install_root is not None and (
            not lib_dir
            or not str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        )
        if hermetic and self.install_root is not None:
            pw_require_target = (
                self.install_root / "npm" / "node_modules" / "playwright"
            )
        elif lib_dir:
            pw_require_target = Path(lib_dir) / "npm" / "node_modules" / "playwright"
        else:
            pw_require_target = None

        if pw_require_target is not None:
            if not pw_require_target.is_dir():
                return None
            require_arg = str(pw_require_target)
        else:
            require_arg = "playwright"
        script = (
            "const pw=require(process.argv[1]);"
            "const bt=pw[process.argv[2]];"
            "if(!bt){process.exit(2);}"
            "try{process.stdout.write(bt.executablePath());}"
            "catch(e){process.exit(3);}"
        )
        node_binary = None if no_cache else self._NODE_BINARY
        if node_binary is None:
            node_binary = Binary(
                name="node",
                binproviders=[
                    EnvProvider(
                        postinstall_scripts=True,
                        min_release_age=0,
                    ),
                ],
                postinstall_scripts=True,
                min_release_age=0,
            ).load(no_cache=no_cache)
            if not no_cache and node_binary is not None:
                self._NODE_BINARY = node_binary
        if node_binary is None or node_binary.loaded_abspath is None:
            return None
        proc = self.exec(
            bin_name=node_binary.loaded_abspath,
            cmd=["-e", script, require_arg, bin_name],
            quiet=True,
            timeout=self.version_timeout,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        path = Path(proc.stdout.strip())
        return path if path.exists() else None

    def _refresh_symlink(self, bin_name: str, target: Path) -> Path:
        assert self.bin_dir is not None, (
            "_refresh_symlink must only be called when bin_dir is set"
        )
        link = self.bin_dir / bin_name
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink(missing_ok=True)
        # On macOS the executable is buried inside a ``.app`` bundle, so
        # write a tiny shell shim instead of a symlink (same pattern as
        # PuppeteerProvider).
        if os.name == "posix" and ".app/Contents/MacOS/" in str(target):
            link.write_text(
                f'#!/bin/sh\nexec {shlex.quote(str(target))} "$@"\n',
                encoding="utf-8",
            )
            link.chmod(0o755)
            return link
        link.symlink_to(target)
        return link

    def default_abspath_handler(
        self,
        bin_name: BinName | HostBinPath,
        no_cache: bool = False,
        **context,
    ) -> HostBinPath | None:
        # Installer binary: delegate to base class (searches PATH directly)
        if str(bin_name) == self.INSTALLER_BIN:
            try:
                abspath = super().default_abspath_handler(
                    bin_name,
                    no_cache=no_cache,
                    **context,
                )
                if abspath:
                    return TypeAdapter(HostBinPath).validate_python(abspath)
            except Exception:
                return None
            return None
        if self.bin_dir is not None:
            link = self.bin_dir / str(bin_name)
            if link.exists() and os.access(link, os.X_OK):
                return link
        resolved = self._playwright_browser_path(
            str(bin_name),
            no_cache=bool(context.get("no_cache", False)),
        )
        if not resolved:
            return None
        # When ``playwright_root`` is pinned, a hit from
        # ``executablePath()`` that points outside that managed tree
        # (e.g. an ambient system install) should not satisfy
        # ``load()`` — otherwise an unrelated host-wide playwright
        # install would silently hijack resolution.
        if self.install_root is not None:
            root_real = self.install_root.resolve(strict=False)
            if root_real not in resolved.resolve(strict=False).parents:
                return None
        if self.bin_dir is None:
            return resolved
        try:
            return self._refresh_symlink(str(bin_name), resolved)
        except OSError:
            return resolved

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
        merged_args = ["--with-deps", *install_args]
        if no_cache and "--force" not in merged_args:
            merged_args = ["--force", *merged_args]

        if self.dry_run:
            return f"DRY_RUN would run: playwright install {' '.join(merged_args)}"

        effective_timeout = timeout if timeout is not None else self.install_timeout
        installer_bin = self.INSTALLER_BINARY().loaded_abspath
        assert installer_bin
        install_cmd = ["install", *merged_args]
        # Retry on dpkg lock contention (apt-get may be held by a
        # concurrent process e.g. unattended-upgrades or a prior test).
        import time as _time

        proc = None
        for attempt in range(3):
            proc = self.exec(
                bin_name=installer_bin,
                cmd=install_cmd,
                timeout=effective_timeout,
            )
            if proc.returncode == 0:
                break
            stderr = proc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            if "dpkg" in stderr and "lock" in stderr and attempt < 2:
                logger.warning("dpkg lock held, retrying in %ds...", 5 * (attempt + 1))
                _time.sleep(5 * (attempt + 1))
                continue
            self._raise_proc_error("install", bin_name, proc)

        # When ``playwright install --with-deps`` runs through the
        # base ``BinProvider.exec`` sudo path on a non-root host, the
        # downloaded browser tree ends up owned by root. Hand it back
        # to the calling user so subsequent file operations (notably
        # ``tempfile.TemporaryDirectory`` cleanup in tests) don't hit
        # ``PermissionError``. The chown itself routes through the
        # same euid=0 → sudo path, so it gets root permission for
        # free. No-op when we're already root or no managed root.
        if (
            self.install_root is not None
            and self.install_root.is_dir()
            and os.geteuid() != 0
        ):
            chown_bin = shutil.which("chown") or "/usr/sbin/chown"
            self.exec(
                bin_name=chown_bin,
                cmd=[
                    "-R",
                    f"{os.getuid()}:{os.getgid()}",
                    str(self.install_root),
                ],
                quiet=True,
            )

        resolved = self._playwright_browser_path(bin_name, no_cache=no_cache)
        if not resolved or not resolved.exists():
            raise FileNotFoundError(
                f"{self.__class__.__name__} could not resolve installed browser "
                f"path for {bin_name} (install_root={self.install_root})",
            )
        if self.bin_dir is not None:
            self._refresh_symlink(bin_name, resolved)
        assert proc is not None
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
        # Browser versions are pinned by the ``playwright`` npm package,
        # so a real upgrade means bumping that package first and then
        # re-running ``playwright install`` to pull the new browser
        # builds. When ``npm_prefix`` is pinned, drive the bump through
        # our managed NpmProvider; otherwise trust the host-installed
        # playwright to already be at the desired version.
        lib_dir = os.environ.get("ABX_PKG_LIB_DIR")
        hermetic = self.install_root is not None and (
            not lib_dir
            or not str(self.install_root).startswith(lib_dir.rstrip("/") + "/")
        )
        if hermetic and self.install_root is not None:
            try:
                updated_cli = NpmProvider(
                    install_root=self.install_root / "npm",
                    postinstall_scripts=True,
                    min_release_age=0,
                ).update("playwright", no_cache=no_cache)
                if updated_cli is not None and updated_cli.loaded_abspath is not None:
                    self._INSTALLER_BINARY = (
                        updated_cli  # bootstrap: seed cache after npm update
                    )
            except Exception:
                logger.debug(
                    "PlaywrightProvider: npm update for ``playwright`` failed, "
                    "falling through to re-running ``playwright install``",
                    exc_info=True,
                )
                self._INSTALLER_BINARY = None  # clear cache to force re-resolution

        merged_args = list(install_args or self.get_install_args(bin_name))
        if "--force" not in merged_args:
            merged_args = ["--force", *merged_args]
        return self.default_install_handler(
            bin_name,
            install_args=merged_args,
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
        # Drop the symlink first (if we're managing one) so ``load()``
        # stops seeing the tool even if browser dir removal partially
        # fails.
        if self.bin_dir is not None:
            (self.bin_dir / bin_name).unlink(missing_ok=True)

        # ``playwright uninstall`` only removes *unused* browsers from
        # the entire host, so drop the matching directories ourselves.
        # Only touch ``playwright_root`` if the caller pinned one — we
        # don't delete from playwright's own OS-default cache.
        if self.install_root is not None and self.install_root.is_dir():
            for entry in self.install_root.iterdir():
                if entry.is_dir() and entry.name.startswith(f"{bin_name}-"):
                    shutil.rmtree(entry, ignore_errors=True)
        return True


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
