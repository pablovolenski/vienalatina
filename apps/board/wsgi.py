"""Entry point for gunicorn: `gunicorn apps.board.wsgi:application`."""

from .app import create_app

application = create_app()
