# 03: Public space data and what we train in 36 h

**Owner: P3** (`ml/`). All facts below were checked on 2026-10-23 unless marked ⚠️ UNVERIFIED. Full list in [sources.md](sources.md).

## 1. Honest feasibility call
| Option | 36 h feasible? | Verdict |
|---|---|---|
| Frozen image embeddings (SigLIP/CLIP via `open_clip`) + **logistic-regression heads** on MSL v2.1 (6.8K images) | **Yes**: embedding 6.8K images on the GX10 is minutes; LR fit is seconds | **Do this (core)** |
| Same heads + AI4Mars terrain crops (a **subset**, e.g. 5–10K crops from the 6.47 GB HF mirror) | Yes, if the download starts first thing | **Do this (core)** |
| ChemCam master list → category priors (human vs AEGIS target choices) | Yes, it's one 26 MB CSV and pandas | **Do this (core, differentiator)** |
| Few-shot arena adaptation: 50–100 photos of *our* rocks, labeled in 30 min | Yes | **Do this (core: fixes the domain gap)** |
| Zero-shot VLM captions via Ollama (for the "analyze" text and reasons) | Yes, if a model that runs on ARM64 is pulled before the venue ⚠️ | Core-lite; fallback = template strings |
| Fine-tune a segmentation net on the full AI4Mars (16 GB merged zip) | Download alone is risky at venue bandwidth; training + debugging won't fit next to everything else | **Don't (stretch at most)** |
| Train a detector from scratch | No | **Don't** |

**Domain gap warning:** Mars imagery ≠ pebbles on brown paper under fluorescent light. The Mars data gives us *what scientists find interesting* (categories, textures, priors); the arena few-shot set gives us *what our camera actually sees*. Both are required. Expect the heads to be noisy on arena images until the few-shot step is done.

## 2. Datasets (exact)
| Dataset | URL | Size | License | Contents | Use |
|---|---|---|---|---|---|
| **MSL Curiosity labeled set v2.1** | https://zenodo.org/records/4033453 | `msl-labeled-data-set-v2.1.zip` **54.9 MB** | **CC-BY-4.0** | 6,820 images, 227×227, 19 classes, splits train 5,920 / val 300 / test 600 | `visual` interest head + `category` head |
| MSL v1 (Deep Mars) | https://zenodo.org/records/1049137 | `msl-images.zip` 60.6 MB | CC-BY-SA-4.0 | 6,691 images, 24 classes, ~256×256, sols 3–1060 (Wagstaff et al., "Deep Mars") | Optional extra data (note share-alike) |
| **AI4Mars** (Zenodo v0.6) | https://zenodo.org/records/15995036 | merged zip **16,232.5 MB**; unmerged labels 1,566 MB | CC-BY-4.0 (NASA portal says "not specified" ⚠️) | ~50K images, ~425K labels, MSL/M2020/MER; classes soil 0, bedrock 1, sand 2, big rock 3, null 255 | `terrain` head (crops) |
| AI4Mars mirror (HF) | https://huggingface.co/datasets/hassanjbara/AI4MARS | 6.47 GB, 19,096 rows (MSL, v0.1) | as uploaded ⚠️ | images + masks | **Preferred download** (smaller) |
| AI4Mars mirror (Kaggle) | Kaggle "AI4Mars" v0.1 ⚠️ exact slug in sources | 6.21 GB | listed CC0 ⚠️ | same | backup |
| **ChemCam master observation list** | https://pds-geosciences.wustl.edu/msl/msl-m-chemcam-libs-4_5-rdr-v1/mslccm_1xxx/document/msl_ccam_obs.csv | ~26 MB | PDS (public) | 123,514 rows × 34 cols (Sol, Target, Distance (m), RSM Az/El, Comments, Target Type…), 6,124 unique targets, sols 10–4843; **9,452 AEGIS rows** over 513 sols (1184–4832) | **Category priors**: human vs AEGIS choices |
| SuperCam observation log | bundle `urn:nasa:pds:mars2020_supercam`, `data_observation_log` (https://pds-geosciences.wustl.edu/m2020/urn-nasa-pds-mars2020_supercam/readme.txt) | ⚠️ | PDS | "Master spreadsheet of target information" | Stretch: Perseverance priors. AEGIS flag ⚠️ UNVERIFIED |
| JPL raw images (Curiosity) | `https://mars.nasa.gov/api/v1/raw_image_items/?order=sol%20desc&per_page=N&page=0&condition_1=msl:mission` | per-call | ⚠️ terms UNVERIFIED (unofficial JSON) | latest raw images | Dashboard "real Mars" backdrop / demo images; few extra eval images |
| JPL raw images (Perseverance) | `https://mars.nasa.gov/rss/api/?feed=raw_images&category=mars2020&feedtype=json&num=N&page=0&order=sol+desc` | per-call | ⚠️ same | | same |
| ~~api.nasa.gov Mars Rover Photos~~ | `api.nasa.gov/mars-photos/...` | — | — | **Returned 404 on 2026-10-23: treat as dead** | don't build on it |

### MSL v2.1 class map (verified from the zip)
`0 Arm cover · 1 Other rover part · 2 Artifact · 3 Nearby surface · 4 Close-up rock · 5 DRT · 6 DRT spot · 7 Distant landscape · 8 Drill hole · 9 Night sky · 10 Float · 11 Layers · 12 Light-toned veins · 13 Mastcam cal target · 14 Sand · 15 Sun · 16 Wheel · 17 Wheel joint · 18 Wheel tracks`

**Mapping to our categories** (`ml/class_map.yaml`):
| Ours | MSL v2.1 classes | AI4Mars |
|---|---|---|
| `float_rock` | 10 Float, 4 Close-up rock | big rock 3 |
| `layered` | 11 Layers | bedrock 1 |
| `veined` | 12 Light-toned veins | — |
| `sand` | 14 Sand | sand 2 |
| `bedrock` | 3 Nearby surface (weak) | bedrock 1 |
| `pebble` | 4 Close-up rock (weak) | soil 0 (weak) |
| *not science* (visual → 0) | 0, 1, 2, 5, 6, 9, 13, 15, 16, 17, 18 | null 255 |
| *science-ish context* | 7 Distant landscape, 8 Drill hole | — |

`visual` head target = 1 for {4, 8, 10, 11, 12, 14}, 0 for the not-science set (binary LR on embeddings).

## 3. ChemCam priors (the "calibrated on real scientist choices" part) <a id="chemcam-priors"></a>
`ml/chemcam_priors.py`:
1. Load `msl_ccam_obs.csv` (pandas; column names per `msl_ccam_obs.lbl`).
2. Split rows: `aegis = Target Type contains "AEGIS"` (variants "AEGIS selected bedrock" 3,417, "AEGIS-selected bedrock" 2,568, …) or Target name starts `aegis_`; `human = rest`, excluding calibration/sky (`Passive Sky` 5,808, `Calibration Target` 4,137).
3. Map Target Type → our categories with a keyword table (`Bedrock`→bedrock, `Float Rock`→float_rock, `Soil`→pebble/soil, `Outcrop`→layered, vein keywords in Comments→veined ⚠️ keyword list to be checked by eye).
4. **Count unique targets** per category (not shots, since one target gets many LIBS points), for human and AEGIS separately.
5. `prior[c] = 0.7·share_human[c] + 0.3·share_aegis[c]`, normalized so max = 1. Write `ml/models/category_priors.json` with both raw shares (the dashboard can show "scientists picked float rocks X% of the time").

Top target types in the file (rows): Bedrock 22,252 · Rock 8,264 · Passive Sky 5,808 · Calibration Target 4,137 · Soil 2,296 · Float Rock 1,724 · Outcrop 1,642.

## 4. Pipeline (`ml/`, run on the GX10)
| Step | Script | Output | Time (estimate) |
|---|---|---|---|
| Download | `download/get_msl_v21.py`, `get_chemcam_obs.py`, `get_ai4mars.py --subset 8000` | `ml/data/…` | MSL+ChemCam: minutes; AI4Mars HF: ⚠️ venue bandwidth (**start first thing or pre-download**) |
| Priors | `chemcam_priors.py` | `category_priors.json` | 1 min |
| Embeddings | `build_embeddings.py --model <open_clip name>` | `msl_v21.npz`, `ai4mars_crops.npz` | minutes on GPU |
| Heads | `train_heads.py` | `heads.joblib` (visual, category, terrain) + `metrics.json` | seconds |
| Arena few-shot | `arena_fewshot.py --dir ml/data/arena/` | updates `heads.joblib` (sample weight ×5 for arena) | 30 min labeling (P1) |

**Arena label layout** (P1 fills it, P3 owns this definition): `ml/data/arena/<category>/<crop>.jpg`, one folder per category from 00 §6.1 (`float_rock`, `layered`, `veined`, `sand`, `bedrock`, `pebble`).

| Eval | `eval.py` | confusion matrix, per-class F1 on MSL test + arena hold-out | 1 min |

**Model choice:** any `open_clip` ViT-B/16-class model (SigLIP or OpenAI CLIP). Pick one that installs cleanly on ARM64 + CUDA on DGX OS ⚠️ (PyTorch is preinstalled per the ASUS spec; `open_clip_torch` wheel compatibility ⚠️ test before I0). Fallback: CPU embeddings of 6.8K 227 px images is still feasible.

**AI4Mars crops:** for each image, sample 128×128 patches whose mask is ≥ 80% one class (skip 255) and take the majority label. 2 crops/image × 4,000 images ≈ 8K crops.

## 5. Acceptance (P3)
- `metrics.json`: visual head AUC on MSL v2.1 test **≥ 0.80** (target; if lower, still ship and report it).
- Category head macro-F1 reported; arena hold-out (≥ 20 images) accuracy reported, not gated.
- `category_priors.json` exists with human and AEGIS shares and the counts used.
- `vision /score` latency < 300 ms for 10 crops on the GX10.

## 6. Licenses and attribution (put in README credits at the end)
- MSL v2.1 and AI4Mars: CC-BY-4.0 → attribute in README and the dashboard "About".
- MSL v1: CC-BY-SA-4.0. If used, models trained on it are fine; **don't redistribute the images** in our repo.
- No dataset images are committed to git (`ml/data/` is gitignored).
