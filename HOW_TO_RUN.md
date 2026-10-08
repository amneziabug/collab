# How to run it (cheat sheet)

## Upload new videos — the normal routine

1. Put your video(s) in **`C:\Users\Acer\Videos\shortpipe`**.
   Only use videos you made or own.
2. Open **Ubuntu (WSL)** from the Start menu.
3. Run:
   ```bash
   ~/github/collab/upload.sh
   ```
4. Look for `Uploaded #N as https://youtu.be/...` and check
   **studio.youtube.com → Content → Shorts**.

That's it. Videos already uploaded are skipped. Want to see what would happen first?
```bash
~/github/collab/upload.sh --dry-run
```

**Even quicker, no typing:** put a double-click shortcut on your Desktop (one time):
```bash
cp ~/github/collab/windows/"Upload to YouTube.bat" /mnt/c/Users/Acer/Desktop/
```
From then on, double-click **Upload to YouTube** on your Desktop.

---

## If something goes wrong

| You see | Do this |
|---|---|
| `YouTube credentials missing or expired` | The YouTube login expires every 7 days (app is in "Testing"). Run `cd ~/github/collab && source ../venv/bin/activate && python3 -m shortpipe auth --no-browser`, open the printed link in your browser, click **Continue** → **Allow**. Then run `upload.sh` again. |
| `Cannot reach Ollama` | Open a second Ubuntu window, run `ollama serve`, leave it open, try again. |
| `still_queued` is more than 0 | YouTube's free limit is ~6 uploads/day. Run `upload.sh` again tomorrow. |
| `Video folder not found` | Check the folder exists and matches `path` under `[folder]` in `config.toml`. |
| `rejected` / `duplicate of #N` | That exact file was already uploaded. |
| A video is too short on YouTube | Videos are cut to `max_duration_seconds` (under `[processing]` in `config.toml`). Set it to `180` for up to 3 minutes. |
| Anything else | Run `python3 -m shortpipe status` and read the last events; logs are in `~/github/collab/logs/pipeline.jsonl`. |

---

## Settings you might change

Edit with `nano ~/github/collab/config.toml` (save: **Ctrl+O**, **Enter**, exit: **Ctrl+X**).

```toml
[folder]
path = "/mnt/c/Users/Acer/Videos/shortpipe"   # C:\ → /mnt/c/ and \ → /
owner = "Tamako"

[youtube]
title = "#fyp #viral"     # "" = let the AI write titles
description = "none"      # "ai" = let the AI write descriptions
privacy = "private"       # "public" to publish (unaudited Google projects may be forced to private)
publish_interval_minutes = 0   # 60 = 1st video public now, the rest 1 hour apart (needs privacy = "public")

[processing]
max_duration_seconds = 180

[ai]
provider = "ollama"
ollama_model = "qwen2.5:7b"
# ollama_vision_model = "gemma3:4b"   # optional: AI looks at video frames
```

## Other commands

Run these from `~/github/collab` after `source ../venv/bin/activate`:

| Command | What it does |
|---|---|
| `python3 -m shortpipe status` | What's uploaded, queued, waiting |
| `python3 -m shortpipe upload-now` | Same as `upload.sh` |
| `python3 -m shortpipe retry-failed` | Retry videos that failed |
| `python3 -m shortpipe daemon` | Old scheduled mode (4–5 a day, laptop must stay on) |
| `git pull` | Get the latest version of the code |
| `rm data/pipeline.db` | Start over (forgets history; doesn't delete anything on YouTube or in your folder) |

## Important files (never share or upload these)

- `secrets/client_secret.json` — your Google OAuth client
- `secrets/youtube_token.json` — your YouTube login
