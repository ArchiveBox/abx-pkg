from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import rich_click as click
from click.testing import CliRunner

from abx_pkg import SemVer
import abx_pkg.cli as cli_module


def _abx_pkg_executable() -> Path:
    """Locate the installed abx-pkg console script for subprocess-based tests."""

    candidate = Path(sys.executable).parent / "abx-pkg"
    if candidate.exists():
        return candidate
    resolved = shutil.which("abx-pkg")
    assert resolved, "abx-pkg console script must be installed in the active venv"
    return Path(resolved)


def _abx_executable() -> Path:
    """Locate the installed `abx` console script for subprocess-based tests."""

    candidate = Path(sys.executable).parent / "abx"
    if candidate.exists():
        return candidate
    resolved = shutil.which("abx")
    assert resolved, "abx console script must be installed in the active venv"
    return Path(resolved)


def _run_cli(
    script: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    """Invoke a console script with a clean ABX_PKG_* environment."""

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ABX_PKG_")
    }
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        [str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _run_abx_pkg_cli(
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real `abx-pkg` console script with a clean env."""

    return _run_cli(
        _abx_pkg_executable(),
        *args,
        env_overrides=env_overrides,
        timeout=timeout,
    )


def _run_abx_cli(
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real `abx` console script with a clean env."""

    return _run_cli(
        _abx_executable(),
        *args,
        env_overrides=env_overrides,
        timeout=timeout,
    )


class FakeBinary:
    loaded_abspath = Path("/tmp/fake-bin")
    loaded_version = SemVer("1.2.3")
    loaded_binprovider = SimpleNamespace(name="pnpm")

    def __init__(self):
        self.calls: list[tuple[str, bool | None]] = []

    def install(self, dry_run=None):
        self.calls.append(("install", dry_run))
        return self


class FakeProvider:
    def __init__(self, name: str, abspath: str | None, version: str | None):
        self.name = name
        self.INSTALLER_BIN = name
        self.INSTALLER_BIN_ABSPATH = Path(abspath) if abspath else None
        self.INSTALLER_BINARY = (
            SimpleNamespace(loaded_version=SemVer(version))
            if abspath and version
            else None
        )


def test_build_providers_uses_managed_lib_layout(tmp_path):
    providers = cli_module.build_providers(
        ["uv", "pip", "pnpm", "cargo", "env"],
        tmp_path,
        dry_run=True,
    )

    assert providers[0].install_root == tmp_path / "uv" / "venv"
    assert providers[1].install_root == tmp_path / "pip" / "venv"
    assert providers[2].install_root == tmp_path / "pnpm"
    assert providers[3].install_root == tmp_path / "cargo"
    assert providers[4].name == "env"
    assert all(provider.dry_run for provider in providers)


def test_install_command_uses_env_defaults(monkeypatch, tmp_path):
    captured = {}
    fake_binary = FakeBinary()

    def fake_build_binary(binary_name, options, *, dry_run):
        captured["binary_name"] = binary_name
        captured["options"] = options
        captured["provider_dry_run"] = dry_run
        return fake_binary

    monkeypatch.setattr(cli_module, "build_binary", fake_build_binary)

    result = CliRunner().invoke(
        cli_module.cli,
        ["install", "prettier"],
        env={
            "ABX_PKG_LIB_DIR": str(tmp_path),
            "ABX_PKG_BINPROVIDERS": "pnpm,uv",
            "ABX_PKG_DRY_RUN": "1",
        },
    )

    assert result.exit_code == 0
    assert captured["binary_name"] == "prettier"
    assert captured["options"].lib_dir == tmp_path.resolve()
    assert captured["options"].provider_names == ["pnpm", "uv"]
    assert captured["options"].dry_run is True
    assert captured["provider_dry_run"] is True
    assert fake_binary.calls == [("install", True)]


def test_version_flag_and_command_render_same_report(monkeypatch, tmp_path):
    fake_providers = [
        FakeProvider("env", "/usr/bin/which", "2.0.0"),
        FakeProvider("uv", "/opt/homebrew/bin/uv", "0.7.1"),
        FakeProvider("apt", None, None),
    ]

    monkeypatch.setattr(
        cli_module,
        "build_providers",
        lambda *args, **kwargs: fake_providers,
    )

    runner = CliRunner()
    flag_result = runner.invoke(
        cli_module.cli,
        ["--version", f"--lib={tmp_path}", "--binproviders=env,uv,apt"],
    )
    command_result = runner.invoke(
        cli_module.cli,
        ["version", f"--lib={tmp_path}", "--binproviders=env,uv,apt"],
    )

    assert flag_result.exit_code == 0
    assert command_result.exit_code == 0
    assert flag_result.output == command_result.output

    lines = flag_result.output.strip().splitlines()
    assert lines[0] == cli_module.get_package_version()
    assert lines[1] == "env env /usr/bin/which 2.0.0"
    assert lines[2] == "uv uv /opt/homebrew/bin/uv 0.7.1"


def test_configure_cli_logging_uses_debug_level_for_interactive_tty(monkeypatch):
    captured = {}

    monkeypatch.setattr(cli_module, "is_interactive_tty", lambda: True)

    def fake_configure_logging(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_module, "configure_logging", fake_configure_logging)

    cli_module.configure_cli_logging(dry_run=False)

    assert captured["level"] == "DEBUG"
    assert captured["handler"].stream is cli_module.sys.stderr
    assert captured["fmt"] == "%(message)s"
    assert captured["replace_handlers"] is True


# ---------------------------------------------------------------------------
# `abx-pkg run` subcommand (real live subprocess-based tests)
# ---------------------------------------------------------------------------


def test_run_executes_preinstalled_binary_via_env_provider():
    """`abx-pkg run` with an already-installed binary should stream its output.

    Uses ``python3`` rather than ``ls`` because BSD ``ls`` (macOS) does
    not support ``--version`` / ``-version`` / ``-v``, so the env
    provider can't ``load()`` it.
    """

    proc = _run_abx_pkg_cli(
        "--binproviders=env",
        "run",
        "python3",
        "-c",
        "print('abx-run-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abx-run-ok"
    assert proc.stderr == ""


def test_run_passes_flag_args_through_without_requiring_dash_dash():
    """Flags after `run BINARY_NAME` must reach the binary, not click.

    Uses ``python3 --version`` instead of ``ls --help`` because macOS ships
    BSD ``ls``, which does not understand ``--help`` and exits non-zero.
    """

    proc = _run_abx_pkg_cli("--binproviders=env", "run", "python3", "--version")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("Python "), proc.stdout
    assert proc.stderr == ""


def test_run_propagates_nonzero_exit_code_from_underlying_binary():
    """Exit codes from the underlying binary must flow back unchanged."""

    proc = _run_abx_pkg_cli(
        "--binproviders=env",
        "run",
        "python3",
        "-c",
        "import sys; sys.stderr.write('boom\\n'); sys.exit(7)",
    )

    assert proc.returncode == 7
    assert proc.stdout == ""
    assert "boom" in proc.stderr


def test_run_stdout_stderr_are_separated_and_not_buffered(tmp_path):
    """stdout and stderr from the underlying binary must stream separately."""

    # Drop a tiny shim script into a fresh PATH directory that the env
    # provider will pick up. The script must respond to --version so
    # EnvProvider can .load() it, then return a non-zero exit code with
    # output split across stdout/stderr.
    script = tmp_path / "abx-pkg-run-shim"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        '  echo "abx-pkg-run-shim 1.2.3"\n'
        "  exit 0\n"
        "fi\n"
        "echo 'this goes to stdout'\n"
        "echo 'this goes to stderr' >&2\n"
        "exit 7\n",
    )
    script.chmod(0o755)

    # Use an ad-hoc PATH that exposes the custom script as a "binary".
    proc = _run_abx_pkg_cli(
        "--binproviders=env",
        "run",
        script.name,
        env_overrides={"PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )

    assert proc.returncode == 7, proc.stderr
    assert proc.stdout == "this goes to stdout\n"
    assert "this goes to stderr" in proc.stderr
    # Nothing from abx-pkg itself should leak into stdout.
    assert "abx-pkg" not in proc.stdout.lower()


def test_run_without_install_exits_one_when_binary_is_missing():
    """If the binary is not installed by any provider, we exit 1."""

    proc = _run_abx_pkg_cli(
        "--binproviders=env",
        "run",
        "abx-pkg-test-definitely-not-installed-xyz",
        "--help",
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "abx-pkg-test-definitely-not-installed-xyz" in proc.stderr


def test_run_respects_abx_pkg_binproviders_env_var():
    """The ABX_PKG_BINPROVIDERS env var should restrict provider resolution."""

    proc = _run_abx_pkg_cli(
        "run",
        "python3",
        "-c",
        "print('from env var')",
        env_overrides={"ABX_PKG_BINPROVIDERS": "env"},
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "from env var"


def test_run_binproviders_flag_overrides_env_var():
    """`--binproviders` on the command line wins over ABX_PKG_BINPROVIDERS."""

    proc = _run_abx_pkg_cli(
        "--binproviders=env",
        "run",
        "python3",
        "-c",
        "print('flag wins')",
        env_overrides={"ABX_PKG_BINPROVIDERS": "pip,brew"},
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "flag wins"


def test_run_with_install_flag_installs_binary_before_executing(tmp_path):
    """`--install` should load_or_install via selected providers, then exec."""

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--install",
        "run",
        "black",
        "--version",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    # stdout must contain *only* black's --version output
    assert proc.stdout.strip().startswith("black")
    # The binary must have actually been installed under our isolated lib dir.
    installed = list((tmp_path / "pip" / "venv").rglob("black"))
    assert installed, (
        f"Expected black to be installed under {tmp_path}/pip/venv, "
        f"found nothing. stderr was:\n{proc.stderr}"
    )


def test_run_with_update_flag_installs_and_updates_before_executing(tmp_path):
    """`--update` should load_or_install + update via selected providers."""

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--update",
        "run",
        "black",
        "--version",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("black")
    installed = list((tmp_path / "pip" / "venv").rglob("black"))
    assert installed


def test_run_with_install_keeps_install_logs_off_stdout(tmp_path):
    """Install progress logs must go to stderr, stdout stays clean."""

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--install",
        "run",
        "black",
        "--version",
        timeout=900,
        # Force a deterministic, non-TTY log level so we can assert on it.
        env_overrides={
            "ABX_PKG_LIB_DIR": str(tmp_path),
            "ABX_PKG_BINPROVIDERS": "pip",
        },
    )

    assert proc.returncode == 0, proc.stderr
    # stdout must be *only* the black --version output, nothing abx-pkg-ish.
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines
    assert stdout_lines[0].startswith("black"), stdout_lines
    for line in stdout_lines:
        assert "Installing" not in line
        assert "Loading" not in line
        assert "Binary.load" not in line


def test_run_pip_subcommand_uses_pip_provider_exec(tmp_path):
    """`abx-pkg --binproviders=pip run pip show X` exercises PipProvider.exec."""

    # Prime a fresh pip venv so we control what's inside.
    install_proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "install",
        "black",
        timeout=900,
    )
    assert install_proc.returncode == 0, install_proc.stderr

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "run",
        "pip",
        "show",
        "black",
        timeout=300,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Name: black" in proc.stdout
    # Ensure the pip that ran was from our isolated venv, not the system pip:
    # pip show always prints a `Location:` line, so we must verify it points
    # *inside* the tmp_path rather than just that the header is present.
    location_lines = [
        line for line in proc.stdout.splitlines() if line.startswith("Location:")
    ]
    assert location_lines, (
        f"pip show did not emit a Location line; stdout was:\n{proc.stdout}"
    )
    assert str(tmp_path) in location_lines[0], (
        f"pip show reported {location_lines[0]!r}, which is outside the "
        f"isolated venv under {tmp_path}. The `run` subcommand probably "
        f"exec'd the system pip instead of the PipProvider's pip."
    )


@pytest.mark.parametrize(
    ("extra_args", "expected_exit", "expected_stdout"),
    [
        (("-c", "print('zero')"), 0, "zero"),
        (
            ("-c", "print('one'); import sys; sys.exit(0)"),
            0,
            "one",
        ),
        (
            ("-c", "import sys; sys.exit(3)"),
            3,
            "",
        ),
    ],
)
def test_run_forwards_variadic_positional_args_to_binary(
    extra_args,
    expected_exit,
    expected_stdout,
):
    proc = _run_abx_pkg_cli(
        "--binproviders=env",
        "run",
        "python3",
        *extra_args,
    )

    assert proc.returncode == expected_exit, proc.stderr
    assert proc.stdout.strip() == expected_stdout


# ---------------------------------------------------------------------------
# `abx` — thin alias for `abx-pkg --install run ...` (argv-rewriting wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_pre", "expected_rest"),
    [
        (["yt-dlp", "--help"], [], ["yt-dlp", "--help"]),
        (["--update", "yt-dlp"], ["--update"], ["yt-dlp"]),
        (
            ["--binproviders=env,uv,pip,apt,brew", "yt-dlp"],
            ["--binproviders=env,uv,pip,apt,brew"],
            ["yt-dlp"],
        ),
        (
            ["--lib", "/tmp/abx-lib", "--dry-run", "yt-dlp", "--help"],
            ["--lib", "/tmp/abx-lib", "--dry-run"],
            ["yt-dlp", "--help"],
        ),
        (
            ["--binproviders", "pip,brew", "black", "-v"],
            ["--binproviders", "pip,brew"],
            ["black", "-v"],
        ),
        (["--version"], ["--version"], []),
        ([], [], []),
        # POSIX `--` option terminator: the `--` itself is consumed and
        # everything after it is treated as the binary name + its args,
        # regardless of whether the first token looks like an option.
        (["--", "yt-dlp", "--help"], [], ["yt-dlp", "--help"]),
        (
            ["--update", "--", "--weird-binary-name", "--help"],
            ["--update"],
            ["--weird-binary-name", "--help"],
        ),
        (
            ["--binproviders=env", "--", "python3", "--version"],
            ["--binproviders=env"],
            ["python3", "--version"],
        ),
        # `--` *after* the binary name is part of the binary's argv and
        # must be forwarded verbatim (not consumed by the splitter).
        (
            ["yt-dlp", "--", "-x"],
            [],
            ["yt-dlp", "--", "-x"],
        ),
    ],
)
def test_split_abx_argv_splits_options_from_binary(argv, expected_pre, expected_rest):
    pre, rest = cli_module._split_abx_argv(argv)
    assert pre == expected_pre
    assert rest == expected_rest


def test_abx_accepts_dash_dash_option_terminator_before_binary():
    """`abx --binproviders=env -- python3 --version` must still work."""

    proc = _run_abx_cli(
        "--binproviders=env",
        "--",
        "python3",
        "--version",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("Python "), proc.stdout


def test_abx_auto_installs_and_runs_preinstalled_env_binary():
    """`abx BIN` on an already-present binary resolves it and execs it."""

    proc = _run_abx_cli(
        "--binproviders=env",
        "python3",
        "-c",
        "print('abx-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abx-ok"


def test_abx_passes_flag_args_through_to_underlying_binary():
    """Flags after the binary name must reach the binary, not abx-pkg.

    Uses ``python3 --version`` because macOS ships BSD ``ls`` which does
    not recognise ``--help``.
    """

    proc = _run_abx_cli("--binproviders=env", "python3", "--version")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("Python "), proc.stdout
    assert proc.stderr == ""


def test_abx_propagates_underlying_exit_code():
    proc = _run_abx_cli(
        "--binproviders=env",
        "python3",
        "-c",
        "import sys; sys.stderr.write('kaboom\\n'); sys.exit(5)",
    )

    assert proc.returncode == 5
    assert proc.stdout == ""
    assert "kaboom" in proc.stderr


def test_abx_respects_binproviders_flag_before_binary_name():
    """`abx --binproviders=LIST BIN ARGS` must forward LIST to abx-pkg."""

    proc = _run_abx_cli(
        "--binproviders=env,uv,pip,apt,brew",
        "python3",
        "-c",
        "print('abx-binproviders-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abx-binproviders-ok"


def test_abx_version_flag_is_forwarded_without_running_a_binary():
    proc = _run_abx_cli("--version")

    assert proc.returncode == 0, proc.stderr
    from abx_pkg.cli import get_package_version

    assert get_package_version() in proc.stdout


def test_abx_without_any_args_prints_usage_and_exits_two():
    proc = _run_abx_cli()

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "Usage: abx" in proc.stderr
    assert "--install run" in proc.stderr


def test_abx_installs_missing_binary_via_selected_provider(tmp_path):
    """Auto-install behaviour: `abx` installs into the isolated lib dir."""

    proc = _run_abx_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "black",
        "--version",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    # stdout must be *only* black --version output, not abx-pkg's install logs.
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines
    assert stdout_lines[0].startswith("black"), stdout_lines
    for line in stdout_lines:
        assert "Installing" not in line
        assert "Loading" not in line
    # Ensure black was actually installed under the isolated lib dir.
    installed = list((tmp_path / "pip" / "venv").rglob("black"))
    assert installed, (
        f"Expected black to be installed under {tmp_path}/pip/venv. "
        f"stderr was:\n{proc.stderr}"
    )


def test_abx_update_flag_is_forwarded_and_runs_after_update(tmp_path):
    """`abx --update BIN ARGS` must trigger load_or_install+update then exec."""

    proc = _run_abx_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--update",
        "black",
        "--version",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("black")
    installed = list((tmp_path / "pip" / "venv").rglob("black"))
    assert installed


# ---------------------------------------------------------------------------
# Full Binary/BinProvider option surface (--min-version, --postinstall-scripts,
# --min-release-age, --overrides, --install-root, --bin-dir, --euid,
# --install-timeout, --version-timeout) wired through shared_options.
# ---------------------------------------------------------------------------


def test_build_cli_options_passes_typed_values_through(tmp_path):
    """build_cli_options is called *after* click callbacks have parsed
    every raw string, so it only ever sees typed values — no parsing
    happens at this layer. Every field should land verbatim on CliOptions."""

    options = cli_module.build_cli_options(
        None,
        lib_dir=str(tmp_path),
        binproviders="env,pip",
        dry_run=True,
        min_version="1.2.3",
        postinstall_scripts=False,
        min_release_age=14.0,
        overrides={"pip": {"install_args": ["black==24.2.0"]}},
        install_root=tmp_path / "custom-root",
        bin_dir=tmp_path / "custom-bin",
        euid=1000,
        install_timeout=300,
        version_timeout=25,
    )

    assert options.lib_dir == tmp_path.resolve()
    assert options.provider_names == ["env", "pip"]
    assert options.dry_run is True
    assert options.min_version == "1.2.3"
    assert options.postinstall_scripts is False
    assert options.min_release_age == 14.0
    assert options.overrides == {"pip": {"install_args": ["black==24.2.0"]}}
    assert options.install_root == tmp_path / "custom-root"
    assert options.bin_dir == tmp_path / "custom-bin"
    assert options.euid == 1000
    assert options.install_timeout == 300
    assert options.version_timeout == 25


def test_build_cli_options_nones_all_leave_fields_at_default(tmp_path):
    """Passing None for every typed value should leave CliOptions at its
    dataclass defaults (i.e. None, with dry_run resolving via env-var fallback)."""

    options = cli_module.build_cli_options(
        None,
        lib_dir=str(tmp_path),
        binproviders="env",
        dry_run=None,
        min_version=None,
        postinstall_scripts=None,
        min_release_age=None,
        overrides=None,
        install_root=None,
        bin_dir=None,
        euid=None,
        install_timeout=None,
        version_timeout=None,
    )

    assert options.min_version is None
    assert options.postinstall_scripts is None
    assert options.min_release_age is None
    assert options.overrides is None
    assert options.install_root is None
    assert options.bin_dir is None
    assert options.euid is None
    assert options.install_timeout is None
    assert options.version_timeout is None


def test_build_providers_passes_provider_level_flags_through(tmp_path):
    """Provider constructors should receive the configured knobs."""

    from abx_pkg import PipProvider

    providers = cli_module.build_providers(
        ["pip", "env"],
        tmp_path,
        dry_run=True,
        install_root=tmp_path / "custom-root",
        bin_dir=tmp_path / "custom-bin",
        euid=1000,
        install_timeout=300,
        version_timeout=25,
    )

    pip_provider, env_provider = providers
    assert isinstance(pip_provider, PipProvider)
    assert pip_provider.dry_run is True
    assert pip_provider.euid == 1000
    assert pip_provider.install_timeout == 300
    assert pip_provider.version_timeout == 25
    # pip has INSTALL_ROOT_FIELD=pip_venv so install_root is honored.
    assert pip_provider.pip_venv == (tmp_path / "custom-root").resolve()

    # env has no INSTALL_ROOT_FIELD or BIN_DIR_FIELD, but build_providers
    # passes install_root/bin_dir blindly; BinProvider.__init__ should
    # have warn-and-ignored rather than raising.
    assert env_provider.dry_run is True
    assert env_provider.euid == 1000
    assert env_provider.install_timeout == 300
    assert env_provider.version_timeout == 25


def test_build_providers_warn_ignores_install_root_on_unsupporting_provider(
    tmp_path,
    caplog,
):
    """Warning should fire on providers without INSTALL_ROOT_FIELD."""

    # Earlier tests in this file may have called configure_logging which
    # disables propagation on the ``abx_pkg`` package logger. Re-enable
    # it so pytest's root-attached caplog handler sees child records.
    import logging as _logging

    _logging.getLogger("abx_pkg").propagate = True

    with caplog.at_level("WARNING", logger="abx_pkg.binprovider"):
        providers = cli_module.build_providers(
            ["env", "apt"],
            tmp_path,
            install_root=tmp_path / "ignored",
        )

    assert len(providers) == 2
    warning_messages = [record.message for record in caplog.records]
    assert any(
        "EnvProvider ignoring unsupported install_root" in msg
        for msg in warning_messages
    ), warning_messages
    assert any(
        "AptProvider ignoring unsupported install_root" in msg
        for msg in warning_messages
    ), warning_messages


def test_build_providers_constructs_every_builtin_provider(tmp_path, caplog):
    """Smoke-test: every builtin provider can be constructed with every CLI flag.

    Ensures we don't regress the warn-and-ignore contract across the full
    set of providers abx-pkg ships — no provider should raise when given
    an install_root/bin_dir it doesn't support, and every provider must
    accept the base-class kwargs (dry_run/euid/install_timeout/version_timeout).
    """

    import logging as _logging

    _logging.getLogger("abx_pkg").propagate = True

    with caplog.at_level("WARNING", logger="abx_pkg.binprovider"):
        providers = cli_module.build_providers(
            list(cli_module.ALL_PROVIDER_NAMES),
            tmp_path,
            dry_run=True,
            install_root=tmp_path / "shared-root",
            bin_dir=tmp_path / "shared-bin",
            euid=1000,
            install_timeout=42,
            version_timeout=7,
        )

    assert len(providers) == len(cli_module.ALL_PROVIDER_NAMES)
    for provider in providers:
        assert provider.dry_run is True
        assert provider.euid == 1000
        assert provider.install_timeout == 42
        assert provider.version_timeout == 7

    unsupported_warnings = [
        record.message
        for record in caplog.records
        if "ignoring unsupported install_root" in record.message
        or "ignoring unsupported bin_dir" in record.message
    ]
    # Every provider without INSTALL_ROOT_FIELD should have emitted a warning.
    env_provider_class = cli_module.PROVIDER_CLASS_BY_NAME["env"]
    assert env_provider_class.INSTALL_ROOT_FIELD is None
    assert any(
        "EnvProvider ignoring unsupported install_root" in msg
        for msg in unsupported_warnings
    ), unsupported_warnings


def test_build_binary_forwards_binary_level_fields(tmp_path):
    """CliOptions.min_version / postinstall_scripts / min_release_age /
    overrides must land on the Binary instance."""

    options = cli_module.CliOptions(
        lib_dir=tmp_path,
        provider_names=["env", "pip"],
        dry_run=False,
        min_version="2.0.0",
        postinstall_scripts=False,
        min_release_age=30.0,
        overrides={"pip": {"install_args": ["custom==1.0"]}},
    )

    binary = cli_module.build_binary("black", options, dry_run=False)

    assert str(binary.min_version) == "2.0.0"
    assert binary.postinstall_scripts is False
    assert binary.min_release_age == 30.0
    assert binary.overrides == {"pip": {"install_args": ["custom==1.0"]}}


def test_install_postinstall_scripts_false_warns_on_unsupporting_providers(tmp_path):
    """Providers that can't enforce postinstall_scripts=False must emit a
    warning to stderr and continue (no hard-fail).

    Exercises the user's example of mixing a provider that supports the
    flag (pip/uv) with one that doesn't (apt). ``Binary.install``
    iterates providers in order and calls ``install()`` on each, so the
    warning fires unconditionally — unlike the ``run`` path, where a
    successful ``load()`` would short-circuit install().
    """

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=apt,uv,pip",
        "--postinstall-scripts=False",
        "--min-release-age=0",
        "install",
        "black",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    # The warn-and-ignore message from AptProvider must be on stderr.
    assert (
        "AptProvider.install ignoring unsupported postinstall_scripts=False"
        in proc.stderr
    ), proc.stderr


def test_install_min_version_too_high_fails_loudly(tmp_path):
    """--min-version should gate Binary.is_valid after install."""

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        "--min-version=9999.0.0",
        "--min-release-age=0",
        "install",
        "black",
        timeout=900,
    )

    assert proc.returncode != 0
    assert "9999" in proc.stderr or "does not satisfy" in proc.stderr


def test_install_with_install_root_override_installs_there(tmp_path):
    """--install-root should pin pip_venv to the override directory."""

    custom_root = tmp_path / "custom-pip-root"
    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        f"--install-root={custom_root}",
        "--min-release-age=0",
        "install",
        "black",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    assert list(custom_root.rglob("black")), (
        f"Expected black under {custom_root}, stderr was:\n{proc.stderr}"
    )
    # And nothing under the lib_dir default location.
    assert not list((tmp_path / "pip" / "venv").rglob("black"))


def test_install_with_overrides_json_uses_custom_install_args(tmp_path):
    """--overrides should thread through to Binary.overrides verbatim."""

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=pip",
        '--overrides={"pip":{"install_args":["black==24.2.0"]}}',
        "--min-release-age=0",
        "install",
        "black",
        timeout=900,
    )

    assert proc.returncode == 0, proc.stderr
    # The pinned version should win over pip's default resolution.
    assert "24.2.0" in proc.stdout


def test_parse_overrides_rejects_invalid_json():
    with pytest.raises(click.BadParameter):
        cli_module._parse_overrides("not-json")


def test_parse_overrides_rejects_non_dict_json():
    with pytest.raises(click.BadParameter):
        cli_module._parse_overrides("[1, 2, 3]")


def test_parse_cli_bool_rejects_garbage():
    with pytest.raises(click.BadParameter):
        cli_module._parse_cli_bool("maybe")


def test_parse_cli_float_rejects_garbage():
    with pytest.raises(click.BadParameter):
        cli_module._parse_cli_float("not-a-number")


def test_parse_cli_int_accepts_int_and_exact_float_strings():
    assert cli_module._parse_cli_int("10") == 10
    assert cli_module._parse_cli_int("10.0") == 10
    assert cli_module._parse_cli_int("None") is None
    assert cli_module._parse_cli_int("null") is None
    assert cli_module._parse_cli_int(None) is None


def test_parse_cli_int_rejects_non_integer_floats_and_garbage():
    with pytest.raises(click.BadParameter):
        cli_module._parse_cli_int("3.5")
    with pytest.raises(click.BadParameter):
        cli_module._parse_cli_int("abc")


# ---------------------------------------------------------------------------
# Bare bool flag expansion: `--dry-run` → `--dry-run=True`, same for
# `--postinstall-scripts`. Value forms are left alone so click parses them
# as a string value.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["--binproviders=env", "--dry-run", "install", "python3"],
            ["--binproviders=env", "--dry-run=True", "install", "python3"],
        ),
        (
            ["--dry-run=False", "install", "python3"],
            ["--dry-run=False", "install", "python3"],
        ),
        (
            ["--dry-run=None", "install", "python3"],
            ["--dry-run=None", "install", "python3"],
        ),
        (
            ["--postinstall-scripts", "install", "python3"],
            ["--postinstall-scripts=True", "install", "python3"],
        ),
        (
            ["--postinstall-scripts=False", "install", "python3"],
            ["--postinstall-scripts=False", "install", "python3"],
        ),
        (
            ["--dry-run", "--postinstall-scripts", "install", "python3"],
            ["--dry-run=True", "--postinstall-scripts=True", "install", "python3"],
        ),
    ],
)
def test_expand_bare_bool_flags_rewrites_bare_forms_in_place(argv, expected):
    assert cli_module._expand_bare_bool_flags(argv) == expected


# ---------------------------------------------------------------------------
# Real-live coverage of every supported flag via `install` (short-running).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra_flag",),
    [
        ("--min-version=0.0.0",),
        ("--min-version=None",),
        ("--postinstall-scripts=True",),
        ("--postinstall-scripts=False",),
        ("--postinstall-scripts=1",),
        ("--postinstall-scripts=0",),
        ("--postinstall-scripts=None",),
        ("--min-release-age=0",),
        ("--min-release-age=0.5",),
        ("--min-release-age=None",),
        ("--install-timeout=60",),
        ("--install-timeout=60.0",),
        ("--install-timeout=None",),
        ("--version-timeout=10",),
        ("--version-timeout=10.0",),
        ("--version-timeout=None",),
        ("--euid=None",),
        ("--overrides=None",),
        ('--overrides={"env":{}}',),
        ("--bin-dir=None",),
        ("--install-root=None",),
        ("--dry-run=True",),
        ("--dry-run=False",),
        ("--dry-run=None",),
    ],
)
def test_install_command_accepts_every_supported_flag_form(extra_flag, tmp_path):
    """Live smoke-test: every flag form resolves `ls` via env without raising."""

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        extra_flag,
        "install",
        "ls",
    )

    assert proc.returncode == 0, (
        f"--lib={tmp_path} --binproviders=env {extra_flag} install ls "
        f"failed with exit {proc.returncode}\nstderr:\n{proc.stderr}"
    )


@pytest.mark.parametrize(
    "subcommand",
    ["install", "load", "load-or-install", "load_or_install"],
)
def test_every_subcommand_accepts_the_full_option_surface(subcommand, tmp_path):
    """Every subcommand honours every option by reusing shared_options."""

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        "--min-version=0.0.0",
        "--postinstall-scripts=False",
        "--min-release-age=0",
        "--install-timeout=60",
        "--version-timeout=10",
        "--dry-run=False",
        subcommand,
        "ls",
    )

    assert proc.returncode == 0, proc.stderr
    assert "ls" in proc.stdout


def test_update_subcommand_accepts_the_full_option_surface(tmp_path):
    """`update` must also honour every option, just like install."""

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        "--min-version=0.0.0",
        "--postinstall-scripts=False",
        "--min-release-age=0",
        "--install-timeout=60",
        "--version-timeout=10",
        "--dry-run=False",
        "update",
        "ls",
    )

    assert proc.returncode == 0, proc.stderr


def test_subcommand_level_option_overrides_group_level():
    """A subcommand-level flag should override the group-level flag field-by-field."""

    proc = _run_abx_pkg_cli(
        "--binproviders=apt",  # group-level: would match nothing useful
        "install",
        "--binproviders=env",  # subcommand-level: wins
        "ls",
    )

    assert proc.returncode == 0, proc.stderr
    assert "env" in proc.stdout
    assert "ls" in proc.stdout


# ---------------------------------------------------------------------------
# Real-live coverage of every supported flag via `run` (uses group_options).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--min-version=0.0.0",
        "--postinstall-scripts=False",
        "--min-release-age=0",
        "--install-timeout=60",
        "--version-timeout=10",
        '--overrides={"env":{}}',
        "--install-root=None",
        "--bin-dir=None",
        "--euid=None",
    ],
)
def test_run_command_honours_group_level_options(flag, tmp_path):
    """`run` reads its options off the group-level CliOptions, so every
    abx-pkg group flag must survive the round-trip through build_binary."""

    proc = _run_abx_pkg_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        flag,
        "run",
        "python3",
        "-c",
        "print('run-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "run-ok"


# ---------------------------------------------------------------------------
# Real-live coverage: `abx` forwards every option to abx-pkg unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--min-version=0.0.0",
        "--postinstall-scripts=True",
        "--postinstall-scripts=False",
        "--min-release-age=0",
        "--install-timeout=60",
        "--version-timeout=10",
        '--overrides={"env":{}}',
        "--install-root=None",
        "--bin-dir=None",
        "--euid=None",
        "--dry-run=False",
    ],
)
def test_abx_forwards_every_option_to_abx_pkg(flag, tmp_path):
    proc = _run_abx_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        flag,
        "python3",
        "-c",
        "print('abx-ok')",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abx-ok"


def test_abx_dry_run_value_form_is_forwarded_to_abx_pkg(tmp_path):
    """`abx --dry-run=True BIN ...` must propagate as dry_run=True."""

    proc = _run_abx_cli(
        f"--lib={tmp_path}",
        "--binproviders=env",
        "--dry-run=True",
        "python3",
        "-c",
        "print('should-not-print')",
    )

    # Dry-run short-circuits without execing the binary.
    assert proc.returncode == 0, proc.stderr
    assert "should-not-print" not in proc.stdout
