<!--
SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>
SPDX-FileCopyrightText: 2026 AI Power Grid

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# AI Power Grid - Decentralized AI Worker Network - The Grid

Community-powered AI generation backend. Users submit jobs; workers run models; the Grid coordinates. Image (Stable Diffusion, FLUX, etc.) and text (LLM) generation via REST API.

## Stack

- Python 3.9+, Flask, PostgreSQL, Redis
- Model catalog from **Base** (Grid Diamond contract); optional on-chain model validation
- Docker supported

## Blockchain & NFTs

- **Model registry**: Image/text models are read from the Grid Diamond contract on Base. Optional validation that workers only serve registered models.
- **Job anchoring**: Job receipts (worker, model hash, input/output hashes) can be anchored on-chain (JobAnchor contract) for verification and rewards.
- **Recipes & NFTs**: ComfyUI workflows stored on-chain (RecipeVault); workflows can be marked NFT-capable. Generation metadata (prompt, seed, params, model) is stored so images can be **recreated from chain data + model** — no need to store the image itself for proof or minting.

See [docs/BLOCKCHAIN_INTEGRATION.md](docs/BLOCKCHAIN_INTEGRATION.md) and [core-integration-package/README.md](core-integration-package/README.md).

## Quick start

```bash
# .env: database, Redis, optional BLOCKCHAIN_ENABLED, MODEL_REGISTRY_ADDRESS, BASE_RPC_URL
pip install -r requirements.txt
# PostgreSQL + Redis running, then:
python server.py
```

API: `http://localhost:5000/api` (or see `/api` for docs).

## Links

| What | Where |
|------|--------|
| API usage / SDK | [README_integration.md](README_integration.md) |
| Image API | [README_StableHorde.md](README_StableHorde.md) |
| Text API | [README_KoboldAIHorde.md](README_KoboldAIHorde.md) |
| Docker | [README_docker.md](README_docker.md) |
| FAQ | [FAQ.md](FAQ.md) |
| Blockchain setup | [docs/BLOCKCHAIN_INTEGRATION.md](docs/BLOCKCHAIN_INTEGRATION.md) |

## Roadmap

- **FastAPI migration**: Replace Flask/Waitress with FastAPI/Uvicorn for async, SSE, and higher concurrency. Plan: [docs/architecture-migration/01-fastapi-migration.md](docs/architecture-migration/01-fastapi-migration.md). Full migration docs: [docs/architecture-migration/](docs/architecture-migration/).

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE) and [LICENSES/](LICENSES/).
