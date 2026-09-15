#!/usr/bin/env bash
set -euo pipefail

port=${1:-5130}
wan_dev=${2:-eno1}
wan_gateway=${3:-}

[[ $EUID -eq 0 ]] || {
  echo "run as root: sudo $0 [port] [wan-interface] [wan-gateway]" >&2
  exit 1
}
[[ $port =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)) || {
  echo "invalid TCP port: $port" >&2
  exit 2
}
ip link show "$wan_dev" >/dev/null 2>&1 || {
  echo "unknown WAN interface: $wan_dev" >&2
  exit 2
}

if [[ -z $wan_gateway ]]; then
  wan_gateway=$(ip route show default dev "$wan_dev" | awk '/ via / {print $3; exit}')
fi
[[ -n $wan_gateway ]] || {
  echo "could not determine the gateway for $wan_dev" >&2
  exit 2
}

install -d -m 0755 /etc/odysseus
cat >/etc/odysseus/public-port.env <<EOF
PORT=$port
WAN_DEV=$wan_dev
WAN_GATEWAY=$wan_gateway
TABLE_ID=201
RULE_PRIORITY=151
PACKET_MARK=0x51
EOF
chmod 0644 /etc/odysseus/public-port.env

cat >/usr/local/sbin/odysseus-public-port-route <<'EOF'
#!/bin/sh
set -eu
. /etc/odysseus/public-port.env

rule_is_effective() {
  ip route get 1.1.1.1 mark "$PACKET_MARK" 2>/dev/null | grep -q "dev $WAN_DEV"
}

case "${1:-}" in
  up)
    ip route replace table "$TABLE_ID" default via "$WAN_GATEWAY" dev "$WAN_DEV"
    # Some Jetson kernels cannot dump/delete a rule containing port selectors.
    # Treat a duplicate add as success only when a marked lookup proves that
    # the expected WAN route is already effective.
    ip rule add priority "$RULE_PRIORITY" fwmark "$PACKET_MARK" lookup "$TABLE_ID" 2>/dev/null || \
      rule_is_effective
    iptables -t mangle -C OUTPUT -p tcp --sport "$PORT" -j MARK --set-mark "$PACKET_MARK" 2>/dev/null || \
      iptables -t mangle -A OUTPUT -p tcp --sport "$PORT" -j MARK --set-mark "$PACKET_MARK"
    ;;
  down)
    iptables -t mangle -D OUTPUT -p tcp --sport "$PORT" -j MARK --set-mark "$PACKET_MARK" 2>/dev/null || true
    ip rule del priority "$RULE_PRIORITY" fwmark "$PACKET_MARK" lookup "$TABLE_ID" 2>/dev/null || true
    ip route flush table "$TABLE_ID" 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 {up|down}" >&2
    exit 2
    ;;
esac
EOF
chmod 0755 /usr/local/sbin/odysseus-public-port-route

cat >/etc/systemd/system/odysseus-public-port-route.service <<'EOF'
[Unit]
Description=Route replies from the public Odysseus port through the physical WAN
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/odysseus-public-port-route up
ExecStop=/usr/local/sbin/odysseus-public-port-route down

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now odysseus-public-port-route.service
echo "TCP source port $port is now routed through $wan_gateway on $wan_dev"
