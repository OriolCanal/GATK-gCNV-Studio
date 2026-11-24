from app import create_app
from app.worker import init_app as init_worker

app = create_app()
init_worker(app)
