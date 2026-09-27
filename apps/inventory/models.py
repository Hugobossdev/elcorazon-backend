"""Inventaire — la matière que la cuisine consomme.

## Ce que ce module ajoute, et pourquoi il ne pouvait pas manquer

Jusqu'ici, le stock du produit était deux colonnes sur `MenuItem` :
`tracks_stock` et `stock_quantity`. La technique était juste — le décrément est
évalué par la base, sans lecture préalable — mais l'**unité** ne l'était pas :
ces colonnes comptent des *plats finis*.

Or les plats d'une cuisine partagent leurs ingrédients. Le même pain sert trois
burgers, la même sauce six plats. Décompter « 12 burgers restants » suppose que
ces douze existent déjà, faits et emballés, ce qui décrit un **magasin** et non
une cuisine. Trois conséquences, toutes constatées :

* la rupture d'un ingrédient ne retirait aucun plat de la carte — le personnel
  devait basculer `is_available` à la main, plat par plat ;
* le **coût matière était inconnu**, donc la marge aussi. Pour une enseigne dont
  le modèle repose sur la maîtrise de ce coût, c'est l'indicateur manquant ;
* aucune perte, aucun ajustement, aucune réception n'était traçable.

## Les trois modèles, et la frontière entre eux

    Ingredient          la référence — ce que l'enseigne sait acheter
        │
    StockItem           ce qu'*une cuisine* en détient, ici et maintenant
        │
    StockMovement       le journal — pourquoi le stock est ce qu'il est

`Ingredient` n'a **pas** de clé vers l'établissement, et c'est délibéré. Le
catalogue, lui, en a une, si bien qu'ouvrir une seconde cuisine oblige à
recopier chaque article (`apps.catalog.duplication`). Après recopie, un réseau
de dix cuisines détient dix « Burger Corazón » sans lien entre eux, et changer
le prix de l'enseigne demande dix écritures dont rien ne vérifie la
concordance.

Refaire cela pour la matière serait répéter un défaut connu au moment précis où
l'on a le choix. Une tomate est une tomate à Lomé comme à Abidjan : c'est son
**stock** et son **coût** qui varient, et ce sont eux qui portent la clé de
l'établissement.

Le coût vit d'ailleurs sur `StockItem` et non sur `Ingredient` pour une raison
qui n'est pas qu'esthétique : la devise est héritée du pays
(`Restaurant.currency`), et un coût d'enseigne en XOF serait faux pour une
cuisine ghanéenne — le sérialiseur du catalogue refuse déjà un prix dont la
devise n'est pas celle de l'établissement.
"""

from __future__ import annotations

from typing import Any

from django.db import models

from apps.accounts.models import User
from apps.restaurants.models import Restaurant
from common.fields import MoneyField, QuantityField
from common.models import SoftDeleteModel, TimeStampedModel, UUIDModel
from common.quantities import Dimension

__all__ = [
    "AdjustmentRequest",
    "AdjustmentStatus",
    "Ingredient",
    "MovementKind",
    "StockItem",
    "StockMovement",
]


class Dimensions(models.TextChoices):
    """Les trois grandeurs, côté Django.

    Duplique délibérément `common.quantities.Dimension` : `common` ne doit pas
    imposer Django à ce qui n'est que de l'arithmétique, et Django a besoin d'un
    `TextChoices` pour ses `choices` et pour drf-spectacular. Le test
    `test_les_deux_tables_de_dimensions_concordent` interdit qu'elles divergent.
    """

    MASS = Dimension.MASS, "Masse"
    VOLUME = Dimension.VOLUME, "Volume"
    COUNT = Dimension.COUNT, "Dénombrement"


class Ingredient(UUIDModel, TimeStampedModel, SoftDeleteModel):
    """Une matière première, au niveau de l'enseigne.

    En suppression **logique** : `StockMovement` y renvoie, et un mouvement est
    une écriture de valeur. Retirer une référence du catalogue d'achat ne doit
    pas effacer l'histoire de ce qu'elle a coûté — c'est le critère exact que
    `SoftDeleteModel` documente.
    """

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, unique=True)
    description = models.TextField(blank=True)

    # La dimension est **immuable en pratique** : la changer réinterpréterait
    # d'un coup tous les mouvements passés — « 500 » deviendrait 500 ml là où il
    # valait 500 mg. Les quantités portent donc leur propre dimension, figée à
    # l'écriture (voir `common.fields.QuantityField`), et celle-ci ne sert qu'à
    # guider la saisie et à refuser une unité incohérente à la création.
    dimension = models.CharField(
        max_length=6,
        choices=Dimensions.choices,
        help_text="Ce que l'on mesure : une masse, un volume, ou des unités.",
    )

    # Allergènes portés par l'ingrédient et non par le plat : c'est la seule
    # place où l'information est saisie une fois. Un plat les hérite de sa
    # recette, ce qui rend impossible le cas où l'on ajoute des arachides à une
    # sauce sans penser aux douze plats qui l'emploient.
    allergens = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Codes d'allergènes, hérités par tout plat dont la recette emploie cet ingrédient."
        ),
    )

    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Une référence inactive ne peut plus entrer en stock, "
            "mais son historique reste lisible."
        ),
    )

    class Meta:
        verbose_name = "ingrédient"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(dimension__in=Dimension.ALL),
                name="ingredient_dimension_in_enum",
            ),
        ]
        indexes = [models.Index(fields=["is_active", "name"])]

    def __str__(self) -> str:
        return self.name


class StockItem(UUIDModel, TimeStampedModel):
    """Ce qu'une cuisine détient d'un ingrédient — l'état courant.

    ## Pourquoi une colonne, et non la somme du journal

    Le stock courant *pourrait* se lire comme la somme des mouvements. C'est la
    forme la plus sûre — une seule vérité — et elle devient impraticable au
    cent-millième mouvement, c'est-à-dire après quelques mois de service.

    La colonne est donc maintenue par `F()` dans la même transaction que le
    mouvement qui la fait bouger, exactement comme `MenuItem.stock_quantity`
    l'était. Ce qui rend ce choix défendable n'est pas la performance, c'est le
    **test de réconciliation** : `somme des mouvements == on_hand` est vérifié
    par la suite, et il a été écrit en même temps que cette colonne. Sans lui,
    on aurait deux vérités et aucun moyen de savoir laquelle a tort.

    ## `reserved`, et pourquoi il n'est pas un drapeau sur la commande

    Une commande confirmée immobilise de la matière avant de la consommer. Porté
    par la ligne de commande, cet engagement serait invisible depuis le stock :
    « il reste 3 kg » sans dire que 2,8 sont déjà promis. Ici, le disponible est
    `on_hand − reserved`, calculable, et une réservation orpheline se voit dans
    le journal.
    """

    restaurant = models.ForeignKey(Restaurant, on_delete=models.PROTECT, related_name="stock_items")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="stock_items")

    on_hand = QuantityField()
    reserved = QuantityField()

    # Seuil d'alerte, facultatif : toutes les références n'en méritent pas un.
    # Nul, l'ingrédient n'apparaît jamais dans les alertes de stock bas — ce qui
    # est un choix, pas un oubli, et se relit comme tel.
    low_stock_threshold = QuantityField(null=True)

    # Coût d'une **unité de coût** — le kilogramme, le litre, l'unité (voir
    # `common.quantities.COST_UNIT`) —, dans la devise de l'établissement. Pas
    # le coût du gramme : en entiers de francs CFA, un oignon à 500 F le kilo
    # coûterait 0,5 F le gramme, arrondi à 0 ou à 1. Nul tant qu'aucune entrée
    # facturée n'a eu lieu : inventer un coût à zéro ferait afficher une marge
    # de 100 %.
    unit_cost = MoneyField(null=True)

    class Meta:
        verbose_name = "ligne de stock"
        verbose_name_plural = "lignes de stock"
        ordering = ["ingredient__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "ingredient"],
                name="one_stock_item_per_restaurant_and_ingredient",
            ),
            # Le stock négatif est interdit — phase 6 du cahier des charges.
            #
            # La contrainte, et non une vérification de service : une correction
            # en `shell`, une commande de peuplement ou `django-admin` passent à
            # côté du service, et un stock négatif est le genre de valeur qu'on
            # découvre en cherchant *autre chose*, trois mois plus tard.
            #
            # Si une cuisine doit un jour pouvoir passer en négatif — une
            # consommation saisie après coup —, ce sera un **type de mouvement**
            # qui l'autorise explicitement, jamais cette contrainte retirée.
            models.CheckConstraint(
                condition=models.Q(on_hand_base__gte=0),
                name="stock_item_on_hand_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(reserved_base__gte=0),
                name="stock_item_reserved_not_negative",
            ),
            # On ne promet pas ce qu'on n'a pas.
            models.CheckConstraint(
                condition=models.Q(reserved_base__lte=models.F("on_hand_base")),
                name="stock_item_reserved_within_on_hand",
            ),
            # Les trois quantités d'une ligne mesurent le même ingrédient : leurs
            # dimensions ne peuvent pas différer. Sans cette contrainte, un
            # `on_hand` en grammes et un seuil en millilitres se compareraient
            # par leur entier de base, et l'alerte de stock bas se déclencherait
            # sur un nombre qui ne veut rien dire.
            models.CheckConstraint(
                condition=models.Q(on_hand_dimension=models.F("reserved_dimension")),
                name="stock_item_dimensions_agree",
            ),
            models.CheckConstraint(
                condition=models.Q(low_stock_threshold_base__isnull=True)
                | models.Q(low_stock_threshold_dimension=models.F("on_hand_dimension")),
                name="stock_item_threshold_dimension_agrees",
            ),
        ]
        indexes = [
            # Le tableau de stock d'une cuisine, et l'alerte de stock bas.
            models.Index(fields=["restaurant", "ingredient"]),
            models.Index(fields=["restaurant", "on_hand_base"]),
        ]

    def __str__(self) -> str:
        return f"{self.ingredient.name} — {self.restaurant.name} : {self.on_hand}"

    @property
    def available(self) -> Any:
        """Ce qui est réellement disponible : le détenu moins le promis."""
        return self.on_hand - self.reserved

    @property
    def is_low(self) -> bool:
        """Sous le seuil d'alerte ? Faux si aucun seuil n'est défini."""
        seuil = self.low_stock_threshold
        return seuil is not None and self.available <= seuil


class MovementKind(models.TextChoices):
    """Pourquoi le stock a bougé.

    ## Un mouvement, un effet

    Chaque type agit sur **une seule** colonne — `on_hand` ou `reserved`, jamais
    les deux. Consommer de la matière réservée s'écrit donc en deux lignes :
    `RELEASE` puis `CONSUMPTION`.

    C'est plus verbeux, et c'est ce qui rend le journal réconciliable : la somme
    des mouvements d'une colonne doit égaler cette colonne, et un mouvement à
    double effet obligerait à savoir, pour chaque type, laquelle de ses deux
    faces compter.

    ## Le signe est porté par le type, pas par la saisie

    `WASTE` est toujours une sortie, `RECEIPT` toujours une entrée. Laisser
    l'appelant choisir le signe permettrait une perte positive — qui créerait du
    stock en déclarant une casse. Le service impose le signe ; le type le
    déclare ici, une fois.
    """

    # --- entrées de matière ------------------------------------------------
    PURCHASE = "purchase", "Achat"
    RECEIPT = "receipt", "Réception"
    # --- sorties de matière ------------------------------------------------
    CONSUMPTION = "consumption", "Consommation"
    WASTE = "waste", "Perte"
    # --- corrections et transferts ----------------------------------------
    ADJUSTMENT = "adjustment", "Ajustement"
    TRANSFER = "transfer", "Transfert"
    # --- engagements (colonne `reserved`) ---------------------------------
    RESERVATION = "reservation", "Réservation"
    RELEASE = "release", "Libération"


#: Types qui agissent sur `reserved` ; tous les autres agissent sur `on_hand`.
RESERVATION_KINDS: frozenset[str] = frozenset({MovementKind.RESERVATION, MovementKind.RELEASE})

#: Sens imposé par le type. `None` : les deux sens sont légitimes.
#:
#: `ADJUSTMENT` et `TRANSFER` sont les seuls à pouvoir aller dans les deux sens
#: — un inventaire physique corrige à la hausse comme à la baisse, et un
#: transfert est une sortie ici et une entrée là-bas.
REQUIRED_SIGN: dict[str, int | None] = {
    MovementKind.PURCHASE: +1,
    MovementKind.RECEIPT: +1,
    MovementKind.CONSUMPTION: -1,
    MovementKind.WASTE: -1,
    MovementKind.ADJUSTMENT: None,
    MovementKind.TRANSFER: None,
    MovementKind.RESERVATION: +1,
    MovementKind.RELEASE: -1,
}


class JournalIsAppendOnly(RuntimeError):
    """Tentative de modifier ou d'effacer un mouvement déjà écrit."""


class StockMovement(UUIDModel):
    """Le journal des mouvements — **en ajout seul**.

    ## Pourquoi rien ne s'y modifie ni ne s'y efface

    Un mouvement de stock est une écriture de valeur : une perte saisie est un
    actif qui disparaît du bilan sans transaction. Un journal qu'on peut
    retoucher ne prouve rien — il dit seulement ce que la dernière personne a
    voulu qu'il dise.

    Une erreur se **contre-passe** : on écrit le mouvement inverse, avec son
    motif. Les deux lignes restent, et l'on sait qu'il y a eu correction. C'est
    la seule forme qui permette de répondre à « pourquoi le stock est-il ce
    qu'il est ? » plusieurs mois après.

    `save()` et `delete()` refusent donc toute réécriture. Ce n'est pas une
    règle de revue : c'est une exception levée.

    ## Ce que chaque ligne fige

    Le coût unitaire est **recopié** sur le mouvement, pas lu par la clé
    étrangère. C'est ce qui rend le coût matière calculable a posteriori : le
    prix du kilo de bœuf change, et une consommation de mars doit rester
    valorisée au prix de mars. Même raison que l'instantané d'`OrderLine`.
    """

    stock_item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="movements")

    kind = models.CharField(max_length=12, choices=MovementKind.choices)

    # Signée : positive pour une entrée, négative pour une sortie. Le sens est
    # imposé par le type (voir `REQUIRED_SIGN`), pas laissé à l'appelant.
    quantity = QuantityField()

    # Coût unitaire au moment du mouvement, figé. Nul pour les mouvements qui
    # n'engagent pas de valeur (réservation, libération) ou quand le coût de la
    # ligne n'est pas encore connu.
    unit_cost = MoneyField(null=True)

    # Qui, et pourquoi. `SET_NULL` : le mouvement survit au départ de la
    # personne — supprimer un compte ne doit pas emporter l'historique
    # comptable, et `CASCADE` ferait exactement cela.
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements"
    )
    reason = models.TextField(
        blank=True,
        help_text="Obligatoire pour un ajustement ou une perte — voir le service.",
    )

    # Rattachement libre à ce qui a causé le mouvement : une référence de
    # commande pour une consommation, un numéro de bon pour une réception.
    # Texte et non clé étrangère : `inventory` ne connaît pas `orders`, et lui
    # faire connaître les commandes inverserait le graphe de dépendances
    # (ADR-002) pour un champ qui n'est lu que par un humain.
    reference = models.CharField(max_length=64, blank=True, db_index=True)

    # Clé d'idempotence de l'écriture qui a produit ce mouvement — l'en-tête
    # `Idempotency-Key` d'une réception ou d'une déclaration de perte.
    #
    # Une réception rejouée par un réseau qui coupe créditerait deux fois la
    # chambre froide, et rien dans le journal ne distinguerait la seconde ligne
    # d'une vraie livraison. L'unicité est tenue **par la base** : deux requêtes
    # simultanées portant la même clé ne peuvent pas écrire chacune leur ligne.
    #
    # Vide pour les mouvements que le système écrit lui-même — réservation,
    # consommation d'une commande —, qui ont déjà leur propre garde.
    request_key = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "mouvement de stock"
        verbose_name_plural = "mouvements de stock"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=[k.value for k in MovementKind]),
                name="stock_movement_kind_in_enum",
            ),
            # Un mouvement de zéro n'est pas un mouvement : il n'explique rien et
            # gonfle le journal. Le refuser ici évite qu'une boucle de reprise en
            # écrive un par ligne de commande sans matière.
            models.CheckConstraint(
                condition=~models.Q(quantity_base=0),
                name="stock_movement_quantity_not_zero",
            ),
            models.UniqueConstraint(
                fields=["request_key"],
                condition=~models.Q(request_key=""),
                name="stock_movement_request_key_unique",
            ),
        ]
        indexes = [
            # Le journal d'une ligne de stock, du plus récent au plus ancien —
            # et la réconciliation, qui le parcourt en entier.
            models.Index(fields=["stock_item", "-created_at"]),
            models.Index(fields=["kind", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.quantity} — {self.created_at:%d/%m/%Y %H:%M}"

    @property
    def affects_reservation(self) -> bool:
        """Ce mouvement agit-il sur `reserved` plutôt que sur `on_hand` ?"""
        return self.kind in RESERVATION_KINDS

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Autorise la création, refuse la réécriture.

        `self._state.adding` est vrai à la première écriture seulement. Un
        `save()` ultérieur — même sur un seul champ, même pour corriger une
        faute de frappe dans le motif — lève : le journal se corrige par
        contre-passation, jamais par retouche.
        """
        if not self._state.adding:
            raise JournalIsAppendOnly(
                "Un mouvement de stock ne se modifie pas. Pour corriger une erreur, "
                "écrire le mouvement inverse avec son motif : les deux lignes "
                "restent, et l'on sait qu'il y a eu correction."
            )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise JournalIsAppendOnly(
            "Un mouvement de stock ne s'efface pas. Pour annuler son effet, "
            "écrire le mouvement inverse avec son motif."
        )


class AdjustmentStatus(models.TextChoices):
    PENDING = "pending", "En attente de validation"
    APPROVED = "approved", "Validée"
    REJECTED = "rejected", "Refusée"


#: Les deux types de mouvement qu'une demande peut porter : ceux qui font
#: disparaître ou apparaître de la valeur sans transaction.
ADJUSTMENT_KINDS: frozenset[str] = frozenset({MovementKind.ADJUSTMENT, MovementKind.WASTE})


class AdjustmentRequest(UUIDModel, TimeStampedModel):
    """Une perte ou une correction qui attend une seconde validation.

    ## Pourquoi une table, et non un mouvement « en attente »

    Le journal (`StockMovement`) ne contient que des **faits** : ce qui a bougé,
    et le stock qui en résulte. Y écrire une ligne provisoire obligerait chaque
    lecture — la réconciliation, le coût matière, le stock courant — à savoir
    l'écarter, et la première qui l'oublierait compterait une perte que personne
    n'a acceptée. La demande vit donc à côté, et ne touche **à rien** tant
    qu'elle n'est pas validée.

    ## Le quatre-yeux, tenu par la base

    Celui qui valide n'est pas celui qui a déclaré. Une règle de service seule
    ne suffirait pas : une correction en `shell` ou un nouveau point d'entrée la
    contourneraient, et c'est précisément le genre d'écriture qu'un contrôle de
    valeur doit empêcher. La contrainte `adjustment_request_four_eyes` la rend
    irreprésentable.

    ## Ce que la demande fige

    La quantité **signée**, telle qu'elle sera écrite — une perte est négative —,
    et la valeur estimée au coût du moment de la déclaration. Celui qui valide
    lit ce qu'on lui a demandé d'accepter, pas un recalcul fait au coût du jour
    de sa validation.
    """

    stock_item = models.ForeignKey(
        StockItem, on_delete=models.PROTECT, related_name="adjustment_requests"
    )
    kind = models.CharField(max_length=12, choices=MovementKind.choices)
    quantity = QuantityField()
    reason = models.TextField()

    # Nulle quand le coût de la ligne est inconnu — et c'est alors, justement,
    # l'une des raisons pour lesquelles la demande attend : on ne peut pas
    # prouver qu'une valeur inconnue est sous le plafond.
    estimated_value = MoneyField(null=True)

    status = models.CharField(
        max_length=8, choices=AdjustmentStatus.choices, default=AdjustmentStatus.PENDING
    )

    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="stock_adjustments_requested"
    )
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_adjustments_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    # Le mouvement écrit à la validation. `PROTECT` : le journal ne s'efface pas,
    # et la demande qui l'a produit non plus.
    movement = models.OneToOneField(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustment_request",
    )

    # Même rôle que sur le mouvement : une déclaration rejouée ne crée pas une
    # seconde demande.
    request_key = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "demande de correction de stock"
        verbose_name_plural = "demandes de correction de stock"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=sorted(ADJUSTMENT_KINDS)),
                name="adjustment_request_kind_is_adjustment_or_waste",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=[s.value for s in AdjustmentStatus]),
                name="adjustment_request_status_in_enum",
            ),
            models.CheckConstraint(
                condition=~models.Q(quantity_base=0),
                name="adjustment_request_quantity_not_zero",
            ),
            models.CheckConstraint(
                condition=~models.Q(reason=""),
                name="adjustment_request_reason_required",
            ),
            # Celui qui décide n'est pas celui qui a demandé.
            models.CheckConstraint(
                condition=models.Q(decided_by__isnull=True)
                | ~models.Q(decided_by=models.F("requested_by")),
                name="adjustment_request_four_eyes",
            ),
            # L'état et ses traces vont ensemble : une demande en attente n'a ni
            # décision ni mouvement ; une validée a les deux ; une refusée a une
            # décision et aucun mouvement.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status=AdjustmentStatus.PENDING,
                        decided_at__isnull=True,
                        movement__isnull=True,
                    )
                    | models.Q(
                        status=AdjustmentStatus.APPROVED,
                        decided_at__isnull=False,
                        movement__isnull=False,
                    )
                    | models.Q(
                        status=AdjustmentStatus.REJECTED,
                        decided_at__isnull=False,
                        movement__isnull=True,
                    )
                ),
                name="adjustment_request_status_matches_traces",
            ),
            models.UniqueConstraint(
                fields=["request_key"],
                condition=~models.Q(request_key=""),
                name="adjustment_request_key_unique",
            ),
        ]
        indexes = [
            # La file de validation d'une cuisine.
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["stock_item", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.quantity} — {self.get_status_display()}"
