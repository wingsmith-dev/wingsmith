# wingsconform as an image: the CLI, its committed profiles and video clips, and the two
# dependencies pinned by uv.lock. Built from this directory:
#
#   docker build -t wingsconform libs/conformance
#   docker run --rm -e WINGSMITH_API_KEY -v "$PWD/results:/work/.local/conformance" \
#       wingsconform run example
#
# Runs are written under /work/.local/conformance, so mount that to keep them. A
# profile outside the package is given by path; mount it too.
#
# Base pinned by digest, same Python as libs/plane and the vllm-bench image.
FROM python:3.15.1-slim-bookworm@sha256:575f8bc12ec26542e0678bc6b60e385218248019d433157ec327264a78c3a853

RUN pip install --no-cache-dir uv==0.9.18

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY wingsconform ./wingsconform
RUN uv sync --frozen --no-dev --no-editable

ENV PATH=/app/.venv/bin:$PATH
WORKDIR /work
RUN chown 65532:65532 /work
USER 65532:65532
ENTRYPOINT ["wingsconform"]
