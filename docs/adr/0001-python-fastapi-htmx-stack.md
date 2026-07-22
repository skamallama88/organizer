# Python FastAPI + HTMX for the web stack

We're using Python's FastAPI for the backend API, HTMX + Alpine.js for the frontend, and the `watchdog` library for filesystem events. The CLI is built with Click/Typer.

Python wins over Go or Node because the unarchive action needs to handle zip, 7z, and rar natively — Python libraries (`zipfile`, `py7zr`, `rarfile`) are mature and zero-friction. FastAPI gives async endpoints for file operations. HTMX keeps the UI simple and server-rendered, avoiding a heavy SPA framework. The whole thing packages trivially as a single Docker image.

The CLI shares the same Python codebase via Click, so `organizer check` and `organizer run` call the same rule engine as the web UI.
