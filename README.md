# Universal Video Downloader API

FastAPI + yt-dlp + FFmpeg backend designed for a Blogger frontend.

## API

### GET /api/info
Example:
`/api/info?url=https://example.com/video`

Returns title, thumbnail, duration, platform and selectable formats.

### POST /api/info
```json
{"url":"https://example.com/video"}
```

### GET /api/download
Example:
`/api/download?url=https%3A%2F%2Fexample.com%2Fvideo&format_id=137`

Special format:
`format_id=mp3`

### POST /api/download
```json
{
  "url": "https://example.com/video",
  "format_id": "137"
}
```

## Local setup

1. Install Python 3.12.
2. Install FFmpeg.
3. Create a virtual environment.
4. `pip install -r requirements.txt`
5. Copy `.env.example` to `.env`.
6. Start:
   `uvicorn main:app --reload --port 8000`

Open:
`http://127.0.0.1:8000/docs`

## Docker

```bash
docker build -t universal-video-downloader-api .
docker run -p 8000:8000 \
  -e ALLOWED_ORIGINS=https://yourblog.blogspot.com \
  universal-video-downloader-api
```

## Notes

- High-resolution formats can be video-only. The `/api/download` endpoint asks yt-dlp to pair the selected video with audio and uses FFmpeg to merge them.
- Raw extractor URLs may expire or require service-specific headers. Treat them as diagnostic data; use the backend download endpoint for the actual file.
- You are responsible for complying with each platform's terms, copyright law, and applicable privacy rules.
