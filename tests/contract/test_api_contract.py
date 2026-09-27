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


class TestContratsDeLaChaineLivraison:
    """Les trois formes ajoutées pour rendre la livraison cohérente.

    Elles sont neuves, donc jamais éprouvées contre le schéma : c'est
    exactement le moment où un champ déclaré obligatoire sort absent.
    """

    def test_la_file_de_cuisine_est_complete(
        self, restaurant: Restaurant, customer: User, schema: dict[str, Any]
    ) -> None:
        """Ce que le poste de cuisine lit — plats compris."""
        from apps.accounts.models import Role, UserType
        from apps.orders.states import OrderStatus
        from apps.restaurants.models import StaffMembership
        from tests.fixtures import build_order

        build_order(restaurant, customer, reference="EC700001", status=OrderStatus.PREPARING)
        cuisinier = User.objects.create_user(
            "contrat.cuisine@elcorazon.test",
            "motdepasse",
            full_name="Cuisine",
            user_type=UserType.STAFF,
        )
        cuisinier.roles.add(
            Role.objects.create(name="Cuisine contrat", permissions=["orders.read"])
        )
        StaffMembership.objects.create(user=cuisinier, restaurant=restaurant)
        client = APIClient()
        client.force_authenticate(cuisinier)

        reponse = client.get(
            reverse("v1:orders:managed-order-kitchen") + f"?restaurant={restaurant.slug}"
        )

        assert reponse.status_code == 200, reponse.data
        for carte in reponse.data["results"]:
            assert_conforme(carte, component(schema, "KitchenOrder"), schema)

    def test_la_course_du_livreur_est_complete(
        self, restaurant: Restaurant, customer: User, courier: Any, schema: dict[str, Any]
    ) -> None:
        """La course porte ce que le livreur doit voir — y compris l'étape de
        la commande, sans laquelle son application proposait de récupérer un
        repas encore en cuisine."""
        from apps.delivery.services import AssignmentService
        from apps.orders.states import OrderStatus
        from tests.fixtures import build_order

        commande = build_order(restaurant, customer, reference="EC700002", status=OrderStatus.READY)
        course = AssignmentService.offer(order=commande, courier=courier)
        client = APIClient()
        client.force_authenticate(courier.user)

        reponse = client.get(reverse("v1:delivery:assignment-detail", args=[course.pk]))

        assert reponse.status_code == 200, reponse.data
        assert_conforme(reponse.data, component(schema, "Assignment"), schema)
        assert reponse.data["order_status"] == OrderStatus.READY

    def test_le_suivi_client_est_complet(
        self, as_customer: APIClient, order: Order, schema: dict[str, Any]
    ) -> None:
        """Sans livreur affecté : le suivi doit rester conforme, pas vide."""
        reponse = as_customer.get(reverse("v1:tracking:order", args=[order.pk]))

        assert reponse.status_code == 200
        assert_conforme(reponse.data, component(schema, "Tracking"), schema)


class TestContratDeLEncaissement:
    """Ce que le paiement publie — et ce qu'il reste à réclamer à la porte.

    Le cinquième objet de la chaîne, et le seul qui n'avait aucun test de
    contrat. Il en avait besoin plus que les autres : `amount_to_collect` est
    **nullable**, et sa valeur a changé de sens — elle se déduisait du *moyen*
    de paiement, elle est maintenant la soustraction de ce qui reste dû. Un
    champ dont la nullité porte une information métier est celui qu'un
    sérialiseur déclare mal sans que rien ne s'en aperçoive : c'est ainsi que
    `courier_fee` était déclaré obligatoire tout en sortant nul.
    """

    def encaisse(self, order: Order, montant: Any, reference: str) -> Any:
        from apps.payments.models import PaymentProvider, PaymentStatus, Transaction
        from apps.payments.services import report_settled_total

        transaction = Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference=reference,
            amount=montant,
            status=PaymentStatus.COMPLETED,
        )
        report_settled_total(order)
        return transaction

    def test_la_transaction_est_complete(
        self, as_customer: APIClient, order: Order, schema: dict[str, Any]
    ) -> None:
        self.encaisse(order, order.total, "PD-CONTRAT-001")

        reponse = as_customer.get(reverse("v1:payments:transaction-list"))

        assert reponse.status_code == 200, reponse.data
        assert reponse.data["results"], "la transaction du client doit lui être visible"
        for ligne in reponse.data["results"]:
            assert_conforme(ligne, component(schema, "Transaction"), schema)

    def test_une_transaction_en_cours_reste_conforme(
        self, as_customer: APIClient, order: Order, schema: dict[str, Any]
    ) -> None:
        """Le cas qui fait sortir les nuls : rien n'est encore encaissé.

        `completed_at` et `failure_reason` sont vides tant que le prestataire
        n'a pas répondu — c'est l'état dans lequel **toute** transaction passe,
        donc celui que le contrat doit supporter en premier.
        """
        from apps.payments.models import PaymentProvider, PaymentStatus, Transaction

        Transaction.objects.create(
            order=order,
            provider=PaymentProvider.PAYDUNYA,
            provider_reference="PD-CONTRAT-002",
            amount=order.total,
            status=PaymentStatus.PROCESSING,
        )

        reponse = as_customer.get(reverse("v1:payments:transaction-list"))

        assert reponse.status_code == 200, reponse.data
        for ligne in reponse.data["results"]:
            assert_conforme(ligne, component(schema, "Transaction"), schema)

    def test_la_commande_publie_ce_qui_a_ete_encaisse(
        self, as_customer: APIClient, order: Order, schema: dict[str, Any]
    ) -> None:
        """`amount_paid` est nul quand rien n'a été réglé, et doit l'être **déclaré**.

        C'est par lui que la livraison et le back-office savent ce qui reste
        dû, sans connaître le module de paiement (ADR-002).
        """
        avant = as_customer.get(reverse("v1:orders:order-detail", args=[order.pk]))
        assert avant.status_code == 200, avant.data
        assert avant.data["amount_paid"] is None
        assert_conforme(avant.data, component(schema, "OrderDetail"), schema)

        self.encaisse(order, order.total, "PD-CONTRAT-003")

        apres = as_customer.get(reverse("v1:orders:order-detail", args=[order.pk]))
        assert apres.data["amount_paid"] == {
            "amount": str(order.total.amount_minor),
            "currency": order.total.currency,
        }
        assert_conforme(apres.data, component(schema, "OrderDetail"), schema)

    @pytest.mark.parametrize(
        ("part", "attendu_nul"),
        [
            (0, False),  # rien de réglé : le total reste à encaisser
            (50, False),  # à moitié réglé : le reste, et non le total
            (100, True),  # soldé : plus rien à réclamer
        ],
    )
    def test_le_montant_a_encaisser_reste_conforme_a_chaque_etat_de_reglement(
        self,
        restaurant: Restaurant,
        customer: User,
        courier: Any,
        schema: dict[str, Any],
        part: int,
        attendu_nul: bool,
    ) -> None:
        """Les trois états du règlement, contre le même composant de schéma.

        Le champ est déclaré nullable ; ces trois cas vérifient qu'il l'est
        **et** que sa valeur suit la soustraction, pas le moyen de paiement.
        """
        from apps.delivery.services import AssignmentService
        from apps.orders.states import OrderStatus
        from common.money import Money
        from tests.fixtures import build_order

        commande = build_order(
            restaurant, customer, reference=f"EC7100{part:02d}", status=OrderStatus.READY
        )
        if part:
            montant = Money(commande.total.amount_minor * part // 100, commande.total.currency)
            self.encaisse(commande, montant, f"PD-CONTRAT-1{part:03d}")

        course = AssignmentService.offer(order=commande, courier=courier)
        client = APIClient()
        client.force_authenticate(courier.user)

        reponse = client.get(reverse("v1:delivery:assignment-detail", args=[course.pk]))

        assert reponse.status_code == 200, reponse.data
        assert_conforme(reponse.data, component(schema, "Assignment"), schema)
        assert (reponse.data["amount_to_collect"] is None) is attendu_nul


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


def _corps(operation: dict[str, Any]) -> str | None:
    """Le composant du corps de requête JSON d'une opération, ou `None`."""
    schema = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    reference = schema.get("$ref")
    return reference.rsplit("/", 1)[-1] if reference else None


def _reponse(operation: dict[str, Any], code: str) -> dict[str, Any]:
    reponse: dict[str, Any] = (
        operation.get("responses", {}).get(code, {}).get("content", {}).get("application/json", {})
    )
    return dict(reponse.get("schema", {}))


class TestContratDesRoutesDuPersonnel:
    """Ce que le back-office envoie, tel que le schéma le promet.

    Un décorateur `@extend_schema` décore la **fonction qui le suit**. Insérer
    une route entre lui et sa méthode le fait changer de propriétaire sans que
    rien ne casse à l'exécution : c'est arrivé à `cancel` quand `notes` a été
    ajoutée — le schéma annonçait un corps d'annulation pour écrire une note,
    une commande entière pour les lire, et plus aucun corps pour annuler.
    """

    def test_annuler_une_commande_exige_un_motif(self, schema: dict[str, Any]) -> None:
        operation = schema["paths"]["/api/v1/orders/manage/{id}/cancel/"]["post"]

        assert _corps(operation) == "StaffCancelRequest"
        assert _reponse(operation, "200") == {"$ref": "#/components/schemas/OrderDetail"}

    def test_les_notes_internes_se_lisent_et_s_ecrivent_comme_des_notes(
        self, schema: dict[str, Any]
    ) -> None:
        route = schema["paths"]["/api/v1/orders/manage/{id}/notes/"]

        assert _reponse(route["get"], "200") == {
            "type": "array",
            "items": {"$ref": "#/components/schemas/OrderNote"},
        }
        assert _corps(route["post"]) == "OrderNoteRequest"
        assert _reponse(route["post"], "201") == {"$ref": "#/components/schemas/OrderNote"}

    def test_un_corps_d_annulation_ne_sert_qu_a_annuler(self, schema: dict[str, Any]) -> None:
        """Le symptôme général du décorateur déplacé : un corps de requête qui
        apparaît sous une route à laquelle il n'appartient pas."""
        porteurs = sorted(
            f"{verbe.upper()} {chemin}"
            for chemin, route in schema["paths"].items()
            for verbe, operation in route.items()
            if isinstance(operation, dict) and _corps(operation) == "StaffCancelRequest"
        )

        assert porteurs == ["POST /api/v1/orders/manage/{id}/cancel/"]
