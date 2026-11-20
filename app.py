"""ZarailensAI web app entry point.

Vercel runs this file with its Python runtime: it finds the FastAPI ``app`` below and
serves the whole site (web app + API) from it. Locally:

    python app.py                 # http://localhost:8000
    uvicorn app:app --reload      # with auto-reload while editing
"""
from server.main import app

__all__=["app"]

if __name__=="__main__":
    import os, webbrowser, threading
    import uvicorn
    port=int(os.getenv("PORT","8000"))
    if not os.getenv("NO_BROWSER"):
        threading.Timer(1.5,lambda:webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run("app:app",host=os.getenv("HOST","127.0.0.1"),port=port)
