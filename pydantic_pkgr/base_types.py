__package__ = "pydantic_pkgr"

import os
import shutil

from pathlib import Path
from typing import List, Dict, Callable, Literal, Any, Annotated

from pydantic import TypeAdapter, AfterValidator, BeforeValidator, ValidationError, validate_call

def validate_binprovider_name(name: str) -> str:
    assert 1 < len(name) < 16, 'BinProvider names must be between 1 and 16 characters long'
    assert name.replace('_', '').isalnum(), 'BinProvider names can only contain a-Z0-9 and underscores'
    assert name[0].isalpha(), 'BinProvider names must start with a letter'
    return name

BinProviderName = Annotated[str, AfterValidator(validate_binprovider_name)]
# in practice this is essentially BinProviderName: Literal['env', 'pip', 'apt', 'brew', 'npm', 'vendor']
# but because users can create their own BinProviders we cant restrict it to a preset list of literal names



def validate_bin_dir(path: Path) -> Path:
    path = path.expanduser().absolute()
    assert path.resolve()
    assert path.is_dir(), f'path entries to add to $PATH must be absolute paths to directories {dir}'
    return path

BinDirPath = Annotated[Path, AfterValidator(validate_bin_dir)]

def validate_PATH(PATH: str | List[str]) -> str:
    paths = PATH.split(':') if isinstance(PATH, str) else list(PATH)
    assert all(Path(bin_dir) for bin_dir in paths)
    return ':'.join(paths).strip(':')

PATHStr = Annotated[str, BeforeValidator(validate_PATH)]

def func_takes_args_or_kwargs(lambda_func: Callable[..., Any]) -> bool:
    """returns True if a lambda func takes args/kwargs of any kind, otherwise false if it's pure/argless"""
    code = lambda_func.__code__
    has_args = code.co_argcount > 0
    has_varargs = code.co_flags & 0x04 != 0
    has_varkw = code.co_flags & 0x08 != 0
    return has_args or has_varargs or has_varkw


@validate_call
def bin_name(bin_path_or_name: str | Path) -> str:
    name = Path(bin_path_or_name).name
    assert 1 <= len(name) < 64, 'Binary names must be between 1 and 63 characters long'
    assert name.replace('-', '').replace('_', '').replace('.', '').replace(' ', '').isalnum(), (
        f'Binary name can only contain a-Z0-9-_. and spaces: {name}')
    assert name[0].isalpha(), 'Binary names must start with a letter'
    return name

BinName = Annotated[str, AfterValidator(bin_name)]

@validate_call
def path_is_file(path: Path | str) -> Path:
    path = Path(path) if isinstance(path, str) else path
    assert path.is_file(), f'Path is not a file: {path}'
    return path

HostExistsPath = Annotated[Path, AfterValidator(path_is_file)]

@validate_call
def path_is_executable(path: HostExistsPath) -> HostExistsPath:
    assert os.access(path, os.X_OK), f'Path is not executable (fix by running chmod +x {path})'
    return path

@validate_call
def path_is_script(path: HostExistsPath) -> HostExistsPath:
    SCRIPT_EXTENSIONS = ('.py', '.js', '.sh')
    assert path.suffix.lower() in SCRIPT_EXTENSIONS, 'Path is not a script (does not end in {})'.format(', '.join(SCRIPT_EXTENSIONS))
    return path

HostExecutablePath = Annotated[HostExistsPath, AfterValidator(path_is_executable)]

@validate_call
def path_is_abspath(path: Path) -> Path:
    path = path.expanduser().absolute()   # resolve ~/ -> /home/<username/ and ../../
    assert path.resolve()                 # make sure symlinks can be resolved, but dont return resolved link
    return path

HostAbsPath = Annotated[HostExistsPath, AfterValidator(path_is_abspath)]
HostBinPath = Annotated[HostExistsPath, AfterValidator(path_is_abspath)] # removed: AfterValidator(path_is_executable)
# not all bins need to be executable to be bins, some are scripts


@validate_call
def bin_abspath(bin_path_or_name: str | BinName | Path, PATH: PATHStr | None=None) -> HostBinPath | None:
    assert bin_path_or_name
    if PATH is None:
        PATH = os.environ.get('PATH', '/bin')
    if PATH:
        PATH = str(PATH)
    else:
        return None

    if str(bin_path_or_name).startswith('/'):
        # already a path, get its absolute form
        abspath = Path(bin_path_or_name).expanduser().absolute()
    else:
        # not a path yet, get path using shutil.which
        binpath = shutil.which(bin_path_or_name, mode=os.X_OK, path=PATH)
        # print(bin_path_or_name, PATH.split(':'), binpath, 'GOPINGNGN')
        if not binpath:
            # some bins dont show up with shutil.which (e.g. django-admin.py)
            for path in PATH.split(':'):
                bin_dir = Path(path)
                # print('BIN_DIR', bin_dir, bin_dir.is_dir())
                if not bin_dir.is_dir():
                    # raise Exception(f'Found invalid dir in $PATH: {bin_dir}')
                    continue
                bin_file = bin_dir / bin_path_or_name
                # print(bin_file, path, bin_file.exists(), bin_file.is_file(), bin_file.is_symlink())
                if bin_file.exists():
                    return bin_file

            return None
        # print(binpath, PATH)
        if str(Path(binpath).parent) not in PATH:
            # print('WARNING, found bin but not in PATH', binpath, PATH)
            # found bin but it was outside our search $PATH
            return None
        abspath = Path(binpath).expanduser().absolute()

    try:
        return TypeAdapter(HostBinPath).validate_python(abspath)
    except ValidationError:
        return None

@validate_call
def bin_abspaths(bin_path_or_name: BinName | Path, PATH: PATHStr | None=None) -> List[HostBinPath]:
    assert bin_path_or_name

    PATH = PATH or os.environ.get('PATH', '/bin')
    abspaths = []

    if str(bin_path_or_name).startswith('/'):
        # already a path, get its absolute form
        abspaths.append(Path(bin_path_or_name).expanduser().absolute())
    else:
        # not a path yet, get path using shutil.which
        for path in PATH.split(':'):
            binpath = shutil.which(bin_path_or_name, mode=os.X_OK, path=path)
            if binpath and str(Path(binpath).parent) in PATH:
                abspaths.append(binpath)

    try:
        return TypeAdapter(List[HostBinPath]).validate_python(abspaths)
    except ValidationError:
        return []




################## Types ##############################################

def is_valid_install_args(install_args: List[str]) -> List[str]:
    """Make sure a string is a valid install string for a package manager, e.g. ['yt-dlp', 'ffmpeg']"""
    assert install_args
    assert all(len(arg) for arg in install_args)
    return install_args

def is_valid_python_dotted_import(import_str: str) -> str:
    assert import_str and import_str.replace('.', '').replace('_', '').isalnum()
    return import_str

InstallArgs = Annotated[List[str], AfterValidator(is_valid_install_args)]

LazyImportStr = Annotated[str, AfterValidator(is_valid_python_dotted_import)]

ProviderHandler = Callable[..., Any] | Callable[[], Any]                               # must take no args [], or [bin_name: str, **kwargs]
#ProviderHandlerStr = Annotated[str, AfterValidator(lambda s: s.startswith('self.'))]
ProviderHandlerRef = LazyImportStr | ProviderHandler
ProviderLookupDict = Dict[str, ProviderHandlerRef]
HandlerType = Literal['abspath', 'version', 'packages', 'install']
