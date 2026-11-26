FROM python:3.15.1-slim@sha256:533b0b2f9b5d46fd097091cf826f27902f1d6c3bca714b6dc74551356aa97a51

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
