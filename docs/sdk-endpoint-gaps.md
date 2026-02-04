# Core API vs SDK endpoint gaps

Core routes are defined in `horde/apis/v2/__init__.py`. The SDK defines paths in `sdk/horde-sdk/horde_sdk/ai_horde_api/endpoints.py` (`AI_HORDE_API_ENDPOINT_SUBPATH`).

## Core endpoints (v2, prefix `/api/v2`)

| Core path | SDK constant | Notes |
|-----------|--------------|--------|
| `/v2/generate/async` | ✅ v2_generate_async | |
| `/v2/generate/status/{id}` | ✅ v2_generate_status | |
| `/v2/generate/check/{id}` | ✅ v2_generate_check | |
| `/v2/generate/rate/{id}` | ❌ **missing** | Aesthetics (rate a generation) |
| `/v2/generate/pop` | ✅ v2_generate_pop | |
| `/v2/generate/submit` | ✅ v2_generate_submit | |
| `/v2/generate/progress` | ❌ **missing** | JobProgressUpdate (worker progress) |
| `/v2/styles/image` | ❌ **missing** | Image styles list/create |
| `/v2/styles/image/{style_id}` | ❌ **missing** | Single image style |
| `/v2/styles/image_by_name/{style_name}` | ❌ **missing** | |
| `/v2/styles/image/{style_id}/example` | ❌ **missing** | |
| `/v2/styles/image/{style_id}/example/{example_id}` | ❌ **missing** | |
| `/v2/generate/text/async` | ✅ v2_generate_text_async | |
| `/v2/generate/text/status/{id}` | ✅ v2_generate_text_status | |
| `/v2/generate/text/pop` | ✅ v2_generate_text_pop | |
| `/v2/generate/text/submit` | ✅ v2_generate_text_submit | |
| `/v2/styles/text` | ❌ **missing** | |
| `/v2/styles/text/{style_id}` | ❌ **missing** | |
| `/v2/styles/text_by_name/{style_name}` | ❌ **missing** | |
| `/v2/collections` | ❌ **missing** | |
| `/v2/collections/{collection_id}` | ❌ **missing** | |
| `/v2/collection_by_name/{collection_name}` | ❌ **missing** | |
| `/v2/users` | ✅ v2_users_all | |
| `/v2/users/{user_id}` | ✅ v2_users | |
| `/v2/find_user` | ✅ v2_find_user | |
| `/v2/sharedkeys` | ✅ v2_sharedkeys_create | |
| `/v2/sharedkeys/{sharedkey_id}` | ⚠️ typo: `v2_sharedkeys` = `/v2_sharedkeys/...` | Should be `/v2/sharedkeys/{sharedkey_id}` |
| `/v2/workers` | ✅ v2_workers_all | |
| `/v2/workers/{worker_id}` | ✅ v2_workers | |
| `/v2/workers/messages` | ❌ **missing** | |
| `/v2/workers/messages/{message_id}` | ❌ **missing** | |
| `/v2/workers/name/{worker_name}` | ❌ **missing** | |
| `/v2/kudos/transfer` | ✅ v2_kudos_transfer | |
| `/v2/kudos/award` | ❌ **missing** | |
| `/v2/status/modes` | ❌ **missing** | |
| `/v2/status/performance` | ✅ v2_status_performance | |
| `/v2/status/models` | ✅ v2_status_models_all | |
| `/v2/status/models/{model_name}` | ✅ v2_status_models | |
| `/v2/status/news` | ❌ **missing** | |
| `/v2/status/heartbeat` | ✅ v2_status_heartbeat | |
| `/v2/teams` | ✅ v2_teams_all | |
| `/v2/teams/{team_id}` | ✅ v2_teams | |
| `/v2/operations/ipaddr` | ❌ **missing** | |
| `/v2/operations/ipaddr/{ipaddr}` | ❌ **missing** | |
| `/v2/operations/block_worker_ipaddr/{worker_id}` | ❌ **missing** | |
| `/v2/interrogate/*` | ✅ all 4 | |
| `/v2/filters` | ❌ **missing** | |
| `/v2/filters/regex` | ❌ **missing** | |
| `/v2/filters/{filter_id}` | ❌ **missing** | |
| `/v2/stats/img/totals` | ✅ v2_stats_img_totals | |
| `/v2/stats/img/models` | ✅ v2_stats_img_models | |
| `/v2/stats/text/totals` | ✅ v2_stats_text_totals | |
| `/v2/stats/text/models` | ✅ v2_stats_text_models | |
| `/v2/documents/terms` | ❌ **missing** | |
| `/v2/documents/privacy` | ❌ **missing** | |
| `/v2/documents/sponsors` | ❌ **missing** | |
| `/v2/auto_worker_type` | ❌ **missing** | |

## Summary

- **Missing from SDK (path constants only; request/response models may also be missing):**  
  `generate/rate/{id}`, `generate/progress`, all `styles/*`, all `collections/*`, `workers/messages`, `workers/messages/{id}`, `workers/name/{name}`, `kudos/award`, `status/modes`, `status/news`, all `operations/*`, all `filters/*`, all `documents/*`, `auto_worker_type`.
- **Bug in SDK:** `v2_sharedkeys` is `/v2_sharedkeys/{sharedkey_id}`; should be `/v2/sharedkeys/{sharedkey_id}`.

Adding the missing path constants to the SDK does not add request/response models; those are tracked separately (e.g. `test_verify_api_surface`).
