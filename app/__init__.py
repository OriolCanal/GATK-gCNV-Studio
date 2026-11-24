from pathlib import Path

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object('app.config.Config')
    if test_config:
        app.config.update(test_config)

    @app.context_processor
    def inject_asset_version():
        def asset_version(filename):
            try:
                return int((Path(app.static_folder) / filename).stat().st_mtime)
            except OSError:
                return '1'
        return {'asset_version': asset_version}

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    db.init_app(app)

    from app import models  # noqa: F401
    from app.routes import bp

    app.register_blueprint(bp)
    with app.app_context():
        db.create_all()

    return app
