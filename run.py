"""Dev entrypoint: python run.py  (production uses gunicorn -c gunicorn.conf.py run:app)."""
from app import create_app

app = create_app()

if __name__ == "__main__":
    # The flag comes from the selected config, so running this file against
    # ProdConfig can never switch the Werkzeug debugger on.
    app.run(debug=app.config.get("DEBUG", False))
