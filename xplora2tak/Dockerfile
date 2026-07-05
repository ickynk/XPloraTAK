ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir \
        requests==2.32.3 \
        paho-mqtt==2.1.0 \
        cryptography==44.0.2

COPY app /app
COPY run.sh /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
