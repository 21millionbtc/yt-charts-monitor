# Always-on 24/7 monitor: polls every 30 seconds, forever.
#
# The image is deliberately tiny - monitor.py uses only the Python standard
# library, so there is nothing to install and no dependency that can break.

FROM python:3.12-slim

WORKDIR /app
COPY monitor.py .

# -1 means "run forever" (see main() in monitor.py).
ENV SPRINT_DURATION_SECONDS=-1
ENV POLL_INTERVAL_SECONDS=120

# DISCORD_WEBHOOK_URL is supplied at runtime as a secret - never baked in.
CMD ["python3", "-u", "monitor.py"]
