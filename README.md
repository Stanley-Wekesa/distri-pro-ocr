# Distri-Pro Asynchronous OCR Service

Independent CPU-based PaddleOCR service for delivery-note proof documents. It accepts a temporary signed file URL, persists the source and job before acknowledging, then processes pages sequentially in durable chunks. It reports progress/results to Distri-Pro using signed HMAC-SHA256 callbacks.

**OCR never approves a delivery or marks a delivery completed.** Approval remains an administrator action in Distri-Pro.

## Implemented API

- `GET /health` — service, database and worker health (no API key; do not expose publicly).
- `POST /jobs` — authenticated job submission; returns HTTP 202 after the source file and job are persisted.
- `GET /jobs/{jobId}` — authenticated status/progress.
- `GET /jobs/{jobId}/results?offset=0&limit=100` — authenticated paginated page results.
- `GET /docs` — FastAPI interactive API documentation.

All job endpoints require `X-API-Key`.

### Submit job

```http
POST /jobs
X-API-Key: <OCR_API_KEY>
Content-Type: application/json
```

```json
{
  "proofId": "123",
  "companyId": "1",
  "contractId": "456",
  "fileUrl": "https://private-storage.example.com/signed-file",
  "fileName": "county-delivery-notes.pdf",
  "mimeType": "application/pdf",
  "callbackUrl": "https://app.example.com/_api/proof_ocr_callback"
}
```

Response (`202 Accepted`):

```json
{"accepted":true,"jobId":"uuid","status":"queued","fileSha256":"..."}
```

The file URL host must be listed in `FILE_URL_ALLOWED_HOSTS`; callback URL host must be listed in `CALLBACK_ALLOWED_HOSTS`. Both URLs must use HTTPS. Redirects are not followed. Configure exact hostnames, comma-separated, without scheme/path.

### Callback signing

Every callback is a POST with `Content-Type: application/json` and:

`X-OCR-Signature: sha256=<hex HMAC-SHA256>`

The signature is calculated over the exact raw JSON bytes sent, using `OCR_CALLBACK_SECRET`. If that variable is empty, `OCR_API_KEY` is used as a fallback. Distri-Pro must verify the signature against the raw request body using constant-time comparison. Callback delivery is retried with exponential backoff; callbacks are at-least-once, so the receiver should process duplicate events idempotently.

Progress payloads contain the pages completed in that chunk, global `pageNumber` values, total/processed counts and SHA-256. The final event is `ocr.completed` with `status: completed` and `completed: true`. Failed jobs emit `ocr.failed` with the last successful processed-page count and error.

## Persistence and recovery

- SQLite database: `/var/data/jobs.sqlite3` (WAL mode).
- Source files: `/var/data/sources/`.
- Page results and chunk completion are committed before the next chunk begins.
- One sequential worker processes chunks; one callback dispatcher sends callbacks.
- On restart, in-flight chunks are returned to the queue; completed chunks/pages remain persisted.
- A repeated submission with the same company, proof, contract and file hash returns the existing job.
- Source files are retained after completion. Implement and agree a retention policy before enabling automatic deletion.
- SQLite requires one service instance and one Uvicorn worker. Do not horizontally scale this deployment.

## Configuration

Copy `.env.example` to `.env` and set all secrets and allowlists. Generate separate secrets:

```bash
openssl rand -hex 32
```

Set `FILE_URL_ALLOWED_HOSTS` and `CALLBACK_ALLOWED_HOSTS` to the real exact hostnames used by your private storage and Distri-Pro deployment. Do not use wildcards. Do not commit `.env`.

Initial defaults: 100 MB maximum file, 1,000 PDF pages, 10 pages per chunk, max chunk setting 20, two retries after the first failed attempt, one sequential worker. These are starting limits and must be load-tested on the actual VM. Ensure the Proxmox VM has sufficient free disk for source files, SQLite and Docker images.

## Deploy on Ubuntu VM with Docker Compose

1. Install Docker Engine and the Compose plugin using Docker's official Ubuntu instructions.
2. Clone the private GitHub repository and enter the project directory.
3. Create `.env` from `.env.example`, set secrets and real host allowlists.
4. Edit the `ports` entry in `docker-compose.yml` to bind the OCR VM's actual private static IP. Do not publish port 8080 to the public internet.
5. Create persistent storage and start:

```bash
mkdir -p data
chmod 700 data
sudo docker compose build
sudo docker compose up -d
sudo docker compose logs -f
```

6. In another shell, verify:

```bash
curl http://127.0.0.1:8080/health
sudo docker compose ps
```

The initial image build downloads PaddleOCR models and can take time and disk space. Keep the VM's data directory on persistent storage and back it up. Proxmox VM start-at-boot should be enabled.

## Important production limitations / integration notes

- Configure the exact file-storage and callback hostnames before the first job. The service intentionally rejects unlisted hosts to reduce SSRF risk.
- The temporary file URL must remain valid long enough for the service to download the full file during submission. Submission returns only after download, validation, hashing and persistence; OCR itself is asynchronous.
- Callback delivery is at-least-once. Distri-Pro should deduplicate repeated callback events using job ID, event type and processed-page position/status.
- The implementation currently extracts serial candidates and raw OCR text. It does not infer school, county or subcounty, and those values are not fabricated.
- The service has not been certified production-ready. Test with real documents, including 1-page images, small PDFs, 20-page PDFs, 135-page PDFs, and larger PDFs where VM resources allow. Also test worker restart, callback outage, invalid key, expired URL, oversized file and page-limit rejection.
- Measure processing time, RAM and disk use on the actual Proxmox VM before increasing limits.
- Restrict inbound TCP 8080 at the Proxmox firewall/network layer to the Distri-Pro App Server. Do not rely solely on UFW when using Docker-published ports.
- Use HTTPS between systems. If the App Server and OCR VM communicate over a private network, put a trusted reverse proxy with TLS in front of the OCR service or otherwise establish an approved secure transport. Do not expose raw HTTP publicly.

## Legacy synchronous endpoint

The old `POST /ocr` endpoint has been removed from this asynchronous service. The separate Distri-Pro integration must use `POST /jobs`, poll status/results as needed, and receive callbacks. Coordinate that API contract with the application-development workstream before connecting production uploads.
