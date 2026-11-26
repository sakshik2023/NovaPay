from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("v1/", include("payments.urls")),
    path("pay/", include("checkout.urls")),
    path("demo-store/", include("demostore.urls")),
    path("", include("dashboard.urls")),
]
