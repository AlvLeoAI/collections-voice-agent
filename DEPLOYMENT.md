# Deployment Guide

This guide covers local container builds and Google Cloud Run deployment for `collections-voice-agent`.
The baseline FastAPI service runs with **zero paid API keys** and zero LLM calls in the core decision path.

## 1. Prerequisites

- Docker `>= 24.0`
- Python `>= 3.11` and [`uv`](https://docs.astral.sh/uv/)
- Google Cloud CLI (`gcloud`) authenticated against your target GCP project (for Cloud Run deployment)

## 2. Local Container Build & Smoke Test

Build the container image from the repository root:

```bash
docker build -t collections-voice-agent:local .
```

Run the FastAPI backend locally with **zero API keys**:

```bash
docker run --rm -p 8080:8080 collections-voice-agent:local
```

Verify the running container in another terminal:

```bash
curl -sf http://127.0.0.1:8080/health
curl -sf -X POST http://127.0.0.1:8080/call/start \
  -H "Content-Type: application/json" \
  -d '{"party_profile":{"target_name":"Alex Morgan"},"account_ref":"acct_demo_001"}'
curl -sf http://127.0.0.1:8080/metrics/summary
```

### Optional Local Streamlit UI Container

Reuse the same image to launch the Streamlit sandbox against a running API (text mode works offline with zero keys; pass `GEMINI_API_KEY` / `ELEVENLABS_API_KEY` only if you want live browser voice):

```bash
docker run --rm -p 8501:8501 \
  -e API_BASE_URL=http://host.docker.internal:8080 \
  collections-voice-agent:local \
  streamlit run frontend/app.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true
```

## 3. Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `PORT` | `8080` | No | Listening port (set automatically by Cloud Run). |
| `RUNTIME_DIR` | `/app/runtime` (Docker) / `runtime` (local) | No | Root directory for local JSON call, job, and attempt stores. |
| `API_BASE_URL` | `http://127.0.0.1:8000` | UI only | Base URL of the FastAPI service for the Streamlit frontend. |
| `GEMINI_API_KEY` | *(blank)* | Optional | Enables optional Gemini 3.5 Transcribe STT in the Streamlit UI (`store=False`). |
| `GEMINI_STT_MODEL` | `gemini-3.5-transcribe` | Optional | Gemini Interactions API model used for optional audio transcription. |
| `ELEVENLABS_API_KEY` | *(blank)* | Optional | Enables optional ElevenLabs in-browser TTS playback in the Streamlit UI. |
| `ELEVENLABS_VOICE_ID` | `21m00Tcm4TlvDq8ikWAM` | Optional | ElevenLabs voice ID used when TTS is enabled. |

## 4. Google Cloud Run Deployment (Single Image, Two-Service Optional Topology)

Set your target project and region first:

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
```

> **Single-Instance Persistence Constraint (`--max-instances 1`)**
>
> `collections-voice-api` persists call records, outbound jobs, and account attempt/suppression ledgers as local JSON files under `RUNTIME_DIR` protected by in-process `threading.Lock` and atomic `os.replace()` writes. Therefore, **`--max-instances 1` on Cloud Run and `--workers 1` in `Dockerfile` are mandatory** for this demo architecture to prevent cross-instance state divergence. Container storage on Cloud Run is also ephemeral across restarts.
>
> **Production upgrade path:** Replace `JsonCallStore`, `JsonJobStore`, and `JsonContactAttemptStore` with Cloud SQL (PostgreSQL) and move outbound job leasing/retries to Cloud Tasks or Pub/Sub before scaling horizontally.

### Service 1: `collections-voice-api` (Required — Zero Paid API Keys)

Deploy directly from source:

```bash
gcloud run deploy collections-voice-api \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 1 \
  --set-env-vars "RUNTIME_DIR=/app/runtime"
```

Capture the deployed API URL and verify `/health`:

```bash
export API_URL=$(gcloud run services describe collections-voice-api \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format "value(status.url)")

curl -sf "$API_URL/health"
```

### Service 2: `collections-voice-ui` (Optional Streamlit Sandbox)

Reuse the Artifact Registry image built for `collections-voice-api` so the image is only built once:

```bash
export IMAGE_URI=$(gcloud run services describe collections-voice-api \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format "value(spec.template.spec.containers[0].image)")

gcloud run deploy collections-voice-ui \
  --image "$IMAGE_URI" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 2 \
  --command "streamlit" \
  --args "run,frontend/app.py,--server.port=8080,--server.address=0.0.0.0,--server.headless=true" \
  --set-env-vars "API_BASE_URL=$API_URL"
```

To enable optional live browser STT (Gemini 3.5 Transcribe) and TTS (ElevenLabs) without rebuilding the image, attach Secret Manager secrets to `collections-voice-ui`:

```bash
gcloud run deploy collections-voice-ui \
  --image "$IMAGE_URI" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 2 \
  --command "streamlit" \
  --args "run,frontend/app.py,--server.port=8080,--server.address=0.0.0.0,--server.headless=true" \
  --set-env-vars "API_BASE_URL=$API_URL,GEMINI_STT_MODEL=gemini-3.5-transcribe" \
  --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest,ELEVENLABS_API_KEY=ELEVENLABS_API_KEY:latest"
```
