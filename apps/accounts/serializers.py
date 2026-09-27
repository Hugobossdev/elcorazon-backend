"""Contrats d'entrée et de sortie de l'identité — ADR-009.

Les sérialiseurs valident la **forme**. Les décisions métier — le mot de passe
actuel est-il correct, faut-il révoquer les sessions — appartiennent au service.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.accounts.models import CustomerNote, Device, DevicePlatform, Role, User
from apps.accounts.permissions import PERMISSIONS
from common.serializers import MoneyField

__all__ = [
    "BlockSerializer",
    "ChangePasswordSerializer",
    "CustomerSerializer",
    "CustomerStatsSerializer",
    "DeviceSerializer",
    "LoginSerializer",
    "PasswordResetConfirmSerializer",
    "PermissionSerializer",
    "ProfileUpdateSerializer",
    "RegisterSerializer",
    "RoleSerializer",
    "TokenPairSerializer",
    "UserSerializer",
    "VerificationChallengeSerializer",
    "VerificationRequestSerializer",
    "VerifyCodeSerializer",
]


class ProfileUpdateSerializer(serializers.ModelSerializer[User]):
    """Ce qu'un compte peut changer **de lui-même**.

    Deux champs, et pas un de plus. Ni `email` — il identifie le compte et sert
    à s'y connecter, le changer se fait avec une vérification —, ni
    `user_type` : un client qui pourrait s'écrire « livreur » ou « staff » se
    donnerait des droits. L'implémentation Supabase écrivait la table `users`
    avec un dictionnaire libre, où rien n'interdisait ces deux clés.
    """

    class Meta:
        model = User
        fields = ["full_name", "phone"]

    def validate_phone(self, value: str | None) -> str | None:
        """Un numéro effacé vaut `NULL`, et non la chaîne vide.

        `phone` est `unique` et `null=True`. Deux `NULL` cohabitent en SQL, deux
        chaînes vides non : le **premier** compte qui efface son numéro passe,
        le second se heurte à la contrainte d'unicité — c'est-à-dire une 500 sur
        un écran de profil, pour un champ que rien n'oblige à remplir.

        Le cas n'est pas théorique : `Dely` envoie le contenu du champ tel quel,
        et un livreur qui n'a pas déclaré de numéro le laisse vide. Le
        sérialiseur du back-office (`CourierUpdateSerializer`) fait la même
        conversion depuis l'origine ; celui-ci ne la faisait pas.
        """
        return (value or "").strip() or None


class UserSerializer(serializers.ModelSerializer[User]):
    """Représentation publique d'un compte.

    Forme **unique** : `/auth/register`, `/auth/login` et `/auth/me` renvoient
    exactement les mêmes clés. L'implémentation précédente en avait deux —
    8 clés à l'inscription contre 15 sur `/me` — deux formes divergentes du
    même objet, que chaque client devait apprendre séparément.
    """

    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "phone",
            "full_name",
            "user_type",
            "avatar",
            "is_active",
            "email_verified_at",
            "phone_verified_at",
            "last_seen_at",
            "permissions",
            # Le siège, en lecture seule. Le back-office en a besoin pour dire
            # *pourquoi* une action est refusée : ce qui relève de l'enseigne —
            # une campagne, un pays, un code promotionnel national — n'est ouvert
            # qu'aux comptes non cloisonnés, et un bouton qui échoue en 403 sans
            # l'expliquer se lit comme une panne. `is_unscoped` n'est pas
            # déductible des permissions : un gérant peut détenir
            # `notifications.send` et n'être pas le siège.
            "is_superuser",
            "created_at",
            "updated_at",
        ]
        # `created_at` et `updated_at` ne sont jamais omis : les clients Dart
        # actuels appellent `DateTime.parse` sans garde nulle, et un champ
        # absent ne dégrade pas l'affichage — il fait planter la connexion.
        read_only_fields = fields

    def get_permissions(self, obj: User) -> list[str]:
        """Vide pour un client ou un livreur — seul le personnel en détient."""
        return sorted(obj.permission_codes())


class TokenPairSerializer(serializers.Serializer[Any]):
    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)
    user = UserSerializer(read_only=True)


class RegisterSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    full_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=16, required=False, allow_blank=True)

    def validate_email(self, value: str) -> str:
        normalized = value.strip().lower()
        if User.objects.filter(email__iexact=normalized).exists():
            raise serializers.ValidationError("Un compte existe déjà avec cette adresse.")
        return normalized

    def validate_phone(self, value: str) -> str:
        if value and User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("Un compte existe déjà avec ce numéro.")
        return value

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value


class LoginSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class ChangePasswordSerializer(serializers.Serializer[Any]):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "Le nouveau mot de passe doit être différent de l'actuel."}
            )
        return attrs


class RefreshSerializer(serializers.Serializer[Any]):
    refresh = serializers.CharField()


# ------------------------------------------------- vérification par code


def _code_field() -> serializers.RegexField:
    """Le champ « code », taillé sur la longueur réellement émise.

    Construit à partir du réglage plutôt que figé à six chiffres : les deux
    valeurs doivent bouger ensemble, et un formulaire qui refuserait un code
    que le serveur vient d'envoyer serait un blocage sans message
    compréhensible.
    """
    longueur = settings.ACCOUNT_VERIFICATION_CODE_LENGTH
    return serializers.RegexField(
        rf"^\d{{{longueur}}}$",
        error_messages={"invalid": f"Le code comporte {longueur} chiffres."},
        trim_whitespace=True,
    )


class VerificationRequestSerializer(serializers.Serializer[Any]):
    """Demande d'envoi ou de renvoi d'un code : l'adresse, et rien d'autre.

    Pas de mot de passe : cette route s'emprunte précisément quand on ne l'a
    plus, ou quand on n'a pas encore de session.
    """

    email = serializers.EmailField()

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class VerifyCodeSerializer(VerificationRequestSerializer):
    """Présentation d'un code reçu."""

    code = _code_field()


class PasswordResetConfirmSerializer(VerifyCodeSerializer):
    """Code reçu + nouveau mot de passe.

    Le mot de passe est éprouvé ici **sans utilisateur**, ce qui écarte le seul
    validateur qui en demande un — la similarité avec le nom et l'adresse. Ce
    n'est pas un oubli : le service refera la passe complète, en connaissant le
    compte, une fois le code accepté. La faire ici avec l'utilisateur
    obligerait à retrouver le compte **avant** d'avoir vu le moindre code, et à
    répondre différemment selon qu'il existe ou non — c'est-à-dire à dire à qui
    demande si telle adresse est inscrite chez nous.
    """

    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate_new_password(self, value: str) -> str:
        validate_password(value)
        return value


class VerificationChallengeSerializer(serializers.Serializer[Any]):
    """Ce que le serveur rend après avoir émis un code.

    `retry_after` et `expires_at` viennent du serveur et pas d'une constante
    côté client : ce sont les mêmes réglages qui gouvernent réellement
    l'émission, et un compte à rebours qui les devinerait finirait par proposer
    « Renvoyer » à un moment où le serveur refuse encore.

    Rien de ce corps ne dit si l'adresse correspond à un compte. La réponse est
    identique dans les deux cas, y compris `retry_after` — un délai nul pour
    une adresse inconnue et soixante secondes pour une adresse connue suffirait
    à faire de cette route un annuaire.
    """

    email = serializers.EmailField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)
    retry_after = serializers.IntegerField(read_only=True)
    code_length = serializers.IntegerField(read_only=True)
    detail = serializers.CharField(read_only=True)


# --------------------------------------------------------------- back-office


class CustomerSerializer(serializers.ModelSerializer[User]):
    """Dossier client vu du service client.

    Intégralement en lecture seule : le nom, l'adresse électronique et le
    téléphone sont les données du client, qu'il modifie depuis son application.
    Les rendre éditables ici ouvrirait un chemin pour changer l'adresse d'un
    compte — donc pour en prendre le contrôle par « mot de passe oublié ».

    Le seul geste d'exploitation est le blocage, et il a sa propre route.
    """

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "phone",
            "full_name",
            "avatar",
            "is_active",
            "email_verified_at",
            "phone_verified_at",
            "last_seen_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class CustomerStatsSerializer(serializers.Serializer[Any]):
    """Fiche chiffrée d'un client — ce que le service client lit avant de parler.

    Tout y est **calculé par le serveur**. L'implémentation Supabase demandait
    au client d'aller chercher les commandes, les adresses et les points, puis
    de faire les totaux lui-même : cinq requêtes depuis un poste de travail, et
    surtout un panier moyen qui dépendait de ce que la pagination avait bien
    voulu rendre.

    Les montants restent des objets `Money` (ADR-007) : un « total dépensé »
    rendu en nombre serait converti en `double` par le client, et l'exactitude
    défendue jusqu'en base se perdrait au dernier mètre.
    """

    orders_count = serializers.IntegerField()
    orders_delivered = serializers.IntegerField()
    orders_cancelled = serializers.IntegerField()
    total_spent = MoneyField()
    average_basket = MoneyField()
    first_order_at = serializers.DateTimeField(allow_null=True)
    last_order_at = serializers.DateTimeField(allow_null=True)
    addresses_count = serializers.IntegerField()
    loyalty_balance = serializers.IntegerField()
    loyalty_lifetime_earned = serializers.IntegerField()


class BlockSerializer(serializers.Serializer[Any]):
    """Motif du blocage.

    Exigé — et non facultatif : un compte fermé sans motif est un litige qu'on
    ne saura pas instruire six mois plus tard, quand le client rappellera.
    """

    reason = serializers.CharField(max_length=280)


class PermissionSerializer(serializers.Serializer[Any]):
    """Une entrée du registre des permissions.

    Le libellé sort sous `description` et non sous `label` : `label` est un
    attribut de `serializers.Field`, et un champ de ce nom l'écraserait — ce
    que le vérificateur de types signale, à juste titre.
    """

    code = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)


class RoleSerializer(serializers.ModelSerializer[Role]):
    """Rôle et ses permissions.

    La validation contre le registre est ici **en plus** de celle du modèle :
    `Role.save()` lève une `ValidationError` Django, que DRF ne traduit pas —
    elle sortirait en 500. Une faute de frappe (`orders.refunds`) mérite un 400
    qui nomme les valeurs admises, pas une erreur serveur.
    """

    permissions = serializers.ListField(
        child=serializers.CharField(max_length=64), allow_empty=True
    )

    class Meta:
        model = Role
        fields = ["id", "name", "description", "permissions", "is_system", "created_at"]
        read_only_fields = ["id", "is_system", "created_at"]

    def validate_permissions(self, value: list[str]) -> list[str]:
        inconnues = sorted(set(value) - set(PERMISSIONS))
        if inconnues:
            raise serializers.ValidationError(
                f"Permissions inconnues : {', '.join(inconnues)}. "
                f"Valeurs admises : {', '.join(sorted(PERMISSIONS))}."
            )
        # Dédoublonné et trié : deux rôles portant les mêmes droits dans un
        # ordre différent se comparent alors à l'œil, dans un écran d'audit.
        return sorted(set(value))


class DeviceSerializer(serializers.ModelSerializer[Device]):
    platform = serializers.ChoiceField(choices=DevicePlatform.choices)

    # `Device.token` est unique en base, ce dont `ModelSerializer` déduit
    # automatiquement un `UniqueValidator`. Il rejette alors en 400 tout jeton
    # déjà enregistré — or c'est le cas **nominal** : ré-enregistrer un appareil
    # au lancement de l'application, ou le réattribuer quand son propriétaire
    # change de compte. La validation héritée contredisait donc l'upsert que
    # `AuthService.register_device` fait exprès, et la requête n'atteignait
    # jamais le service. On la retire ; l'unicité reste tenue par la contrainte
    # de base, qui est le bon endroit pour elle.
    token = serializers.CharField(max_length=512, validators=[])

    class Meta:
        model = Device
        fields = ["id", "token", "platform", "last_used_at", "created_at"]
        read_only_fields = ["id", "last_used_at", "created_at"]


class CustomerNoteSerializer(serializers.ModelSerializer[CustomerNote]):
    """Une note interne sur un client — jamais rendue au client."""

    author_name = serializers.CharField(source="author.full_name", read_only=True, default=None)

    class Meta:
        model = CustomerNote
        fields = ["id", "author_name", "content", "created_at"]
        read_only_fields = ["id", "author_name", "created_at"]

    def validate_content(self, value: str) -> str:
        texte = value.strip()
        if not texte:
            raise serializers.ValidationError("Une note vide n'apprend rien à personne.")
        return texte
