# shortpipe: AI-assisted short-form video pipeline (university project)

A Python demo of AI-assisted automation: it ingests short videos from an **authorized** dataset,
uses an AI model (a local model through Ollama by default, or the Claude API) to analyse and rank them, transcodes them to the Shorts format with ffmpeg,
generates YouTube metadata with AI, and uploads them on a schedule (4–5 per day) as **private**
videos through the YouTube Data API. Every step is recorded in SQLite and a JSON-lines log.

## Pipeline

```
 manifest.json ──► discover ──► rights check ──► AI analysis + ranking ──► ffmpeg processing
 (authorized          │             │ reject            │ reject                  │
  dataset)            ▼             ▼                   ▼                         ▼
                  SQLite: videos / ai_decisions / events         AI metadata (title/desc/tags)
                                                                                  │
                     YouTube Data API (private upload) ◄── upload-due ◄── scheduler (N slots/day)
```

Each video moves through these states, stored in `videos.status`:
`discovered → selected → processed → metadata_ready → scheduled → uploading → uploaded`,
with `rejected` (rights, views, relevance, AI flags) and `failed` (after retries) as exits.
Each stage only picks up videos in its input state, so you can re-run stages safely and run them
separately.

## Project layout

```
shortpipe/
  config.py           TOML config + env overrides (OLLAMA_HOST, ANTHROPIC_API_KEY, CLAUDE_MODEL)
  db.py               SQLite schema: videos, ai_decisions (prompt + raw response), events
  logging_setup.py    console logs + logs/pipeline.jsonl
  sources/
    base.py           VideoSource interface (add new authorized sources here)
    folder.py         a plain folder of your own videos (no manifest; title from filename/tags)
    local_dataset.py  JSON-manifest dataset with license + stats per video
    tiktok.py         your own TikTok videos via the official Display API (Login Kit + PKCE)
    rights.py         authorization gate (license, rights holder, attribution)
  ai/
    client.py         AI providers with JSON-schema output: Ollama (local), Claude API, offline heuristics
    analysis.py       topic / relevance / educational value / content flags
    metadata.py       title, description, tags; enforces YouTube limits, adds credits + AI note
  ranking.py          engagement + reach + AI relevance → final score
  processing.py       ffmpeg: trim ≤60s, 1080x1920 pad, H.264/AAC, loudness normalisation
  scheduler.py        evenly spaced daily slots in a local-time window
  youtube/
    auth.py           OAuth 2.0 installed-app flow, token cache + refresh
    uploader.py       resumable upload, retries with backoff, quota handling, private only
  pipeline.py         orchestration + error handling
  cli.py              command-line entry point
  demo_dataset.py     generates synthetic self-owned test clips
tests/                pytest (offline AI + fake uploader; no network needed)
```

**Day-to-day use:** see [HOW_TO_RUN.md](HOW_TO_RUN.md). Normally you only need `./upload.sh`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # ffmpeg must also be installed
cp config.example.toml config.toml
```

### AI provider

Choose the provider with `provider` under `[ai]` in `config.toml`.

**`ollama` (default): a free local model.** It runs on your own computer, so you need no account or key.

```bash
curl -fsSL https://ollama.com/install.sh | sh   # Linux / WSL (macOS & Windows: installer on ollama.com)
ollama pull llama3.2:3b                         # ~2 GB download, once
ollama serve                                    # only if it isn't already running in the background
```

`llama3.2:3b` runs on most laptops, even without a GPU. On a machine with a decent GPU, a larger
model (e.g. `qwen2.5:7b`, `llama3.1:8b`) gives better rankings and titles; pull it and set
`ollama_model`. On WSL, install Ollama *inside* WSL, so that `localhost:11434` works. If it runs on
the Windows side instead, set `ollama_url` / `OLLAMA_HOST` to the Windows host address.

**`claude`: the Claude API.** You need an API key with credit from console.anthropic.com (billed
separately from Claude.ai subscriptions). Set it with `export ANTHROPIC_API_KEY=sk-ant-...`.

**`offline`: deterministic keyword rules.** No AI, but every other stage runs. Use it for tests and demos.

### YouTube OAuth

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project and enable
   **YouTube Data API v3**.
2. Configure the **OAuth consent screen** (External, Testing) and add your Google account as a
   test user.
3. Under **Credentials**, create an OAuth client ID of type **Desktop app**, download the JSON and
   save it as `secrets/client_secret.json` (git-ignored).
4. Run `python -m shortpipe auth`. On a headless server use `--no-browser` and forward the port
   (`ssh -L 8080:localhost:8080 server`).

The app only requests the `youtube.upload` scope. While the consent screen stays in "Testing",
refresh tokens expire after 7 days; run `auth` again when `upload-due` reports expired credentials.

## Usage

```bash
python -m shortpipe make-demo-dataset   # synthetic test clips + manifest (all self-owned)
python -m shortpipe run                 # discover → analyze → process → metadata → schedule
python -m shortpipe status              # counts, schedule, recent events
python -m shortpipe upload-due --dry-run
python -m shortpipe upload-due          # uploads anything whose slot has passed
python -m shortpipe daemon --interval 300
python -m shortpipe upload-now --dry-run    # preview: everything ready, uploaded immediately
python -m shortpipe upload-now              # prepare all videos and upload them right away
```

With `[youtube] privacy = "public"` and `publish_interval_minutes = 60`, `upload-now` still uploads
everything at once. The first video goes public immediately, and each next one gets a YouTube
`publishAt` time 60 minutes after the previous. YouTube then publishes them by itself, so the
laptop can be off. A later run continues 60 minutes after the last planned one.
`status` shows each video's `public at` time. If YouTube ignores the publish time (unaudited
projects are locked to private), the run logs a warning.

With `publish_window = "18:00-22:00"` and `publish_timezone = "America/New_York"`, publish times
are kept inside that daily window. Videos that don't fit move to the next day's window. A run
inside the window publishes the first video immediately.

`style = "creator"` asks the AI for short, specific titles, and for descriptions that end with 2–3
hashtags. It's meant for your own non-educational videos, and ignores meaningless filenames.
`title_hashtags` is added after each AI title. For your own folder, `skip_analysis = true` skips
the ranking call, but the optional vision step still runs, so titles still match the video.

`upload-now` doesn't need a running daemon or an open laptop. It runs discover → analyze →
process → metadata, then uploads every ready or scheduled video at once (with `[youtube] privacy`), and
exits. YouTube's free quota allows about 6 uploads a day, so anything beyond that stays queued, and
the next `upload-now` uploads it.

Each stage can also be run on its own: `discover`, `analyze`, `process`, `metadata`, `schedule`.
`retry-failed` sends `failed` videos back to the stage that failed, so the next `run` tries them again.
If the AI provider is unavailable (Ollama not running, model not pulled, missing API key), `run` stops with an error before analysis and leaves the videos untouched.

Instead of `daemon`, you can use cron:

```cron
*/10 * * * * cd /path/to/collab && .venv/bin/python -m shortpipe run && .venv/bin/python -m shortpipe upload-due
```

### Using your own dataset

Put your clips in `data/dataset/` and describe them in `manifest.json` (format documented in
`shortpipe/sources/local_dataset.py`). Every entry needs a `license` from
`selection.allowed_licenses` and a `rights_holder`, and `cc-by` entries also need an `attribution`.
Anything else is rejected at discovery and is never processed. The `stats` can come from your own
analytics export or be synthetic. To add another permitted source (for example your own channel's
analytics), implement `VideoSource.discover()`.

### Using a folder of your own videos

The simplest way to use real videos: put them in one folder, and every run picks up new files.

```toml
[sources]
enabled = ["folder"]

[folder]
path = "/mnt/c/Users/YOURNAME/Videos/shortpipe"   # on WSL; any path works
owner = "Your Name"
```

- Each new file is analysed and processed, then the AI writes its title, description and tags,
  and it's scheduled. Because these are your own videos, `auto_approve = true` schedules all of
  them unless the AI flags a problem. There's no view-count threshold, since a folder has no stats.
- The starting title comes from the filename (`my_pendulum-test.mp4` → "My pendulum test"), or
  from a title or comment stored in the file. Descriptive filenames give better results.
- **Letting the AI see the video (optional).** A text model like `qwen2.5:7b` only reads the
  filename and tags. To base titles on what's actually on screen, pull a small vision model and set
  it in `config.toml`:
  ```bash
  ollama pull gemma3:4b
  ```
  ```toml
  [ai]
  ollama_vision_model = "gemma3:4b"
  ```
  For each video, ffmpeg grabs `vision_frames` frames, and the vision model describes them. That
  description goes into the analysis and metadata prompts, and is stored in `visual_summary` and
  `ai_decisions` (stage `vision`). If the vision step fails, the video goes ahead without it.
- **Fixed title / no description.** `[youtube] title = "#fyp #viral"` uses that title for every
  upload, and `description = "none"` sends no description. A CC BY credit is still added when
  the licence requires one. The AI still runs analysis and tags, and its suggested
  title/description stay in `ai_decisions`. The overrides apply at upload time, so they also
  cover videos that were already prepared.
- In your own folder, only serious AI flags (unsafe, misleading, harmful, ...) block a video.
  Minor ones like "not_educational" don't.
- Files are tracked by name and size. Renaming or re-saving a file makes it a new video, but an
  identical copy of a file already used is rejected as a duplicate.
- Videos longer than `processing.max_duration_seconds` (default 60) are cut to that length.
  Shorts can be up to 3 minutes, so raise it to 180 if your videos are longer.

### Using your own TikTok videos

The `tiktok` source uses TikTok's official **Display API**. It lists the videos posted by the account
you log in with, together with their view, like, comment and share counts. The pipeline ranks those
with AI, uploads the best ones to YouTube as private videos, and schedules 4–5 a day, like any other
source.

The source doesn't scrape TikTok or download other people's videos. The Display API only returns
your own videos and has no download endpoint, so you supply the video files yourself, from TikTok's
data export or "Save video" on your own posts.

1. **Create a TikTok app (free).** Log in at https://developers.tiktok.com, then go to **Manage apps** →
   **Connect an app**. Add the **Login Kit** product with the **Desktop** platform, and the scopes
   `user.info.basic` and `video.list`. Register this redirect URI:
   `http://localhost:8765/callback/`. Use **Sandbox** mode and add your TikTok account as a target
   user. You don't need app review.
2. **Configure.** Copy the client key and client secret, then:
   ```bash
   export TIKTOK_CLIENT_KEY=...  TIKTOK_CLIENT_SECRET=...
   ```
   In `config.toml`, set `enabled = ["tiktok"]` under `[sources]`.
3. **Log in.** Run `python -m shortpipe tiktok-auth --no-browser` and open the printed URL. The
   token is saved in `secrets/tiktok_token.json` and refreshed automatically.
4. **Add your video files.** Export your data from TikTok (Settings → Account → Download your data),
   or save your own posts. Put the files in `data/tiktok/`, named `<video_id>.mp4`. A filename that
   contains the id also works. `python -m shortpipe status` lists the ids it is waiting for.
5. **Run** `python -m shortpipe run` as usual. A selected video whose file isn't there yet stays
   `selected` with "waiting for local file", and is processed on the first run after you add it.

TikTok's **Research API** (approved academic access only) returns metadata for popular public
videos, and could feed trend analysis into the ranking. It doesn't provide video files, and
re-uploading other creators' content isn't permitted, so this project doesn't use it for uploads.

## Design notes

- **Privacy.** Uploads are private by default. `[youtube] privacy` can be set to `"unlisted"` or
  `"public"`. Google locks videos uploaded through the API by **unaudited** Cloud projects to
  private, whatever is requested. The uploader compares what YouTube actually applied, logs a
  warning if the video was forced to private, and records the real privacy. Publishing through the
  API needs the [YouTube API compliance audit](https://support.google.com/youtube/contact/yt_api_form).
  `status.publishAt` is never set. The "scheduled publication time" is when *our* scheduler
  uploads it.
- **Quota.** The default YouTube Data API quota is 10,000 units/day and `videos.insert` costs about
  1,600, so `uploads_per_day` is limited to 1–6. On `quotaExceeded`, the video goes back to
  `scheduled` and uploading stops until the next run.
- **AI providers.** All providers share one interface (`AIClient.complete_json`) and get the same
  prompts and JSON schemas, so you can switch with one config line and compare the results.
  - **Ollama:** the schema is passed as Ollama's `format` (structured outputs). Small local models
    still sometimes skip fields or score on 0–10 instead of 0–1, so responses are validated against
    the schema and retried, and scores are normalised to 0–1.
  - **Claude:** calls `claude-opus-5-5` (change with `model` / `CLAUDE_MODEL`) with structured
    outputs and adjustable `effort`. Server-side fallbacks are on, so if the model declines a request,
    the API retries it on a fallback model.
- **Duplicates.** Each source file is hashed (SHA-256) before processing. A file identical to one
  already processed, scheduled or uploaded is rejected as `duplicate of #N`, even when it comes
  from a different source or has a different name. The same TikTok id is never imported twice.
- **Error handling.** The Anthropic SDK retries rate limits, 5xx and network errors with backoff
  (`ai.max_retries`). Provider problems (Ollama not running, model not pulled, missing or invalid
  API key) stop the stage without touching any video. Other
  AI errors mark just that video `failed`, and `retry-failed` sends it back. Uploads are
  resumable, retry 5xx and network errors with exponential backoff, and a video is marked `failed`
  after 3 attempts. An expired or revoked YouTube login, or a used-up quota, stops the upload run
  without using up the videos' attempts. TikTok rate limits and 5xx errors are retried with backoff.
  An expired TikTok login stops the run and asks you to run `tiktok-auth`. Other TikTok failures
  are logged and skipped, so the other sources still run. Every failure is stored in `last_error`
  and the `events` table.
- **Auditability.** Every AI prompt and raw response is stored in `ai_decisions`, so you can show
  why each video was selected or rejected and what metadata the model proposed.
- **AI disclosure.** Generated descriptions end with a note that the metadata was AI-assisted, and
  CC-BY content gets a credit line.

## Tests

```bash
pytest -q
```

The tests use the offline AI provider and a fake uploader. The end-to-end test generates real
clips with ffmpeg and runs every stage.
