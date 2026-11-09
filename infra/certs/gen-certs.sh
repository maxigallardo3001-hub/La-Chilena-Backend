#!/bin/sh
set -e
cd /certs
if [ -f .done ]; then
  echo "Certificados ya generados"
  exit 0
fi
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=la-chilena-dev-ca" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout ca.key -out ca.crt
for svc in $SERVICES; do
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$svc" -keyout "$svc.key" -out "$svc.csr"
  cat > "$svc.ext" <<EOF
subjectAltName=DNS:$svc,DNS:localhost
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
EOF
  openssl x509 -req -in "$svc.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days 365 \
    -extfile "$svc.ext" -out "$svc.crt"
  rm -f "$svc.csr" "$svc.ext"
done
rm -f ca.key ca.srl
chmod 644 ./*.crt ./*.key
touch .done
echo "Certificados generados"
