#!/usr/bin/env python3

__package__ = "abx_pkg"

import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, computed_field, model_validator

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
from .binprovider import BinProvider, remap_kwargs
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
    symlinked into ``browser_bin_dir`` so ``load(bin_name)`` finds it
    directly. When ``playwright_root`` is left unset, playwright picks
    its own default browsers path, the npm CLI bootstraps against the
    host's npm default, and ``load()`` returns the resolved
    ``executablePath()`` directly without creating any managed
    symlinks.

    ``--with-deps`` installs system packages and requires root on
    Linux, so ``euid`` defaults to ``0``: the base ``BinProvider.exec``
    machinery routes every subprocess through ``sudo -n -- ...`` first
    on non-root hosts, falls back to running without sudo if that
    fails, and merges both stderr outputs if both attempts fail. On
    root hosts it just runs directly.
    """

    name: BinProviderName = "playwright"
    INSTALLER_BIN: BinName = "playwright"
    INSTALL_ROOT_FIELD: ClassVar[str | None] = "playwright_root"
    BIN_DIR_FIELD: ClassVar[str | None] = "browser_bin_dir"

    PATH: PATHStr = ""
    postinstall_scripts: bool | None = Field(default=None, repr=False)
    min_release_age: float | None = Field(default=None, repr=False)

    # ``playwright_root`` is both the abx-pkg install root and the
    # ``PLAYWRIGHT_BROWSERS_PATH`` we export to the CLI. Leave unset to
    # let playwright use its own OS-default browsers path.
    # Default: ABX_PKG_PLAYWRIGHT_ROOT > ABX_PKG_LIB_DIR/playwright > None.
    playwright_root: Path | None = abx_pkg_install_root_default("playwright")
    browser_bin_dir: Path | None = None  # symlink dir for resolved browsers

    # Flags prepended to every ``playwright install`` call. Default
    # keeps ``--with-deps`` on so the provider takes care of installing
    # system dependencies for browsers.
    playwright_install_args: list[str] = ["--with-deps"]

    # Force the base ``exec`` path through its sudo-first-then-fallback
    # code on non-root hosts. ``--with-deps`` needs root on Linux to
    # apt-get install system libs; the base class already handles both
    # halves of that pattern for us.
    euid: int | None = 0

    def supports_min_release_age(self, action) -> bool:
        return False

    def supports_postinstall_disable(self, action) -> bool:
        return False

    @computed_field
    @property
    def install_root(self) -> Path | None:
        return self.playwright_root

    @computed_field
    @property
    def bin_dir(self) -> Path | None:
        # Only maintain a managed symlink dir when the caller pinned
        # ``playwright_root`` — otherwise there's nothing abx-pkg is
        # managing, so ``load()`` just returns ``executablePath()``.
        if self.browser_bin_dir:
            return self.browser_bin_dir
        if self.playwright_root:
            return self.playwright_root / "bin"
        return None

    @computed_field
    @property
    def npm_prefix(self) -> Path | None:
        # Nest the npm bootstrap inside ``playwright_root`` when set;
        # otherwise let ``NpmProvider`` use the host's npm default.
        if self.playwright_root:
            return self.playwright_root / "_abx_npm"
        return None

    @computed_field
    @property
    def is_valid(self) -> bool:
        return bool(self.INSTALLER_BIN_ABSPATH)

    @model_validator(mode="after")
    def load_PATH_from_root(self) -> Self:
        path_entries: list[Path] = []
        if self.bin_dir is not None:
            path_entries.append(self.bin_dir)
        if self.npm_prefix is not None:
            path_entries.append(self.npm_prefix / "node_modules" / ".bin")
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
        if self.playwright_root is not None:
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.playwright_root)
            env_assignments.append(
                f"PLAYWRIGHT_BROWSERS_PATH={self.playwright_root}",
            )
        if env_assignments:
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
            self.npm_prefix.parent if self.npm_prefix is not None else None,
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
        if self.playwright_root is not None:
            self.playwright_root.mkdir(parents=True, exist_ok=True)
        if self.bin_dir is not None:
            self.bin_dir.mkdir(parents=True, exist_ok=True)

        # Bootstrap the ``playwright`` npm package (which ships the CLI
        # and its ``playwright-core`` peer). Nest it under
        # ``playwright_root`` when one is pinned; otherwise leave
        # ``npm_prefix`` unset so ``NpmProvider`` falls back to the
        # host's own npm default.
        npm_provider_kwargs: dict = {
            "postinstall_scripts": True,
            "min_release_age": 0,
        }
        if self.npm_prefix is not None:
            npm_provider_kwargs["npm_prefix"] = self.npm_prefix
        cli = Binary(
            name="playwright",
            binproviders=[NpmProvider(**npm_provider_kwargs)],
            overrides={"npm": {"install_args": ["playwright"]}},
            postinstall_scripts=True,
            min_release_age=0,
        ).load_or_install()
        self._INSTALLER_BIN_ABSPATH = cli.abspath
        path_entries: list[Path] = []
        if self.bin_dir is not None:
            path_entries.append(self.bin_dir)
        if self.npm_prefix is not None:
            path_entries.append(self.npm_prefix / "node_modules" / ".bin")
        if path_entries:
            self.PATH = self._merge_PATH(
                *path_entries,
                PATH="",
                prepend=True,
            )

    def _playwright_executable_path(self, bin_name: str) -> Path | None:
        """Return ``playwright[bin_name].executablePath()`` via node.

        Delegates to ``playwright-core`` so we stay consistent with
        upstream layout across OSes and builds without hardcoding
        browser-specific path patterns. When ``npm_prefix`` is pinned
        we ``require()`` the absolute ``<prefix>/node_modules/playwright``
        path so the managed install wins; otherwise we let node's own
        module resolution find whichever ``playwright`` the host ships.
        """
        if self.npm_prefix is not None:
            pw_require_target = self.npm_prefix / "node_modules" / "playwright"
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
        proc = self.exec(
            bin_name="node",
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
        **context,
    ) -> HostBinPath | None:
        if self.bin_dir is not None:
            link = self.bin_dir / str(bin_name)
            if link.exists() and os.access(link, os.X_OK):
                return link
        resolved = self._playwright_executable_path(str(bin_name))
        if not resolved:
            return None
        # When ``playwright_root`` is pinned, a hit from
        # ``executablePath()`` that points outside that managed tree
        # (e.g. an ambient system install) should not satisfy
        # ``load()`` — otherwise an unrelated host-wide playwright
        # install would silently hijack resolution.
        if self.playwright_root is not None:
            root_real = self.playwright_root.resolve(strict=False)
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
        **context,
    ) -> str:
        self.setup()
        install_args = list(install_args or self.get_install_args(bin_name))
        merged_args = [*self.playwright_install_args, *install_args]

        if self.dry_run:
            return f"DRY_RUN would run: playwright install {' '.join(merged_args)}"

        proc = self.exec(
            bin_name=self._require_installer_bin(),
            cmd=["install", *merged_args],
            timeout=timeout if timeout is not None else self.install_timeout,
        )
        if proc.returncode != 0:
            self._raise_proc_error("install", bin_name, proc)

        resolved = self._playwright_executable_path(bin_name)
        if not resolved or not resolved.exists():
            raise FileNotFoundError(
                f"{self.__class__.__name__} could not resolve installed browser "
                f"path for {bin_name} (playwright_root={self.playwright_root})",
            )
        if self.bin_dir is not None:
            self._refresh_symlink(bin_name, resolved)
        return format_subprocess_output(proc.stdout, proc.stderr)

    @remap_kwargs({"packages": "install_args"})
    def default_update_handler(
        self,
        bin_name: str,
        install_args: InstallArgs | None = None,
        timeout: int | None = None,
        **context,
    ) -> str:
        # Browser versions are pinned by the ``playwright`` npm package,
        # so a real upgrade means bumping that package first and then
        # re-running ``playwright install`` to pull the new browser
        # builds. When ``npm_prefix`` is pinned, drive the bump through
        # our managed NpmProvider; otherwise trust the host-installed
        # playwright to already be at the desired version.
        if self.npm_prefix is not None:
            try:
                NpmProvider(
                    npm_prefix=self.npm_prefix,
                    postinstall_scripts=True,
                    min_release_age=0,
                ).update("playwright")
            except Exception:
                logger.debug(
                    "PlaywrightProvider: npm update for ``playwright`` failed, "
                    "falling through to re-running ``playwright install``",
                    exc_info=True,
                )
            # Drop the cached installer abspath so ``setup()`` in the
            # install handler below re-resolves against the (possibly
            # new) bin location.
            self._INSTALLER_BIN_ABSPATH = None

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
        # Drop the symlink first (if we're managing one) so ``load()``
        # stops seeing the tool even if browser dir removal partially
        # fails.
        if self.bin_dir is not None:
            (self.bin_dir / bin_name).unlink(missing_ok=True)

        # ``playwright uninstall`` only removes *unused* browsers from
        # the entire host, so drop the matching directories ourselves.
        # Only touch ``playwright_root`` if the caller pinned one — we
        # don't delete from playwright's own OS-default cache.
        if self.playwright_root is not None and self.playwright_root.is_dir():
            for entry in self.playwright_root.iterdir():
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
