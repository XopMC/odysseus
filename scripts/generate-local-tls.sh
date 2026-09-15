#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 OUTPUT_DIR HOST_OR_IP [HOST_OR_IP ...]" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
out_dir=$1
shift

command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required" >&2
  exit 1
}

mkdir -p "$out_dir"
chmod 700 "$out_dir"

config_file=$(mktemp)
trap 'rm -f "$config_file"' EXIT

{
  cat <<'EOF'
[req]
prompt = no
distinguished_name = subject
req_extensions = extensions

[subject]
CN = Odysseus

[extensions]
subjectAltName = @names
extendedKeyUsage = serverAuth

[names]
EOF
  ip_index=1
  dns_index=1
  for name in "$@"; do
    if [[ $name =~ ^[0-9a-fA-F:.]+$ ]]; then
      printf 'IP.%d = %s\n' "$ip_index" "$name"
      ip_index=$((ip_index + 1))
    else
      printf 'DNS.%d = %s\n' "$dns_index" "$name"
      dns_index=$((dns_index + 1))
    fi
  done
} >"$config_file"

if [[ ! -s "$out_dir/rootCA-key.pem" || ! -s "$out_dir/rootCA.pem" ]]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
    -out "$out_dir/rootCA-key.pem"
  openssl req -x509 -new -sha256 -days 3650 \
    -key "$out_dir/rootCA-key.pem" \
    -subj "/CN=Odysseus Local CA" \
    -out "$out_dir/rootCA.pem"
fi

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out "$out_dir/key.pem"
openssl req -new -key "$out_dir/key.pem" -config "$config_file" \
  -out "$out_dir/request.csr"
openssl x509 -req -sha256 -days 397 \
  -in "$out_dir/request.csr" \
  -CA "$out_dir/rootCA.pem" \
  -CAkey "$out_dir/rootCA-key.pem" \
  -CAcreateserial \
  -extfile "$config_file" \
  -extensions extensions \
  -out "$out_dir/cert.pem"

rm -f "$out_dir/request.csr" "$out_dir/rootCA.srl"
chmod 600 "$out_dir/key.pem" "$out_dir/rootCA-key.pem"
chmod 644 "$out_dir/cert.pem" "$out_dir/rootCA.pem"

echo "TLS certificate: $out_dir/cert.pem"
echo "Install and trust this CA on clients: $out_dir/rootCA.pem"
