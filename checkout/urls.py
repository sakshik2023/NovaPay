from django.urls import path

from . import views

urlpatterns = [
    path("<str:order_id>/", views.checkout_page, name="checkout"),
    path("<str:order_id>/submit/", views.submit, name="checkout_submit"),
    path("<str:order_id>/otp/", views.otp, name="checkout_otp"),
]
