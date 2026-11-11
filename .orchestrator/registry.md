# Registry - hermes-hindsight-plus

One row per domain. `session` is the `sessionID` returned by the subagent tool; pass it back to
continue that domain instead of starting from scratch. `files in play` is the current conflict
surface: two tasks may not share it.

| domain | session | last item | files in play | status |
|---|---|---|---|---|
| packaging-docs | ses_e33c7bc79ffez3eDdVzpQUiyjg | HH-1 routine de déploiement (+ revue : ses_e33c1e81affeAWgveyZltpv5wm) | scripts/deploy.ps1, docs/DEPLOY.md, README.md, MACHINE-AGENTS.md, tests/test_deploy_script.py | done, non commité |
| plugin-core | - | HH-2 déploiement v0.5.0 (ops, hors dépôt) | - | done |
| desktop-ui | ses_e33acb6d2ffew4stL44crO1RVC | HH-6 compteur d'en-tête de Memory review | desktop/plugin.js | done, non commité (diff de 9 lignes relu par l'orchestrateur, pas de passe reviewer dédiée) |
| dashboard-backend | - | - | - | - |
