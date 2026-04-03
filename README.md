<h1><a href="https://github.com/ArchiveBox/abx-pkg"><code>abx-pkg</code></a> &nbsp; &nbsp; &nbsp; &nbsp; 📦  <small><code>apt</code>&nbsp; <code>brew</code>&nbsp; <code>pip</code>&nbsp; <code>npm</code>&nbsp; <code>cargo</code>&nbsp; <code>gem</code>&nbsp; <code>goget</code>&nbsp; <code>nix</code>&nbsp; <code>docker</code> &nbsp;₊₊₊</small><br/><sub>Simple Python interfaces for package managers + installed binaries.</sub></h1>
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

### Supported Package Managers

**So far it supports `installing`/`finding installed`/`updating`/`removing` packages on `Linux`/`macOS` with:**

- `apt` (Ubuntu/Debian/etc.)
- `brew` (macOS/Linux)
- `pip` (Linux/macOS)
- `npm` (Linux/macOS)
- `cargo` (Linux/macOS)
- `gem` (Linux/macOS)
- `goget` (Linux/macOS, via [`GoGetProvider`](./abx_pkg/binprovider_goget.py))
- `nix` (Linux/macOS)
- `docker` (Linux/macOS, using local wrapper scripts that run `docker run`)
- `env` (looks for existing version of binary in user's `$PATH` at runtime)

*Planned:* `apk`, `pkg`, and additional future provider backends.

`DockerProvider` expects image refs as install args, typically via overrides on a `Binary`. It writes a local wrapper script for the binary and executes it via `docker run ...`; the binary version is parsed from the image tag, so semver-like tags work best.

`NpmProvider` prefers a real `npm` executable when both `npm` and `pnpm` are installed. If `npm` is unavailable, it can still drive installs and metadata lookups through `pnpm` using the same provider API.

---


## Usage

```bash
pip install abx-pkg
```

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

**Built-in implementations:** `EnvProvider`, `AptProvider`, `BrewProvider`, `PipProvider`, `NpmProvider`, `CargoProvider`, `GemProvider`, `GoGetProvider`, `NixProvider`, `DockerProvider`, `PyinfraProvider`, `AnsibleProvider`

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
postinstall_scripts = False          # or ABX_PKG_POSTINSTALL_SCRIPTS=1
min_release_age = 7.0                # or ABX_PKG_MIN_RELEASE_AGE=<days>
install_timeout = 120
version_timeout = 10
DRY_RUN = False                      # or DRY_RUN=1
```

- `DRY_RUN`: use `provider.get_provider_with_overrides(dry_run=True)` or `DRY_RUN=1`. Provider subprocesses are logged and skipped, `install()` / `update()` return a placeholder loaded binary, and `uninstall()` returns `True` without mutating the host.
- `install_timeout`: shared provider-level timeout used by `install()`, `update()`, and `uninstall()` handler execution paths.
- `version_timeout`: shared provider-level timeout used by version / metadata probes such as `--version`, `npm show`, `npm list`, `pip show`, `go version -m`, and brew lookups.
- Security controls fail closed: if `min_release_age > 0` or `postinstall_scripts=False` is requested on a provider that does not support that control for that action, `install()` / `update()` raises instead of silently ignoring it.

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
- Security: accepts `min_release_age` / `postinstall_scripts`, but they are effectively irrelevant because install / update are no-ops.
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
- `DRY_RUN`: shared behavior.
- Security: **no `min_release_age` enforcement** and **no postinstall-script disabling**.
- Overrides: in the direct shell fallback, `install_args` becomes `apt-get install -y -qq --no-install-recommends ...`; `update()` uses `apt-get install --only-upgrade ...`.
- Notes: direct mode runs `apt-get update -qq` at most once per day and requires root on Linux.

#### 🍺 [`BrewProvider`](./abx_pkg/binprovider_brew.py) (`brew`)

Source: [`abx_pkg/binprovider_brew.py`](./abx_pkg/binprovider_brew.py) • Tests: [`tests/test_brewprovider.py`](./tests/test_brewprovider.py)

```python
INSTALLER_BIN = "brew"
PATH = "/home/linuxbrew/.linuxbrew/bin:/opt/homebrew/bin:/usr/local/bin"
brew_prefix = guessed host prefix    # /opt/homebrew, /usr/local, or linuxbrew
```

- Install root: `brew_prefix` is for discovery only. `install_root=...` aliases to `brew_prefix`. **This provider does not create an isolated custom Homebrew prefix.**
- Auto-switching: if `postinstall_scripts=True`, it prefers `PyinfraProvider` and then `AnsibleProvider`; otherwise it falls back to direct `brew`.
- `DRY_RUN`: shared behavior.
- Security: **`min_release_age` is unsupported**. `postinstall_scripts=False` is supported for direct brew via `--skip-post-install`.
- Overrides: in the direct shell fallback, `install_args` maps to formula / cask args passed to `brew install`, `brew upgrade`, and `brew uninstall`.
- Notes: direct mode runs `brew update` at most once per day. The test suite verifies that unsupported `min_release_age` fails closed.

#### 🐍 [`PipProvider`](./abx_pkg/binprovider_pip.py) (`pip`)

Source: [`abx_pkg/binprovider_pip.py`](./abx_pkg/binprovider_pip.py) • Tests: [`tests/test_pipprovider.py`](./tests/test_pipprovider.py), [`tests/test_security_controls.py`](./tests/test_security_controls.py)

```python
INSTALLER_BIN = "pip"
PATH = ""                            # auto-built from global/user Python bin dirs
pip_venv = None                      # set this for hermetic installs
cache_dir = user_cache_path("pip", "abx-pkg") or /tmp/pip-cache
pip_install_args = ["--no-input", "--disable-pip-version-check", "--quiet"]
pip_bootstrap_packages = ["pip", "setuptools", "uv"]
```

- Install root: `pip_venv=None` uses the system/user Python environment. Set `pip_venv=Path(...)` or `install_root=Path(...)` for a hermetic venv rooted at `<pip_venv>/bin`, and that venv bin dir becomes the provider's active executable search path.
- Auto-switching: the provider executable is still `pip`, but install / update / show / uninstall calls use `uv pip ...` when `uv` is available and `PIP_BINARY` is not forcing a specific pip path.
- `DRY_RUN`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`.
- Overrides: `install_args` is passed as pip requirement specs; unpinned specs get a `>=min_version` floor when `min_version` is supplied.
- Notes: `postinstall_scripts=False` uses `uv pip --no-build` or plain `pip --only-binary :all:`. `min_release_age` is enforced with `uv --exclude-newer=<cutoff>` or plain `pip --uploaded-prior-to=<cutoff>` when the host pip is new enough. Explicit conflicting flags already present in `install_args` win over the derived defaults.

#### 📦 [`NpmProvider`](./abx_pkg/binprovider_npm.py) (`npm`)

Source: [`abx_pkg/binprovider_npm.py`](./abx_pkg/binprovider_npm.py) • Tests: [`tests/test_npmprovider.py`](./tests/test_npmprovider.py), [`tests/test_security_controls.py`](./tests/test_security_controls.py)

```python
INSTALLER_BIN = "npm"
PATH = ""                            # auto-built from npm/pnpm local + global bin dirs
npm_prefix = None                    # None = global install, Path(...) = hermetic-ish prefix
cache_dir = user_cache_path("npm", "abx-pkg") or /tmp/npm-cache
npm_install_args = ["--force", "--no-audit", "--no-fund", "--loglevel=error"]
```

- Install root: `npm_prefix=None` installs globally. Set `npm_prefix=Path(...)` or `install_root=Path(...)` to install under `<prefix>/node_modules/.bin`; that prefix bin dir becomes the provider's active executable search path.
- Auto-switching: prefers a real `npm` binary, falls back to `pnpm` if `npm` is unavailable, and honors `NPM_BINARY=/abs/path/to/npm-or-pnpm`.
- `DRY_RUN`: shared behavior.
- Security: supports both `min_release_age` and `postinstall_scripts=False`.
- Overrides: `install_args` is passed as npm package specs; unpinned specs get rewritten to `pkg@>=<min_version>` when `min_version` is supplied.
- Notes: direct npm mode uses `--ignore-scripts` and `--min-release-age=<days>`. pnpm mode writes `pnpm-workspace.yaml` with `minimumReleaseAge`; that is how release-age enforcement is configured there. Explicit conflicting flags already present in `install_args` win over the derived defaults.

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
- `DRY_RUN`: shared behavior.
- Security: **no `min_release_age` enforcement** and **no postinstall-script disabling**.
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
- `DRY_RUN`: shared behavior.
- Security: **no `min_release_age` enforcement** and **no postinstall-script disabling**.
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
- `DRY_RUN`: shared behavior.
- Security: **no `min_release_age` enforcement** and **no postinstall-script disabling**.
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
- `DRY_RUN`: shared behavior.
- Security: **no `min_release_age` enforcement** and **no postinstall-script disabling**.
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
- `DRY_RUN`: shared behavior.
- Security: **no `min_release_age` enforcement** and **no postinstall-script disabling**.
- Overrides: `install_args` is a list of Docker image refs. The first item is treated as the main image and becomes the generated shim target.
- Notes: default install args are `["<bin_name>:latest"]`. `install()` / `update()` run `docker pull`, write metadata JSON, and create an executable wrapper that runs `docker run ...`.

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
- `DRY_RUN`: shared behavior.
- Security: **no `min_release_age` enforcement** and **no postinstall-script disabling**.
- Overrides: `install_args` is the package list passed to the selected pyinfra operation.
- Notes: privilege requirements depend on the underlying package manager and selected module.

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
- `DRY_RUN`: shared behavior.
- Security: **no `min_release_age` enforcement** and **no postinstall-script disabling**.
- Overrides: `install_args` becomes the playbook loop input for the chosen Ansible module.
- Notes: when using the Homebrew module, the provider auto-injects the detected brew search path into module kwargs. Privilege requirements still come from the underlying package manager.

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


## Django Usage

With a few more packages, you get type-checked Django fields & forms that support `BinProvider` and `Binary`.

> [!TIP]
> For the full Django experience, we recommend installing these 3 excellent packages:
> - [`django-admin-data-views`](https://github.com/MrThearMan/django-admin-data-views)
> - [`django-pydantic-field`](https://github.com/surenkov/django-pydantic-field)
> - [`django-jsonform`](https://django-jsonform.readthedocs.io/)  
> `pip install abx-pkg django-admin-data-views django-pydantic-field django-jsonform`

<br/>

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

<br/>

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

<details>
<summary><i>Note: If you override the default site admin, you must register the views manually...</i></summary>
<br/><br/>
<b><code>admin.py</code>:</b>
<br/>
<pre><code>
class YourSiteAdmin(admin.AdminSite):
    """Your customized version of admin.AdminSite"""
    ...
<br/>
custom_admin = YourSiteAdmin()
custom_admin.register(get_user_model())
...
from abx_pkg.admin import register_admin_views
register_admin_views(custom_admin)
</code></pre>
</details>

<br/>

### ~~Django Admin Usage: JSONFormWidget for editing `BinProvider` and `Binary` data~~

<details><summary>Expand to see more...</summary>

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

</details>

*For a full example see our provided [`django_example_project/`](https://github.com/ArchiveBox/abx-pkg/tree/main/django_example_project)...*

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
