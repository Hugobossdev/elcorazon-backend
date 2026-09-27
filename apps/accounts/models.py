"""Identité et accès — ADR-004, ADR-005.

Le modèle d'autorisation a deux étages : le **type de compte**, structurel et
porté par le JWT, et les **permissions**, réservées au personnel et portées par
des rôles cumulables. L'appartenance de la ressource — « ce client ne voit que
ses commandes » — est un troisième étage qui vit dans les `get_queryset`, pas
ici.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from apps.accounts.permissions import PERMISSION_CHOICES, validate_permissions
from common.models import TimeStampedModel, UUIDModel
from common.storage import user_media

__all__ = [
    "Device",
    "Role",
    "User",
    "UserType",
    "VerificationCode",
    "VerificationPurpose",
]


class UserType(models.TextChoices):
    """Type de compte — structurel, un seul par utilisateur.

    Détermine l'application utilisable et la nature des ressources accessibles.
    Un changement de type révoque les jetons (ADR-004), car il est embarqué
    dans le JWT.
    """

    CUSTOMER = "customer", "Client"
    COURIER = "courier", "Livreur"
    STAFF = "staff", "Personnel"


phone_validator = RegexValidator(
    regex=r"^\+[1-9]\d{7,14}$",
    message="Le numéro doit être au format international E.164, par exemple +22890123456.",
)


class Role(UUIDModel, TimeStampedModel):
    """Groupement nommé de permissions.

    Le code ne teste jamais un nom de rôle. Ce modèle n'existe que pour
    permettre à un administrateur de composer des jeux de permissions sans
    redéploiement.
    """

    name = models.CharField(max_length=64, unique=True)
    description = models.TextField(blank=True)
    permissions = ArrayField(
        models.CharField(max_length=64, choices=PERMISSION_CHOICES),
        default=list,
        blank=True,
    )
    # Un rôle système ne peut pas être supprimé : retirer « Super Admin » d'une
    # instance en production enfermerait tout le monde dehors.
    is_system = models.BooleanField(default=False)

    class Meta:
        verbose_name = "rôle"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        validate_permissions(self.permissions)

    def save(self, *args: Any, **kwargs: Any) -> None:
        # `clean()` n'est pas appelé automatiquement par `save()` ; sans cet
        # appel, une permission inexistante entrerait en base par l'API ou un
        # script de données.
        validate_permissions(self.permissions)
        super().save(*args, **kwargs)


class UserManager(BaseUserManager["User"]):
    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        if not email:
            raise ValueError("L'adresse e-mail est obligatoire.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str, **extra: Any) -> User:
        extra.setdefault("user_type", UserType.STAFF)
        extra.setdefault("is_superuser", True)
        extra.setdefault("full_name", "Super administrateur")
        if extra["user_type"] != UserType.STAFF:
            raise ValueError("Un superutilisateur est nécessairement du personnel.")
        return self.create_user(email, password, **extra)


class User(UUIDModel, AbstractBaseUser, TimeStampedModel):
    """Compte utilisateur, tous types confondus.

    Volontairement dépourvu de `PermissionsMixin` : les permissions natives de
    Django sont adossées aux modèles et aux opérations CRUD, alors que le
    vocabulaire métier ici est `orders.refund` — qui n'est ni un modèle, ni un
    CRUD (ADR-005).
    """

    email = models.EmailField(unique=True)
    phone = models.CharField(
        max_length=16, unique=True, null=True, blank=True, validators=[phone_validator]
    )
    full_name = models.CharField(max_length=150)
    user_type = models.CharField(
        max_length=16, choices=UserType.choices, default=UserType.CUSTOMER, db_index=True
    )

    # Compartiment public : un avatar s'affiche dans une liste de commandes,
    # aux côtés de dizaines d'autres. Le signer coûterait une signature par
    # vignette et interdirait toute mise en cache (ADR-011).
    avatar = models.ImageField(upload_to="avatars/", storage=user_media, null=True, blank=True)

    is_active = models.BooleanField(
        default=True,
        help_text="Décoché plutôt que supprimé : les commandes passées y renvoient.",
    )
    is_superuser = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    roles = models.ManyToManyField(Role, related_name="users", blank=True)

    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = ["full_name"]

    class Meta:
        verbose_name = "utilisateur"
        indexes = [
            # Le back-office liste les clients actifs et les livreurs en ligne :
            # deux filtres toujours combinés.
            models.Index(fields=["user_type", "is_active"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(is_superuser=False) | models.Q(user_type="staff"),
                name="superuser_is_staff",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} <{self.email}>"

    # ------------------------------------------------------------ autorisation

    @property
    def is_customer(self) -> bool:
        return self.user_type == UserType.CUSTOMER

    @property
    def is_courier(self) -> bool:
        return self.user_type == UserType.COURIER

    @property
    def is_staff_member(self) -> bool:
        return self.user_type == UserType.STAFF

    def permission_codes(self) -> set[str]:
        """Union des permissions de tous les rôles de l'utilisateur."""
        if not self.is_staff_member:
            return set()
        return {code for role in self.roles.all() for code in role.permissions}

    def has_permission(self, code: str) -> bool:
        """Point d'entrée unique de l'autorisation du personnel.

        Un compte inactif ne détient plus rien, quels que soient ses rôles :
        c'est ce qui rend la désactivation immédiatement effective.
        """
        if not self.is_active:
            return False
        if self.is_superuser:
            return True
        return code in self.permission_codes()

    # ------------------------------------------------------- interface admin

    # `django.contrib.admin` attend ces trois membres. Les implémenter en
    # déléguant à notre modèle évite d'embarquer `PermissionsMixin` — donc
    # d'avoir deux systèmes de permissions concurrents, exactement ce que
    # l'ADR-005 supprime.

    @property
    def is_staff(self) -> bool:
        return self.is_staff_member and self.is_active

    def has_perm(self, perm: str, obj: Any = None) -> bool:
        return self.has_permission(perm)

    def has_module_perms(self, app_label: str) -> bool:
        if self.is_superuser:
            return True
        return any(code.startswith(f"{app_label}.") for code in self.permission_codes())

    def touch_last_seen(self) -> None:
        self.last_seen_at = timezone.now()
        self.save(update_fields=["last_seen_at"])


class CustomerNote(UUIDModel, TimeStampedModel):
    """Note **interne** du personnel sur un client.

    Ce que l'équipe sait et que le client ne lit pas : un litige en cours, une
    adresse difficile, un geste consenti. Demandée par le cahier des charges
    (§4.2.6), absente jusqu'ici. Elle ne se modifie ni ne s'efface.
    """

    customer = models.ForeignKey("User", on_delete=models.CASCADE, related_name="staff_notes_about")
    author = models.ForeignKey("User", on_delete=models.PROTECT, related_name="+")
    content = models.TextField()

    class Meta:
        verbose_name = "note interne sur un client"
        verbose_name_plural = "notes internes sur un client"
        ordering: ClassVar[list[str]] = ["created_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["customer", "created_at"])]

    def __str__(self) -> str:
        return f"{self.customer_id} — {self.author_id}"


class DevicePlatform(models.TextChoices):
    IOS = "ios", "iOS"
    ANDROID = "android", "Android"
    WEB = "web", "Web"


class Device(UUIDModel, TimeStampedModel):
    """Appareil enregistré pour les notifications push.

    Rattaché à l'**utilisateur**, pas à la session : un jeton d'accès expire
    toutes les 15 minutes alors qu'un appareil doit rester joignable.

    Le jeton est unique globalement, ce qui rend l'enregistrement idempotent et
    réattribue correctement un téléphone qui change de compte — sinon deux
    utilisateurs se retrouvent abonnés au même appareil, et le second reçoit
    les notifications du premier.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    token = models.CharField(max_length=512, unique=True)
    platform = models.CharField(max_length=8, choices=DevicePlatform.choices)
    last_used_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "appareil"
        indexes = [models.Index(fields=["user", "-last_used_at"])]

    def __str__(self) -> str:
        return f"{self.get_platform_display()} — {self.user.email}"


class VerificationPurpose(models.TextChoices):
    """À quoi sert un code — et donc ce qu'il autorise une fois présenté.

    Le motif est stocké **et vérifié** à la présentation. Sans lui, un code
    obtenu par « mot de passe oublié » vaudrait vérification de compte, et
    inversement : un seul canal suffirait alors à obtenir les deux effets.
    """

    ACCOUNT_VERIFICATION = "account_verification", "Vérification du compte"
    PASSWORD_RESET = "password_reset", "Réinitialisation du mot de passe"


class VerificationCode(UUIDModel, TimeStampedModel):
    """Code à usage unique envoyé à l'utilisateur, et sa trace.

    ## Le code n'est pas stocké

    Seule son empreinte l'est, par le même jeu de hacheurs que les mots de
    passe (`check_password`). Une copie de la base ne rend donc pas les codes
    en circulation : elle rendrait, sinon, la capacité de valider n'importe
    quel compte et de réinitialiser n'importe quel mot de passe — c'est-à-dire
    exactement ce que ce mécanisme est censé protéger.

    ## Un seul code vivant à la fois

    La contrainte partielle `one_live_code_per_purpose` le garantit en base.
    Sans elle, chaque renvoi laisserait le précédent utilisable : au bout de
    dix demandes, dix codes valides pour le même compte, et l'espace à deviner
    est divisé d'autant. `consumed_at` couvre donc les deux fins possibles —
    **employé** par son destinataire, ou **remplacé** par un renvoi. Dans les
    deux cas il ne sert plus, et c'est la seule chose que la contrainte a
    besoin de savoir.

    ## Les tentatives sont comptées

    Six chiffres, c'est un million de possibilités : sans compteur, un
    attaquant qui les parcourt trouve en quelques minutes. `attempts` ferme le
    code après un petit nombre d'essais — la limitation de débit HTTP ne suffit
    pas, elle se contourne en répartissant les requêtes.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verification_codes")
    purpose = models.CharField(max_length=32, choices=VerificationPurpose.choices, db_index=True)
    code_hash = models.CharField(max_length=128)

    # L'adresse **réellement servie**, recopiée ici. Celle du compte peut
    # changer ensuite ; savoir où le code est parti est ce qu'on cherche quand
    # un utilisateur affirme n'avoir rien reçu.
    sent_to = models.EmailField()

    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "code de vérification"
        verbose_name_plural = "codes de vérification"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "purpose"],
                condition=models.Q(consumed_at__isnull=True),
                name="one_live_code_per_purpose",
            ),
        ]
        indexes = [models.Index(fields=["user", "purpose", "-created_at"])]

    def __str__(self) -> str:
        # Jamais le code, ni son empreinte : cette chaîne finit dans les
        # journaux d'administration et les traces d'erreur.
        return f"{self.get_purpose_display()} — {self.sent_to}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_live(self) -> bool:
        """Encore présentable : ni employé, ni remplacé, ni périmé."""
        return self.consumed_at is None and not self.is_expired
