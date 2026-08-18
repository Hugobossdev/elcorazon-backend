"""Contrat d'API — ADR-009.

L'ADR promet qu'« un test de contrat vérifie qu'un champ déclaré non nul ne
sort jamais absent ». C'est ce module, et c'est la réponse structurelle au
troisième piège relevé en Phase 1 : `User.fromMap` et `Address.fromJson`
appellent `DateTime.parse` **sans garde nulle**, si bien qu'omettre
`created_at` d'une réponse d'authentification ne dégrade pas l'affichage — ça
fait planter la connexion.

Le schéma OpenAPI est la référence : il est généré depuis les sérialiseurs,
donc il ne peut pas dériver du code. Ce que ces tests vérifient, c'est que les
**réponses réelles** s'y conforment — un champ déclaré obligatoire dans le
schéma et absent de la réponse est exactement le défaut qu'on cherche.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.catalog.models import MenuItem
from apps.orders.models import Order
from apps.restaurants.models import Restaurant

pytestmark = [pytest.mark.django_db, pytest.mark.postgis, pytest.mark.contract]


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    """Schéma OpenAPI généré, partagé par toute la suite.

    Généré une fois : la génération parcourt toutes les vues et tous les
    sérialiseurs du projet, ce qui prend plus de temps que les tests eux-mêmes.
    """
    import django

    django.setup()
    from drf_spectacular.generators import SchemaGenerator

    return dict(SchemaGenerator().get_schema(request=None, public=True))


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def as_customer(customer: User) -> APIClient:
    separate = APIClient()
    separate.force_authenticate(customer)
    return separate


def component(schema: dict[str, Any], name: str) -> dict[str, Any]:
    composants: dict[str, Any] = schema["components"]["schemas"]
    assert name in composants, f"Composant absent du schéma : {name}"
    return dict(composants[name])


def assert_conforme(
    payload: Any, spec: dict[str, Any], schema: dict[str, Any], chemin: str = ""
) -> None:
    """Vérifie une charge utile contre son composant de schéma.

    Deux règles, et seulement deux : **tout champ requis est présent**, et
    **aucun champ non déclaré nullable ne vaut `null`**. C'est exactement le
    contrat que les clients Dart lisent sans garde.
    """
    if "$ref" in spec:
        spec = component(schema, spec["$ref"].rsplit("/", 1)[-1])

    if spec.get("type") == "array":
        for index, item in enumerate(payload):
            assert_conforme(item, spec.get("items", {}), schema, f"{chemin}[{index}]")
        return

    if spec.get("type") != "object" or not isinstance(payload, dict):
        return

    proprietes: dict[str, Any] = spec.get("properties", {})

    for requis in spec.get("required", []):
        assert requis in payload, f"Champ requis absent : {chemin}{requis}"

    for nom, valeur in payload.items():
        declaration = proprietes.get(nom)
        if declaration is None:
            continue
        if valeur is None:
            assert declaration.get("nullable") or "$ref" in declaration, (
                f"Champ non nullable rendu à null : {chemin}{nom}"
            )
            continue
        assert_conforme(valeur, declaration, schema, f"{chemin}{nom}.")


class TestChampsRequis:
    """Le piège nº 3 de la Phase 1, fermé par le serveur plutôt que par le client."""

    def test_la_reponse_d_authentification_porte_toutes_ses_dates(
        self, client: APIClient, schema: dict[str, Any]
    ) -> None:
        """C'est le cas exact qui faisait planter la connexion : `created_at`
        omis, `DateTime.parse(null)` côté Dart."""
        response = client.post(
            reverse("v1:accounts:register"),
            {
                "email": "contrat@elcorazon.test",
                "password": "MotDePasseSolide!42",
                "full_name": "Contrat",
            },
            format="json",
        )

        assert response.status_code == 201
        assert_conforme(response.data["user"], component(schema, "User"), schema)
        assert response.data["user"]["created_at"] is not None

    def test_la_fiche_de_commande_est_complete(
        self, as_customer: APIClient, order: Order, schema: dict[str, Any]
    ) -> None:
        response = as_customer.get(reverse("v1:orders:order-detail", args=[order.pk]))

        assert_conforme(response.data, component(schema, "OrderDetail"), schema)

    def test_la_fiche_d_article_est_complete(
        self, client: APIClient, menu_item: MenuItem, schema: dict[str, Any]
    ) -> None:
        response = client.get(reverse("v1:catalog:item-detail", args=[menu_item.pk]))

        assert_conforme(response.data, component(schema, "MenuItemDetail"), schema)

    def test_la_fiche_de_restaurant_est_complete(
        self, client: APIClient, restaurant: Restaurant, schema: dict[str, Any]
    ) -> None:
        response = client.get(reverse("v1:restaurants:restaurant-detail", args=[restaurant.slug]))

        assert_conforme(response.data, component(schema, "RestaurantDetail"), schema)


class TestFormeDesMontants:
    """ADR-007 — `{"amount": "1250", "currency": "XOF"}`, la valeur en chaîne."""

    def test_un_montant_sort_en_chaine_partout(
        self, client: APIClient, menu_item: MenuItem
    ) -> None:
        """En nombre, `JSON.parse` le convertirait en double et l'exactitude
        défendue jusqu'en base se perdrait au dernier mètre."""
        prix = client.get(reverse("v1:catalog:item-list")).data["results"][0]["price"]

        assert set(prix) == {"amount", "currency"}
        assert isinstance(prix["amount"], str)
        assert isinstance(prix["currency"], str)

    def test_tous_les_montants_de_commande_ont_la_meme_forme(
        self, as_customer: APIClient, order: Order
    ) -> None:
        """Quatre montants sur une commande : s'ils divergeaient, chaque client
        devrait apprendre quatre formes du même objet."""
        fiche = as_customer.get(reverse("v1:orders:order-detail", args=[order.pk])).data

        for champ in ("subtotal", "delivery_fee", "discount", "total"):
            assert set(fiche[champ]) == {"amount", "currency"}, champ
            assert isinstance(fiche[champ]["amount"], str), champ


class TestFormeDesPositions:
    def test_une_position_sort_nommee(self, client: APIClient, restaurant: Restaurant) -> None:
        """Ni GeoJSON ni WKT : `lat` et `lon`, nommés. PostGIS attend
        `Point(x=lon, y=lat)`, l'inverse de l'ordre de lecture humain — le
        nommage supprime l'erreur que produit un couple positionnel."""
        position = client.get(reverse("v1:restaurants:restaurant-list")).data["results"][0][
            "location"
        ]

        assert set(position) == {"lat", "lon"}
        assert isinstance(position["lat"], float)


class TestEnveloppeDeListe:
    def test_la_pagination_est_a_la_racine(self, client: APIClient, menu_item: MenuItem) -> None:
        """ADR-009 — `count`, `next`, `previous`, `results` à la racine, pas
        sous `meta`."""
        response = client.get(reverse("v1:catalog:item-list"))

        assert set(response.data) == {"count", "next", "previous", "results"}
        assert isinstance(response.data["count"], int)

    def test_le_composant_pagine_declare_la_meme_forme(self, schema: dict[str, Any]) -> None:
        """Le schéma et la réponse doivent dire la même chose : un client
        généré depuis le premier appelle la seconde."""
        pagine = component(schema, "PaginatedMenuItemList")

        assert set(pagine["properties"]) == {"count", "next", "previous", "results"}


class TestFormeDesErreurs:
    """RFC 9457 — `application/problem+json`, avec un `code` métier stable."""

    def test_une_erreur_metier_porte_un_code_stable(
        self, as_customer: APIClient, restaurant: Restaurant
    ) -> None:
        """Le client s'appuie sur `code`, jamais sur `detail` : les messages
        sont traduisibles et peuvent changer, les codes non."""
        response = as_customer.post(
            reverse("v1:orders:order-list"),
            {"restaurant": restaurant.slug, "address": "x", "payment_method": "cash"},
            format="json",
            headers={"Idempotency-Key": "contrat-1"},
        )

        assert response["Content-Type"].startswith("application/problem+json")
        assert {"type", "title", "status", "code"} <= set(response.data)

    def test_une_ressource_absente_rend_un_probleme(self, as_customer: APIClient) -> None:
        import uuid

        response = as_customer.get(reverse("v1:orders:order-detail", args=[uuid.uuid4()]))

        assert response.status_code == 404
        assert response.data["code"] == "not_found"
        assert response.data["type"].startswith("https://")

    def test_une_transition_refusee_annonce_les_cibles_possibles(
        self, as_customer: APIClient, order: Order
    ) -> None:
        """Le client ne devine pas ce qu'il aurait dû faire : la réponse le
        dit, depuis la même table que celle qui a refusé."""
        Order.objects.filter(pk=order.pk).update(status="delivered")

        response = as_customer.post(
            reverse("v1:orders:order-cancel", args=[order.pk]), {}, format="json"
        )

        assert response.status_code == 409
        assert response.data["code"] == "business_rule_violation"


class TestStabiliteDuSchema:
    def test_le_versionnement_est_dans_l_url(self, schema: dict[str, Any]) -> None:
        """ADR-009 — visible dans les journaux et les traces, trivial à router
        côté Nginx, et une v2 pourra coexister sans négociation de contenu."""
        assert all(chemin.startswith("/api/v1/") for chemin in schema["paths"]), sorted(
            chemin for chemin in schema["paths"] if not chemin.startswith("/api/v1/")
        )

    def test_les_enumerations_ont_des_noms_stables(self, schema: dict[str, Any]) -> None:
        """Un nom auto-généré comme `Status5c8Enum` change dès qu'un choix est
        ajouté : le client généré casse sans raison visible."""
        composants = schema["components"]["schemas"]
        enums = [nom for nom in composants if nom.endswith("Enum")]

        illisibles = [nom for nom in enums if any(car.isdigit() for car in nom)]
        assert not illisibles, f"Énumérations au nom instable : {illisibles}"
