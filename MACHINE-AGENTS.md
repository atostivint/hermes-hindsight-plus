# AGENTS.md — développement sur cette machine

> Copie de la consigne machine (même poste : Windows, i5-13500, 31,7 Go). Ce dépôt est en
> **Python** : la section 2 (builds Rust, `cargo-bg`) ne s'applique qu'aux dépôts Rust de cette
> machine. Les sections 1, 3 et 4 restent valables telles quelles pour diagnostiquer un gel.


Machine personnelle, utilisée **en parallèle du jeu et d'autres applications**.
Les règles ci-dessous ne sont pas des préférences de style : chacune vient d'un
incident mesuré sur cette machine. Les ignorer a déjà gelé l'interface.

La machine est partagée : un build ne doit jamais avoir la priorité sur ce que
l'utilisateur est en train de faire.

---

## 1. Matériel — à ne pas re-deviner

| Ressource | Valeur |
|---|---|
| CPU | Intel i5-13500, hybride : **6 P-cores** (LP 0–11, masque `0x00FFF`) + **8 E-cores** (LP 12–19, masque `0xFF000`) |
| RAM | 31,7 Go physiques |
| Limite de commit | ~68,6 Go (RAM + pagefile automatique) |
| GPU | RTX 4070 Ti, 12 Go de VRAM |
| Disque système | Samsung 980 PRO NVMe |

Les jeux et l'interface veulent les P-cores. Les compilations n'ont aucune
contrainte de latence : elles vont sur les E-cores.

---

## 2. Builds Rust

1. **Ne jamais lancer `cargo` directement** pour un build de fond. Utiliser le
   wrapper, déjà dans le `PATH` :

   ```
   cargo-bg build
   cargo-bg test --release
   ```

   `cargo-bg` (`~/.local/bin/cargo-bg.ps1`) parke tout le sous-arbre
   `cargo`/`rustc`/`link` sur les 8 E-cores et le passe en `BelowNormal`.
   L'affinité est héritée par les enfants, donc un seul appel suffit.

2. **Un seul `cargo` à la fois.** Lancer plusieurs `cargo` en parallèle multiplie
   les jobs sans aucune coordination entre eux. C'est la cause racine de
   l'incident du 2026-11-08 : 6 `cargo` → 40 `rustc` simultanés.

3. `~/.cargo/config.toml` plafonne `build.jobs = 6`. C'est délibéré : à 6 jobs le
   pic de RSS d'un build tourne autour de 2 Go, contre ~10 Go sans plafond.
   Ne pas relever ce plafond pour « aller plus vite ». Si un build plein pot est
   vraiment nécessaire, le faire explicitement et **sans jeu lancé** :
   `cargo build -j 14`.

4. Ne pas tuer des `rustc` au hasard pour libérer de la mémoire : ça casse le
   build sans rendre la mémoire durablement. Réduire les jobs, ou baisser la
   priorité, donne le même résultat sans rien casser.

---

## 3. Lenteur ou gel de l'interface : quoi regarder

**Le CPU n'est presque jamais le coupable ici.** Il tournait à 20–50 % pendant
les gels les plus sévères. L'ordre de diagnostic :

1. **`pages/s`** — le seul chiffre qui compte vraiment.

   ```powershell
   (Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory).PagesPerSec
   ```

   - `< 1 000` : normal
   - `> 2 000` : pagination sévère, l'interface va saccader
   - `~ 20 000` : c'est un gel en cours

2. **Commit** — `CommittedBytes / CommitLimit`. Rester sous 80 %. À 97 %, la
   prochaine allocation échoue au lieu de simplement ralentir.

   ```powershell
   $m = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
   "{0:N1} / {1:N1} Go ({2:N0} %)" -f ($m.CommittedBytes/1GB), ($m.CommitLimit/1GB), ($m.CommittedBytes/$m.CommitLimit*100)
   ```

3. **Nombre de process `rustc` simultanés** — au-delà de 8, les jobs ne sont pas
   plafonnés quelque part.

À ne pas confondre avec un problème de driver : `PercentDPCTime` et
`PercentInterruptTime` sont restés à 0–1 % pendant tous les incidents, et le
disque (NVMe) n'a jamais saturé. Ce n'est ni le GPU, ni l'audio, ni l'I/O.

---

## 4. Historique

**2026-11-08** — Gels répétés de l'interface. Cause : 6 `cargo` en parallèle,
40 `rustc`, 9,7 Go de RSS, commit à 97 % de la limite, ~22 000 pages/s de
pagination. Le pagefile s'est étendu tout seul de 38,5 à 41,6 Go.
Résolution : priorité `BelowNormal` sur `rustc`/`cargo`/`link` puis fin du build.
D'où les règles de la section 2.

---

## 5. Déploiement du plugin hermes-hindsight-plus (2026-11-11)

Faits propres à cette machine et à ses deux hôtes : chemins, alias ssh, ACL du dossier
temporaire. Le mécanisme générique (installation, piège du `.hermes-package.json`) est décrit
dans `README.md` et `docs/DEPLOY.md` ; ici, seulement ce qui est mesuré localement.

- **Deux hôtes, deux emplacements :**
  - `oracle` — Linux, joint par `ssh oracle` (alias déjà présent dans `~/.ssh/config`) ; hermes
    home = `/var/lib/hermes-agent/.hermes` (= `$HOME/.hermes` ; `HERMES_HOME` n'est pas exporté
    dans un shell ssh non interactif) ; UI web, pas d'Electron.
  - `windows` — cette machine ; `HERMES_HOME=C:\Users\elekt\AppData\Local\hermes`.
  - Sur les deux : demi-agent dans `<HERMES_HOME>/plugins/hermes-hindsight-plus/`, demi-desktop
    dans `<HERMES_HOME>/desktop-plugins/hermes-hindsight-plus/plugin.js`.
- **Un seul mécanisme d'installation fonctionne**, sur les deux hôtes :
  `hermes plugins install https://github.com/atostivint/hermes-hindsight-plus.git --force --yes-deps --enable`
  (clone, installe, recharge les plugins du gateway). `hermes plugins update <nom> --yes` est
  invalide ; `hermes plugins update <nom>` seul répond « already up to date » et laisse le
  demi-desktop périmé.
- **Le piège `.hermes-package.json` :** le réconciliateur ne rafraîchit
  `<HERMES_HOME>/desktop-plugins/<id>/` que si la copie porte ce marqueur, qu'il n'écrit que
  lorsque le fichier publié est identique octet pour octet au `desktop/plugin.js` du paquet. Une
  copie sans marqueur est prise pour le plugin de l'utilisateur : jamais réécrite, jamais
  rafraîchie. Mesuré le 2026-11-11 sur les deux hôtes : le fichier publié était une copie
  manuelle du working tree en CRLF (53 474 o) quand le demi-agent est le checkout en LF
  (51 936 o) — donc jamais identiques. Réparation : sauvegarder le dossier publié en
  `<HERMES_HOME>/backups/desktop-plugin-halves/hermes-hindsight-plus-<horodatage>` — jamais
  dans `desktop-plugins/`, qui est servi par le loader — puis copier le demi-desktop du paquet
  par-dessus.
  Comparer **en SHA256**, jamais en taille : CRLF/LF rend la taille faussement différente.
- `scripts/deploy.ps1 -Target oracle|windows|all [-DryRun]` fait tout cela ; `docs/DEPLOY.md`
  est la procédure humaine.
- **Lancer la suite avec un `--basetemp` neuf :** `python -m pytest -q` seul échoue avec 48
  `PermissionError` sur `C:\Users\elekt\AppData\Local\Temp\pytest-of-elekt` (ACL périmée sur ce
  dossier temporaire, sans rapport avec le code). Utiliser
  `python -m pytest -q --basetemp=$env:TEMP\hhp-$(Get-Date -Format HHmmss)` → 100 tests passent.
