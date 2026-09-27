"""Contrats de la signalisation d'appel.

Aucun sérialiseur d'entrée ne porte l'appelant, le destinataire ni le canal :
les deux premiers viennent du jeton et de la commande, le troisième est dérivé
de l'appel. Les accepter en entrée, comme le faisait l'implémentation Supabase,
laisserait faire sonner le téléphone de n'importe qui — ou rejoindre le canal
d'une conversation en cours.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.calls.models import Call, CallKind

__all__ = ["CallPlaceSerializer", "CallSerializer", "RtcCredentialsSerializer"]


class CallSerializer(serializers.ModelSerializer[Call]):
    """Un appel tel que ses deux parties le voient."""

    caller_name = serializers.CharField(source="caller.full_name", read_only=True)
    callee_name = serializers.CharField(source="callee.full_name", read_only=True)
    channel_name = serializers.CharField(read_only=True)

    class Meta:
        model = Call
        fields = [
            "id",
            "order",
            "kind",
            "status",
            "caller",
            "caller_name",
            "callee",
            "callee_name",
            "channel_name",
            "answered_at",
            "ended_at",
            "duration_seconds",
            "created_at",
        ]
        read_only_fields = fields


class CallPlaceSerializer(serializers.Serializer[Any]):
    """Le seul champ qu'un appel accepte : sa nature."""

    kind = serializers.ChoiceField(choices=CallKind.choices, default=CallKind.VOICE)


class RtcCredentialsSerializer(serializers.Serializer[Any]):
    """De quoi rejoindre le canal Agora.

    Le certificat d'application n'y figure évidemment pas : il signe le jeton
    côté serveur et ne quitte jamais celui-ci.
    """

    channel_name = serializers.CharField(read_only=True)
    token = serializers.CharField(read_only=True)
    uid = serializers.IntegerField(read_only=True)
    app_id = serializers.CharField(read_only=True)
    expires_in = serializers.IntegerField(read_only=True)
