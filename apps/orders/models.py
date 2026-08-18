"""Commandes.

C'est l'agrégat comptable du produit. Trois principes s'y appliquent, chacun
répondant à une faille prouvée de l'implémentation précédente :

* **C1/C2** — les montants sont figés à la création, calculés serveur depuis le
  catalogue. Une ligne conserve le nom et le prix de l'article **au moment de
  l'achat** : changer un prix au catalogue ne doit jamais réécrire l'histoire.
* **C3/C4** — le statut ne s'écrit que par la machine à états, et la contrainte
  `CHECK` est générée depuis cette même machine. Le code et le schéma ne
  peuvent pas diverger puisqu'ils ont une origine unique.
* **ADR-009** — la création est idempotente par clé cliente. Un mobile qui perd
  le réseau après l'envoi retente ; sans clé, il crée une seconde commande.
"""

from __future__ import annotations

from django.db import models

from apps.accounts.models import User
from apps.catalog.models import MenuItem
from apps.orders.states import ORDER_MACHINE, OrderStatus
from apps.restaurants.models import Restaurant
from common.fields import MoneyField
from common.models import TimeStampedModel, UUIDModel, state_check_constraint

__all__ = ["IdempotencyKey", "Order", "OrderLine", "OrderStatusEvent", "PaymentMethod"]


class PaymentMethod(models.TextChoices):
    MOBILE_MONEY = "mobile_money", "Mobile Money"
    CASH = "cash", "Espèces à la livraison"
    WALLET = "wallet", "Portefeuille"
    CARD = "card", "Carte bancaire"


class Order(UUIDModel, TimeStampedModel):
    reference = models.CharField(
        max_length=12,
        unique=True,
        help_text="Référence courte communiquée au client et au livreur.",
    )

    restaurant = models.ForeignKey(Restaurant, on_delete=models.PROTECT, related_name="orders")
    customer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="orders")

    status = models.CharField(
        max_length=16,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
        db_index=True,
        # L'écriture passe par `OrderService.transition_to`. L'affectation
        # directe est interdite par une règle de revue, et le `CHECK` en base
        # reste le dernier rempart.
    )

    # --- Adresse : copie figée, pas une référence -------------------------
    #
    # Une clé étrangère vers `Address` casserait à la première suppression —
    # que le RGPD impose d'honorer. La commande porte donc sa propre copie, ce
    # qui la rend lisible pour toujours, indépendamment du carnet d'adresses.
    delivery_address_line = models.TextField()
    delivery_landmark = models.CharField(max_length=200, blank=True)
    delivery_location = models.JSONField(
        help_text="{'lat': …, 'lon': …} figé à la commande.",
    )
    delivery_instructions = models.TextField(blank=True)
    recipient_name = models.CharField(max_length=150)
    recipient_phone = models.CharField(max_length=16)

    # --- Montants (C2) ----------------------------------------------------
    subtotal = MoneyField()
    delivery_fee = MoneyField()
    # Ce que la course **vaut**, avant toute remise commerciale — le franco,
    # notamment. `delivery_fee` est ce que le client paie ; les deux diffèrent
    # dès qu'on offre la livraison, et confondre les deux ferait travailler le
    # livreur gratuitement, sa commission étant un pourcentage de cette valeur.
    #
    # Nullable pour les commandes antérieures à ce champ : leur commission
    # retombe sur `delivery_fee`, qui était alors la seule valeur connue.
    delivery_fee_gross = MoneyField(null=True)
    discount = MoneyField()
    total = MoneyField()

    payment_method = models.CharField(max_length=16, choices=PaymentMethod.choices)
    promo_code = models.CharField(max_length=32, blank=True)

    placed_at = models.DateTimeField(auto_now_add=True)
    estimated_delivery_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        verbose_name = "commande"
        ordering = ["-placed_at"]
        constraints = [
            # Générée depuis la machine à états : le schéma ne peut pas
            # accepter un statut que le code ignore, ni l'inverse. C'est ce
            # découplage qui avait produit C4.
            state_check_constraint(ORDER_MACHINE, "status", "order_status_in_enum"),
            models.CheckConstraint(
                condition=models.Q(subtotal_minor__gte=0)
                & models.Q(delivery_fee_minor__gte=0)
                & models.Q(discount_minor__gte=0)
                & models.Q(total_minor__gte=0),
                name="order_amounts_not_negative",
            ),
            models.CheckConstraint(
                # La remise ne peut pas dépasser ce qu'il y a à remiser :
                # au-delà, le total deviendrait négatif et la « commande »
                # rapporterait de l'argent au client.
                condition=models.Q(
                    discount_minor__lte=models.F("subtotal_minor") + models.F("delivery_fee_minor")
                ),
                name="order_discount_within_bounds",
            ),
        ]
        indexes = [
            # Le tableau de bord du personnel : commandes en cours d'un
            # restaurant, les plus récentes d'abord.
            models.Index(fields=["restaurant", "status", "-placed_at"]),
            models.Index(fields=["customer", "-placed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} — {self.get_status_display()}"

    @property
    def is_terminal(self) -> bool:
        return ORDER_MACHINE.is_terminal(self.status)

    @property
    def is_cancellable(self) -> bool:
        return ORDER_MACHINE.can(self.status, OrderStatus.CANCELLED)


class OrderLine(UUIDModel):
    """Ligne de commande — instantané, pas une référence vivante.

    `menu_item` est conservé pour les statistiques, mais **rien** ne s'y appuie
    pour l'affichage ni pour la facturation : `item_name`, `unit_price` et
    `options` sont des copies. Un article renommé, repricé ou retiré du
    catalogue laisse la commande intacte.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT, related_name="order_lines")

    item_name = models.CharField(max_length=120)
    item_image = models.URLField(blank=True)
    unit_price = MoneyField()
    quantity = models.PositiveSmallIntegerField()
    line_total = MoneyField()

    # Options figées : [{"group": "Cuisson", "option": "À point", "delta": 0}, …]
    options = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "ligne de commande"
        verbose_name_plural = "lignes de commande"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="order_line_quantity_positive"
            ),
        ]
        indexes = [models.Index(fields=["order"])]

    def __str__(self) -> str:
        return f"{self.quantity} × {self.item_name}"


class OrderStatusEvent(UUIDModel):
    """Journal des transitions.

    Écrit par la machine à états, dans la même transaction que le changement de
    statut. L'historique est donc un sous-produit gratuit plutôt qu'une écriture
    séparée qu'on peut oublier.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="status_events")
    from_status = models.CharField(max_length=16, choices=OrderStatus.choices)
    to_status = models.CharField(max_length=16, choices=OrderStatus.choices)
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Nul si la transition vient du système (webhook, tâche planifiée).",
    )
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "événement de statut"
        verbose_name_plural = "événements de statut"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["order", "created_at"])]

    def __str__(self) -> str:
        return f"{self.from_status} → {self.to_status}"


class IdempotencyKey(UUIDModel):
    """Clé d'idempotence de création — ADR-009.

    Le réseau mobile coupe pendant l'envoi bien plus souvent qu'on ne le croit.
    Sans cette table, un client qui retente crée une seconde commande — et le
    problème n'est découvert qu'à la livraison de deux repas.

    La réponse d'origine est mémorisée : un rejeu doit renvoyer *exactement* la
    même chose, pas seulement éviter le doublon.
    """

    key = models.CharField(max_length=64)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="idempotency_keys")
    endpoint = models.CharField(max_length=120)
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Nul tant que la requête n'a pas produit sa réponse. C'est **ce champ** qui
    # fait autorité sur l'achèvement : la clé est réservée avant toute écriture
    # métier, si bien qu'elle existe un instant sans réponse à rejouer. Une
    # requête concurrente qui la trouve dans cet état doit attendre, pas
    # recevoir un corps vide.
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "clé d'idempotence"
        verbose_name_plural = "clés d'idempotence"
        constraints = [
            # Portée à l'utilisateur : deux clients peuvent tirer la même clé
            # sans se gêner, et personne ne peut lire la réponse d'autrui en
            # devinant sa clé.
            models.UniqueConstraint(
                fields=["user", "endpoint", "key"], name="idempotency_key_unique_per_user"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.endpoint} — {self.key}"
