FROM ubuntu:22.04

COPY ./dist/tahoe /bin/tahoe

CMD ["tahoe","--version"]