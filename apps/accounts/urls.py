"""Routes de l'authentification — montées sous `/api/v1/auth/`."""

from __future__ import annotations

from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.RegisterView.as_view(), name="register"),
    path("login/", views.LoginView.as_view(), name="login"),
    # `verify/resend/` avant `verify/` : Django essaie les motifs dans l'ordre,
    # et deux chemins littéraux distincts ne se captent pas — mais l'ordre
    # lisible est celui du parcours, et il le reste si l'un devient un motif.
    path("verify/resend/", views.ResendVerificationView.as_view(), name="verify-resend"),
    path("verify/", views.VerifyAccountView.as_view(), name="verify"),
    path("token/refresh/", views.RefreshView.as_view(), name="token-refresh"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("me/", views.MeView.as_view(), name="me"),
    path("password/change/", views.ChangePasswordView.as_view(), name="password-change"),
    path("password/reset/", views.PasswordResetRequestView.as_view(), name="password-reset"),
    path(
        "password/reset/confirm/",
        views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("devices/", views.DeviceView.as_view(), name="devices"),
]
