"""Contrats des promotions — ADR-009, invariant F4.

Ce module ne décrit **que** la promotion elle-même. La route qui l'évalue vit
dans `orders` et non ici, et ce n'est pas un détail de rangement : l'évaluer
demande de lire un panier et un barème de zone, donc de connaître `carts` et
`geography`. `promotions` est en amont de `orders` dans le graphe de
l'ADR-002 ; lui faire connaître le panier aurait été une dépendance de plus
dans le mauvais sens.

C'est un test d'architecture qui l'a signalé — la première rédaction plaçait
la route ici.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.promotions.models import DiscountKind, Promotion
from apps.restaurants.models import Restaurant
from common.serializers import MoneyField

__all__ = ["ManagedPromotionSerializer", "PromotionSerializer"]


class PromotionSerializer(serializers.ModelSerializer[Promotion]):
    """Ce qu'on montre d'un code.

    Ni `used_count`, ni `usage_limit` : le nombre d'utilisations restantes est
    une information commerciale, et l'exposer inviterait à courir sur les
    derniers coupons — ou à découvrir qu'un code n'a servi à personne.
    """

    amount = MoneyField(read_only=True)
    min_order_amount = MoneyField(read_only=True)
    max_discount = MoneyField(read_only=True)

    class Meta:
        model = Promotion
        fields = [
            "id",
            "code",
            "description",
            "kind",
            "percentage",
            "amount",
            "min_order_amount",
            "max_discount",
            "starts_at",
            "ends_at",
        ]
        read_only_fields = fields


class ManagedPromotionSerializer(serializers.ModelSerializer[Promotion]):
    """Le même code, vu de l'exploitation : conditions et compteurs compris.

    `used_count` y figure — c'est ce qu'on vient consulter — mais en lecture
    seule : il est tenu par `PromotionService`, sous verrou, à la création de
    chaque commande. Le rendre inscriptible permettrait de rouvrir un quota
    épuisé sans que rien n'en garde trace.

    `owner` aussi est en lecture seule. Un code nominatif naît d'un échange de
    points, qui l'a fait payer ; en frapper un depuis le back-office
    distribuerait des récompenses sans débit.
    """

    restaurant = serializers.SlugRelatedField[Restaurant](
        slug_field="slug",
        queryset=Restaurant.objects.all(),
        required=False,
        allow_null=True,
        help_text="Vide = code national, valable dans tous les établissements.",
    )
    owner_email = serializers.EmailField(source="owner.email", read_only=True, default=None)
    amount = MoneyField(required=False, allow_null=True)
    min_order_amount = MoneyField(required=False, allow_null=True)
    max_discount = MoneyField(required=False, allow_null=True)

    class Meta:
        model = Promotion
        fields = [
            "id",
            "code",
            "description",
            "kind",
            "percentage",
            "amount",
            "min_order_amount",
            "max_discount",
            "starts_at",
            "ends_at",
            "usage_limit",
            "usage_limit_per_user",
            "used_count",
            "restaurant",
            "owner_email",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "used_count", "owner_email", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Traduit en 400 ce que la base refuserait en 500.

        Les trois contraintes `CHECK` de `Promotion` sont la dernière ligne de
        défense et le restent : elles valent pour un script, une commande de
        gestion, un import. Ce qu'on ajoute ici, c'est un refus **lisible** —
        « il manque le pourcentage » plutôt qu'une violation d'intégrité, que
        l'exploitation lirait comme une panne du serveur et signalerait comme
        telle.
        """
        instance = self.instance

        def valeur(nom: str) -> Any:
            return attrs.get(nom, getattr(instance, nom, None))

        starts_at, ends_at = valeur("starts_at"), valeur("ends_at")
        if starts_at is not None and ends_at is not None and ends_at <= starts_at:
            raise serializers.ValidationError(
                {"ends_at": "La fin de validité doit suivre le début."}
            )

        kind = valeur("kind")
        if kind == DiscountKind.PERCENTAGE and not valeur("percentage"):
            raise serializers.ValidationError(
                {"percentage": "Un code en pourcentage demande un pourcentage strictement positif."}
            )
        if kind == DiscountKind.FIXED:
            montant = valeur("amount")
            if montant is None or not montant.is_positive:
                raise serializers.ValidationError(
                    {"amount": "Un code à montant fixe demande un montant strictement positif."}
                )

        return attrs
