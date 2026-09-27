"""Les heurts de contrainte, traduits en refus lisibles — RFC 9457.

Ce que cette suite garde n'est pas la contrainte : c'est PostgreSQL qui la tient,
et aucune validation applicative ne peut s'y substituer. Ce qui est éprouvé ici,
c'est **ce que le client lit** quand la contrainte parle — un 409 nommé plutôt
qu'un 500 muet, et rien du message d'origine.

Aucune exception n'est fabriquée à la main : les `IntegrityError` de cette suite
viennent réellement de PostgreSQL, ce qui est le seul moyen d'éprouver la
lecture du diagnostic (`sqlstate`, `constraint_name`) telle que le pilote la
rend.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.services import AuthService
from common.exceptions import UNIQUE_CONSTRAINT_PROBLEMS, problem_detail_handler

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

TELEPHONE = "+22890111222"


@pytest.fixture
def occupant() -> User:
    """Un compte qui tient déjà le numéro."""
    user, _ = AuthService.register(
        email="ama@elcorazon.test",
        password="MotDePasseSolide!42",
        full_name="Ama Koffi",
        phone=TELEPHONE,
    )
    return user


def _heurt(**champs: Any) -> IntegrityError:
    """Provoque un vrai heurt de contrainte et rend l'exception levée.

    `transaction.atomic()` est indispensable : sans point de sauvegarde, la
    transaction du test resterait avortée et tout ce qui suit échouerait sur
    « current transaction is aborted ».
    """
    with pytest.raises(IntegrityError) as leve, transaction.atomic():
        User.objects.create(**champs)
    return leve.value


class TestTraductionDUnHeurt:
    def test_un_doublon_de_telephone_devient_un_refus_nomme(self, occupant: User) -> None:
        """Le cas de la course : deux inscriptions au même numéro se croisent, la
        seconde heurte `accounts_user_phone_key`. Sans ce filet, elle rend 500 —
        une panne serveur là où il n'y a qu'un numéro déjà pris."""
        exc = _heurt(email="autre@elcorazon.test", phone=TELEPHONE, full_name="Autre")

        response = problem_detail_handler(exc, {})

        assert response is not None
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "phone_already_exists"
        assert response.data["detail"] == "Ce numéro de téléphone est déjà utilisé."
        # Nommé comme une erreur de validation : le client surligne le champ sans
        # avoir à distinguer le chemin du serializer de celui de la course.
        assert response.data["errors"] == {"phone": ["Ce numéro de téléphone est déjà utilisé."]}

    def test_un_doublon_d_adresse_devient_un_refus_nomme(self, occupant: User) -> None:
        exc = _heurt(email=occupant.email, phone="+22890999888", full_name="Autre")

        response = problem_detail_handler(exc, {})

        assert response is not None
        assert response.data["code"] == "email_already_exists"

    def test_rien_du_message_de_postgresql_ne_sort(self, occupant: User) -> None:
        """`str(exc)` porte l'instruction SQL entière et le `DETAIL` du serveur —
        `Key (phone)=(+228…) already exists`. Le rendre divulguerait le numéro
        d'un autre compte à qui tente une inscription, et le schéma à tout le
        monde."""
        exc = _heurt(email="autre@elcorazon.test", phone=TELEPHONE, full_name="Autre")

        response = problem_detail_handler(exc, {})

        assert response is not None
        corps = json.dumps(response.data, ensure_ascii=False)
        assert TELEPHONE not in corps
        assert "INSERT" not in corps
        assert "accounts_user" not in corps

    def test_le_type_reste_une_uri_stable(self, occupant: User) -> None:
        exc = _heurt(email="autre@elcorazon.test", phone=TELEPHONE, full_name="Autre")

        response = problem_detail_handler(exc, {})

        assert response is not None
        assert response.data["type"].endswith("/phone-already-exists")


class TestCeQuiNEstPasTraduit:
    def test_une_contrainte_inconnue_reste_un_refus_generique(self) -> None:
        """Une unicité que la table ne nomme pas se dit quand même — le client a
        bien demandé deux fois la même chose — mais sans prétendre savoir quel
        champ montrer."""
        from apps.accounts.models import Role

        Role.objects.create(name="Régisseur")
        with pytest.raises(IntegrityError) as leve, transaction.atomic():
            Role.objects.create(name="Régisseur")

        response = problem_detail_handler(leve.value, {})

        assert response is not None
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "resource_already_exists"
        assert "errors" not in response.data

    def test_une_violation_qui_n_est_pas_une_unicite_reste_un_500(self) -> None:
        """Un `CHECK` heurté est un défaut de **notre** côté : le code a écrit un
        état que le schéma déclare impossible. L'habiller en 4xx le ferait sortir
        des alertes tout en laissant croire au client qu'il a mal demandé — le
        gestionnaire rend `None`, et DRF relance en 500 journalisé.

        `superuser_is_staff` : un superutilisateur qui ne serait pas du
        personnel. Aucune requête client ne peut demander cela."""
        with pytest.raises(IntegrityError) as leve, transaction.atomic():
            User.objects.create(
                email="intrus@elcorazon.test",
                full_name="Intrus",
                user_type="customer",
                is_superuser=True,
            )

        assert problem_detail_handler(leve.value, {}) is None


class TestParLAPI:
    def test_l_inscription_double_reste_un_400_de_validation(self, occupant: User) -> None:
        """Le chemin normal ne change pas : le serializer voit le doublon avant
        l'écriture, et un refus de validation est un 400. Le filet ne se
        déclenche que lorsque personne n'a vu venir le heurt."""
        response = APIClient().post(
            reverse("v1:accounts:register"),
            {
                "email": "nouveau@elcorazon.test",
                "password": "MotDePasseSolide!42",
                "full_name": "Nouveau",
                "phone": TELEPHONE,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestLaTable:
    def test_chaque_entree_nomme_une_contrainte_reelle(self) -> None:
        """Une contrainte renommée par une migration rendrait la traduction
        muette sans que rien ne le signale : le heurt retomberait en refus
        générique. Ce test lit le catalogue de PostgreSQL."""
        from django.db import connection

        with connection.cursor() as curseur:
            curseur.execute("SELECT conname FROM pg_constraint")
            connues = {ligne[0] for ligne in curseur.fetchall()}

        assert set(UNIQUE_CONSTRAINT_PROBLEMS) <= connues
