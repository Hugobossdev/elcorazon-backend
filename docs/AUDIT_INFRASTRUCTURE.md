# Audit d'infrastructure du backend — `backend/`

**Date** : 1er septembre 2026 · **Dépôt** : `elcorazon-backend`, branche `main` · **Pile** : Django 5.2 + DRF + PostGIS 17 + Redis 7 + Celery + uvicorn + Nginx, sous Docker Compose · **Stockage** : Cloudinary

Audit de l'infrastructure et de la robustesse, mené sur la pile réelle en
fonctionnement. Aucune donnée n'a été supprimée, aucune migration régénérée,
aucun volume retiré.

---

## Statut

**READY WITH WARNINGS.**

La pile est saine et la séparation développement/production est correcte —
vérifié en ligne, pas seulement dans le code. Six défauts ont été trouvés et
corrigés, dont un de sécurité qui vaut à lui seul l'audit : **la clé privée qui
signe les jetons était embarquée dans l'image Docker**.

Les redémarrages de PostgreSQL avaient deux causes distinctes. L'une est
corrigée dans la configuration ; l'autre est l'environnement lui-même et ne se
corrige pas depuis un fichier de compose — elle est documentée en fin de
rapport avec ce qui la contient.

**Aucune donnée n'a jamais été perdue** : chaque récupération a rejoué quelques
kilo-octets de WAL et retrouvé un état cohérent.

---

## A. Problèmes trouvés

### A1 — CRITIQUE · La clé privée JWT est embarquée dans l'image

**Fichier** : `.dockerignore` (l'omission) · `Dockerfile` (`COPY --chown=corazon:corazon . /app`)

`.dockerignore` écartait `.env`, `.git/`, les caches et les environnements
virtuels — mais pas `secrets/`. Le `COPY . /app` copiait donc dans l'image :

| Fichier | Ce qu'il ouvre |
|---|---|
| `secrets/jwt.pem` | La clé **privée** RS256 de développement |
| `secrets/render/jwt.pem` | La même, pour la **production** |
| `secrets/fcm.json` | Le compte de service Firebase |

Vérifié dans l'image en service :

```
$ docker run --rm --entrypoint sh elcorazon-api:latest -c 'head -c 40 /app/secrets/jwt.pem'
-----BEGIN PRIVATE KEY-----
MIIEvQIBADAN
```

**Impact.** `SIMPLE_JWT["SIGNING_KEY"]` est cette clé. Qui l'obtient forge un
jeton d'accès valide pour n'importe quel compte — client, livreur, personnel du
back-office — sans jamais toucher au serveur ni connaître un mot de passe.
Aucune rotation de mot de passe n'y peut rien, et rien dans les journaux ne
distingue un tel jeton d'un jeton légitime. Le compte de service Firebase, lui,
donne le droit d'émettre des notifications au nom de l'application.

Le registre d'images n'est pas public aujourd'hui ; la gravité tient à ce que
cela ne se voit pas et que la fuite serait totale et silencieuse.

**Gravité : critique.**

### A2 — MAJEUR · L'API était tuée à chaque arrêt, connexions PostgreSQL ouvertes

**Fichier** : `docker-compose.yml`, `services.api.command`

```yaml
command: >
  sh -c "python manage.py migrate --noinput &&
         python manage.py ensure_storage_buckets &&
         uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload"
```

Sans `exec`, le shell reste PID 1 et uvicorn tourne comme son enfant. `sh` ne
relaie pas les signaux : le SIGTERM d'un `docker compose stop` s'arrête sur le
shell, uvicorn ne l'apprend jamais, et au bout des dix secondes de grâce Docker
envoie SIGKILL à tout le groupe.

Constaté avant correction :

```
$ docker compose exec api sh -c 'tr "\0" " " < /proc/1/cmdline'
sh -c python manage.py migrate --noinput && … uvicorn …
```

**Impact.** Les connexions PostgreSQL sont rompues en vol. Le journal du
2026-08-21 en porte la trace exacte :

```
18:05:20 [6484] LOG:  could not send data to client: Broken pipe
18:05:20 [6484] FATAL: connection to client lost
18:05:20 [1]    LOG:  server process (PID 6483) exited with exit code 2
18:05:21 [1]    LOG:  terminating any other active server processes
18:05:28 [1]    LOG:  issuing SIGKILL to recalcitrant children
18:05:46 [1]    LOG:  all server processes terminated; reinitializing
```

Un processus serveur disparu anormalement force le postmaster à **redémarrer
toute l'instance** en récupération : c'est deux des sept récupérations
observées.

**Gravité : majeure.**

### A3 — MAJEUR · Nginx sert 502 sur toute l'API après recréation du conteneur

**Fichier** : `deploy/nginx.conf`, bloc `upstream api { server api:8000; }`

nginx résout le nom `api` **une seule fois**, au chargement de sa
configuration, et garde l'adresse. `docker compose up -d` recrée le conteneur
`api` avec une nouvelle adresse sans toucher à nginx, qui n'a aucune raison
d'être recréé.

Reproduit pendant l'audit :

```
[error] connect() failed (111: Connection refused) while connecting to
upstream: "http://172.18.0.7:8000/api/v1/restaurants/"
```

pendant que le conteneur `api`, parfaitement sain, écoutait sur `172.18.0.6`.

**Impact.** 502 sur `/health/`, sur tout `/api/`, sur les WebSockets — jusqu'à
ce que quelqu'un pense à redémarrer le proxy. Le symptôme accuse l'application
alors que la panne est dans le proxy : c'est exactement le genre de faux
diagnostic qui coûte une demi-journée. Le même piège attend n'importe quel
`docker compose up -d` qui reconstruit l'image de l'API.

**Gravité : majeure.**

### A4 — MAJEUR · `beat` en boucle de redémarrage sur un volume nommé

**Fichiers** : `Dockerfile` (le répertoire manquant) · `docker-compose.prod.yml`, `services.beat.volumes` (`beatdata:/var/lib/celery`)

Un volume nommé monté sur un chemin **absent de l'image** est créé par Docker
en `root:root`. Le processus tourne sous `corazon` (uid 10001) : beat meurt sur
`PermissionError: [Errno 13] … /var/lib/celery/celerybeat-schedule`, et
`restart: unless-stopped` le relance indéfiniment.

Le défaut préexistait dans **`docker-compose.prod.yml`**, où il est latent
faute de déploiement par ce fichier. Il a été rendu visible en portant le même
montage en développement (voir B5) : `RestartCount=87` en quelques minutes.

**Impact.** Aucune tâche planifiée ne part : ni l'expiration des paniers de
groupe, ni celle des points, ni les purges, ni le renouvellement des
abonnements. Silencieusement — le conteneur « redémarre », ce qui ressemble à
de la résilience.

**Gravité : majeure** (au premier déploiement par `docker-compose.prod.yml`).

### A5 — MOYEN · MinIO sur ses identifiants par défaut, écrits dans le dépôt

**Fichier** : `docker-compose.yml`, `services.minio.environment`

```yaml
MINIO_ROOT_USER: minioadmin
MINIO_ROOT_PASSWORD: minioadmin
```

MinIO le signalait lui-même à chaque démarrage :
`WARN: Detected default credentials 'minioadmin:minioadmin'`.

Le commentaire justifiait ce choix par le caractère transitoire du service.
Deux choses le démentent : les ports 9000 et 9001 sont publiés sur **toutes**
les interfaces du poste — pas seulement la boucle locale — et ce service porte
les **pièces d'identité des livreurs** déposées avant la bascule vers
Cloudinary.

**Gravité : moyenne.**

### A6 — MOYEN · Un doublon de téléphone gagné à la course rend 500

**Fichier** : `common/exceptions.py`, `problem_detail_handler`

Le gestionnaire traduisait `IllegalTransition`, `BusinessRuleViolation`,
`ValidationError`, `Http404` et `PermissionDenied` — mais pas `IntegrityError`.
Un heurt de contrainte non rattrapé localement remontait donc en 500.

`RegisterSerializer.validate_phone` interroge la base avant d'écrire ; entre
cette lecture et l'`INSERT`, une seconde inscription au même numéro peut
s'intercaler. La fenêtre est étroite, mais c'est celle qu'ouvre un mobile qui
réémet sa requête sur un réseau lent.

Les deux `duplicate key` du journal (`accounts_user_phone_key`,
`orders_order_reference_key`) ne viennent **pas** de l'API : les valeurs
insérées — `livreur.b@elcorazon.tg`, une commande de référence vide — ne
correspondent à aucun code du dépôt et proviennent d'un script passé à la main
sur la base de développement. La contrainte a fait son travail. Le défaut reste
réel pour autant : le filet manquait.

**Gravité : moyenne.**

### A7 — MOYEN · Dix secondes de grâce pour un point de contrôle qui en prend douze

**Fichier** : `docker-compose.yml`, `services.db`

Docker n'accorde que dix secondes avant SIGKILL. Or sur ce poste un point de
contrôle ordinaire a déjà demandé plus :

```
18:22:57 checkpoint complete: wrote 127 buffers (0.8%); … total=12.451 s
```

Un arrêt tombant sur un tel moment était tué au milieu de son écriture.

**Gravité : moyenne.**

### A8 — MINEUR · La sonde PostgreSQL n'interrogeait pas notre base

`pg_isready -U elcorazon` sans `-d` interroge la base portant le nom de
l'utilisateur. Elle n'existe pas ici, et le serveur répond quand même
« accepting connections » : la sonde passait au vert sur un serveur dont
`elcorazon` aurait été absente.

### A9 — MINEUR · L'échéancier de beat écrit dans l'arbre des sources

`beat` sérialisait son `shelve` dans `/app`, monté sur le dépôt : un
`celerybeat-schedule-wal` de **2,6 Mo** au milieu des sources, une écriture par
tour de boucle sur le montage le plus lent de la pile (la passerelle de
fichiers WSL2), et un échéancier qu'un `git clean` efface.

### A10 — PRÉEXISTANT, non corrigé · La qualité statique n'est pas verte

Trois erreurs `mypy --strict` et deux `ruff check`, dans des fichiers que cet
audit n'a pas touchés :

```
common/storage.py:221  Missing type parameters for generic type "File"
common/storage.py:236  Signature of "url" incompatible with supertype
common/management/commands/migrate_media_to_cloudinary.py:76  "type[Model]" has no attribute "objects"
apps/catalog/management/commands/seed_full_catalog.py:662,663  S310 (urlopen)
tests/common/test_files.py  ruff format
```

Les trois premières datent de la bascule vers Cloudinary. Elles sont hors du
périmètre de cet audit — les corriger demanderait de toucher au stockage, ce
que la consigne écarte — mais la CI est déclarée bloquante sur ces quatre
commandes : elle est donc rouge, ou ne les exécute pas toutes.

### A11 — INFO · L'avertissement Nginx est bénin

```
10-listen-on-ipv6-by-default.sh: info: can not modify
/etc/nginx/conf.d/default.conf (read-only file system?)
```

Le script d'entrée de l'image tente d'ajouter une directive `listen [::]:80`
en modifiant le fichier. Le montage `:ro` l'en empêche, il le signale en
`info:` et passe. nginx démarre et sert normalement — seul l'écouteur IPv6
n'est pas ajouté, sans conséquence : le port est publié en IPv4 et l'amont est
joint par le réseau interne. **Le montage en lecture seule est le bon choix**,
et l'avertissement ne doit pas être « corrigé » en l'ouvrant en écriture.

---

## B. Corrections effectuées

### B1 — `secrets/` sorti du contexte de construction

`.dockerignore` écarte désormais `secrets/` (et `celerybeat-schedule*`).

Rien n'est privé à l'exécution : les deux fichiers de compose montent
`./secrets:/run/secrets:ro`, et les clés JWT sont de toute façon lues dans
l'**environnement** (`JWT_SIGNING_KEY`), pas dans un fichier — c'est ce que
documente `_read_key` dans `config/settings/base.py`. Seul `FCM_CREDENTIALS_PATH`
désigne un fichier, et il pointe sur le montage.

Vérifié après reconstruction des trois images :

```
api : secrets ABSENT (ok)   worker : secrets ABSENT (ok)   beat : secrets ABSENT (ok)
$ docker run --rm --entrypoint sh elcorazon-api:latest -c 'cat /app/secrets/jwt.pem'
cat: /app/secrets/jwt.pem: No such file or directory
```

et le montage reste lisible : `FCM_CREDENTIALS_PATH = /run/secrets/fcm.json | existe = True`.

> **Reste à faire, hors périmètre de cet audit.** Les images déjà construites
> portent la clé. Si l'une d'elles a été publiée dans un registre, ou si le
> poste a été partagé, **la paire de clés doit être renouvelée** — celle de
> développement comme celle de production. Renouveler la clé de signature
> invalide tous les jetons en circulation, ce qui déconnecte les utilisateurs :
> c'est une décision d'exploitation, pas un correctif à passer en silence.

### B2 — `exec` devant uvicorn

`sh -c "… && exec uvicorn …"`. uvicorn devient PID 1, reçoit le SIGTERM et
ferme ses connexions.

Vérifié :

```
$ docker compose exec api sh -c 'tr "\0" " " < /proc/1/cmdline'
/opt/venv/bin/python /opt/venv/bin/uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload
```

L'arrêt complet de la pile est passé de dix secondes de grâce épuisées à
**8,4 s** pour les sept services.

### B3 — Nginx résout `api` à l'exécution

`upstream` remplacé par le résolveur interne de Docker et une adresse portée
par une variable — c'est la variable qui force la résolution par requête :

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;
set $service api;
proxy_pass http://$service:8000$request_uri;
```

Ce que cela coûte : `keepalive 32` disparaît avec le bloc `upstream`. En
développement, une connexion TCP par requête vers uvicorn ne se mesure pas.
**`deploy/nginx.prod.conf` n'est pas modifié** : là-bas les conteneurs ne sont
pas recréés sous le proxy entre deux commandes, et le keepalive y a une valeur.

Vérifié en forçant un changement d'adresse (un conteneur jetable prend la place
libérée) :

| | avant | après |
|---|---|---|
| adresse de `api` | 172.18.0.6 | **172.18.0.9** |
| nginx redémarré ? | — | **non** (`StartedAt` inchangé) |
| `/health/`, `/api/v1/catalog/items/` | — | **200, 200** |

### B4 — `/var/lib/celery` créé dans l'image, propriété de `corazon`

```dockerfile
RUN mkdir -p /var/lib/celery && chown corazon:corazon /var/lib/celery
```

Un volume nommé vide reçoit à son premier montage le contenu **et les droits**
que l'image porte sur ce chemin : il naît `corazon:corazon`. Placé dans l'étage
`runtime`, le correctif vaut pour les deux fichiers de compose.

Le volume déjà créé pendant l'audit a été rendu à `10001:10001` sans être
supprimé. `beat` : `RestartCount=0`, `healthy`.

### B5 — L'échéancier de beat sur un volume nommé

`--schedule /var/lib/celery/celerybeat-schedule` et volume `beatdata`, comme en
production. La sonde de vivacité suit le fichier à son nouvel emplacement.

### B6 — MinIO sur des identifiants lus dans l'environnement

`${MINIO_ROOT_USER:?}` / `${MINIO_ROOT_PASSWORD:?}`, **sans valeur de repli** :
un identifiant absent arrête `docker compose up` en nommant la variable plutôt
que de retomber en silence sur celui que tout le monde connaît. Documenté dans
`.env.example` et `.env.prod.example`, avec la commande qui engendre le mot de
passe. `docker-compose.prod.yml` perd également son repli `:-minioadmin` sur
l'utilisateur.

Changer ces identifiants ne touche pas aux données : MinIO les lit dans son
environnement, jamais dans son volume. L'avertissement a disparu des journaux.

L'architecture de stockage **n'est pas modifiée** — voir la note en fin de
rapport.

### B7 — Les heurts de contrainte deviennent des refus lisibles

`common/exceptions.py` traduit désormais `IntegrityError` :

| Contrainte | Réponse |
|---|---|
| `accounts_user_phone_key` | 409 · `phone_already_exists` · « Ce numéro de téléphone est déjà utilisé. » |
| `accounts_user_email_key` | 409 · `email_already_exists` · « Cette adresse e-mail est déjà utilisée. » |
| autre unicité | 409 · `resource_already_exists` |
| clé étrangère, `NOT NULL`, `CHECK` | **`None`** → 500 journalisé |

Le format suit celui du projet (RFC 9457, `code` stable en `snake_case`,
`detail` traduisible) plutôt que le `{"code": "PHONE_ALREADY_EXISTS"}` proposé :
c'est le contrat que les trois applications Flutter lisent déjà.

Trois décisions valent d'être dites :

* **Le nom de la contrainte est lu dans le diagnostic du pilote**
  (`exc.__cause__.diag.constraint_name`), pas dans le message. Le message
  dépendrait de la langue du serveur et de la version de psycopg.
* **Rien du message d'origine ne sort.** `str(exc)` porte l'instruction SQL et
  le `DETAIL` de PostgreSQL, valeur heurtée comprise — `Key (phone)=(+228…)`.
  Le rendre divulguerait le numéro d'un autre compte à qui tente une
  inscription. Un test l'interdit.
* **Ce qui n'est pas une unicité reste un 500.** Un `CHECK` heurté est un
  défaut de notre côté ; l'habiller en 4xx le ferait sortir des alertes tout en
  laissant croire au client qu'il a mal demandé.

La contrainte PostgreSQL n'est pas touchée. Le chemin normal ne change pas
davantage : le serializer voit le doublon avant l'écriture et rend 400.

### B8 — Délai de grâce et sonde de PostgreSQL

`stop_grace_period: 60s` sur `db`, et `pg_isready -U … -d ${POSTGRES_DB}`.
Soixante secondes ne ralentissent aucun arrêt qui se passe bien : le conteneur
s'éteint dès que PostgreSQL a terminé.

---

## C. Fichiers modifiés

```
.dockerignore                    secrets/ et celerybeat-schedule* écartés
.env.example                     MINIO_ROOT_USER / MINIO_ROOT_PASSWORD documentés
.env.prod.example                idem
Dockerfile                       /var/lib/celery créé, propriété de corazon
docker-compose.yml               exec uvicorn ; stop_grace_period ; pg_isready -d ;
                                 MinIO par variables ; échéancier beat sur volume
docker-compose.prod.yml          MINIO_ROOT_USER sans repli
deploy/nginx.conf                résolution du service api à l'exécution
common/exceptions.py             IntegrityError → problem+json
tests/common/test_integrity.py   nouveau — 8 tests
docs/AUDIT_INFRASTRUCTURE.md     ce document
```

Non modifiés, délibérément : `config/settings/*`, `deploy/nginx.prod.conf`,
`deploy/start-api.sh`, `render.yaml`, `config/celery.py`, tout `apps/`, toutes
les migrations.

---

## D. Tests effectués

### Suite automatisée

| | Avant | Après |
|---|---|---|
| `pytest` | 1303 réussis | **1310 réussis, 1 échec** |
| `ruff check` (fichiers touchés) | — | **PASS** |
| `ruff format --check` (fichiers touchés) | — | **PASS** |
| `mypy --strict common config apps` | 3 erreurs | **3 erreurs** (les mêmes, cf. A10) |

L'échec unique est `tests/social/test_social.py::TestLikesEtCommentaires::test_deux_j_aimes_concurrents_ne_comptent_qu_une_fois`
— un test de course réelle à deux fils. **Intermittent, pas une régression** :
il passait au point de repère, a échoué une fois alors qu'une construction
d'image saturait la machine, et repasse **3 fois sur 3** relancé seul. Il
n'emprunte pas le gestionnaire d'exceptions modifié :
`SocialService.toggle_like` rattrape l'`IntegrityError` lui-même. Le fichier
porte d'ailleurs un test jumeau déterministe, dont le docstring dit que le
concurrent « ne l'attrape qu'avec de la chance ».

### Infrastructure

| Test | Résultat |
|---|---|
| **1** · `docker compose restart` (pile entière) | **PASS** — 7/7 sains, PostgreSQL sans récupération |
| **2** · `docker compose stop` puis `start` | **PASS** — arrêt propre, redémarrage sans récupération |
| **3** · redémarrer Django seul | **PASS** — `db`, `redis`, `worker`, `minio` intacts |
| **4** · redémarrer Redis seul | **PASS** — worker reconnecté, `pong` |
| **5** · redémarrer Nginx seul | **PASS** — rien d'autre redémarré |
| **6** · recréer `api` sous nginx (adresse changée) | **PASS** après B3 — 200 sans toucher au proxy |
| **7** · persistance des données | **PASS** — voir ci-dessous |

### Persistance

Un témoin (`audit_persistance`) et les compteurs métier, relevés avant toute
modification puis après recréation des conteneurs, arrêt/relance et
redémarrage complet :

| | avant | après |
|---|---|---|
| témoins | 1 | **1** |
| utilisateurs | 7 | **7** |
| commandes | 6 | **6** |
| articles | 50 | **50** |

Le volume `pgdata` n'a jamais été touché. Aucun `down -v`, aucun `volume rm`,
aucun `prune`.

> La table `audit_persistance` est le seul objet ajouté à la base. Elle se
> retire par `DROP TABLE audit_persistance;` — commande volontairement **non**
> exécutée.

### Stockage

Les quatre cas demandés, sur le compte Cloudinary réel :

| | Résultat | URL |
|---|---|---|
| image produit | **PASS** | `image/upload/…/elcorazon-products/` — non signée |
| bannière | **PASS** | `image/upload/…/elcorazon-banners/` — non signée |
| avatar utilisateur | **PASS** | `image/upload/…/elcorazon-users/avatars/` — non signée |
| document privé | **PASS** | `raw/download?timestamp=…` — **signée** |

La frontière tient : les trois compartiments publics servent des URL nues,
accrochables par un CDN ; le compartiment des documents ne rend qu'une URL
signée et datée. Les quatre fichiers d'essai ont été retirés.

### Redis et Celery

| | Résultat |
|---|---|
| Séparation des bases | **PASS** — cache et *channels* sur `/0`, courtier sur `/1`, résultats sur `/2` |
| Les 7 tâches sont enregistrées | **PASS** — la liste attendue, sans manque ni ajout |
| Reconnexion après redémarrage de Redis | **PASS** — `Connected to redis://redis:6379/1` |
| Un seul ordonnanceur beat | **PASS** — un conteneur, un `--schedule` |

### Sonde de vivacité

| Appel | Code |
|---|---|
| `http://localhost:8000/health/` (direct) | **200** |
| `http://localhost/health/` (par Nginx) | **200** |
| `http://localhost/api/health/` | **404** |
| `http://localhost/api/v1/catalog/items/` | **200** |

`/health/` est le point d'entrée officiel, et le seul. `/api/health/` n'a
jamais existé : ce chemin tombe dans `location /api/`, est transmis à Django,
qui n'y déclare aucune route. **Aucune incohérence à corriger** — il n'y a pas
deux sondes, il y en a une, plus une adresse erronée appelée un jour par un
client.

La séparation *liveness* / *readiness* est déjà juste : `healthcheck()`
n'accède **ni** à PostgreSQL **ni** à Redis, ce qui est le bon choix pour une
sonde de vivacité — une sonde qui échoue parce que la base est momentanément
indisponible ferait redémarrer en boucle des conteneurs sains. Une sonde de
*readiness* distincte n'existe pas ; elle n'est nécessaire que le jour où un
orchestrateur retirera le service du routage sans le tuer.

### Séparation développement / production — vérifiée **en ligne**

| Épreuve | Résultat |
|---|---|
| `DJANGO_SETTINGS_MODULE` (compose) | `config.settings.dev` en dev, `config.settings.prod` en prod |
| `render.yaml` | `config.settings.prod` |
| Garde-fou de `dev.py` | présent — refuse de démarrer si `RENDER` est posée |
| `https://elcorazon-backend.onrender.com/health/` | **200** |
| Une erreur fuit-elle une trace ? | **non** — `{"type":…,"code":"not_found"}`, RFC 9457 |
| En-têtes de sécurité | `strict-transport-security: max-age=31536000; includeSubDomains; preload`, `x-frame-options: DENY`, `x-content-type-options: nosniff`, `referrer-policy: same-origin` |

La production tourne bien sur `prod.py`, avec `DEBUG=False`. Le
`config.settings.dev` observé dans les journaux historiques appartient au
passé : le garde-fou l'interdit désormais.

### uvicorn

`--reload` n'existe qu'en développement. `docker-compose.prod.yml` lance
gunicorn avec des *workers* uvicorn (`--timeout 60`, `--graceful-timeout 30`) ;
`deploy/start-api.sh`, utilisé par Render, lance `exec uvicorn` sans
rechargement. Aucun serveur concurrent. Rien à corriger.

---

## E. PostgreSQL

### Pourquoi il redémarrait

**Deux causes distinctes**, et la première est de loin la plus fréquente.

**E1 — La machine virtuelle disparaît sous les conteneurs (5 des 7 cas).**

Ce n'est pas une hypothèse. Les sept conteneurs portent le **même**
`FinishedAt`, à la microseconde près :

```
db     Finished=2026-09-01T11:10:02.476611444Z
redis  Finished=2026-09-01T11:10:02.476543944Z
api    Finished=2026-09-01T11:10:02.476542744Z
worker Finished=2026-09-01T11:10:02.471869552Z
beat   Finished=2026-09-01T11:10:02.476539644Z
nginx  Finished=2026-09-01T11:10:02.471898852Z
minio  Finished=2026-09-01T11:10:02.471891052Z
```

Sept arrêts indépendants ne coïncident pas ainsi. C'est le démon Docker qui
réconcilie son état au redémarrage après que la machine virtuelle WSL2 a
disparu — arrêt de Docker Desktop, extinction ou veille de Windows. Et le
journal de PostgreSQL le confirme par ce qu'il **ne** contient pas : entre le
dernier point de contrôle et la bannière de démarrage suivante, **aucune ligne
d'arrêt**. Pas de `received fast shutdown request`, pas de `shutting down`. Le
serveur n'a jamais reçu de signal ; le sol s'est dérobé.

`RestartCount=0` partout : la politique `unless-stopped` n'a pas joué, c'est le
démon qui a tout relancé à son propre démarrage.

**Ce que la configuration ne pouvait pas causer.** L'image `postgis` déclare
déjà `STOPSIGNAL SIGINT` — vérifié : `StopSignal=SIGINT` — c'est-à-dire l'arrêt
*rapide*, et non l'arrêt *smart* qui attendrait la fin des connexions. Le seul
arrêt propre du journal conservé le prouve de bout en bout :

```
2026-08-21 16:50:04 [1] LOG:  received fast shutdown request
2026-08-21 16:50:05 [33] LOG: shutting down
2026-08-21 16:50:05 [33] LOG: checkpoint starting: shutdown immediate
2026-08-21 16:50:05 [1] LOG:  database system is shut down
```

`docker compose stop` fonctionnait donc déjà correctement. Ce qui manquait,
c'était l'occasion de s'en servir.

**E2 — L'API tuée avec ses connexions ouvertes (2 des 7 cas).** Voir A2. C'est
la cause qui était réellement dans le fichier de compose, et elle est corrigée.

**Facteur aggravant : la machine est à court de mémoire.** Le poste porte 8 Go,
dont environ 1 Go libre au moment de l'audit. Il n'y a pas de `.wslconfig` : la
machine virtuelle prend donc le défaut de WSL2, la moitié de la mémoire vive
(Docker rapporte 3,77 Gio). Les conteneurs n'en consomment que ~724 Mio — le
manque est **côté hôte**, pas dans la machine virtuelle. Windows pagine, la
machine virtuelle gèle par intermittence, et PostgreSQL en porte les marques :

* un point de contrôle de **127 tampons** (1 Mo) qui demande `12,451 s` ;
* `syncing data directory (pre-fsync), elapsed time: 20.83 s` ;
* `issuing SIGKILL to recalcitrant children` — les processus fils n'ont pas
  répondu au SIGQUIT en cinq secondes.

C'est ce gel qui transforme un arrêt un peu lent en arrêt tué.

### Est-ce corrigé ?

**La cause interne : oui.** Trois cycles complets menés après correction, tous
propres :

```
2026-09-01 12:12:10 [1] LOG:  received fast shutdown request
2026-09-01 12:12:10 [1] LOG:  aborting any active transactions
2026-09-01 12:12:10 [27] LOG: shutting down
2026-09-01 12:12:10 [27] LOG: checkpoint starting: shutdown immediate
2026-09-01 12:12:10 [1] LOG:  database system is shut down
…
2026-09-01 12:12:31 [29] LOG: database system was shut down at 2026-09-01 12:12:10 UTC
2026-09-01 12:12:31 [1] LOG:  database system is ready to accept connections
```

`database system was shut down at …` remplace `was interrupted` / `was not
properly shut down` / `automatic recovery in progress`. Le démarrage prend
**0,1 s** au lieu des dix à vingt secondes de resynchronisation.

**La cause externe : non, et elle ne peut pas l'être depuis un fichier de
compose.** Tant que la machine virtuelle est arrêtée sans que les conteneurs le
soient, PostgreSQL repartira en récupération. Trois mesures la contiennent, et
aucune n'a été appliquée — elles touchent la machine de l'utilisateur, pas le
dépôt :

1. **Arrêter la pile avant d'éteindre le poste** — `docker compose stop` depuis
   `backend/`. C'est la mesure qui règle le cas, et la seule qui soit gratuite.
2. **Un `C:\Users\<vous>\.wslconfig`** bornant la machine virtuelle, pour que
   Windows garde de quoi respirer :

   ```ini
   [wsl2]
   memory=3GB
   processors=2
   swap=2GB
   ```

   Il ne prend effet qu'après `wsl --shutdown`, **ce qui arrête les
   conteneurs** — à faire au calme, après un `docker compose stop`.
3. **Désactiver le démarrage rapide de Windows**, qui hiberne au lieu
   d'éteindre et gèle la machine virtuelle sans prévenir.

### Les données sont-elles persistantes ?

**Oui**, et elles l'étaient déjà. `pgdata` est un volume nommé, extérieur au
cycle de vie du conteneur ; les recréations le remontent tel quel. Vérifié à
trois reprises pendant l'audit (voir D). Aucune récupération n'a jamais perdu
quoi que ce soit : chaque `redo` a rejoué zéro à quelques kilo-octets et s'est
terminé sur `redo done`.

### La récupération se produit-elle encore ?

Plus depuis la correction, sur les trois cycles éprouvés. Elle **reviendra** au
prochain arrêt brutal de Docker Desktop ou de Windows, tant que les mesures
ci-dessus ne sont pas prises. Ce n'est alors pas un incident : c'est PostgreSQL
qui fait exactement ce pour quoi le WAL existe.

---

## F. Aptitude à la production

### **READY WITH WARNINGS**

Ce qui est solide, et qui n'a pas eu à être touché : la séparation
développement/production, avec un garde-fou qui refuse de démarrer `dev.py` sur
l'hébergeur ; les garde-fous de `prod.py`, qui font échouer le démarrage sur
une variable manquante plutôt que de servir une configuration dégradée ; le
refus par défaut de DRF ; la limitation de débit sur toutes les routes, avec un
comportement déclaré en panne de cache ; la séparation des bases Redis ; le
format d'erreur RFC 9457 ; la sonde de vivacité sans accès base ; l'absence de
tout secret dans l'historique git — vérifiée.

Les réserves :

| # | Réserve | Ce qu'elle demande |
|---|---|---|
| 1 | **Les clés JWT ont voyagé dans les images.** Le correctif ferme la fuite pour l'avenir ; il ne révoque rien. | Décider du renouvellement des deux paires. Renouveler déconnecte tous les utilisateurs — c'est une décision d'exploitation. |
| 2 | **La qualité statique est rouge** (A10) : 3 erreurs mypy, 2 ruff, 1 fichier à reformater. | Hors périmètre ici. À traiter avant de déclarer la CI fiable. |
| 3 | **Le déploiement Render est configuré à la main** et ignore les `envVars` du blueprint. Trois pannes en ont déjà découlé. | Reposer les variables du blueprint, ou recréer le service depuis `render.yaml`. |
| 4 | **MinIO tourne encore.** Il ne sert qu'à relire les fichiers d'avant Cloudinary. | Passer `migrate_media_to_cloudinary`, puis retirer le service, le volume et les ports. |
| 5 | **Le poste de développement est à court de mémoire** (E). | Les trois mesures de la section E. Sans effet sur la production. |
| 6 | **`docker-compose.prod.yml` n'a jamais été déployé.** A4 y dormait ; d'autres surprises peuvent y dormir encore. | Un déploiement d'essai complet avant de s'y fier. |

Aucune de ces réserves n'est bloquante pour l'environnement de développement,
qui tourne et se redémarre proprement. La première demande une décision avant
toute mise en production sérieuse.

---

## Une correction au cahier des charges de cet audit

La consigne demandait l'architecture suivante :

```
Développement local : Django → MinIO
Production          : Django → Cloudinary
```

**Ce n'est pas l'architecture du projet, et l'écart est délibéré.** L'ADR-011 a
tranché en sens inverse le 21 août : **Cloudinary des deux côtés**, au motif
qu'« un développement qui écrit ailleurs qu'en production ne rencontre les
écarts du fournisseur qu'au déploiement, c'est-à-dire trop tard ». Le code le
dit et un test d'architecture l'impose : aucun module hors `common/storage.py`
n'importe `cloudinary`, et plus aucune variable `S3_*` n'existe.

Rétablir MinIO en développement serait donc une **régression d'architecture**,
pas une correction — et la consigne demandait par ailleurs de ne rien
réécrire. Le service MinIO a donc été **sécurisé** (B6) sans être remis dans le
chemin d'écriture : il reste ce que son commentaire annonce, un vestige qu'on
lit le temps d'une migration.
