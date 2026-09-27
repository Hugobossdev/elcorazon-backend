# Backend El Corazón v2

API et logique métier de la plateforme. **Le backend est la seule autorité
métier** — les applications Flutter sont des clients, sans accès direct à la
base.

Conception : [`docs/architecture/`](../docs/architecture/README.md).

## Démarrage

### Avec Docker (recommandé)

```bash
cp .env.example .env
# renseigner DJANGO_SECRET_KEY, POSTGRES_PASSWORD, JWT_SIGNING_KEY, JWT_VERIFYING_KEY
docker compose up
```

- API : <http://localhost:8000>
- Documentation OpenAPI : <http://localhost:8000/api/v1/docs/>
- Stockage objet : Cloudinary (tableau de bord sur <https://cloudinary.com/console>)

PostgreSQL est exposé sur **5433** et non 5432, pour ne pas entrer en conflit
avec une instance déjà installée sur le poste.

### Tests

La suite s'exécute **dans l'image**, parce que GeoDjango s'appuie sur GDAL et
GEOS — des bibliothèques système, pas des paquets Python, absentes d'un poste
Windows nu :

```bash
docker compose up -d db redis
docker compose run --rm api pytest
```

La création de la base de test rejoue toutes les migrations, ce qui domine la
durée d'une exécution. En boucle de développement :

```bash
docker compose run --rm api pytest --reuse-db     # réutilise la base existante
docker compose run --rm api pytest --create-db    # après une nouvelle migration
```

Les tests purement algorithmiques (montants, machine à états, identifiants) ne
touchent ni la base ni le réseau et tournent aussi dans un simple virtualenv :

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Linux/macOS : .venv/bin/python
.venv/Scripts/python -m pytest tests/common
```

> Il n'existe **pas** de repli SQLite. Le schéma emploie des types propres à
> PostgreSQL — `ArrayField`, `geography`, index GiST — qu'un autre moteur ne
> peut pas porter. Un vert obtenu sur un schéma dégradé ne prouverait rien.

## Structure

```
config/       réglages (base/dev/prod/test), routage, ASGI, Celery
common/       socle transverse, sans dépendance aux apps métier
  money.py            montants : entier mineur + devise (ADR-007)
  identifiers.py      UUIDv7 (ADR-007)
  state_machine.py    transitions déclaratives (ADR-010)
  exceptions.py       erreurs métier → RFC 9457 (ADR-009)
  pagination.py       page / curseur (ADR-009)
apps/         18 applications par domaine métier (ADR-002)
tests/        suite de tests
deploy/       Nginx
```

## Points d'entrée ouverts

Le refus étant le défaut (`IsAuthenticated`), voici la liste — auditable — de ce
qui se lit **sans compte** : un visiteur doit pouvoir savoir si on le livre et
voir un prix avant de s'inscrire.

| Route | Verbe | Accès |
|---|---|---|
| `/api/v1/auth/register\|login\|token/refresh` | POST | public, limité en débit |
| `/api/v1/geography/countries\|cities` | GET | public |
| `/api/v1/geography/zones/resolve/?lat=&lon=` | GET | public |
| `/api/v1/restaurants/` (`?lat=&lon=` trie par proximité) | GET | public |
| `/api/v1/catalog/categories\|items` | GET | public |
| `/api/v1/catalog/reviews/` | GET | public |
| `/api/v1/catalog/reviews/` | POST | client authentifié |
| `/api/v1/payments/webhook/{provider}/` | POST | **signature HMAC**, pas de jeton |
| `/api/v1/payments/shares/{token}/` | GET, POST | **jeton du lien**, pas de compte |

Le webhook est la seule route ouverte en écriture sans compte : un prestataire
n'en a pas. Son justificatif est la signature HMAC-SHA256 du corps brut,
vérifiée avant toute écriture — plus fort qu'un jeton porteur, qu'il suffirait
d'intercepter pour rejouer sur un autre corps.

Tout le reste exige un jeton.

**Le personnel a deux clés, pas une.** La permission nommée dit *ce qu'on a le
droit de faire* (`orders.refund`), le rattachement à un établissement
(`restaurants.StaffMembership`) dit **sur quoi**. Un membre du personnel sans
rattachement ne voit rien : un oubli de configuration produit une panne
visible, jamais un accès trop large et silencieux.

## Paiement partagé

La faille la plus grave de l'implémentation précédente était là : n'importe quel
participant pouvait se déclarer payé, ce qui basculait la commande entière en
`completed` — un repas gratuit. Le correctif d'alors avait restreint l'action
aux administrateurs, sans construire le vrai flux.

La réponse n'est pas une vérification de plus, c'est une **structure** : une
part n'est réputée réglée que si elle porte une transaction encaissée, et la
contrainte est en base (`settled_share_requires_transaction`). Il n'existe aucun
chemin — API, back-office, script — pour marquer une part payée sans
encaissement réel. Une part **suit** sa transaction ; elle ne décide de rien.

Le total se divise par `Money.allocate`, qui ne perd pas une unité mineure :
4 000 F en trois donne 1 334, 1 333 et 1 333.

`GET|POST /api/v1/payments/shares/{token}/` s'ouvre **sans compte** — la moitié
des convives d'un repas partagé n'en ont pas, et exiger une inscription ferait
échouer la fonctionnalité sur son cas le plus courant. Le jeton est aléatoire et
non dérivé de la part : les UUIDv7 étant ordonnés dans le temps, un lien qui
circule sur une messagerie ne peut pas s'appuyer dessus. Il ne donne accès qu'à
la part — ni à la commande, ni aux autres participants.

## Codes promotionnels

Les cinq conditions de F4 — période, montant minimum, plafond, quota global,
quota par personne — vivent **en données** : l'exploitation crée « −500 F, dix
premiers clients, ce week-end » depuis le back-office, sans développement.

Deux temps distincts, et c'est ce qui rend le mécanisme sûr :

- `POST /api/v1/orders/preview/` **évalue sans réserver**. Le client voit le
  détail — sous-total, frais, remise, total — avant de s'engager, et découvre
  un code refusé là plutôt qu'en appuyant sur « commander » ;
- la création de commande **consomme, sous verrou**. Le quota y est revérifié :
  entre le devis et la validation, quelqu'un d'autre a pu prendre le dernier
  coupon.

Le client envoie **un code**, jamais un montant — c'est C1 transposé. Et une
commande annulée **rend** le code : il avait été décompté pour un repas jamais
reçu.

## Encaissement

Chaque prestataire a son connecteur, derrière un port unique
(`apps/payments/gateway.py`). Le port porte trois choses : ouvrir une demande
de paiement, **authentifier** les notifications et les **lire** — les deux
dernières parce qu'elles diffèrent d'un prestataire à l'autre. Le bac à sable
signe le corps en HMAC-SHA256 ; PayDunya joint l'empreinte SHA-512 de sa clé
maîtresse.

Ce qui **décide** reste en dehors : gardes C5, idempotence P1, transitions,
plafond P3. Tout cela se vérifie sans compte marchand, et le connecteur PayDunya
se teste hors réseau par transport simulé.

**Avant la mise en service**, il faut créer une facture réelle en mode `test` et
comparer la réponse reçue à ce que lit `PayDunyaGateway._read_checkout`, ainsi
qu'une notification réelle à ce que lit `parse`. Les noms de champs suivent le
contrat documenté, mais n'ont pas pu être confrontés à l'API depuis ce dépôt.

**Le remboursement n'est pas automatisé.** PayDunya n'expose pas d'API de
remboursement : `RefundService` écrit l'intention, son plafond et sa trace, et
le virement se fait depuis leur tableau de bord. Le remboursement reste en
`pending` jusqu'à confirmation humaine — à dire à l'exploitation, sinon
quelqu'un cliquera « rembourser » et croira que c'est fait.

## Notifications push

`ConsolePushBackend` par défaut ; `FirebaseCloudMessagingBackend` se branche par
`PUSH_BACKEND`. Trois choses distinguent FCM d'un simple POST :

- **l'authentification est un jeton OAuth**, pas une clé d'API — la clé serveur
  a été retirée par Google. `google-auth` signe l'assertion et rafraîchit le
  jeton ;
- **l'envoi est unitaire** : l'API v1 n'a pas de diffusion groupée, donc une
  requête par appareil. C'est pourquoi tout cela vit dans une tâche Celery et
  jamais dans le cycle de requête ;
- **la réponse d'erreur porte la décision.** `UNREGISTERED`,
  `INVALID_ARGUMENT` et `SENDER_ID_MISMATCH` font supprimer le jeton ; tout le
  reste — quota, indisponibilité, réseau — déclenche une reprise. Le statut HTTP
  ne décide pas seul : un 400 peut signaler un jeton mort comme une charge utile
  mal formée, et purger sur le second effacerait des appareils sains.

**Avant la mise en service**, envoyer une notification à un appareil de test
(`python manage.py send_test_push <jeton>`) et comparer la réponse d'erreur reçue
à `ERREURS_DEFINITIVES` : c'est cette classification qui compte, et elle n'a pas
pu être confrontée au service réel depuis ce dépôt. Chaque refus de Google est
journalisé sous `fcm.rejet` — statut HTTP, code, décision prise — et c'est ce
`code` qu'on compare. Marche à suivre complète : `docs/firebase.md`.

## Limitation de débit

Elle s'applique **partout** : `anon` et `user` sont le défaut du projet, et une
vue qui déclare `throttle_classes` remplace ce défaut par un quota nommé.

| Quota | Défaut | Pourquoi |
|---|---|---|
| `anon` / `user` | 60 / 120 par minute | Socle : lecture de catalogue, historique |
| `auth_ip` / `auth_identifier` | 20 / 5 par minute | T1 — force brute, par adresse puis par identifiant |
| `order_create` | 10 par minute | Verrous, relecture catalogue, PostGIS |
| `payment_initiate` | 10 par minute | Crée une transaction, appelle le prestataire |
| `cart_write` | 60 par minute | Gestes répétés et légitimes |
| `review_write` | 5 par minute | Anti-remplissage |
| `tracking_ping` | 240 par minute | Rafales de rattrapage à la sortie d'un tunnel |
| `webhook` | 60 par minute | Le prestataire peut grouper ses envois |

**`NUM_PROXIES` n'est pas un réglage d'optimisation.** Nginx *ajoute* à
`X-Forwarded-For` au lieu de le remplacer : sans cette valeur, DRF prend la
chaîne entière, un client qui envoie son propre en-tête obtient une identité
neuve à chaque requête, et le limiteur par adresse IP ne compte plus rien.

## Back-office

`/admin/` — outil d'**exploitation**, pas seconde API. Il sert à valider un
dossier livreur, retrouver une transaction, corriger un repère d'adresse.

Trois règles y sont tenues par le code, parce qu'un back-office est le chemin
le plus court pour contourner ses propres garde-fous :

- **les statuts ne sont pas des champs.** Une liste déroulante sur `status`
  suffirait à écrire « livrée » sur une commande jamais partie — sans machine à
  états, sans journal, sans créditer le livreur. Ils se changent par des
  actions, qui appellent les mêmes services que l'API et sont refusées de la
  même façon ;
- **les montants ne s'éditent pas.** Ils sont recomposés serveur (C2) : un
  total saisi à la main serait un total faux qui a l'air juste ;
- **les écritures comptables ne se suppriment pas.** Commandes, lignes,
  transactions, remboursements et journaux d'événements sont conservés.

Les paiements sont intégralement en lecture seule : le webhook signé est la
seule source de vérité de l'encaissement, et un formulaire qui écrirait
`completed` rouvrirait ici la faille que P2 ferme partout ailleurs.

```bash
docker compose run --rm api python -m django createsuperuser
```

## WebSocket

| Route | Qui |
|---|---|
| `ws/orders/{id}/tracking/` | le client de la commande, son livreur assigné, le personnel de l'établissement |
| `ws/couriers/me/` | le livreur, sur sa propre file — aucun identifiant dans l'URL |

Le jeton se présente en en-tête `Authorization: Bearer …`, ou à défaut en
`?token=` pour les navigateurs, qui ne savent pas poser d'en-tête sur un
WebSocket. **L'autorisation est vérifiée avant l'acceptation** : un socket
refusé est fermé avec un code de la plage 4000 (`4401` jeton, `4403` droit,
`4409` geste interdit sur un socket pourtant ouvert), jamais laissé ouvert en
lecture seule.

Chaque message porte un `seq` croissant par groupe. À la reconnexion, le client
redemande la suite avec `?since=<seq>` ; s'il a été absent plus longtemps que
le journal, il reçoit un `realtime.gap` qui lui dit de recharger par HTTP
plutôt que de croire qu'il n'a rien manqué.

Le contrat complet est dans le schéma OpenAPI, généré depuis les
sérialiseurs :

```bash
docker compose run --rm api python -m django spectacular --fail-on-warn
```

## Qualité

```bash
ruff check .          # style et bogues probables
ruff format .         # format
mypy common config apps
pytest --cov
```

Deux familles de tests ne vérifient pas du métier mais des **règles** :

```bash
pytest -m architecture   # graphe de dépendances, couches, surface publique
pytest -m contract       # forme des réponses face au schéma OpenAPI
```

La première rend exécutable ce que l'ADR-002 annonçait comme « vérifié en CI » :
une dépendance hors du graphe, un cycle entre apps ou une route ouverte sans
figurer dans la liste déclarée font échouer la construction. La seconde ferme
le piège nº 3 de la Phase 1 — un champ déclaré non nul qui sort absent fait
planter les clients Dart, qui appellent `DateTime.parse` sans garde.

Ces quatre commandes sont celles qu'exécute la CI
([`.github/workflows/backend-ci.yml`](../../.github/workflows/backend-ci.yml)).

## Points d'attention

**Les montants ne sont jamais des flottants.** `Money` refuse un `float` à la
construction. Sur un total de commande l'écart est invisible ; sur un cumul de
commissions livreur en fin de mois, il devient un litige.

**Les statuts ne s'écrivent pas directement.** Toute transition passe par la
machine à états, qui vérifie, journalise et émet l'événement dans une seule
transaction. C'est ce qui ferme quatre des douze failles relevées sur
l'implémentation précédente.

**Le refus est le défaut.** La permission globale est `IsAuthenticated` ; toute
route publique se déclare explicitement, ce qui rend la liste des points
d'entrée ouverts auditable en une recherche.

**Aucun secret dans le dépôt.** `.env` et les `*.pem` sont ignorés par git — ce
qui a été vérifié. `.env.example` documente chaque variable sans valeur.
