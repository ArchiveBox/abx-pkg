from __future__ import annotations

import json
import logging as py_logging
import os
import sys
import tomllib
from dataclasses import dataclass, replace
from importlib import metadata
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import rich_click as click

from . import ALL_PROVIDER_NAMES, ALL_PROVIDERS, Binary
from .binprovider import BinProvider, env_flag_is_true
from .exceptions import ABXPkgError, BinaryOperationError
from .logging import configure_logging


DEFAULT_LIB_DIR = Path("~/.config/abx/lib").expanduser()
PROVIDER_CLASS_BY_NAME: dict[str, type[BinProvider]] = {
    cast(str, provider.model_fields["name"].default): provider
    for provider in ALL_PROVIDERS
}
MANAGED_PROVIDER_ROOTS: dict[str, Path] = {
    "pip": Path("pip/venv"),
    "uv": Path("uv/venv"),
    "npm": Path("npm"),
    "pnpm": Path("pnpm"),
    "yarn": Path("yarn"),
    "bun": Path("bun"),
    "deno": Path("deno"),
    "cargo": Path("cargo"),
    "gem": Path("gem"),
    "goget": Path("goget"),
    "nix": Path("nix"),
    "docker": Path("docker"),
    "chromewebstore": Path("chromewebstore"),
    "puppeteer": Path("puppeteer"),
    "bash": Path("bash"),
}


@dataclass(slots=True)
class CliOptions:
    lib_dir: Path
    provider_names: list[str]
    dry_run: bool
    # Binary-level fields forwarded verbatim to the Binary constructor.
    # Binary's own model validators propagate them to each provider via
    # the existing install/load_or_install/update kwarg path, which is
    # also where the ``supports_postinstall_disable`` /
    # ``supports_min_release_age`` warning emitters live.
    min_version: str | None = None
    postinstall_scripts: bool | None = None
    min_release_age: float | None = None
    overrides: dict[str, Any] | None = None
    # Provider-level fields forwarded to every provider constructor.
    # BinProvider.__init__ warns-and-ignores install_root / bin_dir
    # when a provider subclass has no INSTALL_ROOT_FIELD / BIN_DIR_FIELD
    # set, so the CLI can pass them unconditionally.
    install_root: Path | None = None
    bin_dir: Path | None = None
    euid: int | None = None
    install_timeout: int | None = None
    version_timeout: int | None = None


_NONE_STRINGS = frozenset({"", "none", "null", "nil"})


def _none_or_stripped(raw: str | None) -> str | None:
    """Return ``raw.strip()`` unless the value is the ``None`` /
    ``'None'`` / ``'null'`` / ``'nil'`` / empty-string sentinel.

    Called from every CLI parser below as a single short-circuit so
    pyright can narrow ``raw`` past the ``None`` branch and each parser
    stays focused on its one-value-type conversion logic.
    """

    if raw is None:
        return None
    stripped = raw.strip()
    return None if stripped.lower() in _NONE_STRINGS else stripped


def _parse_min_version(raw: str | None) -> str | None:
    return _none_or_stripped(raw)


def _parse_cli_bool(raw: str | None) -> bool | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    lowered = stripped.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise click.BadParameter(f"expected a bool or 'None', got {raw!r}")


def _parse_cli_float(raw: str | None) -> float | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    try:
        return float(stripped)
    except ValueError as err:
        raise click.BadParameter(f"expected a float or 'None', got {raw!r}") from err


def _parse_cli_int(raw: str | None) -> int | None:
    """Parse an integer from a CLI flag, accepting ``"10"`` and ``"10.0"``.

    Rejects ``"10.5"`` (non-integer float) so typos don't silently
    truncate. Returns None for the ``None``/``null``/empty sentinels.
    """

    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        as_float = float(stripped)
    except ValueError as err:
        raise click.BadParameter(
            f"expected an int or 'None', got {raw!r}",
        ) from err
    as_int = int(as_float)
    if as_float != as_int:
        raise click.BadParameter(f"expected an int or 'None', got {raw!r}")
    return as_int


def _parse_overrides(raw: str | None) -> dict[str, Any] | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as err:
        raise click.BadParameter(f"--overrides must be valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise click.BadParameter("--overrides must be a JSON object")
    return data


def _parse_cli_path(raw: str | None) -> Path | None:
    stripped = _none_or_stripped(raw)
    if stripped is None:
        return None
    return Path(stripped).expanduser().resolve()


# Click ``callback=`` adapter: run the supplied parser over every raw
# click value so each option's final value is already typed (bool / int
# / float / Path / dict) by the time it reaches any command callback.
# build_cli_options, build_binary, and build_providers downstream only
# ever see typed values — no string parsing below this layer.
def _click_parse(parser: Callable[[str | None], Any]) -> Callable[..., Any]:
    return lambda _ctx, _param, value: parser(value)


def get_package_version() -> str:
    try:
        return metadata.version("abx-pkg")
    except metadata.PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with pyproject_path.open("rb") as pyproject_file:
            project = tomllib.load(pyproject_file)
        return str(project["project"]["version"])


def resolve_lib_dir(raw_value: str | Path | None) -> Path:
    lib_dir = Path(raw_value or os.environ.get("ABX_PKG_LIB_DIR") or DEFAULT_LIB_DIR)
    return lib_dir.expanduser().resolve()


def parse_provider_names(raw_value: str | None) -> list[str]:
    if raw_value is None:
        env_value = os.environ.get("ABX_PKG_BINPROVIDERS")
        if env_value is None:
            return list(ALL_PROVIDER_NAMES)
        raw_value = env_value

    provider_names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_value.split(","):
        name = raw_name.strip()
        if not name or name in seen:
            continue
        provider_names.append(name)
        seen.add(name)

    if not provider_names:
        raise click.BadParameter("expected at least one provider name")

    invalid = [name for name in provider_names if name not in PROVIDER_CLASS_BY_NAME]
    if invalid:
        valid = ", ".join(ALL_PROVIDER_NAMES)
        invalid_names = ", ".join(invalid)
        raise click.BadParameter(
            f"unknown provider name(s): {invalid_names}. Valid providers: {valid}",
        )

    return provider_names


def resolve_dry_run(flag_value: bool | None) -> bool:
    if flag_value is not None:
        return flag_value
    return env_flag_is_true("ABX_PKG_DRY_RUN") or env_flag_is_true("DRY_RUN")


def _provider_install_root(provider_name: str, lib_dir: Path) -> Path | None:
    suffix = MANAGED_PROVIDER_ROOTS.get(provider_name)
    if suffix is None:
        return None
    return lib_dir / suffix


def build_providers(
    provider_names: list[str],
    lib_dir: Path,
    *,
    dry_run: bool = False,
    install_root: Path | None = None,
    bin_dir: Path | None = None,
    euid: int | None = None,
    install_timeout: int | None = None,
    version_timeout: int | None = None,
) -> list[BinProvider]:
    providers: list[BinProvider] = []
    for provider_name in provider_names:
        provider_class = PROVIDER_CLASS_BY_NAME[provider_name]
        provider_kwargs: dict[str, Any] = {"dry_run": dry_run}
        if euid is not None:
            provider_kwargs["euid"] = euid
        if install_timeout is not None:
            provider_kwargs["install_timeout"] = install_timeout
        if version_timeout is not None:
            provider_kwargs["version_timeout"] = version_timeout
        # Use the user-supplied --install-root if given; otherwise fall
        # back to the managed ABX_PKG_LIB_DIR layout. BinProvider.__init__
        # warns-and-ignores install_root for providers whose
        # INSTALL_ROOT_FIELD is None, so the CLI can pass it blindly.
        root = (
            install_root
            if install_root is not None
            else _provider_install_root(provider_name, lib_dir)
        )
        if root is not None:
            provider_kwargs["install_root"] = root
        if bin_dir is not None:
            provider_kwargs["bin_dir"] = bin_dir
        providers.append(provider_class(**provider_kwargs))
    return providers


def build_binary(binary_name: str, options: CliOptions, *, dry_run: bool) -> Binary:
    binary_kwargs: dict[str, Any] = {
        "name": binary_name,
        "binproviders": build_providers(
            options.provider_names,
            options.lib_dir,
            dry_run=dry_run,
            install_root=options.install_root,
            bin_dir=options.bin_dir,
            euid=options.euid,
            install_timeout=options.install_timeout,
            version_timeout=options.version_timeout,
        ),
    }
    # Binary's field validators coerce str → SemVer, dict → BinaryOverrides,
    # etc., so just forward the parsed values verbatim. Binary.install /
    # load_or_install / update then propagate postinstall_scripts /
    # min_release_age to each provider's install() kwarg, where the
    # existing ``supports_postinstall_disable`` / ``supports_min_release_age``
    # warn-and-ignore path fires for providers that can't enforce them.
    for key, value in (
        ("min_version", options.min_version),
        ("postinstall_scripts", options.postinstall_scripts),
        ("min_release_age", options.min_release_age),
        ("overrides", options.overrides),
    ):
        if value is not None:
            binary_kwargs[key] = value
    return Binary(**binary_kwargs)


def build_cli_options(
    ctx: click.Context | None,
    *,
    lib_dir: str | None,
    binproviders: str | None,
    dry_run: bool | None,
    min_version: str | None,
    postinstall_scripts: bool | None,
    min_release_age: float | None,
    overrides: dict[str, Any] | None,
    install_root: Path | None,
    bin_dir: Path | None,
    euid: int | None,
    install_timeout: int | None,
    version_timeout: int | None,
) -> CliOptions:
    """Single entry-point used by the group callback and every subcommand.

    All CLI flag values arrive here already typed — click's per-option
    ``callback=`` parsers run first, so there's no string-to-bool /
    string-to-int / JSON-decode work left at this layer. Every field is
    forwarded verbatim into the returned ``CliOptions``; Binary /
    BinProvider constructors downstream honor them via the existing
    kwarg paths, and the warn-and-ignore machinery in
    ``BinProvider.__init__`` / ``BinProvider.install`` handles providers
    that can't enforce a given option.

    Subcommand-level values override the group-level values on
    ``ctx.obj['group_options']`` field-by-field; if ``ctx`` is ``None``
    (the group callback itself), values are taken as-is.
    """

    group: CliOptions | None = (
        cast(CliOptions, ctx.obj["group_options"])
        if ctx is not None and ctx.obj and "group_options" in ctx.obj
        else None
    )

    def _override(value: Any, group_value: Any) -> Any:
        """Inherit from group unless the subcommand supplied a value."""
        return group_value if value is None else value

    if group is None:
        return CliOptions(
            lib_dir=resolve_lib_dir(lib_dir),
            provider_names=parse_provider_names(binproviders),
            dry_run=resolve_dry_run(dry_run),
            min_version=min_version,
            postinstall_scripts=postinstall_scripts,
            min_release_age=min_release_age,
            overrides=overrides,
            install_root=install_root,
            bin_dir=bin_dir,
            euid=euid,
            install_timeout=install_timeout,
            version_timeout=version_timeout,
        )
    return CliOptions(
        lib_dir=group.lib_dir if lib_dir is None else resolve_lib_dir(lib_dir),
        provider_names=(
            group.provider_names
            if binproviders is None
            else parse_provider_names(binproviders)
        ),
        dry_run=_override(dry_run, group.dry_run),
        min_version=_override(min_version, group.min_version),
        postinstall_scripts=_override(postinstall_scripts, group.postinstall_scripts),
        min_release_age=_override(min_release_age, group.min_release_age),
        overrides=_override(overrides, group.overrides),
        install_root=_override(install_root, group.install_root),
        bin_dir=_override(bin_dir, group.bin_dir),
        euid=_override(euid, group.euid),
        install_timeout=_override(install_timeout, group.install_timeout),
        version_timeout=_override(version_timeout, group.version_timeout),
    )


def is_interactive_tty() -> bool:
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stderr, "isatty", lambda: False)(),
    )


def configure_cli_logging(*, dry_run: bool) -> None:
    configure_logging(
        level="DEBUG" if is_interactive_tty() else ("INFO" if dry_run else "WARNING"),
        handler=py_logging.StreamHandler(sys.stderr),
        fmt="%(message)s",
        replace_handlers=True,
    )


def format_error(err: Exception) -> str:
    if isinstance(err, BinaryOperationError) and err.errors:
        details = "\n".join(
            f"{provider_name}: {message}"
            for provider_name, message in err.errors.items()
        )
        summary = str(err).split(" ERRORS=", 1)[0]
        return f"{summary}\n{details}"
    return str(err)


def version_report(options: CliOptions) -> str:
    lines = [get_package_version()]
    for provider in build_providers(
        options.provider_names,
        options.lib_dir,
        dry_run=False,
    ):
        installer_binary = provider.INSTALLER_BINARY
        if not installer_binary or not provider.INSTALLER_BIN_ABSPATH:
            continue
        version = installer_binary.loaded_version or "unknown"
        lines.append(
            f"{provider.name} {provider.INSTALLER_BIN} {provider.INSTALLER_BIN_ABSPATH} {version}",
        )
    return "\n".join(lines)


def shared_options(command):
    # Options apply innermost-first; the --help listing order is the
    # reverse of the decoration order, so --lib / --binproviders /
    # --dry-run stay last here to preserve the pre-existing --help layout.
    # Every non-trivial option gets a ``callback=`` that runs its raw
    # string through a parser so the command receives a typed value
    # (bool / int / float / Path / dict) instead of a string.
    for decorator in (
        click.option(
            "--version-timeout",
            metavar="SECONDS",
            default=None,
            callback=_click_parse(_parse_cli_int),
            help="Seconds to wait for version/metadata probes. 'None' restores default.",
        ),
        click.option(
            "--install-timeout",
            metavar="SECONDS",
            default=None,
            callback=_click_parse(_parse_cli_int),
            help="Seconds to wait for install/update/uninstall subprocesses. 'None' restores default.",
        ),
        click.option(
            "--euid",
            metavar="UID",
            default=None,
            callback=_click_parse(_parse_cli_int),
            help="Pin the UID used when providers shell out. 'None' auto-detects.",
        ),
        click.option(
            "--bin-dir",
            metavar="PATH",
            default=None,
            callback=_click_parse(_parse_cli_path),
            help="Override the per-provider bin directory (providers without BIN_DIR_FIELD warn and ignore). 'None' restores defaults.",
        ),
        click.option(
            "--install-root",
            metavar="PATH",
            default=None,
            callback=_click_parse(_parse_cli_path),
            help="Override the per-provider install directory (providers without INSTALL_ROOT_FIELD warn and ignore). 'None' restores defaults.",
        ),
        click.option(
            "--overrides",
            metavar="JSON",
            default=None,
            callback=_click_parse(_parse_overrides),
            help='JSON-encoded Binary.overrides dict, e.g. \'{"pip":{"install_args":["pkg"]}}\'. \'None\' restores defaults.',
        ),
        click.option(
            "--min-release-age",
            metavar="DAYS",
            default=None,
            callback=_click_parse(_parse_cli_float),
            help="Minimum days since publication. Providers that can't enforce it warn and ignore. 'None' restores defaults.",
        ),
        click.option(
            "--postinstall-scripts",
            metavar="BOOL",
            default=None,
            callback=_click_parse(_parse_cli_bool),
            help="Allow post-install scripts ('True'/'False'/'1'/'0'/'None' or bare `--postinstall-scripts` for implicit True). Providers that can't disable them warn and ignore.",
        ),
        click.option(
            "--min-version",
            metavar="SEMVER",
            default=None,
            callback=_click_parse(_parse_min_version),
            help="Minimum acceptable version floor for the binary. 'None' means any version is acceptable.",
        ),
        click.option(
            "--dry-run",
            metavar="BOOL",
            default=None,
            callback=_click_parse(_parse_cli_bool),
            help="Show installer commands without executing them ('True'/'False'/'None' or bare `--dry-run` for implicit True).",
        ),
        click.option(
            "--binproviders",
            metavar="LIST",
            default=None,
            help="Comma-separated provider order. Defaults to ABX_PKG_BINPROVIDERS or all providers.",
        ),
        click.option(
            "--lib",
            "lib_dir",
            metavar="PATH",
            default=None,
            help="Base library directory. Defaults to ABX_PKG_LIB_DIR or ~/.config/abx/lib.",
        ),
    ):
        command = decorator(command)
    return command


# Single canonical list of kwargs carried by every CLI callback that
# uses @shared_options. Defined once so command callbacks don't have to
# enumerate all of them, and so ``get_command_options`` has a single
# source of truth for what gets forwarded to ``build_cli_options``.
_SHARED_OPTION_NAMES: tuple[str, ...] = (
    "lib_dir",
    "binproviders",
    "dry_run",
    "min_version",
    "postinstall_scripts",
    "min_release_age",
    "overrides",
    "install_root",
    "bin_dir",
    "euid",
    "install_timeout",
    "version_timeout",
)


def get_command_options(
    ctx: click.Context,
    **shared_kwargs: Any,
) -> CliOptions:
    return build_cli_options(ctx, **shared_kwargs)


def run_binary_command(
    binary_name: str,
    *,
    action: str,
    options: CliOptions,
) -> None:
    binary = build_binary(binary_name, options, dry_run=options.dry_run)
    method = getattr(binary, action)
    configure_cli_logging(dry_run=options.dry_run)

    try:
        if action == "load":
            result = method()
        else:
            result = method(dry_run=options.dry_run)
    except ABXPkgError as err:
        raise click.ClickException(format_error(err)) from err

    if options.dry_run and action != "load":
        return

    if action == "uninstall":
        click.echo(binary_name)
        return

    provider = result.loaded_binprovider
    provider_name = provider.name if provider is not None else "unknown"
    click.echo(f"{result.loaded_abspath} {result.loaded_version} {provider_name}")


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.pass_context
@shared_options
@click.option(
    "--install",
    "install_before_run",
    is_flag=True,
    default=False,
    help="Only used by `run`: load_or_install the binary before executing it.",
)
@click.option(
    "--update",
    "update_before_run",
    is_flag=True,
    default=False,
    help="Only used by `run`: load_or_install and update the binary before executing it.",
)
@click.option(
    "--version",
    "show_version",
    is_flag=True,
    default=False,
    help="Show the abx-pkg version and available installer binaries.",
)
def cli(
    ctx: click.Context,
    install_before_run: bool,
    update_before_run: bool,
    show_version: bool,
    **shared_kwargs: Any,
) -> None:
    """Manage binaries via abx-pkg binproviders."""

    ctx.ensure_object(dict)
    options = build_cli_options(None, **shared_kwargs)
    ctx.obj["group_options"] = options
    ctx.obj["install_before_run"] = install_before_run
    ctx.obj["update_before_run"] = update_before_run

    if show_version:
        click.echo(version_report(options))
        ctx.exit()

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("version")
@click.pass_context
@shared_options
def version_command(ctx: click.Context, **shared_kwargs: Any) -> None:
    """Show the package version and available installer binaries."""

    options = get_command_options(ctx, **shared_kwargs)
    click.echo(version_report(options))


@cli.command("install")
@click.argument("binary_name")
@click.pass_context
@shared_options
def install_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    """Install a binary via the selected providers in order."""

    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="install", options=options)


@cli.command("update")
@click.argument("binary_name")
@click.pass_context
@shared_options
def update_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    """Update a binary via the selected providers in order."""

    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="update", options=options)


@cli.command("uninstall")
@click.argument("binary_name")
@click.pass_context
@shared_options
def uninstall_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    """Uninstall a binary via the selected providers in order."""

    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="uninstall", options=options)


@cli.command("load")
@click.argument("binary_name")
@click.pass_context
@shared_options
def load_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    """Load an already-installed binary via the selected providers in order."""

    options = get_command_options(ctx, **shared_kwargs)
    # Load never installs, so force dry_run off regardless of what the
    # user passed; the other option fields are preserved so min_version
    # etc. still apply.
    options = replace(options, dry_run=False)
    run_binary_command(binary_name, action="load", options=options)


@cli.command("load_or_install")
@click.argument("binary_name")
@click.pass_context
@shared_options
def load_or_install_command(
    ctx: click.Context,
    binary_name: str,
    **shared_kwargs: Any,
) -> None:
    """Load a binary or install it via the selected providers in order."""

    options = get_command_options(ctx, **shared_kwargs)
    run_binary_command(binary_name, action="load_or_install", options=options)


cli.add_command(load_or_install_command, "load-or-install")


@cli.command(
    "run",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": [],
    },
)
@click.argument("binary_name")
@click.argument("binary_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run_command(
    ctx: click.Context,
    binary_name: str,
    binary_args: tuple[str, ...],
) -> None:
    """Run an installed binary, passing all remaining arguments through to it.

    Options to abx-pkg itself (e.g. --binproviders, --lib, --install, --update)
    must appear BEFORE the `run` subcommand name. Everything after the binary
    name is forwarded verbatim to the underlying binary's argv.
    """

    group_options = cast(CliOptions, ctx.obj["group_options"])
    install_before_run = bool(ctx.obj.get("install_before_run", False))
    update_before_run = bool(ctx.obj.get("update_before_run", False))

    configure_cli_logging(dry_run=group_options.dry_run)

    binary = build_binary(
        binary_name,
        group_options,
        dry_run=group_options.dry_run,
    )

    try:
        if update_before_run:
            binary = binary.load_or_install(dry_run=group_options.dry_run)
            binary = binary.update(dry_run=group_options.dry_run)
        elif install_before_run:
            binary = binary.load_or_install(dry_run=group_options.dry_run)
        else:
            binary = binary.load()
    except ABXPkgError as err:
        click.echo(format_error(err), err=True)
        ctx.exit(1)
        return

    if group_options.dry_run:
        # Provider exec honors dry_run and returns a no-op CompletedProcess;
        # keep the behavior consistent here so nothing is actually run.
        ctx.exit(0)
        return

    if not binary.is_valid:
        click.echo(
            f"abx-pkg: {binary_name}: binary could not be loaded",
            err=True,
        )
        ctx.exit(1)
        return

    # binary.is_valid guarantees both fields are set; narrow for pyright.
    assert binary.loaded_binprovider is not None
    assert binary.loaded_abspath is not None
    proc = binary.loaded_binprovider.exec(
        bin_name=binary.loaded_abspath,
        cmd=list(binary_args),
        capture_output=False,
    )
    ctx.exit(proc.returncode)


# Bool flags that should auto-set to True when passed bare (e.g. `--dry-run`
# with no ``=VALUE``). Pre-processing in main() / abx_main() rewrites bare
# occurrences to ``--flag=True`` so a single click string option can handle
# both the bare and the value form. Callers pass ``--dry-run=False`` or
# ``--dry-run=None`` to override the auto-True semantics.
_BARE_TRUE_BOOL_FLAGS = frozenset({"--dry-run", "--postinstall-scripts"})


def _expand_bare_bool_flags(argv: list[str]) -> list[str]:
    """Translate bare bool flags (``--dry-run``) into their value form
    (``--dry-run=True``) so click's plain string option can parse both."""

    return [f"{tok}=True" if tok in _BARE_TRUE_BOOL_FLAGS else tok for tok in argv]


def main() -> None:
    cli(_expand_bare_bool_flags(sys.argv[1:]))


# ---------------------------------------------------------------------------
# `abx` — thin alias for `abx-pkg --install run ...`
# ---------------------------------------------------------------------------

# Group-level options that consume a following value (e.g. `--lib PATH`,
# `--binproviders LIST`). Derived at import time by introspecting the
# click group's own option definitions — no hardcoding — so any option
# added later via @shared_options automatically joins this set. Used by
# _split_abx_argv to know when to pull an extra token into the
# "pre-package-name" prefix; options written as `--name=value` never hit
# this code path (they're handled by the `"=" in tok` branch).
_ABX_PKG_GROUP_OPTS_WITH_VALUES = frozenset(
    opt
    for param in cli.params
    if isinstance(param, click.Option) and not param.is_flag
    for opt in param.opts
    if opt.startswith("--")
)

_ABX_USAGE = (
    "Usage: abx [OPTIONS] BINARY_NAME [BINARY_ARGS]...\n"
    "\n"
    "Install (if needed) and run a package-managed binary.\n"
    "Equivalent to `abx-pkg [OPTIONS] --install run BINARY_NAME [BINARY_ARGS]`.\n"
    "\n"
    "Options (forwarded to abx-pkg): --lib, --binproviders, --dry-run,\n"
    "--update, --version, --help.\n"
)


def _split_abx_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split ``argv`` around the first positional (binary name) token.

    Everything up to (and not including) the first non-option token is
    treated as `abx-pkg` group options and returned as ``pre``. The binary
    name and all following tokens are returned verbatim as ``rest``.

    Options that take a separate value (`--lib PATH`, `--binproviders LIST`)
    are handled so the value token is kept with its option instead of being
    mistaken for the binary name.
    """
    pre: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            # POSIX option terminator: everything after `--` is the binary
            # name and its arguments, never abx-pkg options. Consume the
            # `--` so we don't end up injecting `--install run` *after* a
            # stray `--` that would otherwise force click to treat every
            # following token as a positional group argument.
            return pre, argv[i + 1 :]
        if tok.startswith("--") and "=" in tok:
            pre.append(tok)
            i += 1
            continue
        if tok in _ABX_PKG_GROUP_OPTS_WITH_VALUES:
            pre.append(tok)
            if i + 1 < len(argv):
                pre.append(argv[i + 1])
                i += 2
            else:
                i += 1
            continue
        if tok.startswith("-") and tok != "-":
            pre.append(tok)
            i += 1
            continue
        # First non-option token: this is the binary name.
        return pre, argv[i:]
    return pre, []


def abx_main() -> None:
    """Console-script entrypoint for the thin ``abx`` alias.

    Rewrites ``abx [OPTS] BINARY [ARGS]`` into
    ``abx-pkg [OPTS] --install run BINARY [ARGS]`` and hands it off to the
    existing click group. Keeps us from redefining any of the rich-click
    surface area — every option is still documented and parsed exactly
    once, by ``abx-pkg`` itself.
    """
    argv = _expand_bare_bool_flags(list(sys.argv[1:]))
    pre, rest = _split_abx_argv(argv)

    if not rest:
        # No binary name given. Forward info-only flags so `abx --version`
        # and `abx --help` still do something useful; otherwise print our
        # own usage to stderr and exit 2 like click would.
        if any(flag in pre for flag in ("--help", "-h", "--version")):
            cli(pre)
            return
        click.echo(_ABX_USAGE, err=True)
        sys.exit(2)

    # --update already implies load_or_install, so adding --install alongside
    # it is a no-op; always injecting --install keeps this wrapper stateless.
    cli([*pre, "--install", "run", *rest])


__all__ = [
    "CliOptions",
    "abx_main",
    "build_binary",
    "build_providers",
    "cli",
    "get_package_version",
    "is_interactive_tty",
    "main",
    "parse_provider_names",
    "resolve_dry_run",
    "resolve_lib_dir",
    "version_report",
]
