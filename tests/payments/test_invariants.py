"""Invariants du paiement — P1, P2.

P2 est le correctif de la faille la plus grave de l'implémentation précédente :
tout participant d'un paiement partagé pouvait se déclarer payé, ce qui
basculait la commande entière en `completed` — commande gratuite, reproduit
empiriquement à l'époque.

Le correctif d'alors avait restreint l'action aux administrateurs, sans
construire le vrai flux. Ici, la structure l'impose : une part encaissée porte
obligatoirement une transaction, et la contrainte est en base.
"""

from __future__ import annotations

import os
import uuid

import pytest
from django.db import IntegrityError, transaction

from apps.orders.models import Order
from apps.payments.gateway import SandboxGateway
from apps.payments.models import (
    PAYMENT_MACHINE,
    PaymentProvider,
    PaymentStatus,
    SplitPayment,
    SplitShare,
    Transaction,
    WebhookEvent,
)
from common.exceptions import BusinessRuleViolation
from common.money import Money
from tests.fixtures import XOF

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


@pytest.fixture
def split(order: Order, customer) -> SplitPayment:
    return SplitPayment.objects.create(
        order=order, initiated_by=customer, total_amount=Money(4_000, XOF)
    )


@pytest.fixture
def settled_transaction(order: Order) -> Transaction:
    return Transaction.objects.create(
        order=order,
        provider=PaymentProvider.PAYDUNYA,
        provider_reference="PD-VERIFIEE-001",
        amount=Money(1_334, XOF),
        status=PaymentStatus.COMPLETED,
    )


class TestPartsDePaiement:
    """P2 — une part encaissée est adossée à une transaction vérifiée."""

    def test_une_part_ne_peut_pas_s_auto_declarer_payee(self, split: SplitPayment) -> None:
        """Le cœur de la faille : sans transaction, `completed` est refusé par
        la base — pas par une politique qu'on peut contourner."""
        share = SplitShare.objects.create(
            split=split, display_name="Kossi", amount=Money(1_333, XOF)
        )

        with (
            pytest.raises(IntegrityError, match="settled_share_requires_transaction"),
            transaction.atomic(),
        ):
            SplitShare.objects.filter(pk=share.pk).update(status=PaymentStatus.COMPLETED)

    def test_une_part_adossee_a_une_transaction_est_acceptee(
        self, split: SplitPayment, settled_transaction: Transaction
    ) -> None:
        share = SplitShare.objects.create(
            split=split,
            display_name="Ama",
            amount=Money(1_334, XOF),
            transaction=settled_transaction,
        )
        SplitShare.objects.filter(pk=share.pk).update(status=PaymentStatus.COMPLETED)

        share.refresh_from_db()
        assert share.status == PaymentStatus.COMPLETED
        assert share.transaction.is_settled

    def test_les_etats_non_encaisses_n_exigent_rien(self, split: SplitPayment) -> None:
        """La contrainte ne gêne pas le parcours normal : une part en attente
        ou en cours n'a évidemment pas encore de transaction."""
        share = SplitShare.objects.create(split=split, display_name="Yao", amount=Money(1_333, XOF))
        for status in (PaymentStatus.PROCESSING, PaymentStatus.FAILED, PaymentStatus.CANCELLED):
            SplitShare.objects.filter(pk=share.pk).update(status=status)

    def test_une_transaction_ne_sert_qu_une_part(
        self, split: SplitPayment, settled_transaction: Transaction
    ) -> None:
        """Sans cette unicité, un même encaissement solderait plusieurs parts —
        la commande serait réputée payée pour une fraction de son montant."""
        SplitShare.objects.create(
            split=split,
            display_name="Ama",
            amount=Money(1_334, XOF),
            transaction=settled_transaction,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            SplitShare.objects.create(
                split=split,
                display_name="Kodjo",
                amount=Money(1_333, XOF),
                transaction=settled_transaction,
            )

    def test_un_montant_nul_est_refuse_par_le_modele(self, split: SplitPayment) -> None:
        """Première ligne de défense : un refus métier lisible et rattrapable.

        Avant, seule la contrainte `CHECK` arrêtait le montant nul, et elle le
        faisait par un `IntegrityError` — donc un 500 — qui laissait en prime la
        transaction courante inutilisable.
        """
        with pytest.raises(BusinessRuleViolation, match="strictement positif"):
            SplitShare.objects.create(split=split, display_name="Fantôme", amount=Money(0, XOF))

        assert not SplitShare.objects.filter(display_name="Fantôme").exists()

    def test_un_montant_nul_est_refuse_par_la_base(self, split: SplitPayment) -> None:
        """Dernière ligne de défense (ADR-010), vérifiée en contournant `save()`.

        `bulk_create` n'appelle pas `save()` : c'est précisément le genre de
        chemin qui échappe au garde-fou applicatif, et la raison pour laquelle
        la contrainte doit rester en base plutôt que d'y être remplacée.
        """
        with pytest.raises(IntegrityError, match="share_amount_positive"), transaction.atomic():
            SplitShare.objects.bulk_create(
                [SplitShare(split=split, display_name="Fantôme", amount=Money(0, XOF))]
            )


class TestRepartitionSansPerte:
    def test_la_somme_des_parts_egale_le_total(self, split: SplitPayment) -> None:
        """`Money.allocate` garantit qu'aucun franc ne disparaît — une division
        naïve en perdrait jusqu'à deux par commande."""
        parts = split.total_amount.allocate([1, 1, 1])

        for index, part in enumerate(parts):
            SplitShare.objects.create(split=split, display_name=f"P{index}", amount=part)

        total = sum(s.amount.amount_minor for s in split.shares.all())
        assert total == split.total_amount.amount_minor == 4_000


class TestIdempotenceDesWebhooks:
    """P1 — un rejeu ne peut pas rétrograder un encaissement."""

    def test_le_meme_evenement_ne_passe_pas_deux_fois(self) -> None:
        payload = {
            "provider": PaymentProvider.PAYDUNYA,
            "event_id": "evt_abc123",
            "payload": {"status": "completed"},
        }
        WebhookEvent.objects.create(**payload)

        with pytest.raises(IntegrityError, match="webhook_event_unique"), transaction.atomic():
            WebhookEvent.objects.create(**payload)

    def test_deux_prestataires_peuvent_partager_un_identifiant(self) -> None:
        WebhookEvent.objects.create(provider=PaymentProvider.PAYDUNYA, event_id="evt_1", payload={})
        WebhookEvent.objects.create(provider=PaymentProvider.WALLET, event_id="evt_1", payload={})

    def test_un_encaissement_ne_redescend_jamais(self) -> None:
        """La machine à états l'interdit : depuis `completed`, seule la
        transition vers `refunded` existe."""
        for cible in (PaymentStatus.PENDING, PaymentStatus.PROCESSING, PaymentStatus.FAILED):
            assert not PAYMENT_MACHINE.can(PaymentStatus.COMPLETED, cible)

        assert PAYMENT_MACHINE.can(PaymentStatus.COMPLETED, PaymentStatus.REFUNDED)


class TestReferencePrestataire:
    """La référence de bac à sable doit distinguer deux transactions ouvertes
    dans la même milliseconde."""

    def test_la_reference_derive_de_la_cle_primaire_entiere(self, order: Order) -> None:
        """Elle était tronquée à 16 caractères hexadécimaux, soit 64 bits.

        Un UUIDv7 (ADR-007) n'est pas aléatoire sur toute sa longueur : ses 48
        premiers bits sont l'horodatage en millisecondes et les 4 suivants le
        numéro de version, constant. Sur 64 bits tronqués il ne restait donc que
        **12 bits** — 4 096 valeurs — pour départager deux transactions de la
        même milliseconde, d'où les doublons sur
        `payments_transaction_provider_reference_key`.
        """
        txn = Transaction(
            order=order,
            provider=PaymentProvider.CASH,
            provider_reference="",
            amount=Money(4_000, XOF),
        )

        reference = SandboxGateway().open_checkout(txn).provider_reference

        assert reference == f"SBX-{txn.pk.hex.upper()}"
        assert len(reference) == len("SBX-") + 32

    def test_mille_transactions_de_la_meme_milliseconde_ne_collisionnent_pas(
        self, order: Order
    ) -> None:
        """Le paradoxe des anniversaires sur 12 bits donnait une collision plus
        probable qu'improbable dès la soixante-quinzième transaction — un import
        de commandes ou une rafale de parts de paiement partagé y suffisent."""
        gateway = SandboxGateway()
        horodatage = 1_760_000_000_000

        def uuid7_fige() -> uuid.UUID:
            """UUIDv7 dont l'horodatage est figé : simule la même milliseconde."""
            valeur = (horodatage & 0xFFFF_FFFF_FFFF) << 80
            valeur |= 0x7 << 76
            aleatoire = int.from_bytes(os.urandom(10), "big")
            valeur |= ((aleatoire >> 62) & 0xFFF) << 64
            valeur |= 0b10 << 62
            valeur |= aleatoire & 0x3FFF_FFFF_FFFF_FFFF
            return uuid.UUID(int=valeur)

        references = {
            gateway.open_checkout(
                Transaction(
                    id=uuid7_fige(),
                    order=order,
                    provider=PaymentProvider.CASH,
                    provider_reference="",
                    amount=Money(4_000, XOF),
                )
            ).provider_reference
            for _ in range(1_000)
        }

        assert len(references) == 1_000

    def test_la_reference_tient_dans_la_colonne(self, order: Order) -> None:
        """`provider_reference` est un `CharField(max_length=128)` : la référence
        complète doit y entrer sans troncature silencieuse."""
        txn = Transaction.objects.create(
            order=order,
            provider=PaymentProvider.CASH,
            provider_reference=SandboxGateway()
            .open_checkout(
                Transaction(
                    order=order,
                    provider=PaymentProvider.CASH,
                    provider_reference="",
                    amount=Money(4_000, XOF),
                )
            )
            .provider_reference,
            amount=Money(4_000, XOF),
        )
        txn.refresh_from_db()

        assert txn.provider_reference.startswith("SBX-")
        assert len(txn.provider_reference) == 36


class TestTransactions:
    def test_la_reference_prestataire_est_unique(self, order: Order) -> None:
        """C'est la clé de rapprochement : la dupliquer reviendrait à
        enregistrer deux fois le même encaissement."""
        Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="PD-001",
            amount=Money(4_000, XOF),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Transaction.objects.create(
                order=order,
                provider=PaymentProvider.PAYDUNYA,
                provider_reference="PD-001",
                amount=Money(4_000, XOF),
            )

    def test_un_montant_nul_est_refuse_par_le_modele(self, order: Order) -> None:
        """Encaisser 0 F n'a pas de sens : refusé avant d'atteindre la base."""
        with pytest.raises(BusinessRuleViolation, match="strictement positif"):
            Transaction.objects.create(
                order=order,
                provider=PaymentProvider.CASH,
                provider_reference="CASH-000",
                amount=Money(0, XOF),
            )

        assert not Transaction.objects.filter(provider_reference="CASH-000").exists()

    def test_un_montant_negatif_est_refuse_par_le_modele(self, order: Order) -> None:
        with pytest.raises(BusinessRuleViolation, match="strictement positif"):
            Transaction.objects.create(
                order=order,
                provider=PaymentProvider.CASH,
                provider_reference="CASH-NEG",
                amount=Money(-500, XOF),
            )

    def test_un_montant_nul_est_refuse_par_la_base(self, order: Order) -> None:
        """ADR-010 — le schéma reste la dernière ligne de défense, y compris
        pour les chemins qui n'appellent pas `save()`."""
        with (
            pytest.raises(IntegrityError, match="transaction_amount_positive"),
            transaction.atomic(),
        ):
            Transaction.objects.bulk_create(
                [
                    Transaction(
                        order=order,
                        provider=PaymentProvider.CASH,
                        provider_reference="CASH-000",
                        amount=Money(0, XOF),
                    )
                ]
            )

    def test_un_changement_de_statut_ne_revalide_pas_le_montant(self, order: Order) -> None:
        """`save(update_fields=[...])` ne doit contrôler que ce qu'il écrit.

        Sans cette nuance, `PaymentService._move` — qui ne touche que `status`
        et `completed_at` — se ferait refuser sur toute transaction ancienne
        dont le montant ne lui appartient pas.
        """
        txn = Transaction.objects.create(
            order=order,
            provider=PaymentProvider.CASH,
            provider_reference="CASH-MOVE",
            amount=Money(4_000, XOF),
        )
        txn.status = PaymentStatus.PROCESSING
        txn.save(update_fields=["status", "updated_at"])

        txn.refresh_from_db()
        assert txn.status == PaymentStatus.PROCESSING
