# Base pinned by digest and every package pinned in requirements.txt, so a rebuild of
# one tag produces the same client. Same Python as libs/plane.
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

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
