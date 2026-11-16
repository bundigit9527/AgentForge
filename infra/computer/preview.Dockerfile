# Extend your existing, operator-approved desktop image; pin its immutable digest in delivery.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
ENV LD_PRELOAD=""
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libreoffice-impress libreoffice-writer libreoffice-calc fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
USER 1000:1000
