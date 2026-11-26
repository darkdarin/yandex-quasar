# Quasar Adapter app image. The build context is the folder made by ha-addon/build.sh (app files + adapter source):
# CI builds it for every release and publishes ghcr.io/darkdarin/quasar-adapter (the app's "image"); a local app
# without "image:" is built by the Supervisor from the same folder.
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.14-alpine3.22
FROM ${BUILD_FROM}

# quasar-protos (Yandex-copyrighted, not vendored in the repo) are compiled into the image at build time.
ARG QUASAR_PROTOS_COMMIT=1044fbdfcd0c2d6d8a26681e31186d13fbb5aef6

# Dependencies come from uv.lock and PyPI only: the base image adds HA's own wheel index (UV_EXTRA_INDEX_URL),
# which lacks some of the locked versions.
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
COPY adapter /tmp/src

RUN apk add --no-cache ffmpeg openssl tzdata \
    && apk add --no-cache --virtual .build-deps git protobuf-dev \
    && cd /tmp/src \
    && uv export --frozen --no-dev --no-emit-project --no-header -o /tmp/requirements.txt \
    && env -u UV_EXTRA_INDEX_URL uv pip install --system --break-system-packages --no-cache -r /tmp/requirements.txt \
    && env -u UV_EXTRA_INDEX_URL uv pip install --system --break-system-packages --no-cache --no-deps /tmp/src \
    && git clone --quiet https://github.com/v3rt3p/quasar-protos.git /tmp/protos \
    && git -C /tmp/protos checkout --quiet ${QUASAR_PROTOS_COMMIT} \
    && cd / && OUT=$(python3 -c "import quasar_adapter, pathlib; print(pathlib.Path(quasar_adapter.__file__).parent / '_proto')") \
    && mkdir -p "$OUT" \
    && cd /tmp/protos \
    && protoc -I. --python_out="$OUT" $(find . -name '*.proto' -not -path './google/*' | sed 's#^\./##') 2>/dev/null \
    && cd / && python3 -c "from quasar_adapter import aapi, provision; print('adapter ok')" \
    && rm -rf /tmp/protos /tmp/src /tmp/requirements.txt /usr/local/bin/uv \
    && apk del .build-deps

COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
