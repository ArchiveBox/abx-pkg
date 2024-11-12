__package__ = 'abx_pkg'

from typing import Any, Optional, Dict, List
from typing_extensions import Self

from pydantic import Field, model_validator, computed_field, field_validator, validate_call, field_serializer, ConfigDict, InstanceOf

from .semver import SemVer
from .shallowbinary import ShallowBinary
from .binprovider import BinProvider, EnvProvider, BinaryOverrides
from .base_types import (
    BinName,
    bin_abspath,
    bin_abspaths,
    HostBinPath,
    BinProviderName,
    PATHStr,
    Sha256,
)

DEFAULT_PROVIDER = EnvProvider()


class Binary(ShallowBinary):
    model_config = ConfigDict(extra='allow', populate_by_name=True, validate_defaults=True, validate_assignment=True, from_attributes=True, revalidate_instances='always', arbitrary_types_allowed=True)

    name: BinName = ''
    description: str = ''

    binproviders_supported: List[InstanceOf[BinProvider]] = Field(default_factory=lambda : [DEFAULT_PROVIDER], alias='binproviders')
    overrides: BinaryOverrides = Field(default_factory=dict)
    
    loaded_binprovider: Optional[InstanceOf[BinProvider]] = Field(default=None, alias='binprovider')
    loaded_abspath: Optional[HostBinPath] = Field(default=None, alias='abspath')
    loaded_version: Optional[SemVer] = Field(default=None, alias='version')
    loaded_sha256: Optional[Sha256] = Field(default=None, alias='sha256')
    
    # bin_filename:  see below
    # is_executable: see below
    # is_script
    # is_valid: see below


    @model_validator(mode='after')
    def validate(self):
        # assert self.name, 'Binary.name must not be empty'
        # self.description = self.description or self.name
        
        assert self.binproviders_supported, f'No providers were given for package {self.name}'

        # pull in any overrides from the binproviders
        for binprovider in self.binproviders_supported:
            overrides_for_bin = binprovider.overrides.get(self.name, {})
            if overrides_for_bin:
                self.overrides[binprovider.name] = {
                    **overrides_for_bin,
                    **self.overrides.get(binprovider.name, {}),
                }
        return self

    @field_validator('loaded_abspath', mode='before')
    def parse_abspath(cls, value: Any) -> Optional[HostBinPath]:
        return bin_abspath(value) if value else None

    @field_validator('loaded_version', mode='before')
    def parse_version(cls, value: Any) -> Optional[SemVer]:
        return SemVer(value) if value else None

    @field_serializer('overrides', when_used='json')
    def serialize_overrides(self, overrides: BinaryOverrides) -> Dict[BinProviderName, Dict[str, str]]:
        return {
            binprovider_name: {
                handler_type: str(handler_value)
                for handler_type, handler_value in binprovider_overrides.items()
            }
            for binprovider_name, binprovider_overrides in overrides.items()
        }

    @computed_field
    @property
    def loaded_abspaths(self) -> Dict[BinProviderName, List[HostBinPath]]:
        if not self.loaded_abspath:
            # binary has not been loaded yet
            return {}
        
        all_bin_abspaths = {self.loaded_binprovider.name: [self.loaded_abspath]} if self.loaded_binprovider else {}
        for binprovider in self.binproviders_supported:
            if not binprovider.PATH:
                # print('skipping provider', binprovider.name, binprovider.PATH)
                continue
            for abspath in bin_abspaths(self.name, PATH=binprovider.PATH):
                existing = all_bin_abspaths.get(binprovider.name, [])
                if abspath not in existing:
                    all_bin_abspaths[binprovider.name] = [
                        *existing,
                        abspath,
                    ]
        return all_bin_abspaths
    

    @computed_field
    @property
    def loaded_bin_dirs(self) -> Dict[BinProviderName, PATHStr]:
        return {
            provider_name: ':'.join([str(bin_abspath.parent) for bin_abspath in bin_abspaths])
            for provider_name, bin_abspaths in self.loaded_abspaths.items()
        }
    
    @computed_field
    @property
    def python_name(self) -> str:
        return self.name.replace('-', '_').replace('.', '_')
    
    # @validate_call
    def get_binprovider(self, binprovider_name: BinProviderName, **extra_overrides) -> InstanceOf[BinProvider]:
        for binprovider in self.binproviders_supported:
            if binprovider.name == binprovider_name:
                overrides_for_binprovider = {
                    self.name: self.overrides.get(binprovider_name, {})
                }
                return binprovider.get_provider_with_overrides(overrides=overrides_for_binprovider, **extra_overrides)

        raise KeyError(f'{binprovider_name} is not a supported BinProvider for Binary(name={self.name})')

    @validate_call
    def install(self, binproviders: Optional[List[BinProviderName]]=None, **extra_overrides) -> Self:
        assert self.name, f'No binary name was provided! {self}'

        if binproviders is not None and len(list(binproviders)) == 0:
            return self
        
        
        inner_exc = Exception('No providers were available')
        errors = {}
        for binprovider in self.binproviders_supported:
            if binproviders and (binprovider.name not in binproviders):
                continue
            
            try:
                provider = self.get_binprovider(binprovider_name=binprovider.name, **extra_overrides)
                
                installed_bin = provider.install(self.name)
                if installed_bin is not None and installed_bin.loaded_abspath:
                    # print('INSTALLED', self.name, installed_bin)
                    return self.__class__(**{
                        **self.model_dump(),
                        **installed_bin.model_dump(exclude={'binproviders_supported'}),
                        'loaded_binprovider': provider,
                        'binproviders_supported': self.binproviders_supported,
                        'overrides': self.overrides,
                    })
            except Exception as err:
                # print(err)
                # raise
                inner_exc = err
                errors[binprovider.name] = str(err)
                
        provider_names = ', '.join(binproviders or [p.name for p in self.binproviders_supported])
        raise Exception(f'None of the configured providers ({provider_names}) were able to install binary: {self.name} ERRORS={errors}') from inner_exc

    @validate_call
    def load(self, binproviders: Optional[List[BinProviderName]]=None, nocache=False, **extra_overrides) -> Self:
        assert self.name, f'No binary name was provided! {self}'

        # if we're already loaded, skip loading
        if self.is_valid:
            return self
        
        # if binproviders list is passed but it's empty, skip loading
        if binproviders is not None and len(list(binproviders)) == 0:
            return self

        inner_exc = Exception('No providers were available')
        errors = {}
        for binprovider in self.binproviders_supported:
            if binproviders and binprovider.name not in binproviders:
                continue
            
            try:
                provider = self.get_binprovider(binprovider_name=binprovider.name, **extra_overrides)
                
                installed_bin = provider.load(self.name, nocache=nocache)
                if installed_bin is not None and installed_bin.loaded_abspath:
                    # print('LOADED', binprovider, self.name, installed_bin)
                    return self.__class__(**{
                        **self.model_dump(),
                        **installed_bin.model_dump(exclude={'binproviders_supported'}),
                        'loaded_binprovider': provider,
                        'binproviders_supported': self.binproviders_supported,
                        'overrides': self.overrides,
                    })
                else:
                    continue
            except Exception as err:
                # print(err)
                inner_exc = err
                errors[binprovider.name] = str(err)
        
        provider_names = ', '.join(binproviders or [p.name for p in self.binproviders_supported])
        raise Exception(f'None of the configured providers ({provider_names}) were able to load binary: {self.name} ERRORS={errors}') from inner_exc

    @validate_call
    def load_or_install(self, binproviders: Optional[List[BinProviderName]]=None, nocache: bool=False, **extra_overrides) -> Self:
        assert self.name, f'No binary name was provided! {self}'

        if self.is_valid:
            return self

        if binproviders is not None and len(list(binproviders)) == 0:
            return self

        inner_exc = Exception('No providers were available')
        errors = {}
        for binprovider in self.binproviders_supported:
            if binproviders and binprovider.name not in binproviders:
                continue
            
            try:
                provider = self.get_binprovider(binprovider_name=binprovider.name, **extra_overrides)
                
                installed_bin = provider.load_or_install(self.name, nocache=nocache)
                if installed_bin is not None and installed_bin.loaded_abspath:
                    # print('LOADED_OR_INSTALLED', self.name, installed_bin)
                    return self.__class__(**{
                        **self.model_dump(),
                        **installed_bin.model_dump(exclude={'binproviders_supported'}),
                        'loaded_binprovider': provider,
                        'binproviders_supported': self.binproviders_supported,
                        'overrides': self.overrides,
                    })
                else:
                    continue
            except Exception as err:
                # print(err)
                inner_exc = err
                errors[binprovider.name] = str(err)
                continue
        
        provider_names = ', '.join(binproviders or [p.name for p in self.binproviders_supported])
        raise Exception(f'None of the configured providers ({provider_names}) were able to find or install binary: {self.name} ERRORS={errors}') from inner_exc
        