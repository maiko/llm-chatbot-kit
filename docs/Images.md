# Image generation and configurable text backends

The kit supports `BOT_MODE=chat` (legacy default), `image`, and `both`.
Image mode requires no OpenAI key, text model or privileged Discord intents.
Combined mode adds image slash commands to the existing persona/chat features.
Endpoints and model IDs are configuration: the kit does not select a model,
provision infrastructure or assume where the backend runs.

## API contracts

Images use an authenticated HTTP(S) API with `POST /images/generations`, relative
to `IMAGE_API_BASE_URL`. Requests include `model`, `prompt`, `size`, `n=1` and
`response_format=b64_json`. The backend must return a PNG as
`data[0].b64_json`. The bot uploads those bytes to Discord and never follows a
result URL. `quality` and the nonstandard `seed` extension are opt-in per preset.
Backends using another protocol need a separate adapter.

Setting `TEXT_API_BASE_URL` selects the async Chat Completions adapter for chat
and the listening judge. It uses `POST /chat/completions` and standard JSON or SSE
responses, with no automatic request to another provider after failure.
The legacy OpenAI Responses adapter remains the default in chat mode.
Configured Chat Completions usage is not assigned a cloud price estimate.

TLS verification is enabled. `IMAGE_CA_FILE` and `TEXT_CA_FILE` optionally trust
a private PEM CA; leave them unset for normal system trust. Redirects are refused.
Resolve the configured hostnames through your deployment's DNS.

## Setup

Use Python 3.12 for the supplied deployment examples:

```bash
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps .
```

Copy `examples/image.env.example` to a private runtime file, fill in the Discord
token, API credentials and guild/channel IDs, then set permissions to `0600`.
Image and configured text modes require explicit guild/channel allowlists;
optional role allowlists restrict usage further. Empty required lists fail startup.
Configured text refuses DMs, bot authors and webhooks.

Use `BOT_MODE=both` with `TEXT_API_BASE_URL`, `TEXT_API_KEY`, `TEXT_MODEL`,
`TEXT_GUILD_IDS` and `TEXT_CHANNEL_IDS` to add Chat Completions replies.
`examples/local-bot.yml` provides a simple persona with passive listening disabled.
Mention the bot for a reply. Chat/both retain the inherited Message Content,
Members and Presence intents; image-only requires none of those privileged intents.

Image generation and Chat Completions run independently by default, so chat stays
available while an image is being generated. Each backend keeps its own single
request lock. Set `SERIALIZE_BACKENDS=true` when both endpoints share constrained
resources: the bot then shares one execution lock and chat waits for an active
image generation to release it. External applications are outside these locks; use a backend
scheduler when shared resources require one.

## Addressed reply queue

Mentions, configured word triggers and supported DMs enter a bounded FIFO reply
queue. The bot adds 📝 while the message is pending or being answered, then removes
its own reaction after delivery. Grant Add Reactions and Read Message History in
chat channels. Reaction failures are logged and do not discard a reply.
`TEXT_QUEUE_LIMIT` defaults to 20 pending replies (1–100), plus one active reply;
a full queue receives an explicit retry notice.

Only one reply executes at a time. Addressed replies wait for persona quota windows
instead of being rejected; spontaneous interventions are skipped when busy or
rate limited. Access is checked again before execution and after quota waits.
Each input snapshot ends at its own event, with preceding queued answers inserted
beside their questions. Later messages cannot change an earlier queued request.

This text queue lives in memory and is not replayed after a restart. Wait until
pending replies and image jobs have finished before replacing the bot. Image queue
persistence and recovery remain independent. `--no-stream` sends complete replies;
streaming is an optional delivery mode with the same reply queue.

## Configurable image presets

`IMAGE_PRESETS_JSON` maps up to 25 user-facing preset names to backend settings.
The default is `{"default":{"model":"default"}}`; use your backend's actual
model ID or alias. `IMAGE_DEFAULT_PRESET` defaults to `default` and must name one
of the configured presets. For example:

```json
{
  "default": {"model": "your-image-model"},
  "preview": {
    "model": "your-preview-model",
    "quality": "fast",
    "size_multiple": 32,
    "min_size": 256,
    "max_size": 1536,
    "max_pixels": 1572864,
    "supports_seed": true
  }
}
```

Preset names contain 1–32 lowercase letters, digits, underscores or hyphens.
Unknown settings and invalid limits fail startup. A preset's model ID is required;
quality is omitted unless configured. Default size constraints are 64–2048 per
edge, multiples of 1 and at most 4,194,304 pixels; configure them to match your API.
The slash size defaults to 1024×1024. Prompt limit is 4000 characters.

Seed is a decimal string to preserve the full 63-bit range through Discord.
Only `supports_seed=true` sends the `seed` extension; supplied seeds are refused
for other presets. A seed omitted for a supporting preset is generated and saved.
Changing presets requires guild command synchronization again.

Set `IMAGE_SYNC_COMMANDS=true` for initial registration or command changes,
then turn it off. Sync is limited to `IMAGE_GUILD_IDS` and runs in `setup_hook`,
not on reconnect. It writes Discord command state. Invite with `bot` and
`applications.commands`; grant View Channel, Send Messages (or Send Messages
in Threads) and Attach Files in the allowed channels. Administrator is unnecessary.

## Fast and quality modes

Optionally map two modes to existing presets:

```dotenv
IMAGE_MODE_PRESETS_JSON={"fast":"preview","quality":"default"}
IMAGE_DEFAULT_MODE=quality
IMAGE_QUALITY_RERUN_ENABLED=true
```

The mappings are operator configuration; the kit does not choose models, sampling
steps or deployment locations. Both presets must support seeds when reruns are enabled.
Run guild command synchronization after enabling these options.

`/image-mode fast` or `/image-mode quality` saves your default for this server,
independently of other members and servers. `/imagine mode:fast` or `mode:quality`
overrides it for one request. An explicit `preset` also overrides the default;
select either a mode or a preset, not both. Image tool guidance uses the member's
selected preset, and calls without a preset inherit it.

With reruns enabled, a completed fast image includes **Regenerate in quality**.
Only its requester can use the button or `/image-quality [job_id]`. This submits a
new job with the same prompt, seed and dimensions through the configured quality
preset. Queue limits, daily quotas and current channel/role permissions apply.
It is a new generation, not an upscale: changing models or settings can change
composition despite an identical seed. An uncertain generation cannot be rerun.

Enabling reruns retains the prompt alongside the completed image until
`IMAGE_RETENTION_HOURS` expires (24 hours by default), even when prompt publication
is disabled. Cancelled, failed and uncertain-generation prompts are erased.
Buttons are restored after restart; expired jobs fail without submitting anything.
Server-specific mode preferences remain in the private state database until removed
by the operator. With reruns disabled, existing terminal prompt erasure is unchanged.

## Commands and progress

| Command | Behavior |
|---|---|
| `/imagine prompt preset size seed mode` | Validate and queue one image using a configured preset. |
| `/image-status [job_id]` | Your job's state, position, queue size and safe error code; private reply. |
| `/image-cancel [job_id]` | Cancel your own pending job; never interrupts a running backend task. |
| `/image-result [job_id]` | Privately retrieve your retained PNG without regeneration. Explicit retrieval may produce another copy. |
| `/image-resolve job_id backend_idle_confirmed` | Configured owner only: release an uncertain generation after independently checking backend idle state. |

Admission defers the interaction immediately and acknowledges it privately.
A public status message in the request channel shows waiting, position and queue
size, then generating, then receives the final PNG in that same message.
Position/total include all outstanding bot jobs, including the running one;
waiting counts only queued jobs. These numbers exclude external backend clients.
Status updates on queue changes, with a five-second polling fallback; unchanged
content is not edited. English and French status strings are included.

### Optional backend progress

`IMAGE_PROGRESS_ENABLED=true` enables an explicit, nonstandard telemetry contract.
The kit sends a fresh opaque `X-Image-Request-ID` header with each generation POST
and polls `GET images/progress/{request_id}` on the same configured API every five
seconds, using the same credential and verified TLS. The API returns JSON up to
4096 bytes: `{"phase":"sampling","value":12,"max":25}`. Supported phases are
`preparing`, `sampling`, `decoding`, `saving`, `completed`, and `unavailable`.
Step counts must be integers with `0 <= value <= max <= 100000` and `max > 0`;
counts can be null before the sampler starts. Other phases use null counts.
No backend URL, prompt, preview or arbitrary phase text is shown.

The existing status message shows a ten-segment bar, sampler step count and
percentage, followed by preparation/decoding/finalization/upload states. Sampling
100% means the sampling stage finished; it does not mean the image was delivered.
Progress failures hide the bar and leave normal generation/delivery running;
progress never retries a generation. Telemetry is transient and is discarded on
restart or when the job leaves its running state. The final PNG uses the same
message and cannot be overwritten by a late progress update. Leave the option off
for standard image APIs without this extension.

Queue capacity defaults to five outstanding jobs, one per user; quota defaults
to ten admissions per rolling 24 hours. Failed and cancelled jobs consume an
admission. Limits apply before generation. Cancellation and failure update the
public status too. The persisted channel message ID is independent of the
interaction token's 15-minute expiry, so long jobs and restarts can still deliver.
If the status cannot be published, `/image-status` remains available and the PNG
uses a normal channel message. A confirmed deleted placeholder allows a fresh send.

## Images requested in conversation

Set `IMAGE_TOOLS_ENABLED=true` in `BOT_MODE=both` with a configured Chat Completions
backend that supports standard `tools` and structured `tool_calls`. The default is
false. Addressed chat turns use one completion with `tool_choice=auto`;
ordinary text replies still work, and passive listening never receives this tool.
Streaming is supported: text arrives progressively while tool-name/argument
fragments stay buffered. Admission waits for a complete stream with a valid finish
reason and explicit `[DONE]`; truncated, oversized or disconnected tool streams
never submit an image. There is no second completion after streaming failure.
`--no-stream` selects the complete-response path instead.

`/image-result` shows queue/progress status while a request is still active,
and paused status for an uncertain generation. It only offers a retained image
after generation finishes; an active job is not reported as a missing result.
A model can rewrite an image request and call `generate_image` with `prompt`,
optional `preset` and `size`. The kit validates these arguments before submitting
the same queue used by `/imagine`.

The requester and destination come from the Discord message, never from the model.
Image allowlists, roles, current channel permissions, quotas, size limits and one
outstanding request per user apply. At most one call is accepted per message;
unknown tools, extra arguments and multiple calls are rejected without admission.
Discord message IDs make admission idempotent. A deterministic queue receipt is
returned after submission; no second completion or recursive tool loop runs.
The model decides whether the current user asked for an image, so enable the feature
only with a backend qualified for this behavior. It grants no access to shell,
files, arbitrary URLs or other Discord destinations.

`IMAGE_PROMPT_GUIDANCE_FILE` optionally loads a UTF-8 file up to 16,000 characters
at startup. Mount it read-only and put backend-specific visual instructions there,
for example how to describe subjects, lighting, composition and style. The kit
adds available presets and dimension limits automatically. Keep private deployment
information out of public examples; the file is sent to your configured text API.
Missing files or oversized guidance fail startup. `/imagine` preserves the user's
prompt and does not invoke the text model.

## Persistence and failure handling

`IMAGE_STATE_DIR` holds the single-process SQLite queue, lock and PNG artifacts.
Directory permissions are `0700`, DB/PNG `0600`; protect volume backups as well.
By default, prompts are kept only while queued/running and removed at ready/terminal states.
With `IMAGE_INCLUDE_PROMPT=true`, the exact API prompt is retained through delivery
and published with the final image, inline or as a UTF-8 text attachment when
it exceeds Discord's message length. Unless quality reruns are enabled, it is erased
from the queue on terminal states; cancellation and uncertain recovery always erase it. `/image-result` retrieves only the retained PNG.
Enable this option only when prompt publication is wanted.
Completed jobs expire after `IMAGE_RETENTION_HOURS` (default/minimum 24 hours,
preserving quota accounting). Pending and unresolved generations survive restart.
Logs contain job IDs/states/errors, not prompts or inbound message excerpts.
A second process owning the same state directory is rejected.

Only explicit backend 429 rejections are retried, up to three attempts with bounded
Retry-After. Network timeout, 5xx or interrupted generation becomes **unknown**,
never an automatic new generation. New image dispatch pauses until the owner independently checks the backend and
resolves the unknown job. Serialized mode also blocks new configured text calls;
independent mode keeps chat available.
A timed-out request may still complete at the server; the kit never calls a
backend's global cancellation endpoint.

PNGs are saved before upload. Restart from `ready` delivers without generation.
An interrupted or ambiguous upload becomes `delivery_unknown` and is never
resent automatically; its owner may use `/image-result`. Destination/member role
and permissions are rechecked before delivery. Missing permissions, revoked
access, deleted destinations and oversized files keep explicit failure state.
Attachment limits come from Discord's current interaction/guild limits.
Reconnect does not duplicate workers, status updaters or the periodic chat saver.

## Deployment and rollback

`examples/llm-chatbot.service` runs a dedicated Linux user with a private env file,
restricted state directory, private devices, `0077` umask and 768 MiB memory cap.
`examples/docker-compose.images.yml` builds the source with pinned dependencies,
UID 1000, a read-only root and 512 MiB/one CPU limits. Prepare `examples/data`
owned by UID 1000, private `examples/image.env` and `examples/config/bot.yml`.
Mount a public CA file only when required. Adjust resource budgets for your workload.

```bash
cd examples
docker compose -f docker-compose.images.yml config --quiet
docker compose -f docker-compose.images.yml up -d --build
```

Set `BOT_READY_FILE` to an optional private writable path for connection readiness.
The marker is cleared at startup, disconnect and close, and created when Discord
is ready or resumed. A container healthcheck can test its presence without reading
conversation logs. It proves a gateway connection, not successful inference.

Keep one replica per token/state directory. Stop the bot before rollback, retain
its private state, then select the previous checkout/image. Backend lifecycle and
model provisioning remain deployment responsibilities.

Primary contracts: [discord.py interactions](https://discordpy.readthedocs.io/en/stable/interactions/api.html),
[HTTPX async API](https://www.python-httpx.org/async/),
[Python 3.12.15 release](https://www.python.org/downloads/release/python-31215/).
