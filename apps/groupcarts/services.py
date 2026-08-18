"""Règles du panier collaboratif.

Le module répond à quatre questions, et ce sont les quatre que l'ancienne
implémentation laissait au client :

* **qui peut déposer une ligne ?** un participant inscrit, dans un panier encore
  ouvert et dont l'échéance n'est pas passée ;
* **qui peut la modifier ?** celui qui l'a déposée, et l'hôte — personne d'autre.
  Auparavant, tous les participants écrivaient dans les mêmes lignes de commande ;
* **quand la commande naît-elle ?** à la confirmation de l'hôte, jamais avant.
  Auparavant elle existait dès l'ouverture du panier partagé, ce qui plaçait en
  base des commandes que personne n'avait validées ni payées ;
* **combien ça coûte ?** ce que `price_selection` dit, comme pour le panier
  personnel — le calcul n'est pas dupliqué ici (C1, C2).

La diffusion temps réel part **après le commit** : annoncer « Kossi a ajouté un
poulet braisé » sur une transaction qui échoue ensuite afficherait chez tous les
participants une ligne qui n'existe pas, et rien ne viendrait la retirer.
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Sequence

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.carts.services import PricedSelection, price_selection, validate_selection
from apps.catalog.models import MenuItem, Option
from apps.groupcarts.models import GroupCart, GroupCartLine, GroupCartLineOption, GroupCartMember
from apps.groupcarts.states import EXPIRABLE, GROUP_CART_MACHINE, GroupCartStatus
from apps.orders.models import Order
from apps.orders.services import OrderService
from apps.profiles.models import Address
from apps.restaurants.models import Restaurant
from common.exceptions import BusinessRuleViolation
from common.money import CurrencyMismatch, Money
from common.realtime import group_cart_group, publish

__all__ = ["GroupCartService", "generate_code"]

#: Alphabet du code d'invitation.
#:
#: Ni `O`/`0` ni `I`/`1`/`L` : le code est lu à voix haute ou recopié depuis une
#: capture d'écran, et l'ambiguïté typographique se paie en « le code ne marche
#: pas » plutôt qu'en faute de frappe visible.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

#: Nombre de tirages avant d'abandonner sur collision de code.
#:
#: Avec 31⁶ ≈ 887 millions de combinaisons et quelques centaines de paniers
#: vivants, une collision est déjà improbable ; cinq tirages la rendent
#: négligeable sans jamais boucler indéfiniment si la table se remplit.
CODE_ATTEMPTS = 5


def generate_code() -> str:
    """Code d'invitation tiré au sort cryptographiquement.

    `secrets` et non `random` : un code prévisible laisserait un tiers rejoindre
    le déjeuner d'un groupe qu'il ne connaît pas, et y ajouter des plats que
    l'hôte paierait.
    """
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class GroupCartService:
    # -------------------------------------------------------------- ouverture

    @staticmethod
    @transaction.atomic
    def open(
        *,
        host: User,
        restaurant: Restaurant,
        title: str = "",
        window_minutes: int | None = None,
    ) -> GroupCart:
        """Ouvre un panier collaboratif et y inscrit l'hôte.

        L'hôte devient membre dans la même transaction : un panier dont l'hôte
        n'est pas membre lui interdirait d'y déposer ses propres plats, et c'est
        le genre d'incohérence qu'on ne découvre qu'en production, au premier
        hôte qui essaie de commander pour lui-même.
        """
        minutes = window_minutes or settings.GROUP_CART_DEFAULT_WINDOW_MINUTES
        if not 1 <= minutes <= settings.GROUP_CART_MAX_WINDOW_MINUTES:
            raise BusinessRuleViolation(
                "L'échéance doit tenir entre une minute et "
                f"{settings.GROUP_CART_MAX_WINDOW_MINUTES} minutes.",
                max_window_minutes=settings.GROUP_CART_MAX_WINDOW_MINUTES,
            )

        group_cart = GroupCartService._create_with_code(
            host=host,
            restaurant=restaurant,
            title=title.strip(),
            closes_at=timezone.now() + dt.timedelta(minutes=minutes),
        )
        GroupCartMember.objects.create(group_cart=group_cart, user=host)
        return group_cart

    @staticmethod
    def _create_with_code(
        *, host: User, restaurant: Restaurant, title: str, closes_at: dt.datetime
    ) -> GroupCart:
        """Crée le panier en retirant un code libre.

        La collision est traitée par `IntegrityError` et non par un `exists()`
        préalable : entre la vérification et l'insertion, une autre requête peut
        prendre le même code. C'est la contrainte d'unicité qui tranche, comme
        partout ailleurs dans ce projet — on ne demande pas la permission, on
        tente et on rattrape.
        """
        for _ in range(CODE_ATTEMPTS):
            try:
                # Point de sauvegarde : sans lui, l'`IntegrityError` avorterait
                # la transaction entière et le tirage suivant échouerait sur une
                # transaction cassée plutôt que sur le code.
                with transaction.atomic():
                    return GroupCart.objects.create(
                        host=host,
                        restaurant=restaurant,
                        title=title,
                        code=generate_code(),
                        closes_at=closes_at,
                    )
            except IntegrityError:
                continue

        raise BusinessRuleViolation(  # pragma: no cover - 31⁶ combinaisons
            "Impossible d'attribuer un code d'invitation ; réessayez."
        )

    # --------------------------------------------------------------- adhésion

    @staticmethod
    def by_code(code: str) -> GroupCart:
        """Panier désigné par son code d'invitation.

        La casse est ignorée : le code est recopié à la main, et refuser
        `ab3k9p` quand la base contient `AB3K9P` serait un refus sans cause
        réelle.
        """
        try:
            return GroupCart.objects.select_related("restaurant").get(code=code.strip().upper())
        except GroupCart.DoesNotExist:
            raise BusinessRuleViolation(
                "Aucun panier de groupe ne porte ce code.", invitation_code=code.strip().upper()
            ) from None

    @staticmethod
    @transaction.atomic
    def join(*, group_cart: GroupCart, user: User) -> GroupCartMember:
        """Inscrit un participant.

        Rejoindre deux fois n'est pas une erreur : le lien d'invitation est
        partagé dans une conversation de groupe, et il sera tapoté plusieurs fois
        par la même personne. Le second appel rend l'adhésion existante.
        """
        locked = GroupCartService._locked(group_cart)
        GroupCartService._assert_contributable(locked)

        member, created = GroupCartMember.objects.get_or_create(group_cart=locked, user=user)
        if created:
            GroupCartService._announce(
                locked,
                "groupcart.member_joined",
                {"member": str(user.pk), "member_name": user.full_name},
            )
        return member

    @staticmethod
    def members_of(group_cart: GroupCart) -> QuerySet[GroupCartMember]:
        return group_cart.members.select_related("user")

    @staticmethod
    def is_member(*, group_cart: GroupCart, user: User) -> bool:
        return group_cart.members.filter(user=user).exists()

    # ---------------------------------------------------------- contributions

    @staticmethod
    @transaction.atomic
    def add_line(
        *,
        group_cart: GroupCart,
        member: User,
        menu_item: MenuItem,
        quantity: int,
        options: Sequence[Option],
        notes: str = "",
    ) -> GroupCartLine:
        """Dépose une ligne au nom d'un participant.

        La fusion des lignes identiques est **par membre** : deux participants qui
        commandent le même plat gardent deux lignes, sans quoi l'écran afficherait
        « 2 × poulet braisé » sous le nom d'un seul et l'autre ne retrouverait pas
        sa commande. À l'intérieur d'un même membre, en revanche, deux ajouts
        identiques se cumulent — sinon le panier se remplit de doublons à chaque
        tapotement.
        """
        locked = GroupCartService._locked(group_cart)
        GroupCartService._assert_contributable(locked)
        GroupCartService._assert_member(locked, member)
        GroupCartService._assert_orderable_here(locked, menu_item)
        validate_selection(menu_item, options)

        existing = GroupCartService._identical_line(locked, member, menu_item, options, notes)
        if existing is not None:
            existing.quantity += quantity
            existing.save(update_fields=["quantity", "updated_at"])
            line = existing
        else:
            line = GroupCartLine.objects.create(
                group_cart=locked,
                member=member,
                menu_item=menu_item,
                quantity=quantity,
                notes=notes,
            )
            GroupCartLineOption.objects.bulk_create(
                GroupCartLineOption(line=line, option=option) for option in options
            )

        GroupCartService._announce(
            locked,
            "groupcart.line_added",
            {
                "line": str(line.pk),
                "member": str(member.pk),
                "member_name": member.full_name,
                "item_name": menu_item.name,
                "quantity": line.quantity,
            },
        )
        return line

    @staticmethod
    @transaction.atomic
    def set_quantity(*, line: GroupCartLine, actor: User, quantity: int) -> GroupCartLine:
        group_cart = GroupCartService._locked(line.group_cart)
        GroupCartService._assert_contributable(group_cart)
        GroupCartService._assert_may_touch(group_cart, line, actor)

        line.quantity = quantity
        line.save(update_fields=["quantity", "updated_at"])

        GroupCartService._announce(
            group_cart,
            "groupcart.line_updated",
            {"line": str(line.pk), "member": str(line.member_id), "quantity": quantity},
        )
        return line

    @staticmethod
    @transaction.atomic
    def remove_line(*, line: GroupCartLine, actor: User) -> None:
        group_cart = GroupCartService._locked(line.group_cart)
        GroupCartService._assert_contributable(group_cart)
        GroupCartService._assert_may_touch(group_cart, line, actor)

        identifier, member_id = str(line.pk), str(line.member_id)
        line.delete()

        GroupCartService._announce(
            group_cart,
            "groupcart.line_removed",
            {"line": identifier, "member": member_id},
        )

    # ------------------------------------------------------------ valorisation

    @staticmethod
    def load(group_cart: GroupCart) -> GroupCart:
        """Recharge un panier avec tout ce que la valorisation demande.

        Sans ces préchargements, un panier de groupe de vingt lignes — ce qui est
        la taille normale d'un déjeuner d'équipe — déclenche une requête par
        ligne pour l'article, une par ligne pour ses options, et une par option
        pour son groupe.
        """
        return (
            GroupCart.objects.select_related("restaurant__zone__city__country", "host")
            .prefetch_related(
                Prefetch(
                    "lines",
                    queryset=GroupCartLine.objects.select_related(
                        "menu_item", "member"
                    ).prefetch_related(
                        Prefetch(
                            "options",
                            queryset=GroupCartLineOption.objects.select_related("option__group"),
                        )
                    ),
                ),
                Prefetch("members", queryset=GroupCartMember.objects.select_related("user")),
            )
            .get(pk=group_cart.pk)
        )

    @staticmethod
    def price(group_cart: GroupCart) -> PricedSelection:
        """Valorise le panier entier — même code que le panier personnel."""
        return price_selection(group_cart.lines.all(), group_cart.restaurant.currency)

    @staticmethod
    def price_per_member(group_cart: GroupCart) -> dict[str, Money]:
        """Ce que chaque participant a mis dans le panier.

        Purement informatif : le règlement du groupe reste l'affaire de l'hôte,
        qui paie la commande, et du partage de frais s'il y a lieu. Mais un écran
        qui ne dit pas « tu en es à 4 500 F » laisse chacun ajouter à l'aveugle,
        et c'est l'hôte qui découvre le total.
        """
        currency = group_cart.restaurant.currency
        lines = list(group_cart.lines.all())
        totals: dict[str, Money] = {}

        # Les deux séquences sont appariées plutôt que relues depuis
        # `PricedLine.line` : le protocole `PriceableLine` n'expose pas
        # `member_id`, qui n'a aucun sens pour un panier personnel, et le lui
        # ajouter aurait fait fuiter le panier collaboratif dans le contrat
        # commun.
        for line, priced in zip(lines, price_selection(lines, currency).lines, strict=True):
            member_id = str(line.member_id)
            totals[member_id] = totals.get(member_id, Money.zero(currency)) + priced.total
        return totals

    # ------------------------------------------------------------ transitions

    @staticmethod
    @transaction.atomic
    def lock(*, group_cart: GroupCart, actor: User) -> GroupCart:
        """Clôt les ajouts sans commander.

        Le geste existe pour lui-même : l'hôte annonce que c'est fini, tous les
        participants le voient, et le total cesse de bouger pendant qu'il choisit
        l'adresse et le moyen de paiement.
        """
        locked = GroupCartService._locked(group_cart)
        GroupCartService._assert_host(locked, actor)
        return GroupCartService._transition(locked, GroupCartStatus.LOCKED)

    @staticmethod
    @transaction.atomic
    def confirm(
        *,
        group_cart: GroupCart,
        actor: User,
        address: Address,
        payment_method: str,
        instructions: str = "",
        promo_code: str = "",
    ) -> Order:
        """Transforme le panier collaboratif en **une** commande, au nom de l'hôte.

        Le panier est verrouillé en base pour toute la transaction, et la clôture
        est faite ici si elle ne l'a pas été : sans cela, une ligne déposée entre
        la valorisation et la création de la commande serait facturée sans avoir
        été relue, ou perdue après avoir été affichée. C'est la même course que
        celle qui, sur les commandes, a rendu le verrou obligatoire.

        La commande est celle de l'hôte : c'est lui qui l'a confirmée, lui qui la
        paie, lui qui la suit, et c'est son adresse qui est livrée. Les autres
        participants la voient par le partage de commande (S3), pas en la
        possédant.
        """
        locked = GroupCartService._locked(group_cart)
        GroupCartService._assert_host(locked, actor)

        if locked.status == GroupCartStatus.OPEN:
            locked = GroupCartService._transition(locked, GroupCartStatus.LOCKED, announce=False)
        # Passé ce point, plus aucune contribution n'est acceptée : la
        # valorisation ci-dessous porte donc exactement sur ce que l'hôte a lu.
        GROUP_CART_MACHINE.validate(locked.status, GroupCartStatus.CONFIRMED)

        order = OrderService.create_from_selection(
            user=actor,
            restaurant=locked.restaurant,
            selection=GroupCartService.price(GroupCartService.load(locked)),
            address=address,
            payment_method=payment_method,
            instructions=instructions,
            promo_code=promo_code,
        )

        locked.order = order
        locked.status = GroupCartStatus.CONFIRMED
        locked.closed_at = timezone.now()
        locked.save(update_fields=["order", "status", "closed_at", "updated_at"])

        GroupCartService._announce(
            locked,
            "groupcart.confirmed",
            {"status": locked.status, "order": str(order.pk), "reference": order.reference},
        )
        return order

    @staticmethod
    @transaction.atomic
    def cancel(*, group_cart: GroupCart, actor: User, reason: str = "") -> GroupCart:
        locked = GroupCartService._locked(group_cart)
        GroupCartService._assert_host(locked, actor)
        return GroupCartService._transition(locked, GroupCartStatus.CANCELLED, reason=reason)

    @staticmethod
    def expire_due(*, now: dt.datetime | None = None) -> int:
        """Referme les paniers dont l'échéance est passée.

        Ligne par ligne et non par un `update()` de masse : chaque fermeture doit
        être diffusée à ses participants, qui attendent devant un écran ouvert. Le
        volume le permet — ce sont les paniers échus depuis le dernier tour, pas
        l'historique.
        """
        horizon = now or timezone.now()
        echus = GroupCart.objects.filter(status__in=EXPIRABLE, closes_at__lte=horizon)

        fermes = 0
        for group_cart in echus.iterator():
            with transaction.atomic():
                locked = GroupCartService._locked(group_cart)
                if locked.status not in EXPIRABLE:
                    # L'hôte a confirmé entre la lecture et le verrou : sa
                    # commande existe, et l'expirer maintenant afficherait
                    # « échéance dépassée » sur un repas déjà en cuisine.
                    continue
                GroupCartService._transition(locked, GroupCartStatus.EXPIRED)
                fermes += 1
        return fermes

    @staticmethod
    def _transition(
        group_cart: GroupCart, target: str, *, reason: str = "", announce: bool = True
    ) -> GroupCart:
        """Change l'état — **le seul** chemin d'écriture du statut.

        Un rejeu vers l'état courant ne fait rien et ne lève pas : deux
        participants dont l'application retente la clôture ne doivent pas voir
        d'erreur pour une action déjà accomplie (P1 transposé).
        """
        if GROUP_CART_MACHINE.is_noop(group_cart.status, target):
            return group_cart

        GROUP_CART_MACHINE.validate(group_cart.status, target)

        group_cart.status = target
        group_cart.closed_at = timezone.now()
        group_cart.cancellation_reason = reason
        group_cart.save(update_fields=["status", "closed_at", "cancellation_reason", "updated_at"])

        if announce:
            GroupCartService._announce(
                group_cart, f"groupcart.{target}", {"status": target, "reason": reason}
            )
        return group_cart

    # ------------------------------------------------------------- vérifications

    @staticmethod
    def _locked(group_cart: GroupCart) -> GroupCart:
        """Relit le panier sous verrou.

        Toutes les écritures passent par ici. Deux participants qui ajoutent en
        même temps, ou un ajout concurrent d'une confirmation, sont sérialisés :
        c'est ce qui garantit qu'une ligne est soit dans la commande, soit
        refusée, et jamais « ajoutée après le total ».
        """
        return (
            GroupCart.objects.select_for_update().select_related("restaurant").get(pk=group_cart.pk)
        )

    @staticmethod
    def _assert_contributable(group_cart: GroupCart) -> None:
        if group_cart.status != GroupCartStatus.OPEN:
            raise BusinessRuleViolation(
                "Ce panier de groupe n'accepte plus de modification.",
                current_status=group_cart.status,
            )
        if group_cart.closes_at <= timezone.now():
            # L'échéance est opposée **ici**, sans attendre la tâche planifiée :
            # entre deux tours de la tâche, un panier échu continuerait sinon
            # d'accepter des plats que l'hôte croyait ne plus pouvoir arriver.
            raise BusinessRuleViolation(
                "L'échéance de ce panier de groupe est dépassée.",
                current_status=group_cart.status,
                closes_at=group_cart.closes_at.isoformat(),
            )

    @staticmethod
    def _assert_member(group_cart: GroupCart, user: User) -> None:
        if not GroupCartService.is_member(group_cart=group_cart, user=user):
            raise BusinessRuleViolation("Rejoignez ce panier de groupe avant d'y ajouter un plat.")

    @staticmethod
    def _assert_host(group_cart: GroupCart, user: User) -> None:
        if group_cart.host_id != user.pk:
            raise BusinessRuleViolation(
                "Seul l'hôte du panier de groupe peut faire cela.",
                host=str(group_cart.host_id),
            )

    @staticmethod
    def _assert_may_touch(group_cart: GroupCart, line: GroupCartLine, actor: User) -> None:
        """Une ligne se modifie par son auteur, ou par l'hôte.

        L'hôte est inclus parce qu'il paie et qu'il assume le total : il doit
        pouvoir retirer le plat d'un participant parti, ou une quantité entrée par
        erreur. Aucun autre participant ne peut toucher à la ligne d'un tiers —
        c'était précisément le trou de l'ancienne implémentation, où tout le monde
        écrivait dans les mêmes lignes.
        """
        if line.member_id != actor.pk and group_cart.host_id != actor.pk:
            raise BusinessRuleViolation(
                "Cette ligne appartient à un autre participant.",
                member=str(line.member_id),
            )

    @staticmethod
    def _assert_orderable_here(group_cart: GroupCart, menu_item: MenuItem) -> None:
        """Mêmes contrôles que sur le panier personnel.

        Répétés et non factorisés avec `CartService` : ils portent sur des types
        différents, et les rendre génériques aurait demandé un protocole pour
        gagner cinq lignes.
        """
        if menu_item.restaurant_id != group_cart.restaurant_id:
            raise BusinessRuleViolation(
                "Cet article appartient à un autre restaurant.",
                restaurant_id=str(group_cart.restaurant_id),
            )
        if menu_item.is_deleted or not menu_item.is_available:
            raise BusinessRuleViolation(f"« {menu_item.name} » n'est pas disponible.")

        try:
            menu_item.price + Money.zero(group_cart.restaurant.currency)
        except CurrencyMismatch as exc:
            raise BusinessRuleViolation(str(exc)) from exc

    @staticmethod
    def _identical_line(
        group_cart: GroupCart,
        member: User,
        menu_item: MenuItem,
        options: Sequence[Option],
        notes: str,
    ) -> GroupCartLine | None:
        wanted = {option.pk for option in options}
        candidates = group_cart.lines.filter(
            member=member, menu_item=menu_item, notes=notes
        ).prefetch_related("options")
        for line in candidates:
            if {selection.option_id for selection in line.options.all()} == wanted:
                return line
        return None

    # --------------------------------------------------------------- diffusion

    @staticmethod
    def _announce(group_cart: GroupCart, event: str, payload: dict[str, object]) -> None:
        """Diffuse un changement aux participants, **après le commit**.

        Publier pendant la transaction afficherait chez tout le monde une ligne
        qui disparaîtrait ensuite si la transaction échouait — et rien ne viendrait
        corriger l'écran.
        """
        body = {"group_cart": str(group_cart.pk), **payload}
        transaction.on_commit(lambda: publish(group_cart_group(group_cart.pk), event, body))
