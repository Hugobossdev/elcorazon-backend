"""Erreurs métier et leur traduction en RFC 9457 — ADR-009.

Le test décisif est `test_un_membre_reserve_est_refuse_a_la_construction`.
`code` est un nom parfaitement naturel pour « le code d'invitation » ou « le code
promotionnel », et c'est aussi le nom du champ **stable** sur lequel les clients
s'appuient. Sans garde, la collision se manifestait par un doublon de mot-clé à
la sérialisation : un 500 à la place d'un 409 par ailleurs légitime, et seulement
le jour où ce refus se produisait en production.
"""

from __future__ import annotations

import pytest

from common.exceptions import (
    RESERVED_MEMBERS,
    BusinessRuleViolation,
    InsufficientStock,
    problem_detail_handler,
)


class TestDonneesContextuelles:
    def test_une_donnee_libre_voyage_avec_le_refus(self) -> None:
        """C'est l'intérêt de `extra` : le client sait *quoi* corriger sans avoir
        à analyser la phrase de `detail`, qui est traduisible."""
        exc = BusinessRuleViolation("Stock épuisé.", menu_item_id="abc", remaining=2)

        assert exc.extra == {"menu_item_id": "abc", "remaining": 2}

    @pytest.mark.parametrize("member", sorted(RESERVED_MEMBERS - {"detail"}))
    def test_un_membre_reserve_est_refuse_a_la_construction(self, member: str) -> None:
        """Refusé au plus tôt, et bruyamment : la faute est de programmation, elle
        doit tomber au premier passage dans le code plutôt que sur un incident."""
        with pytest.raises(ValueError, match="réservé"):
            BusinessRuleViolation("Refus.", **{member: "valeur"})

    def test_detail_est_protege_par_la_signature_elle_meme(self) -> None:
        """Écarté de la liste précédente parce qu'il est déjà inexprimable :
        `detail` est le paramètre positionnel, donc Python refuse le doublon avant
        que la garde ne soit atteinte. Une contrainte structurelle valant mieux
        qu'une vérification, il n'y a rien à ajouter — seulement à le constater."""
        with pytest.raises(TypeError, match="detail"):
            BusinessRuleViolation("Refus.", **{"detail": "autre"})

    def test_le_message_propose_une_sortie(self) -> None:
        """Un refus qui ne dit pas quoi faire se termine en relecture du code du
        socle par quelqu'un qui cherchait juste à nommer un champ."""
        with pytest.raises(ValueError, match="invitation_code"):
            BusinessRuleViolation("Refus.", code="AB3K9P")

    def test_la_regle_vaut_pour_les_sous_classes(self) -> None:
        """`InsufficientStock` et les autres héritent du constructeur : la garde ne
        doit pas s'évaporer dès qu'on spécialise l'exception."""
        with pytest.raises(ValueError, match="réservé"):
            InsufficientStock("Stock épuisé.", status="sold_out")

    def test_les_membres_reserves_couvrent_le_corps_rfc_9457(self) -> None:
        """Si `_problem` gagne un champ, il doit rejoindre cette liste — sinon la
        collision redevient possible sans que rien ne le signale."""
        assert RESERVED_MEMBERS == {
            "code",
            "detail",
            "errors",
            "headers",
            "status",
            "title",
            "type",
        }


@pytest.mark.django_db
class TestUneRaisonLisible:
    """Tout refus porte une phrase affichable — RFC 9457, §`detail`.

    Les trois applications lisent `detail`, et ne retombent sur `errors` que
    lorsque c'est un objet champ par champ. Trois refus sortaient sans ni l'un
    ni l'autre, et s'affichaient « Une erreur est survenue » : une
    `ValidationError` Django levée hors sérialiseur, une liste d'erreurs DRF,
    et les 403/404 posés par le gestionnaire lui-même.
    """

    def test_une_validation_django_hors_serialiseur_dit_pourquoi(self) -> None:
        from django.core.exceptions import ValidationError as DjangoValidationError

        reponse = problem_detail_handler(
            DjangoValidationError("Permissions inconnues : orders.refunds."), {}
        )

        assert reponse is not None
        assert reponse.data["detail"] == "Permissions inconnues : orders.refunds."
        assert reponse.data["errors"] == {
            "non_field_errors": ["Permissions inconnues : orders.refunds."]
        }

    def test_une_validation_par_champ_reste_par_champ(self) -> None:
        from django.core.exceptions import ValidationError as DjangoValidationError

        reponse = problem_detail_handler(DjangoValidationError({"email": ["Déjà pris."]}), {})

        assert reponse is not None
        assert reponse.data["errors"] == {"email": ["Déjà pris."]}

    def test_une_liste_d_erreurs_drf_devient_une_phrase(self) -> None:
        from rest_framework.exceptions import ValidationError

        reponse = problem_detail_handler(ValidationError(["Le panier est vide."]), {})

        assert reponse is not None
        assert reponse.data["detail"] == "Le panier est vide."
        assert reponse.data["errors"] == {"non_field_errors": ["Le panier est vide."]}

    def test_un_refus_et_un_absent_se_disent(self) -> None:
        from django.core.exceptions import PermissionDenied
        from django.http import Http404

        refus = problem_detail_handler(PermissionDenied(), {})
        absent = problem_detail_handler(Http404(), {})

        assert refus is not None and absent is not None
        assert refus.data["detail"]
        assert absent.data["detail"]
