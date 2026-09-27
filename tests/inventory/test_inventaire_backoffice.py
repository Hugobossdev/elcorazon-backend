"""Back-office de l'inventaire — ADR-005, les trois étages.

Chaque route arrive avec sa permission, son cloisonnement et ses **tests de
refus** : c'est la règle du plan (document 10, §4), et la seule qui empêche la
surface de grandir plus vite que les garanties qui la tiennent.

Les refus d'abord, parce que ce sont eux que l'implémentation précédente n'avait
pas : ses rôles n'étaient appliqués que côté interface.
"""

from __future__ import annotations

import ast
import uuid
from typing import Any

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.geography.models import DeliveryZone
from apps.inventory.models import (
    AdjustmentRequest,
    AdjustmentStatus,
    Ingredient,
    MovementKind,
    StockItem,
    StockMovement,
)
from apps.inventory.services import InventoryService
from apps.restaurants.models import Restaurant, RestaurantStatus, StaffMembership
from common.money import Money
from common.quantities import Dimension, Quantity
from tests.architecture.graph import APPS_ROOT

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


def kg(valeur: str) -> Quantity:
    return Quantity.from_unit(valeur, "kg")


def membre(email: str, restaurant: Restaurant | None, *permissions: str) -> User:
    user = User.objects.create_user(
        email, "motdepasse", full_name=email.split("@")[0], user_type=UserType.STAFF
    )
    user.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    if restaurant is not None:
        StaffMembership.objects.create(user=user, restaurant=restaurant)
    return user


def client_de(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def cle() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


TOUT = (
    "inventory.read",
    "inventory.write",
    "inventory.receive",
    "inventory.adjust",
    "inventory.approve",
)


@pytest.fixture
def ailleurs(zone: DeliveryZone) -> Restaurant:
    return Restaurant.objects.create(
        name="El Corazón Kara",
        slug="el-corazon-kara",
        zone=zone,
        address="Kara",
        location=zone.city.centroid,
        phone="+22890000001",
        status=RestaurantStatus.ACTIVE,
    )


@pytest.fixture
def gerant(restaurant: Restaurant) -> User:
    return membre("gerant@elcorazon.test", restaurant, *TOUT)


@pytest.fixture
def commis(restaurant: Restaurant) -> User:
    """Le poste de cuisine : reçoit et déclare, ne configure ni ne valide."""
    return membre(
        "commis@elcorazon.test",
        restaurant,
        "inventory.read",
        "inventory.receive",
        "inventory.adjust",
    )


@pytest.fixture
def lecteur(restaurant: Restaurant) -> User:
    return membre("lecteur@elcorazon.test", restaurant, "inventory.read")


@pytest.fixture
def siege() -> User:
    return User.objects.create_superuser("siege@elcorazon.test", "motdepasse", full_name="Siège")


@pytest.fixture
def boeuf() -> Ingredient:
    return Ingredient.objects.create(name="Bœuf", slug="boeuf", dimension=Dimension.MASS)


@pytest.fixture
def stock(restaurant: Restaurant, boeuf: Ingredient) -> StockItem:
    """Dix kilos à 4 000 F le kilo."""
    item = InventoryService.open_item(restaurant=restaurant, ingredient=boeuf)
    InventoryService.receive(item=item, quantity=kg("10"), unit_cost=Money(4_000, XOF))
    item.refresh_from_db()
    return item


def plafonner(restaurant: Restaurant, montant: int | None) -> None:
    restaurant.stock_adjustment_ceiling = Money(montant, XOF) if montant is not None else None
    restaurant.save(
        update_fields=["stock_adjustment_ceiling_minor", "stock_adjustment_ceiling_currency"]
    )


def url(nom: str, *args: Any) -> str:
    return reverse(f"v1:inventory:{nom}", args=args)


# ============================================================== référentiel


class TestReferentiel:
    def test_le_referentiel_se_lit_avec_inventory_read(
        self, lecteur: User, boeuf: Ingredient
    ) -> None:
        reponse = client_de(lecteur).get(url("managed-ingredient-list"))

        assert reponse.status_code == status.HTTP_200_OK
        assert [i["slug"] for i in reponse.data["results"]] == ["boeuf"]

    def test_sans_permission_il_ne_se_lit_pas(self, restaurant: Restaurant) -> None:
        sans_droit = membre("sans@elcorazon.test", restaurant, "catalog.read")

        reponse = client_de(sans_droit).get(url("managed-ingredient-list"))

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    def test_un_gerant_de_cuisine_ne_cree_pas_d_ingredient(self, gerant: User) -> None:
        """Une tomate est une tomate à Lomé comme à Abidjan : le référentiel
        n'appartient à aucune cuisine, et le défaut sûr est le siège."""
        reponse = client_de(gerant).post(
            url("managed-ingredient-list"),
            {"name": "Tomate", "slug": "tomate", "dimension": Dimension.MASS},
            format="json",
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN
        assert not Ingredient.objects.filter(slug="tomate").exists()

    def test_le_siege_cree_et_la_dimension_ne_change_plus(self, siege: User) -> None:
        client = client_de(siege)
        creation = client.post(
            url("managed-ingredient-list"),
            {
                "name": "Huile",
                "slug": "huile",
                "dimension": Dimension.VOLUME,
                "allergens": ["Arachide", "arachide", " "],
            },
            format="json",
        )
        assert creation.status_code == status.HTTP_201_CREATED
        assert creation.data["allergens"] == ["arachide"]

        changement = client.patch(
            url("managed-ingredient-detail", creation.data["id"]),
            {"dimension": Dimension.MASS},
            format="json",
        )

        assert changement.status_code == status.HTTP_400_BAD_REQUEST

    def test_un_ingredient_ne_se_supprime_pas(self, siege: User, boeuf: Ingredient) -> None:
        """Il se retire (`is_active`), et son histoire reste lisible."""
        reponse = client_de(siege).delete(url("managed-ingredient-detail", boeuf.pk))

        assert reponse.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


# ===================================================================== stock


class TestLignesDeStock:
    def test_ouvrir_une_ligne_est_idempotent(
        self, gerant: User, restaurant: Restaurant, boeuf: Ingredient
    ) -> None:
        client = client_de(gerant)
        corps = {"restaurant": restaurant.slug, "ingredient": str(boeuf.pk)}

        premiere = client.post(url("managed-stock-item-list"), corps, format="json")
        seconde = client.post(url("managed-stock-item-list"), corps, format="json")

        assert premiere.status_code == status.HTTP_201_CREATED
        assert seconde.status_code == status.HTTP_200_OK
        assert premiere.data["id"] == seconde.data["id"]
        assert premiere.data["on_hand"] == {"amount": "0", "unit": "g", "dimension": "mass"}

    def test_ouvrir_hors_perimetre_est_refuse(
        self, gerant: User, ailleurs: Restaurant, boeuf: Ingredient
    ) -> None:
        reponse = client_de(gerant).post(
            url("managed-stock-item-list"),
            {"restaurant": ailleurs.slug, "ingredient": str(boeuf.pk)},
            format="json",
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN
        assert not StockItem.objects.filter(restaurant=ailleurs).exists()

    def test_le_stock_d_une_autre_cuisine_est_introuvable(
        self, gerant: User, ailleurs: Restaurant, boeuf: Ingredient
    ) -> None:
        autre = InventoryService.open_item(restaurant=ailleurs, ingredient=boeuf)
        client = client_de(gerant)

        liste = client.get(url("managed-stock-item-list"))
        detail = client.get(url("managed-stock-item-detail", autre.pk))

        assert all(ligne["id"] != str(autre.pk) for ligne in liste.data["results"])
        assert detail.status_code == status.HTTP_404_NOT_FOUND

    def test_la_ligne_rend_son_cout_au_kilogramme_et_sa_valeur(
        self, lecteur: User, stock: StockItem
    ) -> None:
        reponse = client_de(lecteur).get(url("managed-stock-item-detail", stock.pk))

        assert reponse.data["unit_cost"] == {"amount": "4000", "currency": XOF}
        assert reponse.data["cost_unit"] == "kg"
        assert reponse.data["stock_value"] == {"amount": "40000", "currency": XOF}

    def test_le_stock_ne_s_ecrit_pas_par_un_patch(self, gerant: User, stock: StockItem) -> None:
        """La correction s'appelle un ajustement, et elle porte un motif."""
        client_de(gerant).patch(
            url("managed-stock-item-detail", stock.pk),
            {"on_hand": {"amount": "99", "unit": "kg"}},
            format="json",
        )

        stock.refresh_from_db()
        assert stock.on_hand == kg("10")

    def test_le_seuil_d_alerte_et_le_filtre_des_lignes_basses(
        self, gerant: User, stock: StockItem
    ) -> None:
        client = client_de(gerant)

        mauvais = client.patch(
            url("managed-stock-item-detail", stock.pk),
            {"low_stock_threshold": {"amount": "5", "unit": "l"}},
            format="json",
        )
        assert mauvais.status_code == status.HTTP_400_BAD_REQUEST

        client.patch(
            url("managed-stock-item-detail", stock.pk),
            {"low_stock_threshold": {"amount": "12", "unit": "kg"}},
            format="json",
        )
        basses = client.get(url("managed-stock-item-list"), {"low": "true"})

        assert [ligne["id"] for ligne in basses.data["results"]] == [str(stock.pk)]
        assert basses.data["results"][0]["is_low"] is True


# ================================================================= réception


class TestReception:
    def test_sans_cle_d_idempotence_la_reception_est_refusee(
        self, commis: User, stock: StockItem
    ) -> None:
        reponse = client_de(commis).post(
            url("managed-stock-item-receive", stock.pk),
            {"quantity": {"amount": "5", "unit": "kg"}},
            format="json",
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_sans_inventory_receive_la_reception_est_refusee(
        self, lecteur: User, stock: StockItem
    ) -> None:
        reponse = client_de(lecteur).post(
            url("managed-stock-item-receive", stock.pk),
            {"quantity": {"amount": "5", "unit": "kg"}},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    def test_le_prix_du_lot_devient_un_cout_au_kilogramme(
        self, commis: User, restaurant: Restaurant, boeuf: Ingredient
    ) -> None:
        """12 000 F les 5 kg : 2 400 F le kilo. On ne demande pas à un
        magasinier le prix du gramme."""
        item = InventoryService.open_item(restaurant=restaurant, ingredient=boeuf)

        reponse = client_de(commis).post(
            url("managed-stock-item-receive", item.pk),
            {
                "quantity": {"amount": "5", "unit": "kg"},
                "total_cost": {"amount": "12000", "currency": XOF},
                "reference": "BL-2026-0913",
            },
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_201_CREATED
        assert reponse.data["kind"] == MovementKind.RECEIPT
        assert reponse.data["unit_cost"] == {"amount": "2400", "currency": XOF}
        assert reponse.data["value"] == {"amount": "12000", "currency": XOF}
        assert reponse.data["actor"]["id"] == str(commis.pk)
        item.refresh_from_db()
        assert item.on_hand == kg("5")

    def test_une_reception_rejouee_rend_la_premiere(self, commis: User, stock: StockItem) -> None:
        client = client_de(commis)
        entete = cle()
        corps = {"quantity": {"amount": "5", "unit": "kg"}}

        premiere = client.post(
            url("managed-stock-item-receive", stock.pk), corps, format="json", headers=entete
        )
        seconde = client.post(
            url("managed-stock-item-receive", stock.pk), corps, format="json", headers=entete
        )

        assert premiere.data["id"] == seconde.data["id"]
        stock.refresh_from_db()
        assert stock.on_hand == kg("15")

    def test_une_reception_dans_la_mauvaise_dimension_est_refusee(
        self, commis: User, stock: StockItem
    ) -> None:
        reponse = client_de(commis).post(
            url("managed-stock-item-receive", stock.pk),
            {"quantity": {"amount": "5", "unit": "l"}},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_une_quantite_trop_precise_est_refusee_et_non_arrondie(
        self, commis: User, stock: StockItem
    ) -> None:
        reponse = client_de(commis).post(
            url("managed-stock-item-receive", stock.pk),
            {"quantity": {"amount": "0.0001", "unit": "g"}},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_on_ne_recoit_pas_dans_le_stock_d_une_autre_cuisine(
        self, commis: User, ailleurs: Restaurant, boeuf: Ingredient
    ) -> None:
        autre = InventoryService.open_item(restaurant=ailleurs, ingredient=boeuf)

        reponse = client_de(commis).post(
            url("managed-stock-item-receive", autre.pk),
            {"quantity": {"amount": "5", "unit": "kg"}},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_404_NOT_FOUND


# ============================================================ déclarations


class TestPertesEtCorrections:
    def test_sous_le_plafond_la_perte_est_ecrite_en_201(
        self, commis: User, restaurant: Restaurant, stock: StockItem
    ) -> None:
        plafonner(restaurant, 5_000)

        reponse = client_de(commis).post(
            url("managed-stock-item-waste", stock.pk),
            {"quantity": {"amount": "500", "unit": "g"}, "reason": "Tombé au sol"},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_201_CREATED
        assert reponse.data["outcome"] == "applied"
        assert reponse.data["request"] is None
        assert reponse.data["movement"]["value"] == {"amount": "-2000", "currency": XOF}

    def test_au_dela_la_perte_attend_en_202(
        self, commis: User, restaurant: Restaurant, stock: StockItem
    ) -> None:
        plafonner(restaurant, 5_000)

        reponse = client_de(commis).post(
            url("managed-stock-item-waste", stock.pk),
            {"quantity": {"amount": "2", "unit": "kg"}, "reason": "Froid rompu"},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_202_ACCEPTED
        assert reponse.data["outcome"] == "pending_approval"
        assert reponse.data["movement"] is None
        assert reponse.data["request"]["estimated_value"] == {"amount": "8000", "currency": XOF}
        stock.refresh_from_db()
        assert stock.on_hand == kg("10")

    def test_une_perte_sans_motif_est_refusee(self, commis: User, stock: StockItem) -> None:
        reponse = client_de(commis).post(
            url("managed-stock-item-waste", stock.pk),
            {"quantity": {"amount": "1", "unit": "kg"}, "reason": ""},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_sans_inventory_adjust_rien_ne_se_declare(
        self, lecteur: User, stock: StockItem
    ) -> None:
        reponse = client_de(lecteur).post(
            url("managed-stock-item-waste", stock.pk),
            {"quantity": {"amount": "1", "unit": "kg"}, "reason": "Tombé"},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    def test_un_comptage_devient_un_ecart_calcule_par_le_serveur(
        self, commis: User, restaurant: Restaurant, stock: StockItem
    ) -> None:
        """Le commis dit ce qu'il trouve sur l'étagère ; le serveur fait la
        soustraction contre le stock du moment."""
        plafonner(restaurant, 5_000)

        reponse = client_de(commis).post(
            url("managed-stock-item-adjust", stock.pk),
            {"counted": {"amount": "9.5", "unit": "kg"}, "reason": "Inventaire du soir"},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_201_CREATED
        assert reponse.data["movement"]["quantity"]["amount"] == "-500"
        stock.refresh_from_db()
        assert stock.on_hand == kg("9.5")

    def test_un_ecart_dans_la_mauvaise_unite_est_refuse_en_400_et_non_en_500(
        self, commis: User, stock: StockItem
    ) -> None:
        """« −1 l » de bœuf : une donnée mal formée. L'erreur de dimension, qui
        n'est pas une erreur métier du socle, remontait en panne serveur."""
        reponse = client_de(commis).post(
            url("managed-stock-item-adjust", stock.pk),
            {"delta": {"amount": "-1", "unit": "l"}, "reason": "Inventaire"},
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST
        assert reponse.data["code"] == "dimension_mismatch"

    def test_compte_et_ecart_ensemble_sont_refuses(self, commis: User, stock: StockItem) -> None:
        reponse = client_de(commis).post(
            url("managed-stock-item-adjust", stock.pk),
            {
                "counted": {"amount": "9", "unit": "kg"},
                "delta": {"amount": "-1", "unit": "kg"},
                "reason": "Inventaire",
            },
            format="json",
            headers=cle(),
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST


# ================================================================ validation


class TestValidation:
    @pytest.fixture
    def demande(self, commis: User, restaurant: Restaurant, stock: StockItem) -> AdjustmentRequest:
        plafonner(restaurant, None)
        reponse = client_de(commis).post(
            url("managed-stock-item-waste", stock.pk),
            {"quantity": {"amount": "2", "unit": "kg"}, "reason": "Froid rompu"},
            format="json",
            headers=cle(),
        )
        assert reponse.status_code == status.HTTP_202_ACCEPTED
        return AdjustmentRequest.objects.get(pk=reponse.data["request"]["id"])

    def test_le_commis_ne_valide_pas(self, commis: User, demande: AdjustmentRequest) -> None:
        reponse = client_de(commis).post(
            url("managed-adjustment-request-approve", demande.pk), {}, format="json"
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN

    def test_le_gerant_valide_et_la_perte_sort_du_stock(
        self, gerant: User, demande: AdjustmentRequest, stock: StockItem
    ) -> None:
        reponse = client_de(gerant).post(
            url("managed-adjustment-request-approve", demande.pk),
            {"note": "Relevé de température joint"},
            format="json",
        )

        assert reponse.status_code == status.HTTP_200_OK
        assert reponse.data["status"] == AdjustmentStatus.APPROVED
        assert reponse.data["decided_by"]["id"] == str(gerant.pk)
        assert reponse.data["movement"] is not None
        stock.refresh_from_db()
        assert stock.on_hand == kg("8")

    def test_detenir_la_permission_ne_suffit_pas_a_valider_sa_propre_perte(
        self, gerant: User, restaurant: Restaurant, stock: StockItem
    ) -> None:
        """Le quatre-yeux n'est pas une permission : un gérant muni de
        `inventory.approve` ne valide pas ce qu'il a lui-même déclaré."""
        plafonner(restaurant, None)
        client = client_de(gerant)
        declaration = client.post(
            url("managed-stock-item-waste", stock.pk),
            {"quantity": {"amount": "2", "unit": "kg"}, "reason": "Froid rompu"},
            format="json",
            headers=cle(),
        )

        reponse = client.post(
            url("managed-adjustment-request-approve", declaration.data["request"]["id"]),
            {},
            format="json",
        )

        assert reponse.status_code == status.HTTP_409_CONFLICT
        assert reponse.data["code"] == "four_eyes_required"

    def test_un_refus_sans_motif_est_refuse(self, gerant: User, demande: AdjustmentRequest) -> None:
        reponse = client_de(gerant).post(
            url("managed-adjustment-request-reject", demande.pk), {"note": ""}, format="json"
        )

        assert reponse.status_code == status.HTTP_400_BAD_REQUEST

    def test_la_file_d_une_autre_cuisine_est_introuvable(
        self, demande: AdjustmentRequest, ailleurs: Restaurant
    ) -> None:
        gerant_d_ailleurs = membre("kara@elcorazon.test", ailleurs, *TOUT)
        client = client_de(gerant_d_ailleurs)

        liste = client.get(url("managed-adjustment-request-list"))
        validation = client.post(
            url("managed-adjustment-request-approve", demande.pk), {}, format="json"
        )

        assert liste.data["results"] == []
        assert validation.status_code == status.HTTP_404_NOT_FOUND
        demande.refresh_from_db()
        assert demande.status == AdjustmentStatus.PENDING

    def test_la_file_se_filtre_sur_les_demandes_en_attente(
        self, lecteur: User, demande: AdjustmentRequest
    ) -> None:
        reponse = client_de(lecteur).get(
            url("managed-adjustment-request-list"), {"status": "pending"}
        )

        assert [d["id"] for d in reponse.data["results"]] == [str(demande.pk)]


# =================================================================== journal


class TestJournal:
    def test_le_journal_se_lit_par_curseur_et_reste_cloisonne(
        self, lecteur: User, stock: StockItem, ailleurs: Restaurant, boeuf: Ingredient
    ) -> None:
        autre = InventoryService.open_item(restaurant=ailleurs, ingredient=boeuf)
        InventoryService.receive(item=autre, quantity=kg("3"))

        reponse = client_de(lecteur).get(url("managed-stock-movement-list"))

        assert reponse.status_code == status.HTTP_200_OK
        assert "next" in reponse.data and "count" not in reponse.data
        assert {m["stock_item"] for m in reponse.data["results"]} == {stock.pk}

    def test_le_journal_ne_s_ecrit_pas_par_l_api(self, gerant: User, stock: StockItem) -> None:
        mouvement = StockMovement.objects.filter(stock_item=stock).first()
        assert mouvement is not None
        client = client_de(gerant)

        creation = client.post(url("managed-stock-movement-list"), {}, format="json")
        suppression = client.delete(url("managed-stock-movement-detail", mouvement.pk))

        assert creation.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        assert suppression.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


# ============================================================ le plafond


class TestPlafondDeLaCuisine:
    def test_le_siege_fixe_le_plafond_dans_la_devise_de_la_cuisine(
        self, siege: User, restaurant: Restaurant
    ) -> None:
        client = client_de(siege)
        adresse = reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug])

        en_euros = client.patch(
            adresse,
            {"stock_adjustment_ceiling": {"amount": "5000", "currency": "EUR"}},
            format="json",
        )
        en_francs = client.patch(
            adresse,
            {"stock_adjustment_ceiling": {"amount": "5000", "currency": XOF}},
            format="json",
        )

        assert en_euros.status_code == status.HTTP_400_BAD_REQUEST
        assert en_francs.status_code == status.HTTP_200_OK
        restaurant.refresh_from_db()
        assert restaurant.stock_adjustment_ceiling == Money(5_000, XOF)

    def test_le_gerant_ne_fixe_pas_son_propre_plafond(
        self, gerant: User, restaurant: Restaurant
    ) -> None:
        """Celui dont le plafond encadre les écritures ne le desserre pas."""
        reponse = client_de(gerant).patch(
            reverse("v1:restaurants:managed-restaurant-detail", args=[restaurant.slug]),
            {"stock_adjustment_ceiling": {"amount": "999999", "currency": XOF}},
            format="json",
        )

        assert reponse.status_code == status.HTTP_403_FORBIDDEN


# ============================================================== architecture


class TestAucuneRouteNeContourneLePlafond:
    def test_le_back_office_n_appelle_jamais_waste_ni_adjust(self) -> None:
        """Ces deux écritures ignorent le plafond : elles sont ce qu'appelle la
        validation, une fois la seconde personne passée. Une route qui les
        appellerait directement ouvrirait une perte sans contrôle de valeur."""
        source = (APPS_ROOT / "inventory" / "backoffice.py").read_text(encoding="utf-8")
        appels = {
            noeud.attr
            for noeud in ast.walk(ast.parse(source))
            if isinstance(noeud, ast.Attribute)
            and isinstance(noeud.value, ast.Name)
            and noeud.value.id == "InventoryService"
        }

        assert not appels & {"waste", "adjust", "_apply", "consume", "reserve", "release"}
        assert {"declare_waste", "declare_adjustment", "receive"} <= appels
