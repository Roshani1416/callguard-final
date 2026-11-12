# CallGuard

CallGuard evaluates support calls against a configurable SOP. Whisper translates speech locally and a local sentiment model examines the transcript. The backend sends transcript text, checklist items and relevant policy text to Groq for evaluation. The concierge and SOP editor also use Groq. Firebase handles sign-in and the frontend's SOP documents.

## Run locally

On Windows, put the server-side key in `model/.env` as `GROQ_API_KEY=your_key`, install FFmpeg, then run `.\Start-Backend.ps1` from the project root. The launcher creates a project virtual environment, checks its dependencies and starts the API. Use `.\Start-Backend.ps1 -Diarization` if you have configured the optional speaker model.

Use Python 3.11 in a virtual environment and install FFmpeg (both ffmpeg and ffprobe must be on PATH). From the project root:

```sh
cd model
python -m pip install -r requirements.txt
python main.py
```

Set `GROQ_API_KEY` in the backend environment or in `model/.env`. The default Groq model is `llama-3.3-70b-versatile`. Override with `GROQ_MODEL`. Speech models download on the first analysis. For optional speaker labels, install `requirements-diarization.txt` and set `HF_TOKEN` for a Hugging Face account with access to the pyannote 3.1 pipeline and segmentation model. Without it, speaker labels are unknown.

In a second terminal run `npm ci` and `npm run dev` from the root. Open the printed Vite URL and sign in. The default backend is http://127.0.0.1:8000.

## Deploy

Build with `docker build -t callguard-api ./model` (add `--build-arg DIARIZATION=1` for speaker labels). Start one container with a persistent volume:

```sh
docker run -d --name callguard --restart unless-stopped --env-file /secure/path/callguard.env -v callguard-data:/data -v callguard-models:/root/.cache/huggingface -p 127.0.0.1:8000:8000 callguard-api
```

Expose the API through an HTTPS reverse proxy. Limit request bodies there to 101 MiB and allow a response timeout suitable for long CPU jobs. Set `CORS_ORIGINS` to the exact frontend origin (comma-separated for several). Set `FIREBASE_PROJECT_ID` to the frontend Firebase project. Set `VITE_API_BASE_URL` to the public HTTPS backend URL **before** building the frontend. On Netlify use `npm run build` and publish `dist`. Keep the Groq key on the backend.

## Access and storage

Each signed-in user sees their own call insights and policy documents on the backend. An administrator with the Firebase custom claim `admin: true` can see all call insights and update the shared SOP. Publish the included firestore.rules for the users and sops collections in Firebase. If this Firebase project is shared with other apps, merge their existing collection rules before publishing.

Calls use SQLite. For migration, place the original calls.json in the persistent data directory before starting the backend. Imported records have no verified owner and are visible to administrators. Existing users should re-upload their policy PDF once to associate it with their account. New recordings are removed on request completion, including errors. Approved policy text and scored reports persist. The deployment image excludes previous recordings, reports, credentials and policy documents.

One worker processes one call at a time. A waiting request gets a retry message after ten seconds. Limit each call to 100 MiB and two hours and each PDF to 10 MiB. A missing or incomplete evaluation produces an error and creates no employee score.
