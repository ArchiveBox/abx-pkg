from __future__ import annotations

import logging as py_logging
import os
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
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
) -> list[BinProvider]:
    providers: list[BinProvider] = []
    for provider_name in provider_names:
        provider_class = PROVIDER_CLASS_BY_NAME[provider_name]
        provider_kwargs: dict[str, Any] = {"dry_run": dry_run}
        install_root = _provider_install_root(provider_name, lib_dir)
        if install_root is not None:
            provider_kwargs["install_root"] = install_root
        providers.append(provider_class(**provider_kwargs))
    return providers


def build_binary(binary_name: str, options: CliOptions, *, dry_run: bool) -> Binary:
    return Binary(
        name=binary_name,
        binproviders=build_providers(
            options.provider_names,
            options.lib_dir,
            dry_run=dry_run,
        ),
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
    command = click.option(
        "--dry-run/--no-dry-run",
        default=None,
        help="Show installer commands without executing them.",
    )(command)
    command = click.option(
        "--binproviders",
        metavar="LIST",
        default=None,
        help="Comma-separated provider order. Defaults to ABX_PKG_BINPROVIDERS or all providers.",
    )(command)
    command = click.option(
        "--lib",
        "lib_dir",
        metavar="PATH",
        default=None,
        help="Base library directory. Defaults to ABX_PKG_LIB_DIR or ~/.config/abx/lib.",
    )(command)
    return command


def get_command_options(
    ctx: click.Context,
    *,
    lib_dir: str | None,
    binproviders: str | None,
    dry_run: bool | None,
) -> CliOptions:
    group_options = cast(CliOptions, ctx.obj["group_options"])
    return CliOptions(
        lib_dir=group_options.lib_dir if lib_dir is None else resolve_lib_dir(lib_dir),
        provider_names=(
            group_options.provider_names
            if binproviders is None
            else parse_provider_names(binproviders)
        ),
        dry_run=group_options.dry_run if dry_run is None else dry_run,
    )


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
    lib_dir: str | None,
    binproviders: str | None,
    dry_run: bool | None,
    install_before_run: bool,
    update_before_run: bool,
    show_version: bool,
) -> None:
    """Manage binaries via abx-pkg binproviders."""

    options = CliOptions(
        lib_dir=resolve_lib_dir(lib_dir),
        provider_names=parse_provider_names(binproviders),
        dry_run=resolve_dry_run(dry_run),
    )
    ctx.ensure_object(dict)
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
def version_command(
    ctx: click.Context,
    lib_dir: str | None,
    binproviders: str | None,
    dry_run: bool | None,
) -> None:
    """Show the package version and available installer binaries."""

    options = get_command_options(
        ctx,
        lib_dir=lib_dir,
        binproviders=binproviders,
        dry_run=dry_run,
    )
    click.echo(version_report(options))


@cli.command("install")
@click.argument("binary_name")
@click.pass_context
@shared_options
def install_command(
    ctx: click.Context,
    binary_name: str,
    lib_dir: str | None,
    binproviders: str | None,
    dry_run: bool | None,
) -> None:
    """Install a binary via the selected providers in order."""

    options = get_command_options(
        ctx,
        lib_dir=lib_dir,
        binproviders=binproviders,
        dry_run=dry_run,
    )
    run_binary_command(binary_name, action="install", options=options)


@cli.command("update")
@click.argument("binary_name")
@click.pass_context
@shared_options
def update_command(
    ctx: click.Context,
    binary_name: str,
    lib_dir: str | None,
    binproviders: str | None,
    dry_run: bool | None,
) -> None:
    """Update a binary via the selected providers in order."""

    options = get_command_options(
        ctx,
        lib_dir=lib_dir,
        binproviders=binproviders,
        dry_run=dry_run,
    )
    run_binary_command(binary_name, action="update", options=options)


@cli.command("uninstall")
@click.argument("binary_name")
@click.pass_context
@shared_options
def uninstall_command(
    ctx: click.Context,
    binary_name: str,
    lib_dir: str | None,
    binproviders: str | None,
    dry_run: bool | None,
) -> None:
    """Uninstall a binary via the selected providers in order."""

    options = get_command_options(
        ctx,
        lib_dir=lib_dir,
        binproviders=binproviders,
        dry_run=dry_run,
    )
    run_binary_command(binary_name, action="uninstall", options=options)


@cli.command("load")
@click.argument("binary_name")
@click.pass_context
@shared_options
def load_command(
    ctx: click.Context,
    binary_name: str,
    lib_dir: str | None,
    binproviders: str | None,
    dry_run: bool | None,
) -> None:
    """Load an already-installed binary via the selected providers in order."""

    options = get_command_options(
        ctx,
        lib_dir=lib_dir,
        binproviders=binproviders,
        dry_run=dry_run,
    )
    run_binary_command(
        binary_name,
        action="load",
        options=CliOptions(
            lib_dir=options.lib_dir,
            provider_names=options.provider_names,
            dry_run=False,
        ),
    )


@cli.command("load_or_install")
@click.argument("binary_name")
@click.pass_context
@shared_options
def load_or_install_command(
    ctx: click.Context,
    binary_name: str,
    lib_dir: str | None,
    binproviders: str | None,
    dry_run: bool | None,
) -> None:
    """Load a binary or install it via the selected providers in order."""

    options = get_command_options(
        ctx,
        lib_dir=lib_dir,
        binproviders=binproviders,
        dry_run=dry_run,
    )
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

    if binary.loaded_abspath is None or binary.loaded_binprovider is None:
        click.echo(
            f"abx-pkg: {binary_name}: binary could not be loaded",
            err=True,
        )
        ctx.exit(1)
        return

    proc = binary.loaded_binprovider.exec(
        bin_name=binary.loaded_abspath,
        cmd=list(binary_args),
        capture_output=False,
    )
    ctx.exit(proc.returncode)


def main() -> None:
    cli()


# ---------------------------------------------------------------------------
# `abx` — thin alias for `abx-pkg --install run ...`
# ---------------------------------------------------------------------------

# Group-level options of the `abx-pkg` CLI that consume a following value
# (e.g. `--lib PATH`, `--binproviders LIST`). Used by _split_abx_argv to
# know when to pull an extra token into the "pre-package-name" prefix.
# Options using the `--name=value` form don't need to be listed here.
_ABX_PKG_GROUP_OPTS_WITH_VALUES = frozenset({"--lib", "--binproviders"})

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
    argv = list(sys.argv[1:])
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
