# Runtime image for the members area.
#
# Thin, like docker/translate/Dockerfile: no build tooling, no compiler, and
# nothing in it that is not needed to answer a request.
#
# Build on the server, from the repository root so the app is in context:
#   docker build -t vienalatina/board:1 -f docker/board/Dockerfile .

FROM python:3.12-slim

# Runs as a non-root user with the uid the host data directory is chowned to.
# Two reasons, and the second is the one that bites: root in the container
# writes root-owned files into the mounted volume, and then backups and
# manual inspection from the host need sudo for no good reason.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin board

WORKDIR /srv

COPY apps/board/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY apps /srv/apps

ENV BOARD_DB=/data/board.db \
    PYTHONUNBUFFERED=1

USER board
EXPOSE 8080

# Two workers is plenty for a group this size and keeps the footprint near
# 60-80MB, which is what the CX22 can spare alongside Gitea, Woodpecker and a
# translation run. --timeout is short because every request here is a SQLite
# read or write; anything slower than this is stuck, not busy.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "2", \
     "--timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "apps.board.wsgi:application"]
