# T057 Multimodal Fix — Rollout & Diagnosis

## The bug

`ModelConfig.input_modalities` (`src/claw_eval/config.py:45`) defaults to `["text"]`.
The **native claweval loop** (`runner/loop.py:187` → `model_supports_modality` in
`runner/media_loader.py:220`) refuses to inject an image content-block into the
conversation unless `"image"` is present in `input_modalities`. So for an image-input
task the picture never reaches the model, the agent can only guess, and the score
collapses to the residual rule/tool dimensions (~0.2).

`T057_deepseek_logo_identification` is exactly this kind of task. Prompt:
*"Which company does this logo belong to? The image is at fixtures/media/image.jpg"*.
Oracle answer: **DeepSeek**. Grader: `ImageQAOracleMixin`, whose `completion` =
`0.30 * image_loaded + 0.70 * answer_score`, where `image_loaded` requires a trace
`MediaLoad` event with `modality=image, status=loaded`.

## The intended fix

New config `config_multimodal_smoke.yaml` (copied from `config_concurrency_smoke.yaml`,
env-substituted sonnet model + judge), adding:

```yaml
model:
  api_key: ${CLAWEVAL_LLM_API_KEY}
  base_url: ${CLAWEVAL_LLM_BASE_URL}
  model_id: ${CLAWEVAL_LLM_MODEL}
  input_modalities: [text, image]
```

## T057 rerun result — before vs after

| Run | harness | task_score | completion | image loaded? | model's answer |
|-----|---------|-----------|------------|---------------|----------------|
| baseline | openclaw | ~0.20 | low | no | (guess) |
| this rerun | openclaw | **0.10** | **0.00** | **no** | **"Docker"** (WRONG) |

The fix did **NOT** help under the openclaw harness — the score got *worse*, not better.
The agent never saw the image. It fell back to the `caption_describe_image` tool, which
returned a generic *"stylized blue whale logo"* caption (no brand), `web_search` was
disabled, and the model guessed **Docker** (because Docker's logo is a blue whale).
No `MediaLoad` event appeared in the trace at all → `image_loaded = 0`.

Trace: `traces/t057_multimodal_rerun/claude-sonnet-4-5_26-06-26-15-26/`

## Diagnosis — why declaring the modality didn't work for openclaw

`input_modalities` is consulted **only by the native claweval loop**. The openclaw
harness is a completely separate execution path and **never reads `cfg.media` or
`model.input_modalities`, never calls the media loader, and never emits `MediaLoad`
events**:

- The only consumer of `media_cfg` / `input_modalities` is the **native** harness
  (`harnesses/claweval.py:95` → `runner/loop.run_task` →
  `_build_initial_user_content` → `model_supports_modality`,
  `to_content_block` → base64 `ImageBlock`).
- The **openclaw** harness (`harnesses/openclaw.py:287` and `:471`) invokes OpenClaw
  with `prompt=task.prompt.text` and the container CLI runs
  `openclaw agent --local --json --message <prompt>`
  (`harnesses/_openclaw_container.py:258`) — **text only**. There is no
  `--attachments`/`--files`/vision-block mechanism.
- Image fixtures ARE copied into the container work-dir (via `inject_files` /
  `_prepare_workdir`) as **on-disk files**, so the agent can only "see" them through a
  vision/caption *tool*, never as a native vision input to the model.
- The trace adapter (`harnesses/_trace_adapter.py`) translates openclaw's text-only
  session messages as-is and emits no media events.

So for the openclaw harness, `input_modalities: [text, image]` is a **no-op**.

## Verdict

- Declaring `input_modalities: [text, image]` **does** fix image-input scoring for the
  **native claweval harness** (`--harness claweval`), where the loop injects the image
  as a base64 vision block and emits `MediaLoad(modality=image, status=loaded)`.
- It does **NOT** fix anything for the **openclaw harness**, which has no vision-input
  pathway — openclaw exposes images only as files on disk and only passes the text
  prompt to the model. For openclaw, T057 can at best be solved via a real vision/caption
  *tool* that returns the brand (the mock `caption` service here returns a generic whale
  description, so even tool-mediated solving fails and the model guesses Docker).
- **Caveat:** this is image-input only. Video tasks additionally require a
  video-capable model endpoint, not merely this flag.

## Reusable recipe

For ANY future multimodal (image-input) rollout:

1. Use a config that declares `input_modalities: [text, image]` **and** an
   image-capable model endpoint (claude-sonnet-4-5 on deepwisdom newapi qualifies).
2. Run it on the **native claweval harness** (`--harness claweval`) so the loop's media
   injection path actually fires and emits `MediaLoad(...loaded)`.
3. Do **not** expect openclaw (`--harness openclaw`) to consume vision input — it only
   delivers attachments as files and prompts the model with text. Solving an image task
   under openclaw requires a working vision *tool*, not the `input_modalities` flag.
