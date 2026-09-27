"""Contrats de la livraison — ADR-009, invariants L1 et L5.

Aucun sérialiseur d'entrée ne porte `verification_status`, `deliveries_completed`
ni `total_earnings`. Un livreur qui pourrait écrire son propre statut de dossier
se validerait lui-même ; un livreur qui pourrait écrire ses compteurs se
paierait. Ces champs n'existent pas en écriture — il n'y a donc rien à valider.
"""

from __future__ import annotations

from datetime import time
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.accounts.models import User, UserType, phone_validator
from apps.delivery.models import (
    Assignment,
    CourierProfile,
    CourierRating,
    CourierShift,
    VehicleType,
)
from apps.delivery.states import DELIVERY_MACHINE, ENGAGED_STATUSES, VERIFICATION_MACHINE
from apps.geography.models import DeliveryZone
from apps.restaurants.models import Restaurant
from common.money import Money
from common.serializers import LocationField, MoneyField

__all__ = [
    "AssignmentSerializer",
    "CourierApplicationAcceptedSerializer",
    "CourierProfileSerializer",
    "CourierProvisioningSerializer",
    "CourierPublicSerializer",
    "CourierRatingSerializer",
    "CourierRatingWriteSerializer",
    "CourierSelfApplicationSerializer",
    "CourierSelfUpdateSerializer",
    "CourierShiftSerializer",
    "CourierUpdateSerializer",
    "DeclineSerializer",
    "DeliveryTransitionSerializer",
    "DocumentsSerializer",
    "OfferSerializer",
    "OnlineSerializer",
    "VerificationSerializer",
]


class CourierPublicSerializer(serializers.ModelSerializer[CourierProfile]):
    """Ce qu'un client peut voir du livreur qui lui apporte sa commande.

    Prénom, véhicule, note — de quoi le reconnaître à la porte. Ni téléphone
    personnel, ni pièces d'identité, ni position hors course : suivre son
    livreur pendant sa livraison est un service, le suivre ensuite est une
    filature.
    """

    full_name = serializers.CharField(source="user.full_name", read_only=True)
    avatar = serializers.ImageField(source="user.avatar", read_only=True)

    class Meta:
        model = CourierProfile
        fields = ["id", "full_name", "avatar", "vehicle_type", "rating_average", "rating_count"]
        read_only_fields = fields


class CourierProfileSerializer(serializers.ModelSerializer[CourierProfile]):
    """Dossier complet — lisible par son titulaire et par le personnel.

    Les trois pièces justificatives y figurent, en **lecture seule** : c'est ce
    que l'écran de validation doit montrer avant de trancher, et l'instruire
    sans les voir n'aurait pas de sens. Elles sortent en URL signées, qui
    expirent — le stockage est privé. L'implémentation précédente les déposait
    dans un compartiment **public** (`getPublicUrl`) : une pièce d'identité y
    restait lisible par quiconque connaissait l'adresse, indéfiniment.

    Elles ne s'écrivent pas ici : c'est le livreur qui dépose ses pièces, depuis
    son application (`DocumentsSerializer`), et tout dépôt repasse le dossier en
    attente (L5).
    """

    full_name = serializers.CharField(source="user.full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    # Le téléphone du livreur, pour le personnel qui doit le joindre quand une
    # course coince. Il manquait, et son absence se voyait à l'écran : le
    # formulaire de correction ne pouvait pas préremplir le champ qu'il
    # proposait de modifier.
    #
    # Il n'apparaît **pas** dans `CourierPublicSerializer`, qui est ce qu'un
    # client voit de son livreur : le joindre pendant la course passe par le
    # canal d'appel (`apps.calls`), qui ne divulgue aucun numéro personnel.
    phone = serializers.CharField(source="user.phone", read_only=True)
    restaurant = serializers.SlugRelatedField[Restaurant](slug_field="slug", read_only=True)
    last_location = LocationField(read_only=True)
    total_earnings = MoneyField(read_only=True)
    can_accept_orders = serializers.BooleanField(read_only=True)
    # Les zones où il roule. Vide : toutes celles de sa cuisine — voir
    # `CourierProfile.service_zones`.
    service_zones = serializers.SerializerMethodField()

    #: Distance jusqu'à la cuisine, en mètres — présente **seulement** là où la
    #: requête l'annote, c'est-à-dire sur `/delivery/couriers/available/{order}/`
    #: (`CourierService.available_for`, tri PostGIS depuis le restaurant).
    #: Ailleurs, elle vaut `null` : inventer un zéro laisserait croire que le
    #: livreur est sur le pas de la porte.
    distance_m = serializers.SerializerMethodField()

    class Meta:
        model = CourierProfile
        fields = [
            "id",
            "full_name",
            "email",
            "phone",
            "restaurant",
            "service_zones",
            "verification_status",
            "verification_notes",
            "verified_at",
            # Les deux numéros portés au dossier. Ils s'écrivaient déjà — le
            # back-office les saisit à l'embauche (`CourierProvisioningSerializer`)
            # et les corrige ensuite (`CourierUpdateSerializer`) — mais aucune
            # réponse ne les rendait : ni le livreur ni le personnel ne pouvaient
            # relire ce qui avait été enregistré, et le formulaire de correction
            # ne savait pas préremplir les champs qu'il proposait de corriger.
            "national_id_number",
            "licence_number",
            "id_document",
            "licence_document",
            "vehicle_document",
            "id_document_expires_on",
            "licence_document_expires_on",
            "vehicle_document_expires_on",
            "vehicle_type",
            "vehicle_plate",
            "is_online",
            "can_accept_orders",
            "last_location",
            "last_location_at",
            "deliveries_completed",
            "deliveries_cancelled",
            "distance_m",
            "rating_average",
            "rating_count",
            "total_earnings",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field(
        serializers.ListField(child=serializers.DictField(), help_text="`id` et `name`.")
    )
    def get_distance_m(self, obj: CourierProfile) -> int | None:
        """Mètres jusqu'à la cuisine, ou `null` hors de la route des éligibles."""
        distance = getattr(obj, "to_restaurant", None)
        return None if distance is None else round(distance.m)

    def get_service_zones(self, obj: CourierProfile) -> list[dict[str, str]]:
        return [{"id": str(zone.pk), "name": zone.name} for zone in obj.service_zones.all()]


class CourierZonesSerializer(serializers.Serializer[Any]):
    """Corps de `POST /delivery/couriers/{id}/zones/` — la liste **entière**.

    Remplace, n'ajoute pas : l'écran coche des zones et envoie ce qu'il voit.
    Une liste vide lève la restriction.
    """

    zones = serializers.PrimaryKeyRelatedField[DeliveryZone](
        queryset=DeliveryZone.objects.select_related("city"), many=True, allow_empty=True
    )


class CourierProvisioningSerializer(serializers.Serializer[Any]):
    """Embauche d'un livreur : le compte et le dossier, en une requête.

    Deux écrans pour ce qui est un seul geste — créer le compte ici, ouvrir le
    dossier là — laisserait régulièrement des comptes de type livreur sans
    dossier, c'est-à-dire des gens qui se connectent à l'application et n'y
    trouvent rien.

    Les pièces justificatives ne sont **pas** ici : c'est le livreur qui les
    dépose, depuis son application (`POST /delivery/me/`), et c'est bien lui qui
    les a. Les numéros, eux, se relèvent d'une carte présentée à l'embauche —
    ils sont donc facultatifs mais acceptés.

    Conforme à la promesse du module : aucun champ de statut de dossier ni de
    compteur en entrée. Le dossier naît en attente, les compteurs à zéro.
    """

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    full_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")
    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug",
        queryset=Restaurant.objects.all(),
        help_text="Établissement de rattachement — il doit être dans votre périmètre.",
    )
    vehicle_type = serializers.ChoiceField(choices=VehicleType.choices)
    vehicle_plate = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default=""
    )
    national_id_number = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )
    licence_number = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )

    def validate_email(self, value: str) -> str:
        """Normalisée et unique, comme à l'inscription.

        La normalisation n'est pas cosmétique : l'adresse est l'identifiant de
        connexion, et deux comptes ne différant que par la casse rendraient
        l'un des deux inaccessible.
        """
        normalized = value.strip().lower()
        if User.objects.filter(email__iexact=normalized).exists():
            raise serializers.ValidationError("Un compte existe déjà avec cette adresse.")
        return normalized

    def validate_phone(self, value: str) -> str:
        if value and User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("Un compte existe déjà avec ce numéro.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Valide le mot de passe **en connaissant l'identité** du futur compte.

        Ici et non dans un validateur de champ, où c'est pourtant plus court :
        le validateur de similarité de Django compare le mot de passe aux
        attributs de l'utilisateur, et sans utilisateur à lui donner il ne
        compare rien. C'est exactement le contrôle qui manque quand un tiers
        choisit un mot de passe pour quelqu'un d'autre — « livreur123 » pour
        `nouveau.livreur@…` est le cas nominal, pas le cas tordu.
        """
        futur = User(
            email=attrs["email"],
            full_name=attrs["full_name"],
            phone=attrs.get("phone") or "",
        )
        try:
            validate_password(attrs["password"], user=futur)
        except DjangoValidationError as erreur:
            # Rattachée au champ : levée telle quelle, elle atterrirait dans
            # `non_field_errors`, où le formulaire ne l'affiche pas.
            raise serializers.ValidationError({"password": list(erreur.messages)}) from erreur
        return attrs


class CourierSelfApplicationSerializer(CourierProvisioningSerializer):
    """Candidature déposée par le livreur lui-même — `POST /delivery/apply/`.

    Le même formulaire que l'embauche, à trois différences près, et chacune
    tient au fait que **personne du personnel n'est là** pour relire la saisie.

    * **Le téléphone est obligatoire et au format international.** À l'embauche
      il est relevé d'une carte présentée, et le back-office corrige une
      coquille en deux clics ; ici il est tapé par un candidat, et c'est le seul
      moyen de le joindre pour instruire son dossier. `phone_validator` est donc
      opposé à la saisie, alors que le modèle ne l'applique jamais de lui-même
      — `save()` n'appelle pas `full_clean()`.
    * **L'établissement doit être ouvert au public.** La liste où le candidat
      choisit est celle de `GET /restaurants/`, qui ne rend que les
      établissements actifs ; accepter les autres permettrait de se rattacher,
      en tapant un slug, à une adresse fermée ou pas encore ouverte.
    * **Aucun jeton n'en sort.** Voir `CourierApplicationView`.

    Ce qui **ne** change **pas** : le dossier naît `pending`, les compteurs à
    zéro, et aucun champ de statut n'est accepté en entrée. Un candidat ne se
    valide pas lui-même — c'est l'invariant L1, et il est intact.
    """

    phone = serializers.CharField(max_length=16, validators=[phone_validator])
    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug",
        queryset=Restaurant.objects.filter(is_active=True),
        help_text="Établissement auquel vous vous rattachez, par son slug (voir /restaurants/).",
    )


class CourierApplicationAcceptedSerializer(serializers.Serializer[Any]):
    """Accusé de dépôt d'une candidature.

    Volontairement pauvre. Il ne rend ni jeton, ni dossier : le candidat n'a
    pas encore de session, et rien de ce qu'il vient d'écrire ne lui apprendrait
    quoi que ce soit. Ce qu'il lui faut tient en trois choses — où le code est
    parti, combien de temps il vaut, et à partir de quand un renvoi est
    possible — plus l'état du dossier, pour que l'écran suivant dise la vérité
    sur ce qui reste à faire.
    """

    email = serializers.EmailField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)
    retry_after = serializers.IntegerField(read_only=True)
    code_length = serializers.IntegerField(read_only=True)
    verification_status = serializers.CharField(read_only=True)
    detail = serializers.CharField(read_only=True)


class AssignmentSerializer(serializers.ModelSerializer[Assignment]):
    """Course, vue par le livreur ou par le personnel."""

    order_reference = serializers.CharField(source="order.reference", read_only=True)
    restaurant_name = serializers.CharField(source="order.restaurant.name", read_only=True)
    pickup_location = LocationField(source="order.restaurant.location", read_only=True)
    delivery_address_line = serializers.CharField(
        source="order.delivery_address_line", read_only=True
    )
    delivery_landmark = serializers.CharField(source="order.delivery_landmark", read_only=True)
    delivery_location = serializers.JSONField(source="order.delivery_location", read_only=True)
    recipient_name = serializers.CharField(source="order.recipient_name", read_only=True)
    recipient_phone = serializers.SerializerMethodField()
    # Ce que le livreur doit savoir pour livrer, et qui manquait : la consigne
    # laissée par le client (« sonnez deux fois, portail bleu »), la zone et la
    # ville où il roule, et **ce qu'il doit encaisser**. Une commande payée en
    # espèces partait sans montant : le livreur l'apprenait du client, sur le
    # pas de la porte.
    delivery_instructions = serializers.CharField(
        source="order.delivery_instructions", read_only=True
    )
    delivery_zone_name = serializers.CharField(source="order.delivery_zone_name", read_only=True)
    city_name = serializers.CharField(
        source="order.city.name", read_only=True, allow_null=True, default=None
    )
    payment_method = serializers.CharField(source="order.payment_method", read_only=True)
    # L'étape de la **commande**, que l'étape de la course ne dit pas.
    #
    # Sans elle, l'application du livreur ne peut pas savoir si la cuisine a
    # fini : `allowed_transitions` est calculé sur la seule machine de la
    # course, si bien que « J'ai récupéré la commande » s'affichait dès
    # l'acceptation. Le serveur refuse désormais ce geste
    # (`AssignmentService._exiger_une_commande_qui_suit`) ; ce champ permet à
    # l'écran de ne pas le proposer, plutôt que de le faire échouer.
    order_status = serializers.CharField(source="order.status", read_only=True)
    order_total = MoneyField(source="order.total", read_only=True)
    estimated_delivery_at = serializers.DateTimeField(
        source="order.estimated_delivery_at", read_only=True, allow_null=True
    )
    amount_to_collect = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()
    courier = CourierPublicSerializer(read_only=True)
    # **Nulle tant que la course est seulement proposée** : la rémunération est
    # figée à l'acceptation. Le schéma la déclarait non nulle, alors que le
    # serveur rend bien `null` sur une offre — un client généré depuis ce
    # contrat aurait planté à la première proposition reçue. Relevé par le test
    # de contrat, pas à l'exécution : l'application Dart, elle, la lisait déjà
    # comme facultative.
    courier_fee = MoneyField(read_only=True, allow_null=True)
    allowed_transitions = serializers.SerializerMethodField()
    # **Qu'il y a** une preuve, jamais où elle est : la photo montre une porte,
    # une adresse, parfois une personne, et sert à trancher un litige — elle
    # reste sur le stockage privé, hors de portée de l'application.
    has_proof_of_delivery = serializers.SerializerMethodField()

    class Meta:
        model = Assignment
        fields = [
            "id",
            "order",
            "order_reference",
            "restaurant_name",
            "pickup_location",
            "delivery_address_line",
            "delivery_landmark",
            "delivery_location",
            "recipient_name",
            "recipient_phone",
            "delivery_instructions",
            "delivery_zone_name",
            "city_name",
            "payment_method",
            "order_status",
            "order_total",
            "estimated_delivery_at",
            "amount_to_collect",
            "items",
            "courier",
            "status",
            "allowed_transitions",
            "courier_fee",
            "has_proof_of_delivery",
            "offered_at",
            "accepted_at",
            "picked_up_at",
            "delivered_at",
            "decline_reason",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_allowed_transitions(self, obj: Assignment) -> list[str]:
        return sorted(DELIVERY_MACHINE.targets_from(obj.status))

    def get_has_proof_of_delivery(self, obj: Assignment) -> bool:
        return bool(obj.proof_of_delivery)

    def get_recipient_phone(self, obj: Assignment) -> str:
        """Le numéro du destinataire — pour le livreur, **tant qu'il porte la course**.

        Proposée, la course peut être refusée : le livreur qui décline n'a pas à
        repartir avec le numéro d'un client qu'il ne livrera pas. Terminée, elle
        ne demande plus d'appeler personne. Le numéro lui est rendu vide dans
        ces deux cas — l'adresse et le quartier, eux, restent lisibles, parce
        qu'ils servent à décider d'accepter.

        Le personnel, qui supervise la même course depuis le back-office
        (`ManagedAssignmentViewSet`), le lit toujours : c'est lui qui rappelle
        le client quand une livraison coince.
        """
        requete = self.context.get("request")
        lecteur = getattr(requete, "user", None)
        if getattr(lecteur, "user_type", None) == UserType.STAFF:
            return obj.order.recipient_phone
        return obj.order.recipient_phone if obj.status in ENGAGED_STATUSES else ""

    @extend_schema_field(MoneyField(allow_null=True))
    def get_amount_to_collect(self, obj: Assignment) -> dict[str, Any] | None:
        """Ce qu'il reste à encaisser à la porte — nul s'il n'y a plus rien.

        ## Ce que cette méthode répondait

        `payment_method != cash → rien à encaisser`. C'est-à-dire : le **moyen**
        de paiement tenait lieu d'**état** du paiement. Une commande en mobile
        money dont le règlement n'a pas abouti — prestataire qui refuse, client
        qui abandonne à l'écran de paiement, puis confirmation à la main depuis
        le back-office — s'affichait donc « Déjà réglée. Vous n'avez rien à
        encaisser », et le livreur repartait sans son argent. Symétriquement,
        une commande réglée pour moitié n'annonçait que le total.

        La question n'est pas « comment le client comptait-il payer ? » mais
        « combien reste-t-il dû ? ». C'est une soustraction, et `Order.amount_paid`
        en donne le second terme : `payments` l'y reporte à chaque encaissement
        et à chaque remboursement soldé (`report_settled_total`), parce que
        `delivery` n'a pas le droit de lire `payments` (ADR-002).

        Nul veut donc dire « rien à réclamer », quel que soit le moyen annoncé.
        """
        regle = obj.order.amount_paid or Money.zero(obj.order.total.currency)
        reste = obj.order.total - regle
        if reste.amount_minor <= 0:
            return None
        return MoneyField().to_representation(reste)

    @extend_schema_field(
        serializers.ListField(
            child=serializers.DictField(), help_text="Nom, image, quantité, options."
        )
    )
    def get_items(self, obj: Assignment) -> list[dict[str, Any]]:
        """Ce qu'il y a dans le sac, pour le vérifier au retrait — **sans les prix**."""
        return [
            {
                "name": ligne.item_name,
                "item_image": ligne.item_image,
                "quantity": ligne.quantity,
                "options": [option.get("option", "") for option in ligne.options],
                "notes": ligne.notes,
            }
            for ligne in obj.order.lines.all()
        ]


class OfferSerializer(serializers.Serializer[Any]):
    courier = serializers.PrimaryKeyRelatedField[CourierProfile](
        queryset=CourierProfile.objects.all()
    )


class DeclineSerializer(serializers.Serializer[Any]):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class DeliveryTransitionSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=sorted(DELIVERY_MACHINE.states))
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class VerificationSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=sorted(VERIFICATION_MACHINE.states))
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    # Dates relevées sur les pièces, au moment de les instruire. Facultatives :
    # seules celles fournies sont écrites.
    id_document_expires_on = serializers.DateField(required=False)
    licence_document_expires_on = serializers.DateField(required=False)
    vehicle_document_expires_on = serializers.DateField(required=False)


class OnlineSerializer(serializers.Serializer[Any]):
    is_online = serializers.BooleanField()


class ProofOfDeliverySerializer(serializers.Serializer[Any]):
    """La photo prise à la remise. `ImageField` et non `FileField` : Pillow
    relit le fichier, et un document qui n'est pas une image est refusé ici."""

    photo = serializers.ImageField()


class DocumentsSerializer(serializers.Serializer[Any]):
    """Dépôt de pièces justificatives.

    Toutes facultatives : on remplace ce qu'on remplace. Mais déposer **une**
    pièce suffit à repasser le dossier en attente (L5) — un dossier validé sur
    des documents qu'on a ensuite changés n'est plus validé.
    """

    id_document = serializers.FileField(required=False)
    licence_document = serializers.FileField(required=False)
    vehicle_document = serializers.FileField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs:
            raise serializers.ValidationError("Aucune pièce fournie.")
        return attrs


class CourierRatingSerializer(serializers.ModelSerializer[CourierRating]):
    """Une note telle qu'on la relit — pour savoir si la course est déjà notée."""

    courier = serializers.UUIDField(source="assignment.courier_id", read_only=True)
    order = serializers.UUIDField(source="assignment.order_id", read_only=True)

    class Meta:
        model = CourierRating
        fields = ["id", "order", "courier", "score", "comment", "created_at"]
        read_only_fields = fields


class CourierRatingWriteSerializer(serializers.Serializer[Any]):
    """Les deux seuls champs qu'une note accepte.

    Ni le livreur ni la course : ils se déduisent de la commande citée dans
    l'URL. Les accepter du client permettrait de noter le livreur d'un autre.
    """

    score = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class CourierShiftSerializer(serializers.ModelSerializer[CourierShift]):
    """Créneau planifié — indicatif, jamais opposable (voir `backoffice.py`).

    `courier_name` est rendu à côté de l'identifiant : un planning se lit par
    nom, et l'écran aurait sinon à charger la flotte entière pour afficher une
    ligne.
    """

    courier_name = serializers.CharField(source="courier.user.full_name", read_only=True)

    class Meta:
        model = CourierShift
        fields = [
            "id",
            "courier",
            "courier_name",
            "day_of_week",
            "start_time",
            "end_time",
            "is_available",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "courier_name", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Traduit en 400 ce que la contrainte `CHECK` refuserait en 500.

        Un créneau à cheval sur minuit n'est pas accepté : il s'écrit en deux
        lignes, sur deux jours, ce qui reste juste et se trie. L'accepter
        obligerait chaque lecture du planning à traiter le cas « fin < début »
        comme un débordement, et une seule oubliée afficherait une barre de
        longueur négative.
        """
        instance = self.instance
        debut = attrs.get("start_time") or (instance.start_time if instance else None)
        fin = attrs.get("end_time") or (instance.end_time if instance else None)

        if debut is not None and fin is not None and fin <= debut:
            raise serializers.ValidationError(
                {
                    "end_time": (
                        "La fin doit suivre le début. Un créneau qui passe minuit "
                        "s'écrit en deux lignes, sur deux jours."
                    )
                }
            )

        self._refuser_le_chevauchement(attrs, debut=debut, fin=fin)
        return attrs

    def _refuser_le_chevauchement(
        self, attrs: dict[str, Any], *, debut: time | None, fin: time | None
    ) -> None:
        """Deux créneaux d'un même livreur ne se recouvrent pas le même jour.

        `courier_shift_unique_start` ne refusait que deux créneaux commençant à
        la **même** minute. « Lundi 9 h – 17 h » et « lundi 12 h – 20 h »
        passaient donc tous les deux : le planning affichait le livreur attendu
        deux fois à midi, et l'exploitation lisait une amplitude fausse — elle
        comptait huit heures plus huit heures pour onze heures de présence.

        Ce n'est pas une contrainte de base : elle demande de comparer chaque
        ligne aux autres, ce qu'un `CHECK` ne sait pas faire. Le refus est donc
        ici, et il est explicite — il nomme le créneau qui gêne, sans quoi
        l'exploitation corrigerait au hasard.
        """
        instance = self.instance
        livreur = attrs.get("courier") or (instance.courier if instance else None)
        jour = attrs.get("day_of_week", instance.day_of_week if instance else None)
        if livreur is None or jour is None or debut is None or fin is None:
            return

        voisins = CourierShift.objects.filter(courier=livreur, day_of_week=jour)
        if instance is not None:
            voisins = voisins.exclude(pk=instance.pk)
        # Deux intervalles se recouvrent si chacun commence avant que l'autre
        # finisse. Bornes ouvertes : 9 h – 12 h et 12 h – 18 h s'enchaînent.
        chevauchant = voisins.filter(start_time__lt=fin, end_time__gt=debut).first()
        if chevauchant is None:
            return

        raise serializers.ValidationError(
            {
                "start_time": (
                    f"Ce livreur est déjà planifié de {chevauchant.start_time:%H:%M} "
                    f"à {chevauchant.end_time:%H:%M} ce jour-là. Ajustez ce créneau, "
                    "ou supprimez celui qui le recouvre."
                )
            }
        )


class CourierSelfUpdateSerializer(serializers.Serializer[Any]):
    """Correction de son **propre** dossier par le livreur — `PATCH /delivery/me/`.

    ## Ce que le livreur corrige lui-même, et pourquoi c'est peu

    Quatre champs, tous descriptifs : le véhicule qu'il conduit, sa plaque, et
    les deux numéros que portent ses pièces. Ce sont exactement les valeurs
    qu'il est seul à connaître et que personne d'autre n'a de raison de
    ressaisir — un livreur qui passe du scooter à la voiture le sait avant le
    back-office, et l'obliger à téléphoner pour faire changer une lettre de sa
    plaque produit un dossier faux plutôt qu'un appel.

    Tout le reste lui est fermé, et la liste blanche est ici plutôt que dans la
    vue : un champ oublié dans une liste noire s'écrit, un champ oublié dans une
    liste blanche ne s'écrit pas. En particulier `verification_status` —
    l'invariant L1 tout entier tient à ce qu'un livreur ne puisse pas l'écrire,
    faute de quoi il validerait son propre recrutement — et les compteurs, qui
    sont des agrégats de faits et non des déclarations.

    ## Ce que corriger un numéro **ne fait pas**

    Le dossier n'est pas remis en instruction. Seul le dépôt d'une **pièce**
    rouvre l'examen (L5, `CourierService.replace_documents`) : c'est la pièce
    qu'un instructeur lit, pas le champ texte à côté. Remettre un dossier en
    attente parce qu'une plaque a perdu un tiret suspendrait un livreur en
    pleine tournée.

    Le nom et le téléphone ne sont **pas** ici : ils appartiennent au compte, et
    `PATCH /auth/me/` les porte déjà pour tous les types de comptes. Les
    dédoubler ici donnerait deux routes pour un même geste, qui divergeraient.
    """

    vehicle_type = serializers.ChoiceField(choices=VehicleType.choices, required=False)
    vehicle_plate = serializers.CharField(max_length=32, required=False, allow_blank=True)
    national_id_number = serializers.CharField(max_length=64, required=False, allow_blank=True)
    licence_number = serializers.CharField(max_length=64, required=False, allow_blank=True)

    #: Énumérés dans le refus d'un corps vide. Le message dit ce qui *est*
    #: accepté plutôt que ce qui ne l'est pas : l'appelant cherche le nom du
    #: champ qu'il aurait dû envoyer.
    CHAMPS_ACCEPTES = "type de véhicule, plaque, numéro de pièce d'identité, numéro de permis"

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Refuse un corps vide.

        DRF l'accepterait et rendrait 200 sans rien écrire — l'écran
        annoncerait « modifications enregistrées » sur une requête qui n'a rien
        enregistré.
        """
        if not attrs:
            raise serializers.ValidationError(
                f"Aucun champ modifiable dans cette requête. Les champs acceptés "
                f"sont : {self.CHAMPS_ACCEPTES}."
            )
        return attrs

    def update(self, instance: CourierProfile, validated_data: dict[str, Any]) -> CourierProfile:
        """Écrit le dossier, **dans une seule transaction**.

        Elle ne sert à rien tant qu'un seul objet est touché ; elle sert dès que
        la sous-classe y ajoute le compte, et la placer ici évite qu'on oublie
        de l'ouvrir là-bas.
        """
        with transaction.atomic():
            self._ecrire(instance, validated_data)

        instance.refresh_from_db()
        return instance

    def _ecrire(self, instance: CourierProfile, validated_data: dict[str, Any]) -> None:
        """Le geste d'écriture, isolé pour que la sous-classe s'y greffe."""
        for champ, valeur in validated_data.items():
            setattr(instance, champ, valeur)
        if validated_data:
            instance.save(update_fields=[*validated_data, "updated_at"])


class CourierUpdateSerializer(CourierSelfUpdateSerializer):
    """Correction d'un dossier livreur par le **personnel** — `PATCH`.

    Le dossier de [`CourierSelfUpdateSerializer`], plus les deux champs du
    compte qu'un back-office a de bonnes raisons de rectifier : un nom mal
    orthographié à l'embauche, un numéro de téléphone qui change.

    Ce qui reste **hors d'atteinte**, et pourquoi :

    * `verification_status`, `verified_by`, `verified_at` — instruire un
      dossier passe par `verification/`, qui exige `couriers.approve` ou
      `couriers.suspend` et journalise l'auteur. Les laisser ici ferait de
      `couriers.write` un droit de valider son propre recrutement ;
    * les **pièces justificatives** — c'est le livreur qui les dépose, depuis
      son application, et tout dépôt repasse le dossier en attente (L5) ;
    * `is_online` — c'est une déclaration du livreur, qui sait s'il roule. Le
      personnel qui le mettrait « en ligne » à distance le rendrait éligible à
      des courses qu'il ne verrait pas ;
    * les **compteurs** (`deliveries_*`, `rating_*`, gains) — ce sont des
      agrégats de faits, tenus par le serveur. Les rendre modifiables
      permettrait de fabriquer une réputation ;
    * `restaurant` — muter un livreur d'un établissement à l'autre le fait
      sortir du périmètre de qui le mute, qui perd alors le dossier de vue au
      milieu du geste. C'est un transfert, il mérite sa propre route et sa
      propre garde ;
    * `email` — c'est l'identifiant du compte, donc un chemin de reprise par
      « mot de passe oublié ». Il ne se change pas depuis l'écran d'un tiers.

    Le statut de vérification n'est **pas** remis en attente quand la plaque ou
    le type de véhicule changent, et c'est délibéré : la correction d'une
    coquille suspendrait un livreur en pleine tournée. Un vrai changement de
    véhicule s'accompagne d'une nouvelle carte grise, et c'est ce dépôt-là qui
    rouvre l'instruction (L5).
    """

    full_name = serializers.CharField(max_length=150, required=False)
    phone = serializers.CharField(
        max_length=16,
        required=False,
        allow_blank=True,
        validators=[phone_validator],
    )

    #: Champs portés par le compte plutôt que par le dossier. La distinction
    #: n'a pas à remonter jusqu'à l'appelant : il corrige « le livreur ».
    CHAMPS_DU_COMPTE = ("full_name", "phone")

    CHAMPS_ACCEPTES = f"nom, téléphone, {CourierSelfUpdateSerializer.CHAMPS_ACCEPTES}"

    def validate_phone(self, value: str) -> str:
        """Unique sur les comptes, comme à l'inscription.

        Vérifié ici pour rendre 400 avec un message lisible : la contrainte
        d'unicité de la base sortirait en 500. Le numéro vide est permis — il
        signifie « non renseigné » et n'entre pas dans l'unicité.
        """
        numero = value.strip()
        if not numero:
            return ""

        occupe = User.objects.filter(phone=numero)
        if self.instance is not None:
            occupe = occupe.exclude(pk=self.instance.user_id)
        if occupe.exists():
            raise serializers.ValidationError("Ce numéro est déjà associé à un autre compte.")
        return numero

    def _ecrire(self, instance: CourierProfile, validated_data: dict[str, Any]) -> None:
        """Écrit le compte, puis le dossier — sous la transaction ouverte par la
        classe de base.

        Sans elle, un numéro de téléphone refusé par la base après qu'une
        plaque a été écrite laisserait le dossier à moitié corrigé, sans que
        l'appelant sache laquelle des deux moitiés a pris.
        """
        compte = {
            champ: validated_data.pop(champ)
            for champ in self.CHAMPS_DU_COMPTE
            if champ in validated_data
        }

        if compte:
            for champ, valeur in compte.items():
                # Un téléphone vide vaut `NULL` et non `""` : la colonne est
                # `unique`, et deux chaînes vides s'y heurteraient alors que
                # deux `NULL` cohabitent.
                setattr(
                    instance.user,
                    champ,
                    None if champ == "phone" and not valeur else valeur,
                )
            instance.user.save(update_fields=[*compte, "updated_at"])

        super()._ecrire(instance, validated_data)


class PeriodEarningsSerializer(serializers.Serializer[Any]):
    """Ce qu'une période a rapporté, et combien de courses l'ont produite."""

    earned = MoneyField(read_only=True)
    deliveries = serializers.IntegerField(read_only=True)


class EarningsSerializer(serializers.Serializer[Any]):
    """Gains du livreur, agrégés côté serveur — `GET /delivery/me/earnings/`.

    Les quatre périodes sont rendues ensemble : l'écran les présente en onglets,
    et trois appels pour trois onglets feraient trois fois le même travail.

    `lifetime` vient du dossier plutôt que d'une somme : c'est le compteur que
    `_credit` tient à chaque livraison, et le solde sur lequel un retrait
    s'apprécie.
    """

    today = PeriodEarningsSerializer(read_only=True)
    week = PeriodEarningsSerializer(read_only=True)
    month = PeriodEarningsSerializer(read_only=True)
    lifetime = PeriodEarningsSerializer(read_only=True)
