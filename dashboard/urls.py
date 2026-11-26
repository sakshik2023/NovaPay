from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("docs/", views.api_docs, name="docs"),
    path("register/", views.register, name="register"),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("dashboard/", views.overview, name="overview"),
    path("dashboard/payments/", views.payments_list, name="payments"),
    path("dashboard/payments/<str:public_id>/", views.payment_detail, name="payment_detail"),
    path("dashboard/orders/", views.orders_list, name="orders"),
    path("dashboard/links/", views.links, name="links"),
    path("dashboard/refunds/", views.refunds_list, name="refunds"),
    path("dashboard/settlements/", views.settlements, name="settlements"),
    path("dashboard/settlements/<str:public_id>/", views.settlement_detail, name="settlement_detail"),
    path("dashboard/ledger/", views.ledger_view, name="ledger"),
    path("dashboard/webhooks/", views.webhooks_view, name="webhooks"),
    path("dashboard/keys/", views.keys_view, name="keys"),
    path("dashboard/risk/", views.risk, name="risk"),
    path("dashboard/audit/", views.audit_view, name="audit"),
    path("dashboard/settings/", views.settings_view, name="settings"),
]
