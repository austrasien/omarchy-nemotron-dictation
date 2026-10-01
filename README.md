# Omarchy Nemotron dictation

Sidecar de **dictée française en live** pour [Omarchy](https://omarchy.org) / Hyprland : Nemotron 3.5 (NeMo-Speech.cpp) tape les mots pendant que tu parles. Ce n’est **ni un plugin ni un hook**.

La carte pixellisée en bas d’écran, c’est [Omakoe](https://github.com/ok/omakoe) (plugin overlay). Ce repo fournit le moteur, le typage, les units systemd et le pont audio que Omakoe lit.

Voxtype peut rester installé pour un rollback. Pendant une prise, le toggle arrête `voxtype.service`.

## Prérequis

- Omarchy (Hyprland Lua)
- `python3`, `wtype`, `pw-record`, `wpctl`, `curl`
- ~1 Go disque (binaire CPU + GGUF Q8 ~700 Mo)
- CPU raisonnable (pas besoin de NVIDIA)

## Install

```bash
git clone <url-de-ce-repo> omarchy-nemotron-dictation
cd omarchy-nemotron-dictation
chmod +x install.sh uninstall.sh
./install.sh
omarchy restart shell    # pour la waveform Omakoe
```

Options : `--no-hud`, `--no-pull`, `--no-binds`.

Touches après install :

| Raccourci | Effet |
|---|---|
| Super+Ctrl+X ou F9 | toggle (start / stop + envoi du reliquat) |
| Échap (pendant une prise) | annuler, ne tape pas le reliquat |

10 secondes sans parole = stop + flush automatique. Le média est baissé pendant la prise.

## Dictionnaire de noms (`phrases.txt`)

**Ce repo ne contient aucun dico perso.** Les prénoms, sociétés, sigles, c’est chez toi.

À l’install, si le fichier n’existe pas encore, l’exemple `phrases.example.txt` (mots techniques Omarchy) est copié vers :

```
~/.config/omarchy/nemotron/phrases.txt
```

S’il existe déjà, il n’est **jamais** écrasé. `uninstall.sh` le garde, sauf `./uninstall.sh --purge`.

### Format

- Un mot ou nom par ligne.
- `#` en début de ligne = commentaire.
- Lignes vides ignorées.
- Les accents comptent : `café` n’est pas `cafe`.
- Tu peux mettre un sigle (`EPFL`) et sa ville (`Lausanne`) sur deux lignes.

Le client relit le fichier **à chaque prise**. Pas besoin de redémarrer le serveur : Super+Ctrl+X, parle, c’est la nouvelle liste.

### Ce que ça fait

Au début de la session WebSocket, les lignes sont envoyées au serveur en `speech_contexts` (boost 6). Le modèle est censé préférer ces graphies quand l’audio est ambigu (noms propres, outils).

Sur le GGUF Q8 actuel, Nemotron peut **ignorer** le boosting s’il n’a pas de tokenizer SentencePiece embarqué (`RNNT word boosting requested but no phrases could be tokenized`). Dans ce cas le fichier ne casse rien : la dictée tourne, sans bonus. Ça reste le bon endroit pour tes noms le jour où le modèle les honore.

### Exemple

```text
# ~/.config/omarchy/nemotron/phrases.txt
Omarchy
Hyprland
Cursor
Camille
Dupont
```

## Rollback Voxtype

```bash
./uninstall.sh
systemctl --user enable --now voxtype.service
```

Les binds Super+Ctrl+X / F9 reviennent au défaut Omarchy après `hyprctl reload` (le bloc marqué est retiré de `bindings.lua`).

## Fichiers posés

| Chemin | Rôle |
|---|---|
| `~/.config/omarchy/bin/nemotron-dictation*` | toggle + client Python |
| `~/.local/bin/voxtype-audio-bridge` | wrapper : Voxtype si actif, sinon niveaux Nemotron |
| `~/.config/systemd/user/nemo-speech.service` | serveur ASR localhost:18765 |
| `~/.config/omarchy/nemotron/phrases.txt` | dico perso (gitignore) |

N’écrit jamais dans `/usr/share/omarchy/` ni `~/.config/uwsm/env.d/`.

## Licence

MIT. Nemotron / NeMo-Speech.cpp : licences NVIDIA et Hugging Face du modèle.
