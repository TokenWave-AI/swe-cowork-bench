#!/bin/bash
# keep the harbor front alive; if it can't serve, fall back to real harbor
while true; do
  if ! curl -s -m 10 --cacert /etc/harborfront/crt.pem --resolve harbor.tokenwave.us:443:127.0.0.1 -o /dev/null https://harbor.tokenwave.us/v2/; then
    pkill -f "python3 -u /usr/local/bin/harborfront.py"; sleep 1
    setsid nohup python3 -u /usr/local/bin/harborfront.py 38.55.103.74 >> /var/log/harborfront.log 2>&1 < /dev/null &
    sleep 5
    curl -s -m 10 --cacert /etc/harborfront/crt.pem --resolve harbor.tokenwave.us:443:127.0.0.1 -o /dev/null https://harbor.tokenwave.us/v2/ \
      || sed -i '/127.0.0.1 harbor.tokenwave.us/d' /etc/hosts
    echo "$(date -u +%FT%TZ) restarted front" >> /var/log/hfwatch.log
  fi
  sleep 30
done
