"""Champs de modèle réutilisables.

`MoneyField` est le seul moyen autorisé de persister un montant (ADR-007). Il
matérialise deux colonnes — l'entier en unité mineure et la devise — et les
expose comme un unique objet `Money`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, overload

from django.core.exceptions import ValidationError
from django.db import models

from common.money import CURRENCY_EXPONENTS, Money

__all__ = ["MoneyField"]


class MoneyField[Amount: (Money, Money | None)]:
    """Descripteur de montant : deux colonnes, un objet.

    À déclarer dans le corps du modèle :

        class Order(models.Model):
            total = MoneyField()

    Django n'ayant pas de type composite natif, `contribute_to_class` ajoute
    les colonnes `<nom>_minor` (BIGINT) et `<nom>_currency` (CHAR(3)), et le
    descripteur les recompose. L'alternative — un `DecimalField` seul — perd la
    devise, ce qui rend le multi-pays impossible et l'historique ambigu.

    Le paramètre de type porte la nullité : `MoneyField()` se lit `Money`,
    `MoneyField(null=True)` se lit `Money | None`. Sans cette distinction, le
    vérificateur de types laisserait passer une addition sur un seuil de franco
    non renseigné — un `TypeError` en production, au moment précis où l'on
    calcule ce que le client doit payer.
    """

    @overload
    def __init__(
        self: MoneyField[Money],
        *,
        null: Literal[False] = False,
        default_currency: str | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self: MoneyField[Money | None],
        *,
        null: Literal[True],
        default_currency: str | None = None,
    ) -> None: ...

    def __init__(self, *, null: bool = False, default_currency: str | None = None) -> None:
        self.null = null
        self.default_currency = default_currency
        self.name: str = ""

    if TYPE_CHECKING:
        # `contribute_to_class` remplace l'attribut de classe par une vraie
        # `property` : à l'exécution, ces méthodes ne sont jamais appelées.
        # Elles n'existent que pour dire au vérificateur ce que l'attribut rend
        # une fois le modèle construit.
        def __get__(self, instance: object, owner: type[object] | None = None) -> Amount: ...

        def __set__(self, instance: object, value: Amount) -> None: ...

    def contribute_to_class(self, cls: type[models.Model], name: str, **_: Any) -> None:
        self.name = name
        minor_attr, currency_attr = f"{name}_minor", f"{name}_currency"

        models.BigIntegerField(null=self.null, blank=self.null).contribute_to_class(cls, minor_attr)
        models.CharField(
            max_length=3,
            null=self.null,
            blank=self.null,
            default=self.default_currency,
            choices=[(c, c) for c in sorted(CURRENCY_EXPONENTS)],
        ).contribute_to_class(cls, currency_attr)

        nullable = self.null

        def getter(instance: models.Model) -> Money | None:
            minor = getattr(instance, minor_attr)
            currency = getattr(instance, currency_attr)
            if minor is None or not currency:
                return None
            return Money(minor, currency)

        def setter(instance: models.Model, value: Money | None) -> None:
            if value is None:
                if not nullable:
                    raise ValidationError(f"{name} ne peut pas être nul.")
                setattr(instance, minor_attr, None)
                setattr(instance, currency_attr, None)
                return

            if not isinstance(value, Money):
                raise TypeError(
                    f"{name} attend un objet Money, reçu {type(value).__name__}. "
                    "Un nombre nu n'a pas de devise, et un montant sans devise "
                    "n'a pas de sens dès qu'il existe plus d'un pays."
                )

            setattr(instance, minor_attr, value.amount_minor)
            setattr(instance, currency_attr, value.currency)

        # Une **vraie** `property`, et non un descripteur maison : Django ne
        # reconnaît comme argument de constructeur que les champs déclarés et
        # les attributs que `inspect.getattr_static` identifie comme `property`.
        # Avec un descripteur quelconque, `DeliveryZone(base_fee=…)` lève
        # « unexpected keyword arguments » — ce qu'un test a attrapé.
        setattr(cls, name, property(getter, setter))
