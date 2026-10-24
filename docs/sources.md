# Sources

Everything in this spec that is a fact (not a design choice) comes from one of these. Checked **2026-10-23** unless noted. Anything not backed by a source here is marked **⚠️ UNVERIFIED** where it appears; the full list is in [05 Unknowns](05-unknowns-and-risks.md#unknowns).

## SpacetimeDB
| What | URL |
|---|---|
| Docs home | https://spacetimedb.com/docs |
| Full docs for agents (used for API shapes: `table`, `schema`, `reducer`, `useTable`, `SenderError`) | https://spacetimedb.com/docs/llms-full.txt |
| HTTP: database calls / SQL | https://spacetimedb.com/docs/http/database |
| HTTP: identity (`POST /v1/identity`) | https://spacetimedb.com/docs/http/identity |
| SATS-JSON | https://spacetimedb.com/docs/sats-json |
| File storage guidance | https://spacetimedb.com/docs/tables/file-storage |
| CLI reference (`publish`, `--delete-data`, unstable commands) | https://spacetimedb.com/docs/cli-reference |
| Releases (v2.13.0, ARM64 build) | https://github.com/clockworklabs/SpacetimeDB/releases |
| Installer | https://install.spacetimedb.com |
| Maincloud (fallback hosting) | https://maincloud.spacetimedb.com |
| TypeScript agent rules (local copy `research/ts-rules.mdc`, from the SpacetimeDB docs' AI rules) | see docs home |

## Space data
| What | URL |
|---|---|
| MSL Curiosity labeled data set v2.1 (CC-BY-4.0) | https://zenodo.org/records/4033453 |
| MSL v1 "Deep Mars" (CC-BY-SA-4.0) | https://zenodo.org/records/1049137 |
| AI4Mars v0.6 | https://zenodo.org/records/15995036 |
| AI4Mars HF mirror | https://huggingface.co/datasets/hassanjbara/AI4MARS |
| ChemCam master observation list | https://pds-geosciences.wustl.edu/msl/msl-m-chemcam-libs-4_5-rdr-v1/mslccm_1xxx/document/msl_ccam_obs.csv |
| SuperCam PDS bundle readme | https://pds-geosciences.wustl.edu/m2020/urn-nasa-pds-mars2020_supercam/readme.txt |
| Curiosity raw images JSON (unofficial, terms ⚠️) | https://mars.nasa.gov/api/v1/raw_image_items/?order=sol%20desc&per_page=N&page=0&condition_1=msl:mission |
| Perseverance raw images JSON (unofficial, terms ⚠️) | https://mars.nasa.gov/rss/api/?feed=raw_images&category=mars2020&feedtype=json&num=N&page=0&order=sol+desc |
| AEGIS on Perseverance (JPL) | https://doi.org/10.48577/jpl.nymwbh |

## Bradford assay (simulated; real parameters only)
| What | URL |
|---|---|
| Thermo Pierce Coomassie Plus (Bradford) manual MAN0011203: BSA standards 2000…25, 0 µg/mL; test-tube protocol 0.05 mL + 1.5 mL, 10 min, 595 nm; linear range BSA 125–1000 µg/mL | https://documents.thermofisher.com/TFS-Assets/LSG/manuals/MAN0011203_CoomassiePlus_Bradford_Asy_UG.pdf |
| Thermo Tech Tip TR0025, protein assay spectra (Coomassie max 595 nm) | https://assets.thermofisher.com/TFS-Assets/LSG/Application-Notes/TR0025-Protein-assay-spectra.pdf |
| Bio-Rad Quick Start Bradford manual 4110065A: dye forms 470/650/595 nm; BSA linear 125–1,000 µg/mL; incubation ≥ 5 min, ≤ 1 h | https://www.bio-rad.com/sites/default/files/webroot/web/pdf/lsr/literature/4110065A.pdf |
| Bio-Rad Protein Assay LIT33: absorbance max shifts 465 → 595 nm | https://www.bio-rad.com/webroot/web/pdf/lsr/literature/LIT33.pdf |
| Bio-Rad Bradford assay feature page: red → blue; curve non-linear at high concentration | https://www.bio-rad.com/en-us/feature/bradford-assay.html |

## Hardware (prices as listed on 2026-10-23)
| Part | URL |
|---|---|
| PCA9685 16-ch servo driver ($14.95) | https://www.adafruit.com/product/815 |
| ADS1115 ADC ($14.95) | https://www.adafruit.com/product/1085 |
| TB6612 motor driver ($6.95) | https://www.adafruit.com/product/2448 |
| SG92R micro servo ($5.95) | https://www.adafruit.com/product/169 |
| MPU-6050 IMU ($12.95) | https://www.adafruit.com/product/3886 |
| 4×AA holder with switch ($2.95) | https://www.adafruit.com/product/830 |
| 4700 µF capacitor ($1.95) | https://www.adafruit.com/product/1589 |
| 100 µF capacitors ×10 ($1.95) | https://www.adafruit.com/product/2193 |
| Pololu D24V22F6 ($27.95) / D24V22F5 #2858 ($26.95) / D36V50F6 #4092 ($39.95) | https://www.pololu.com/product/2859 · https://www.pololu.com/product/2858 · https://www.pololu.com/product/4092 |
| ASUS Ascent GX10 tech specs | https://www.asus.com/us/networking-iot-servers/desktop-ai-supercomputer/ultra-small-ai-supercomputers/asus-ascent-gx10/techspec/ |
| pigpio does not support Pi 5 | https://github.com/joan2937/pigpio/issues/589 |
| xAI Grok API: models, OpenAI-compatible base URL, structured outputs, speech-to-text | https://docs.x.ai/docs/models, https://docs.x.ai/docs/guides/structured-outputs, https://docs.x.ai/developers/model-capabilities/audio/speech-to-text |

## Not verified from a primary source
- SpaceXSI track rules: known only from the team brief (U-17).
- Everything else marked ⚠️ in the docs; see [05](05-unknowns-and-risks.md#unknowns).
