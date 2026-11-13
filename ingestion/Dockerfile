
FROM rust:1.95-slim-bookworm AS build

ARG APP_NAME=ingestion_api

RUN apt-get update \
    && apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY --from=proto . /proto
COPY Cargo.lock Cargo.toml build.rs ./
RUN mkdir src \
    && echo "// dummy file" > src/lib.rs \
    && cargo build --release

COPY src src
RUN touch src/*.rs && cargo build --locked --release
RUN cp ./target/release/$APP_NAME /bin/ingestion_server

FROM debian:bookworm-slim AS final

RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "10001" \
    ingestion

RUN mkdir -p /mnt/data/raw \
    && chown -R 10001:10001 /mnt/data

USER ingestion

COPY --from=build /bin/ingestion_server /bin/

EXPOSE 50051

CMD ["/bin/ingestion_server"]