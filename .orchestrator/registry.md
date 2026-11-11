# Registry - hermes-hindsight-plus

One row per domain. `session` is the `sessionID` returned by the subagent tool; pass it back to
continue that domain instead of starting from scratch. `files in play` is the current conflict
surface: two tasks may not share it.

| domain | session | last item | files in play | status |
|---|---|---|---|---|
| packaging-docs | ses_e33c7bc79ffez3eDdVzpQUiyjg (bump : ses_e33a9832fffepDHZucImyJiqlt, BOM : ses_e33a6a8f0ffeUFz3TkTsWoEzS8, 0.6.0 : ses_e33614746ffeKMbP9wav4mP5ue) | HH-17 release 0.6.0 | scripts/deploy.ps1, docs/DEPLOY.md, README.md, MACHINE-AGENTS.md, plugin.yaml, dashboard/manifest.json, tests/test_deploy_script.py | done, poussé (1d5e5c9) |
| plugin-core | - | HH-18 déploiement 0.6.0 (ops, hors dépôt) | - | done |
| desktop-ui | ses_e33649a4effekZJxlzCIsJPdIN (revue : ses_e3360447cffeVf9akuegI8IY1e, correctifs : ses_e335a3e8affeDw10aftpJg8NJI) | HH-12/HH-15 mode cartes | desktop/plugin.js | done, poussé (b1709e7) |
| dashboard-backend | ses_e3366b99bffepRBCCaFUmlKnvc (registre : ses_e336cbb1dffe0P5JeyFAYQQ2Tg, nits : ses_e335a0d62ffefr2AenhPf1Z9iL) | HH-10/HH-11/HH-13/HH-16 file de revue + registre | review.py, dashboard/plugin_api.py, tests/test_review.py, tests/test_review_decisions.py | done, poussé (b649f1e) |
