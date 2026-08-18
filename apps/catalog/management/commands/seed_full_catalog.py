"""Peuple la carte complète d'un restaurant : 50 articles, options et photos.

Complète `seed_demo_catalog`, qui ne pose que douze articles sans personnalisation
ni image — assez pour vérifier qu'un écran s'affiche, trop peu pour éprouver une
carte réelle : pagination, filtres par catégorie, groupes d'options obligatoires
ou facultatifs, écarts de prix, régimes alimentaires.

Idempotente (`get_or_create` sur `(restaurant, slug)`, puis sur les noms de
groupes et d'options) : un second appel ne duplique rien.

## Les images

Elles sont **téléchargées** dans le compartiment `products` de MinIO, pas
référencées par une URL distante. C'est ce qu'impose `MenuItem.image`, qui est un
`ImageField` : le catalogue doit rester lisible sans dépendre d'un hébergeur
tiers, et les URL publiques servies ensuite sont celles du projet (ADR-011).

Le téléchargement est donc **facultatif** (`--with-images`) : la commande peuple
la carte sans réseau par défaut. Un seed qui échouerait parce qu'un CDN est
injoignable serait un mauvais seed.

Chaque photo a été vérifiée à l'œil, pas seulement en code de retour HTTP : une
URL valide peut renvoyer une image parfaitement nette de tout autre chose. Les
plats togolais viennent d'Openverse (Flickr/Wikimedia, licences Creative Commons)
faute de couverture correcte ailleurs ; leur qualité est celle d'une photographie
documentaire, pas d'un studio. Voir `PHOTOS_NON_COMMERCIALES` pour la seule
image dont la licence interdit un usage commercial.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.catalog.models import Category, MenuItem, Option, OptionGroup
from apps.restaurants.models import Restaurant
from common.money import Money

DEVISE = "XOF"


# --------------------------------------------------------------------- schéma


@dataclass(frozen=True, slots=True)
class Choix:
    nom: str
    delta: int = 0  # écart de prix en F CFA, éventuellement négatif


@dataclass(frozen=True, slots=True)
class Groupe:
    nom: str
    min_select: int
    max_select: int
    choix: tuple[Choix, ...]


@dataclass(frozen=True, slots=True)
class Plat:
    slug: str
    nom: str
    prix: int
    description: str
    minutes: int
    calories: int
    ingredients: tuple[str, ...]
    allergenes: tuple[str, ...] = ()
    regimes: tuple[str, ...] = ()
    populaire: bool = False
    vip: bool = False
    groupes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Rubrique:
    slug: str
    nom: str
    emoji: str
    plats: tuple[Plat, ...] = field(default_factory=tuple)


# ------------------------------------------------------- groupes d'options
#
# Nommés une fois et réutilisés : « Sauces » a la même définition sur un burger
# et sur une portion de frites. Les exprimer en données plutôt qu'en code est le
# parti pris d'`OptionGroup` — l'exploitation crée un « choisir 2 parmi 5 » sans
# développement.

GROUPES: dict[str, Groupe] = {
    "cuisson": Groupe(
        "Cuisson du steak", 1, 1,
        (Choix("Saignant"), Choix("À point"), Choix("Bien cuit")),
    ),
    "supplements_burger": Groupe(
        "Suppléments", 0, 4,
        (
            Choix("Bacon grillé", 500),
            Choix("Cheddar fondu", 300),
            Choix("Œuf au plat", 400),
            Choix("Avocat frais", 600),
            Choix("Oignons confits", 300),
        ),
    ),
    "sauces": Groupe(
        "Sauces", 0, 2,
        (
            Choix("Ketchup"),
            Choix("Mayonnaise"),
            Choix("Algérienne", 100),
            Choix("Barbecue", 100),
            Choix("Piment fort"),
        ),
    ),
    "formule": Groupe(
        "Formule", 0, 1,
        (Choix("Frites + boisson", 1500), Choix("Grandes frites + boisson", 2000)),
    ),
    "accompagnement": Groupe(
        "Accompagnement", 1, 1,
        (
            Choix("Frites maison"),
            Choix("Riz blanc"),
            Choix("Alloco", 300),
            Choix("Attiéké", 300),
            Choix("Salade verte"),
        ),
    ),
    "piment": Groupe(
        "Niveau de piment", 1, 1,
        (Choix("Doux"), Choix("Moyen"), Choix("Fort"), Choix("Très fort")),
    ),
    "taille_pizza": Groupe(
        "Taille", 1, 1,
        (Choix("26 cm"), Choix("33 cm", 1500), Choix("40 cm", 3000)),
    ),
    "base_pizza": Groupe(
        "Base", 1, 1,
        (Choix("Sauce tomate"), Choix("Crème fraîche", 200)),
    ),
    "supplements_pizza": Groupe(
        "Suppléments", 0, 3,
        (
            Choix("Fromage supplémentaire", 500),
            Choix("Champignons", 400),
            Choix("Olives noires", 300),
            Choix("Piment frais", 200),
            Choix("Œuf", 400),
        ),
    ),
    "proteine": Groupe(
        "Protéine supplémentaire", 0, 2,
        (
            Choix("Poulet grillé", 1000),
            Choix("Poisson braisé", 1200),
            Choix("Bœuf mijoté", 1200),
            Choix("Œuf dur", 300),
        ),
    ),
    "assaisonnement": Groupe(
        "Assaisonnement", 1, 1,
        (
            Choix("Vinaigrette maison"),
            Choix("Sauce César"),
            Choix("Yaourt-citron"),
            Choix("Sans assaisonnement"),
        ),
    ),
    "supplements_salade": Groupe(
        "Suppléments", 0, 3,
        (
            Choix("Poulet grillé", 1000),
            Choix("Avocat", 600),
            Choix("Feta", 500),
            Choix("Œuf dur", 300),
        ),
    ),
    "portion": Groupe(
        "Portion", 1, 1,
        (Choix("Normale"), Choix("Grande", 500)),
    ),
    "supplements_dessert": Groupe(
        "Suppléments", 0, 2,
        (
            Choix("Chantilly", 200),
            Choix("Coulis de chocolat", 200),
            Choix("Boule de glace", 500),
        ),
    ),
    "temperature": Groupe(
        "Température", 1, 1,
        (Choix("Bien fraîche"), Choix("Tempérée")),
    ),
    "taille_boisson": Groupe(
        "Format", 1, 1,
        (Choix("33 cl"), Choix("50 cl", 300)),
    ),
}

B = ("cuisson", "supplements_burger", "sauces", "formule")
B_VEGE = ("supplements_burger", "sauces", "formule")
G = ("accompagnement", "piment", "sauces")
P = ("taille_pizza", "base_pizza", "supplements_pizza")
S = ("piment", "proteine")
SAL = ("assaisonnement", "supplements_salade")
ACC = ("portion", "sauces")
DES = ("supplements_dessert",)
BOI = ("temperature",)
BOI_XL = ("temperature", "taille_boisson")


# ------------------------------------------------------------------ la carte

CARTE: tuple[Rubrique, ...] = (
    Rubrique("burgers", "Burgers", "🍔", (
        Plat("cheeseburger", "Cheeseburger Classique", 2500,
             "Steak de bœuf haché du jour, cheddar fondant, oignons frais, cornichons "
             "et notre sauce maison dans un pain brioché toasté.",
             12, 620, ("pain brioché", "bœuf", "cheddar", "oignon", "cornichon"),
             ("gluten", "lait"), (), populaire=True, groupes=B),
        Plat("double-cheese-bacon", "Double Cheese Bacon", 4000,
             "Deux steaks, double cheddar, bacon grillé croustillant et oignons rouges. "
             "Le burger qu'on ne partage pas.",
             15, 980, ("pain brioché", "bœuf", "cheddar", "bacon", "oignon rouge"),
             ("gluten", "lait"), (), populaire=True, groupes=B),
        Plat("burger-poulet-croustillant", "Burger Poulet Croustillant", 3000,
             "Filet de poulet pané minute, salade croquante, tomate et sauce légèrement "
             "relevée. Croustillant dehors, moelleux dedans.",
             14, 710, ("pain brioché", "poulet", "salade", "tomate"),
             ("gluten", "œuf"), (), groupes=B_VEGE),
        Plat("burger-bbq-oignons", "Burger BBQ Oignons Confits", 3500,
             "Steak de bœuf, oignons longuement confits, cheddar et sauce barbecue fumée.",
             14, 830, ("pain brioché", "bœuf", "oignon confit", "cheddar", "sauce barbecue"),
             ("gluten", "lait"), (), groupes=B),
        Plat("big-corazon", "Big Corazón", 4500,
             "Notre signature : triple étage, bœuf, cheddar, bacon, salade, tomate et "
             "sauce Corazón. Servi avec une portion de frites.",
             18, 1150, ("pain brioché", "bœuf", "cheddar", "bacon", "salade", "tomate"),
             ("gluten", "lait", "œuf"), (), populaire=True, vip=True, groupes=B),
        Plat("burger-oeuf-bacon", "Burger Œuf Bacon", 3300,
             "Steak de bœuf, œuf au plat coulant, bacon grillé et cheddar. "
             "Le petit-déjeuner qui a grandi.",
             14, 870, ("pain brioché", "bœuf", "œuf", "bacon", "cheddar"),
             ("gluten", "lait", "œuf"), (), groupes=B),
        Plat("veggie-burger-avocat", "Veggie Burger Avocat", 2800,
             "Galette de légumes et pois chiches, avocat frais, roquette et tomate séchée. "
             "Généreux sans viande.",
             12, 540, ("pain complet", "pois chiche", "avocat", "roquette", "tomate séchée"),
             ("gluten",), ("vegetarian",), groupes=B_VEGE),
        Plat("burger-piment-fort", "Burger Piment Fort", 3300,
             "Steak de bœuf, piment frais du marché, jalapeños et sauce relevée. "
             "Annoncé, donc assumé.",
             14, 790, ("pain brioché", "bœuf", "piment", "jalapeño", "cheddar"),
             ("gluten", "lait"), (), groupes=B),
    )),
    Rubrique("grillades", "Poulet & Grillades", "🍗", (
        Plat("poulet-braise", "Poulet Braisé Entier", 5000,
             "Poulet entier mariné aux épices, braisé lentement au feu de bois. "
             "À partager — ou pas.",
             35, 1320, ("poulet", "ail", "gingembre", "épices"),
             (), ("halal",), populaire=True, groupes=G),
        Plat("demi-poulet-yassa", "Demi-Poulet Yassa", 3000,
             "Demi-poulet grillé, oignons fondants et citron confit. "
             "La recette sénégalaise, servie comme à la maison.",
             25, 780, ("poulet", "oignon", "citron", "moutarde"),
             ("moutarde",), ("halal",), populaire=True, groupes=G),
        Plat("ailes-bbq", "Ailes de Poulet BBQ (8 pièces)", 2800,
             "Huit ailes marinées douze heures, laquées à la sauce barbecue et grillées.",
             20, 690, ("poulet", "sauce barbecue", "paprika"),
             (), ("halal",), groupes=G),
        Plat("brochettes-boeuf", "Brochettes de Bœuf (3 pièces)", 2500,
             "Trois brochettes de bœuf mariné, grillées à la commande, "
             "poivrons et oignons entre chaque morceau.",
             18, 520, ("bœuf", "poivron", "oignon", "épices"),
             (), ("halal",), groupes=G),
        Plat("cotes-porc", "Côtes de Porc Grillées", 4000,
             "Travers de porc caramélisés à la sauce barbecue, cuisson lente.",
             30, 940, ("porc", "sauce barbecue", "miel"),
             (), (), groupes=G),
        Plat("tilapia-braise", "Tilapia Braisé", 4500,
             "Tilapia entier du lac, farci aux herbes et braisé. "
             "Servi avec sa sauce piquante à part.",
             28, 480, ("tilapia", "herbes", "citron", "ail"),
             ("poisson",), (), groupes=G),
        Plat("poulet-pane-epice", "Poulet Pané Épicé", 2600,
             "Morceaux de poulet panés à la chapelure épicée, frits minute.",
             16, 720, ("poulet", "chapelure", "paprika", "piment"),
             ("gluten", "œuf"), ("halal",), groupes=G),
    )),
    Rubrique("pizzas", "Pizzas", "🍕", (
        Plat("pizza-margherita", "Pizza Margherita", 4000,
             "Sauce tomate, mozzarella fondante et basilic frais. La simplicité bien faite.",
             18, 850, ("pâte", "tomate", "mozzarella", "basilic"),
             ("gluten", "lait"), ("vegetarian",), populaire=True, groupes=P),
        Plat("pizza-reine", "Pizza Reine", 4800,
             "Sauce tomate, mozzarella, jambon et champignons de Paris.",
             20, 960, ("pâte", "tomate", "mozzarella", "jambon", "champignon"),
             ("gluten", "lait"), (), groupes=P),
        Plat("pizza-4-fromages", "Pizza 4 Fromages", 5500,
             "Mozzarella, chèvre, bleu et parmesan sur base crème. Pour les amateurs.",
             20, 1080, ("pâte", "crème", "mozzarella", "chèvre", "bleu", "parmesan"),
             ("gluten", "lait"), ("vegetarian",), groupes=P),
        Plat("pizza-pepperoni", "Pizza Pepperoni", 5000,
             "Sauce tomate, mozzarella et généreuses tranches de pepperoni piquant.",
             20, 1020, ("pâte", "tomate", "mozzarella", "pepperoni"),
             ("gluten", "lait"), (), populaire=True, groupes=P),
        Plat("pizza-thon-oignons", "Pizza Thon Oignons", 5200,
             "Sauce tomate, mozzarella, thon et oignons rouges, filet d'huile d'olive.",
             20, 890, ("pâte", "tomate", "mozzarella", "thon", "oignon rouge"),
             ("gluten", "lait", "poisson"), (), groupes=P),
        Plat("pizza-corazon", "Pizza Corazón", 6000,
             "Notre pizza signature : bœuf épicé, poulet grillé, pepperoni, poivrons "
             "et double mozzarella.",
             24, 1240, ("pâte", "tomate", "mozzarella", "bœuf", "poulet", "pepperoni", "poivron"),
             ("gluten", "lait"), (), vip=True, groupes=P),
    )),
    Rubrique("specialites", "Spécialités Togolaises", "🍛", (
        Plat("riz-sauce-arachide", "Riz Sauce Arachide", 2000,
             "Riz blanc parfumé et sauce à la pâte d'arachide mijotée, viande fondante. "
             "Le plat qui rassemble.",
             25, 780, ("riz", "arachide", "tomate", "viande"),
             ("arachide",), (), populaire=True, groupes=S),
        Plat("fufu-gombo", "Fufu Sauce Gombo", 2200,
             "Fufu pilé à la main, sauce gombo onctueuse et morceaux de viande.",
             25, 690, ("igname", "gombo", "viande", "piment"),
             (), (), groupes=S),
        Plat("attieke-poisson", "Attiéké Poisson Braisé", 2500,
             "Semoule de manioc et poisson braisé entier, oignons marinés et piment frais.",
             28, 720, ("attiéké", "poisson", "oignon", "piment"),
             ("poisson",), (), populaire=True, groupes=S),
        Plat("riz-djolof-poulet", "Riz Djolof Poulet", 2800,
             "Riz cuisiné dans son bouillon de tomate et d'épices, poulet grillé "
             "et alloco en accompagnement.",
             30, 840, ("riz", "tomate", "poulet", "épices"),
             (), ("halal",), populaire=True, groupes=S),
        Plat("gboma-dessi", "Gboma Dessi", 2400,
             "Sauce d'épinards locaux mijotée à l'huile rouge, viande et poisson fumé. "
             "Servie avec pâte ou riz.",
             30, 610, ("épinard", "huile de palme", "viande", "poisson fumé"),
             ("poisson",), (), groupes=S),
    )),
    Rubrique("salades", "Salades & Wraps", "🥗", (
        Plat("salade-cesar-poulet", "Salade César au Poulet", 3000,
             "Laitue romaine croquante, poulet grillé, copeaux de parmesan, "
             "croûtons dorés et sauce César.",
             10, 480, ("laitue", "poulet", "parmesan", "croûton"),
             ("gluten", "lait", "œuf"), (), populaire=True, groupes=SAL),
        Plat("wrap-poulet-crudites", "Wrap Poulet Crudités", 2500,
             "Tortilla garnie de poulet grillé, salade, tomate, carotte râpée "
             "et sauce yaourt-citron.",
             10, 520, ("tortilla", "poulet", "salade", "tomate", "carotte"),
             ("gluten", "lait"), ("halal",), groupes=SAL),
        Plat("salade-avocat-crevettes", "Salade Avocat Crevettes", 3800,
             "Avocat mûr à point, crevettes roses, mesclun et vinaigrette aux agrumes.",
             12, 430, ("avocat", "crevette", "mesclun", "agrumes"),
             ("crustacés",), (), groupes=SAL),
        Plat("wrap-boeuf-epice", "Wrap Bœuf Épicé", 2700,
             "Tortilla, émincé de bœuf mariné aux épices, oignons grillés et sauce relevée.",
             12, 580, ("tortilla", "bœuf", "oignon", "épices"),
             ("gluten",), ("halal",), groupes=SAL),
        Plat("salade-vegetarienne", "Salade Composée Végétarienne", 2200,
             "Avocat, pois chiches, tomates cerises, concombre, radis et graines torréfiées.",
             10, 390, ("avocat", "pois chiche", "tomate cerise", "concombre", "radis"),
             (), ("vegetarian", "vegan"), groupes=SAL),
    )),
    Rubrique("accompagnements", "Accompagnements", "🍟", (
        Plat("frites-maison", "Frites Maison", 1000,
             "Pommes de terre fraîches taillées et frites deux fois. Croustillantes, forcément.",
             8, 340, ("pomme de terre", "huile", "sel"),
             (), ("vegetarian", "vegan"), populaire=True, groupes=ACC),
        Plat("frites-cheddar-bacon", "Frites Cheddar Bacon", 1800,
             "Nos frites nappées de cheddar fondu et parsemées d'éclats de bacon grillé.",
             10, 610, ("pomme de terre", "cheddar", "bacon"),
             ("lait",), (), populaire=True, groupes=ACC),
        Plat("patates-douces-frites", "Frites de Patate Douce", 1300,
             "Patates douces taillées en bâtonnets, légèrement épicées et rôties.",
             10, 380, ("patate douce", "huile", "paprika"),
             (), ("vegetarian", "vegan"), groupes=ACC),
        Plat("onion-rings", "Onion Rings (8 pièces)", 1500,
             "Huit anneaux d'oignon enrobés d'une pâte croustillante.",
             9, 420, ("oignon", "farine", "épices"),
             ("gluten",), ("vegetarian",), groupes=ACC),
        Plat("nuggets-poulet", "Nuggets de Poulet (6 pièces)", 2000,
             "Six nuggets de blanc de poulet panés, sauce au choix.",
             10, 480, ("poulet", "chapelure"),
             ("gluten", "œuf"), ("halal",), groupes=ACC),
        Plat("alloco-plantain", "Alloco (Bananes Plantain)", 1200,
             "Bananes plantain bien mûres frites, dorées et fondantes. "
             "L'accompagnement qui met tout le monde d'accord.",
             10, 360, ("banane plantain", "huile"),
             (), ("vegetarian", "vegan"), populaire=True, groupes=ACC),
        Plat("salade-de-chou", "Salade de Chou", 800,
             "Chou blanc et carotte finement émincés, sauce crémeuse légèrement acidulée.",
             5, 180, ("chou", "carotte", "mayonnaise"),
             ("œuf",), ("vegetarian",), groupes=("portion",)),
    )),
    Rubrique("desserts", "Desserts", "🍰", (
        Plat("brownie-chocolat", "Brownie Chocolat Fondant", 1500,
             "Brownie au chocolat noir, cœur fondant et éclats de noix. Servi tiède.",
             6, 460, ("chocolat", "beurre", "œuf", "noix"),
             ("gluten", "lait", "œuf", "fruits à coque"), ("vegetarian",),
             populaire=True, groupes=DES),
        Plat("tarte-citron-meringuee", "Tarte au Citron Meringuée", 1500,
             "Pâte sablée, crème de citron acidulée et meringue dorée au chalumeau.",
             6, 390, ("citron", "œuf", "sucre", "beurre"),
             ("gluten", "lait", "œuf"), ("vegetarian",), groupes=DES),
        Plat("glace-vanille", "Glace Vanille (2 boules)", 1200,
             "Deux boules de glace à la vanille de Madagascar.",
             3, 280, ("lait", "vanille", "sucre"),
             ("lait",), ("vegetarian",), groupes=DES),
        Plat("beignets-sucres", "Beignets Sucrés (6 pièces)", 1000,
             "Six beignets moelleux saupoudrés de sucre, servis chauds.",
             8, 420, ("farine", "sucre", "œuf", "lait"),
             ("gluten", "lait", "œuf"), ("vegetarian",), groupes=DES),
        Plat("salade-fruits", "Salade de Fruits Frais", 1400,
             "Mangue, ananas, papaye et banane du marché, coupés à la commande.",
             7, 160, ("mangue", "ananas", "papaye", "banane"),
             (), ("vegetarian", "vegan"), groupes=DES),
        Plat("cheesecake-fruits-rouges", "Cheesecake Fruits Rouges", 1800,
             "Cheesecake crémeux sur base de biscuit, nappé d'un coulis de fruits rouges.",
             6, 510, ("fromage frais", "biscuit", "fruits rouges"),
             ("gluten", "lait", "œuf"), ("vegetarian",), groupes=DES),
    )),
    Rubrique("boissons", "Boissons", "🥤", (
        Plat("coca-cola", "Coca-Cola 33cl", 500,
             "Canette de Coca-Cola bien fraîche.",
             1, 139, ("eau gazéifiée", "sucre", "extraits"),
             (), ("vegetarian", "vegan"), groupes=BOI),
        Plat("jus-bissap", "Jus de Bissap", 750,
             "Infusion de fleurs d'hibiscus, menthe fraîche et une pointe de sucre. "
             "Servi très frais.",
             3, 90, ("hibiscus", "menthe", "sucre"),
             (), ("vegetarian", "vegan"), populaire=True, groupes=BOI_XL),
        Plat("jus-gingembre", "Jus de Gingembre", 800,
             "Gingembre frais pressé, citron et ananas. Piquant comme il faut.",
             3, 110, ("gingembre", "citron", "ananas"),
             (), ("vegetarian", "vegan"), groupes=BOI_XL),
        Plat("eau-minerale", "Eau Minérale 50cl", 300,
             "Bouteille d'eau minérale naturelle.",
             1, 0, ("eau",),
             (), ("vegetarian", "vegan"), groupes=BOI),
        Plat("smoothie-mangue-ananas", "Smoothie Mangue-Ananas", 1500,
             "Mangue et ananas mixés minute, sans sucre ajouté.",
             5, 210, ("mangue", "ananas"),
             (), ("vegetarian", "vegan"), populaire=True, groupes=BOI_XL),
        Plat("the-glace-citron", "Thé Glacé Citron", 700,
             "Thé noir infusé à froid, citron frais et glaçons.",
             3, 80, ("thé", "citron", "sucre"),
             (), ("vegetarian", "vegan"), groupes=BOI_XL),
    )),
)


# ------------------------------------------------------------------- photos
#
# Deux sources, pour une raison simple : Unsplash offre une photographie de
# studio mais ne couvre pas la cuisine ouest-africaine ; Openverse (Flickr,
# Wikimedia) la couvre en licence Creative Commons, avec la qualité d'une photo
# documentaire. Chaque association a été contrôlée visuellement.

_UNSPLASH = "https://images.unsplash.com/{}?w=1200&q=80&fm=jpg&fit=crop"

PHOTOS: dict[str, str] = {
    # Burgers
    "cheeseburger": _UNSPLASH.format("photo-1568901346375-23c9450c58cd"),
    "double-cheese-bacon": _UNSPLASH.format("photo-1553979459-d2229ba7433b"),
    "burger-poulet-croustillant": _UNSPLASH.format("photo-1606755962773-d324e0a13086"),
    "burger-bbq-oignons": _UNSPLASH.format("photo-1594212699903-ec8a3eca50f5"),
    "big-corazon": _UNSPLASH.format("photo-1571091718767-18b5b1457add"),
    "burger-oeuf-bacon": _UNSPLASH.format("photo-1550317138-10000687a72b"),
    "veggie-burger-avocat": _UNSPLASH.format("photo-1520072959219-c595dc870360"),
    "burger-piment-fort": _UNSPLASH.format("photo-1586190848861-99aa4a171e90"),
    # Grillades
    "poulet-braise": _UNSPLASH.format("photo-1598103442097-8b74394b95c6"),
    "demi-poulet-yassa": _UNSPLASH.format("photo-1532550907401-a500c9a57435"),
    "ailes-bbq": _UNSPLASH.format("photo-1567620832903-9fc6debc209f"),
    "brochettes-boeuf": _UNSPLASH.format("photo-1603360946369-dc9bb6258143"),
    "cotes-porc": _UNSPLASH.format("photo-1544025162-d76694265947"),
    "tilapia-braise": _UNSPLASH.format("photo-1519708227418-c8fd9a32b7a2"),
    "poulet-pane-epice": _UNSPLASH.format("photo-1562967914-608f82629710"),
    # Pizzas
    "pizza-margherita": _UNSPLASH.format("photo-1574071318508-1cdbab80d002"),
    "pizza-reine": _UNSPLASH.format("photo-1513104890138-7c749659a591"),
    "pizza-4-fromages": _UNSPLASH.format("photo-1593560708920-61dd98c46a4e"),
    "pizza-pepperoni": _UNSPLASH.format("photo-1628840042765-356cda07504e"),
    "pizza-thon-oignons": _UNSPLASH.format("photo-1604382354936-07c5d9983bd3"),
    "pizza-corazon": _UNSPLASH.format("photo-1594007654729-407eedc4be65"),
    # Spécialités togolaises — Openverse
    "riz-sauce-arachide": "https://live.staticflickr.com/31/54975494_794328a5ec.jpg",
    "fufu-gombo": _UNSPLASH.format("photo-1604329760661-e71dc83f8f26"),
    "attieke-poisson": "https://upload.wikimedia.org/wikipedia/commons/4/40/Attieke_with_fish.jpg",
    "riz-djolof-poulet": "https://live.staticflickr.com/3251/2853745181_8a5f9fa230_b.jpg",
    "gboma-dessi": "https://live.staticflickr.com/5217/5387418083_892ae3f353_b.jpg",
    # Salades & wraps
    "salade-cesar-poulet": _UNSPLASH.format("photo-1546793665-c74683f339c1"),
    "wrap-poulet-crudites": _UNSPLASH.format("photo-1562059390-a761a084768e"),
    "salade-avocat-crevettes": _UNSPLASH.format("photo-1551248429-40975aa4de74"),
    "wrap-boeuf-epice": _UNSPLASH.format("photo-1626700051175-6818013e1d4f"),
    "salade-vegetarienne": _UNSPLASH.format("photo-1512621776951-a57141f2eefd"),
    # Accompagnements
    "frites-maison": _UNSPLASH.format("photo-1573080496219-bb080dd4f877"),
    "frites-cheddar-bacon": _UNSPLASH.format("photo-1598679253544-2c97992403ea"),
    "patates-douces-frites": "https://live.staticflickr.com/3175/3001628444_fd74248575_b.jpg",
    "onion-rings": _UNSPLASH.format("photo-1639024471283-03518883512d"),
    "nuggets-poulet": _UNSPLASH.format("photo-1562967916-eb82221dfb92"),
    "alloco-plantain": "https://live.staticflickr.com/3315/4580820053_87f69e10ac_b.jpg",
    "salade-de-chou": _UNSPLASH.format("photo-1607532941433-304659e8198a"),
    # Desserts
    "brownie-chocolat": _UNSPLASH.format("photo-1606313564200-e75d5e30476c"),
    "tarte-citron-meringuee": _UNSPLASH.format("photo-1519915028121-7d3463d20b13"),
    "glace-vanille": _UNSPLASH.format("photo-1497034825429-c343d7c6a68f"),
    "beignets-sucres": _UNSPLASH.format("photo-1551106652-a5bcf4b29ab6"),
    "salade-fruits": _UNSPLASH.format("photo-1564093497595-593b96d80180"),
    "cheesecake-fruits-rouges": _UNSPLASH.format("photo-1533134242443-d4fd215305ad"),
    # Boissons
    "coca-cola": _UNSPLASH.format("photo-1554866585-cd94860890b7"),
    "jus-bissap": _UNSPLASH.format("photo-1497534446932-c925b458314e"),
    "jus-gingembre": _UNSPLASH.format("photo-1600271886742-f049cd451bba"),
    "eau-minerale": _UNSPLASH.format("photo-1616118132534-381148898bb4"),
    "smoothie-mangue-ananas": _UNSPLASH.format("photo-1546173159-315724a31696"),
    "the-glace-citron": _UNSPLASH.format("photo-1556679343-c7306c1976bc"),
}

#: Licences interdisant l'usage commercial. Acceptable pour peupler un
#: environnement de développement, à remplacer avant toute mise en production.
PHOTOS_NON_COMMERCIALES = frozenset({"riz-sauce-arachide"})

#: Un agent est nécessaire : Wikimedia refuse les requêtes sans en-tête `User-Agent`
#: identifiable et répond 403.
_AGENT = {"User-Agent": "elcorazon-seed/1.0 (catalogue de développement)"}


class Command(BaseCommand):
    help = "Peuple la carte complète (50 articles, options, images) d'un restaurant."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--restaurant",
            default="el-corazon-lome",
            help="Slug du restaurant à peupler (défaut : el-corazon-lome).",
        )
        parser.add_argument(
            "--with-images",
            action="store_true",
            help="Télécharge les photos dans le compartiment `products`. Demande un accès réseau.",
        )
        parser.add_argument(
            "--replace-images",
            action="store_true",
            help="Retélécharge les photos des articles qui en ont déjà une.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        slug = options["restaurant"]
        try:
            restaurant = Restaurant.objects.get(slug=slug)
        except Restaurant.DoesNotExist as exc:
            raise CommandError(
                f"Aucun restaurant avec le slug {slug!r}. "
                "Créer le restaurant avant de peupler sa carte."
            ) from exc

        # La structure est posée d'un bloc : une carte à moitié écrite, avec des
        # articles sans leurs groupes d'options, laisserait l'application afficher
        # un plat impossible à commander.
        with transaction.atomic():
            rubriques, articles, groupes, choix = self._peupler(restaurant)

        self.stdout.write(
            self.style.SUCCESS(
                f"Restaurant {slug!r} : {rubriques} catégorie(s), {articles} article(s), "
                f"{groupes} groupe(s) d'options et {choix} option(s) créé(s) "
                "(déjà présents ignorés)."
            )
        )

        if options["with_images"]:
            self._telecharger_images(restaurant, remplacer=options["replace_images"])

    # -- structure ------------------------------------------------------

    def _peupler(self, restaurant: Restaurant) -> tuple[int, int, int, int]:
        n_rub = n_art = n_grp = n_opt = 0

        for rang_rub, rubrique in enumerate(CARTE):
            categorie, cree = Category.objects.get_or_create(
                restaurant=restaurant,
                slug=rubrique.slug,
                defaults={
                    "name": rubrique.nom,
                    "emoji": rubrique.emoji,
                    "sort_order": rang_rub,
                },
            )
            n_rub += int(cree)

            for rang_plat, plat in enumerate(rubrique.plats):
                article, cree = MenuItem.objects.get_or_create(
                    restaurant=restaurant,
                    slug=plat.slug,
                    defaults={
                        "category": categorie,
                        "name": plat.nom,
                        "description": plat.description,
                        "price": Money(plat.prix, DEVISE),
                        "preparation_minutes": plat.minutes,
                        "calories": plat.calories,
                        "ingredients": list(plat.ingredients),
                        "allergens": list(plat.allergenes),
                        "dietary_tags": list(plat.regimes),
                        "is_popular": plat.populaire,
                        "vip_exclusive": plat.vip,
                        "sort_order": rang_plat,
                    },
                )
                n_art += int(cree)

                for rang_grp, cle in enumerate(plat.groupes):
                    modele = GROUPES[cle]
                    groupe, cree = OptionGroup.objects.get_or_create(
                        menu_item=article,
                        name=modele.nom,
                        defaults={
                            "min_select": modele.min_select,
                            "max_select": modele.max_select,
                            "sort_order": rang_grp,
                        },
                    )
                    n_grp += int(cree)

                    for choix in modele.choix:
                        _, cree = Option.objects.get_or_create(
                            group=groupe,
                            name=choix.nom,
                            defaults={"price_delta": Money(choix.delta, DEVISE)},
                        )
                        n_opt += int(cree)

        return n_rub, n_art, n_grp, n_opt

    # -- images ---------------------------------------------------------

    @staticmethod
    def _recuperer(url: str, *, essais: int = 4) -> bytes | None:
        """Télécharge une image, en réessayant les échecs transitoires.

        Cinquante requêtes rapprochées vers le même hôte suffisent à déclencher
        une limitation de débit ou à saturer le résolveur DNS du conteneur : au
        premier essai, la moitié des photos échouait sur des `URLError` que le
        même appel, rejoué seul, servait sans broncher. L'attente croît entre
        les tentatives, et une pause sépare chaque téléchargement réussi.
        """
        for tentative in range(essais):
            try:
                requete = urllib.request.Request(url, headers=_AGENT)
                with urllib.request.urlopen(requete, timeout=30) as reponse:
                    return bytes(reponse.read())
            except urllib.error.HTTPError as exc:
                # 404 ou 403 : rejouer n'y changera rien, l'URL est à corriger.
                if exc.code not in (429, 500, 502, 503, 504):
                    return None
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(1.5 * (tentative + 1))
        return None

    def _telecharger_images(self, restaurant: Restaurant, *, remplacer: bool) -> None:
        articles = MenuItem.objects.filter(restaurant=restaurant)
        poses = ignores = 0
        echecs: list[str] = []

        for article in articles:
            url = PHOTOS.get(article.slug)
            if url is None:
                continue
            if article.image and not remplacer:
                ignores += 1
                continue

            octets = self._recuperer(url)
            if octets is None:
                # Une photo manquante ne doit pas faire échouer le peuplement :
                # la carte reste commandable sans elle, et la commande est
                # rejouable pour rattraper les seules images absentes.
                echecs.append(article.slug)
                continue

            article.image.save(f"{article.slug}.jpg", ContentFile(octets), save=True)
            poses += 1
            time.sleep(0.4)

        self.stdout.write(
            self.style.SUCCESS(
                f"Images : {poses} déposée(s), {ignores} déjà présente(s), {len(echecs)} en échec."
            )
        )
        for echec in echecs:
            self.stdout.write(self.style.WARNING(f"  échec : {echec}"))

        non_commerciales = sorted(PHOTOS_NON_COMMERCIALES)
        if non_commerciales:
            self.stdout.write(
                self.style.WARNING(
                    "Licence non commerciale (à remplacer avant mise en production) : "
                    + ", ".join(non_commerciales)
                )
            )
