"""Services d'identité — ADR-003, ADR-004.

`accounts` porte un service parce que ses opérations remplissent les critères :
elles écrivent dans plusieurs tables sous transaction et portent une décision
de sécurité. L'inscription et le changement de mot de passe ne sont pas des
CRUD.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Device, User, UserType

__all__ = ["AuthService", "TokenPair"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TokenPair:
    access: str
    refresh: str


class AuthService:
    """Opérations d'identité qui engagent la sécurité du compte."""

    @staticmethod
    def issue_tokens(user: User) -> TokenPair:
        """Émet un couple de jetons portant le type de compte.

        Le type est embarqué pour éviter une lecture base à chaque appel. Il
        peut donc être périmé jusqu'à l'expiration du jeton d'accès — quinze
        minutes — ce qui est sans risque de montée en privilège, puisqu'un
        changement de type révoque les jetons (`revoke_all_sessions`).
        """
        refresh = RefreshToken.for_user(user)
        refresh["user_type"] = user.user_type
        refresh["email"] = user.email
        return TokenPair(access=str(refresh.access_token), refresh=str(refresh))

    @staticmethod
    @transaction.atomic
    def register(
        *, email: str, password: str, full_name: str, phone: str | None = None
    ) -> tuple[User, TokenPair]:
        """Inscription — toujours en tant que client.

        Le type de compte n'est **pas** un paramètre d'entrée exposé : un
        livreur est créé par le back-office après instruction de son dossier,
        un membre du personnel par un pair. Accepter `user_type` du client
        serait une escalade de privilège en un champ de formulaire.
        """
        validate_password(password)
        user = User.objects.create_user(
            email=email,
            password=password,
            full_name=full_name,
            phone=phone or None,
            user_type=UserType.CUSTOMER,
        )
        return user, AuthService.issue_tokens(user)

    @staticmethod
    @transaction.atomic
    def change_password(*, user: User, current_password: str, new_password: str) -> TokenPair:
        """Change le mot de passe et **révoque les autres sessions** — T2.

        L'implémentation précédente ne révoquait rien : les sessions ouvertes
        ailleurs survivaient au changement, ce qui vide l'opération de son sens
        — on change son mot de passe précisément parce qu'on soupçonne qu'il a
        fuité.

        Toutes les sessions sont révoquées, y compris la courante, et un
        nouveau couple est émis. Conserver la session courante demanderait de
        faire confiance à un jeton fourni par le client, ce qui rouvre la porte
        qu'on vient de fermer.
        """
        if not user.check_password(current_password):
            raise ValueError("Le mot de passe actuel est incorrect.")
        if current_password == new_password:
            raise ValueError("Le nouveau mot de passe doit être différent de l'actuel.")

        validate_password(new_password, user=user)
        user.set_password(new_password)
        user.save(update_fields=["password"])

        AuthService.revoke_all_sessions(user)
        return AuthService.issue_tokens(user)

    @staticmethod
    def revoke_all_sessions(user: User) -> int:
        """Met en liste noire tous les jetons de rafraîchissement de l'utilisateur.

        Appelée au changement de mot de passe, à la désactivation d'un compte
        et au changement de type — les trois moments où un jeton en circulation
        représente un droit qui ne devrait plus exister.
        """
        revoked = 0
        for token in OutstandingToken.objects.filter(user=user):
            try:
                # Le stub de simple-jwt annonce `Token | None` là où la
                # bibliothèque accepte la chaîne encodée — c'est même son usage
                # principal. L'annotation est fausse, pas l'appel.
                RefreshToken(token.token).blacklist()  # type: ignore[arg-type]
                revoked += 1
            except Exception:
                # Jeton déjà expiré ou déjà en liste noire : le résultat
                # recherché est atteint. Interrompre la boucle laisserait les
                # jetons suivants — les valides — actifs, ce qui serait
                # exactement l'inverse du but. Journalisé en `debug` : c'est le
                # cas nominal, pas un incident, mais la trace reste disponible
                # quand on cherche pourquoi un compteur de révocation est bas.
                logger.debug("Jeton %s non révocable, ignoré.", token.pk, exc_info=True)
                continue
        return revoked

    @staticmethod
    def logout(*, refresh_token: str) -> None:
        """Révoque la session courante."""
        RefreshToken(refresh_token).blacklist()  # type: ignore[arg-type]

    @staticmethod
    def register_device(*, user: User, token: str, platform: str) -> Device:
        """Enregistre un appareil pour les notifications push.

        `update_or_create` sur le **jeton** et non sur le couple
        (utilisateur, jeton) : un téléphone qui change de compte doit être
        réattribué, sans quoi deux utilisateurs se retrouvent abonnés au même
        appareil et le second reçoit les notifications du premier.
        """
        device, _ = Device.objects.update_or_create(
            token=token, defaults={"user": user, "platform": platform}
        )
        return device
