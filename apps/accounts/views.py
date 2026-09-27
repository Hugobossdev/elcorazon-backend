"""Points d'entrée de l'authentification — ADR-004.

Les vues sont minces : elles valident la forme, appellent le service, traduisent
le résultat en HTTP. Aucune règle de sécurité n'y est décidée — c'est
`AuthService` qui décide, et il est testable sans requête.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
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

from apps.accounts.models import User, VerificationPurpose
from apps.accounts.serializers import (
    ChangePasswordSerializer,
    DeviceSerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    ProfileUpdateSerializer,
    RefreshSerializer,
    RegisterSerializer,
    TokenPairSerializer,
    UserSerializer,
    VerificationChallengeSerializer,
    VerificationRequestSerializer,
    VerifyCodeSerializer,
)
from apps.accounts.services import (
    AuthService,
    TokenPair,
    VerificationChallenge,
    VerificationService,
)
from apps.accounts.throttling import AuthCodeIssueThrottle, AuthIdentifierThrottle, AuthIPThrottle
from common.exceptions import BusinessRuleViolation
from common.permissions import authenticated_user

__all__ = [
    "ChangePasswordView",
    "DeviceView",
    "LoginView",
    "LogoutView",
    "MeView",
    "PasswordResetConfirmView",
    "PasswordResetRequestView",
    "RefreshView",
    "RegisterView",
    "ResendVerificationView",
    "VerifyAccountView",
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


def _challenge_response(challenge: VerificationChallenge, detail: str) -> Response:
    """Réponse d'émission de code — **la même**, compte connu ou non.

    202 et non 200 : le serveur a accepté la demande, l'envoi part après le
    commit, et rien dans cette réponse ne prouve qu'un message a été expédié.
    """
    return Response(
        {
            "email": challenge.email,
            "expires_at": challenge.expires_at,
            "retry_after": challenge.retry_after,
            "code_length": challenge.code_length,
            "detail": detail,
        },
        status=status.HTTP_202_ACCEPTED,
    )


def _silent_challenge(email: str) -> VerificationChallenge:
    """Ce qu'on répond quand il n'y a rien à envoyer.

    Adresse inconnue, ou compte déjà vérifié : dans les deux cas aucun courriel
    ne part, et la réponse doit pourtant être indiscernable de celle d'un envoi
    réussi. Les durées annoncées sont celles des réglages — les vraies : un
    `retry_after` nul pour une adresse inconnue et soixante secondes pour une
    adresse connue transformerait cette route en annuaire d'abonnés.
    """
    return VerificationChallenge(
        email=email,
        expires_at=timezone.now()
        + timedelta(seconds=settings.ACCOUNT_VERIFICATION_CODE_TTL_SECONDS),
        retry_after=settings.ACCOUNT_VERIFICATION_RESEND_COOLDOWN_SECONDS,
        code_length=settings.ACCOUNT_VERIFICATION_CODE_LENGTH,
    )


#: Phrase unique des deux routes d'émission. Elle ne dit pas « un code a été
#: envoyé » mais « si un compte existe » : c'est ce qui la rend vraie dans les
#: deux cas, sans que l'utilisateur légitime y perde quoi que ce soit.
ENVOI_ANNONCE = (
    "Si un compte correspond à cette adresse, un code vient d'y être envoyé. "
    "Pensez à regarder vos courriers indésirables."
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


class VerifyAccountView(APIView):
    """`/auth/verify/` — présentation du code reçu, et ouverture de session.

    C'est **la** route qui rend son premier couple de jetons à un livreur
    inscrit par lui-même : `POST /delivery/apply/` n'en rend aucun. La
    vérification cesse ainsi d'être une formalité que le client pourrait
    sauter — sans code, il n'y a simplement pas de session à obtenir.

    `AllowAny`, comme la connexion : l'appelant n'a par construction pas encore
    de jeton. Les deux limiteurs habituels s'appliquent — un code à six
    chiffres est un secret court, et le compteur d'essais du service en est la
    seconde ligne (voir `VerificationService.consume`).
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthIPThrottle, AuthIdentifierThrottle]

    @extend_schema(
        request=VerifyCodeSerializer, responses={200: TokenPairSerializer}, tags=["auth"]
    )
    def post(self, request: Request) -> Response:
        serializer = VerifyCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user, tokens = VerificationService.confirm_account(**serializer.validated_data)

        user.last_seen_at = timezone.now()
        user.save(update_fields=["last_seen_at"])

        return _token_response(user, tokens)


class ResendVerificationView(APIView):
    """`/auth/verify/resend/` — renvoi du code de vérification.

    Ne renvoie **rien** pour un compte déjà vérifié, et c'est délibéré : le
    code rendu par `/auth/verify/` ouvre une session, si bien qu'en émettre un
    pour un compte vérifié reviendrait à ajouter une connexion sans mot de
    passe à qui sait relever la boîte. Le besoin, lui, est couvert par
    `/auth/password/reset/`, qui l'assume et révoque les sessions en cours.

    La réponse reste identique dans tous les cas.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthIPThrottle, AuthCodeIssueThrottle]

    @extend_schema(
        request=VerificationRequestSerializer,
        responses={202: VerificationChallengeSerializer},
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        serializer = VerificationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        user = VerificationService.find(email=email)
        if user is None or user.email_verified_at is not None:
            return _challenge_response(_silent_challenge(email), ENVOI_ANNONCE)

        challenge = VerificationService.issue(
            user=user, purpose=VerificationPurpose.ACCOUNT_VERIFICATION
        )
        return _challenge_response(challenge, ENVOI_ANNONCE)


class PasswordResetRequestView(APIView):
    """`/auth/password/reset/` — demande d'un code de réinitialisation.

    Ouverte à tous les comptes, vérifiés ou non : ne plus savoir son mot de
    passe n'a rien à voir avec l'état de la vérification, et le refuser à un
    compte non vérifié enfermerait dehors quelqu'un qui a raté les deux étapes
    d'affilée.

    Un compte **désactivé** n'en reçoit pas. Lui rouvrir un mot de passe ne lui
    rendrait aucun accès — `authenticate` refuse déjà les comptes inactifs —
    mais lui adresserait un courriel après une fermeture, ce qui se lit comme
    une erreur de notre part.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthIPThrottle, AuthCodeIssueThrottle]

    @extend_schema(
        request=VerificationRequestSerializer,
        responses={202: VerificationChallengeSerializer},
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        serializer = VerificationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        user = VerificationService.find(email=email)
        if user is None or not user.is_active:
            return _challenge_response(_silent_challenge(email), ENVOI_ANNONCE)

        challenge = VerificationService.issue(user=user, purpose=VerificationPurpose.PASSWORD_RESET)
        return _challenge_response(challenge, ENVOI_ANNONCE)


class PasswordResetConfirmView(APIView):
    """`/auth/password/reset/confirm/` — code + nouveau mot de passe.

    Rend un couple de jetons, comme `/auth/password/change/` et pour la même
    raison : toutes les sessions viennent d'être révoquées, et renvoyer
    l'utilisateur vers l'écran de connexion pour qu'il ressaisisse le mot de
    passe qu'il vient de choisir n'ajouterait aucune sécurité.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthIPThrottle, AuthIdentifierThrottle]

    @extend_schema(
        request=PasswordResetConfirmSerializer,
        responses={200: TokenPairSerializer},
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user, tokens = VerificationService.reset_password(**serializer.validated_data)

        user.last_seen_at = timezone.now()
        user.save(update_fields=["last_seen_at"])

        return _token_response(user, tokens)


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
