# Registry - hermes-hindsight-plus

One row per **role + domain**. `session` is the `sessionID` returned by the subagent tool: pass it
back to continue that role on that domain instead of starting from scratch. `files in play` is the
current conflict surface: two tasks may not share it.

Reuse rule: a worker/reviewer session is reused only when its model **bills tokens** (paid). On a
free model (`-free`, input price 0) or a local one, a warm cache saves nothing: always start a fresh
child. Judge from the configured model, never from the reported cost - OpenCode Go reports cost 0
because usage is in the subscription, even on paid models. This repo runs on
`opencode-go/deepseek-v4.1-flash` (paid subscription), so reuse applies.

Start a fresh child anyway for a new domain, a different agent, or when the child's context no
longer matches the files under SCOPE (e.g. it was working on `review.py` and the task is now
`desktop/plugin.js`).

| role | domain | session (reuse) | last item | files in play | status |
|---|---|---|---|---|---|
| worker | packaging-docs | `ses_e33614746ffeKMbP9wav4mP5ue` (deploy routine : `ses_e33c7bc79ffez3eDdVzpQUiyjg`, BOM : `ses_e33a6a8f0ffeUFz3TkTsWoEzS8`, topology : `ses_e3334f572ffebZS7pPDOxal3W1`) | release 0.7.0 | scripts/deploy.ps1, docs/DEPLOY.md, README.md, MACHINE-AGENTS.md, plugin.yaml, dashboard/manifest.json, tests/test_deploy_script.py | done, poussé `e2ed7ad` |
| worker | dashboard-backend | `ses_e29dbd286ffeUsL64wT14v7ik0` (registre : `ses_e336cbb1dffe0P5JeyFAYQQ2Tg`, revue : `ses_e3366b99bffepRBCCaFUmlKnvc`, curatable : `ses_e29f35f01ffeb3C1aeAtly8EzU`) | `sort=risk` + `volatile_only` | review.py, dashboard/plugin_api.py, tests/test_review.py, tests/test_review_decisions.py | done, poussé `e2ed7ad` |
| worker | desktop-ui | `ses_e29f38804ffeyM67IUJC2U6xrs` (cartes : `ses_e33649a4effekZJxlzCIsJPdIN`, pane : `ses_e2b980fd9ffegUlH1TNoO7RSQ9`, clés : `ses_e2a002f30ffeN3aXCecvlpO5xA`) | « ? », Curatables, Risque | desktop/plugin.js | done, poussé `e2ed7ad` |
| worker | plugin-core | `ses_e2a03df78ffe9UPXSS0Hphc1z6` | lecteur `state.db` multi-marqueurs | core.py, tests/test_injections_state_db.py | done, poussé `091e05e` |
| reviewer | deploy + desktop | `ses_e33c1e81affeAWgveyZltpv5wm` | routine de déploiement (10 findings) | - (lecture seule) | done |
| reviewer | review backend + cartes | `ses_e3360447cffeVf9akuegI8IY1e` | file globale + registre + mode cartes (14 findings) | - (lecture seule) | done |

## Last release

`0.7.0` - `origin/main` = `e2ed7ad`, deployed on oracle + this Windows host by
`scripts/deploy.ps1 -Target all -RestartApi`; gateway restarted 14 s after `plugin_api.py`.

## Open decisions (see inbox.md)

- HH-31: the review window only covers the newest 2000 facts of a 59 931-fact bank.
- HH-35: import criteria into a fresh bank, `bank_id` switch, `auto_retain: false` + a retention skill.
