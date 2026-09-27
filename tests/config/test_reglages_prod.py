"""Garde-fous du chargement des réglages de production.

Ces tests existent parce qu'un défaut est passé, et qu'il était **silencieux**.

`.env.prod.example` montait les identifiants Firebase — `FCM_CREDENTIALS_PATH`,
`FCM_PROJECT_ID`, `FCM_TIMEOUT_SECONDS` — et ne déclarait pas `PUSH_BACKEND`.
Le défaut de `base.py` s'appliquait donc : `ConsolePushBackend`, qui journalise
et déclare **tous les jetons livrés**. Rien n'échouait, rien n'était retenté,
aucune métrique ne bougeait. Un déploiement paraissait configuré et n'envoyait
rien — pas une offre de course à un livreur, pas une alerte de commande au
personnel, pas une notification de livraison à un client.

C'est le genre de défaut qu'aucun test métier n'attrape : la couche est
substituée dans les tests, et le mauvais branchement ne se lit que dans un
fichier d'environnement. Il se vérifie donc ici, sur le chargement lui-même.

## Pourquoi un sous-processus

Importer `config.settings.prod` remplace les réglages du processus. Le faire
dans la session pytest laisserait la suite tourner sur une configuration de
production à demi chargée. Le sous-processus isole, et reproduit le seul instant
qui compte : le chargement du module de réglages sur un environnement donné.

Il isole aussi de `backend/.env`, que `decouple` trouverait autrement : la
variable posée dans l'environnement l'emporte sur le fichier, ce qui rend le test
identique sur un poste de développement et en CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

#: Racine du projet serveur — `manage.py` y vit.
RACINE = Path(__file__).resolve().parents[2]

CONSOLE = "apps.notifications.push.ConsolePushBackend"
FCM = "apps.notifications.fcm.FirebaseCloudMessagingBackend"

BAC_A_SABLE = "apps.payments.gateway.SandboxGateway"
PAYDUNYA = "apps.payments.paydunya.PayDunyaGateway"

#: Le minimum qu'exige `prod.py` pour aller jusqu'au bout de son chargement.
#:
#: Les quatre premières sont lues sans repli par le garde-fou existant ; les
#: quatre suivantes par la section messagerie. Aucune n'a de valeur réaliste :
#: ce qu'on vérifie ici est la **présence** d'un contrôle, pas la validité des
#: identifiants, que seul l'usage peut dire.
ENVIRONNEMENT_MINIMAL = {
    "DJANGO_SETTINGS_MODULE": "config.settings.prod",
    "DJANGO_SECRET_KEY": "pour-le-test-seulement",
    "JWT_SIGNING_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    "JWT_VERIFYING_KEY": "-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----",
    "POSTGRES_PASSWORD": "motdepasse",
    "EMAIL_HOST": "smtp.exemple.invalid",
    "EMAIL_HOST_USER": "no-reply@exemple.invalid",
    "EMAIL_HOST_PASSWORD": "motdepasse",
    "DEFAULT_FROM_EMAIL": "El Corazón <no-reply@exemple.invalid>",
    # Les deux réglages de paiement suivent la même logique que les précédents :
    # `prod.py` refuse de démarrer sans eux. Ils entrent donc dans le minimum,
    # avec des valeurs qui ne valent rien — ce qu'on vérifie est la présence
    # d'un contrôle, pas la validité d'identifiants.
    "PAYDUNYA_GATEWAY": PAYDUNYA,
    "PAYMENT_WEBHOOK_SECRET": "pour-le-test-seulement",
    # Le garde-fou des notifications push s'exécute **avant** ceux du paiement.
    # Absent d'ici, il n'était satisfait que par le `backend/.env` du poste,
    # que `decouple` relit depuis `RACINE` : les tests de paiement passaient en
    # local et échouaient en CI, où ce fichier n'existe pas, sur le refus de la
    # console — sans jamais atteindre le contrôle qu'ils visent. Les tests du
    # push le surchargent.
    "PUSH_BACKEND": FCM,
}


def charger_prod(**surcharges: str) -> subprocess.CompletedProcess[str]:
    """Charge les réglages de production dans un sous-processus neuf.

    L'import du module de réglages suffit, et `django.setup()` serait de trop :
    le garde-fou est du code de niveau module, il s'exécute donc à l'import.
    Aller jusqu'au chargement des applications ferait en outre dépendre ce test
    de la présence de GDAL sur la machine, ce qui n'a rien à voir avec ce qu'il
    vérifie.
    """
    environnement = {
        # `PATH` et `SYSTEMROOT` sont nécessaires au démarrage de l'interpréteur
        # sous Windows ; le reste de l'environnement est volontairement écarté
        # pour que le test ne dépende pas du poste qui l'exécute.
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        **ENVIRONNEMENT_MINIMAL,
        **surcharges,
    }
    # `S603` signale un appel `subprocess` sur entrée non fiable. Il n'y en a
    # pas ici : l'exécutable est celui de l'interpréteur courant et la commande
    # est un littéral. Ce que le test fait varier est l'environnement, pas la
    # ligne de commande.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import config.settings.prod"],
        cwd=RACINE,
        env=environnement,
        capture_output=True,
        text=True,
        check=False,
    )


class TestServiceDeNotificationPush:
    def test_le_defaut_declare_par_base_est_la_console(self) -> None:
        """L'absence de la variable **vaut** la console.

        C'est la moitié du défaut d'origine : le gabarit de production ne taisait
        pas un réglage neutre, il en taisait un dont le repli est inoffensif en
        développement et muet en exploitation. Le vérifier ici donne son sens au
        refus testé juste après.

        Lu dans la source plutôt que sur `settings.PUSH_BACKEND` : la valeur
        effective dépend du `.env` du poste, quand ce qu'on affirme ici est le
        **repli déclaré**, qui n'en dépend pas.
        """
        source = (RACINE / "config" / "settings" / "base.py").read_text(encoding="utf-8")

        assert f'"PUSH_BACKEND", default="{CONSOLE}"' in source, (
            "Le repli de PUSH_BACKEND n'est plus la console : le refus de "
            "`prod.py` ne couvre alors plus l'absence de la variable."
        )

    def test_la_console_est_refusee_en_production(self) -> None:
        resultat = charger_prod(PUSH_BACKEND=CONSOLE)

        assert resultat.returncode != 0, (
            "La production a démarré sur ConsolePushBackend : "
            "elle n'enverra aucune notification et déclarera tout livré."
        )
        assert "PUSH_BACKEND" in resultat.stderr
        # Le message doit dire quoi poser, pas seulement ce qui ne va pas : c'est
        # lu par quelqu'un qui déploie, souvent la nuit.
        assert FCM in resultat.stderr

    def test_le_connecteur_fcm_passe(self) -> None:
        """Le contrôle ne doit pas refuser ce qu'il est censé exiger."""
        resultat = charger_prod(PUSH_BACKEND=FCM)

        assert resultat.returncode == 0, resultat.stderr[-2000:]

    @pytest.mark.parametrize("gabarit", [".env.prod.example", "render.yaml"])
    def test_les_gabarits_de_deploiement_posent_le_connecteur(self, gabarit: str) -> None:
        """Les deux fichiers dont on part pour déployer doivent porter la ligne.

        Le garde-fou de `prod.py` transforme l'oubli en échec de démarrage ; ces
        deux assertions font qu'on ne le découvre pas au déploiement.
        """
        contenu = (RACINE / gabarit).read_text(encoding="utf-8")

        assert "PUSH_BACKEND" in contenu, f"{gabarit} ne déclare pas PUSH_BACKEND."
        assert FCM in contenu, f"{gabarit} ne branche pas le connecteur FCM."


class TestConnecteurDePaiement:
    """Le même piège que le push, en plus coûteux.

    `SandboxGateway` n'est pas une maquette inerte : il ouvre des paiements,
    accepte des notifications et solde des commandes. Il diffère du vrai sur les
    deux points qui comptent — il n'ouvre aucune facture chez le prestataire,
    donc n'encaisse jamais ; et son `parse` lit le statut **dans le corps posté**
    au lieu de le relire chez PayDunya.

    Le blueprint de production ne déclarait aucune variable de paiement. Le
    déploiement encaissait donc par le bac à sable, sans que rien ne le signale.
    """

    def test_le_defaut_declare_par_base_est_le_bac_a_sable(self) -> None:
        """L'absence de la variable **vaut** le bac à sable.

        C'est la moitié du défaut : le gabarit ne taisait pas un réglage neutre,
        il en taisait un dont le repli encaisse zéro franc en déclarant que
        c'est réglé.
        """
        source = (RACINE / "config" / "settings" / "base.py").read_text(encoding="utf-8")

        assert f'"PAYDUNYA_GATEWAY", default="{BAC_A_SABLE}"' in source, (
            "Le repli de PAYDUNYA_GATEWAY n'est plus le bac à sable : le refus "
            "de `prod.py` ne couvre alors plus l'absence de la variable."
        )

    def test_le_bac_a_sable_est_refuse_en_production(self) -> None:
        resultat = charger_prod(PAYDUNYA_GATEWAY=BAC_A_SABLE)

        assert resultat.returncode != 0, (
            "La production a démarré sur SandboxGateway : elle n'encaissera rien "
            "et croira sur parole le statut posté dans les notifications."
        )
        assert "PAYDUNYA_GATEWAY" in resultat.stderr
        # Le message doit dire quoi poser : il est lu par quelqu'un qui déploie.
        assert PAYDUNYA in resultat.stderr

    def test_l_absence_de_variable_est_refusee(self) -> None:
        """Le cas réellement survenu : la variable n'existait nulle part.

        Distinct du précédent — celui-ci passe une valeur, celui-là n'en passe
        aucune et laisse le repli de `base.py` s'appliquer.
        """
        environnement = dict(ENVIRONNEMENT_MINIMAL)
        del environnement["PAYDUNYA_GATEWAY"]

        resultat = subprocess.run(  # noqa: S603
            [sys.executable, "-c", "import config.settings.prod"],
            cwd=RACINE,
            env={
                "PATH": os.environ.get("PATH", ""),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                **environnement,
            },
            capture_output=True,
            text=True,
            check=False,
        )

        assert resultat.returncode != 0
        assert "PAYDUNYA_GATEWAY" in resultat.stderr

    def test_le_connecteur_paydunya_passe(self) -> None:
        """Le contrôle ne doit pas refuser ce qu'il est censé exiger."""
        resultat = charger_prod(PAYDUNYA_GATEWAY=PAYDUNYA)

        assert resultat.returncode == 0, resultat.stderr[-2000:]

    @pytest.mark.parametrize("gabarit", [".env.prod.example", "render.yaml"])
    def test_les_gabarits_de_deploiement_posent_le_connecteur(self, gabarit: str) -> None:
        """Les deux fichiers dont on part pour déployer doivent porter la ligne.

        `render.yaml` ne la portait pas du tout : c'est par là que le bac à
        sable est arrivé en production.
        """
        contenu = (RACINE / gabarit).read_text(encoding="utf-8")

        assert "PAYDUNYA_GATEWAY" in contenu, f"{gabarit} ne déclare pas PAYDUNYA_GATEWAY."
        assert PAYDUNYA in contenu, f"{gabarit} ne branche pas le connecteur PayDunya."


class TestSecretDesNotifications:
    """`PAYMENT_WEBHOOK_SECRET` s'oublie en restant **vide**.

    Un HMAC-SHA256 calculé avec une clé vide se recalcule par quiconque lit ce
    dépôt. Et comme `POST /payments/{order}/initiate/` rend au client sa propre
    `provider_reference`, il ne restait rien à deviner : le client pouvait
    signer lui-même une notification « encaissé ».

    Le secret reste nécessaire avec PayDunya branché — les espèces et le
    portefeuille passent par `SandboxGateway`, dont `authenticate` s'en sert.
    """

    def test_un_secret_vide_est_refuse(self) -> None:
        resultat = charger_prod(PAYMENT_WEBHOOK_SECRET="")

        assert resultat.returncode != 0, (
            "La production a démarré sans secret de webhook : n'importe qui peut "
            "forger une notification de paiement."
        )
        assert "PAYMENT_WEBHOOK_SECRET" in resultat.stderr

    def test_un_secret_renseigne_passe(self) -> None:
        resultat = charger_prod(PAYMENT_WEBHOOK_SECRET="une-valeur-longue-et-aleatoire")

        assert resultat.returncode == 0, resultat.stderr[-2000:]

    @pytest.mark.parametrize("gabarit", [".env.prod.example", "render.yaml"])
    def test_les_gabarits_declarent_le_secret(self, gabarit: str) -> None:
        """`.env.prod.example` le taisait, `render.yaml` aussi."""
        contenu = (RACINE / gabarit).read_text(encoding="utf-8")

        assert "PAYMENT_WEBHOOK_SECRET" in contenu, (
            f"{gabarit} ne déclare pas PAYMENT_WEBHOOK_SECRET."
        )


class TestOriginesAutorisees:
    """`CORS_ALLOW_LOCAL_DEV_ORIGINS` était absente, donc **vraie** par défaut.

    L'API de production acceptait donc les origines `http://localhost:<port>`.
    C'était délibéré tant qu'aucun back-office n'avait d'adresse stable ; ça ne
    l'est plus dès qu'une l'a, et un défaut permissif ne se remarque pas.
    """

    def test_le_blueprint_ferme_explicitement_le_repli_local(self) -> None:
        contenu = (RACINE / "render.yaml").read_text(encoding="utf-8")

        assert "CORS_ALLOW_LOCAL_DEV_ORIGINS" in contenu, (
            "render.yaml ne déclare pas CORS_ALLOW_LOCAL_DEV_ORIGINS : le repli "
            "de `prod.py` s'applique, et il autorise localhost."
        )
