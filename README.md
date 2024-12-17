<h1><a href="https://github.com/ArchiveBox/abx-pkg"><code>abx-pkg</code></a> &nbsp; &nbsp; &nbsp; &nbsp; 📦  <small><code>apt</code>&nbsp; <code>brew</code>&nbsp; <code>pip</code>&nbsp; <code>npm</code> &nbsp;₊₊₊</small><br/><sub>Simple Python interfaces for package managers + installed binaries.</sub></h1>
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
It's designed for when `requirements.txt` isn't enough, and you have to detect or install dependencies at runtime.


```shell
pip install abx-pkg
```


> ✨ Built with [`pydantic`](https://pydantic-docs.helpmanual.io/) v2 for strong static typing guarantees and json import/export compatibility  
> 📦 Provides consistent cross-platform interfaces for dependency resolution & installation at runtime  
> 🌈 Integrates with [`django`](https://docs.djangoproject.com/en/5.0/) >= 4.0, [`django-ninja`](https://django-ninja.dev/), and OpenAPI + [`django-jsonform`](https://django-jsonform.readthedocs.io/) out-of-the-box  
> 🦄 Lets you use the builtin `abx-pkg` methods, or [`pyinfra`](https://github.com/pyinfra-dev/pyinfra) / [`ansible`](https://github.com/ansible/ansible) for the install operations

<sub><i>Built by <a href="https://github.com/ArchiveBox">ArchiveBox</a> to install & auto-update our extractor dependencies at runtime (<code>chrome</code>, <code>wget</code>, <code>curl</code>, etc.) on `macOS`/`Linux`/`Docker`.</i></sub>

<br/>

> [!WARNING]
> This is `BETA` software, the API is mostly stable but there may be minor changes later on.


**Source Code**: [https://github.com/ArchiveBox/abx-pkg/](https://github.com/ArchiveBox/abx-pkg/)

**Documentation**: [https://github.com/ArchiveBox/abx-pkg/blob/main/README.md](https://github.com/ArchiveBox/abx-pkg/blob/main/README.md)

<br/>

```python
from abx_pkg import *

apt, brew, pip, npm, env = AptProvider(), BrewProvider(), PipProvider(), NpmProvider(), EnvProvider()

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
    # Path('/usr/bin/curl') SemVer('7.81.0') AptProvider() True abc134...

    binary.exec(cmd=['--version'])   # curl 7.81.0 (x86_64-apple-darwin23.0) libcurl/7.81.0 ...
```

```python
from pydantic import InstanceOf
from abx_pkg import Binary, BinProvider, BrewProvider, EnvProvider

# you can also define binaries as classes, making them usable for type checking
class CurlBinary(Binary):
    name: str = 'curl'
    binproviders: list[InstanceOf[BinProvider]] = [BrewProvider(), EnvProvider()]

curl = CurlBinary().install()
assert isinstance(curl, CurlBinary)                                 # CurlBinary is a unique type you can use in annotations now
print(curl.abspath, curl.version, curl.binprovider, curl.is_valid)  # Path('/opt/homebrew/bin/curl') SemVer('8.4.0') BrewProvider() True
curl.exec(cmd=['--version'])                                        # curl 8.4.0 (x86_64-apple-darwin23.0) libcurl/8.4.0 ...
```

```python
from abx_pkg import Binary, EnvProvider, PipProvider

# We also provide direct package manager (aka BinProvider) APIs
apt = AptProvider()
apt.install('wget')
print(apt.PATH, apt.get_abspaths('wget'), apt.get_version('wget'))

# even if packages are installed by tools we don't control (e.g. pyinfra/ansible/puppet/etc.)
from pyinfra.operations import apt
apt.packages(name="Install ffmpeg", packages=['ffmpeg'], _sudo=True)

# our Binary API provides a nice type-checkable, validated, serializable handle
ffmpeg = Binary(name='ffmpeg').load()
print(ffmpeg)                       # name=ffmpeg abspath=/usr/bin/ffmpeg version=3.3.0 is_valid=True binprovider=apt ...
print(ffmpeg.abspaths)              # show all the ffmpeg binaries found in $PATH (in case theres more than one available)
print(ffmpeg.model_dump())          # ... everything can also be dumped/loaded as json-ready dict
print(ffmpeg.model_json_schema())   # ... OpenAPI-ready JSON schema showing all available fields
```

### Supported Package Managers

**So far it supports `installing`/`finding installed`/~~`updating`/`removing`~~ packages on `Linux`/`macOS` with:**

- `apt` (Ubuntu/Debian/etc.)
- `brew` (macOS/Linux)
- `pip` (Linux/macOS/Windows)
- `npm` (Linux/macOS/Windows)
- `env` (looks for existing version of binary in user's `$PATH` at runtime)
- `vendor` (you can bundle vendored copies of packages you depend on within your source)

*Planned:* `docker`, `cargo`, `nix`, `apk`, `go get`, `gem`, `pkg`, *and more using `ansible`/[`pyinfra`](https://github.com/pyinfra-dev/pyinfra)...*

---


## Usage

```bash
pip install abx-pkg
```

### [`BinProvider`](https://github.com/ArchiveBox/abx-pkg/blob/main/abx_pkg/binprovider.py#:~:text=class%20BinProvider)

**Implementations: `EnvProvider`, `AptProvider`, `BrewProvider`, `PipProvider`, `NpmProvider`**

This type represents a "provider of binaries", e.g. a package manager like `apt`/`pip`/`npm`, or `env` (which finds binaries in your `$PATH`).

`BinProvider`s implement the following interface:
* `.INSTALLER_BIN -> /opt/homebrew/bin/brew`  provider's pkg manager location
* `.PATH -> PATHStr('/opt/homebrew/bin:/usr/local/bin:...')`  where provider stores bins
* `get_packages(bin_name: str) -> InstallArgs(['curl', 'libcurl4', '...])` find pkg dependencies for a bin
- `install(bin_name: str)` install a bin using binprovider to install needed packages
- `load(bin_name: str)`  find an existing installed binary
- `load_or_install(bin_name: str)` `->` `Binary` find existing / install if needed
- `get_version(bin_name: str) -> SemVer('1.0.0')`  get currently installed version
- `get_abspath(bin_name: str) -> Path('/absolute/path/to/bin')` get installed bin abspath
* `get_abspaths(bin_name: str) -> [Path('/opt/homebrew/bin/curl'), Path('/other/paths/to/curl'), ...]` get all matching bins found
* `get_sha256(bin_name: str) -> str` get sha256 hash hexdigest of the binary


```python
import platform
from typing import List
from abx_pkg import EnvProvider, PipProvider, AptProvider, BrewProvider

### Example: Finding an existing install of bash using the system $PATH environment
env = EnvProvider()
bash = env.load(bin_name='bash')      # Binary('bash', provider=env)
print(bash.abspath)                   # Path('/opt/homebrew/bin/bash')
print(bash.version)                   # SemVer('5.2.26')
bash.exec(['-c', 'echo hi'])          # hi

### Example: Installing curl using the apt package manager
apt = AptProvider()
curl = apt.install(bin_name='curl')   # Binary('curl', provider=apt)
print(curl.abspath)                   # Path('/usr/bin/curl')
print(curl.version)                   # SemVer('8.4.0')
print(curl.sha256)                    # 9fd780521c97365f94c90724d80a889097ae1eeb2ffce67b87869cb7e79688ec
curl.exec(['--version'])              # curl 7.81.0 (x86_64-pc-linux-gnu) libcurl/7.81.0 ...

### Example: Finding/Installing django with pip (w/ customized binpath resolution behavior)
pip = PipProvider(
    abspath_handler={'*': lambda self, bin_name, **context: inspect.getfile(bin_name)},  # use python inspect to get path instead of os.which
)
django_bin = pip.load_or_install('django') # Binary('django', provider=pip)
print(django_bin.abspath)             # Path('/usr/lib/python3.10/site-packages/django/__init__.py')
print(django_bin.version)             # SemVer('5.0.2')
```

### [`Binary`](https://github.com/ArchiveBox/abx-pkg/blob/main/abx_pkg/binary.py#:~:text=class%20Binary)

This type represents a single binary dependency aka a package (e.g. `wget`, `curl`, `ffmpeg`, etc.).  
It can define one or more `BinProvider`s that it supports, along with overrides to customize the behavior for each.

`Binary`s implement the following interface:
- `load()`, `install()`, `load_or_install()` `->` `Binary`
- `binprovider: InstanceOf[BinProvider]`
- `abspath: Path`
- `abspaths: list[Path]`
- `version: SemVer`
- `sha256: str`

```python
from abx_pkg import BinProvider, Binary, BinProviderName, BinName, ProviderLookupDict, SemVer

class CustomBrewProvider(BrewProvider):
    name: str = 'custom_brew'

    def get_macos_packages(self, bin_name: str, **context) -> list[str]:
        extra_packages_lookup_table = json.load(Path('macos_packages.json'))
        return extra_packages_lookup_table.get(platform.machine(), [bin_name])


### Example: Create a re-usable class defining a binary and its providers
class YtdlpBinary(Binary):
    name: BinName = 'ytdlp'
    description: str = 'YT-DLP (Replacement for YouTube-DL) Media Downloader'

    binproviders_supported: list[BinProvider] = [EnvProvider(), PipProvider(), AptProvider(), CustomBrewProvider()]
    
    # customize installed package names for specific package managers
    provider_overrides: dict[BinProviderName, ProviderLookupDict] = {
        'pip': {'packages': ['yt-dlp[default,curl-cffi]']}, # can use literal values (packages -> list[str], version -> SemVer, abspath -> Path, install -> str log)
        'apt': {'packages': lambda: ['yt-dlp', 'ffmpeg']},  # also accepts any pure Callable that returns a list of packages
        'brew': {'packages': 'self.get_macos_packages'},    # also accepts string reference to function on self (where self is the BinProvider)
    }


ytdlp = YtdlpBinary().load_or_install()
print(ytdlp.binprovider)                  # BrewProvider(...)
print(ytdlp.abspath)                      # Path('/opt/homebrew/bin/yt-dlp')
print(ytdlp.abspaths)                     # [Path('/opt/homebrew/bin/yt-dlp'), Path('/usr/local/bin/yt-dlp')]
print(ytdlp.version)                      # SemVer('2024.4.9')
print(ytdlp.sha256)                       # 46c3518cfa788090c42e379971485f56d007a6ce366dafb0556134ca724d6a36
print(ytdlp.is_valid)                     # True
```

```python
from abx_pkg import BinProvider, Binary, BinProviderName, BinName, ProviderLookupDict, SemVer

#### Example: Create a binary that uses Podman if available, or Docker otherwise
class DockerBinary(Binary):
    name: BinName = 'docker'

    binproviders_supported: list[BinProvider] = [EnvProvider(), AptProvider()]
    
    provider_overrides: dict[BinProviderName, ProviderLookupDict] = {
        'env': {
            # example: prefer podman if installed (falling back to docker)
            'abspath': lambda: os.which('podman') or os.which('docker') or os.which('docker-ce'),
        },
        'apt': {
            # example: vary installed package name based on your CPU architecture
            'packages': {
                'amd64': ['docker'],
                'armv7l': ['docker-ce'],
                'arm64': ['docker-ce'],
            }.get(platform.machine(), 'docker'),
        },
    }

docker = DockerBinary().load_or_install()
print(docker.binprovider)                 # EnvProvider()
print(docker.abspath)                     # Path('/usr/local/bin/podman')
print(docker.abspaths)                    # [Path('/usr/local/bin/podman'), Path('/opt/homebrew/bin/podman')]
print(docker.version)                     # SemVer('6.0.2')
print(docker.is_valid)                    # True

# You can also pass **kwargs to override properties at runtime,
# e.g. if you want to force the abspath to be at a specific path:
custom_docker = DockerBinary(abspath='~/custom/bin/podman').load()
print(custom_docker.name)                 # 'docker'
print(custom_docker.binprovider)          # EnvProvider()
print(custom_docker.abspath)              # Path('/Users/example/custom/bin/podman')
print(custom_docker.version)              # SemVer('5.0.2')
print(custom_docker.is_valid)             # True
```

### [`SemVer`](https://github.com/ArchiveBox/abx-pkg/blob/main/abx_pkg/semver.py#:~:text=class%20SemVer)

```python
from abx_pkg import SemVer

### Example: Use the SemVer type directly for parsing & verifying version strings
SemVer.parse('Google Chrome 124.0.6367.208+beta_234. 234.234.123')  # SemVer(124, 0, 6367')
SemVer.parse('2024.04.05)                                           # SemVer(2024, 4, 5)
SemVer.parse('1.9+beta')                                            # SemVer(1, 9, 0)
str(SemVer(1, 9, 0))                                                # '1.9.0'
```
<br/>

> These types are all meant to be used library-style to make writing your own apps easier.  
> e.g. you can use it to build things like: [`playwright install --with-deps`](https://playwright.dev/docs/browsers#install-system-dependencies))


<br/>

---
---

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

*Fore more info see the [`django-pydantic-field`](https://github.com/surenkov/django-pydantic-field) docs...*

Example Django `models.py` showing how to store `Binary` and `BinProvider` instances in DB fields:
```python
from typing import List
from django.db import models
from pydantic import InstanceOf
from abx_pkg import BinProvider, Binary, SemVer
from django_pydantic_field import SchemaField

class InstalledBinary(models.Model):
    name = models.CharField(max_length=63)
    binary: Binary = SchemaField()
    binproviders: list[InstanceOf[BinProvider]] = SchemaField(default=[])
    version: SemVer = SchemaField(default=(0,0,1))
```

And here's how to save a `Binary` using the example model:
```python
# find existing curl Binary in $PATH
curl = Binary(name='curl').load()

# save it to the DB using our new model
obj = InstalledBinary(
    name='curl',
    binary=curl,                                  # store Binary/BinProvider/SemVer values directly in fields
    binproviders=[env],                           # no need for manual JSON serialization / schema checking
    min_version=SemVer('6.5.0'),
)
obj.save()                                      
```

When fetching it back from the DB, the `Binary` field is auto-deserialized / immediately usable:
```
obj = InstalledBinary.objects.get(name='curl')    # everything is transparently serialized to/from the DB,
                                                  # and is ready to go immediately after querying:
assert obj.binary.abspath == curl.abspath
print(obj.binary.abspath)                         #   Path('/usr/local/bin/curl')
obj.binary.exec(['--version'])                    #   curl 7.81.0 (x86_64-apple-darwin23.0) libcurl/7.81.0 ...
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
    'admin_data_views'
    'abx_pkg'
    # ...
]

# point these to a function that gets the list of all binaries / a single binary
ABX_PKG_GET_ALL_BINARIES = 'abx_pkg.views.get_all_binaries'
ABX_PKG_GET_BINARY = 'abx_pkg.views.get_binary'

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


## Examples

### Advanced: Implement your own package manager behavior by subclassing BinProvider

```python
from subprocess import run, PIPE

from abx_pkg import BinProvider, BinProviderName, BinName, SemVer

class CargoProvider(BinProvider):
    name: BinProviderName = 'cargo'
    
    def on_setup_paths(self):
        if '~/.cargo/bin' not in sys.path:
            sys.path.append('~/.cargo/bin')

    def on_install(self, bin_name: BinName, **context):
        packages = self.on_get_packages(bin_name)
        installer_process = run(['cargo', 'install', *packages.split(' ')], capture_output = True, text=True)
        assert installer_process.returncode == 0

    def on_get_packages(self, bin_name: BinName, **context) -> InstallArgs:
        # optionally remap bin_names to strings passed to installer 
        # e.g. 'yt-dlp' -> ['yt-dlp, 'ffmpeg', 'libcffi', 'libaac']
        return [bin_name]

    def on_get_abspath(self, bin_name: BinName, **context) -> Path | None:
        self.on_setup_paths()
        return Path(os.which(bin_name))

    def on_get_version(self, bin_name: BinName, **context) -> SemVer | None:
        self.on_setup_paths()
        return SemVer(run([bin_name, '--version'], stdout=PIPE).stdout.decode())

cargo = CargoProvider()
rg = cargo.install(bin_name='ripgrep')
print(rg.binprovider)                   # CargoProvider()
print(rg.version)                       # SemVer(14, 1, 0)
```


<br/>

---

<br/>

*Note:* this package used to be called `pydantic-pkgr`, it was renamed to `abx-pkg` on 2024-11-12.

### TODO

- [x] Implement initial basic support for `apt`, `brew`, and `pip`
- [x] Provide editability and actions via Django Admin UI using [`django-pydantic-field`](https://github.com/surenkov/django-pydantic-field) and [`django-jsonform`](https://django-jsonform.readthedocs.io/en/latest/)
- [ ] Implement `update` and `remove` actions on BinProviders
- [ ] Add `preinstall` and `postinstall` hooks for things like adding `apt` sources and running cleanup scripts
- [ ] Implement more package managers (`cargo`, `gem`, `go get`, `ppm`, `nix`, `docker`, etc.)
- [ ] Add `Binary.min_version` that affects `.is_valid` based on whether it meets minimum `SemVer` threshold


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
