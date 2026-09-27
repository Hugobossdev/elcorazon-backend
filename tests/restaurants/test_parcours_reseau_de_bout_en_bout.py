"""Le parcours réel, d'un bout à l'autre, par l'API seule.

    ADMIN     pays → ville → zones → cuisine → horaires → carte → flotte → service
    CLIENT    adresse → cuisine désignée → carte → personnalisation → panier → commande
    CUISINE   confirmée → en préparation → prête
    LIVRAISON livreur compatible → proposition automatique → acceptation → retrait → livrée
    CLIENT    commande livrée
    ADMIN     la commande se retrouve sous Côte d'Ivoire → Abidjan → Cocody → la cuisine

Aucune écriture en base hors du décor minimal qu'aucune route n'expose — le
compte du siège et le code de rôle du personnel de cuisine. Tout le reste passe
par les routes qu'appellent les trois applications, dans l'ordre où elles les
appellent : c'est ce qui en fait un test de bout en bout et non une suite de
tests unitaires rangés dans le même fichier.

Deux zones dans la ville, et deux livreurs affectés chacun à l'une : la course
de Cocody doit partir chez celui de Cocody, **même s'il est plus loin** de la
cuisine que celui de Yopougon.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.delivery.models import Assignment, CourierProfile
from apps.delivery.states import DeliveryStatus
from apps.orders.models import Order
from apps.orders.states import OrderStatus
from apps.restaurants.models import StaffMembership

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]

XOF = "XOF"


def carre(ouest: float, sud: float, est: float, nord: float) -> dict[str, Any]:
    return {
        "type": "Polygon",
        "coordinates": [[[ouest, sud], [est, sud], [est, nord], [ouest, nord], [ouest, sud]]],
    }


def connecte(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def ok(reponse: Any, attendu: int = status.HTTP_200_OK) -> dict[str, Any]:
    assert reponse.status_code == attendu, reponse.data
    return dict(reponse.data)


SECRET_WEBHOOK = "secret-e2e"


def notifier_encaissement(reference: str, montant: int) -> None:
    """Le prestataire confirme l'encaissement — notification signée, seule
    source de vérité du paiement (`apps.payments.services`)."""
    corps = json.dumps(
        {
            "event_id": f"evt-{reference}",
            "provider_reference": reference,
            "status": "completed",
            "amount": montant,
            "currency": XOF,
        }
    ).encode()
    signature = hmac.new(SECRET_WEBHOOK.encode(), corps, hashlib.sha256).hexdigest()
    reponse = APIClient().post(
        reverse("v1:payments:webhook", args=["paydunya"]),
        data=corps,
        content_type="application/json",
        headers={"X-Signature": signature},
    )
    assert reponse.status_code == status.HTTP_200_OK, reponse.data


class TestReseauDeBoutEnBout:
    # Les deux chemins d'encaissement : à la porte, et en ligne avant la
    # préparation. Le second n'était éprouvé nulle part de bout en bout — ni
    # l'ouverture du paiement, ni sa confirmation par le prestataire, ni ce que
    # le livreur lit ensuite.
    @pytest.mark.parametrize("moyen", ["cash", "mobile_money"])
    def test_de_l_ouverture_du_marche_a_la_commande_livree(
        self, moyen: str, settings: Any, django_capture_on_commit_callbacks: Any
    ) -> None:
        settings.PAYMENT_WEBHOOK_SECRET = SECRET_WEBHOOK
        siege = connecte(User.objects.create_superuser("siege.e2e@elcorazon.test", "motdepasse"))

        # ================================================= 1-3. le marché
        ok(
            siege.post(
                reverse("v1:geography:managed-country-list"),
                {
                    "iso_code": "CI",
                    "name": "Côte d'Ivoire",
                    "currency": XOF,
                    "currency_symbol": "FCFA",
                    "phone_prefix": "+225",
                    "timezone": "Africa/Abidjan",
                },
                format="json",
            ),
            status.HTTP_201_CREATED,
        )
        abidjan = ok(
            siege.post(
                reverse("v1:geography:managed-city-list"),
                {
                    "country": "CI",
                    "name": "Abidjan",
                    "slug": "abidjan",
                    "centroid": {"lat": 5.36, "lon": -4.01},
                },
                format="json",
            ),
            status.HTTP_201_CREATED,
        )
        cocody = ok(
            siege.post(
                reverse("v1:geography:managed-zone-list"),
                {
                    "city": abidjan["id"],
                    "name": "Cocody",
                    "boundary": carre(-4.02, 5.33, -3.95, 5.40),
                    "base_fee": {"amount": "1000", "currency": XOF},
                    "fee_per_km": {"amount": "0", "currency": XOF},
                },
                format="json",
            ),
            status.HTTP_201_CREATED,
        )
        yopougon = ok(
            siege.post(
                reverse("v1:geography:managed-zone-list"),
                {
                    "city": abidjan["id"],
                    "name": "Yopougon",
                    "boundary": carre(-4.12, 5.30, -4.04, 5.37),
                    "base_fee": {"amount": "1500", "currency": XOF},
                    "fee_per_km": {"amount": "0", "currency": XOF},
                },
                format="json",
            ),
            status.HTTP_201_CREATED,
        )

        # =================================================== 4. la cuisine
        cuisine = ok(
            siege.post(
                reverse("v1:restaurants:managed-restaurant-list"),
                {
                    "name": "El Corazón Cocody",
                    "slug": "el-corazon-cocody",
                    "zone": cocody["id"],
                    "address": "Rue des Jardins, Cocody",
                    "location": {"lat": 5.36, "lon": -3.99},
                    "phone": "+22507000001",
                    "default_preparation_minutes": 15,
                },
                format="json",
            ),
            status.HTTP_201_CREATED,
        )
        assert cuisine["status"] == "draft"
        assert cuisine["country"] == "CI"
        slug = cuisine["slug"]

        # Ouverte à toute heure : le test ne dépend pas de l'horloge.
        for jour in range(7):
            for ouvre, ferme in (("00:00", "12:00"), ("12:00", "00:00")):
                ok(
                    siege.post(
                        reverse("v1:restaurants:managed-opening-hours-list"),
                        {
                            "restaurant": cuisine["id"],
                            "weekday": jour,
                            "opens_at": ouvre,
                            "closes_at": ferme,
                        },
                        format="json",
                    ),
                    status.HTTP_201_CREATED,
                )

        # =================================================== 6. la carte
        categorie = ok(
            siege.post(
                reverse("v1:catalog:managed-category-list"),
                {"restaurant": slug, "name": "Plats", "slug": "plats"},
                format="json",
            ),
            status.HTTP_201_CREATED,
        )
        plat = ok(
            siege.post(
                reverse("v1:catalog:managed-item-list"),
                {
                    "restaurant": slug,
                    "category": categorie["id"],
                    "name": "Poulet braisé",
                    "slug": "poulet-braise",
                    "price": {"amount": "3500", "currency": XOF},
                },
                format="json",
            ),
            status.HTTP_201_CREATED,
        )
        piment = ok(
            siege.post(
                reverse("v1:catalog:managed-option-group-list"),
                {"menu_item": plat["id"], "name": "Piment", "min_select": 1, "max_select": 1},
                format="json",
            ),
            status.HTTP_201_CREATED,
        )
        fort = ok(
            siege.post(
                reverse("v1:catalog:managed-option-list"),
                {
                    "group": piment["id"],
                    "name": "Fort",
                    "price_delta": {"amount": "200", "currency": XOF},
                },
                format="json",
            ),
            status.HTTP_201_CREATED,
        )

        # Le personnel de cuisine : il n'existe pas de route pour créer un rôle
        # système, et le rattachement porte ce qui compte ici — le périmètre.
        cuisinier = User.objects.create_user(
            "cuisine.cocody@elcorazon.test",
            "motdepasse",
            full_name="Cuisine",
            user_type=UserType.STAFF,
        )
        cuisinier.roles.add(
            Role.objects.create(
                name="Cuisine Cocody",
                permissions=["orders.read", "orders.update_status", "restaurants.read"],
            )
        )
        StaffMembership.objects.create(user=cuisinier, restaurant_id=cuisine["id"])

        # ================================================ la flotte, par zone
        livreurs: dict[str, CourierProfile] = {}
        for nom, zone in (("cocody", cocody), ("yopougon", yopougon)):
            dossier = ok(
                siege.post(
                    reverse("v1:delivery:courier-list"),
                    {
                        "email": f"livreur.{nom}@elcorazon.test",
                        "password": "MotDePasseSolide!42",
                        "full_name": f"Livreur {nom.title()}",
                        "restaurant": slug,
                        "vehicle_type": "motorcycle",
                    },
                    format="json",
                ),
                status.HTTP_201_CREATED,
            )
            ok(
                siege.post(
                    reverse("v1:delivery:courier-verification", args=[dossier["id"]]),
                    {"status": "approved"},
                    format="json",
                )
            )
            ok(
                siege.post(
                    reverse("v1:delivery:courier-zones", args=[dossier["id"]]),
                    {"zones": [zone["id"]]},
                    format="json",
                )
            )
            profil = CourierProfile.objects.get(pk=dossier["id"])
            livreurs[nom] = profil

        # ============================================ 5. la mise en service
        for cible in ("configuring", "ready", "active"):
            ok(
                siege.post(
                    reverse("v1:restaurants:managed-restaurant-status", args=[slug]),
                    {"status": cible},
                    format="json",
                )
            )

        # Les livreurs se mettent en ligne et signalent leur position : celui de
        # Yopougon est tout près de la cuisine, celui de Cocody plus loin.
        for nom, position in (("cocody", (5.38, -3.96)), ("yopougon", (5.36, -3.991))):
            livreur = connecte(livreurs[nom].user)
            ok(livreur.post(reverse("v1:delivery:me-online"), {"is_online": True}, format="json"))
            CourierProfile.objects.filter(pk=livreurs[nom].pk).update(
                last_location=Point(position[1], position[0], srid=4326)
            )

        # ============================================= 7-10. le client
        client_compte = User.objects.create_user(
            "client.e2e@elcorazon.test",
            "motdepasse",
            full_name="Awa Client",
            phone="+2250700000099",
        )
        client = connecte(client_compte)

        # L'annuaire le propose ; la géographie désigne la cuisine pour l'adresse.
        annuaire = ok(client.get(reverse("v1:restaurants:restaurant-list")))
        assert [c["slug"] for c in annuaire["results"]] == [slug]
        desserte = ok(
            client.post(
                reverse("v1:restaurants:delivery-check"),
                {"lat": 5.37, "lon": -3.98},
                format="json",
            )
        )
        assert desserte["is_available"] is True
        assert desserte["restaurant"]["slug"] == slug
        assert desserte["zone"]["name"] == "Cocody"

        # Hors de toute zone : la réponse, pas une panne.
        hors = ok(
            client.post(
                reverse("v1:restaurants:delivery-check"), {"lat": 7.69, "lon": -5.03}, format="json"
            )
        )
        assert hors["is_available"] is False
        assert hors["unavailable_code"] == "no_kitchen_available"

        adresse = ok(
            client.post(
                reverse("v1:profiles:address-list"),
                {
                    "label": "Maison",
                    "line1": "Riviera 2",
                    "city": abidjan["id"],
                    "location": {"lat": 5.37, "lon": -3.98},
                    "delivery_instructions": "Portail vert",
                },
                format="json",
            ),
            status.HTTP_201_CREATED,
        )

        # ============================================ 11-13. carte et panier
        carte = ok(client.get(reverse("v1:catalog:item-list"), {"restaurant": slug}))
        assert [a["name"] for a in carte["results"]] == ["Poulet braisé"]
        ok(
            client.post(
                reverse("v1:carts:cart-add-line", args=[slug]),
                {"menu_item": plat["id"], "quantity": 2, "options": [fort["id"]]},
                format="json",
            ),
            status.HTTP_201_CREATED,
        )

        # ============================================ 14-16. la commande
        commande = ok(
            client.post(
                reverse("v1:orders:order-list"),
                {"restaurant": slug, "address": adresse["id"], "payment_method": moyen},
                format="json",
                headers={"Idempotency-Key": str(uuid.uuid4())},
            ),
            status.HTTP_201_CREATED,
        )
        # Recalculé par le serveur : 2 × (3 500 + 200) + 1 000 de zone.
        assert commande["subtotal"] == {"amount": "7400", "currency": XOF}
        assert commande["delivery_fee"] == {"amount": "1000", "currency": XOF}
        assert commande["total"] == {"amount": "8400", "currency": XOF}
        assert commande["restaurant"] == slug
        assert (commande["country"], commande["city"], commande["delivery_zone_name"]) == (
            "CI",
            "Abidjan",
            "Cocody",
        )

        # ========================================== 16 bis. le paiement en ligne
        if moyen == "mobile_money":
            # Le moyen est publié par le serveur : c'est cette liste que la
            # caisse propose.
            moyens = client.get(reverse("v1:payments:methods"))
            assert moyens.status_code == status.HTTP_200_OK
            assert "mobile_money" in [m["code"] for m in moyens.data]

            ouverture = ok(
                client.post(reverse("v1:payments:initiate", args=[commande["id"]])),
                status.HTTP_201_CREATED,
            )
            assert ouverture["transaction"]["amount"] == {"amount": "8400", "currency": XOF}
            # « Réessayer » : la même demande, jamais une seconde facture.
            reprise = ok(client.post(reverse("v1:payments:initiate", args=[commande["id"]])))
            assert reprise["transaction"]["id"] == ouverture["transaction"]["id"]

            notifier_encaissement(ouverture["transaction"]["provider_reference"], 8400)
            payee = ok(client.get(reverse("v1:orders:order-detail", args=[commande["id"]])))
            # Confirmée par l'encaissement, pas par la cuisine.
            assert payee["status"] == OrderStatus.CONFIRMED
            assert payee["amount_paid"] == {"amount": "8400", "currency": XOF}

        # ============================================ 17-19. la cuisine
        poste = connecte(cuisinier)
        a_prendre = "pending" if moyen == "cash" else "confirmed"
        file = ok(poste.get(reverse("v1:orders:managed-order-list"), {"status": a_prendre}))
        assert [c["reference"] for c in file["results"]] == [commande["reference"]]
        for cible in ("confirmed", "preparing") if moyen == "cash" else ("preparing",):
            ok(
                poste.post(
                    reverse("v1:orders:managed-order-status", args=[commande["id"]]),
                    {"status": cible},
                    format="json",
                )
            )
        with django_capture_on_commit_callbacks(execute=True):
            ok(
                poste.post(
                    reverse("v1:orders:managed-order-status", args=[commande["id"]]),
                    {"status": "ready"},
                    format="json",
                )
            )

        # ============================= 20-21. le système trouve le bon livreur
        course = Assignment.objects.get(order_id=commande["id"])
        assert course.courier == livreurs["cocody"], (
            "la course de Cocody part chez le livreur de Cocody"
        )
        assert course.status == DeliveryStatus.OFFERED

        livreur_cocody = connecte(livreurs["cocody"].user)
        livreur_yopougon = connecte(livreurs["yopougon"].user)
        assert ok(livreur_yopougon.get(reverse("v1:delivery:assignment-list")))["count"] == 0

        proposee = ok(
            livreur_cocody.get(reverse("v1:delivery:assignment-detail", args=[course.pk]))
        )
        assert proposee["delivery_zone_name"] == "Cocody"
        # À la porte, le livreur encaisse le tout ; payée en ligne, rien.
        assert proposee["amount_to_collect"] == (
            {"amount": "8400", "currency": XOF} if moyen == "cash" else None
        )
        assert proposee["delivery_instructions"] == "Portail vert"
        assert proposee["recipient_phone"] == ""  # pas avant l'acceptation

        # ================================ 22-24. acceptation, retrait, livraison
        acceptee = ok(
            livreur_cocody.post(reverse("v1:delivery:assignment-accept", args=[course.pk]))
        )
        assert acceptee["recipient_phone"] == "+2250700000099"
        # La commande est prête : le livreur peut la retirer. C'est ce que la
        # course dit désormais, et ce que l'application lit pour proposer — ou
        # non — le geste.
        assert acceptee["order_status"] == OrderStatus.READY

        # Le client, lui, voit **qui** vient : identité et numéro, tant que la
        # course est engagée. Les deux manquaient au contrat, si bien que
        # « Message » et « Appeler » restaient grisés toute la livraison.
        pendant = ok(client.get(reverse("v1:tracking:order", args=[commande["id"]])))
        assert pendant["courier"]["full_name"] == livreurs["cocody"].user.full_name
        assert pendant["courier"]["phone"] == livreurs["cocody"].user.phone

        for etape in ("picked_up", "on_the_way", "delivered"):
            ok(
                livreur_cocody.post(
                    reverse("v1:delivery:assignment-status", args=[course.pk]),
                    {"status": etape},
                    format="json",
                )
            )

        # =============================================== 25. le client
        suivi = ok(client.get(reverse("v1:orders:order-detail", args=[commande["id"]])))
        assert suivi["status"] == OrderStatus.DELIVERED

        # Livrée : il reste de quoi noter la livraison — le nom — mais plus
        # personne à joindre pour cette course, donc plus de numéro.
        apres = ok(client.get(reverse("v1:tracking:order", args=[commande["id"]])))
        assert apres["courier"]["full_name"] == livreurs["cocody"].user.full_name
        assert apres["courier"]["phone"] == ""

        # ============================================== 26. le siège
        filtres = {
            "country__iso_code": "CI",
            "city__slug": "abidjan",
            "delivery_zone": cocody["id"],
            "restaurant__slug": slug,
        }
        supervision = ok(siege.get(reverse("v1:orders:managed-order-list"), filtres))
        assert [c["reference"] for c in supervision["results"]] == [commande["reference"]]
        assert (
            ok(
                siege.get(
                    reverse("v1:orders:managed-order-list"),
                    {**filtres, "delivery_zone": yopougon["id"]},
                )
            )["count"]
            == 0
        )

        reseau = siege.get(
            reverse("v1:analytics:report-network"),
            {"start": "2000-01-01", "end": "2100-01-01", "level": "zone"},
        )
        assert reseau.status_code == status.HTTP_200_OK, reseau.data
        assert reseau.data == [
            {
                "key": cocody["id"],
                "name": "Cocody",
                "city": "Abidjan",
                "country": "CI",
                "currency": XOF,
                "orders_count": 1,
                "in_progress_count": 0,
                "delivered_count": 1,
                "cancelled_count": 0,
                "revenue_minor": 8400,
            }
        ]
        assert Order.objects.get(pk=commande["id"]).delivery_zone_id == uuid.UUID(cocody["id"])
