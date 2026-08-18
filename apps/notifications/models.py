"""Notifications in-app — ADR-008.

Le WebSocket et le push sont des **transports** : ils atteignent l'utilisateur
s'il est là, et se perdent sinon. La notification est la trace persistante du
même événement — ce que l'utilisateur retrouve en ouvrant son application deux
heures plus tard, quand le socket était fermé et la bannière push balayée.

D'où une table plutôt qu'un simple envoi. L'implémentation précédente n'en
avait pas : un changement de statut manqué était manqué pour toujours.
"""

from __future__ import annotations

from django.db import models

from apps.accounts.models import User
from common.models import TimeStampedModel, UUIDModel

__all__ = ["Audience", "Campaign", "CampaignStatus", "Notification", "NotificationKind"]


class NotificationKind(models.TextChoices):
    ORDER_STATUS = "order_status", "Statut de commande"
    DELIVERY_OFFER = "delivery_offer", "Course proposée"
    PAYMENT = "payment", "Paiement"
    ACCOUNT = "account", "Compte"
    MARKETING = "marketing", "Marketing"


class Notification(UUIDModel, TimeStampedModel):
    """Message destiné à une personne, lisible après coup.

    `kind` n'est pas décoratif : il porte la distinction entre le
    **transactionnel** et le **marketing**, qui décide si l'envoi respecte les
    préférences de l'utilisateur. « Votre livreur arrive » part quoi qu'il
    arrive ; « −20 % ce week-end » se coupe.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=16, choices=NotificationKind.choices, db_index=True)

    title = models.CharField(max_length=120)
    body = models.TextField()

    # Charge utile minimale, à l'usage du client : de quoi ouvrir le bon écran.
    # Pas de duplication de l'objet métier — il aura changé d'ici la lecture.
    data = models.JSONField(default=dict, blank=True)

    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "notification"
        ordering = ["-created_at"]
        indexes = [
            # La requête de l'écran : mes notifications, les plus récentes
            # d'abord. Et le compteur de non-lues, qui est la même requête
            # filtrée.
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user", "read_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} — {self.title}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class Audience(models.TextChoices):
    """Segments adressables par une campagne.

    Volontairement **fermé** : « les clients qui n'ont pas commandé depuis
    trente jours » est une campagne de reconquête qu'on relance chaque mois, pas
    une requête à réécrire à chaque envoi. Un champ de filtre libre ferait
    entrer du langage de requête dans un formulaire d'exploitation, avec ce que
    cela suppose de fautes silencieuses sur la population visée.

    Les segments ne dépendent que de `accounts` et de `orders` — les seules
    applications que `notifications` connaît (ADR-002). Un ciblage par ville ou
    par établissement demanderait d'inverser le graphe.
    """

    ALL_CUSTOMERS = "all_customers", "Tous les clients"
    COURIERS = "couriers", "Tous les livreurs"
    ACTIVE_CUSTOMERS = "active_customers", "Clients ayant commandé récemment"
    LAPSED_CUSTOMERS = "lapsed_customers", "Clients sans commande récente"


class CampaignStatus(models.TextChoices):
    DRAFT = "draft", "Brouillon"
    SENT = "sent", "Envoyée"


class Campaign(UUIDModel, TimeStampedModel):
    """Envoi de masse préparé, tracé, et qui ne part qu'une fois.

    C'est ce qui la distingue du reste du module : les notifications
    transactionnelles partent une par une, à la personne concernée, déclenchées
    par un événement métier. Ici, quelqu'un décide d'écrire à plusieurs milliers
    de gens à la fois — d'où trois propriétés que la table porte :

    * **elle se prépare avant de partir** (`status`) : rédiger et envoyer dans
      la même requête ne laisse aucune place à la relecture, et une faute de
      frappe dans un envoi de masse ne se rattrape pas ;
    * **elle laisse une trace** (`created_by`, `sent_at`) : sans elle,
      « pourquoi ce client a-t-il reçu ça ? » est une question sans réponse ;
    * **elle ne part qu'une fois** (`sent_at`) : rien dans le message ne
      permettrait de reconnaître un doublon après coup.
    """

    title = models.CharField(max_length=120)
    body = models.TextField()

    audience = models.CharField(max_length=24, choices=Audience.choices)
    segment_days = models.PositiveSmallIntegerField(
        default=30,
        help_text="Fenêtre des segments « récemment » et « sans commande récente ».",
    )

    status = models.CharField(
        max_length=8, choices=CampaignStatus.choices, default=CampaignStatus.DRAFT, db_index=True
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    # Nombre de notifications **réellement écrites**, donc hors comptes ayant
    # refusé le marketing. Compter la taille du segment plutôt que les envois
    # aboutis donnerait un taux d'ouverture flatteur et faux.
    recipient_count = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="campaigns", null=True, blank=True
    )

    class Meta:
        verbose_name = "campagne"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.title} ({self.get_status_display()})"
