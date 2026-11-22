# PostgreSQL 18 with pg_textsearch, which ranks czn-data's search tools by BM25.
#
# Two targets, built from the same PGDG package so every database runs the same extension:
# - server: a Postgres image for the local database, tests, and CI.
# - extension: only the extension's files, in the layout CloudNativePG mounts from an image
#   volume (spec.postgresql.extensions). Its library is built for Debian 13 (trixie), so the
#   cluster's Postgres image must be a trixie one as well.
# Both need pg_textsearch in shared_preload_libraries; server sets it below, and the cluster
# sets it in its postgresql parameters.
FROM postgres:18 AS server
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-18-pg-textsearch \
    && rm -rf /var/lib/apt/lists/*
CMD ["postgres", "-c", "shared_preload_libraries=pg_textsearch"]

FROM scratch AS extension
COPY --from=server /usr/lib/postgresql/18/lib/pg_textsearch.so /lib/
COPY --from=server /usr/share/postgresql/18/extension/pg_textsearch* /share/extension/
