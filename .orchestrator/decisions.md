# Décisions de l'utilisateur - hermes-hindsight-plus

Une ligne par arbitrage. C'est la mémoire qui survit aux compactions, aux redémarrages et aux agents
qui relisent le dépôt. Quand une décision change, on ne l'efface pas : on la barre et on ajoute la
nouvelle, avec la date. Format : `date - décision - pourquoi - où c'est appliqué`.

2026-11-11 - "Déployer une version" = Oracle (demi agent + demi desktop) ET Desktop Windows (les deux demis) - l'utilisateur veut que chaque nouvelle version arrive sur son agent Oracle et sur son app bureau, sans y penser - `scripts/deploy.ps1`, `docs/DEPLOY.md`.
2026-11-11 - Sur Windows l'install v0.3.0 (Source: user, sans provenance) est remplacée par une install git (`hermes plugins install <url> --force --yes-deps --enable`) - une seule fois, ensuite `update` fonctionne - fait en séance, v0.5.0 installée.
2026-11-11 - Reloads et redémarrages automatiques autorisés après un déploiement - le nouveau code doit être actif immédiatement - `hermes plugins install` recharge déjà le gateway.
2026-11-11 - `hermes plugins update <nom>` ne suffit pas à déployer : le demi-desktop sans `.hermes-package.json` n'est jamais réécrit (traité comme plugin de l'utilisateur) - mesuré sur Oracle, deux installs et un `update` laissaient le `plugin.js` du 5 nov en place - le déploiement passe par `install --force` puis réparation du demi-desktop (copie du `desktop/plugin.js` du paquet, backup horodaté).
