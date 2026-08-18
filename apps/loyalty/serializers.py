"""Contrats de la fidélité — ADR-009.

**Tout est en lecture seule, et c'est l'essentiel de ce module.** Un point ne
s'obtient qu'en se faisant livrer une commande, ne se dépense qu'en échangeant
une récompense au catalogue. Aucun sérialiseur d'entrée n'accepte de `delta`, de
`balance`, de `points_cost` ni de `discount` : c'est C1 transposé à la fidélité.
Un client qui annoncerait son propre solde serait la même faille que celui qui
annonçait son propre prix.

L'échange n'a donc **aucun corps de requête** : la récompense est désignée par
l'URL, son coût est lu en base, et le solde est celui du jeton. Il n'y a rien
que l'appelant puisse déclarer, donc rien à valider — ce qui vaut mieux qu'une
validation à écrire correctement.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.loyalty.models import (
    PointsAccount,
    PointsEntry,
    Reward,
    RewardKind,
    RewardRedemption,
    Subscription,
    SubscriptionPlan,
)
from apps.promotions.serializers import PromotionSerializer
from common.serializers import MoneyField

__all__ = [
    "ManagedRewardSerializer",
    "PointsAccountSerializer",
    "PointsEntrySerializer",
    "RedemptionResultSerializer",
    "RewardRedemptionSerializer",
    "RewardSerializer",
    "SubscribeRequestSerializer",
    "SubscriptionPlanSerializer",
    "SubscriptionResultSerializer",
    "SubscriptionSerializer",
]


class PointsAccountSerializer(serializers.ModelSerializer[PointsAccount]):
    """Le solde, tel que l'écran de fidélité l'affiche.

    Ni `id` ni horodatages de création : le compte est un singleton par
    utilisateur, dont l'identifiant ne sert à aucune route. L'exposer inviterait
    à le passer en paramètre quelque part, et donc à écrire un jour un
    cloisonnement de plus à tenir.

    Les cumuls de vie (`lifetime_earned`, `lifetime_spent`) sont là parce qu'un
    client qui voit « 120 points » sans savoir combien il en a gagné en tout n'a
    aucun moyen de vérifier que rien n'a disparu.
    """

    class Meta:
        model = PointsAccount
        fields = ["balance", "lifetime_earned", "lifetime_spent", "last_activity_at"]
        read_only_fields = fields


class PointsEntrySerializer(serializers.ModelSerializer[PointsEntry]):
    """Une ligne du journal (F5).

    C'est la pièce qui permet à un client de **contester** son solde : sans le
    détail des mouvements, « vous avez 120 points » est à prendre ou à laisser.
    D'où `balance_after`, qui fige ce que valait le compte à cet instant — un
    écart avec le solde courant devient visible au lieu d'être supposé absent.
    """

    class Meta:
        model = PointsEntry
        fields = ["id", "kind", "delta", "balance_after", "description", "order", "created_at"]
        read_only_fields = fields


class RewardSerializer(serializers.ModelSerializer[Reward]):
    """Une récompense du catalogue.

    `discount` sort en `Money` — `{"amount": "500", "currency": "XOF"}` — et non
    en entier nu : la règle de l'ADR-007 vaut pour tous les montants du projet,
    et une exception ici obligerait le client à traiter ce champ autrement que
    les vingt autres.
    """

    discount = MoneyField(read_only=True)

    class Meta:
        model = Reward
        fields = [
            "id",
            "name",
            "description",
            "kind",
            "points_cost",
            "discount",
            "validity_days",
            "restaurant",
        ]
        read_only_fields = fields


class RewardRedemptionSerializer(serializers.ModelSerializer[RewardRedemption]):
    """Un échange passé.

    `promotion_code` est repris ici plutôt que suivi par une clé étrangère vers
    la promotion : le code est ce que le client recopie dans son panier, et il
    doit rester lisible dans l'historique même si la promotion a expiré et
    qu'elle est purgée du catalogue.
    """

    reward = RewardSerializer(read_only=True)

    class Meta:
        model = RewardRedemption
        fields = ["id", "reward", "points_spent", "promotion_code", "created_at"]
        read_only_fields = fields


class RedemptionResultSerializer(serializers.Serializer[Any]):
    """Réponse d'un échange : ce qui a été acheté, le code, et le solde restant.

    Le solde est renvoyé avec le code pour épargner au client un second appel
    juste après le premier — l'écran qui affiche « voici votre code » affiche le
    nouveau solde à côté, et deux requêtes pour un geste laisseraient une
    fenêtre où l'un des deux nombres est périmé.
    """

    redemption = RewardRedemptionSerializer(read_only=True)
    promotion = PromotionSerializer(read_only=True)
    balance = serializers.IntegerField(read_only=True)


class SubscriptionPlanSerializer(serializers.ModelSerializer[SubscriptionPlan]):
    """Un plan du catalogue — P4 : c'est d'ici, et de nulle part ailleurs, que vient le prix."""

    price = MoneyField(read_only=True)

    class Meta:
        model = SubscriptionPlan
        fields = ["id", "name", "description", "price", "billing_period_days"]
        read_only_fields = fields


class SubscriptionSerializer(serializers.ModelSerializer[Subscription]):
    """Un abonnement — le sien, jamais celui d'un autre (filtre de requête, ADR-005)."""

    plan = SubscriptionPlanSerializer(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            "id",
            "plan",
            "status",
            "auto_renew",
            "current_period_start",
            "current_period_end",
            "cancelled_at",
            "created_at",
        ]
        read_only_fields = fields


class SubscribeRequestSerializer(serializers.Serializer[Any]):
    """Le seul champ qu'une souscription accepte : lequel des plans.

    Aucun prix, aucune période : les accepter du client rouvrirait exactement
    P4, la faille que ce module ferme.
    """

    plan = serializers.PrimaryKeyRelatedField(
        queryset=SubscriptionPlan.objects.filter(is_active=True)
    )


class SubscriptionResultSerializer(serializers.Serializer[Any]):
    """Réponse d'une souscription : l'abonnement ouvert et comment le régler."""

    subscription = SubscriptionSerializer(read_only=True)
    checkout_url = serializers.CharField(read_only=True)
    instructions = serializers.CharField(read_only=True)


class ManagedRewardSerializer(serializers.ModelSerializer[Reward]):
    """Récompense vue du back-office — le catalogue, inactives comprises.

    `discount` s'écrit ici, et c'est le seul endroit : une récompense « remise
    de 500 F » engage l'enseigne à la hauteur de ce montant, comme un prix
    (C1). Il reste un `Money` (ADR-007), pas un entier nu, sans quoi une remise
    saisie en francs finirait un jour comparée à un total en centimes.

    `restaurant` vide crée une récompense **nationale**, échangeable partout.
    """

    discount = MoneyField(required=False, allow_null=True)

    class Meta:
        model = Reward
        fields = [
            "id",
            "name",
            "description",
            "kind",
            "points_cost",
            "discount",
            "validity_days",
            "restaurant",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_points_cost(self, value: int) -> int:
        """Une récompense gratuite se prendrait en boucle.

        La contrainte n'existe pas en base — `PositiveIntegerField` accepte
        zéro — et c'est ici qu'elle a un sens lisible : à zéro point, l'échange
        ne débite rien et la récompense se réclame autant de fois qu'on la
        demande.
        """
        if value < 1:
            raise serializers.ValidationError(
                "Une récompense à zéro point ne débite rien : elle se réclamerait en boucle."
            )
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Une remise sans montant serait refusée par la base, en 500.

        La contrainte `reward_discount_is_set` reste la dernière ligne de
        défense — elle vaut pour un script ou un import — mais le refus lisible
        appartient ici : « il manque le montant » plutôt qu'une violation
        d'intégrité, que l'exploitation lirait comme une panne.
        """
        instance = self.instance
        genre = attrs.get("kind") or (instance.kind if instance else None)
        remise = attrs.get("discount") or (instance.discount if instance else None)

        if genre == RewardKind.DISCOUNT and (remise is None or remise.amount_minor <= 0):
            raise serializers.ValidationError(
                {"discount": "Une récompense de type remise doit porter un montant."}
            )

        return attrs

    def _appliquer_remise(self, validated_data: dict[str, Any], instance: Reward) -> None:
        """Répartit le `Money` sur les deux colonnes du modèle.

        `Reward.discount` est une **propriété calculée** et non un `MoneyField`
        composite : elle n'a pas de descripteur en écriture, et laisser DRF la
        poser lèverait `property has no setter`. La séparation se fait donc
        ici, au seul endroit où une remise s'écrit.
        """
        remise = validated_data.pop("discount", None)
        if remise is not None:
            instance.discount_minor = remise.amount_minor
            instance.discount_currency = remise.currency

    def create(self, validated_data: dict[str, Any]) -> Reward:
        remise = validated_data.pop("discount", None)
        reward = Reward(**validated_data)
        if remise is not None:
            reward.discount_minor = remise.amount_minor
            reward.discount_currency = remise.currency
        reward.save()
        return reward

    def update(self, instance: Reward, validated_data: dict[str, Any]) -> Reward:
        self._appliquer_remise(validated_data, instance)
        for champ, valeur in validated_data.items():
            setattr(instance, champ, valeur)
        instance.save()
        return instance
