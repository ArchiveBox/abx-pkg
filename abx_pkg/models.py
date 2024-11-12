# pip install django-pydantic-field

### EXAMPLE USAGE
#
# from django.db import models
# from django_pydantic_field import SchemaField
#
# from abx_pkg import BinProvider, EnvProvider, Binary
#
# DEFAULT_PROVIDER = EnvProvider()
#
# class MyModel(models.Model):
#     ...
#
#     # SchemaField supports storing a single BinProvider/Binary in a field...
#     favorite_binprovider: BinProvider = SchemaField(default=DEFAULT_PROVIDER)
#
#     # ... or inside a collection type like list[...] dict[...]
#     optional_binaries: list[Binary] = SchemaField(default=[])
#
# curl = Binary(name='curl', providers=[DEFAULT_PROVIDER]).load()
#
# obj = MyModel(optional_binaries=[curl])
# obj.save()
#
# assert obj.favorite_binprovider == DEFAULT_PROVIDER
# assert obj.optional_binaries[0].provider == DEFAULT_PROVIDER
