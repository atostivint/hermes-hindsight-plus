# Inbox - hermes-hindsight-plus

File de travail réelle. Une ligne par item ; `done` = mergé et vérifié, `ready` = prêt à partir,
`blocked` = attend une info de l'utilisateur. Les décisions qui expliquent ces items sont dans
`decisions.md`.

| id | item | domaine | état | preuve |
|---|---|---|---|---|
| HH-1 | Routine de déploiement : `scripts/deploy.ps1` + `docs/DEPLOY.md` + section README + faits machine + 3 tests | packaging-docs | done | 100 passed ; `ruff check tests/test_deploy_script.py` clean ; `hermes plugins validate .` ok ; dry-run exit 0 sous 5.1 et pwsh 7 ; revue passée (10 findings corrigés) |
| HH-2 | Déploiement réel sur Oracle + Windows | ops | done | v0.5.1 poussée sur `origin/main` (5c55001) puis déployée par `scripts/deploy.ps1 -Target all` : `hermes plugins show` v0.5.1 des deux côtés, sha256 identiques paquet/publié sur les deux hôtes, gateway rechargé |
| HH-3 | Git : `main` locale 3 commits derrière `origin/main` ; 8 branches mergées à supprimer ; worktree `.paseo/worktrees/2b62p0ub/huge-impala` obsolète | packaging-docs | blocked | attend l'accord de l'utilisateur (fast-forward + ménage, aucune écriture faite) |
| HH-4 | `python -m pytest -q` échoue sans `--basetemp` (ACL cassée sur `%TEMP%\pytest-of-elekt`, 48 erreurs PermissionError) | env | known | contournement : `--basetemp=<dir neuf>` → 100 passed ; documenté dans MACHINE-AGENTS.md §5 |
| HH-5 | `AGENTS.md` et `POLICY.md` annoncent encore « 97 tests » (100 depuis `tests/test_deploy_script.py`) | packaging-docs | ready | POLICY.md mis à jour par l'orchestrateur ; `AGENTS.md` demande un worker (fichier applicatif) |
| HH-6 | En-tête Memory review : `100 souvenirs sur 21 a revoir` (taille de page vs total, « à revoir » compté sur une autre page, libellé inversé) | desktop-ui | done | `node --check` ok ; `hermes plugins validate .` ok ; 100 passed ; nouvelle ligne `100 / 245 souvenirs · 21 à revoir sur cette page` |
| HH-7 | « Il me manque des informations » dans la page de revue : quelle info exactement (invalidés ? date ? verdict ? filtres ?) | desktop-ui | blocked | attend la précision de l'utilisateur, pas d'interprétation |
| HH-8 | Pane injections introuvable côté utilisateur : replié par défaut (`defaultCollapsed: true`), onglet « Memory » à droite, exige un chat ouvert | desktop-ui | known | à confirmer visuellement : l'utilisateur voit-il l'onglet ? |
| HH-9 | `deploy.ps1` : le heredoc envoyé à `ssh` partait avec un BOM, donc `set -euo pipefail` ne s'exécutait pas sur Oracle | packaging-docs | done | corrigé (`$OutputEncoding` sans préambule + ligne 1 `:` blindée) ; run réel oracle exit 0, `hermes plugins show` v0.5.1, sha256 identiques, aucune ligne `command not found` |
| HH-10 | File de revue globale : scan de toute la banque, tri global, `review_only`, `matched`/`to_review`, `q` côté serveur | dashboard-backend | en cours | worker ses_e33724ce1ffesQ3EfCjRqJd8yc ; attendu : tests nommés (tri inter-pages, filtre, troncature, offset, q) |
| HH-11 | Registre local `review-decisions.json` + exclusion des « Correct » de la file (retour si texte modifié ou après 90 j) | dashboard-backend | pending | démarre après HH-10 (mêmes fichiers) |
| HH-12 | Mode cartes : une carte, Correct / Faux / Je ne sais pas, raccourcis ←/→/Espace, Faux = révocation douce | desktop-ui | en cours | 1re passe (ses_e336c786fffezUDuQB6R1SY2CQ) : styles + `ReviewCard` + requête posés, puis limite d'étapes ; reprise ses_e33649a4effekZJxlzCIsJPdIN pour le câblage |
| HH-13 | Revue de HH-10/11 : `truncated` faux, plafond d'appels HTTP, prédicat dupliqué, ids vides, `total` non entier, `le=200` en dur | dashboard-backend | en cours | worker ses_e3366b99bffepRBCCaFUmlKnvc, verdict fix-then-ship appliqué |
| HH-14 | Page : clamper `offset` quand `matched` rétrécit après une décision (sinon page vide avec `matched > 0`) | desktop-ui | ready | à appliquer après HH-12 (même fichier) |
| HH-15 | Revue du mode cartes : « Faux » échoue en 400 (raison refusée par `check_reason`), double-tir, Espace qui vole le focus, selects inertes, scan par frappe, impasse de pagination, progression et en-tête faux | desktop-ui | en cours | 11 points, worker ses_e335a3e8affeDw10aftpJg8NJI |
| HH-16 | Nits backend : `needs_review` teste la véracité au lieu de `> 0`, route décisions sans contrôle d'existence (docstring), `_REVIEW_MAX_PAGE` évalué à l'import + `getattr` mort | dashboard-backend | en cours | worker ses_e335a0d62ffefr2AenhPf1Z9iL |
| HH-17 | Release 0.6.0 : `plugin.yaml`, `dashboard/manifest.json`, README (section Memory review + cartes) | packaging-docs | done | 129 passed, validate ok, diff de 3 fichiers |

<!-- Exemple de ligne (à supprimer au premier vrai item) :
| HH-1 | Un modèle absent renvoie 404 : le hook crée le modèle une fois puis n'injecte rien au tour courant | plugin-core | ready | pytest tests/test_plugin.py -k create_missing + git diff --stat |
-->
