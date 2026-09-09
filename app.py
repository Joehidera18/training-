"""Run locally with Python or serve this WSGI application with Gunicorn."""
import atexit
import os
from pathlib import Path
from lab.service import Service, wsgi_application

BASE_DIR = Path(__file__).resolve().parent


def create_app(db_path=None, data_dir=None):
    service = Service(BASE_DIR,
        db_path or os.getenv("RESEARCH_DB_PATH", str(BASE_DIR / "research.sqlite3")),
        data_dir or os.getenv("RESEARCH_DATA_DIR", str(BASE_DIR / "data")),
        token=os.getenv("APP_ACCESS_TOKEN"))
    application = wsgi_application(service)
    application.service = service
    return application


app = create_app()
agent = app.service.agent
atexit.register(agent.stop)
atexit.register(app.service.research.cancel)
atexit.register(app.service.coinbase.stop)

if __name__ == "__main__":
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, make_server
    import webbrowser
    import threading

    class ThreadedServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "5000"))
    with make_server(host, port, app, server_class=ThreadedServer) as server:
        print(f"CryptO V10 — research, paper trading and Coinbase: http://{host}:{port}", flush=True)
        if os.getenv("OPEN_BROWSER") == "1":
            threading.Timer(.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        server.serve_forever()
