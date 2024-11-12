from django.contrib import admin




def register_admin_views(admin_site: admin.AdminSite):
    """register the django-admin-data-views defined in settings.ADMIN_DATA_VIEWS"""

    from admin_data_views.admin import get_app_list, admin_data_index_view, get_admin_data_urls, get_urls

    CustomAdminCls = admin_site.__class__

    admin_site.get_app_list = get_app_list.__get__(admin_site, CustomAdminCls)
    admin_site.admin_data_index_view = admin_data_index_view.__get__(admin_site, CustomAdminCls)
    admin_site.get_admin_data_urls = get_admin_data_urls.__get__(admin_site, CustomAdminCls)
    admin_site.get_urls = get_urls(admin_site.get_urls).__get__(admin_site, CustomAdminCls)

    return admin_site


register_admin_views(admin.site)

# if you've implemented a custom admin site, you should call this funciton on your site

# class YourSiteAdmin(admin.AdminSite):
#     ...
#
# custom_site = YourSiteAdmin()
#
# register_admin_views(custom_site)
