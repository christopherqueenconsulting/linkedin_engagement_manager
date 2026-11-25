#!/usr/bin/env bash
# Generate a LOCAL C2PA signing chain: a throwaway root CA and an ES256 end-entity certificate that
# meets the C2PA signer-certificate profile (leaf, not a CA, digitalSignature key usage, an EKU).
# A bare `openssl req -x509` cert lacks that profile and a signer may refuse it (#2794).
#
#   scripts/c2pa_make_cert.sh <out_dir> [--owner-uid UID]
#
# Writes <out_dir>/chain.pem (leaf, then root) and <out_dir>/signer.key (mode 0600). The root key
# is discarded: nothing needs it after the leaf is issued. Refuses to overwrite either file, never
# prints key material, and writes only inside <out_dir>. openssl only.
#
# The signing container runs as a different uid than the host user that owns the key, so a 0600 key
# the container cannot open passes the exists-check and then fails per asset. --owner-uid chowns
# both files (needs sudo); without it the exact `sudo chown` line is printed instead.
# Owner runbook: docs/content-quality-audits/video.md ("C2PA signing — owner runbook").
set -euo pipefail

usage() {
  echo "usage: $0 <out_dir> [--owner-uid UID]" >&2
  exit 2
}

[ $# -ge 1 ] || usage
out_dir="$1"
shift
owner_uid=""
while [ $# -gt 0 ]; do
  case "$1" in
    --owner-uid)
      [ $# -ge 2 ] || usage
      owner_uid="$2"
      shift 2
      ;;
    *) usage ;;
  esac
done
if [ -n "$owner_uid" ] && ! [[ "$owner_uid" =~ ^[0-9]+$ ]]; then
  echo "error: --owner-uid must be a number, got '$owner_uid'" >&2
  exit 2
fi
command -v openssl >/dev/null 2>&1 || { echo "error: openssl not found" >&2; exit 1; }

chain="$out_dir/chain.pem"
key="$out_dir/signer.key"
for f in "$chain" "$key"; do
  if [ -e "$f" ] || [ -L "$f" ]; then
    echo "error: $f already exists; refusing to overwrite (move it aside first)" >&2
    exit 1
  fi
done

umask 077
mkdir -p "$out_dir"
work="$(mktemp -d "$out_dir/.c2pa-work.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# Root CA: a CA, may sign certificates. Its key lives only in $work.
cat >"$work/root.cnf" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = v3_root
prompt = no
[dn]
O = LEM Local C2PA
CN = LEM Local C2PA Root CA
[v3_root]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF

# End-entity signer: NOT a CA, digitalSignature only, emailProtection EKU (one the C2PA profile allows).
cat >"$work/leaf.cnf" <<'EOF'
[req]
distinguished_name = dn
prompt = no
[dn]
O = LEM Local C2PA
CN = LEM Content Credentials Signer
[v3_leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = emailProtection
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
EOF

openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$work/root.key" 2>/dev/null
openssl req -new -x509 -key "$work/root.key" -sha256 -days 3650 \
  -config "$work/root.cnf" -out "$work/root.pem" 2>/dev/null

openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$work/signer.key" 2>/dev/null
openssl req -new -key "$work/signer.key" -config "$work/leaf.cnf" -out "$work/leaf.csr" 2>/dev/null
openssl x509 -req -in "$work/leaf.csr" -CA "$work/root.pem" -CAkey "$work/root.key" \
  -CAcreateserial -days 365 -sha256 -extfile "$work/leaf.cnf" -extensions v3_leaf \
  -out "$work/leaf.pem" 2>/dev/null

# Leaf first, then root. Key is 0600 from the umask; the chain is public material.
cat "$work/leaf.pem" "$work/root.pem" >"$chain"
cp "$work/signer.key" "$key"
chmod 0600 "$key"
chmod 0644 "$chain"

echo "wrote $chain (leaf, then root)"
echo "wrote $key (mode 0600)"

if [ -n "$owner_uid" ]; then
  if [ "$(id -u)" = "0" ]; then
    chown "$owner_uid" "$chain" "$key"
  else
    sudo chown "$owner_uid" "$chain" "$key"
  fi
  echo "chowned both files to uid $owner_uid"
else
  echo
  echo "Next: the signing container must be able to READ the key. Find its uid:"
  echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T celery_worker id -u"
  echo "then give it both files (usually 1000):"
  echo "  sudo chown <uid> \"$chain\" \"$key\""
fi
