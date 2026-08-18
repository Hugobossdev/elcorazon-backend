"""Panier collaboratif — plusieurs personnes, un seul panier, une seule commande.

C'est la « commande groupée » du client Flutter, ramenée côté serveur. L'ancienne
implémentation la faisait vivre entièrement dans l'application : chaque
participant écrivait dans les tables `orders` et `order_items` par abonnement
temps réel, sans qu'aucune autorité ne dise qui avait le droit d'ajouter quoi, ni
à quel prix. Un participant pouvait modifier la ligne d'un autre, et une commande
existait en base avant que quiconque ait confirmé ou payé.

Trois principes structurent ce qui suit :

* **le panier collaboratif n'est pas une commande.** Il n'y en a aucune tant que
  l'hôte n'a pas confirmé ; jusque-là, rien n'est réservé, rien n'est facturé,
  rien n'apparaît en cuisine ;
* **une ligne appartient à celui qui l'a ajoutée.** C'est ce que l'ancienne
  version ne savait pas dire, et c'est ce qui rend l'écran lisible — « qui a
  commandé quoi » — autant que ce qui borne les droits d'écriture ;
* **aucun montant n'est stocké**, comme pour le panier personnel (C1) : le total
  est relu du catalogue à chaque lecture, et une seule fois à la confirmation.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import MenuItem, Option
from apps.groupcarts.states import GROUP_CART_MACHINE, GroupCartStatus
from apps.orders.models import Order
from apps.restaurants.models import Restaurant
from common.models import TimeStampedModel, UUIDModel, state_check_constraint

__all__ = ["GroupCart", "GroupCartLine", "GroupCartLineOption", "GroupCartMember"]


class GroupCart(UUIDModel, TimeStampedModel):
    """Un panier partagé, pour un établissement, ouvert par un hôte.

    L'**hôte** est celui qui ouvre, clôt, confirme et paie. Le partage des frais
    entre participants existe déjà par ailleurs (`payments/split.py`) et n'a pas
    à être refait ici : ce modèle répond à « collecter les choix de tout le
    monde », pas à « qui règle combien ».

    Le rattachement à un restaurant est posé à l'ouverture et ne bouge plus : une
    commande ne peut pas mélanger deux établissements, et laisser l'hôte en
    changer invaliderait silencieusement toutes les lignes déjà déposées.
    """

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="group_carts")
    host = models.ForeignKey(User, on_delete=models.CASCADE, related_name="hosted_group_carts")
    title = models.CharField(
        max_length=120,
        blank=True,
        help_text="Libellé libre — « Déjeuner de l'équipe ». Purement d'affichage.",
    )

    #: Code d'invitation, court et lisible à voix haute.
    #:
    #: Un code plutôt qu'un lien signé : on rejoint une commande groupée en
    #: recevant six caractères par messagerie, souvent recopiés à la main. Il
    #: n'est pas un secret durable et n'a pas à l'être — il n'ouvre que l'accès
    #: à un panier éphémère, dont l'échéance est de quelques heures, et il cesse
    #: de fonctionner dès la clôture.
    code = models.CharField(max_length=12, unique=True, db_index=True)

    status = models.CharField(
        max_length=16,
        choices=GroupCartStatus.choices,
        default=GroupCartStatus.OPEN,
        db_index=True,
    )

    #: Échéance après laquelle le panier n'accepte plus de contribution.
    #:
    #: Obligatoire, et c'est délibéré : un panier collaboratif sans échéance
    #: reste ouvert indéfiniment, et le groupe attend un hôte qui a oublié. La
    #: valeur par défaut est posée par le service, pas ici — elle relève d'un
    #: réglage, pas du schéma.
    closes_at = models.DateTimeField(db_index=True)

    #: Commande issue de la confirmation. Nulle jusque-là, et c'est tout le
    #: propos : rien n'existe en cuisine avant que l'hôte ait confirmé.
    order = models.OneToOneField(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="group_cart",
    )

    closed_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        verbose_name = "panier collaboratif"
        verbose_name_plural = "paniers collaboratifs"
        ordering = ["-created_at"]
        constraints = [
            state_check_constraint(GROUP_CART_MACHINE, "status", "group_cart_status_in_enum"),
        ]
        indexes = [
            # La requête de la tâche d'échéance : les paniers encore vivants
            # dont l'heure est passée.
            models.Index(fields=["status", "closes_at"]),
            models.Index(fields=["host", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.title or 'Panier de groupe'} — {self.restaurant.name} ({self.code})"

    @property
    def accepts_contributions(self) -> bool:
        """Peut-on encore y déposer une ligne ?

        Exposé en propriété pour qu'aucun appelant ne recompose la condition à sa
        façon en oubliant l'un des deux termes. L'échéance compte autant que le
        statut : un panier resté `open` après son heure n'accepte plus rien, même
        si la tâche d'échéance n'est pas encore passée le marquer.
        """
        return self.status == GroupCartStatus.OPEN and self.closes_at > timezone.now()


class GroupCartMember(UUIDModel):
    """Participation d'une personne à un panier collaboratif.

    L'hôte en est un membre comme les autres — inscrit à l'ouverture. Le traiter
    à part obligerait chaque lecture à réunir « l'hôte plus les membres », et la
    moindre omission ferait disparaître ses propres lignes de l'écran.
    """

    group_cart = models.ForeignKey(GroupCart, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="group_cart_memberships")
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "participant"
        ordering = ["joined_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["group_cart", "user"], name="one_membership_per_group_cart_and_user"
            )
        ]

    def __str__(self) -> str:
        return self.user.full_name or self.user.email


class GroupCartLine(UUIDModel, TimeStampedModel):
    """Ligne déposée par un participant.

    `member` porte **qui** a ajouté la ligne, et ce n'est pas une donnée
    d'affichage : c'est la clé du droit d'écriture. Un participant modifie et
    retire ses lignes, jamais celles d'un autre — ce que l'ancienne version, qui
    donnait à tous un accès en écriture aux mêmes lignes de commande, ne pouvait
    pas garantir.

    Aucun prix ni libellé ici, pour la même raison que dans `carts` : les avoir
    en colonne permettrait de les recevoir du client.
    """

    group_cart = models.ForeignKey(GroupCart, on_delete=models.CASCADE, related_name="lines")
    member = models.ForeignKey(User, on_delete=models.CASCADE, related_name="group_cart_lines")
    menu_item = models.ForeignKey(
        MenuItem, on_delete=models.CASCADE, related_name="group_cart_lines"
    )
    quantity = models.PositiveSmallIntegerField(default=1)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "ligne de panier collaboratif"
        verbose_name_plural = "lignes de panier collaboratif"
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="group_cart_line_quantity_positive"
            ),
            # Pas d'unicité sur (group_cart, member, menu_item) : deux fois le
            # même plat avec des cuissons différentes sont deux lignes. La fusion
            # des lignes réellement identiques relève du service, qui compare
            # aussi les options — et elle est **par membre**, sans quoi l'ajout
            # d'un participant renforcerait la ligne d'un autre.
        ]
        indexes = [models.Index(fields=["group_cart", "member"])]

    def __str__(self) -> str:
        return f"{self.quantity} × {self.menu_item.name} ({self.member.full_name})"

    def selected_options(self) -> list[Option]:
        """Options retenues, dans l'ordre d'affichage de leur groupe.

        Même méthode que sur `CartLine`, et c'est ce qui satisfait le protocole
        `PriceableLine` : les deux paniers sont valorisés par `price_selection`,
        donc par le même code.
        """
        return sorted(
            (selection.option for selection in self.options.all()),
            key=lambda option: (option.group.sort_order, option.group_id, option.sort_order),
        )


class GroupCartLineOption(UUIDModel):
    """Option retenue sur une ligne de panier collaboratif.

    Table de liaison et non champ JSON, pour la raison qui vaut déjà dans
    `carts` : les options sont revalidées à la confirmation — existent-elles
    encore, sont-elles disponibles, respectent-elles les bornes de leur groupe ?
    """

    line = models.ForeignKey(GroupCartLine, on_delete=models.CASCADE, related_name="options")
    option = models.ForeignKey(
        Option, on_delete=models.CASCADE, related_name="group_cart_selections"
    )

    class Meta:
        verbose_name = "option de ligne de panier collaboratif"
        verbose_name_plural = "options de ligne de panier collaboratif"
        constraints = [
            models.UniqueConstraint(
                fields=["line", "option"], name="one_selection_per_group_line_and_option"
            )
        ]

    def __str__(self) -> str:
        return self.option.name
