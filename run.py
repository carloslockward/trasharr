"""Entry point for trasharr.

Used by gunicorn in the Docker image as ``run:app`` and directly for local
development (``python run.py``).
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)