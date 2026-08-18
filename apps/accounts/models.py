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

__all__ = ["Device", "Role", "User", "UserType"]


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
