from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import fitz
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
from paddleocr import PaddleOCR
from PIL import Image, UnidentifiedImageError

APP_NAME = "Distri-Pro Asynchronous OCR Service"
VERSION = "1.0.0"
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
SOURCE_DIR = DATA_DIR / "sources"
DB_PATH = DATA_DIR / "jobs.sqlite3"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "104857600"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "1000"))
PAGES_PER_CHUNK = int(os.getenv("PAGES_PER_CHUNK", "10"))
MAX_CHUNK_SIZE = int(os.getenv("MAX_CHUNK_SIZE", "20"))
CHUNK_RETRIES = int(os.getenv("CHUNK_RETRIES", "2"))
PDF_RENDER_SCALE = float(os.getenv("PDF_RENDER_SCALE", "2.0"))
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "2"))
CALLBACK_POLL_SECONDS = float(os.getenv("CALLBACK_POLL_SECONDS", "3"))
DOWNLOAD_TIMEOUT_SECONDS = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "180"))
CALLBACK_TIMEOUT_SECONDS = float(os.getenv("CALLBACK_TIMEOUT_SECONDS", "20"))
OCR_API_KEY = os.getenv("OCR_API_KEY", "")
OCR_CALLBACK_SECRET = os.getenv("OCR_CALLBACK_SECRET", "")
FILE_URL_ALLOWED_HOSTS = {x.strip().lower() for x in os.getenv("FILE_URL_ALLOWED_HOSTS", "").split(",") if x.strip()}
CALLBACK_ALLOWED_HOSTS = {x.strip().lower() for x in os.getenv("CALLBACK_ALLOWED_HOSTS", "").split(",") if x.strip()}
SERIAL_REGEX = os.getenv("SERIAL_REGEX", r"(?i)\b[A-Z0-9]{2,}(?:[/-][A-Z0-9]{1,}){1,5}(?:\s+\d+)?\b")
TOP_CROP_PERCENT = 0.30

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("distri_pro_ocr")
app = FastAPI(title=APP_NAME, version=VERSION)
ocr_engine: PaddleOCR | None = None
stop_event = threading.Event()
worker_thread: threading.Thread | None = None
callback_thread: threading.Thread | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY, proof_id TEXT NOT NULL, company_id TEXT NOT NULL,
          contract_id TEXT NOT NULL, file_name TEXT NOT NULL, mime_type TEXT NOT NULL,
          source_path TEXT NOT NULL, file_sha256 TEXT NOT NULL, callback_url TEXT NOT NULL,
          status TEXT NOT NULL, total_pages INTEGER NOT NULL DEFAULT 0,
          processed_pages INTEGER NOT NULL DEFAULT 0, attempt_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(company_id, proof_id, contract_id, file_sha256)
        );
        CREATE TABLE IF NOT EXISTS chunks (
          chunk_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
          start_page INTEGER NOT NULL, end_page INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT NOT NULL,
          UNIQUE(job_id, start_page)
        );
        CREATE TABLE IF NOT EXISTS pages (
          job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE, page_number INTEGER NOT NULL,
          result_json TEXT NOT NULL, PRIMARY KEY(job_id, page_number)
        );
        CREATE TABLE IF NOT EXISTS callbacks (
          callback_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
          event TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status, chunk_id);
        CREATE INDEX IF NOT EXISTS idx_callbacks_status ON callbacks(status, next_attempt);
        """)
        # A process restart must release any chunk that was in-flight. Completed chunks remain completed.
        c.execute("UPDATE chunks SET status='queued', updated_at=? WHERE status='processing'", (now(),))
        c.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='processing'", (now(),))
        c.execute("UPDATE callbacks SET status='queued' WHERE status='sending'")


@app.on_event("startup")
def startup() -> None:
    global ocr_engine, worker_thread, callback_thread
    if PAGES_PER_CHUNK < 1 or PAGES_PER_CHUNK > MAX_CHUNK_SIZE:
        raise RuntimeError("PAGES_PER_CHUNK must be between 1 and MAX_CHUNK_SIZE")
    if not OCR_API_KEY:
        log.warning("OCR_API_KEY is not configured; authenticated endpoints will reject requests")
    if not OCR_CALLBACK_SECRET:
        log.warning("OCR_CALLBACK_SECRET is not configured; OCR_API_KEY will be used for callback signing")
    init_db()
    log.info("Initializing PaddleOCR engine")
    ocr_engine = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=True, engine="paddle")
    stop_event.clear()
    worker_thread = threading.Thread(target=worker_loop, name="ocr-worker", daemon=True)
    callback_thread = threading.Thread(target=callback_loop, name="callback-worker", daemon=True)
    worker_thread.start()
    callback_thread.start()
    log.info("OCR service started; single sequential worker enabled")


@app.on_event("shutdown")
def shutdown() -> None:
    stop_event.set()
    for thread in (worker_thread, callback_thread):
        if thread and thread.is_alive():
            thread.join(timeout=10)


def require_api_key(candidate: str | None) -> None:
    if not OCR_API_KEY:
        raise HTTPException(503, "OCR_API_KEY is not configured")
    if candidate is None or not hmac.compare_digest(candidate, OCR_API_KEY):
        raise HTTPException(401, "Invalid API key")


def allowed_url(raw: str, hosts: set[str], label: str) -> None:
    p = urlparse(raw)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise HTTPException(400, f"{label} must be an HTTPS URL")
    host = p.hostname.lower().rstrip(".")
    if not hosts or host not in hosts:
        raise HTTPException(400, f"{label} host is not in the configured allowlist")


class JobRequest(BaseModel):
    proofId: str = Field(min_length=1, max_length=200)
    companyId: str = Field(min_length=1, max_length=200)
    contractId: str = Field(min_length=1, max_length=200)
    fileUrl: HttpUrl
    fileName: str = Field(min_length=1, max_length=255)
    mimeType: str = Field(min_length=1, max_length=100)
    callbackUrl: HttpUrl


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download_source(url: str, destination: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    try:
        with httpx.stream("GET", url, follow_redirects=False, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Source file exceeds configured size limit")
            with destination.open("wb") as out:
                for block in response.iter_bytes(1024 * 1024):
                    size += len(block)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Source file exceeds configured size limit")
                    digest.update(block)
                    out.write(block)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not download source document: {type(exc).__name__}") from exc
    if size == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, "Downloaded source file is empty")
    return size, digest.hexdigest()


def pdf_page_count(path: Path, mime: str, filename: str) -> int:
    is_pdf = mime.lower() == "application/pdf" or filename.lower().endswith(".pdf")
    if is_pdf:
        try:
            with fitz.open(path) as pdf:
                if pdf.is_encrypted:
                    raise HTTPException(400, "Password-protected PDFs are not supported")
                pages = pdf.page_count
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, "The supplied PDF is invalid") from exc
        if pages < 1:
            raise HTTPException(400, "The PDF contains no pages")
        if pages > MAX_PDF_PAGES:
            raise HTTPException(413, f"PDF exceeds the {MAX_PDF_PAGES}-page limit")
        return pages
    if mime.lower() not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "Supported files are PDF, JPG, PNG and WEBP")
    try:
        with Image.open(path) as im:
            im.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(400, "The uploaded file is not a valid image") from exc
    return 1


def extract_result_text(result: Any) -> tuple[list[str], list[float]]:
    data = result.json if hasattr(result, "json") else result
    if callable(data): data = data()
    if isinstance(data, str): data = json.loads(data)
    if isinstance(data, dict) and "res" in data: data = data["res"]
    texts = data.get("rec_texts", []) if isinstance(data, dict) else []
    scores = data.get("rec_scores", []) if isinstance(data, dict) else []
    lines = [str(t).strip() for t in texts if str(t).strip()]
    confidence = []
    for score in scores:
        try: confidence.append(float(score))
        except (TypeError, ValueError): pass
    return lines, confidence


def average_confidence(scores: list[float]) -> float | None:
    return round(sum(scores) / len(scores), 4) if scores else None


def find_serial(lines: list[str]) -> tuple[str | None, bool]:
    pattern = re.compile(SERIAL_REGEX)
    labelled, candidates = [], []
    for line in lines:
        found = [m.group(0).strip() for m in pattern.finditer(line)]
        candidates.extend(found)
        if re.search(r"\b(serial|delivery\s*note|d\.?\s*n\.?)\b", line, re.I): labelled.extend(found)
    unique = list(dict.fromkeys(labelled or candidates))
    return (unique[0], False) if len(unique) == 1 else (None, True)


def run_paddle(image: Image.Image) -> tuple[list[str], list[float]]:
    if ocr_engine is None: raise RuntimeError("OCR engine is not ready")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp: temp_name = tmp.name
        image.convert("RGB").save(temp_name, format="JPEG", quality=95)
        lines, scores = [], []
        for result in ocr_engine.predict(temp_name):
            a, b = extract_result_text(result); lines.extend(a); scores.extend(b)
        return lines, scores
    finally:
        if temp_name: Path(temp_name).unlink(missing_ok=True)


def process_page(image: Image.Image, page_number: int) -> dict[str, Any]:
    width, height = image.size
    crop = image.crop((0, 0, width, max(1, int(height * TOP_CROP_PERCENT))))
    lines, scores = run_paddle(crop)
    serial, review = find_serial(lines)
    ocr_pass = "top_30_percent"
    if serial is None:
        lines, scores = run_paddle(image)
        serial, review = find_serial(lines)
        ocr_pass = "full_page_fallback"
    return {
        "pageNumber": page_number,
        "serialNumber": serial,
        "reviewRequired": review,
        "reviewReason": "Serial number was not uniquely identified; verify manually." if review else None,
        "ocrPass": ocr_pass,
        "ocrText": lines,
        "averageTextConfidence": average_confidence(scores),
    }


def render_page(source: Path, page_number: int, mime: str, filename: str) -> Image.Image:
    is_pdf = mime.lower() == "application/pdf" or filename.lower().endswith(".pdf")
    if is_pdf:
        with fitz.open(source) as pdf:
            page = pdf.load_page(page_number - 1)
            pix = page.get_pixmap(matrix=fitz.Matrix(PDF_RENDER_SCALE, PDF_RENDER_SCALE), alpha=False)
            return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    with Image.open(source) as im: return im.convert("RGB")


def queue_callback(conn: sqlite3.Connection, job: sqlite3.Row, event: str, pages: list[dict[str, Any]], completed: bool) -> None:
    payload = {
        "event": event, "jobId": job["job_id"], "proofId": job["proof_id"],
        "companyId": job["company_id"], "contractId": job["contract_id"],
        "status": "completed" if completed else "processing",
        "totalPages": job["total_pages"], "processedPages": job["processed_pages"],
        "fileSha256": job["file_sha256"], "pages": pages, "completed": completed,
    }
    conn.execute("INSERT INTO callbacks(job_id,event,payload_json,status,created_at) VALUES(?,?,?,'queued',?)",
                 (job["job_id"], event, json.dumps(payload, separators=(",", ":")), now()))


def worker_loop() -> None:
    while not stop_event.is_set():
        try:
            process_next_chunk()
        except Exception:
            log.exception("Unexpected worker error")
        stop_event.wait(WORKER_POLL_SECONDS)


def process_next_chunk() -> None:
    with db() as conn:
        row = conn.execute("SELECT c.*,j.* FROM chunks c JOIN jobs j ON j.job_id=c.job_id WHERE c.status='queued' AND j.status IN ('queued','processing') ORDER BY j.created_at,c.start_page LIMIT 1").fetchone()
        if not row: return
        conn.execute("UPDATE chunks SET status='processing',attempts=attempts+1,updated_at=? WHERE chunk_id=?", (now(), row["chunk_id"]))
        conn.execute("UPDATE jobs SET status='processing',attempt_count=attempt_count+1,updated_at=? WHERE job_id=?", (now(), row["job_id"]))
        job_id, start_page, end_page = row["job_id"], row["start_page"], row["end_page"]
        source, mime, filename = Path(row["source_path"]), row["mime_type"], row["file_name"]
        attempts = row["attempts"] + 1
    try:
        results = []
        for number in range(start_page, end_page + 1):
            image = render_page(source, number, mime, filename)
            results.append(process_page(image, number))
        with db() as conn:
            for item in results:
                conn.execute("INSERT OR REPLACE INTO pages(job_id,page_number,result_json) VALUES(?,?,?)", (job_id, item["pageNumber"], json.dumps(item)))
            conn.execute("UPDATE chunks SET status='completed',last_error=NULL,updated_at=? WHERE chunk_id=?", (now(), row["chunk_id"]))
            count = conn.execute("SELECT COUNT(*) FROM pages WHERE job_id=?", (job_id,)).fetchone()[0]
            conn.execute("UPDATE jobs SET processed_pages=?,updated_at=? WHERE job_id=?", (count, now(), job_id))
            job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            remaining = conn.execute("SELECT COUNT(*) FROM chunks WHERE job_id=? AND status!='completed'", (job_id,)).fetchone()[0]
            done = remaining == 0
            if done:
                conn.execute("UPDATE jobs SET status='completed',updated_at=? WHERE job_id=?", (now(), job_id))
                job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            queue_callback(conn, job, "ocr.completed" if done else "ocr.progress", results, done)
        log.info("Job %s processed pages %s-%s", job_id, start_page, end_page)
        if done:
            # Source file is retained for configured retention; cleanup is not automatic before safe persistence.
            log.info("Job %s completed; source retained at %s", job_id, source)
    except Exception as exc:
        log.exception("Chunk failure for job %s pages %s-%s", job_id, start_page, end_page)
        with db() as conn:
            if attempts <= CHUNK_RETRIES:
                conn.execute("UPDATE chunks SET status='queued',last_error=?,updated_at=? WHERE chunk_id=?", (str(exc)[:1000], now(), row["chunk_id"]))
                conn.execute("UPDATE jobs SET status='queued',last_error=?,updated_at=? WHERE job_id=?", (str(exc)[:1000], now(), job_id))
            else:
                conn.execute("UPDATE chunks SET status='failed',last_error=?,updated_at=? WHERE chunk_id=?", (str(exc)[:1000], now(), row["chunk_id"]))
                conn.execute("UPDATE jobs SET status='failed',last_error=?,updated_at=? WHERE job_id=?", (str(exc)[:1000], now(), job_id))
                job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                payload = {"event":"ocr.failed","jobId":job_id,"proofId":job["proof_id"],"companyId":job["company_id"],"contractId":job["contract_id"],"status":"failed","totalPages":job["total_pages"],"processedPages":job["processed_pages"],"fileSha256":job["file_sha256"],"error":str(exc)[:1000],"completed":False}
                conn.execute("INSERT INTO callbacks(job_id,event,payload_json,status,created_at) VALUES(?,?,?,'queued',?)", (job_id,"ocr.failed",json.dumps(payload,separators=(",",":")),now()))


def callback_loop() -> None:
    while not stop_event.is_set():
        try: send_next_callback()
        except Exception: log.exception("Unexpected callback dispatcher error")
        stop_event.wait(CALLBACK_POLL_SECONDS)


def send_next_callback() -> None:
    with db() as conn:
        item = conn.execute("SELECT c.*,j.callback_url FROM callbacks c JOIN jobs j ON j.job_id=c.job_id WHERE c.status='queued' AND c.next_attempt<=? ORDER BY c.callback_id LIMIT 1", (time.time(),)).fetchone()
        if not item: return
        conn.execute("UPDATE callbacks SET status='sending',attempts=attempts+1 WHERE callback_id=?", (item["callback_id"],))
        attempts = item["attempts"] + 1
    body = item["payload_json"].encode("utf-8")
    secret = OCR_CALLBACK_SECRET or OCR_API_KEY
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        with httpx.Client(timeout=CALLBACK_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = client.post(item["callback_url"], content=body, headers={"Content-Type":"application/json", "X-OCR-Signature":f"sha256={signature}"})
            response.raise_for_status()
        with db() as conn: conn.execute("UPDATE callbacks SET status='sent',last_error=NULL WHERE callback_id=?", (item["callback_id"],))
        log.info("Callback %s delivered for job %s", item["callback_id"], item["job_id"])
    except Exception as exc:
        delay = min(3600, 5 * (2 ** min(attempts - 1, 10)))
        with db() as conn:
            conn.execute("UPDATE callbacks SET status='queued',next_attempt=?,last_error=? WHERE callback_id=?", (time.time()+delay, str(exc)[:1000], item["callback_id"]))
        log.warning("Callback delivery failed for job %s; retry in %ss (%s)", item["job_id"], delay, type(exc).__name__)


@app.get("/")
def root() -> dict[str, str]: return {"service": APP_NAME, "version": VERSION, "docs": "/docs"}


@app.get("/health")
def health() -> JSONResponse:
    ready = ocr_engine is not None and DB_PATH.exists() and worker_thread is not None and worker_thread.is_alive()
    return JSONResponse(status_code=200 if ready else 503, content={"status":"ok" if ready else "starting", "ocrReady":ocr_engine is not None, "workerRunning":bool(worker_thread and worker_thread.is_alive()), "databaseExists":DB_PATH.exists()})


@app.post("/jobs", status_code=202)
def submit_job(request: JobRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    require_api_key(x_api_key)
    file_url, callback_url = str(request.fileUrl), str(request.callbackUrl)
    allowed_url(file_url, FILE_URL_ALLOWED_HOSTS, "fileUrl")
    allowed_url(callback_url, CALLBACK_ALLOWED_HOSTS, "callbackUrl")
    job_id = str(uuid.uuid4())
    temp_path = SOURCE_DIR / f"{job_id}.download"
    size, digest = download_source(file_url, temp_path)
    try:
        total_pages = pdf_page_count(temp_path, request.mimeType, request.fileName)
        suffix = ".pdf" if request.mimeType.lower()=="application/pdf" or request.fileName.lower().endswith(".pdf") else ".image"
        final_path = SOURCE_DIR / f"{job_id}{suffix}"
        temp_path.replace(final_path)
        with db() as conn:
            existing = conn.execute("SELECT * FROM jobs WHERE company_id=? AND proof_id=? AND contract_id=? AND file_sha256=?", (request.companyId,request.proofId,request.contractId,digest)).fetchone()
            if existing:
                final_path.unlink(missing_ok=True)
                return {"accepted":True,"jobId":existing["job_id"],"status":existing["status"],"fileSha256":digest,"duplicate":True}
            stamp = now()
            conn.execute("INSERT INTO jobs(job_id,proof_id,company_id,contract_id,file_name,mime_type,source_path,file_sha256,callback_url,status,total_pages,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?)", (job_id,request.proofId,request.companyId,request.contractId,Path(request.fileName).name,request.mimeType,str(final_path),digest,callback_url,total_pages,stamp,stamp))
            for start in range(1,total_pages+1,PAGES_PER_CHUNK):
                end = min(total_pages,start+PAGES_PER_CHUNK-1)
                conn.execute("INSERT INTO chunks(job_id,start_page,end_page,status,updated_at) VALUES(?,?,?,'queued',?)", (job_id,start,end,stamp))
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    log.info("Accepted job %s (%s bytes, %s pages, sha256=%s)",job_id,size,digest,total_pages)
    return {"accepted":True,"jobId":job_id,"status":"queued","fileSha256":digest}


@app.get("/jobs/{job_id}")
def job_status(job_id: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    require_api_key(x_api_key)
    with db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?",(job_id,)).fetchone()
        if not job: raise HTTPException(404,"Job not found")
        return {"jobId":job["job_id"],"proofId":job["proof_id"],"companyId":job["company_id"],"contractId":job["contract_id"],"status":job["status"],"totalPages":job["total_pages"],"processedPages":job["processed_pages"],"attemptCount":job["attempt_count"],"lastError":job["last_error"],"fileSha256":job["file_sha256"],"createdAt":job["created_at"],"updatedAt":job["updated_at"]}


@app.get("/jobs/{job_id}/results")
def job_results(job_id: str, offset: int = 0, limit: int = 100, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    require_api_key(x_api_key)
    if offset < 0 or limit < 1 or limit > 500: raise HTTPException(400,"offset must be >=0 and limit must be 1-500")
    with db() as conn:
        job = conn.execute("SELECT job_id,status,total_pages,processed_pages FROM jobs WHERE job_id=?",(job_id,)).fetchone()
        if not job: raise HTTPException(404,"Job not found")
        rows = conn.execute("SELECT result_json FROM pages WHERE job_id=? ORDER BY page_number LIMIT ? OFFSET ?",(job_id,limit,offset)).fetchall()
        return {"jobId":job_id,"status":job["status"],"totalPages":job["total_pages"],"processedPages":job["processed_pages"],"offset":offset,"limit":limit,"pages":[json.loads(r["result_json"]) for r in rows]}
