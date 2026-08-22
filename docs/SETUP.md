# Setup Reference

This is the detailed reference for every setting. For the quick one-time
setup, use `../GETTING_STARTED.md` instead — come here only if you need more
detail on a specific step, or you're setting this up on a new machine.

## 1. Install the server-side dependencies

```bash
cd /home/enjay/projects/meeting-saathi
pip3 install --user -r requirements.txt
playwright install chromium
```

`faster-whisper` and `ctranslate2` (used for local transcription) may
already be installed on this machine — `pip3 install` will just confirm that
and skip re-downloading them.

If you'd rather isolate this project's Python packages from the rest of your
system instead of using `--user`:
```bash
sudo apt install python3.10-venv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```
(then remember to `source .venv/bin/activate` every time before running it)

## 2. Install the Chrome extension

See `INSTALL_EXTENSION.md`. This is what actually records your meetings —
without it, nothing will trigger the pipeline below.

## 3. Create your `.env` file

```bash
cp .env.example .env
```

Then fill in each value. Full list of settings:

| Setting | What it's for | Required? |
|---|---|---|
| `GEMINI_API_KEY` | Lets the program ask Gemini to write your MOM/Requirement Gathering Sheet/Action Points | **Yes** (free signup, not a paid API) |
| `GEMINI_MODEL` | Which Gemini model writes the documents | No, has a sensible free-tier default |
| `WHISPER_MODEL_SIZE` | How accurate (and slow) local transcription is | No, `small` is a good default for this computer |
| `WHISPER_COMPUTE_TYPE` | Technical speed setting for transcription | No, leave as `int8` |
| `HUGGINGFACE_TOKEN` | Free local model that figures out "who said what" | **Yes** (free signup, not a paid API) |
| `ASSEMBLYAI_API_KEY` | Speeds up the rare case where the local model above is slow (sparse DOM speaker coverage) by using a paid cloud API instead | No — optional, paid (pay-as-you-go, ~$0.17/hour of audio). Leave blank to keep using the free local model for that case |
| `BASE_STORAGE_DIR` | Where your meeting folders get saved | No, defaults to `~/Downloads/Meeting Saathi` |
| `KEEP_RAW_RECORDING` | Whether to also keep the raw uploaded recording | No, off by default (transcript is the backup) |
| `PORT` | Which port the local server runs on — the extension is hardcoded to talk to `localhost:8420` | No, leave as 8420 unless you also edit `extension/offscreen.js` |

### Getting `GEMINI_API_KEY`

Genuinely free — no billing setup, no credit card, just a Google account:

1. Go to https://aistudio.google.com/apikey
2. Click "Create API key" and copy it
3. The free tier has daily/per-minute rate limits (see
   https://ai.google.dev/gemini-api/docs/rate-limits), which are far more
   than a normal day's meeting volume would use

### Getting `HUGGINGFACE_TOKEN`

The extension only gives the program one mixed audio recording (everyone's
voice combined) — there's no per-person separation coming from anywhere
else, so this local model is what identifies "who said what." It's free:

1. Sign up at https://huggingface.co
2. Go to https://huggingface.co/pyannote/speaker-diarization-community-1 and accept
   the model's terms (one click, no cost)
3. Create a read-access token at https://huggingface.co/settings/tokens
4. Put it in `.env` as `HUGGINGFACE_TOKEN`

## 4. Run the server

```bash
cd /home/enjay/projects/meeting-saathi
uvicorn app.main:app --host 127.0.0.1 --port 8420
```

Leave this running (or have your assistant run it in the background) — the
extension needs it reachable at `localhost:8420` any time you finish a
meeting.

## 5. Confirm it's working

```bash
pytest
```
All tests should pass with no setup required (they don't call any real
external services).
