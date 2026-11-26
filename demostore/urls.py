from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="store_index"),
    path("add/<str:pid>/", views.add, name="store_add"),
    path("cart/", views.cart, name="store_cart"),
    path("remove/<str:pid>/", views.remove, name="store_remove"),
    path("checkout/", views.checkout, name="store_checkout"),
    path("return/", views.payment_return, name="store_return"),
    path("webhook/", views.webhook, name="store_webhook"),
    path("orders/", views.orders, name="store_orders"),
]
