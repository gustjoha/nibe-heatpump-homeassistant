ARG BUILD_FROM
FROM $BUILD_FROM

RUN pip3 install --no-cache-dir \
    aiohttp \
    python-dateutil

COPY rootfs /
RUN chmod +x /usr/bin/run.sh

EXPOSE 8099

CMD ["/usr/bin/run.sh"]
