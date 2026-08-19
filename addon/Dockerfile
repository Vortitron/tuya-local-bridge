ARG BUILD_FROM
FROM ${BUILD_FROM}

# An add-on's build context is its own directory, so the package cannot be
# COPYed from the repo root — it is installed from git instead.
#
# Pinned to a commit rather than a branch, for two reasons. Docker caches
# this layer on the text of the RUN line, so "@main" produced a cache hit and
# reinstalled the *old* package on every rebuild -- the add-on version went up,
# the code inside did not, and the same bug came back looking unfixed. A commit
# also makes a given add-on version reproducible.
#
# Bump this with the add-on version when releasing.
ARG BRIDGE_REF=8706cbc

# Supervisor passes BUILD_VERSION; naming it here means a version bump alone is
# enough to invalidate the layer even if BRIDGE_REF is ever left alone.
ARG BUILD_VERSION

RUN apk add --no-cache python3 py3-pip git

RUN echo "add-on ${BUILD_VERSION} from ${BRIDGE_REF}" > /build-info \
    && pip3 install --no-cache-dir --break-system-packages \
        "tuya-local-bridge[web,qr,lan] @ git+https://github.com/Vortitron/tuya-local-bridge@${BRIDGE_REF}" \
        websocket-client

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
