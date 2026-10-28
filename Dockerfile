FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/hf_cache

# CPU-only PyTorch first: the default Linux wheel bundles ~2 GB of GPU libraries this project cannot use.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Dependencies next. This layer is cached, so it is rebuilt only when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The code. In docker-compose.yml the project folder is mounted over /app, so edits apply without a rebuild.
COPY . .
