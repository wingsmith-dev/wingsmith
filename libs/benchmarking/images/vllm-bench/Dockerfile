# Base pinned by digest and every package pinned in requirements.txt, so a rebuild of
# one tag produces the same client. Same Python as libs/plane.
FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /bench
COPY stubs /bench
# upstream/ carries vLLM's Apache-2.0 LICENSE, which lands at /bench/LICENSE.
COPY upstream /bench
COPY main.py /bench/main.py
COPY sweep.py /bench/sweep.py
COPY fakehealth.py /bench/fakehealth.py

ENV HF_HOME=/hf
ENTRYPOINT ["python", "/bench/main.py"]
