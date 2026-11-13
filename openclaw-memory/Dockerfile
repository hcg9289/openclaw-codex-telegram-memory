# syntax=docker/dockerfile:1
ARG PG_MAJOR=16
FROM postgres:16.15-bookworm@sha256:2b429c90204d3dc581371d2ce267ffb0aeeddf4968e176de5003037b4d0de8a9 AS builder

ARG PG_MAJOR
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential postgresql-server-dev-${PG_MAJOR} \
    && rm -rf /var/lib/apt/lists/*

ADD https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.2.tar.gz /tmp/pgvector-v0.8.2.tar.gz
RUN echo "69f4019389af05dc1c9548deb8628e62878e6e207c03907f2b8af2016472cdaa  /tmp/pgvector-v0.8.2.tar.gz" | sha256sum -c - \
    && mkdir /tmp/pgvector \
    && tar -xzf /tmp/pgvector-v0.8.2.tar.gz --strip-components=1 -C /tmp/pgvector \
    && rm /tmp/pgvector-v0.8.2.tar.gz
WORKDIR /tmp/pgvector
RUN make clean && make OPTFLAGS="" && make install

FROM postgres:16.15-bookworm@sha256:2b429c90204d3dc581371d2ce267ffb0aeeddf4968e176de5003037b4d0de8a9
COPY --from=builder /usr/lib/postgresql/16/lib/vector.so /usr/lib/postgresql/16/lib/vector.so
COPY --from=builder /usr/share/postgresql/16/extension/vector* /usr/share/postgresql/16/extension/
