"""Campagnes de notifications ciblées — ADR-008.

Deux vérifications portent ce module :

* `test_un_double_envoi_ne_part_qu_une_fois` — un envoi de masse ne se rappelle
  pas, et le destinataire qui reçoit deux fois le même message se désabonne ;
* `test_un_refus_de_marketing_est_respecte` — le consentement est décidé par
  `notify` et par lui seul ; le redécider ici produirait deux règles, dont
  l'une finirait par être la mauvaise.
"""

from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, User, UserType
from apps.notifications import services
from apps.notifications.models import Audience, Campaign, CampaignStatus, Notification
from apps.notifications.services import (
    _send_if_still_due,
    recipients_of,
    schedule_campaign,
    send_campaign,
    unschedule_campaign,
)
from apps.notifications.tasks import send_scheduled_campaigns
from apps.profiles.models import CustomerPreference
from apps.restaurants.models import Restaurant, StaffMembership
from common.exceptions import BusinessRuleViolation
from tests.fixtures import build_order

pytestmark = [pytest.mark.django_db, pytest.mark.postgis]


def personnel(email: str, restaurant: Restaurant, *permissions: str) -> User:
    member = User.objects.create_user(
        email, "motdepasse", full_name="Personnel", user_type=UserType.STAFF
    )
    member.roles.add(Role.objects.create(name=f"Rôle {email}", permissions=list(permissions)))
    StaffMembership.objects.create(user=member, restaurant=restaurant)
    return member


@pytest.fixture
def marketing() -> APIClient:
    """Le siège — seul compte qui rédige et envoie (voir `TestPerimetre`).

    Une campagne vise « les clients actifs », « ceux qui ne commandent plus » :
    des segments qui ne s'arrêtent à aucune ville. `notifications.send` dit
    qu'on a le droit d'envoyer ; il ne peut rien dire de *à qui*.
    """
    client = APIClient()
    client.force_authenticate(User.objects.create_superuser("siege@elcorazon.test", "motdepasse"))
    return client


def campagne(**extra: object) -> Campaign:
    valeurs: dict[str, object] = {
        "title": "−20 % ce week-end",
        "body": "Profitez-en jusqu'à dimanche.",
        "audience": Audience.ALL_CUSTOMERS,
    }
    valeurs.update(extra)
    return Campaign.objects.create(**valeurs)


class TestRedaction:
    def test_sans_permission_rien_n_est_lisible(self, customer: User) -> None:
        client = APIClient()
        client.force_authenticate(customer)

        assert (
            client.get(reverse("v1:notifications:campaign-list")).status_code
            == status.HTTP_403_FORBIDDEN
        )

    def test_une_campagne_nait_en_brouillon_et_porte_son_auteur(self, marketing: APIClient) -> None:
        """Une trace qu'on peut renseigner soi-même ne trace rien : l'auteur
        vient du jeton."""
        response = marketing.post(
            reverse("v1:notifications:campaign-list"),
            {
                "title": "Nouveau burger",
                "body": "À découvrir dès aujourd'hui.",
                "audience": Audience.ALL_CUSTOMERS,
                "created_by": "peu importe",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == CampaignStatus.DRAFT
        assert response.data["created_by_email"] == "siege@elcorazon.test"

    def test_une_campagne_envoyee_ne_se_modifie_plus(
        self, marketing: APIClient, customer: User
    ) -> None:
        """L'historique afficherait un texte que personne n'a reçu."""
        envoyee = send_campaign(campagne())

        response = marketing.patch(
            reverse("v1:notifications:campaign-detail", args=[envoyee.pk]),
            {"title": "Réécrit après coup"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        envoyee.refresh_from_db()
        assert envoyee.title == "−20 % ce week-end"


class TestSegments:
    def test_le_segment_par_defaut_est_la_clientele_active(
        self, customer: User, courier_user: User
    ) -> None:
        """Ni les livreurs, ni les comptes bloqués."""
        bloque = User.objects.create_user("bloque@elcorazon.test", "motdepasse", is_active=False)

        cibles = set(recipients_of(campagne()).values_list("email", flat=True))

        assert cibles == {customer.email}
        assert courier_user.email not in cibles
        assert bloque.email not in cibles

    def test_la_reconquete_vise_aussi_ceux_qui_n_ont_jamais_commande(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        """La formulation par exclusion embarque les comptes sans commande, et
        c'est la population qu'une campagne de reconquête vise en premier."""
        recent = User.objects.create_user("recent@elcorazon.test", "motdepasse")
        build_order(restaurant, recent)

        cibles = set(
            recipients_of(campagne(audience=Audience.LAPSED_CUSTOMERS)).values_list(
                "email", flat=True
            )
        )

        assert customer.email in cibles
        assert recent.email not in cibles

    def test_le_segment_actif_ne_retient_que_les_commandes_recentes(
        self, customer: User, restaurant: Restaurant
    ) -> None:
        ancienne = build_order(restaurant, customer, reference="EC000042")
        type(ancienne).objects.filter(pk=ancienne.pk).update(
            placed_at=timezone.now() - dt.timedelta(days=90)
        )

        cibles = recipients_of(campagne(audience=Audience.ACTIVE_CUSTOMERS))

        assert cibles.count() == 0

    def test_l_estimation_annonce_un_majorant(self, marketing: APIClient, customer: User) -> None:
        """Elle compte le segment, pas les envois aboutis : le consentement ne
        se vérifie qu'à l'écriture de chaque notification."""
        brouillon = campagne()

        response = marketing.get(reverse("v1:notifications:campaign-audience", args=[brouillon.pk]))

        assert response.data["recipients"] == 1


class TestEnvoi:
    def test_l_envoi_ecrit_une_notification_par_destinataire(
        self, marketing: APIClient, customer: User
    ) -> None:
        brouillon = campagne()

        response = marketing.post(
            reverse("v1:notifications:campaign-send", args=[brouillon.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == CampaignStatus.SENT
        assert response.data["recipient_count"] == 1
        assert Notification.objects.filter(user=customer, kind="marketing").count() == 1

    def test_un_double_envoi_ne_part_qu_une_fois(
        self, marketing: APIClient, customer: User
    ) -> None:
        """Un double clic sur « envoyer » arrive régulièrement ; le rejeu est
        absorbé plutôt que refusé, pour ne pas faire croire à un échec."""
        brouillon = campagne()
        url = reverse("v1:notifications:campaign-send", args=[brouillon.pk])

        premier = marketing.post(url, {}, format="json")
        second = marketing.post(url, {}, format="json")

        assert premier.status_code == second.status_code == status.HTTP_200_OK
        assert Notification.objects.filter(user=customer, kind="marketing").count() == 1
        assert second.data["sent_at"] == premier.data["sent_at"]

    def test_un_refus_de_marketing_est_respecte(self, marketing: APIClient, customer: User) -> None:
        """`notify` écarte le compte, et le compteur ne l'inclut donc pas :
        annoncer le segment plutôt que les envois donnerait un taux d'ouverture
        flatteur et faux."""
        CustomerPreference.objects.update_or_create(
            user=customer, defaults={"marketing_push_enabled": False}
        )
        brouillon = campagne()

        response = marketing.post(
            reverse("v1:notifications:campaign-send", args=[brouillon.pk]), {}, format="json"
        )

        assert response.data["recipient_count"] == 0
        assert not Notification.objects.filter(user=customer, kind="marketing").exists()


class TestPerimetre:
    """Une campagne relève du siège, comme un code promotionnel national.

    `notifications.send` figure dans le rôle « Manager », qui est cloisonné :
    un gérant de Lomé poussait donc un message à la clientèle d'Abidjan — tous
    pays confondus — sans qu'aucune garde ne s'y oppose. C'est le contraire
    exact de ce que l'ADR-005 tient partout ailleurs, et un envoi de masse ne
    se rappelle pas.
    """

    @pytest.fixture
    def gerant(self, restaurant: Restaurant) -> APIClient:
        client = APIClient()
        client.force_authenticate(
            personnel("gerant@elcorazon.test", restaurant, "notifications.send")
        )
        return client

    def test_un_compte_cloisonne_n_envoie_pas(self, gerant: APIClient, customer: User) -> None:
        brouillon = campagne()

        response = gerant.post(
            reverse("v1:notifications:campaign-send", args=[brouillon.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        brouillon.refresh_from_db()
        assert brouillon.status == CampaignStatus.DRAFT
        assert not Notification.objects.filter(kind="marketing").exists()

    def test_un_compte_cloisonne_ne_redige_pas(self, gerant: APIClient) -> None:
        response = gerant.post(
            reverse("v1:notifications:campaign-list"),
            {"title": "Depuis Lomé", "body": "…", "audience": Audience.ALL_CUSTOMERS},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not Campaign.objects.filter(title="Depuis Lomé").exists()

    def test_un_compte_cloisonne_ne_modifie_pas(self, gerant: APIClient) -> None:
        brouillon = campagne()

        response = gerant.patch(
            reverse("v1:notifications:campaign-detail", args=[brouillon.pk]),
            {"title": "Réécrit"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        brouillon.refresh_from_db()
        assert brouillon.title == "−20 % ce week-end"

    def test_un_compte_cloisonne_n_estime_pas_la_clientele_de_l_enseigne(
        self, gerant: APIClient, customer: User
    ) -> None:
        """Le compte porte sur toute la clientèle : c'est déjà une réponse sur
        un périmètre qui n'est pas le sien."""
        response = gerant.get(reverse("v1:notifications:campaign-audience", args=[campagne().pk]))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_un_compte_cloisonne_lit_les_campagnes_et_leur_bilan(self, gerant: APIClient) -> None:
        """Ce qui est fermé est l'écriture. Un texte de campagne n'est pas une
        donnée d'exploitation, et son bilan est cloisonné à la lecture."""
        envoyee = send_campaign(campagne())

        liste = gerant.get(reverse("v1:notifications:campaign-list"))
        bilan = gerant.get(reverse("v1:notifications:campaign-stats", args=[envoyee.pk]))

        assert liste.status_code == status.HTTP_200_OK
        assert bilan.status_code == status.HTTP_200_OK


class TestProgrammation:
    """Dater un envoi — la dernière pièce du geste « rédiger, relire, envoyer ».

    Un envoi de masse se prépare la veille et part quand les gens ont leur
    téléphone en main : « −20 % ce midi » écrit à 3 h du matin n'a aucune
    chance. L'envoi lui-même reste `send_campaign`, avec son verrou et son
    unicité — le battement ne fait que l'appeler à l'heure dite.
    """

    def test_une_campagne_datee_attend_son_heure(
        self, marketing: APIClient, customer: User
    ) -> None:
        brouillon = campagne()
        quand = timezone.now() + dt.timedelta(hours=2)

        response = marketing.post(
            reverse("v1:notifications:campaign-schedule", args=[brouillon.pk]),
            {"scheduled_at": quand.isoformat()},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == CampaignStatus.SCHEDULED
        assert not Notification.objects.filter(kind="marketing").exists()

    def test_une_heure_passee_est_refusee(self, marketing: APIClient) -> None:
        """Elle partirait au tour suivant, ce qui n'est pas ce qu'on a demandé —
        et « Envoyer » existe pour partir maintenant."""
        response = marketing.post(
            reverse("v1:notifications:campaign-schedule", args=[campagne().pk]),
            {"scheduled_at": (timezone.now() - dt.timedelta(minutes=1)).isoformat()},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_une_campagne_programmee_ne_se_modifie_pas(self, marketing: APIClient) -> None:
        """Le texte relu au moment de dater est celui qui partira."""
        brouillon = campagne()
        marketing.post(
            reverse("v1:notifications:campaign-schedule", args=[brouillon.pk]),
            {"scheduled_at": (timezone.now() + dt.timedelta(hours=1)).isoformat()},
            format="json",
        )

        response = marketing.patch(
            reverse("v1:notifications:campaign-detail", args=[brouillon.pk]),
            {"title": "Réécrit après programmation"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_annuler_la_programmation_rend_le_brouillon(self, marketing: APIClient) -> None:
        brouillon = campagne()
        marketing.post(
            reverse("v1:notifications:campaign-schedule", args=[brouillon.pk]),
            {"scheduled_at": (timezone.now() + dt.timedelta(hours=1)).isoformat()},
            format="json",
        )

        response = marketing.post(
            reverse("v1:notifications:campaign-unschedule", args=[brouillon.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["status"] == CampaignStatus.DRAFT
        assert response.data["scheduled_at"] is None

    def test_le_battement_envoie_ce_qui_est_du_et_rien_d_autre(self, customer: User) -> None:
        due = campagne(
            status=CampaignStatus.SCHEDULED, scheduled_at=timezone.now() - dt.timedelta(minutes=1)
        )
        plus_tard = campagne(
            title="Plus tard",
            status=CampaignStatus.SCHEDULED,
            scheduled_at=timezone.now() + dt.timedelta(hours=3),
        )

        resultat = send_scheduled_campaigns()

        assert resultat["campaigns"] == 1
        due.refresh_from_db()
        plus_tard.refresh_from_db()
        assert due.status == CampaignStatus.SENT
        assert plus_tard.status == CampaignStatus.SCHEDULED
        assert Notification.objects.filter(user=customer, kind="marketing").count() == 1

    def test_un_battement_rejoue_n_envoie_pas_deux_fois(self, customer: User) -> None:
        """Deux battements restés debout après un redéploiement, ou un tour
        relancé à la main : le destinataire ne doit rien recevoir en double."""
        campagne(
            status=CampaignStatus.SCHEDULED, scheduled_at=timezone.now() - dt.timedelta(minutes=1)
        )

        premier = send_scheduled_campaigns()
        second = send_scheduled_campaigns()

        assert premier["campaigns"] == 1
        assert second["campaigns"] == 0
        assert Notification.objects.filter(user=customer, kind="marketing").count() == 1

    def test_une_heure_passee_pendant_un_arret_part_avec_retard(self, customer: User) -> None:
        """Le battement rattrape : une campagne perdue vaut bien pire qu'une
        campagne en retard."""
        campagne(
            status=CampaignStatus.SCHEDULED, scheduled_at=timezone.now() - dt.timedelta(days=1)
        )

        assert send_scheduled_campaigns()["campaigns"] == 1

    def test_programmer_releve_du_siege(self, restaurant: Restaurant) -> None:
        client = APIClient()
        client.force_authenticate(
            personnel("gerant@elcorazon.test", restaurant, "notifications.send")
        )

        response = client.post(
            reverse("v1:notifications:campaign-schedule", args=[campagne().pk]),
            {"scheduled_at": (timezone.now() + dt.timedelta(hours=1)).isoformat()},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestProgrammationConcurrente:
    """Le battement et un opérateur agissent sur la même campagne au même instant.

    L'instance **périmée** — chargée avant que le battement n'envoie — rejoue
    exactement l'état d'une requête parallèle, sans concurrence réelle : c'est
    ce que lit une vue qui a chargé la campagne pendant que l'envoi tournait.
    """

    def test_reprogrammer_une_campagne_partie_entre_temps_est_refuse(self, customer: User) -> None:
        """Sans relecture sous verrou, « programmée » était reposé sur une
        campagne déjà partie — qui repartait à la nouvelle heure."""
        programmee = campagne(
            status=CampaignStatus.SCHEDULED, scheduled_at=timezone.now() - dt.timedelta(minutes=1)
        )
        perimee = Campaign.objects.get(pk=programmee.pk)
        send_scheduled_campaigns()

        with pytest.raises(BusinessRuleViolation):
            schedule_campaign(perimee, quand=timezone.now() + dt.timedelta(hours=1))

        programmee.refresh_from_db()
        assert programmee.status == CampaignStatus.SENT
        assert send_scheduled_campaigns()["campaigns"] == 0
        assert Notification.objects.filter(user=customer, kind="marketing").count() == 1

    def test_deprogrammer_une_campagne_partie_entre_temps_est_refuse(self, customer: User) -> None:
        """Redevenue brouillon, elle aurait pu être envoyée une seconde fois."""
        programmee = campagne(
            status=CampaignStatus.SCHEDULED, scheduled_at=timezone.now() - dt.timedelta(minutes=1)
        )
        perimee = Campaign.objects.get(pk=programmee.pk)
        send_scheduled_campaigns()

        with pytest.raises(BusinessRuleViolation):
            unschedule_campaign(perimee)

        programmee.refresh_from_db()
        assert programmee.status == CampaignStatus.SENT

    def test_une_programmation_annulee_avant_le_verrou_ne_part_pas(self, customer: User) -> None:
        """Le battement lit la liste des campagnes échues sans verrou ; l'annulation
        peut tomber entre cette lecture et l'envoi. `send_campaign` ne refuse
        qu'une campagne déjà partie : c'est la relecture de `_send_if_still_due`
        qui fait respecter l'annulation."""
        programmee = campagne(
            status=CampaignStatus.SCHEDULED, scheduled_at=timezone.now() - dt.timedelta(minutes=1)
        )
        unschedule_campaign(programmee)

        assert _send_if_still_due(programmee.pk) is None
        programmee.refresh_from_db()
        assert programmee.status == CampaignStatus.DRAFT
        assert not Notification.objects.filter(kind="marketing").exists()

    def test_une_campagne_en_echec_ne_bloque_pas_les_suivantes(
        self, customer: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La plus ancienne passe en tête de chaque tour : si son échec
        interrompait le tour, aucune autre campagne ne partirait plus jamais."""
        en_echec = campagne(
            title="En échec",
            status=CampaignStatus.SCHEDULED,
            scheduled_at=timezone.now() - dt.timedelta(hours=1),
        )
        suivante = campagne(
            title="Suivante",
            status=CampaignStatus.SCHEDULED,
            scheduled_at=timezone.now() - dt.timedelta(minutes=1),
        )
        envoi_reel = services.send_campaign

        def envoi(campaign: Campaign) -> Campaign:
            if campaign.pk == en_echec.pk:
                raise RuntimeError("prestataire indisponible")
            return envoi_reel(campaign)

        monkeypatch.setattr(services, "send_campaign", envoi)

        resultat = send_scheduled_campaigns()

        assert resultat == {"campaigns": 1, "recipients": 1, "failures": 1}
        en_echec.refresh_from_db()
        suivante.refresh_from_db()
        # Retentée au tour suivant, et non perdue.
        assert en_echec.status == CampaignStatus.SCHEDULED
        assert suivante.status == CampaignStatus.SENT
