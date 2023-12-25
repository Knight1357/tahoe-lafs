# Docker
```shell
apt update
apt -yq install build-essential python3-dev libffi-dev libssl-dev python3-virtualenv git

git clone https://github.com/Knight1357/tahoe-lafs.git

cd /root/tahoe-lafs; \
virtualenv venv; \
./venv/bin/pip install --upgrade setuptools; \
./venv/bin/pip install --editable .; \
./venv/bin/tahoe --version;
```

### Docker run

```shell
docker run -dit --restart=always --network=host --name tahoe-lafs knight13579/tahoe-lafs:1.1
```

### Docker run backend
```shell
docker exec -it tahoe-lafs /bin/bash
```

### create and run introducer
```shell
/root/tahoe-lafs/venv/bin/tahoe create-introducer --location=tcp:127.0.0.1:3458 --port=tcp:3458 /root/.tahoe-introducer
/root/tahoe-lafs/venv/bin/tahoe run /root/.tahoe-introducer &
```

### create and run node
```shell
/root/tahoe-lafs/venv/bin/tahoe create-node --location=tcp:127.0.0.1:3457 --port=tcp:3457 --introducer=$(cat /root/.tahoe-introducer/private/introducer.furl) /root/.tahoe-server
/root/tahoe-lafs/venv/bin/tahoe run /root/.tahoe-server &
```

### create client
```shell
tahoe create-client --webport=3456 --introducer=$(cat /root/.tahoe-introducer/private/introducer.furl) --basedir=/root/.tahoe-client --shares-needed=1 --shares-happy=1 --shares-total=1
tahoe run /root/.tahoe-client &
```


---

### create and run introducer
```shell
tahoe create-introducer --location=tcp:127.0.0.1:3458 --port=tcp:3458 .tahoe-introducer
tahoe run .tahoe-introducer &
```

### create and run node
```shell
tahoe create-node --location=tcp:127.0.0.1:3457 --port=tcp:3457 --introducer=$(cat .tahoe-introducer/private/introducer.furl) .tahoe-server
tahoe run .tahoe-server &
```

### create client
```shell
tahoe create-client --webport=3456 --introducer=$(cat .tahoe-introducer/private/introducer.furl) --basedir=.tahoe-client --shares-needed=1 --shares-happy=1 --shares-total=1
tahoe run .tahoe-client &
```

