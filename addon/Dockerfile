ARG BUILD_FROM
FROM ${BUILD_FROM}

# An add-on's build context is its own directory, so the package cannot be
# COPYed from the repo root — it is installed from git instead. Override
# BRIDGE_REF to build a branch or tag other than main.
ARG BRIDGE_REF=main

RUN apk add --no-cache python3 py3-pip git

RUN pip3 install --no-cache-dir --break-system-packages \
        "tuya-local-bridge[web,qr,lan] @ git+https://github.com/Vortitron/tuya-local-bridge@${BRIDGE_REF}" \
        websocket-client

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
