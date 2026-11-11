# Registry - hermes-hindsight-plus

One row per domain. `session` is the `sessionID` returned by the subagent tool; pass it back to
continue that domain instead of starting from scratch. `files in play` is the current conflict
surface: two tasks may not share it.

| domain | session | last item | files in play | status |
|---|---|---|---|---|
| packaging-docs | ses_e33c7bc79ffez3eDdVzpQUiyjg (bump : ses_e33a9832fffepDHZucImyJiqlt, BOM : ses_e33a6a8f0ffeUFz3TkTsWoEzS8) | HH-9 garde `set -e` perdu par le BOM + HH-1 routine + HH-5 version | scripts/deploy.ps1, docs/DEPLOY.md, README.md, MACHINE-AGENTS.md, plugin.yaml, dashboard/manifest.json, tests/test_deploy_script.py | done, poussé (5c55001, db19013) |
| plugin-core | - | HH-2 déploiement v0.5.1 (ops, hors dépôt) | - | done |
| desktop-ui | ses_e33acb6d2ffew4stL44crO1RVC | HH-6 compteur d'en-tête de Memory review | desktop/plugin.js | done, poussé (5c55001) |
| dashboard-backend | - | - | - | - |
