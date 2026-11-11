# Décisions de l'utilisateur - hermes-hindsight-plus

Une ligne par arbitrage. C'est la mémoire qui survit aux compactions, aux redémarrages et aux agents
qui relisent le dépôt. Quand une décision change, on ne l'efface pas : on la barre et on ajoute la
nouvelle, avec la date. Format : `date - décision - pourquoi - où c'est appliqué`.

2026-11-11 - "Déployer une version" = Oracle (demi agent + demi desktop) ET Desktop Windows (les deux demis) - l'utilisateur veut que chaque nouvelle version arrive sur son agent Oracle et sur son app bureau, sans y penser - `scripts/deploy.ps1`, `docs/DEPLOY.md`.
2026-11-11 - Sur Windows l'install v0.3.0 (Source: user, sans provenance) est remplacée par une install git (`hermes plugins install <url> --force --yes-deps --enable`) - une seule fois, ensuite `update` fonctionne - fait en séance, v0.5.0 installée.
2026-11-11 - Reloads et redémarrages automatiques autorisés après un déploiement - le nouveau code doit être actif immédiatement - `hermes plugins install` recharge déjà le gateway.
2026-11-11 - `hermes plugins update <nom>` ne suffit pas à déployer : le demi-desktop sans `.hermes-package.json` n'est jamais réécrit (traité comme plugin de l'utilisateur) - mesuré sur Oracle, deux installs et un `update` laissaient le `plugin.js` du 5 nov en place - le déploiement passe par `install --force` puis réparation du demi-desktop (copie du `desktop/plugin.js` du paquet, backup horodaté).
2026-11-11 - L'utilisateur demande explicitement le commit + push puis le déploiement ("est-ce que tu pourrais le pousser s'il te plaît ?") - l'autorisation de commit/push est donnée par demande, elle ne se présume pas - `origin/main` 5c55001 = `release: 0.5.1`, déployé sur Oracle et Windows le même jour.
2026-11-11 - Mode cartes de revue validé : "Correct" sort le souvenir de la file, "Faux" le révoque upstream, "Je ne sais pas" le laisse dans la file - c'est la seule forme qui vide la file sans écrire de faux positif dans Hindsight - `desktop/plugin.js` (cartes), registre local `review-decisions.json`, pas d'écriture sauf "Faux".
2026-11-11 - "Correct" persiste dans un registre local et redevient une carte si le texte change ou après 90 jours - sinon la même carte revient chaque jour et la file ne se vide jamais - `review.py` + `dashboard/plugin_api.py`.
2026-11-11 - Les verdicts de "Check with Hermes" restent un avis affiché, jamais appliqué automatiquement - déjà la règle du dépôt, reconfirmée - page Memory review.
