"""Cycle de vie des fichiers attachés aux modèles.

Django **n'efface jamais** le fichier qu'un `FileField` vient de remplacer. Ce
n'est pas un oubli de sa part : il ne peut pas savoir si l'ancien objet est
encore référencé ailleurs. Mais dans ce projet il ne l'est pas, et le laisser
en place a deux conséquences très différentes selon le compartiment :

* **compartiments publics** — un burger photographié trois fois laisse deux
  images mortes dans `products`. On paie du stockage pour rien ;
* **compartiment privé** — et c'est celui qui compte. Un livreur redépose ses
  pièces à chaque rejet de dossier (invariant L5 : tout dépôt repasse le
  dossier en attente). Sans effacement, chaque pièce d'identité, permis et
  carte grise jamais déposés s'accumulent indéfiniment dans `documents`.
  Conserver sans fin des pièces d'identité qu'on a soi-même remplacées n'est
  pas un problème de facture, c'est un problème de rétention de données
  personnelles.

## Pourquoi un signal plutôt qu'une surcharge de `save()`

Les fichiers n'arrivent pas que par l'API : l'administration Django écrit les
mêmes modèles, et une commande de gestion pourrait le faire aussi. Un signal
`pre_save` couvre tous les chemins d'écriture d'un coup, là où une surcharge
par sérialiseur en couvrirait un seul — celui qu'on aurait pensé à traiter.

Le branchement est **automatique** : `register()` parcourt les modèles
installés et se connecte à ceux qui portent un `FileField`. Un champ fichier
ajouté demain est couvert sans qu'on y pense, ce qui est précisément le genre
d'oubli qu'on ne remarque qu'à l'audit.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import FileField, Model
from django.db.models.signals import post_delete, pre_save

__all__ = ["delete_files_of_deleted_row", "delete_replaced_files", "register"]


def _file_fields(model: type[Model]) -> list[str]:
    """Noms des champs fichier d'un modèle, `ImageField` compris (il en hérite)."""
    return [field.name for field in model._meta.get_fields() if isinstance(field, FileField)]


def delete_replaced_files(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Efface les fichiers que cet enregistrement s'apprête à remplacer.

    Appelé avant l'écriture : on relit la ligne telle qu'elle est encore en
    base, et on compare champ par champ. Un champ inchangé n'est pas touché —
    c'est la comparaison des **noms** qui le garantit, et l'omettre effacerait
    le fichier courant à chaque sauvegarde du modèle.
    """
    if instance.pk is None:
        return  # Création : il n'y a rien à remplacer.

    champs = _file_fields(sender)
    if not champs:
        return

    try:
        # `_default_manager` et non `objects` : un modèle est libre de nommer
        # son gestionnaire autrement, et c'est celui-ci que Django tient pour
        # canonique. Il ne filtre pas les suppressions logiques, ce qui est ce
        # qu'on veut — on cherche la ligne telle qu'elle est écrite.
        ancien = sender._default_manager.get(pk=instance.pk)
    except ObjectDoesNotExist:
        # `pk` renseigné mais absent de la base : une clé choisie par
        # l'appelant, ou une restauration. Rien à remplacer.
        return

    for champ in champs:
        fichier_ancien = getattr(ancien, champ)
        fichier_nouveau = getattr(instance, champ)

        # `.name` est vide quand le champ ne porte pas de fichier. Comparer les
        # objets `FieldFile` eux-mêmes ne dirait rien d'utile : deux instances
        # distinctes désignent le même objet stocké.
        if not fichier_ancien.name or fichier_ancien.name == fichier_nouveau.name:
            continue

        # `save=False` : on est déjà dans le `pre_save` de cet enregistrement,
        # et sauvegarder ici relancerait le signal.
        fichier_ancien.delete(save=False)


def delete_files_of_deleted_row(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Efface les fichiers d'un enregistrement supprimé.

    Ne concerne que les suppressions **réelles**. Les modèles à suppression
    logique (un article de catalogue reste lisible depuis les commandes
    passées) ne déclenchent pas ce signal, et c'est voulu : leur fichier doit
    survivre, sans quoi une commande de l'an dernier afficherait un cadre vide.
    """
    for champ in _file_fields(sender):
        fichier = getattr(instance, champ)
        if fichier.name:
            fichier.delete(save=False)


def register() -> None:
    """Branche les deux signaux sur tous les modèles portant un fichier."""
    for model in apps.get_models():
        if not _file_fields(model):
            continue

        # `dispatch_uid` : `ready()` peut être appelé deux fois (autoreload du
        # serveur de développement). Sans identifiant, le fichier remplacé
        # serait effacé deux fois — inoffensif ici, mais le doublon masquerait
        # une vraie double-connexion le jour où elle compterait.
        pre_save.connect(
            delete_replaced_files,
            sender=model,
            dispatch_uid=f"fichiers-remplaces-{model._meta.label_lower}",
        )
        post_delete.connect(
            delete_files_of_deleted_row,
            sender=model,
            dispatch_uid=f"fichiers-supprimes-{model._meta.label_lower}",
        )
