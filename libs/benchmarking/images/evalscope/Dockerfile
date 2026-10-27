FROM python:3.12.16-slim@sha256:f3aa44c1f89302501f8b812a3abe4ca997177f227e295d9a76ac0d683b493800

RUN pip install --no-cache-dir \
      "evalscope==1.11.1" \
      # The operator's generic wrapper reads the harness's stdout with awk, and evalscope's
      # report dumper uses numpy's toJSON; both are present transitively, but pinning them
      # here is cheaper than discovering the next base-image prune.
      numpy

WORKDIR /bench

# The operator runs `sh -c <script>` against this image; no project entrypoint exists.
# The command line comes from the plane in `parameters["command"]`, so no flags are baked in.
ENTRYPOINT ["sh"]
