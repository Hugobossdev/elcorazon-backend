"""Contrats du suivi — ADR-009, invariant L3.

Le sérialiseur d'entrée ne porte **ni** la course **ni** le livreur : la
première vient de l'URL, le second du jeton. Un relevé dont l'émetteur serait
un champ du corps serait exactement la faille qu'on ferme — n'importe qui
écrirait le suivi de n'importe qui.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.tracking.models import LocationPing
from common.serializers import LocationField

__all__ = [
    "ChatMessageSerializer",
    "LocationPingSerializer",
    "PingWriteSerializer",
    "TrackingSerializer",
]


class LocationPingSerializer(serializers.ModelSerializer[LocationPing]):
    point = LocationField(read_only=True)

    class Meta:
        model = LocationPing
        fields = [
            "id",
            "point",
            "accuracy_m",
            "speed_mps",
            "heading_deg",
            "recorded_at",
            "received_at",
        ]
        read_only_fields = fields


class PingWriteSerializer(serializers.Serializer[Any]):
    """Relevé émis par l'application du livreur.

    `recorded_at` est l'horodatage **de l'appareil**, distinct de la réception.
    Un livreur qui traverse une zone sans réseau émet en différé : sans cette
    distinction, une rafale de relevés rattrapés dessinerait un trajet
    instantané, et l'ETA calculé dessus serait absurde.
    """

    point = LocationField()
    recorded_at = serializers.DateTimeField()
    accuracy_m = serializers.FloatField(required=False, allow_null=True, default=None)
    speed_mps = serializers.FloatField(required=False, allow_null=True, default=None, min_value=0)
    heading_deg = serializers.FloatField(
        required=False, allow_null=True, default=None, min_value=0, max_value=359.999
    )


class ChatMessageSerializer(serializers.Serializer[Any]):
    """Message échangé sur `ws/orders/{order_id}/chat/`.

    Le chat n'est pas persisté (Phase 1 §5) : ce sérialiseur valide la forme
    du message avant relais, il ne porte ni modèle ni écriture en base.
    L'émetteur n'est **pas** un champ du corps — comme pour le suivi de
    position, il vient de la connexion authentifiée, jamais d'une valeur
    fournie par le client.
    """

    text = serializers.CharField(max_length=2000, allow_blank=False, trim_whitespace=True)


class TrackingSerializer(serializers.Serializer[Any]):
    """Suivi rendu au client d'une commande.

    Volontairement pauvre : la dernière position, le statut de la course et le
    livreur réduit à ce qu'on montre à la porte. Ni l'historique complet du
    trajet, ni l'identité étendue du livreur — le client suit son repas, il ne
    surveille pas un employé.
    """

    order = serializers.UUIDField(read_only=True)
    assignment_status = serializers.CharField(read_only=True)
    courier = serializers.DictField(read_only=True)
    last_position = LocationPingSerializer(read_only=True, allow_null=True)
    estimated_delivery_at = serializers.DateTimeField(read_only=True, allow_null=True)
