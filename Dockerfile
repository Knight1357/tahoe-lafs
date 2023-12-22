FROM ubuntu:22.04

RUN apt update
RUN apt -yq install build-essential python3-dev libffi-dev libssl-dev python3-virtualenv git

COPY . /root/tahoe-lafs

RUN \
  cd /root/tahoe-lafs; \
  virtualenv venv; \
  ./venv/bin/pip install --upgrade setuptools; \
  ./venv/bin/pip install --editable .; \
  ./venv/bin/tahoe --version;
RUN \
  cd /root; \
  mkdir /root/.tahoe-client; \
  mkdir /root/.tahoe-introducer; \
  mkdir /root/.tahoe-server;
RUN /root/tahoe-lafs/venv/bin/tahoe create-introducer --location=tcp:introducer:3458 --port=tcp:3458 /root/.tahoe-introducer
RUN /root/tahoe-lafs/venv/bin/tahoe run /root/.tahoe-introducer
RUN /root/tahoe-lafs/venv/bin/tahoe create-node --location=tcp:server:3457 --port=tcp:3457 --introducer=$(cat /root/.tahoe-introducer/private/introducer.furl) /root/.tahoe-server
RUN /root/tahoe-lafs/venv/bin/tahoe create-client --webport=3456 --introducer=$(cat /root/.tahoe-introducer/private/introducer.furl) --basedir=/root/.tahoe-client --shares-needed=1 --shares-happy=1 --shares-total=1
VOLUME ["/root/.tahoe-client", "/root/.tahoe-server", "/root/.tahoe-introducer"]
EXPOSE 3456 3457 3458
ENTRYPOINT ["/root/tahoe-lafs/venv/bin/tahoe"]
CMD []