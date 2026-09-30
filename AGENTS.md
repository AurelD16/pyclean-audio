# AGENTS.md — pyclean-audio

Application web locale (FastAPI + page unique) qui améliore l'audio des fichiers
audio/vidéo avec LavaSR v2 (`LavaEnhance2`, modèle HF `YatharthS/LavaSR`).

## Commandes

```bash
# environnement (une fois) — c'est ce que fait run.sh
uv venv .venv
uv pip install "LavaSR @ git+https://github.com/ysharma3501/LavaSR.git" fastapi "uvicorn[standard]" python-multipart
# supplémentaire pour --transcribe / case « Transcrire » : ./run.sh --asr
# (ou : uv pip install "nemo_toolkit[asr]", ou -r requirements.txt)
# NeMo est volontairement hors installation de base : plusieurs Go, et il impose
# sa version de torch. --asr l'ajoute à un .venv existant, sans le recréer.

# serveur web (port 8787)
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
# ou : ./run.sh            (crée .venv et installe les 4 paquets de base)
#     ./run.sh --asr       (idempotent : n'installe NeMo que s'il manque)
#     ./run.sh --help

# CLI
.venv/bin/python -m app.cli FICHIER [--denoise] [--transcribe] [--input-sr 8000|16000|24000] [--cutoff Hz] [--format wav|mp3] [-o OUTDIR]
.venv/bin/python -m app.cli DOSSIER ...   # récursif ; sortie par défaut <DOSSIER>_pyclean-audio à côté (arbre mimé)

# tests et lint
uv pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # 134 tests, ~6 s, aucun modèle chargé
.venv/bin/python -m ruff check .      # seul `ruff check` fait foi (pas de ruff format)
```

Aucun **modèle** n'est chargé par les tests : ni poids LavaSR, ni NeMo. Le seul
module lourd importé est `torch`/`torchaudio`, par `tests/test_stream.py` (qui
vérifie l'équivalence bit à bit du rééchantillonnage) et donc par
`app/enhancer.py`. Les tests de pipeline passent par `app.enhancer` /
`app.transcriber` **factices** : `processor` fait ses imports *dans* le corps de
`process_file`, `tests/conftest.py` injecte les doublures dans `sys.modules`.
Les tests de `probe`/`process_file` sont ignorés si `ffmpeg`/`ffprobe` ne sont
pas dans le PATH (`requires_ffmpeg`) ; `run_ffmpeg` est testé avec un faux
`ffmpeg` (script shell) pour provoquer erreurs, blocage et annulation.
La validation du comportement réel (qualité audio, VRAM) se fait par
exécution, voir « Tests rapides » plus bas.

## Architecture

- `app/enhancer.py` — wrapper du modèle (singleton, verrou d'inférence,
  lazy-load + preload au démarrage) **et lecture fenêtrée** : `Stream16k`
  convertit l'entrée en 16 kHz fenêtre par fenêtre. **C'est ici que vit le
  découpage** : blocs de 60 s à 16 kHz, chevauchement 2 s, fondu croisé
  cosinus égalité de puissance, écriture WAV séquentielle (mémoire O(fenêtre),
  ~40 Mo quelle que soit la durée du fichier). `plan_ranges()` est une fonction
  pure testée sans modèle.
  - `Stream16k` est **bit-à-bit équivalent** au rééchantillonnement global de
    tout le fichier, à deux conditions à ne pas casser : (a) le début de
    fenêtre est calé sur un multiple de `lcm` des pas de convolution des
    éventuels deux rééchantillonnages (`_resample_stride`), sinon un décalage
    d'un échantillon s'entend aux jonctions ; (b) la marge de lecture
    (`READ_MARGIN_SEC`) est **des deux côtés**, sinon les ~6 premiers
    échantillons de chaque fenêtre lisent un zéro de remplissage au lieu du
    signal. `tests/test_stream.py` verrouille cette équivalence.
- `app/processor.py` — pipeline ffmpeg : décodage mono 48 kHz, extraction de
  piste audio (vidéos), remontage vidéo (`-c:v copy`, audio AAC 192 k),
  encodage MP3 (libmp3lame 192 k) ; `process_file(..., output_format="wav")`
  produit en plus un `<stem>_pyclean-audio.mp3` si `output_format="mp3"` (toujours
  accompagné du WAV, même pour les vidéos) ; `ALLOWED_EXT` +
  `iter_media_files()` (énumération récursive partagée entre CLI et API).
  - `run_ffmpeg` : stderr va dans un **fichier temporaire**, jamais dans un
    pipe (un pipe non drainé bloque ffmpeg à 64 Ko et suspend le job) ; le
    processus est lancé en `start_new_session` et tué **par groupe**
    (`_kill`), sinon un petit-enfant survit et le tube stdout ne se ferme
    jamais ; `timeout` optionnel (chien de garde). Si le callback de
    progression lève (annulation), ffmpeg est tué avant de remonter.
  - `MAX_DURATION` est vérifié dès `probe()` : un fichier trop long est rejeté
    en quelques secondes, avant toute inférence.
- `app/main.py` — API : `POST /api/enhance` (upload 1 fichier ; produit
  toujours WAV **et** MP3 pour proposer les deux téléchargements),
  `POST /api/enhance_folder` (upload d'un dossier, fichiers envoyés avec
  leur chemin relatif multipart, paramètre `output_format` "mp3" (défaut)
  ou "wav" qui pilote l'artefact audio et le ZIP), `GET /api/jobs/{id}`,
  `POST /api/jobs/{id}/cancel`, `DELETE /api/jobs/{id}`,
  `GET /api/jobs/{id}/file/{original_wav|enhanced_wav|enhanced_mp3|video|transcript|transcript_srt}`,
  `GET /api/jobs/{id}/file/{index}/{...}` (job dossier),
  `GET /api/jobs/{id}/zip` (archive des résultats, job dossier),
  `GET /api/status` (état du modèle LavaSR + `transcriber` + `queue` +
  `retention`). Les deux endpoints acceptent `transcribe` (bool, défaut
  false) qui produit un artefact `transcript` (`<stem>_transcript.txt`, avec
  `(aucune parole détectée)` si rien n'est reconnu) et `transcript_srt`
  (`<stem>_pyclean-audio.srt`) **seulement si le modèle a renvoyé des
  horodatages** ; tous les deux sont inclus dans le ZIP quand ils existent.
  - **Noms servis ≠ noms sur disque** : `DOWNLOADS` renomme au téléchargement
    (original → `{stem}.wav`, amélioré → `{stem}_pyclean-audio.wav|.mp3`),
    et `_build_folder_zip` écrit le SRT en `{stem}.srt` (sans le suffixe
    `_pyclean-audio`). Les collisions de stem dans un même sous-dossier sont
    suffixées `_2`, `_3`… à la fois pour le dossier de sortie et pour le ZIP.
  - **Assainissement des noms** : `_safe_relpath()` supprime `..` et les
    chemins absolus des noms multipart ; le nom de l'upload en mode fichier est
    réduit à `[A-Za-z0-9._-]` et à 80 caractères, mais `job["stem"]` (donc les
    noms de téléchargement) garde le nom d'origine. `_save_upload()` écrit par
    1 Mo, compte un budget **partagé** entre les fichiers d'un même envoi et
    ignore un fichier vide (mode dossier) au lieu d'échouer.
  - **File d'attente** : un `queue.Queue` borné (`PYCLEAN_QUEUE_MAX`) et un
    **unique** thread worker. La cible reçoit le job en **premier argument**
    (`_enqueue` le rajoute) — sinon `_run_job`/`_run_folder_job` échoue. Le
    `queue_position` renvoyé est un instantané pris à l'envoi, jamais recalculé.
    `_worker` rattrape les exceptions pour qu'un job en échec ne disparaisse
    pas de l'interface (`state="error"`).
  - **Rétention** : `_purge_expired()` (tâche asyncio du `lifespan`) supprime
    les jobs terminés au bout de `PYCLEAN_JOB_TTL` ; `_purge_orphans()` vide
    `data/jobs/` au démarrage (les jobs sont en mémoire, tout le reste est
    injoignable) ; `_make_room()` évince les jobs terminés les plus anciens si
    `PYCLEAN_JOB_MAX` est atteint, et ne renvoie 503 que si tout est actif.
  - **Transcription indisponible** : `_check_transcribe()` renvoie **400** si
    `transcribe` est demandé alors que NeMo n'est pas installé (`is_available()`),
    au lieu d'accepter un job qui échouerait en cours de route sur un
    `ModuleNotFoundError: nemo`. L'interface s'en sert aussi pour désactiver la
    case avant l'envoi.
  - `GET /api/jobs/{id}` renvoie `_snapshot(job)` : copie **profonde** des
    `artifacts`, car le thread de traitement les mute pendant l'encodage JSON
    (« dictionary changed size during iteration »). Les clés `_`-prefixées et
    les chemins serveur ne sont jamais exposés (les artefacts sont réduits à
    leur nom de fichier, `zip` à un booléen).
  - Un job « dossier » a `kind="folder"` et une liste `files[]` (état,
    artefacts et progression par fichier) ; les fichiers sont traités
    séquentiellement, les chemins source/sortie sont passés **hors** du dict
    d'entrée (liste `sources` parallèle). `keep_original=False` : l'A/B n'existe
    pas en mode dossier, le WAV 48 kHz d'origine est supprimé (−43 % de
    disque).
- `app/cancel.py` — `JobCancelled` + `raise_if_cancelled()` (points de contrôle
  entre blocs, entre tranches, à chaque ligne de progression) et **`GPU_LOCK`**,
  verrou global pris autour d'`enhance_wav` **et** de la transcription : les
  verrous par modèle ne suffisent pas. Aujourd'hui, le worker unique de
  `main.py` sérialise déjà les jobs, donc ce verrou n'est jamais disputé — il
  reste le filet de sécurité pour qui appellerait `process_file` directement
  (script, test) : sans lui, l'amélioration et la transcription pourraient se
 superposer et les deux modèles résident en même temps sur les 8 Go.
- `app/config.py` — toutes les bornes et la rétention, lues dans l'environnement
  (voir tableau « Configuration » dans README.md) ; une valeur illisible ou
  nulle retombe sur le défaut.
- `app/transcriber.py` — wrapper du modèle de transcription Parakeet TDT
  (`nvidia/parakeet-tdt-0.6b-v3`, checkpoint NeMo chargé via
  `nemo.collections.asr.models.ASRModel.restore_from` sur le `.nemo` du
  cache HuggingFace local, téléchargé si absent ; `half()` sur CUDA).
  Singleton + verrou comme `enhancer.py` (VRAM partagée), lazy-load,
  `transcribe(wav) -> Transcript`. Le import `nemo` reste dans `_load()` (lourd).
  `is_available()` (fonction libre) répond à « NeMo est-il installé ? » par
  `importlib.util.find_spec` — pas d'import, donc quasi gratuit même appelé à
  chaque `GET /api/status` (l'interface rafraîchit toutes les 2 s) ; le résultat
  est publié dans `status()["available"]` et consommé par `_check_transcribe()`
  (`app/main.py`) et par la case à cocher de l'interface.
  **C'est ici que vit le découpage** : blocs de `CHUNK_SEC = 30` s
  chevauchés de `OVERLAP_SEC = 10` s, lus en streaming sur un seul
  `sf.SoundFile` (`seek`), rééchantillonnés à 16 kHz par bloc,
  `model.transcribe([arr_numpy], return_hypotheses=True, num_workers=0,
  timestamps=True)`, progression émise par bloc. `plan_chunks()` rend quatre
  bornes par bloc
  `(début_lu, fin_lue, début_gardé, fin_gardée)` : le début d'un blob est la
  zone où Parakeet se trompe le plus, on ne le prend pas dans le bloc courant
  (il l'a déjà été en fin de bloc précédent) ; les fenêtres gardées pavent
  `[0, frames)` sans trou **ni doublon**. `to_cues(ts, offset, t0, t1)` applique
  la fenêtre — `t0`/`t1` sont **relatifs au bloc lu**, qui commence
  `OVERLAP_SEC` avant la fenêtre gardée, et `offset` le retranslate en temps de
  fichier. Le texte `.txt` est bâti sur les mêmes mots que les sous-titres
  (retour au texte brut du modèle s'il n'y a pas d'horodatages).
  `timestamps=True` remplit `hypothesis.timestamp` (dict `word` / `segment`) :
  `to_cues()` en tire des sous-titres lisibles (phrases si le modèle en
  donne, sinon regroupement de mots ; temps forcés croissants, l'alignement TDT
  n'est pas monotone) et `render_srt()` produit le `.srt`.
- `app/cli.py` — interface en ligne de commande (réutilise `processor`) ;
  accepte un fichier OU un dossier (récursif, continue sur erreur,
  récapitulatif final) ; `--format wav|mp3` (défaut : wav) ;
  `--transcribe` (défaut : désactivé) ajoute `<stem>_transcript.txt` et
  `<stem>_pyclean-audio.srt`.
- `static/index.html` — interface (glisser-déposer fichier ou dossier —
  lecture récursive via `webkitGetAsEntry` / `webkitdirectory` — A/B **lié**
  (les deux lecteurs se suivent sur play/pause/seek, boutons « ▶ Origine » /
  « ▶ Amélioré », case « lié »), case « Transcrire l'audio nettoyé » (défaut :
  décochée, **désactivée avec la commande `./run.sh --asr` si NeMo manque** —
  `setTranscribeEnabled()` sur le même `available` que l'API), position dans la
  file d'attente, bouton « Annuler le traitement », bouton
  « Supprimer les résultats », polling du job, liste des résultats par
  fichier + téléchargement ZIP).
- `tests/` — pytest (aucun modèle chargé : `app.enhancer` / `app.transcriber`
  sont remplacés par des modules factices dans `conftest.py`) ; `ruff check`
  est le seul lint qui fait foi.

## Contraintes du modèle à respecter

- Le modèle consomme **toujours 16 kHz mono** (`Stream16k` : sr de la source →
  `input_sr` → 16 kHz) et produit **48 kHz** (facteur 3). Toute modification du
  chemin d'échantillonnage doit préserver cette chaîne.
- Avant chaque inférence, il faut poser le raffineur :
  `model.bwe_model.lr_refiner = FastLRMerge(device, cutoff=cutoff,
  transition_bins=1024)` avec `cutoff = input_sr // 2` par défaut.
- La sortie d'un bloc doit être ramenée à **exactement 3 × n_in** échantillons
  (robage/padding) — la librairie peut dériver de quelques samples.
- Ne PAS utiliser `model.enhance(..., batch=True)` de la librairie : elle
  padding sans rober la fin. Le découpage maison de `enhancer.py` remplace ce
  mode.
- L'inférence est sérialisée par `LavaEnhancer._lock` (état global du
  raffineur + VRAM). Ne pas lancer d'inférence en dehors de ce verrou, ni en
  dehors de `GPU_LOCK` (qui couvre aussi Parakeet).
- `model.enhance` attend un tenseur **[batch, temps]** : lui passer un vecteur
  1-D fait planter vocos (`Padding size 2 is not supported for 1D input`).
- **Parakeet (transcription) — trois points à ne pas casser :**
  - *Tranches courtes, chevauchées.* Deux raisons, opposées :
    (a) **VRAM** — les activations de l'encodeur croissent linéairement avec
    la durée : une heure en un seul blob pique à ~7,5 Go et fait OOM sur une
    carte 8 Go ;
    (b) **justesse** — le checkpoint est entraîné sur des énoncés de
    `max_duration: 40 s` (`model_config.yaml`). Au-delà, Parakeet perd les
    extrémités et *condense* des passages entiers. Mesuré sur le même audio de
    300 s (contrôle bit à bit de l'audio amélioré, blocs lus en entier) :

    | bloc | 1er mot | mots dans [0,30 s] | mots en 120-300 s | fin |
    |---|---|---|---|---|
    | 300 s | 51 s | 62 | — | 299 s |
    | 150 s | 26 s | 92 | — | 300 s |
    | 60 s  | 0,8 s (max 15,9 s) | 109 | 190 | **284 s (16 s perdues)** |
    | 30 s  | 0,7 s (max 6,5 s) | 95 | **254** | 300 s |

    `CHUNK_SEC = 30` avec `OVERLAP_SEC = 10` : chaque bloc est lu avec 10 s de
    contexte en amont et la fenêtre gardée est rejettée, donc le début d'un
    bloc est toujours pris chez le bloc précédent, et la queue (que 60 s perdait)
    est prise chez le bloc suivant. `OVERLAP_SEC` doit rester **≥ le blanc de
    tête** (6,5 s mesurés en 30 s), sinon la jointure perd les premiers mots.
    Ne pas rallonger les tranches pour « gagner du temps » : 30 s coûte 1,5×
    d'audio passé au modèle (~20 s de transcription GPU pour 54 min) et fait
    tomber le pic VRAM de 5,0 Go à 2,4 Go. Ne pas passer en un seul blob.
  - *Pas de déplacement de device.* Boucler `cuda → cpu → cuda` corrompt le
    modèle : `illegal memory access`, puis sortie `⁇`. Le device est choisi
    au chargement et n'est plus bougé. (NeMo construit le modèle sur CPU et
    le pose sur CUDA à la 1re inférence : c'est normal, 0 Mo alloué au
    chargement n'indique pas un échec.)
  - *`gc.collect()` + `torch.cuda.empty_cache()` après `_load()`* : sans ça
    le résidu fp32 (~2,4 Go) reste réservé et la carte est inutilisable pour
    un second job. Après gc : ~1,2 Go (poids fp16).
- `soundfile.SoundFile` : ouvrir en écriture avec `mode="w"` explicite
  (`sf.SoundFile(path, "w", samplerate=48000, channels=1, subtype="PCM_16")`)
  et comparer les longueurs avec `numel()` (pas `size()`).

## Environnement

- Python 3.11.16 dans `.venv` (torch 2.14+cu130) ; le Python système (3.14) ne
  sert qu'aux utilitaires. Toujours lancer via `.venv/bin/python`.
- GPU NVIDIA + CUDA détectés automatiquement (`torch.cuda.is_available()`),
  sinon CPU. Le premier téléchargement du modèle LavaSR (**~115 Mo**, blobs
  `~/.cache/huggingface/hub/models--YatharthS--LavaSR`) vient de HuggingFace
  et est mis en cache ; `lifespan` le précharge dans un thread.
- Débit LavaSR mesuré (RTX 2000 Ada, 8 Go, torch 2.14+cu130) : **~200× le
  temps réel** — 300 s d'audio (5 blocs de 60 s) enhancés en 1,41 s sans
  débruiteur, 1,57 s avec. Le temps passé par un job court est donc surtout
  ffmpeg (décodage 48 kHz, MP3, remontage), pas l'inférence.
- Transcription (option `--transcribe` / case web) : Parakeet TDT 0.6B v3
  via NeMo (`nemo_toolkit[asr]`, **`./run.sh --asr`**, absent de
  l'installation de base),
  checkpoint `.nemo` `nvidia/parakeet-tdt-0.6b-v3` (**~2,4 Go** sur disque)
  chargé depuis le cache HuggingFace local (`~/.cache/huggingface/hub`,
  surchargeable par `HF_HOME`) s'il est déjà là, sinon téléchargé.
  `half()` sur CUDA (~1,2 Go de poids) ;
  transcription par blocs de 30 s chevauchés de 10 s (1,5× d'audio passé au
  modèle), VRAM bornée — **2,4 Go de pic mesurés** avec LavaSR chargé, quel que
  soit le nombre de blocs, contre 5,0 Go en blocs de 300 s. 54 min de fichier
  se transcrivent en ~20 s sur GPU.
  Sur CPU uniquement : mesuré à ~13× le temps réel (230 s d'audio en 18 s sur
  20 cœurs) — la GPU reste préférable.
  NeMo 3.0 : un chemin local se charge avec `ASRModel.restore_from`
  (`from_pretrained` n'accepte que des repo-id HuggingFace).
- `ffmpeg`/`ffprobe` requis sur le PATH.
- Le LSP/IDE utilise souvent le Python système : les erreurs
  « Import "torch" could not be resolved » sont attendues et sans objet.
- Pour redémarrer le serveur depuis un shell : `pkill -f "uvicorn app[.]main"`
  (la notation crochet évite de tuer sa propre ligne de commande) puis lancer
  détaché : `setsid bash -c '(.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8787 >> /tmp/server.log 2>&1 < /dev/null &)'`.

## Tests rapides

```bash
# signal dégradé 8 kHz + vidéo de test
ffmpeg -f lavfi -i "sine=frequency=350:duration=6" -af "lowpass=2400,aresample=8000" test_8k.wav
ffmpeg -f lavfi -i "testsrc=duration=5:size=320x240:rate=25" -f lavfi -i "sine=frequency=300:duration=5" -af lowpass=2500 -c:v libx264 -c:a aac test.mp4

# web (fichier seul : WAV et MP3 proposés)
JOB=$(curl -s -X POST -F "file=@test_8k.wav" http://127.0.0.1:8787/api/enhance | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s http://127.0.0.1:8787/api/jobs/$JOB
curl -s -o out.wav http://127.0.0.1:8787/api/jobs/$JOB/file/enhanced_wav
curl -s -o out.mp3 http://127.0.0.1:8787/api/jobs/$JOB/file/enhanced_mp3

# CLI (défaut : wav ; --format mp3 ajoute <stem>_pyclean-audio.mp3)
.venv/bin/python -m app.cli test.mp4 -o out/
.venv/bin/python -m app.cli test.mp4 -o out_mp3/ --format mp3

# dossier (récursif) — CLI
mkdir -p tdir/under && cp test_8k.wav tdir/ && cp test.mp4 tdir/under/
.venv/bin/python -m app.cli tdir -o out_dir/
.venv/bin/python -m app.cli tdir -o out_dir_mp3/ --format mp3

# dossier (récursif) — web (format de sortie par défaut : MP3 ; -F "output_format=wav" pour WAV)
JOB=$(curl -s -X POST \
  -F "files=@test_8k.wav;filename=root/a.mp3" \
  -F "files=@test.mp4;filename=under/b.mp4" \
  http://127.0.0.1:8787/api/enhance_folder | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s http://127.0.0.1:8787/api/jobs/$JOB            # files[] : état par fichier, output_format
curl -s -o a.mp3  http://127.0.0.1:8787/api/jobs/$JOB/file/0/enhanced_mp3
curl -s -o all.zip http://127.0.0.1:8787/api/jobs/$JOB/zip   # SRT inclus en <stem>.srt

# transcription (fichier seul) : -F "transcribe=true" → artefacts transcript
# et transcript_srt (le .srt seulement si le modèle a renvoyé des horodatages)
JOB=$(curl -s -X POST -F "file=@test_8k.wav" -F "transcribe=true" \
  http://127.0.0.1:8787/api/enhance | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s http://127.0.0.1:8787/api/jobs/$JOB | python3 -m json.tool

# régression OOM : fichier long (1 h) + transcription, surveiller la VRAM
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -l 2 > /tmp/vram.log &
JOB=$(curl -s -X POST -F "file=@long.mp3" -F "transcribe=true" \
  http://127.0.0.1:8787/api/enhance | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s http://127.0.0.1:8787/api/jobs/$JOB          # state=done, artefacts
awk '{if($NF+0>m)m=$NF+0} END{print "pic VRAM: " m " MiB"}' /tmp/vram.log
```

Ces commandes de test écrivent `test_8k.wav` et `test.mp4` (~426 Mo) à la
racine du dépôt : `.gitignore` ne couvre ni l'un ni l'autre (il ne liste que
`*.mp3`, `data`, `.venv`, `__pycache__`, `.pytest_cache/`, `.ruff_cache/`),
et les autres sorties (`out*/`, `tdir/`) sont à effacer à la main.

Critères de validité :
- durée de sortie == durée d'entrée (à < 1 frame près en 48 kHz) ;
- MP3 : durée identique au WAV, 48 kHz mono, ~192 kbit/s ;
- aucune discontinuité aux jonctions de blocs (max|Δ| local ≤ percentile 99,99
  global du signal) ;
- la bande haute (> Nyquist d'entrée) doit contenir plus d'énergie qu'un simple
  upsampling sinc de la même entrée (preuve de la BWE) ;
- pour les vidéos : flux vidéo identique (`-c:v copy`), audio AAC 48 kHz ;
- transcription d'un fichier long : `state=done`, transcript non vide, SRT
  horodaté et croissant, **premier mot < 5 s et dernier mot à < 1 s de la fin**
  (le découpage perd sinon les extrémités des blocs), aucun trou > 8 s dans le
  SRT là où l'audio a une énergie > 5 % de la médiane, pic VRAM ≤ ~2,5 Go sur
  une carte 8 Go et **constant** quel que soit le nombre de blocs ;
- file d'attente : deux envois successifs → le second reste `queued` tant que
  le premier tourne, puis passe `done` ;
- annulation : `POST /api/jobs/{id}/cancel` → `state=cancelled` et le dossier
  du job disparaît de `data/jobs/` ;
- rétention : `PYCLEAN_JOB_TTL=20 PYCLEAN_PURGE_INTERVAL=5` → le job devient
  404 et `data/jobs/` se vide.

## Limites connues (documentées dans « Limitations » de README.md, en anglais)

- Sortie mono 48 kHz ; vidéos → MP4 (sous-titres et pistes multiples perdus).
- Durée max ~10 000 s, fichier max 2 Go, dossier 500 fichiers / 8 Go.
- HEAD renvoie 404 sur les routes FastAPI (sans impact navigateur : GET).
- Un traitement à la fois (file d'attente) ; `keep_original=False` en mode
  dossier (pas d'A/B) : `original_wav` n'y est donc jamais produit.
- Encodage MP3 et remontage vidéo restent **séquentiels** : les paralléliser
  ne gagne que ~0,4 s sur une vidéo de 5 min (mesuré), pour un multiplexage de
  progression bien plus verbeux. À reconsidérer si le codec change.
- La transcription est indisponible sans `nemo_toolkit[asr]` : `./run.sh --asr`
  l'ajoute au `.venv` existant. Tant qu'il manque, l'API refuse `transcribe=true`
  en **400** et l'interface désactive la case — plus d'échec
  `ModuleNotFoundError: nemo` en cours de job. Le `.srt` n'est produit que si
  le modèle renvoie des horodatages ; le `.txt` existe toujours (éventuellement
  avec `(aucune parole détectée)`). Les sous-titres ne sont pas **muxés** dans
  le MP4 : c'est un fichier à charger à part.
