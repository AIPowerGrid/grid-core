<!-- SPDX-FileCopyrightText: 2026 AI Power Grid -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Recipe Dispatch — how to add a media workflow

**Audience:** anyone adding a new image/video model to the grid.
**TL;DR:** you do **not** write worker code or hand-edit node IDs. You export a
ComfyUI workflow, run one command that auto-detects the node map, fill in the
knobs you want to expose, drop the file in `recipes/`, and restart. The model is
then advertised and served.

---

## The model: recipe = governed workflow

A **recipe** is a ComfyUI workflow plus a `_grid` metadata block that says *which
node slots are user-variable* and *what values are allowed*. It's the one authoring
format — the legacy worker-side `_bridge` workflows are being retired (see
[Legacy path](#legacy-_bridge-path-being-retired)).

Two layers, clean split:

| Layer | Who | Does |
|-------|-----|------|
| **Coordinator** (grid-core) | `services/recipes.py` | caches approved recipes, resolves `recipe + client inputs → concrete graph`, gates every knob |
| **Worker** (grid-media-worker) | `bridge/ws_worker.py` | receives the resolved `recipe_spec` and runs it on ComfyUI verbatim |

Clients never send graphs. They pick a model (the recipe's `name`) and supply
inputs (`prompt`, `seed`, `image`, dims…). The coordinator injects those inputs
into the *parsed* graph at the declared paths — never string substitution — so a
prompt full of `{"}` is just a dict value and can't alter graph structure. Only
recipes in the cache (i.e. approved) can be resolved.

---

## Anatomy of the `_grid` block

From `recipes/flux2-klein-t2i.json`:

```jsonc
{
  "_grid": {
    "name": "FLUX.2 Klein 4B FP8",        // client-facing model id — keep stable once public
    "modelName": "FLUX.2 Klein 4B FP8",   // advertised model (≥1 recipe per model)
    "engine": "comfyui",                  // comfyui | native-ltx | drawthings | …
    "jobType": "image",                   // image | video
    "deterministic": false,               // true = same seed+inputs → identical output (NFT repro)
    "requiredModels": ["FLUX.2 Klein 4B FP8"],  // checkpoint(s) the worker must have to advertise

    // THE NODE MAP: client input name -> dotted path into the graph.
    // A value may be one path (str) or several (list) — e.g. a seed fed to
    // multiple sampling passes, set identically.
    "vars": {
      "prompt":          "92:74.inputs.text",
      "negative_prompt": "92:87.inputs.text",
      "seed":            "92:73.inputs.noise_seed",
      "steps":           "92:62.inputs.steps",
      "cfg":             "92:63.inputs.cfg",
      "width":           "92:68.inputs.value",
      "height":          "92:69.inputs.value",
      "sampler":         "92:61.inputs.sampler_name"
    },

    // GOVERNANCE. Supplied knobs outside these are REJECTED (422), never clamped —
    // the caller learns their request was invalid instead of silently getting
    // different output. Omitted knobs keep the graph's baked default.
    "clamps": { "steps": [4, 6], "cfg": [1, 1.5], "width": [512, 1536], "height": [512, 1536] },
    "enums":  { "sampler": ["euler", "dpmpp_2m", "dpmpp_2m_sde", "res_multistep"] },

    // OPTIONAL: LoRA injection points (worker splices LoraLoader nodes here).
    // Omit entirely if the model doesn't support LoRAs — it will then reject `loras`.
    "loraInject": {
      "model_src":  ["92:70", 0],
      "clip_src":   ["92:71", 0],
      "model_sinks": [["92:63", "model"]],
      "clip_sinks":  [["92:74", "clip"], ["92:87", "clip"]]
    }
  },

  // ...the rest of the file is the ComfyUI API-format graph, verbatim.
  "9":  { "class_type": "SaveImage", "inputs": { ... } },
  "92:61": { "class_type": "KSamplerSelect", "inputs": { "sampler_name": "euler" } }
}
```

**Everything under `_grid` is authoring metadata; everything else is the graph.**
`register_recipe()` splits them at load time.

### Var names the API knows

Declaring a var by these names lights up a capability automatically:

| var | effect |
|-----|--------|
| `prompt` | required string input |
| `negative_prompt` | optional string input |
| `seed` | integer, echoed back for repro |
| `image` | model accepts a source frame → `img2img` / `img2video` capability |
| `denoise` | latent-blend strength knob → `strength` capability (low = stay near source) |
| any numeric (`steps`, `cfg`, `width`…) | number input; add a `clamps` entry to gate it |
| any categorical (`sampler`, `scheduler`) | add an `enums` entry to allow-list it |

`cfg` is exposed to clients as `cfg_scale` (the one client-facing rename).

---

## Add a workflow — step by step

### 1. Export from ComfyUI in **API format**

In ComfyUI: enable dev mode (Settings → "Enable Dev mode Options") → **Save (API
Format)**. This gives the flat `{ "node_id": { "class_type", "inputs" } }` shape.
The normal "Save" (UI format, a `nodes` array) is **not** supported by the importer.

### 2. Auto-detect the node map

```bash
cd grid-core
python -m grid_api.services.recipe_import my_export.json \
    --name "My Model Name" --model "My Model Name" \
    --job-type image \
    -o recipes/my-model-t2i.json
```

The importer *traces the graph* to fill `vars` for the slots that are hard to find
by hand — positive vs negative prompt (via the conditioning wiring), every seed
input, the `LoadImage` start frame — and prints `NOTE:` lines for anything
ambiguous. Example run on the Klein workflow:

```
Detected 3 var slot(s): prompt, negative_prompt, seed
Next: review `_grid` — add clamps/enums for any numeric/categorical knob you expose…
```

It got `prompt`/`negative_prompt`/`seed` exactly right. It does **not** guess the
simple numeric knobs — that's step 3.

### 3. Finish the `_grid` block by hand

Open the written file and add what auto-detection can't decide for you:

- **Numeric knobs you want to expose** — add the path to `vars` *and* a band to
  `clamps` (`steps`, `cfg`, `width`, `height`, video `length`/`fps`…). If you don't
  expose it, the graph's baked default stands. Find a node's id/field by searching
  the graph for its `class_type` + input key.
- **Sampler / scheduler** — add to `vars` and allow-list in `enums`.
- **`image`** (for an i2i/edit recipe) — usually auto-detected; confirm it points at
  the right `LoadImage`.
- **`loraInject`** — only if the model supports LoRAs.
- **`deterministic: true`** — only if the pipeline truly reproduces bit-for-bit.

> Whatever you expose becomes **public API surface**. Don't widen a knob past what
> produces good output — a bad `cfg` or off-list sampler yields garbage, not a
> variation.

### 4. Validate

```bash
python -m grid_api.services.recipe_import --validate recipes/my-model-t2i.json
# OK — recipe is structurally valid
```

This checks every `vars` path targets a real `node.inputs` slot, every clamp/enum
key is a declared var, and every graph edge points at a real node — i.e. the class
of typo that silently breaks a model. It's pure/offline; it also runs in
`pytest grid_api/services/tests/test_recipes.py`.

### 5. Ship it

- **Pre-vault (today):** the file in `recipes/` is loaded at startup by
  `load_local_recipes()` (wired in `main.py`). Restart grid-core → the model is
  advertised and served. That's it.
- **On-chain (when RecipeVault is live):** the same `{_grid, …graph}` JSON is what
  gets stored in RecipeVault; `sync_from_recipevault()` pulls it into the identical
  cache. Authoring format is unchanged — see [Migration state](#migration-state).

The model auto-advertises: `param_schema()` derives the client-facing parameter
list (with min/max bands and enum options) straight from your `_grid`, so
`/v1/models` and the param docs update with no extra work.

---

## Variant recipes (t2i / i2i / edit)

A model can have **several** recipes and the resolver picks per request:

- `flux2-klein-t2i.json` — no `image` var → chosen when there's no source frame.
- `flux2-klein-i2i.json` — has `image`, no `denoise` → edit/reference recipe.
- `flux2-klein-i2i-blend.json` — has `image` + `denoise` → latent-blend strength.

`resolve_for_model()` routes: source frame + `denoise` supplied → blend; source
frame alone → edit; no source → t2i. Give each variant the **same** `modelName`
and they're grouped automatically.

---

## How dispatch works (what happens at request time)

```
client → /v1/images (model="My Model", prompt, seed, steps…)
  → recipes.resolve_for_model(model, inputs, has_source)
      · pick the variant, deep-copy its graph
      · gate each supplied knob (clamp/enum) — reject 422 if out of band
      · inject values at the vars paths into the PARSED graph
  → media.py dispatches { recipe_engine, recipe_spec, seed, lora_inject, image_paths }
  → worker (bridge/ws_worker.py) runs recipe_spec on ComfyUI verbatim
```

The worker binds the source image (if any) into the `image` slot and splices LoRA
loaders at `loraInject` — it does **not** re-map or template anything else. The
graph it runs is exactly what the coordinator resolved.

---

## Migration state (read before you deploy)

Two switches gate the end-to-end recipe path. Until both are set, a new recipe
authors and validates fine but won't *execute* on the live worker:

1. **Worker executor** — running a resolved `recipe_spec` is the branch
   `feat/recipe-dispatch` in **grid-media-worker**, currently **unmerged**. The
   deployed worker (`main`) still uses the legacy `_bridge` path. Merge + deploy it
   before recipes run live.
2. **On-chain source** — `RECIPEVAULT_ADDRESS` is **unset** on prod, so recipes
   come from local `recipes/*.json`, not the chain. Setting it flips the source with
   no authoring change.

Neither blocks *writing* recipes now — the local-file path is fully wired on the
coordinator.

---

## Legacy `_bridge` path (being retired)

The `workflows/*.json` files in **grid-media-worker** carry a `_bridge` block
(`nodes: {seed: "92:73"}`) that an older worker-side templater fills. Only two files
(the FLUX.2 Klein pair) were ever converted; the rest are heuristic. This path is
ungoverned (no clamps/enums) and is superseded by recipes. **Do not author new
`_bridge` workflows** — use the recipe flow above.

---

## Reference

- `grid_api/services/recipes.py` — resolver, capability gates, RecipeVault sync.
- `grid_api/services/recipe_import.py` — the importer/CLI + `validate_recipe`.
- `recipes/*.json` — six worked examples (t2i, i2i, i2i-blend, i2v, turbo).
- `recipes/AGENTS.md` — one-screen authoring contract.
- Tests: `grid_api/services/tests/test_recipes.py`, `test_media_contract.py`.
