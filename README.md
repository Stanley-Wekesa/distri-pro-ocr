# Distri-Pro OCR API

A separate CPU-based PaddleOCR HTTP service for extracting printed text from delivery-note proof images.

## Current scope

- JPG, PNG and WEBP image uploads
- PaddleOCR 3.x printed-text recognition
- API-key protected `POST /ocr`
- Public `GET /health` health check
- Candidate serial extraction using a configurable regular expression
- Returns raw OCR text and flags ambiguous serials for human review

The starter intentionally returns `school`, `county` and `subCounty` as `null` until we agree on their exact printed labels/layout and test against real delivery notes. It never approves a delivery.

## Run locally with Docker

1. Copy `.env.example` to `.env` and set a long random `OCR_API_KEY`.
2. Build:

   ```bash
   docker build -t distri-pro-ocr .
   ```

3. Run:

   ```bash
   docker run --rm -p 8080:8080 --env-file .env distri-pro-ocr
   ```

4. Check health:

   ```bash
   curl http://localhost:8080/health
   ```

5. Open API documentation at `http://localhost:8080/docs`.

## Test an image

```bash
curl -X POST "http://localhost:8080/ocr" \
  -H "X-API-Key: YOUR_SECRET_KEY" \
  -F "file=@sample-delivery-note.jpg"
```

## Deploy on Render

1. Push this folder to a **private GitHub repository** named `distri-pro-ocr`.
2. In Render, create a **New Web Service** and connect that repository.
3. Choose **Docker** as the runtime.
4. Add environment variables:
   - `OCR_API_KEY`: a long random secret
   - `MAX_UPLOAD_BYTES`: `12000000`
   - `SERIAL_REGEX`: the agreed serial-number pattern
5. Deploy and wait for the build to finish.
6. Test `https://YOUR-RENDER-SERVICE.onrender.com/health`.
7. Keep the service URL and API key private. Do not put the key in browser JavaScript or commit it to GitHub.

Render free instances may sleep and may not have enough memory for PaddleOCR. Treat the free tier as a trial only; if the build fails, the service is killed for memory, or cold starts are too slow, move to an always-on instance with adequate RAM. Do not connect production uploads until the service passes the image test pilot.

## API response

```json
{
  "success": true,
  "serialNumber": "candidate-from-image",
  "school": null,
  "county": null,
  "subCounty": null,
  "ocrText": ["recognized line 1", "recognized line 2"],
  "averageTextConfidence": 0.96,
  "reviewRequired": false,
  "reviewReason": null,
  "note": "OCR output is a suggestion only. It does not approve or complete a delivery."
}
```

`averageTextConfidence` is the average confidence across recognized text lines, not a calibrated confidence that the serial number is correct. The serial is deliberately not assigned a fabricated confidence score.

## Security and production notes

- Only the backend should call this service.
- Keep port 8080 internal to the container; expose the service through the hosting platform's HTTPS URL.
- Use a strong API key and rotate it if exposed.
- Keep the original delivery-note image in Distri-Pro storage; this service does not persist uploads.
- Add stricter rate limiting and request logging policy before production if needed.
- Restrict access to the service where the hosting platform supports private networking or IP controls.
- OCR matching and admin approval must remain separate business steps.

## Next configuration required

We need a few genuine delivery-note samples and the exact serial-number pattern to tune `SERIAL_REGEX` and add reliable school/county/sub-county extraction. Do not assume a serial match is correct just because a regular expression found one.
