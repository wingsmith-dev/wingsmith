FROM python:3.12-slim

RUN pip install --no-cache-dir \
      aiohttp==3.14.1 \
      numpy \
      tqdm \
      "transformers>=4.57" \
      regex \
      pybase64 \
      pillow \
      huggingface_hub \
      datasets \
      pandas \
      jinja2 \
      requests \
      typing_extensions \
      matplotlib \
      seaborn

WORKDIR /bench
COPY stubs /bench
COPY upstream /bench
COPY main.py /bench/main.py
COPY sweep.py /bench/sweep.py
COPY fakehealth.py /bench/fakehealth.py

ENV HF_HOME=/hf
ENTRYPOINT ["python", "/bench/main.py"]
