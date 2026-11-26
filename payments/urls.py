from django.urls import path

from . import api

urlpatterns = [
    path("ping", api.Ping.as_view()),
    path("orders", api.OrderList.as_view()),
    path("orders/<str:public_id>", api.OrderDetail.as_view()),
    path("payments", api.PaymentList.as_view()),
    path("payments/<str:public_id>", api.PaymentDetail.as_view()),
    path("payments/<str:public_id>/otp", api.PaymentOTP.as_view()),
    path("payments/<str:public_id>/refunds", api.RefundList.as_view()),
    path("settlements", api.SettlementList.as_view()),
    path("webhooks/test", api.WebhookTest.as_view()),
]
