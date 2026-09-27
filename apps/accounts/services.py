"""Services d'identité — ADR-003, ADR-004.

`accounts` porte un service parce que ses opérations remplissent les critères :
elles écrivent dans plusieurs tables sous transaction et portent une décision
de sécurité. L'inscription et le changement de mot de passe ne sont pas des
CRUD.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import (
    Device,
    User,
    UserType,
    VerificationCode,
    VerificationPurpose,
)
from common.exceptions import BusinessRuleViolation

__all__ = [
    "AuthService",
    "InvalidVerificationCode",
    "TokenPair",
    "VerificationChallenge",
    "VerificationService",
]

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

        # Un code part aussi pour un client, et le couple de jetons est rendu
        # quand même : la vérification de l'adresse est **proposée** ici, elle
        # n'est pas opposée. Un client qui commande un repas n'a pas à attendre
        # un courriel pour voir la carte, et le lui imposer briserait l'app
        # cliente qui existe et fonctionne. La différence avec le livreur tient
        # à la route employée : `POST /delivery/apply/` ne rend, elle, aucun
        # jeton — voir `CourierApplicationView`.
        VerificationService.issue(user=user, purpose=VerificationPurpose.ACCOUNT_VERIFICATION)

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


class InvalidVerificationCode(BusinessRuleViolation):
    """Code absent, faux, périmé, déjà employé, ou trop d'essais.

    **Un seul motif pour cinq situations**, et c'est délibéré : les distinguer
    dirait à qui essaie s'il vise un compte existant, si le code a déjà servi,
    ou s'il lui reste des essais — trois renseignements dont seul un attaquant
    a l'usage. Le destinataire légitime, lui, n'a qu'un geste possible dans les
    cinq cas : redemander un code.

    En **400** et non en 409 comme le reste de sa famille : ce qui est refusé
    est la valeur soumise, pas l'état du système. Le client la traite comme une
    saisie à corriger, au même titre qu'un champ invalide.
    """

    code = "invalid_verification_code"
    # `HTTPStatus` et non `rest_framework.status` : un service ne connaît pas
    # le transport (ADR-003, vérifié par `test_les_services_ne_connaissent_pas
    # _le_transport`). Le nombre, lui, appartient au vocabulaire de HTTP, pas à
    # celui de DRF — et c'est bien un nombre que le gestionnaire d'exceptions
    # attend.
    status_code = HTTPStatus.BAD_REQUEST
    title = "Code de vérification refusé"


@dataclass(frozen=True, slots=True)
class VerificationChallenge:
    """Ce que le serveur dit au client après avoir émis (ou refusé d'émettre).

    Aucune de ces valeurs ne trahit le code. `retry_after` et `expires_at`
    existent pour que l'écran sache animer son compte à rebours et annoncer la
    péremption **sans les recalculer** : deux applications qui décideraient
    elles-mêmes de ces durées en afficheraient tôt ou tard deux différentes, et
    l'une des deux mentirait au livreur.
    """

    email: str
    expires_at: dt.datetime
    retry_after: int
    code_length: int


class VerificationService:
    """Codes à usage unique : émission, renvoi, présentation.

    Le canal est le **courriel**, et lui seul. C'est le seul dont ce backend
    dispose réellement (`EMAIL_BACKEND`, console en développement, SMTP en
    production) ; aucun opérateur SMS n'est configuré ni facturé. Prétendre
    envoyer un SMS reviendrait à écrire un écran « vérifiez votre numéro »
    devant un code que personne ne recevrait jamais.
    """

    @staticmethod
    def _new_code() -> str:
        """Code décimal tiré du générateur cryptographique.

        `secrets` et non `random` : le second est un Mersenne Twister dont
        l'état se reconstitue à partir de quelques tirages observés — et
        observer les tirages, ici, c'est simplement demander des codes pour son
        propre compte.

        Le zéro de tête est conservé (`zfill`) : sans lui un code sur dix
        serait rendu à cinq chiffres, et le champ de saisie n'accepterait
        jamais sa validation.
        """
        length = settings.ACCOUNT_VERIFICATION_CODE_LENGTH
        return str(secrets.randbelow(10**length)).zfill(length)

    @staticmethod
    def _challenge(record: VerificationCode) -> VerificationChallenge:
        cooldown = dt.timedelta(seconds=settings.ACCOUNT_VERIFICATION_RESEND_COOLDOWN_SECONDS)
        remaining = (record.created_at + cooldown) - timezone.now()
        return VerificationChallenge(
            email=record.sent_to,
            expires_at=record.expires_at,
            # Jamais négatif : un client qui recevrait −3 en ferait un compte à
            # rebours qui ne se termine pas.
            #
            # Arrondi **au-dessus**, et c'est ce qui ferme un oracle. Le délai
            # restant d'un code qui vient d'être émis vaut 59,99 s, pas 60 :
            # tronquer rendait 59 pour une adresse connue là où la réponse
            # muette d'une adresse inconnue annonçait la constante, 60. L'écart
            # d'une seconde suffisait à distinguer les deux — c'est-à-dire à
            # faire de cette route l'annuaire d'abonnés que tout le reste du
            # module s'applique à ne pas être. Arrondir au-dessus les fait
            # coïncider, et ne fait jamais réessayer trop tôt.
            retry_after=max(0, math.ceil(remaining.total_seconds())),
            code_length=settings.ACCOUNT_VERIFICATION_CODE_LENGTH,
        )

    @staticmethod
    @transaction.atomic
    def issue(*, user: User, purpose: str) -> VerificationChallenge:
        """Émet un code et l'expédie — ou rend le précédent s'il est trop récent.

        Le délai de garde est appliqué **ici**, dans le service, et pas
        seulement par la limitation de débit HTTP. Les deux ne protègent pas la
        même chose : le limiteur borne le nombre de requêtes qu'une origine
        peut émettre, ce service borne le nombre de courriels qu'une **adresse**
        peut recevoir. Sans lui, un client mobile qui réémet sa requête sur un
        réseau lent expédie trois courriels pour un geste.

        Le code précédent est invalidé à chaque nouvelle émission. Le laisser
        vivre diviserait l'espace à deviner par le nombre de renvois : dix
        demandes, dix codes valides.
        """
        now = timezone.now()
        cooldown = dt.timedelta(seconds=settings.ACCOUNT_VERIFICATION_RESEND_COOLDOWN_SECONDS)

        recent = (
            VerificationCode.objects.filter(
                user=user,
                purpose=purpose,
                consumed_at__isnull=True,
                expires_at__gt=now,
                created_at__gt=now - cooldown,
            )
            .order_by("-created_at")
            .first()
        )
        if recent is not None:
            return VerificationService._challenge(recent)

        VerificationCode.objects.filter(
            user=user, purpose=purpose, consumed_at__isnull=True
        ).update(consumed_at=now)

        code = VerificationService._new_code()
        record = VerificationCode.objects.create(
            user=user,
            purpose=purpose,
            code_hash=make_password(code),
            sent_to=user.email,
            expires_at=now + dt.timedelta(seconds=settings.ACCOUNT_VERIFICATION_CODE_TTL_SECONDS),
        )

        # Après le commit, pour deux raisons distinctes : une panne de
        # messagerie ne doit pas annuler la création du compte qui vient
        # d'aboutir, et un courriel parti pour une transaction qui sera
        # ensuite annulée annoncerait un code qui n'existe pas.
        transaction.on_commit(lambda: VerificationService._send(record, code, purpose))

        return VerificationService._challenge(record)

    @staticmethod
    def _send(record: VerificationCode, code: str, purpose: str) -> None:
        """Expédie le courriel — en absorbant l'échec.

        L'exception est journalisée et s'arrête là. Nous sommes après le
        commit : la relever ne défera rien, et la laisser remonter ferait
        échouer la requête HTTP **alors que le compte est créé** — l'utilisateur
        lirait « erreur » devant un compte qui existe, et sa seconde tentative
        se heurterait à « cette adresse est déjà prise ».

        Le code ne passe **jamais** par le journal. Il est ici en clair, une
        fois, le temps de l'écrire dans le message ; il n'existe nulle part
        ailleurs.
        """
        minutes = max(1, settings.ACCOUNT_VERIFICATION_CODE_TTL_SECONDS // 60)
        if purpose == VerificationPurpose.PASSWORD_RESET:
            sujet = "El Corazón — réinitialisation de votre mot de passe"
            intro = (
                "Vous avez demandé à choisir un nouveau mot de passe.\n"
                "Saisissez ce code dans l'application pour continuer :"
            )
            fin = (
                "Si vous n'êtes pas à l'origine de cette demande, ignorez ce "
                "message : votre mot de passe actuel reste valable."
            )
        else:
            sujet = "El Corazón — votre code de vérification"
            intro = (
                "Bienvenue chez El Corazón.\n"
                "Saisissez ce code dans l'application pour activer votre compte :"
            )
            fin = (
                "Si vous n'êtes pas à l'origine de cette demande, ignorez ce "
                "message : aucun compte ne sera activé sans ce code."
            )

        try:
            send_mail(
                subject=sujet,
                message=f"{intro}\n\n    {code}\n\nIl expire dans {minutes} minutes.\n\n{fin}\n",
                from_email=None,  # `DEFAULT_FROM_EMAIL`
                recipient_list=[record.sent_to],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Envoi du code %s impossible.", record.pk)

    @staticmethod
    def consume(
        *, user: User, purpose: str, code: str, on_success: Callable[[], None] | None = None
    ) -> None:
        """Présente un code, et n'exécute [on_success] que s'il est bon.

        ## Pourquoi la levée est *hors* de la transaction

        L'échec doit laisser une trace : c'est le compteur d'essais qui ferme un
        code au bout de quelques tentatives, et c'est lui qui rend la force
        brute impraticable sur six chiffres. Or lever à l'intérieur du bloc
        atomique annulerait l'incrément en même temps que le reste — le
        compteur resterait éternellement à zéro, et le mécanisme entier ne
        protégerait rien. On écrit donc, on sort, puis on lève.

        ## Pourquoi l'effet passe par un rappel

        [on_success] s'exécute **dans la même transaction** que la
        consommation, aussitôt après elle. Les deux tiennent ou tombent
        ensemble : un mot de passe refusé par les validateurs annule la
        consommation, et le code reste présentable — sans quoi choisir un
        mot de passe trop faible brûlerait le code et obligerait à recommencer
        la demande depuis le début. Appeler ce service puis agir à côté
        n'offrirait pas cette garantie : la consommation serait déjà validée.
        """
        now = timezone.now()
        echec = False

        with transaction.atomic():
            record = (
                VerificationCode.objects.select_for_update()
                .filter(user=user, purpose=purpose, consumed_at__isnull=True, expires_at__gt=now)
                .order_by("-created_at")
                .first()
            )

            if record is None or not check_password(code, record.code_hash):
                if record is not None:
                    record.attempts += 1
                    # Le dernier essai ferme le code : sans cela, la limite ne
                    # serait qu'un ralentisseur qu'on franchit en attendant.
                    if record.attempts >= settings.ACCOUNT_VERIFICATION_MAX_ATTEMPTS:
                        record.consumed_at = now
                    record.save(update_fields=["attempts", "consumed_at", "updated_at"])
                echec = True
            else:
                record.consumed_at = now
                record.save(update_fields=["consumed_at", "updated_at"])
                if on_success is not None:
                    on_success()

        if echec:
            raise InvalidVerificationCode(
                "Ce code est incorrect ou n'est plus valable. Demandez-en un nouveau."
            )

    @staticmethod
    def find(*, email: str) -> User | None:
        """Le compte visé par une adresse, sans jamais dire s'il existe.

        Rend `None` plutôt que de lever : c'est à l'appelant de décider quoi
        répondre, et la bonne réponse est presque toujours la même que celle du
        cas nominal.
        """
        return User.objects.filter(email__iexact=email.strip().lower()).first()

    @staticmethod
    def confirm_account(*, email: str, code: str) -> tuple[User, TokenPair]:
        """Valide un compte et ouvre la session — la seule route qui fasse les deux.

        C'est ici, et non à l'inscription, que le premier couple de jetons d'un
        livreur est émis. La différence est ce qui rend la vérification
        **opposable** : une inscription qui rendrait déjà des jetons laisserait
        entrer quelqu'un qui n'a jamais prouvé qu'il relève son adresse, et
        l'écran de saisie du code ne serait plus qu'une formalité que le client
        pourrait sauter.

        Un compte déjà vérifié ne fait pas exception : il faut quand même un
        code vivant, faute de quoi cette route deviendrait une connexion sans
        mot de passe.
        """
        user = VerificationService.find(email=email)
        if user is None:
            # Même refus que pour un code faux — voir `InvalidVerificationCode`.
            raise InvalidVerificationCode(
                "Ce code est incorrect ou n'est plus valable. Demandez-en un nouveau."
            )

        def marquer() -> None:
            if user.email_verified_at is None:
                user.email_verified_at = timezone.now()
                user.save(update_fields=["email_verified_at", "updated_at"])

        VerificationService.consume(
            user=user,
            purpose=VerificationPurpose.ACCOUNT_VERIFICATION,
            code=code,
            on_success=marquer,
        )

        return user, AuthService.issue_tokens(user)

    @staticmethod
    def reset_password(*, email: str, code: str, new_password: str) -> tuple[User, TokenPair]:
        """Repose un mot de passe oublié, et **révoque toutes les sessions**.

        La révocation est le même geste qu'au changement de mot de passe (T2),
        pour la même raison en plus fort : on réinitialise précisément quand on
        ne sait plus qui détient l'ancien.

        Le mot de passe est validé **une fois le code accepté**, à l'intérieur
        du bloc atomique : un refus annule alors la consommation du code, qui
        reste présentable. Valider avant obligerait à comparer le mot de passe
        aux attributs du compte — donc à confirmer que ce compte existe — à qui
        n'a présenté aucun code.
        """
        user = VerificationService.find(email=email)
        if user is None:
            raise InvalidVerificationCode(
                "Ce code est incorrect ou n'est plus valable. Demandez-en un nouveau."
            )

        def reposer() -> None:
            try:
                validate_password(new_password, user=user)
            except DjangoValidationError as erreur:
                # Rattachée au champ, sinon le formulaire ne l'affiche nulle
                # part. La transaction s'annule : le code reste utilisable, et
                # l'utilisateur n'a qu'à choisir un autre mot de passe.
                raise DjangoValidationError({"new_password": list(erreur.messages)}) from erreur

            user.set_password(new_password)
            # `email_verified_at` au passage : recevoir ce code prouve que
            # l'adresse est bien relevée, ce qui est exactement ce que la
            # vérification de compte établit. Le redemander ensuite serait
            # demander deux fois la même preuve.
            if user.email_verified_at is None:
                user.email_verified_at = timezone.now()
            user.save(update_fields=["password", "email_verified_at", "updated_at"])
            AuthService.revoke_all_sessions(user)

        VerificationService.consume(
            user=user,
            purpose=VerificationPurpose.PASSWORD_RESET,
            code=code,
            on_success=reposer,
        )

        return user, AuthService.issue_tokens(user)
