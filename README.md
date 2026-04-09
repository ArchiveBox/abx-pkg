<h1><a href="https://github.com/ArchiveBox/abx-pkg"><code>abx-pkg</code></a> &nbsp; &nbsp; &nbsp; &nbsp; 📦  <small><code>apt</code>&nbsp; <code>brew</code>&nbsp; <code>pip</code>&nbsp; <code>uv</code>&nbsp; <code>npm</code>&nbsp; <code>pnpm</code>&nbsp; <code>yarn</code>&nbsp; <code>bun</code>&nbsp; <code>deno</code>&nbsp; <code>cargo</code>&nbsp; <code>gem</code>&nbsp; <code>goget</code>&nbsp; <code>nix</code>&nbsp; <code>docker</code>&nbsp; <code>bash</code>&nbsp; <code>chromewebstore</code>&nbsp; <code>puppeteer</code></small><br/><sub>Simple Python interfaces for package managers + installed binaries.</sub></h1>
<br/>

[![PyPI][pypi-badge]][pypi]
[![Python Version][version-badge]][pypi]
[![Django Version][django-badge]][pypi]
[![GitHub][licence-badge]][licence]
[![GitHub Last Commit][repo-badge]][repo]
<!--[![Downloads][downloads-badge]][pypi]-->

<br/>

**It's an ORM for your package managers, providing a nice python types for packages + installers.**  
  
**This is a [Python library](https://pypi.org/project/abx-pkg/) for installing & managing packages locally with a variety of package managers.**  
It's designed for when `requirements.txt` isn't enough, and you have to detect or install dependencies at runtime. It's great for installing and managing MCP servers and their dependencies at runtime.


```bash
pip install abx-pkg
```

```python
from abx_pkg import Binary, npm

curl = Binary(name="curl").load()
print(curl.abspath, curl.version, curl.exec(cmd=["--version"]))

npm.install("puppeteer")
```

> 📦 Provides consistent interfaces for runtime dependency resolution & installation across multiple package managers & OSs
> ✨ Built with [`pydantic`](https://pydantic-docs.helpmanual.io/) v2 for strong static typing guarantees and easy conversion to/from json
> 🌈 Usable with [`django`](https://docs.djangoproject.com/en/5.0/) >= 4.0, [`django-ninja`](https://django-ninja.dev/), and OpenAPI + [`django-jsonform`](https://django-jsonform.readthedocs.io/) to build UIs & APIs
> 🦄 Driver layer can be [`pyinfra`](https://github.com/pyinfra-dev/pyinfra) / [`ansible`](https://github.com/ansible/ansible) / or built-in `abx-pkg` engine

<sub><i>Built by <a href="https://github.com/ArchiveBox">ArchiveBox</a> to install & auto-update our extractor dependencies at runtime (<code>chrome</code>, <code>wget</code>, <code>curl</code>, etc.) on `macOS`/`Linux`/`Docker`.</i></sub>

<br/>

**Source Code**: [https://github.com/ArchiveBox/abx-pkg/](https://github.com/ArchiveBox/abx-pkg/)

**Documentation**: [https://github.com/ArchiveBox/abx-pkg/blob/main/README.md](https://github.com/ArchiveBox/abx-pkg/blob/main/README.md)

<br/>

```python
from abx_pkg import Binary, apt, brew, pip, npm, env

# Provider singletons are available as simple imports — no manual instantiation needed
dependencies = [
    Binary(name='curl',       binproviders=[env, apt, brew]),
    Binary(name='wget',       binproviders=[env, apt, brew]),
    Binary(name='yt-dlp',     binproviders=[env, pip, apt, brew]),
    Binary(name='playwright', binproviders=[env, pip, npm]),
    Binary(name='puppeteer',  binproviders=[env, npm]),
]
for binary in dependencies:
    binary = binary.load_or_install()

    print(binary.abspath, binary.version, binary.binprovider, binary.is_valid, binary.sha256)
    # Path(...) SemVer(...) EnvProvider()/AptProvider()/BrewProvider()/PipProvider()/NpmProvider() True '<sha256>'

    binary.exec(cmd=['--version'])   # curl 7.81.0 (x86_64-apple-darwin23.0) libcurl/7.81.0 ...
```

`Binary.min_version` is optional. Leave it as `None` when any discovered version is acceptable, or set it to a `SemVer`/string to enforce a minimum version after load/install.

```python
from abx_pkg import Binary, apt, brew, env

# Use providers directly for package manager operations
apt.install('wget')
print(apt.PATH, apt.get_abspaths('wget'), apt.get_version('wget'))

# our Binary API provides a nice type-checkable, validated, serializable handle
ffmpeg = Binary(name='ffmpeg', binproviders=[env, apt, brew]).load()
print(ffmpeg)                       # Binary(name='ffmpeg', abspath=Path(...), version=SemVer(...), sha256='...')
print(ffmpeg.abspaths)              # show all matching binaries found via each provider PATH
print(ffmpeg.model_dump(mode='json'))  # JSON-ready dict
print(ffmpeg.model_json_schema())   # ... OpenAPI-ready JSON schema showing all available fields
```

```python
from pydantic import InstanceOf
from abx_pkg import Binary, BinProvider, BrewProvider, EnvProvider

# You can also instantiate provider classes manually for custom configuration,
# or define binaries as classes for type checking
class CurlBinary(Binary):
    name: str = 'curl'
    binproviders: list[InstanceOf[BinProvider]] = [BrewProvider(), EnvProvider()]

curl = CurlBinary().install()
assert isinstance(curl, CurlBinary)                                 # CurlBinary is a unique type you can use in annotations now
print(curl.abspath, curl.version, curl.binprovider, curl.is_valid)  # Path(...) SemVer(...) BrewProvider()/EnvProvider() True
curl.exec(cmd=['--version'])                                        # curl 8.4.0 (x86_64-apple-darwin23.0) libcurl/8.4.0 ...
```

### Supported Providers

**So far it supports `installing`/`finding installed`/`updating`/`removing` packages or binaries on `Linux`/`macOS` with:**

- `apt` (Ubuntu/Debian/etc.)
- `brew` (macOS/Linux)
- `pip` (Linux/macOS)
- `uv` (Linux/macOS)
- `npm` (Linux/macOS)
- `pnpm` (Linux/macOS)
- `yarn` (Linux/macOS, Yarn 4+ / Berry recommended)
- `bun` (Linux/macOS)
- `deno` (Linux/macOS)
- `cargo` (Linux/macOS)
- `gem` (Linux/macOS)
- `goget` (Linux/macOS, via [`GoGetProvider`](./abx_pkg/binprovider_goget.py))
- `nix` (Linux/macOS)
- `docker` (Linux/macOS, using local wrapper scripts that run `docker run`)
- `env` (looks for existing version of binary in user's `$PATH` at runtime)
- `bash` (Linux/macOS, runs explicit shell-command overrides in a managed install root)
- `chromewebstore` (Linux/macOS, downloads and unpacks Chrome Web Store extensions)
- `puppeteer` (Linux/macOS, installs browser artifacts via `@puppeteer/browsers`)
- `pyinfra` (Linux/macOS, delegates to host package managers through `pyinfra`)
- `ansible` (Linux/macOS, delegates to host package managers through `ansible-runner`)

*Planned:* `apk`, `pkg`, and additional future provider backends.

`DockerProvider` expects image refs as install args, typically via overrides on a `Binary`. It writes a local wrapper script for the binary and executes it via `docker run ...`; the binary version is parsed from the image tag, so semver-like tags work best.

`NpmProvider` prefers a real `npm` executable when both `npm` and `pnpm` are installed. If `npm` is unavailable, it can still drive installs and metadata lookups through `pnpm` using the same provider API.

`PnpmProvider`, `YarnProvider`, `BunProvider`, and `DenoProvider` are dedicated wrappers around their respective package managers — pick one explicitly when you don't want `NpmProvider`'s auto-switching behavior, or when you want the security defaults of a specific tool. All four hydrate `min_release_age` from the latest supply-chain hardening flags shipped by their upstream CLIs.

---


## Usage

```bash
pip install abx-pkg
```

### CLI

Installing `abx-pkg` also provides an `abx-pkg` CLI entrypoint:

```bash
abx-pkg --version
abx-pkg version

abx-pkg install yt-dlp
abx-pkg update yt-dlp
abx-pkg uninstall yt-dlp
abx-pkg load yt-dlp
abx-pkg load_or_install yt-dlp
abx-pkg load-or-install yt-dlp
```

The CLI uses the same ordered failover behavior as `Binary(...)`: it tries the selected binproviders in order and stops after the first success.

```bash
env ABX_PKG_BINPROVIDERS=env,uv,apt,brew abx-pkg install yt-dlp
```

By default, managed provider state is rooted under `~/.config/abx/lib`. You can override that with `ABX_PKG_LIB_DIR` or `--lib`.

```bash
abx-pkg --lib=./relativelib install yt-dlp
env ABX_PKG_LIB_DIR=/tmp/abxlib abx-pkg install yt-dlp
```

You can restrict or reorder providers with `ABX_PKG_BINPROVIDERS` or `--binproviders`.

```bash
env ABX_PKG_BINPROVIDERS=env,uv,apt,brew,pnpm,gem,cargo,goget abx-pkg install yt-dlp
abx-pkg install --binproviders=pnpm prettier
```

`--dry-run` and `ABX_PKG_DRY_RUN=1` show the package-manager commands that would run without mutating the host.

```bash
env ABX_PKG_DRY_RUN=1 abx-pkg install some-dangerous-package
abx-pkg install --dry-run --binproviders=pnpm prettier
```

`abx-pkg --version` prints the package semver on the first line, then one line per available provider installer binary:

```text
1.9.28
env which /usr/bin/which 999.999.999
uv uv /path/to/uv 0.11.4
pnpm pnpm /path/to/pnpm 10.12.1
```

CLI result lines are written to `stdout`. Progress and debug logging are written to `stderr`, and interactive TTY sessions default to `DEBUG` logging.

### Lazy Provider Singletons

All built-in providers are available as lazy singletons — just import them by name:

```python
from abx_pkg import apt, brew, pip, npm, env

apt.install('curl')
env.load('wget')
```

These are instantiated on first access and cached for reuse. If you need custom configuration, you can still instantiate provider classes directly:

```python
from pathlib import Path
from abx_pkg import PipProvider

custom_pip = PipProvider(pip_venv=Path("/tmp/abx-pkg-venv"), min_release_age=0)
```

### Version Floors

`Binary.min_version` is enforced after a provider resolves or installs a binary. Provider discovery can still succeed, but the final `Binary` will be rejected if the loaded version is below your required floor.

```python
from abx_pkg import Binary, SemVer, env, brew

curl = Binary(
    name="curl",
    min_version=SemVer("8.0.0"),
    binproviders=[env, brew],
).load_or_install()
```

Use `min_version=None` to explicitly disable version floor checks.

### [`BinProvider`](https://github.com/ArchiveBox/abx-pkg/blob/main/abx_pkg/binprovider.py#:~:text=class%20BinProvider)

**Built-in implementations:** `EnvProvider`, `AptProvider`, `BrewProvider`, `PipProvider`, `UvProvider`, `NpmProvider`, `PnpmProvider`, `YarnProvider`, `BunProvider`, `DenoProvider`, `CargoProvider`, `GemProvider`, `GoGetProvider`, `NixProvider`, `DockerProvider`, `PyinfraProvider`, `AnsibleProvider`, `BashProvider`, `ChromeWebstoreProvider`, `PuppeteerProvider`, `PlaywrightProvider`

This type represents a provider of binaries, e.g. a package manager like `apt` / `pip` / `npm`, or `env` (which only resolves binaries already present in `$PATH`).

#### 🧩 Shared API

Every provider exposes the same lifecycle surface:

- `load()` / `install()` / `update()` / `uninstall()` / `load_or_install()`
- `get_install_args()` to resolve package names / formulae / image refs / module specs
- `get_abspath()` / `get_abspaths()` / `get_version()` / `get_sha256()`

Shared base defaults come from [`abx_pkg/binprovider.py`](./abx_pkg/binprovider.py) and apply unless a concrete provider overrides them:

```python
INSTALLER_BIN = "env"
PATH = str(Path(sys.executable).parent)
postinstall_scripts = None           # some providers override this with ABX_PKG_POSTINSTALL_SCRIPTS
min_release_age = None               # some providers override this with ABX_PKG_MIN_RELEASE_AGE
install_timeout = 120                # or ABX_PKG_INSTALL_TIMEOUT=120
version_timeout = 10                 # or ABX_PKG_VERSION_TIMEOUT=10
dry_run = False                      # or ABX_PKG_DRY_RUN=1 / DRY_RUN=1
```

- `dry_run`: use `provider.get_provider_with_overrides(dry_run=True)`, pass `dry_run=True` directly to `install()` / `update()` / `uninstall()` / `load_or_install()`, or set `ABX_PKG_DRY_RUN=1` / `DRY_RUN=1`. If both env vars are set, `ABX_PKG_DRY_RUN` wins. Provider subprocesses are logged and skipped, `install()` / `update()` return a placeholder loaded binary, and `uninstall()` returns `True` without mutating the host.
- `install_timeout`: shared provider-level timeout used by `install()`, `update()`, and `uninstall()` handler execution paths. Can also be set with `ABX_PKG_INSTALL_TIMEOUT`.
- `version_timeout`: shared provider-level timeout used by version / metadata probes such as `--version`, `npm show`, `npm list`, `pip show`, `go version -m`, and brew lookups. Can also be set with `ABX_PKG_VERSION_TIMEOUT`.
- `postinstall_scripts` and `min_release_age` are standard provider/binary/action kwargs, but only supporting providers hydrate default values from `ABX_PKG_POSTINSTALL_SCRIPTS` and `ABX_PKG_MIN_RELEASE_AGE`.
- Providers that do not support one of those controls leave the provider default as `None`. If you pass an explicit unsupported value during `install()` / `update()`, it is logged as a warning and ignored.
- Precedence is: explicit action args > `Binary(...)` defaults > provider defaults.

#### 🌱 Environment variables

All abx-pkg env vars are read once at import time and only apply when set. Explicit constructor kwargs always override these defaults.

**Behavioral controls** (apply across all providers):

| Variable | Default | Effect |
| --- | --- | --- |
| `ABX_PKG_DRY_RUN` / `DRY_RUN` | `0` | Flips the shared `dry_run` default. `ABX_PKG_DRY_RUN` wins if both are set. Provider subprocesses are logged and skipped, `install()` / `update()` return a placeholder, `uninstall()` returns `True`. |
| `ABX_PKG_INSTALL_TIMEOUT` | `120` | Seconds to wait for `install()` / `update()` / `uninstall()` handler subprocesses. |
| `ABX_PKG_VERSION_TIMEOUT` | `10` | Seconds to wait for version / metadata probes (`--version`, `npm show`, `pip show`, etc.). |
| `ABX_PKG_POSTINSTALL_SCRIPTS` | unset | Hydrates the provider-level default for the `postinstall_scripts` kwarg on every provider that supports it (`pip`, `uv`, `npm`, `pnpm`, `yarn`, `bun`, `deno`, `brew`, `chromewebstore`, `puppeteer`). |
| `ABX_PKG_MIN_RELEASE_AGE` | `7` | Hydrates the provider-level default (in days) for the `min_release_age` kwarg on every provider that supports it (`pip`, `uv`, `npm`, `pnpm`, `yarn`, `bun`, `deno`). |

**Install-root controls** (one global default + one per-provider override):

| Variable | Applies to | Effect |
| --- | --- | --- |
| `ABX_PKG_LIB_DIR` | **every** provider with an `INSTALL_ROOT_FIELD` | Centralized install root. When set, each provider defaults its install root to `$ABX_PKG_LIB_DIR/<provider name>` (e.g. `<lib>/npm`, `<lib>/pip`, `<lib>/gem`, `<lib>/playwright`). Accepts relative (`./lib`), tilde (`~/.config/abx/lib`), and absolute (`/tmp/abxlib`) paths. |
| `ABX_PKG_BASH_ROOT` | `BashProvider` (`bash_root`) | Per-provider override; beats `ABX_PKG_LIB_DIR/bash`. |
| `ABX_PKG_BREW_ROOT` | `BrewProvider` (`brew_prefix`) | Per-provider override; beats `ABX_PKG_LIB_DIR/brew`. |
| `ABX_PKG_BUN_ROOT` | `BunProvider` (`bun_prefix`) | Per-provider override; beats `ABX_PKG_LIB_DIR/bun`. |
| `ABX_PKG_CARGO_ROOT` | `CargoProvider` (`cargo_root`) | Per-provider override; beats `ABX_PKG_LIB_DIR/cargo`. |
| `ABX_PKG_CHROMEWEBSTORE_ROOT` | `ChromeWebstoreProvider` (`extensions_root`) | Per-provider override; beats `ABX_PKG_LIB_DIR/chromewebstore`. |
| `ABX_PKG_DENO_ROOT` | `DenoProvider` (`deno_root`) | Per-provider override; beats `ABX_PKG_LIB_DIR/deno`. |
| `ABX_PKG_DOCKER_ROOT` | `DockerProvider` (`docker_root`) | Per-provider override; beats `ABX_PKG_LIB_DIR/docker`. |
| `ABX_PKG_GEM_ROOT` | `GemProvider` (`gem_home`) | Per-provider override; beats `ABX_PKG_LIB_DIR/gem`. |
| `ABX_PKG_GOGET_ROOT` | `GoGetProvider` (`gopath`) | Per-provider override; beats `ABX_PKG_LIB_DIR/goget`. |
| `ABX_PKG_NIX_ROOT` | `NixProvider` (`nix_profile`) | Per-provider override; beats `ABX_PKG_LIB_DIR/nix`. |
| `ABX_PKG_NPM_ROOT` | `NpmProvider` (`npm_prefix`) | Per-provider override; beats `ABX_PKG_LIB_DIR/npm`. |
| `ABX_PKG_PIP_ROOT` | `PipProvider` (`pip_venv`) | Per-provider override; beats `ABX_PKG_LIB_DIR/pip`. |
| `ABX_PKG_PLAYWRIGHT_ROOT` | `PlaywrightProvider` (`playwright_root`) | Per-provider override; beats `ABX_PKG_LIB_DIR/playwright`. |
| `ABX_PKG_PNPM_ROOT` | `PnpmProvider` (`pnpm_prefix`) | Per-provider override; beats `ABX_PKG_LIB_DIR/pnpm`. |
| `ABX_PKG_PUPPETEER_ROOT` | `PuppeteerProvider` (`puppeteer_root`) | Per-provider override; beats `ABX_PKG_LIB_DIR/puppeteer`. |
| `ABX_PKG_UV_ROOT` | `UvProvider` (`uv_venv`) | Per-provider override; beats `ABX_PKG_LIB_DIR/uv`. |
| `ABX_PKG_YARN_ROOT` | `YarnProvider` (`yarn_prefix`) | Per-provider override; beats `ABX_PKG_LIB_DIR/yarn`. |

Install-root precedence (most specific wins): explicit `install_root=` / provider-specific kwarg (e.g. `npm_prefix=`, `pip_venv=`) > `ABX_PKG_<NAME>_ROOT` > `ABX_PKG_LIB_DIR/<name>` > built-in default.

Supported override keys are the same everywhere:

```python
from pathlib import Path
from abx_pkg import PipProvider

provider = PipProvider(pip_venv=Path("/tmp/venv")).get_provider_with_overrides(
    overrides={
        "black": {
            "install_args": ["black==24.4.2"],
            "version": "self.default_version_handler",
            "abspath": "self.default_abspath_handler",
        },
    },
    dry_run=True,
    version_timeout=30,
)
```

- `install_args` / `packages`: package-manager arguments for that provider. `packages` is the legacy alias.
- `abspath`, `version`, `install`, `update`, `uninstall`: literal values, callables, or `"self.method_name"` references that replace the provider handler for a specific binary.

Providers with isolated install locations also expose a shared constructor surface:

- `install_root`: shared alias for provider-specific roots such as `pip_venv`, `npm_prefix`, `cargo_root`, `gem_home`, `gopath`, `nix_profile`, `docker_shim_dir.parent`, and `brew_prefix`.
- `bin_dir`: shared alias for providers that separate package state from executable output, such as `gem_bindir`, `gobin`, and `docker_shim_dir`.
- `provider.install_root` / `provider.bin_dir`: normalized computed properties you can inspect after construction, regardless of which provider-specific args were used.
- Legacy provider-specific args still work. The shared aliases are additive, not replacements.
- Providers that do not have an isolated install location reject `install_root` / `bin_dir` at construction time instead of silently ignoring them.
- When an explicit install root or bin dir is configured, that provider-specific bin location wins during binary discovery and subprocess execution instead of being left behind ambient host `PATH` entries.

#### 🌍 [`EnvProvider`](./abx_pkg/binprovider.py) (`env`)

Source: [`abx_pkg/binprovider.py`](./abx_pkg/binprovider.py) • Tests: [`tests/test_envprovider.py`](./tests/test_envprovider.py)

```python
INSTALLER_BIN = "which"
PATH = DEFAULT_ENV_PATH              # current PATH + current Python bin dir
```

- Install root: none. `env` is read-only and only searches existing binaries on `$PATH`.
- Auto-switching: none.
- Security: `min_release_age` and `postinstall_scripts` are unsupported here and are ignored with a warning if explicitly passed to `install()` / `update()`.
- Overrides: `abspath` / `version` are the useful ones here. `python` has a built-in override to the current `sys.executable` and interpreter version.
- Notes: `install()` / `update()` return explanatory no-op messages, and `uninstall()` returns `False`.

#### 🐧 [`AptProvider`](./abx_pkg/binprovider_apt.py) (`apt`)

Source: [`abx_pkg/binprovider_apt.py`](./abx_pkg/binprovider_apt.py) • Tests: [`tests/test_aptprovider.py`](./tests/test_aptprovider.py)

```python
INSTALLER_BIN = "apt-get"
PATH = ""                            # populated from `dpkg -L bash` bin dirs
euid = 0                             # always runs as root
```

- Install root: **no hermetic prefix support**. Installs into the host package database.
- Auto-switching: tries `PyinfraProvider` first, then `AnsibleProvider`, then falls back to direct `apt-get`.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: in the direct shell fallback, `install_args` becomes `apt-get install -y -qq --no-install-recommends ...`; `update()` uses `apt-get install --only-upgrade ...`.
- Notes: direct mode runs `apt-get update -qq` at most once per day and requests privilege escalation when needed.

#### 🍺 [`BrewProvider`](./abx_pkg/binprovider_brew.py) (`brew`)

Source: [`abx_pkg/binprovider_brew.py`](./abx_pkg/binprovider_brew.py) • Tests: [`tests/test_brewprovider.py`](./tests/test_brewprovider.py)

```python
INSTALLER_BIN = "brew"
PATH = "/home/linuxbrew/.linuxbrew/bin:/opt/homebrew/bin:/usr/local/bin"
brew_prefix = guessed host prefix    # /opt/homebrew, /usr/local, or linuxbrew
```

- Install root: `brew_prefix` is for discovery only. `install_root=...` aliases to `brew_prefix`. **This provider does not create an isolated custom Homebrew prefix.**
- Auto-switching: if `postinstall_scripts=True`, it prefers `PyinfraProvider` and then `AnsibleProvider`; otherwise it falls back to direct `brew`.
- `dry_run`: shared behavior.
- Security: `min_release_age` is unsupported and is ignored with a warning if explicitly requested. `postinstall_scripts=False` is supported for direct `brew` installs via `--skip-post-install`, and `ABX_PKG_POSTINSTALL_SCRIPTS` hydrates the provider default here.
- Overrides: in the direct shell fallback, `install_args` maps to formula / cask args passed to `brew install`, `brew upgrade`, and `brew uninstall`.
- Notes: direct mode runs `brew update` at most once per day. Explicit `--skip-post-install` args in `install_args` win over derived defaults.

#### 🐍 [`PipProvider`](./abx_pkg/binprovider_pip.py) (`pip`)

Source: [`abx_pkg/binprovider_pip.py`](./abx_pkg/binprovider_pip.py) • Tests: [`tests/test_pipprovider.py`](./tests/test_pipprovider.py), [`tests/test_security_controls.py`](./tests/test_security_controls.py)

```python
INSTALLER_BIN = "pip"
PATH = ""                            # auto-built from global/user Python bin dirs
pip_venv = None                      # set this for hermetic installs
cache_dir = user_cache_path("pip", "abx-pkg") or <system temp>/pip-cache
pip_install_args = ["--no-input", "--disable-pip-version-check", "--quiet"]
pip_bootstrap_packages = ["pip", "setuptools", "uv"]
```

- Install root: `pip_venv=None` uses the system/user Python environment. Set `pip_venv=Path(...)` or `install_root=Path(...)` for a hermetic venv rooted at `<pip_venv>/bin`, and that venv bin dir becomes the provider's active executable search path.
- Auto-switching: the provider executable is still `pip`, but install / update / show / uninstall calls use `uv pip ...` when `uv` is available and `PIP_BINARY` is not forcing a specific pip path.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABX_PKG_MIN_RELEASE_AGE` and `ABX_PKG_POSTINSTALL_SCRIPTS`.
- Overrides: `install_args` is passed as pip requirement specs; unpinned specs get a `>=min_version` floor when `min_version` is supplied.
- Notes: `ABX_PKG_POSTINSTALL_SCRIPTS` and `ABX_PKG_MIN_RELEASE_AGE` apply here by default. `postinstall_scripts=False` uses `uv pip --no-build` or plain `pip --only-binary :all:`. `min_release_age` is enforced with `uv --exclude-newer=<cutoff>` or plain `pip --uploaded-prior-to=<cutoff>` when the host pip is new enough. Explicit conflicting flags already present in `install_args` win over the derived defaults.

#### 🚀 [`UvProvider`](./abx_pkg/binprovider_uv.py) (`uv`)

Source: [`abx_pkg/binprovider_uv.py`](./abx_pkg/binprovider_uv.py) • Tests: [`tests/test_uvprovider.py`](./tests/test_uvprovider.py)

```python
INSTALLER_BIN = "uv"
PATH = ""                            # prepends <uv_venv>/bin or uv_tool_bin_dir
uv_venv = None                       # None = global uv tool mode, Path(...) = hermetic venv
uv_tool_dir = None                   # mirrors $UV_TOOL_DIR (global mode only)
uv_tool_bin_dir = None               # mirrors $UV_TOOL_BIN_DIR (global mode only)
cache_dir = user_cache_path("uv", "abx-pkg") or <system temp>/uv-cache
uv_install_args = []
```

- Install root: **two modes, picked by whether `uv_venv` is set.**
  - *Hermetic venv mode (`uv_venv=Path(...)` or `install_root=Path(...)`)*: creates a real venv at the requested path via `uv venv` and installs packages into it with `uv pip install --python <venv>/bin/python ...`. Binaries land in `<uv_venv>/bin/<name>`. This is the idiomatic "install a Python library + its CLI entrypoints into an isolated environment" path and matches `PipProvider`'s `pip_venv` semantics.
  - *Global tool mode (`uv_venv=None`)*: delegates to `uv tool install` which creates a fresh venv per tool under `UV_TOOL_DIR` (default `~/.local/share/uv/tools`) and writes shims into `UV_TOOL_BIN_DIR` (default `~/.local/bin`). Pass `uv_tool_dir=Path(...)` / `uv_tool_bin_dir=Path(...)` to override those dirs hermetically. This is the idiomatic "install a CLI tool globally" path.
- Auto-switching: none. Honors `UV_BINARY=/abs/path/to/uv`. Unlike `PipProvider`, `UvProvider` never falls back to plain `pip` — if `uv` isn't on the host, the provider is unavailable.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABX_PKG_MIN_RELEASE_AGE` and `ABX_PKG_POSTINSTALL_SCRIPTS`. In both modes, `postinstall_scripts=False` becomes `--no-build` (wheels-only, no arbitrary sdist build scripts) and `min_release_age` becomes `--exclude-newer=<ISO8601>` (uv 0.4+). Explicit conflicting flags already present in `install_args` win over the derived defaults.
- Overrides: `install_args` is passed as requirement specs; unpinned specs get a `>=min_version` floor when `min_version` is supplied.
- Notes: update in venv mode is `uv pip install --upgrade`; update in global mode is `uv tool install --force` (re-installs the tool's venv). Uninstall in venv mode uses `uv pip uninstall --python <venv>/bin/python`; in global mode it uses `uv tool uninstall <name>`.

#### 📦 [`NpmProvider`](./abx_pkg/binprovider_npm.py) (`npm`)

Source: [`abx_pkg/binprovider_npm.py`](./abx_pkg/binprovider_npm.py) • Tests: [`tests/test_npmprovider.py`](./tests/test_npmprovider.py), [`tests/test_security_controls.py`](./tests/test_security_controls.py)

```python
INSTALLER_BIN = "npm"
PATH = ""                            # auto-built from npm/pnpm local + global bin dirs
npm_prefix = None                    # None = global install, Path(...) = hermetic-ish prefix
cache_dir = user_cache_path("npm", "abx-pkg") or <system temp>/npm-cache
npm_install_args = ["--force", "--no-audit", "--no-fund", "--loglevel=error"]
```

- Install root: `npm_prefix=None` installs globally. Set `npm_prefix=Path(...)` or `install_root=Path(...)` to install under `<prefix>/node_modules/.bin`; that prefix bin dir becomes the provider's active executable search path.
- Auto-switching: prefers a real `npm` binary, falls back to `pnpm` if `npm` is unavailable, and honors `NPM_BINARY=/abs/path/to/npm-or-pnpm`.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABX_PKG_MIN_RELEASE_AGE` and `ABX_PKG_POSTINSTALL_SCRIPTS`.
- Overrides: `install_args` is passed as npm package specs; unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: `ABX_PKG_POSTINSTALL_SCRIPTS` and `ABX_PKG_MIN_RELEASE_AGE` apply here by default. Direct npm mode uses `--ignore-scripts` and `--min-release-age=<days>` when the host npm supports it. pnpm mode writes `pnpm-workspace.yaml` with `minimumReleaseAge`; that is how release-age enforcement is configured there. Explicit conflicting flags already present in `install_args` win over the derived defaults.

#### 📦 [`PnpmProvider`](./abx_pkg/binprovider_pnpm.py) (`pnpm`)

Source: [`abx_pkg/binprovider_pnpm.py`](./abx_pkg/binprovider_pnpm.py) • Tests: [`tests/test_pnpmprovider.py`](./tests/test_pnpmprovider.py)

```python
INSTALLER_BIN = "pnpm"
PATH = ""                            # auto-built from pnpm local + global bin dirs
pnpm_prefix = None                   # None = global install, Path(...) = hermetic-ish prefix
cache_dir = user_cache_path("pnpm", "abx-pkg") or <system temp>/pnpm-cache
pnpm_install_args = ["--loglevel=error"]
```

- Install root: `pnpm_prefix=None` installs globally. Set `pnpm_prefix=Path(...)` or `install_root=Path(...)` to install under `<prefix>/node_modules/.bin`; that prefix bin dir becomes the provider's active executable search path.
- Auto-switching: none. This provider always shells out to `pnpm` directly. Use `NpmProvider` for the auto-switching `npm`-or-`pnpm` behavior. Honors `PNPM_BINARY=/abs/path/to/pnpm`.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABX_PKG_MIN_RELEASE_AGE` and `ABX_PKG_POSTINSTALL_SCRIPTS`. `min_release_age` requires pnpm 10.16+, and `supports_min_release_age()` returns `False` on older hosts (then it logs a warning and continues).
- Overrides: `install_args` is passed as pnpm package specs; unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: pnpm has no `--min-release-age` CLI flag; this provider passes `--config.minimumReleaseAge=<minutes>` (the camelCase / kebab-case form pnpm exposes via its `--config.<key>=<value>` override). `PNPM_HOME` is auto-populated so `pnpm add -g` works without polluting the user's shell config.

#### 🧶 [`YarnProvider`](./abx_pkg/binprovider_yarn.py) (`yarn`)

Source: [`abx_pkg/binprovider_yarn.py`](./abx_pkg/binprovider_yarn.py) • Tests: [`tests/test_yarnprovider.py`](./tests/test_yarnprovider.py)

```python
INSTALLER_BIN = "yarn"
PATH = ""                            # prepends <yarn_prefix>/node_modules/.bin
yarn_prefix = None                   # workspace dir, defaults to ABX_PKG_YARN_ROOT or ~/.cache/abx-pkg/yarn
cache_dir = user_cache_path("yarn", "abx-pkg") or <system temp>/yarn-cache
yarn_install_args = []
```

- Install root: Yarn 4 / Yarn Berry is workspace-based, so the provider always operates inside a project directory. Set `yarn_prefix=Path(...)` or `install_root=Path(...)` for a hermetic workspace; the workspace is auto-initialized with a stub `package.json` and `.yarnrc.yml` (`nodeLinker: node-modules` so binaries land in `<workspace>/node_modules/.bin`). When unset, the provider uses `$ABX_PKG_YARN_ROOT` or `~/.cache/abx-pkg/yarn`.
- Auto-switching: none. Honors `YARN_BINARY=/abs/path/to/yarn`. Both Yarn classic (1.x) and Yarn Berry (2+) work for basic install/update/uninstall, but only Yarn 4.10+ supports the security flags.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABX_PKG_MIN_RELEASE_AGE` and `ABX_PKG_POSTINSTALL_SCRIPTS`. Both controls require Yarn 4.10+; on older hosts `supports_min_release_age()` / `supports_postinstall_disable()` return `False` and explicit values are logged-and-ignored.
- Overrides: `install_args` is passed as Yarn package specs; unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: Yarn has no `--ignore-scripts` / `--minimum-release-age` CLI flags; the provider writes `npmMinimalAgeGate: 7d` (or whatever days value is configured) and `enableScripts: false` into `<yarn_prefix>/.yarnrc.yml` and additionally passes `--mode skip-build` to `yarn add` / `yarn up` when `postinstall_scripts=False`. Updates use `yarn up <pkg>` (Berry) or `yarn upgrade <pkg>` (classic). `YARN_GLOBAL_FOLDER` and `YARN_CACHE_FOLDER` are pointed at `cache_dir` so installs share a single cache across workspaces.

#### 🥖 [`BunProvider`](./abx_pkg/binprovider_bun.py) (`bun`)

Source: [`abx_pkg/binprovider_bun.py`](./abx_pkg/binprovider_bun.py) • Tests: [`tests/test_bunprovider.py`](./tests/test_bunprovider.py)

```python
INSTALLER_BIN = "bun"
PATH = ""                            # prepends <bun_prefix>/bin
bun_prefix = None                    # mirrors $BUN_INSTALL, None = ~/.bun (host-default)
cache_dir = user_cache_path("bun", "abx-pkg") or <system temp>/bun-cache
bun_install_args = []
```

- Install root: `bun_prefix=None` writes into the host `$BUN_INSTALL` (default `~/.bun`). Set `bun_prefix=Path(...)` or `install_root=Path(...)` to install under `<prefix>/bin`; the provider also creates `<prefix>/install/global` for the global `node_modules` dir, which is where bun puts the actual package state. The prefix bin dir becomes the provider's active executable search path.
- Auto-switching: none. Honors `BUN_BINARY=/abs/path/to/bun`.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`, and hydrates their provider defaults from `ABX_PKG_MIN_RELEASE_AGE` and `ABX_PKG_POSTINSTALL_SCRIPTS`. `min_release_age` requires Bun 1.3+, and `supports_min_release_age()` returns `False` on older hosts.
- Overrides: `install_args` is passed as Bun package specs; unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: install/update use `bun add -g` (with `--force` as the update fallback). The provider passes `--ignore-scripts` for `postinstall_scripts=False` and `--minimum-release-age=<seconds>` (Bun's unit is seconds; this provider converts from days). Explicit conflicting flags already present in `install_args` win over the derived defaults.

#### 🦕 [`DenoProvider`](./abx_pkg/binprovider_deno.py) (`deno`)

Source: [`abx_pkg/binprovider_deno.py`](./abx_pkg/binprovider_deno.py) • Tests: [`tests/test_denoprovider.py`](./tests/test_denoprovider.py)

```python
INSTALLER_BIN = "deno"
PATH = ""                            # prepends <deno_root>/bin
deno_root = None                     # mirrors $DENO_INSTALL_ROOT, None = ~/.deno
deno_dir = None                      # mirrors $DENO_DIR for cache isolation
cache_dir = user_cache_path("deno", "abx-pkg") or <system temp>/deno-cache
deno_install_args = ["--allow-all"]
deno_default_scheme = "npm"          # 'npm' or 'jsr'
```

- Install root: `deno_root=None` writes into the host `$DENO_INSTALL_ROOT` (default `~/.deno`). Set `deno_root=Path(...)` or `install_root=Path(...)` for a hermetic root with executables under `<deno_root>/bin`. Set `deno_dir=Path(...)` to also isolate the module cache.
- Auto-switching: none. Honors `DENO_BINARY=/abs/path/to/deno`.
- `dry_run`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False` / `True`, and hydrates their provider defaults from `ABX_PKG_MIN_RELEASE_AGE` and `ABX_PKG_POSTINSTALL_SCRIPTS`. `min_release_age` requires Deno 2.5+, and `supports_min_release_age()` returns `False` on older hosts.
- Overrides: `install_args` is passed as `deno install` package specs and is auto-prefixed with `npm:` (or `jsr:` if `deno_default_scheme="jsr"`) when an unqualified bare name is supplied. Already-qualified specs (`npm:`, `jsr:`, `https://...`) are passed through verbatim. Unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: install / update both run `deno install -g --force --allow-all -n <bin_name> <pkg>` because Deno's idiomatic update path is just a fresh global install. Deno's npm lifecycle scripts are *opt-in* (the opposite of npm), so the provider only adds `--allow-scripts` when `postinstall_scripts=True`. `min_release_age` is passed as `--minimum-dependency-age=<minutes>` (Deno's preferred unit; this provider converts from days). `DENO_TLS_CA_STORE=system` is set so installs work on hosts with corporate / sandboxed CA bundles.

#### 🧪 [`BashProvider`](./abx_pkg/binprovider_bash.py) (`bash`)

Source: [`abx_pkg/binprovider_bash.py`](./abx_pkg/binprovider_bash.py) • Tests: [`tests/test_bashprovider.py`](./tests/test_bashprovider.py)

```python
INSTALLER_BIN = "sh"
PATH = ""
bash_root = $ABX_PKG_BASH_ROOT or ~/.cache/abx-pkg/bash
bash_bin_dir = <bash_root>/bin
```

- Install root: set `bash_root` / `install_root` for the managed state dir, and `bash_bin_dir` / `bin_dir` for the executable output dir.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: this provider is driven by literal per-binary shell overrides for `install`, `update`, and `uninstall`.
- Notes: the provider exports `INSTALL_ROOT`, `BIN_DIR`, `BASH_INSTALL_ROOT`, and `BASH_BIN_DIR` into the shell environment for those commands.

#### 🦀 [`CargoProvider`](./abx_pkg/binprovider_cargo.py) (`cargo`)

Source: [`abx_pkg/binprovider_cargo.py`](./abx_pkg/binprovider_cargo.py) • Tests: [`tests/test_cargoprovider.py`](./tests/test_cargoprovider.py)

```python
INSTALLER_BIN = "cargo"
PATH = ""                            # prepends cargo_root/bin and cargo_home/bin
cargo_root = None                    # set this for hermetic installs
cargo_home = $CARGO_HOME or ~/.cargo
cargo_install_args = ["--locked"]
```

- Install root: set `cargo_root=Path(...)` or `install_root=Path(...)` for isolated installs under `<cargo_root>/bin`; otherwise installs go through `cargo_home`.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is passed to `cargo install`; `min_version` becomes `cargo install --version >=...`.
- Notes: the provider also sets `CARGO_HOME`, `CARGO_TARGET_DIR`, and `CARGO_INSTALL_ROOT` when applicable.

#### 💎 [`GemProvider`](./abx_pkg/binprovider_gem.py) (`gem`)

Source: [`abx_pkg/binprovider_gem.py`](./abx_pkg/binprovider_gem.py) • Tests: [`tests/test_gemprovider.py`](./tests/test_gemprovider.py)

```python
INSTALLER_BIN = "gem"
PATH = DEFAULT_ENV_PATH
gem_home = None                      # defaults to $GEM_HOME or ~/.local/share/gem
gem_bindir = None                    # defaults to <gem_home>/bin
gem_install_args = ["--no-document"]
```

- Install root: set `gem_home` or `install_root`, and optionally `gem_bindir` or `bin_dir`, for hermetic installs; otherwise it uses `$GEM_HOME` or `~/.local/share/gem`.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` maps to `gem install ...`, `gem update ...`, and `gem uninstall ...`; `min_version` becomes `--version >=...`.
- Notes: generated wrapper scripts are patched so they activate the configured `GEM_HOME` instead of the host default.

#### 🐹 [`GoGetProvider`](./abx_pkg/binprovider_goget.py) (`goget`)

Source: [`abx_pkg/binprovider_goget.py`](./abx_pkg/binprovider_goget.py) • Tests: [`tests/test_gogetprovider.py`](./tests/test_gogetprovider.py)

```python
INSTALLER_BIN = "go"
PATH = DEFAULT_ENV_PATH
gobin = None                         # defaults to <gopath>/bin
gopath = $GOPATH or ~/go
go_install_args = []
```

- Install root: set `gopath` or `install_root` for the Go workspace, and `gobin` or `bin_dir` for the executable dir; otherwise installs land in `<gopath>/bin`.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is passed to `go install ...`; the default is `["<bin_name>@latest"]`.
- Notes: `update()` is just `install()` again. Version detection prefers `go version -m <binary>` and falls back to the generic version probe. The provider name is `goget`, not `go_get`.

#### ❄️ [`NixProvider`](./abx_pkg/binprovider_nix.py) (`nix`)

Source: [`abx_pkg/binprovider_nix.py`](./abx_pkg/binprovider_nix.py) • Tests: [`tests/test_nixprovider.py`](./tests/test_nixprovider.py)

```python
INSTALLER_BIN = "nix"
PATH = ""                            # prepends <nix_profile>/bin
nix_profile = $ABX_PKG_NIX_PROFILE or ~/.nix-profile
nix_state_dir = None                 # optional XDG state/cache isolation
nix_install_args = [
    "--extra-experimental-features", "nix-command",
    "--extra-experimental-features", "flakes",
]
```

- Install root: set `nix_profile=Path(...)` or `install_root=Path(...)` for a custom profile; add `nix_state_dir=Path(...)` to isolate state/cache paths too.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is passed to `nix profile install ...`; default is `["nixpkgs#<bin_name>"]`.
- Notes: update/uninstall operate on the resolved profile element name rather than reusing the full flake ref.

#### 🐳 [`DockerProvider`](./abx_pkg/binprovider_docker.py) (`docker`)

Source: [`abx_pkg/binprovider_docker.py`](./abx_pkg/binprovider_docker.py) • Tests: [`tests/test_dockerprovider.py`](./tests/test_dockerprovider.py)

```python
INSTALLER_BIN = "docker"
PATH = ""                            # prepends docker_shim_dir
docker_shim_dir = ($ABX_PKG_DOCKER_ROOT or ~/.cache/abx-pkg/docker) / "bin"
docker_run_args = ["--rm", "-i"]
```

- Install root: **partial only**. Images are pulled into Docker's host-managed image store; the provider only controls the local shim dir and metadata dir. Use `install_root=Path(...)` for the shim/metadata root or `bin_dir=Path(...)` for the shim dir directly.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is a list of Docker image refs. The first item is treated as the main image and becomes the generated shim target.
- Notes: default install args are `["<bin_name>:latest"]`. `install()` / `update()` run `docker pull`, write metadata JSON, and create an executable wrapper that runs `docker run ...`.

#### 🧩 [`ChromeWebstoreProvider`](./abx_pkg/binprovider_chromewebstore.py) (`chromewebstore`)

Source: [`abx_pkg/binprovider_chromewebstore.py`](./abx_pkg/binprovider_chromewebstore.py) • Tests: [`tests/test_chromewebstoreprovider.py`](./tests/test_chromewebstoreprovider.py)

```python
INSTALLER_BIN = "node"
PATH = ""
extensions_root = $ABX_PKG_CHROMEWEBSTORE_ROOT or ~/.cache/abx-pkg/chromewebstore
extensions_dir = <extensions_root>/extensions
```

- Install root: set `extensions_root` / `install_root` for the managed extension cache root, and `extensions_dir` / `bin_dir` for the unpacked extension output dir.
- Auto-switching: none.
- `dry_run`: shared behavior.
- Security: `min_release_age` is unsupported and is ignored with a warning if explicitly requested. `postinstall_scripts=False` is supported as a standard kwarg and `ABX_PKG_POSTINSTALL_SCRIPTS` hydrates the provider default here, but there is no extra install-time toggle beyond the packaged JS runtime path this provider already uses.
- Overrides: `install_args` are `[webstore_id, "--name=<extension_name>"]`.
- Notes: the packaged JS runtime under `abx_pkg/js/chrome/` is used to download, unpack, and cache the extension, and the resolved binary path is the unpacked `manifest.json`.

#### 🎭 [`PuppeteerProvider`](./abx_pkg/binprovider_puppeteer.py) (`puppeteer`)

Source: [`abx_pkg/binprovider_puppeteer.py`](./abx_pkg/binprovider_puppeteer.py) • Tests: [`tests/test_puppeteerprovider.py`](./tests/test_puppeteerprovider.py)

```python
INSTALLER_BIN = "puppeteer-browsers"
PATH = ""
puppeteer_root = $ABX_PKG_PUPPETEER_ROOT or ~/.cache/abx-pkg/puppeteer
browser_bin_dir = <puppeteer_root>/bin
browser_cache_dir = <puppeteer_root>/cache
```

- Install root: set `puppeteer_root` / `install_root` for the managed root, `browser_bin_dir` / `bin_dir` for symlinked executables, and `browser_cache_dir` for downloaded browser artifacts.
- Auto-switching: bootstraps `@puppeteer/browsers` through `NpmProvider` and then uses that CLI for browser installs.
- `dry_run`: shared behavior.
- Security: `min_release_age` is unsupported for browser installs and is ignored with a warning if explicitly requested. `postinstall_scripts=False` is supported for the underlying npm bootstrap path, and `ABX_PKG_POSTINSTALL_SCRIPTS` hydrates the provider default here.
- Overrides: `install_args` are passed through to `@puppeteer/browsers install ...`, with the provider appending its managed `--path=<cache_dir>`.
- Notes: installed-browser resolution uses semantic version ordering, not lexicographic string sorting.

#### 🎬 [`PlaywrightProvider`](./abx_pkg/binprovider_playwright.py) (`playwright`)

Source: [`abx_pkg/binprovider_playwright.py`](./abx_pkg/binprovider_playwright.py) • Tests: [`tests/test_playwrightprovider.py`](./tests/test_playwrightprovider.py)

```python
INSTALLER_BIN = "playwright"
PATH = ""
playwright_root = None           # when set, doubles as PLAYWRIGHT_BROWSERS_PATH
browser_bin_dir = <playwright_root>/bin  # symlink dir for resolved browsers
playwright_install_args = ["--with-deps"]
euid = 0                         # routes exec() through sudo-first-then-fallback
```

- Install root: set `playwright_root` / `install_root` to pin both the abx-pkg managed root AND `PLAYWRIGHT_BROWSERS_PATH` to the same directory. Leave it unset to let playwright use its own OS-default browsers path (`~/.cache/ms-playwright` on Linux etc.) — in that case abx-pkg maintains no managed symlink dir or npm prefix at all, the `playwright` npm CLI bootstraps against the host's npm default, and `load()` returns the resolved `executablePath()` directly. `browser_bin_dir` / `bin_dir` overrides the symlink directory when `playwright_root` is pinned.
- Auto-switching: bootstraps the `playwright` npm package through `NpmProvider`, then runs `playwright install --with-deps <install_args>` against it. Resolves each installed browser's real executable via the `playwright-core` Node.js API (`chromium.executablePath()` etc.) and writes a symlink into `bin_dir` when one is configured.
- `dry_run`: shared behavior — the install handler short-circuits to a placeholder without touching the host.
- Privilege handling: `--with-deps` installs system packages and requires root on Linux. ``euid`` defaults to ``0``, which routes every ``exec()`` call through the base ``BinProvider.exec`` sudo-first-then-fallback path — it tries ``sudo -n -- playwright install --with-deps ...`` first on non-root hosts, falls back to running the command directly if sudo fails or isn't available, and merges both stderr outputs into the final error if both attempts fail.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported for browser installs and are ignored with a warning if explicitly requested.
- Overrides: `install_args` are appended onto `playwright install` after `playwright_install_args` (defaults to `["--with-deps"]`) and passed through verbatim — use whatever browser names / flags the `playwright install` CLI accepts (`chromium`, `firefox`, `webkit`, `--no-shell`, `--only-shell`, `--force`, etc.).
- Notes: `update()` bumps the managed `playwright` npm package first (via `NpmProvider.update`) so its pinned browser versions refresh, then re-runs `playwright install --force <install_args>` to pull any new browser builds. `uninstall()` removes the relevant `<bin_name>-*/` directories from `playwright_root` alongside the bin-dir symlink, since `playwright uninstall` only drops *unused* browsers on its own. Both `update()` and `uninstall()` leave playwright's OS-default cache untouched when `playwright_root` is unset.

#### 🛠️ [`PyinfraProvider`](./abx_pkg/binprovider_pyinfra.py) (`pyinfra`)

Source: [`abx_pkg/binprovider_pyinfra.py`](./abx_pkg/binprovider_pyinfra.py) • Tests: [`tests/test_pyinfraprovider.py`](./tests/test_pyinfraprovider.py)

```python
INSTALLER_BIN = "pyinfra"
PATH = os.environ.get("PATH", DEFAULT_PATH)
pyinfra_installer_module = "auto"
pyinfra_installer_kwargs = {}
```

- Install root: **no hermetic prefix support**. It delegates to host package managers through pyinfra operations.
- Auto-switching: `installer_module="auto"` resolves to `operations.brew.packages` on macOS and `operations.server.packages` on Linux.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` is the package list passed to the selected pyinfra operation.
- Notes: privilege requirements depend on the underlying package manager and selected module. When pyinfra tries a privileged sudo path and then falls back, both error outputs are preserved if the final attempt also fails.

#### 📘 [`AnsibleProvider`](./abx_pkg/binprovider_ansible.py) (`ansible`)

Source: [`abx_pkg/binprovider_ansible.py`](./abx_pkg/binprovider_ansible.py) • Tests: [`tests/test_ansibleprovider.py`](./tests/test_ansibleprovider.py)

```python
INSTALLER_BIN = "ansible"
PATH = os.environ.get("PATH", DEFAULT_PATH)
ansible_installer_module = "auto"
ansible_playbook_template = ANSIBLE_INSTALL_PLAYBOOK_TEMPLATE
```

- Install root: **no hermetic prefix support**. It delegates to the host via `ansible-runner`.
- Auto-switching: `installer_module="auto"` resolves to `community.general.homebrew` on macOS and `ansible.builtin.package` on Linux.
- `dry_run`: shared behavior.
- Security: `min_release_age` and `postinstall_scripts=False` are unsupported and are ignored with a warning if explicitly requested.
- Overrides: `install_args` becomes the playbook loop input for the chosen Ansible module.
- Notes: when using the Homebrew module, the provider auto-injects the detected brew search path into module kwargs. Privilege requirements still come from the underlying package manager, and failed sudo attempts are included in the final error if the fallback attempt also fails.

### [`Binary`](https://github.com/ArchiveBox/abx-pkg/blob/main/abx_pkg/binary.py#:~:text=class%20Binary)

This type represents a single binary dependency aka a package (e.g. `wget`, `curl`, `ffmpeg`, etc.).  
It can define one or more `BinProvider`s that it supports, along with overrides to customize the behavior for each.

`Binary`s implement the following interface:
- `load()`, `install()`, `update()`, `uninstall()`, `load_or_install()` `->` `Binary`
- `binproviders`
- `binprovider` / `loaded_binprovider`
- `abspath` / `loaded_abspath`
- `abspaths` / `loaded_abspaths`
- `version` / `loaded_version`
- `sha256` / `loaded_sha256`

`Binary.install()` and `Binary.update()` return a fresh loaded `Binary`.
`Binary.uninstall()` returns a `Binary` with `binprovider`, `abspath`, `version`, and `sha256` cleared after removal.
`Binary.load()`, `Binary.install()`, `Binary.load_or_install()`, and `Binary.update()` all enforce `min_version` consistently.

```python
from pydantic import InstanceOf
from abx_pkg import BinProvider, Binary, BinProviderName, BinName, HandlerDict, SemVer, BrewProvider
from abx_pkg import env, pip, apt

class CustomBrewProvider(BrewProvider):
    name: BinProviderName = 'custom_brew'

    def get_macos_packages(self, bin_name: str, **context) -> list[str]:
        return ['yt-dlp'] if bin_name == 'ytdlp' else [bin_name]

# Example: Create a reusable class defining a binary and its providers
class YtdlpBinary(Binary):
    name: BinName = 'ytdlp'
    description: str = 'YT-DLP (Replacement for YouTube-DL) Media Downloader'

    # define the providers this binary supports
    binproviders: list[InstanceOf[BinProvider]] = [env, pip, apt, CustomBrewProvider()]
    
    # customize installed package names for specific package managers
    overrides: dict[BinProviderName, HandlerDict] = {
        'pip': {'install_args': ['yt-dlp[default,curl-cffi]']}, # can use literal values (install_args -> list[str], version -> SemVer, abspath -> Path, install -> str log)
        'apt': {'install_args': lambda: ['yt-dlp', 'ffmpeg']},  # also accepts any pure Callable that returns a list of packages
        'custom_brew': {'install_args': 'self.get_macos_packages'},    # also accepts string reference to function on self (where self is the BinProvider)
    }


ytdlp = YtdlpBinary().load_or_install()
print(ytdlp.binprovider)                  # EnvProvider(...) / PipProvider(...) / AptProvider(...) / CustomBrewProvider(...)
print(ytdlp.abspath)                      # Path(...)
print(ytdlp.abspaths)                     # {'env': [Path(...)], 'custom_brew': [Path(...)]}
print(ytdlp.version)                      # SemVer(...)
print(ytdlp.sha256)                       # '<sha256>'
print(ytdlp.is_valid)                     # True

# Lifecycle actions preserve the Binary type and refresh/clear loaded metadata as needed
ytdlp = ytdlp.update()
assert ytdlp.is_valid
ytdlp = ytdlp.uninstall()
assert ytdlp.abspath is None and ytdlp.version is None
```

```python
import os
import platform
from pydantic import InstanceOf
from abx_pkg import BinProvider, Binary, BinProviderName, BinName, HandlerDict, SemVer
from abx_pkg import env, apt

# Example: Create a binary that uses Podman if available, or Docker otherwise
class DockerBinary(Binary):
    name: BinName = 'docker'

    # define the providers this binary supports
    binproviders: list[InstanceOf[BinProvider]] = [env, apt]
    
    overrides: dict[BinProviderName, HandlerDict] = {
        'env': {
            # example: prefer podman if installed (falling back to docker)
            'abspath': lambda: os.which('podman') or os.which('docker') or os.which('docker-ce'),
        },
        'apt': {
            # example: vary installed package name based on your CPU architecture
            'install_args': {
                'amd64': ['docker'],
                'armv7l': ['docker-ce'],
                'arm64': ['docker-ce'],
            }.get(platform.machine(), 'docker'),
        },
    }

docker = DockerBinary().load_or_install()
print(docker.binprovider)                 # EnvProvider(...) / AptProvider(...)
print(docker.abspath)                     # Path(...)
print(docker.abspaths)                    # {'env': [Path(...)], ...}
print(docker.version)                     # SemVer(...)
print(docker.is_valid)                    # True

# You can also seed loaded field values at construction time,
# e.g. if you want to point at a specific existing binary path:
custom_docker = DockerBinary(abspath='~/custom/bin/podman').load()
print(custom_docker.name)                 # 'docker'
print(custom_docker.binprovider)          # EnvProvider(...) / AptProvider(...)
print(custom_docker.abspath)              # Path(...)
print(custom_docker.version)              # SemVer(...)
print(custom_docker.is_valid)             # True
```

### [`SemVer`](https://github.com/ArchiveBox/abx-pkg/blob/main/abx_pkg/semver.py#:~:text=class%20SemVer)

```python
from abx_pkg import SemVer

### Example: Use the SemVer type directly for parsing & verifying version strings
SemVer.parse('Google Chrome 124.0.6367.208+beta_234. 234.234.123')  # SemVer(124, 0, 6367)
SemVer.parse('2024.04.05')                                          # SemVer(2024, 4, 5)
SemVer.parse('1.9+beta')                                            # SemVer(1, 9, 0)
str(SemVer(1, 9, 0))                                                # '1.9.0'
```
<br/>

> These types are all meant to be used library-style to make writing your own apps easier.  
> e.g. you can use it to build things like [`playwright install --with-deps`](https://playwright.dev/docs/browsers#install-system-dependencies).


<br/>

---
---

<br/>
<br/>

## Development

`abx-pkg` uses `uv` for local development, dependency sync, linting, and tests.

```bash
# create/update the local env with dev deps
uv sync --all-extras --all-groups

# run formatting/lint/type checks
uv run prek run --all-files

# run the full test suite from tests/
uv run pytest -sx tests/

# build distributions
uv build
```

- Tests now live under [`tests/`](./tests/).
- Use `uv run pytest -sx tests/test_npmprovider.py` or a specific node like `uv run pytest -sx tests/test_npmprovider.py::TestNpmProvider::test_provider_dry_run_does_not_install_zx` when iterating on one provider.

<br/>
<br/>


<details>
<summary><strong>Django Usage</strong></summary>

With a few more packages, you get type-checked Django fields & forms that support `BinProvider` and `Binary`.

> [!TIP]
> For the full Django experience, we recommend installing these 3 excellent packages:
> - [`django-admin-data-views`](https://github.com/MrThearMan/django-admin-data-views)
> - [`django-pydantic-field`](https://github.com/surenkov/django-pydantic-field)
> - [`django-jsonform`](https://django-jsonform.readthedocs.io/)
> `pip install abx-pkg django-admin-data-views django-pydantic-field django-jsonform`

### Django Model Usage: Store `BinProvider` and `Binary` entries in your model fields

```bash
pip install django-pydantic-field
```

*For more info see the [`django-pydantic-field`](https://github.com/surenkov/django-pydantic-field) docs...*

Example Django `models.py` showing how to store `Binary` and `BinProvider` instances in DB fields:
```python
from django.db import models
from abx_pkg import BinProvider, Binary, SemVer
from django_pydantic_field import SchemaField

class Dependency(models.Model):
    label = models.CharField(max_length=63)
    default_binprovider: BinProvider = SchemaField()
    binaries: list[Binary] = SchemaField(default=[])
    min_version: SemVer = SchemaField(default=(0, 0, 1))
```

And here's how to save a `Binary` using the example model:
```python
from abx_pkg import Binary, SemVer, env

# find existing curl Binary in $PATH
curl = Binary(name='curl').load()

# save it to the DB using our new model
obj = Dependency(
    label='runtime tools',
    default_binprovider=env,                      # store BinProvider values directly
    binaries=[curl],                              # store Binary/SemVer values directly
    min_version=SemVer('6.5.0'),
)
obj.save()
```

When fetching it back from the DB, the `Binary` field is auto-deserialized / immediately usable:
```
obj = Dependency.objects.get(label='runtime tools')    # everything is transparently serialized to/from the DB,
                                                        # and is ready to go immediately after querying:
assert obj.binaries[0].abspath == curl.abspath
print(obj.binaries[0].abspath)                         #   Path('/usr/local/bin/curl')
obj.binaries[0].exec(cmd=['--version'])               #   curl 7.81.0 (x86_64-apple-darwin23.0) libcurl/7.81.0 ...
```
*For a full example see our provided [`django_example_project/`](https://github.com/ArchiveBox/abx-pkg/tree/main/django_example_project)...*

### Django Admin Usage: Display `Binary` objects nicely in the Admin UI

<img height="220" alt="Django Admin binaries list view" src="https://github.com/ArchiveBox/abx-pkg/assets/511499/a9980217-f39e-434e-b266-20cd6feb17c3" align="top"><img height="220" alt="Django Admin binaries detail view" src="https://github.com/ArchiveBox/abx-pkg/assets/511499/d4d9086e-c8f4-4b6e-8ee8-8c8a864715b0" align="top">

```bash
pip install abx-pkg django-admin-data-views
```
*For more info see the [`django-admin-data-views`](https://github.com/MrThearMan/django-admin-data-views) docs...*

Then add this to your `settings.py`:
```python
INSTALLED_APPS = [
    # ...
    'admin_data_views',
    'abx_pkg',
    # ...
]

# point these to a function that gets the list of all binaries / a single binary
ABX_PKG_GET_ALL_BINARIES = 'project.views.get_all_binaries'
ABX_PKG_GET_BINARY = 'project.views.get_binary'

ADMIN_DATA_VIEWS = {
    "NAME": "Environment",
    "URLS": [
        {
            "route": "binaries/",
            "view": "abx_pkg.views.binaries_list_view",
            "name": "binaries",
            "items": {
                "route": "<str:key>/",
                "view": "abx_pkg.views.binary_detail_view",
                "name": "binary",
            },
        },
        # Coming soon: binprovider_list_view + binprovider_detail_view ...
    ],
}
```
*For a full example see our provided [`django_example_project/`](https://github.com/ArchiveBox/abx-pkg/tree/main/django_example_project)...*

If you override the default site admin, you must register the views manually:

```python
class YourSiteAdmin(admin.AdminSite):
    """Your customized version of admin.AdminSite"""
    ...

custom_admin = YourSiteAdmin()
custom_admin.register(get_user_model())
...
from abx_pkg.admin import register_admin_views
register_admin_views(custom_admin)
```

### ~~Django Admin Usage: JSONFormWidget for editing `BinProvider` and `Binary` data~~

<img src="https://github.com/ArchiveBox/abx-pkg/assets/511499/63705a57-4f62-4dbe-9f3a-0515323d8b5e" width="600px"/>

> [!IMPORTANT]
> This feature is coming soon but is blocked on a few issues being fixed first:
> - https://github.com/surenkov/django-pydantic-field/issues/64
> - https://github.com/surenkov/django-pydantic-field/issues/65
> - https://github.com/surenkov/django-pydantic-field/issues/66

~~Install `django-jsonform` to get auto-generated Forms for editing BinProvider, Binary, etc. data~~
```bash
pip install django-pydantic-field django-jsonform
```
*For more info see the [`django-jsonform`](https://django-jsonform.readthedocs.io/) docs...*

`admin.py`:
```python
from django.contrib import admin
from django_jsonform.widgets import JSONFormWidget
from django_pydantic_field.v2.fields import PydanticSchemaField

class MyModelAdmin(admin.ModelAdmin):
    formfield_overrides = {PydanticSchemaField: {"widget": JSONFormWidget}}

admin.site.register(MyModel, MyModelAdmin)
```

*For a full example see our provided [`django_example_project/`](https://github.com/ArchiveBox/abx-pkg/tree/main/django_example_project)...*

</details>

<br/>

---

<br/>


## Logging

`abx-pkg` uses the standard Python `logging` module. By default it stays quiet unless your application configures logging explicitly.

```python
import logging
from abx_pkg import Binary, env, configure_logging

configure_logging(logging.INFO)

python = Binary(name='python', binproviders=[env]).load()
```

To enable Rich logging:

```bash
pip install "abx-pkg[rich]"
```

```python
import logging
from abx_pkg import Binary, EnvProvider, configure_rich_logging

configure_rich_logging(logging.DEBUG)

python = Binary(name='python', binproviders=[EnvProvider()]).load()
```

Debug logging is hardened so logging itself does not become the failure. If a provider/model object has a broken or overly-expensive `repr()`, `abx-pkg` falls back to a short `ClassName(...)` summary instead of raising while formatting log output.

`configure_rich_logging(...)` uses `rich.logging.RichHandler` under the hood, so log levels, paths, arguments, and command lines render with terminal colors when supported.

You can also manage it with standard logging primitives:

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("abx_pkg").setLevel(logging.DEBUG)
```

## Examples

### Advanced: Implement your own package manager behavior by subclassing BinProvider

```python
from pathlib import Path
from abx_pkg import (
    BinProvider,
    BinProviderName,
    BinName,
    HostBinPath,
    InstallArgs,
    SemVer,
    bin_abspath,
    bin_version,
)

class CargoProvider(BinProvider):
    name: BinProviderName = 'cargo'
    INSTALLER_BIN: BinName = 'cargo'
    PATH = str(Path.home() / '.cargo/bin')

    def default_install_args_handler(self, bin_name: BinName, **context) -> InstallArgs:
        return [bin_name]

    def default_install_handler(
        self,
        bin_name: BinName,
        install_args: InstallArgs | None = None,
        postinstall_scripts: bool | None = None,
        min_release_age: float | None = None,
        min_version: SemVer | None = None,
        timeout: int | None = None,
    ) -> str:
        install_args = install_args or self.get_install_args(bin_name)
        installer = self.INSTALLER_BIN_ABSPATH
        assert installer
        proc = self.exec(installer, cmd=['install', *install_args], timeout=timeout)
        assert proc.returncode == 0
        return proc.stdout.strip() or proc.stderr.strip()

    def default_abspath_handler(self, bin_name: BinName, **context) -> HostBinPath | None:
        return bin_abspath(bin_name, PATH=self.PATH)

    def default_version_handler(
        self,
        bin_name: BinName,
        abspath: HostBinPath | None = None,
        timeout: int | None = None,
        **context,
    ) -> SemVer | None:
        return self._version_from_exec(bin_name, abspath=abspath, timeout=timeout)

cargo = CargoProvider()
rg = cargo.install(bin_name='ripgrep')
print(rg.binprovider)                   # CargoProvider(...)
print(rg.version)                       # SemVer(...)
```


<br/>

---

<br/>

*Note:* this package used to be called `pydantic-pkgr`, it was renamed to `abx-pkg` on 2024-11-12.

### TODO

- [x] Implement initial basic support for `apt`, `brew`, and `pip`
- [x] Provide editability and actions via Django Admin UI using [`django-pydantic-field`](https://github.com/surenkov/django-pydantic-field) and [`django-jsonform`](https://django-jsonform.readthedocs.io/en/latest/)
- [ ] Add `preinstall` and `postinstall` hooks for things like adding `apt` sources and running cleanup scripts
- [ ] Implement more package managers (`apk`, `ppm`, `pkg`, etc.)


### Other Packages We Like

- https://github.com/MrThearMan/django-signal-webhooks
- https://github.com/MrThearMan/django-admin-data-views
- https://github.com/lazybird/django-solo
- https://github.com/joshourisman/django-pydantic-settings
- https://github.com/surenkov/django-pydantic-field
- https://github.com/jordaneremieff/djantic

[coverage-badge]: https://coveralls.io/repos/github/ArchiveBox/abx-pkg/badge.svg?branch=main
[status-badge]: https://img.shields.io/github/actions/workflow/status/ArchiveBox/abx-pkg/test.yml?branch=main
[pypi-badge]: https://img.shields.io/pypi/v/abx-pkg?v=1
[licence-badge]: https://img.shields.io/github/license/ArchiveBox/abx-pkg?v=1
[repo-badge]: https://img.shields.io/github/last-commit/ArchiveBox/abx-pkg?v=1
[issues-badge]: https://img.shields.io/github/issues-raw/ArchiveBox/abx-pkg?v=1
[version-badge]: https://img.shields.io/pypi/pyversions/abx-pkg?v=1
[downloads-badge]: https://img.shields.io/pypi/dm/abx-pkg?v=1
[django-badge]: https://img.shields.io/pypi/djversions/abx-pkg?v=1

[coverage]: https://coveralls.io/github/ArchiveBox/abx-pkg?branch=main
[status]: https://github.com/ArchiveBox/abx-pkg/actions/workflows/test.yml
[pypi]: https://pypi.org/project/abx-pkg
[licence]: https://github.com/ArchiveBox/abx-pkg/blob/main/LICENSE
[repo]: https://github.com/ArchiveBox/abx-pkg/commits/main
[issues]: https://github.com/ArchiveBox/abx-pkg/issues
