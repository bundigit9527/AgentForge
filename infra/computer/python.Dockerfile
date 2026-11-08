ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
USER root
ENV PATH=/usr/local/bin:/usr/bin:/bin
# Runtime dependencies belong in the image, not in user-controlled install commands.
COPY python-requirements.txt /tmp/python-requirements.txt
RUN pip install --no-cache-dir -r /tmp/python-requirements.txt && rm /tmp/python-requirements.txt
USER 65534:65534
WORKDIR /workspace
CMD ["python3"]
