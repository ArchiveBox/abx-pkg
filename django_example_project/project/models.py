from django.db import models
from django_pydantic_field import SchemaField

from abx_pkg import BinProvider, EnvProvider, Binary, SemVer


DEFAULT_PROVIDER = EnvProvider()


class Dependency(models.Model):
    """Example model implementing fields that contain BinProvider and Binary data"""

    label = models.CharField(max_length=63)

    default_binprovider: BinProvider = SchemaField(default=DEFAULT_PROVIDER)

    binaries: list[Binary] = SchemaField(default=[])

    min_version: SemVer = SchemaField(default=(0,0,1))

    class Meta:
        verbose_name_plural = 'Dependencies'
