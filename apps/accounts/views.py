"""Points d'entrée de l'authentification — ADR-004.

Les vues sont minces : elles valident la forme, appellent le service, traduisent
le résultat en HTTP. Aucune règle de sécurité n'y est décidée — c'est
`AuthService` qui décide, et il est testable sans requête.
"""

from __future__ import annotations

from django.contrib.auth import authenticate
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.models import User
from apps.accounts.serializers import (
    ChangePasswordSerializer,
    DeviceSerializer,
    LoginSerializer,
    ProfileUpdateSerializer,
    RefreshSerializer,
    RegisterSerializer,
    TokenPairSerializer,
    UserSerializer,
)
from apps.accounts.services import AuthService, TokenPair
from apps.accounts.throttling import AuthIdentifierThrottle, AuthIPThrottle
from common.exceptions import BusinessRuleViolation
from common.permissions import authenticated_user

__all__ = [
    "ChangePasswordView",
    "DeviceView",
    "LoginView",
    "LogoutView",
    "MeView",
    "RefreshView",
    "RegisterView",
]


def _token_response(
    user: User, tokens: TokenPair, http_status: int = status.HTTP_200_OK
) -> Response:
    """Réponse unique à jeton, partagée par inscription, connexion et changement
    de mot de passe. Trois points d'entrée, un seul contrat."""
    return Response(
        {"access": tokens.access, "refresh": tokens.refresh, "user": UserSerializer(user).data},
        status=http_status,
    )


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthIPThrottle, AuthIdentifierThrottle]

    @extend_schema(request=RegisterSerializer, responses={201: TokenPairSerializer}, tags=["auth"])
    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user, tokens = AuthService.register(**serializer.validated_data)
        return _token_response(user, tokens, status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthIPThrottle, AuthIdentifierThrottle]

    @extend_schema(request=LoginSerializer, responses={200: TokenPairSerializer}, tags=["auth"])
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = authenticate(
            request,
            username=serializer.validated_data["email"],
            password=serializer.validated_data["password"],
        )

        # Message unique, que le compte soit inexistant, inactif ou le mot de
        # passe faux : distinguer ces cas transformerait le point d'entrée en
        # oracle d'existence de comptes. `authenticate` renvoie déjà None pour
        # un compte inactif.
        if user is None:
            raise AuthenticationFailed("Identifiants invalides.")

        user.last_seen_at = timezone.now()
        user.save(update_fields=["last_seen_at"])

        return _token_response(user, AuthService.issue_tokens(user))


class RefreshView(TokenRefreshView):
    """Rotation du jeton de rafraîchissement.

    `ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` : chaque usage produit
    un nouveau couple et invalide l'ancien, si bien que rejouer un jeton
    consommé est détecté.
    """

    # `TokenViewBase` déclare l'attribut comme un tuple vide, ce dont le
    # vérificateur de types déduit un type figé. La liste est bien ce
    # qu'attend DRF à l'exécution.
    permission_classes = [AllowAny]  # type: ignore[assignment]
    throttle_classes = [AuthIPThrottle]


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=RefreshSerializer, responses={204: None}, tags=["auth"])
    def post(self, request: Request) -> Response:
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            AuthService.logout(refresh_token=serializer.validated_data["refresh"])
        except TokenError:
            # Jeton déjà expiré ou révoqué : l'utilisateur voulait être
            # déconnecté, il l'est. Renvoyer une erreur laisserait un client
            # boucler sur une déconnexion qui a déjà eu lieu.
            pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: UserSerializer}, tags=["auth"])
    def get(self, request: Request) -> Response:
        return Response(UserSerializer(authenticated_user(request)).data)

    @extend_schema(request=ProfileUpdateSerializer, responses={200: UserSerializer}, tags=["auth"])
    def patch(self, request: Request) -> Response:
        """Met à jour son propre nom et son téléphone.

        Le compte modifié est celui du jeton : il n'y a pas d'identifiant en
        entrée, donc pas de compte d'autrui à viser. La réponse est la forme
        habituelle du compte, la même que `GET /auth/me/`.
        """
        user = authenticated_user(request)
        serializer = ProfileUpdateSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(UserSerializer(user).data)


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AuthIPThrottle]

    @extend_schema(
        request=ChangePasswordSerializer, responses={200: TokenPairSerializer}, tags=["auth"]
    )
    def post(self, request: Request) -> Response:
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            tokens = AuthService.change_password(
                user=authenticated_user(request), **serializer.validated_data
            )
        except ValueError as exc:
            raise BusinessRuleViolation(str(exc)) from exc

        # T2 — toutes les sessions sont révoquées, y compris la courante. Le
        # client doit remplacer ses jetons par ceux-ci.
        return _token_response(authenticated_user(request), tokens)


class DeviceView(APIView):
    """Enregistrement d'un appareil pour les notifications push."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=DeviceSerializer, responses={200: DeviceSerializer}, tags=["auth"])
    def post(self, request: Request) -> Response:
        serializer = DeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device = AuthService.register_device(
            user=authenticated_user(request),
            token=serializer.validated_data["token"],
            platform=serializer.validated_data["platform"],
        )
        return Response(DeviceSerializer(device).data)

    @extend_schema(request=DeviceSerializer, responses={204: None}, tags=["auth"])
    def delete(self, request: Request) -> Response:
        # Le retrait est scopé à l'utilisateur : personne ne peut désabonner
        # l'appareil d'autrui en devinant son jeton.
        authenticated_user(request).devices.filter(token=request.data.get("token", "")).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
