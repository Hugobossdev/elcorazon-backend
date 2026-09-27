"""Reprise : encaisse les espèces des commandes livrées avant que la règle existe.

    python manage.py encaisser_especes_livrees              # rapport, rien n'est écrit
    python manage.py encaisser_especes_livrees --appliquer  # écrit

## Pourquoi

Jusqu'au 2026-09-24, rien n'enregistrait les espèces remises au livreur : une
commande espèces livrée gardait `amount_paid = null`, et sa transaction ouverte
restait `processing`. Depuis, `apps/payments/cash.py` les encaisse au passage
en `delivered`. Cette commande applique **la même règle**, par la même
fonction, aux commandes livrées avant.

## Ce qu'elle corrige en plus de la règle courante : la date

L'encaissement est daté du jour de la **livraison**, pas de la reprise. Les
chiffres d'encaissement filtrent sur `Transaction.created_at` : une espèce
reprise aujourd'hui tomberait sinon, avec toutes les autres, dans la journée
de la reprise. Une transaction née de la reprise prend donc `delivered_at`
comme création et comme encaissement ; une demande ouverte par le client garde
sa date de création et prend `delivered_at` comme encaissement.

## Sûreté

Rien n'est écrit sans `--appliquer`. Chaque commande est traitée dans sa propre
transaction : une erreur n'en annule pas d'autres, et elle est rapportée. Une
seconde exécution ne trouve plus rien — une commande soldée n'a plus de reste
dû.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F, Q, QuerySet

from apps.orders.models import Order, PaymentMethod
from apps.orders.states import OrderStatus
from apps.payments.cash import record_cash_collected
from apps.payments.models import Transaction


def commandes_a_reprendre() -> QuerySet[Order]:
    """Espèces, livrées, et dont l'encaissé reporté ne couvre pas le total.

    Le report (`amount_paid`) sert de filtre ; le calcul qui décide reste celui
    de `record_cash_collected`, qui relit les transactions.
    """
    return (
        Order.objects.filter(payment_method=PaymentMethod.CASH, status=OrderStatus.DELIVERED)
        .filter(Q(amount_paid_minor__isnull=True) | Q(amount_paid_minor__lt=F("total_minor")))
        .order_by("delivered_at")
    )


class Command(BaseCommand):
    help = "Encaisse les espèces des commandes livrées avant la règle d'encaissement à la remise."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--appliquer",
            action="store_true",
            help="Écrit les encaissements. Sans cette option, seul le rapport est produit.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        appliquer: bool = options["appliquer"]
        commandes = list(commandes_a_reprendre())
        self.stdout.write(
            f"{len(commandes)} commande(s) espèces livrée(s) sans encaissement enregistré."
        )

        if not appliquer:
            for order in commandes:
                self.stdout.write(
                    f"  {order.reference}  {order.total}  livrée le {order.delivered_at}"
                )
            if commandes:
                self.stdout.write("Rien n'a été écrit. Relancer avec --appliquer.")
            return

        reprises = 0
        echecs = 0
        for order in commandes:
            try:
                with transaction.atomic():
                    especes = record_cash_collected(order)
                    if especes is None:
                        continue
                    self._dater(especes, order)
                reprises += 1
                self.stdout.write(f"  encaissée  {order.reference}  {especes.amount}")
            except Exception as erreur:  # rapportée, la reprise continue
                echecs += 1
                self.stderr.write(f"  ÉCHEC     {order.reference}  {erreur}")

        self.stdout.write(f"{reprises} encaissement(s) enregistré(s), {echecs} échec(s).")

    @staticmethod
    def _dater(especes: Transaction, order: Order) -> None:
        """Replace l'encaissement au jour de la livraison.

        `update` et non `save` : `created_at` est un `auto_now_add`, que `save`
        ne laisse pas réécrire.
        """
        livree_le = order.delivered_at
        if livree_le is None:
            return
        champs: dict[str, Any] = {"completed_at": livree_le}
        if especes.created_at > livree_le:
            # Née de la reprise : elle n'existait pas avant la livraison.
            champs["created_at"] = livree_le
        Transaction.objects.filter(pk=especes.pk).update(**champs)
