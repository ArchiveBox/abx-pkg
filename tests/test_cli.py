from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
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


def _run_abx_pkg_cli(
    *args: str,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real `abx-pkg` console script with a clean env."""

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ABX_PKG_")
    }
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        [str(_abx_pkg_executable()), *args],
        capture_output=True,
        text=True,
        env=env,
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
    """`abx-pkg run` with an already-installed binary should stream its output."""

    proc = _run_abx_pkg_cli("--binproviders=env", "run", "ls", "/")

    assert proc.returncode == 0, proc.stderr
    assert "bin" in proc.stdout.split()
    # stderr should contain no abx-pkg bookkeeping (ls has nothing to say)
    assert proc.stderr == ""


def test_run_passes_flag_args_through_without_requiring_dash_dash():
    """Flags after `run BINARY_NAME` must reach the binary, not click."""

    proc = _run_abx_pkg_cli("--binproviders=env", "run", "ls", "--help")

    assert proc.returncode == 0, proc.stderr
    assert "Usage:" in proc.stdout
    assert "list" in proc.stdout.lower()


def test_run_propagates_nonzero_exit_code_from_underlying_binary():
    """Exit codes from the underlying binary must flow back unchanged."""

    proc = _run_abx_pkg_cli(
        "--binproviders=env",
        "run",
        "ls",
        "/__abx_pkg_nonexistent_path__",
    )

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "No such file" in proc.stderr or "cannot access" in proc.stderr


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
        "ls",
        "/",
        env_overrides={"ABX_PKG_BINPROVIDERS": "env"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "bin" in proc.stdout


def test_run_binproviders_flag_overrides_env_var():
    """`--binproviders` on the command line wins over ABX_PKG_BINPROVIDERS."""

    proc = _run_abx_pkg_cli(
        "--binproviders=env",
        "run",
        "ls",
        "/",
        env_overrides={"ABX_PKG_BINPROVIDERS": "pip,brew"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "bin" in proc.stdout


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
    ("extra_args", "expected_exit"),
    [
        ((), 0),
        (("/",), 0),
        (("/__abx_pkg_missing__",), None),  # None = "not zero"
    ],
)
def test_run_forwards_variadic_positional_args_to_binary(extra_args, expected_exit):
    proc = _run_abx_pkg_cli("--binproviders=env", "run", "ls", *extra_args)

    if expected_exit is None:
        assert proc.returncode != 0
    else:
        assert proc.returncode == expected_exit, proc.stderr
