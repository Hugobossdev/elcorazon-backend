"""Écritures de stock — le seul chemin autorisé.

## Pourquoi tout passe par ici

Deux règles ne tiennent que si un unique endroit les applique :

1. **Un mouvement et la colonne qu'il déplace s'écrivent ensemble.** Séparés,
   ils divergent — et la divergence ne se voit qu'à la réconciliation, des
   semaines plus tard. Chaque fonction de ce module est donc `@transaction.atomic`
   et écrit les deux.

2. **Le sens est imposé par le type de mouvement.** Une perte est une sortie ;
   laisser l'appelant passer une quantité positive permettrait de *créer* du
   stock en déclarant une casse.

## La concurrence

Deux commandes confirmées simultanément peuvent réserver le même dernier
kilogramme. La protection est la même que celle qui tenait déjà sur
`MenuItem.stock_quantity` : un `UPDATE … WHERE disponible >= n` **évalué par la
base**, sans lecture préalable. Si aucune ligne n'est touchée, c'est qu'une
autre transaction est passée avant — et `InsufficientStock` est levée.

Lire puis écrire aurait laissé la fenêtre ouverte entre les deux, ce qui est
précisément le défaut que `test_deux_commandes_concurrentes_n_emportent_pas_la_meme_unite`
verrouille pour le catalogue, et que
`test_deux_reservations_concurrentes_n_emportent_pas_le_meme_stock` verrouille
ici.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.models import User
from apps.inventory.models import (
    REQUIRED_SIGN,
    RESERVATION_KINDS,
    AdjustmentRequest,
    AdjustmentStatus,
    Ingredient,
    MovementKind,
    StockItem,
    StockMovement,
)
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation, InsufficientStock
from common.money import Money
from common.quantities import DimensionMismatch, Quantity, value_minor

# `InventoryService` et non `StockService` : `apps.catalog.services` porte déjà
# ce dernier nom pour le compteur de plats finis. Deux services homonymes dans
# un même dépôt finissent par être importés l'un pour l'autre, et celui-ci
# écrit un journal comptable.
__all__ = [
    "DecisionAlreadyTaken",
    "Declaration",
    "FourEyesRequired",
    "IdempotencyKeyReused",
    "InactiveIngredient",
    "InventoryService",
]


class MotiveRequired(BusinessRuleViolation):
    """Ajustement ou perte sans motif.

    Ce sont les deux mouvements qui font disparaître de la valeur sans
    contrepartie. Un journal qui dit « −4 kg » sans dire pourquoi ne permet ni
    l'audit ni la correction : il déplace le problème sans l'éclairer.
    """

    code = "motive_required"
    title = "Motif obligatoire"


class WrongDirection(BusinessRuleViolation):
    """Quantité dont le signe contredit le type de mouvement."""

    code = "wrong_movement_direction"
    title = "Sens du mouvement incohérent"


class InactiveIngredient(BusinessRuleViolation):
    """Entrée de matière sur une référence retirée."""

    code = "inactive_ingredient"
    title = "Ingrédient retiré"


class IdempotencyKeyReused(BusinessRuleViolation):
    """Une clé d'idempotence déjà employée pour **une autre** écriture.

    Distincte d'un rejeu : un rejeu porte la même clé et la même demande, et
    rend l'écriture d'origine. Ici la clé est la même et la demande diffère —
    une erreur d'intégration, que l'accepter transformerait en écriture
    silencieusement ignorée.
    """

    code = "idempotency_key_reused"
    title = "Clé d'idempotence déjà employée"


class FourEyesRequired(BusinessRuleViolation):
    """Validation demandée par la personne même qui a déclaré."""

    code = "four_eyes_required"
    title = "Seconde personne requise"


class DecisionAlreadyTaken(BusinessRuleViolation):
    """Décision contraire sur une demande déjà tranchée."""

    code = "adjustment_already_decided"
    title = "Demande déjà tranchée"


@dataclass(frozen=True, slots=True)
class Declaration:
    """Ce qu'une déclaration de perte ou de correction a produit.

    Exactement l'un des deux : le mouvement, si la valeur passait sous le
    plafond ; la demande, si elle attend une seconde validation. L'appelant ne
    choisit pas — c'est le plafond de la cuisine qui décide.
    """

    movement: StockMovement | None = None
    request: AdjustmentRequest | None = None

    @property
    def is_pending(self) -> bool:
        return self.request is not None and self.request.status == AdjustmentStatus.PENDING


class InventoryService:
    """Toutes les écritures de stock, et rien d'autre."""

    # ------------------------------------------------------------ ouverture

    @staticmethod
    @transaction.atomic
    def open_item(
        *,
        restaurant: Restaurant,
        ingredient: Ingredient,
        low_stock_threshold: Quantity | None = None,
    ) -> StockItem:
        """Ouvre la ligne de stock d'un ingrédient dans une cuisine.

        Idempotente : rappelée, elle rend la ligne existante sans l'écraser. La
        rendre destructrice ferait perdre un stock réel au premier double clic
        du back-office.

        Les quantités partent à zéro, dans la dimension de l'ingrédient — jamais
        « inconnue » : une ligne sans dimension ne pourrait recevoir aucun
        mouvement, et le premier arrivé la fixerait au hasard de sa saisie.
        """
        if not ingredient.is_active or ingredient.is_deleted:
            raise InactiveIngredient(
                f"« {ingredient.name} » est retiré du référentiel : "
                "on n'ouvre pas de stock pour une matière qu'on n'achète plus.",
                ingredient=ingredient.slug,
            )

        zero = Quantity.zero(ingredient.dimension)

        if (
            low_stock_threshold is not None
            and low_stock_threshold.dimension != ingredient.dimension
        ):
            raise DimensionMismatch(ingredient.dimension, low_stock_threshold.dimension)

        item, cree = StockItem.objects.get_or_create(
            restaurant=restaurant,
            ingredient=ingredient,
            defaults={
                "on_hand": zero,
                "reserved": zero,
                "low_stock_threshold": low_stock_threshold,
            },
        )
        if not cree and low_stock_threshold is not None:
            item.low_stock_threshold = low_stock_threshold
            item.save(
                update_fields=[
                    "low_stock_threshold_base",
                    "low_stock_threshold_dimension",
                    "updated_at",
                ]
            )
        return item

    # ------------------------------------------------------------ entrées

    @staticmethod
    def receive(
        *,
        item: StockItem,
        quantity: Quantity,
        unit_cost: Money | None = None,
        kind: str = MovementKind.RECEIPT,
        actor: User | None = None,
        reason: str = "",
        reference: str = "",
        request_key: str = "",
    ) -> StockMovement:
        """Fait entrer de la matière.

        `unit_cost` est le coût d'une **unité de coût** — le kilogramme, le
        litre, l'unité (`common.quantities.COST_UNIT`) — et non celui du lot.
        C'est ce qui permet de valoriser une consommation de 20 g sans rouvrir
        la facture d'achat, par `value_minor`. Il n'est pas tenu au gramme : en
        entiers d'une devise sans décimales, l'arrondi y mangerait la marge.

        Il met à jour le coût courant de la ligne. Le calcul retenu est le
        **coût moyen pondéré**, et non le dernier prix connu : sur une matière
        dont le cours bouge, le dernier prix fait sauter la marge affichée à
        chaque livraison, alors que la cuisine consomme encore l'ancien lot.

        ## Rejouée avec la même clé, elle rend la première

        `request_key` porte l'en-tête `Idempotency-Key` de la route. La ligne de
        stock est verrouillée **avant** de chercher la clé : deux réceptions
        simultanées de la même livraison s'attendent, et la seconde trouve la
        première au lieu de créditer deux fois la chambre froide.
        """
        if kind not in {MovementKind.PURCHASE, MovementKind.RECEIPT, MovementKind.TRANSFER}:
            raise WrongDirection(f"{kind} n'est pas une entrée de matière.")

        with transaction.atomic():
            verrouille = (
                StockItem.objects.select_for_update()
                .select_related("ingredient", "restaurant__zone__city__country")
                .get(pk=item.pk)
            )

            if request_key:
                deja = InventoryService._rejeu_mouvement(
                    request_key, stock_item=verrouille, kind=kind, quantity=quantity
                )
                if deja is not None:
                    return deja

            ingredient = verrouille.ingredient
            if not ingredient.is_active or ingredient.is_deleted:
                raise InactiveIngredient(
                    f"« {ingredient.name} » est retiré du référentiel : "
                    "une livraison ne peut plus y entrer.",
                    ingredient=ingredient.slug,
                )

            if unit_cost is not None and unit_cost.currency != verrouille.restaurant.currency:
                raise BusinessRuleViolation(
                    f"Cette cuisine achète en {verrouille.restaurant.currency} ; "
                    f"coût reçu en {unit_cost.currency}.",
                    expected_currency=verrouille.restaurant.currency,
                )

            return InventoryService._apply(
                item=verrouille,
                kind=kind,
                quantity=quantity,
                unit_cost=unit_cost,
                actor=actor,
                reason=reason,
                reference=reference,
                request_key=request_key,
            )

    # ------------------------------------------------------------ sorties

    @staticmethod
    def consume(
        *,
        item: StockItem,
        quantity: Quantity,
        actor: User | None = None,
        reference: str = "",
        from_reservation: bool = False,
    ) -> StockMovement:
        """Sort de la matière parce qu'elle a été cuisinée.

        `quantity` est passée **positive** : l'appelant dit ce qu'il consomme,
        le service met le signe. Lui faire porter le `-` déplacerait la règle
        vers les dizaines de points d'appel, dont un finirait par l'oublier.

        `from_reservation` libère d'abord l'engagement correspondant, pour que
        la matière consommée cesse d'être comptée comme promise. Deux lignes au
        journal, un effet chacune — voir `MovementKind`.
        """
        sortie = -abs_quantity(quantity)

        if from_reservation:
            with transaction.atomic():
                InventoryService._apply(
                    item=item,
                    kind=MovementKind.RELEASE,
                    quantity=sortie,
                    actor=actor,
                    reference=reference,
                )
                return InventoryService._apply(
                    item=item,
                    kind=MovementKind.CONSUMPTION,
                    quantity=sortie,
                    actor=actor,
                    reference=reference,
                )

        return InventoryService._apply(
            item=item,
            kind=MovementKind.CONSUMPTION,
            quantity=sortie,
            actor=actor,
            reference=reference,
        )

    @staticmethod
    def waste(
        *,
        item: StockItem,
        quantity: Quantity,
        reason: str,
        actor: User | None = None,
    ) -> StockMovement:
        """Déclare une perte. Le motif est obligatoire."""
        if not reason.strip():
            raise MotiveRequired(
                "Une perte doit dire pourquoi : « −4 kg » sans motif ne permet "
                "ni l'audit ni la correction."
            )
        return InventoryService._apply(
            item=item,
            kind=MovementKind.WASTE,
            quantity=-abs_quantity(quantity),
            actor=actor,
            reason=reason,
        )

    @staticmethod
    def adjust(
        *,
        item: StockItem,
        delta: Quantity,
        reason: str,
        actor: User | None = None,
    ) -> StockMovement:
        """Corrige le stock après un inventaire physique.

        `delta` est **signé** : c'est le seul mouvement où l'appelant décide du
        sens, parce qu'un comptage corrige dans les deux directions et que le
        service n'a aucun moyen de deviner lequel.
        """
        if not reason.strip():
            raise MotiveRequired("Un ajustement doit dire ce qui l'a motivé.")
        return InventoryService._apply(
            item=item,
            kind=MovementKind.ADJUSTMENT,
            quantity=delta,
            actor=actor,
            reason=reason,
        )

    # ------------------------------------------------------------ déclarations

    @staticmethod
    def declare_waste(
        *,
        item: StockItem,
        quantity: Quantity,
        reason: str,
        actor: User | None,
        request_key: str = "",
    ) -> Declaration:
        """Déclare une perte — écrite tout de suite, ou soumise à validation.

        **Le seul chemin par lequel une route déclare une perte.** `waste`, en
        dessous, écrit sans regarder le plafond : c'est ce qu'appelle la
        validation, une fois la seconde personne passée.
        """
        return InventoryService._declare(
            item=item,
            kind=MovementKind.WASTE,
            quantity=-abs_quantity(quantity),
            reason=reason,
            actor=actor,
            request_key=request_key,
        )

    @staticmethod
    def declare_adjustment(
        *,
        item: StockItem,
        delta: Quantity,
        reason: str,
        actor: User | None,
        request_key: str = "",
    ) -> Declaration:
        """Déclare une correction d'inventaire — `delta` signé.

        Le plafond porte sur la **valeur absolue** : une correction à la hausse
        crée de la valeur comme une perte en retire, et une hausse fictive est la
        façon ordinaire de masquer, le mois suivant, une sortie non déclarée.
        """
        return InventoryService._declare(
            item=item,
            kind=MovementKind.ADJUSTMENT,
            quantity=delta,
            reason=reason,
            actor=actor,
            request_key=request_key,
        )

    @staticmethod
    @transaction.atomic
    def approve(*, request: AdjustmentRequest, actor: User, note: str = "") -> AdjustmentRequest:
        """Valide une demande : le mouvement est écrit, et la demande le désigne.

        Rejouée sur une demande déjà validée, elle la rend telle quelle — un
        double clic du gérant ne doit ni échouer ni écrire deux fois.

        Le mouvement porte le **déclarant** comme acteur : le journal dit qui a
        constaté la perte ; la demande dit qui l'a acceptée. Les deux restent
        lisibles, et aucun ne remplace l'autre.

        Si le stock ne suffit plus — la perte déclarée dépasse ce qui reste,
        consommé entre-temps —, `InsufficientStock` remonte et la demande reste
        en attente : c'est à la personne qui valide de la refuser, en le disant.
        """
        verrouillee = (
            AdjustmentRequest.objects.select_for_update()
            .select_related("stock_item")
            .get(pk=request.pk)
        )

        if verrouillee.status == AdjustmentStatus.APPROVED:
            return verrouillee
        if verrouillee.status == AdjustmentStatus.REJECTED:
            raise DecisionAlreadyTaken("Cette demande a déjà été refusée.")
        if verrouillee.requested_by_id == actor.pk:
            raise FourEyesRequired(
                "Une perte ou une correction se valide par une autre personne que "
                "celle qui l'a déclarée."
            )

        if verrouillee.kind == MovementKind.WASTE:
            mouvement = InventoryService.waste(
                item=verrouillee.stock_item,
                quantity=verrouillee.quantity,
                reason=verrouillee.reason,
                actor=verrouillee.requested_by,
            )
        else:
            mouvement = InventoryService.adjust(
                item=verrouillee.stock_item,
                delta=verrouillee.quantity,
                reason=verrouillee.reason,
                actor=verrouillee.requested_by,
            )

        verrouillee.status = AdjustmentStatus.APPROVED
        verrouillee.decided_by = actor
        verrouillee.decided_at = timezone.now()
        verrouillee.decision_note = note
        verrouillee.movement = mouvement
        verrouillee.save(
            update_fields=[
                "status",
                "decided_by",
                "decided_at",
                "decision_note",
                "movement",
                "updated_at",
            ]
        )
        return verrouillee

    @staticmethod
    @transaction.atomic
    def reject(*, request: AdjustmentRequest, actor: User, note: str) -> AdjustmentRequest:
        """Refuse une demande. Le motif est obligatoire, et rien ne bouge au stock."""
        if not note.strip():
            raise MotiveRequired(
                "Un refus doit dire pourquoi : la personne qui a déclaré la perte "
                "doit savoir quoi recompter."
            )

        verrouillee = AdjustmentRequest.objects.select_for_update().get(pk=request.pk)

        if verrouillee.status == AdjustmentStatus.REJECTED:
            return verrouillee
        if verrouillee.status == AdjustmentStatus.APPROVED:
            raise DecisionAlreadyTaken(
                "Cette demande a déjà été validée : sa correction est au journal. "
                "Pour l'annuler, déclarer le mouvement inverse."
            )
        if verrouillee.requested_by_id == actor.pk:
            raise FourEyesRequired(
                "Une demande se tranche par une autre personne que celle qui l'a déclarée."
            )

        verrouillee.status = AdjustmentStatus.REJECTED
        verrouillee.decided_by = actor
        verrouillee.decided_at = timezone.now()
        verrouillee.decision_note = note
        verrouillee.save(
            update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"]
        )
        return verrouillee

    @staticmethod
    @transaction.atomic
    def _declare(
        *,
        item: StockItem,
        kind: str,
        quantity: Quantity,
        reason: str,
        actor: User | None,
        request_key: str,
    ) -> Declaration:
        """Le plafond décide : écrire maintenant, ou attendre une seconde personne.

        ## Ce qui passe seul

        Une valeur **connue** et **au plus égale** au plafond de la cuisine. Tout
        le reste attend :

        * pas de plafond fixé — personne n'a encore décidé ce qui peut passer
          seul, donc rien ne passe seul ;
        * coût de la ligne inconnu — on ne peut pas prouver qu'une valeur
          inconnue est sous le plafond ;
        * valeur au-delà.

        ## Ce qui est refusé tout de suite

        Une perte plus grande que ce que la ligne détient. La laisser en attente
        ferait valider plus tard une demande que le stock refuserait de toute
        façon — la personne qui a compté doit l'apprendre au moment où elle
        compte.
        """
        if not reason.strip():
            raise MotiveRequired(
                "Une perte ou une correction doit dire pourquoi : « −4 kg » sans "
                "motif ne permet ni l'audit ni la correction."
            )
        if quantity.is_zero:
            raise WrongDirection("Une correction de zéro n'explique rien.")

        verrouille = (
            StockItem.objects.select_for_update()
            .select_related("ingredient", "restaurant")
            .get(pk=item.pk)
        )
        if quantity.dimension != verrouille.on_hand.dimension:
            raise DimensionMismatch(verrouille.on_hand.dimension, quantity.dimension)

        if request_key:
            deja = InventoryService._rejeu_declaration(
                request_key, stock_item=verrouille, kind=kind, quantity=quantity
            )
            if deja is not None:
                return deja

        if quantity.is_negative and -quantity.amount_base > verrouille.on_hand.amount_base:
            raise InsufficientStock(
                f"Impossible de retirer {abs_quantity(quantity).humanise()} de "
                f"« {verrouille.ingredient.name} » : il n'en reste que "
                f"{verrouille.on_hand.humanise()}.",
                ingredient=verrouille.ingredient.slug,
                requested=str(abs_quantity(quantity).as_reference),
                available=str(verrouille.on_hand.as_reference),
                unit=quantity.reference_unit,
            )

        cout = verrouille.unit_cost
        valeur = (
            Money(abs(value_minor(quantity, cout.amount_minor)), cout.currency)
            if cout is not None
            else None
        )
        plafond = verrouille.restaurant.stock_adjustment_ceiling

        passe_seule = (
            valeur is not None
            and plafond is not None
            and valeur.currency == plafond.currency
            and valeur.amount_minor <= plafond.amount_minor
        )

        if passe_seule:
            # `_apply` directement, et non `waste` ou `adjust` : le motif et le
            # signe viennent d'être vérifiés ici, et seul `_apply` sait porter la
            # clé d'idempotence jusqu'au journal.
            mouvement = InventoryService._apply(
                item=verrouille,
                kind=kind,
                quantity=quantity,
                actor=actor,
                reason=reason,
                request_key=request_key,
            )
            return Declaration(movement=mouvement)

        # `quantity` et `estimated_value` sont des champs composites : même
        # limite de l'introspection que pour `StockMovement` plus bas.
        demande = AdjustmentRequest.objects.create(  # type: ignore[misc]
            stock_item=verrouille,
            kind=kind,
            quantity=quantity,
            reason=reason,
            estimated_value=valeur,
            requested_by=actor,
            request_key=request_key,
        )
        return Declaration(request=demande)

    @staticmethod
    def _rejeu_mouvement(
        request_key: str, *, stock_item: StockItem, kind: str, quantity: Quantity
    ) -> StockMovement | None:
        """Le mouvement déjà écrit sous cette clé, s'il décrit la même écriture."""
        deja = StockMovement.objects.filter(request_key=request_key).first()
        if deja is None:
            return None
        if deja.stock_item_id == stock_item.pk and deja.kind == kind and deja.quantity == quantity:
            return deja
        raise IdempotencyKeyReused(
            "Cette clé d'idempotence a déjà servi à une autre écriture de stock.",
        )

    @staticmethod
    def _rejeu_declaration(
        request_key: str, *, stock_item: StockItem, kind: str, quantity: Quantity
    ) -> Declaration | None:
        """La déclaration déjà faite sous cette clé — mouvement écrit, ou demande."""
        mouvement = InventoryService._rejeu_mouvement(
            request_key, stock_item=stock_item, kind=kind, quantity=quantity
        )
        if mouvement is not None:
            return Declaration(movement=mouvement)

        demande = AdjustmentRequest.objects.filter(request_key=request_key).first()
        if demande is None:
            return None
        if (
            demande.stock_item_id == stock_item.pk
            and demande.kind == kind
            and demande.quantity == quantity
        ):
            return Declaration(request=demande)
        raise IdempotencyKeyReused(
            "Cette clé d'idempotence a déjà servi à une autre écriture de stock.",
        )

    # ------------------------------------------------------------ engagements

    @staticmethod
    def reserve(
        *,
        item: StockItem,
        quantity: Quantity,
        actor: User | None = None,
        reference: str = "",
    ) -> StockMovement:
        """Immobilise de la matière pour une commande confirmée.

        Échoue si le disponible — `on_hand − reserved` — n'y suffit pas. C'est
        la seule écriture de ce module qui peut être refusée par manque, et
        c'est voulu : réserver est la promesse, et une promesse qu'on ne peut
        pas tenir doit être refusée au moment où on la fait, pas au moment de
        la cuisson.
        """
        return InventoryService._apply(
            item=item,
            kind=MovementKind.RESERVATION,
            quantity=abs_quantity(quantity),
            actor=actor,
            reference=reference,
        )

    @staticmethod
    def release(
        *,
        item: StockItem,
        quantity: Quantity,
        actor: User | None = None,
        reference: str = "",
    ) -> StockMovement:
        """Rend un engagement — commande annulée, ou matière finalement cuisinée."""
        return InventoryService._apply(
            item=item,
            kind=MovementKind.RELEASE,
            quantity=-abs_quantity(quantity),
            actor=actor,
            reference=reference,
        )

    # ------------------------------------------------------------ le cœur

    @staticmethod
    @transaction.atomic
    def _apply(
        *,
        item: StockItem,
        kind: str,
        quantity: Quantity,
        unit_cost: Money | None = None,
        actor: User | None = None,
        reason: str = "",
        reference: str = "",
        request_key: str = "",
    ) -> StockMovement:
        """Écrit le mouvement **et** déplace la colonne, dans la même transaction.

        Le mouvement est écrit en second, après que la mise à jour conditionnelle
        a réussi. L'ordre compte : un journal qui porterait une ligne dont la
        colonne n'a pas bougé serait pire qu'un journal incomplet — il
        affirmerait un stock qui n'existe pas.
        """
        if quantity.is_zero:
            raise WrongDirection("Un mouvement de zéro n'explique rien et gonfle le journal.")
        if quantity.dimension != item.on_hand.dimension:
            raise DimensionMismatch(item.on_hand.dimension, quantity.dimension)

        attendu = REQUIRED_SIGN[kind]
        if attendu is not None and (quantity.amount_base > 0) != (attendu > 0):
            sens = "une entrée" if attendu > 0 else "une sortie"
            raise WrongDirection(f"{kind} est toujours {sens}.")

        colonne = "reserved_base" if kind in RESERVATION_KINDS else "on_hand_base"
        delta = quantity.amount_base

        # Mise à jour **conditionnelle**, évaluée par la base.
        #
        # Les deux gardes ne protègent pas la même chose :
        #   — sur `on_hand`, on ne descend pas sous zéro ;
        #   — sur `reserved`, on ne promet pas plus que ce qu'on détient, ce qui
        #     s'écrit `reserved + delta <= on_hand`.
        #
        # Les contraintes `CHECK` disent la même chose et resteraient le dernier
        # rempart ; elles lèveraient une `IntegrityError` que l'appelant ne
        # saurait pas distinguer d'une panne. Le filtre, lui, rend zéro ligne
        # touchée — un fait exploitable, traduit en `InsufficientStock`.
        requete = StockItem.objects.filter(pk=item.pk)
        if delta < 0:
            requete = requete.filter(**{f"{colonne}__gte": -delta})
        elif kind in RESERVATION_KINDS:
            requete = requete.filter(reserved_base__lte=F("on_hand_base") - delta)

        touchees = requete.update(**{colonne: F(colonne) + delta})

        if touchees == 0:
            item.refresh_from_db()
            manque = item.reserved if kind in RESERVATION_KINDS else item.on_hand
            disponible = item.available if kind in RESERVATION_KINDS else manque
            raise InsufficientStock(
                f"Stock insuffisant pour « {item.ingredient.name} » : "
                f"{quantity.humanise()} demandé, {disponible.humanise()} disponible.",
                ingredient=item.ingredient.slug,
                requested=str(quantity.as_reference),
                available=str(disponible.as_reference),
                unit=quantity.reference_unit,
            )

        if unit_cost is not None:
            InventoryService._reprice(item=item, entree=quantity, unit_cost=unit_cost)

        item.refresh_from_db()

        # `type: ignore[misc]` — même motif que `OrderService.place` et
        # `PaymentService.initiate` : le greffon django-stubs ne reconnaît pas
        # les attributs posés par `contribute_to_class`, et voit `quantity` et
        # `unit_cost` comme inconnus du modèle. C'est une limite de
        # l'introspection, pas un défaut de typage — les deux sont bien des
        # `property` sur la classe construite.
        return StockMovement.objects.create(  # type: ignore[misc]
            stock_item=item,
            kind=kind,
            quantity=quantity,
            unit_cost=unit_cost if unit_cost is not None else item.unit_cost,
            actor=actor,
            reason=reason,
            reference=reference,
            request_key=request_key,
        )

    @staticmethod
    def _reprice(*, item: StockItem, entree: Quantity, unit_cost: Money) -> None:
        """Coût moyen pondéré après une entrée.

        La moyenne est pondérée par les quantités **avant** et **après**
        l'entrée, en unités de base — donc en entiers, sans arrondi intermédiaire.
        Le seul arrondi est celui de `Money`, à l'unité mineure, et il est fait
        une fois.

        Premier approvisionnement — aucun coût connu, ou stock antérieur nul —
        le coût entrant s'applique tel quel : il n'y a rien à moyenner, et
        pondérer par zéro donnerait zéro.
        """
        item.refresh_from_db()
        ancien = item.unit_cost

        apres = item.on_hand.amount_base
        avant = apres - entree.amount_base

        if ancien is None or avant <= 0 or apres <= 0:
            item.unit_cost = unit_cost
        else:
            if ancien.currency != unit_cost.currency:
                raise BusinessRuleViolation(
                    f"Cette ligne de stock est valorisée en {ancien.currency} ; "
                    f"coût reçu en {unit_cost.currency}.",
                    code="currency_mismatch",
                )
            total = (
                Decimal(ancien.amount_minor) * avant
                + Decimal(unit_cost.amount_minor) * entree.amount_base
            )
            moyen = (total / apres).to_integral_value(rounding="ROUND_HALF_UP")
            item.unit_cost = Money(int(moyen), unit_cost.currency)

        item.save(update_fields=["unit_cost_minor", "unit_cost_currency", "updated_at"])


def abs_quantity(quantity: Quantity) -> Quantity:
    """Valeur absolue — pour que l'appelant n'ait pas à porter le signe."""
    return Quantity(abs(quantity.amount_base), quantity.dimension)
