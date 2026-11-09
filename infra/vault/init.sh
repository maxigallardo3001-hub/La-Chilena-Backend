#!/bin/sh
set -e
export VAULT_ADDR="${VAULT_ADDR:-https://vault:8201}"
export VAULT_CACERT="${VAULT_CACERT:-/certs/ca.crt}"
export VAULT_TOKEN="$VAULT_DEV_ROOT_TOKEN"

mongo_uri() {
  printf 'mongodb://%s_svc:%s@mongo:27017/%s?authSource=%s' "$1" "$2" "$1" "$1"
}

mk_token() {
  svc="$1"
  printf 'path "secret/data/%s" {\n  capabilities = ["read"]\n}\n' "$svc" | vault policy write "$svc-read" -
  vault token create -policy="$svc-read" -no-default-policy -ttl=720h -field=token > "/tokens/$svc/token"
  chmod 644 "/tokens/$svc/token"
}

vault kv put secret/auth \
  "accept_api-gateway=$GW_AUTH_SECRET" \
  "accept_clients=$CLIENTS_AUTH_SECRET" \
  "notify_credential=$AUTH_NOTIFY_SECRET" \
  "csrf_key=$AUTH_CSRF_KEY" \
  "mongo_uri=$(mongo_uri auth "$DB_PASS_AUTH")" \
  "bootstrap_admin_email=$BOOTSTRAP_ADMIN_EMAIL" \
  "bootstrap_admin_password=$BOOTSTRAP_ADMIN_PASSWORD" \
  "demo_password_administrador=$DEMO_PASSWORD_ADMIN" \
  "demo_password_cajero=$DEMO_PASSWORD_CAJERO" \
  "demo_password_despacho=$DEMO_PASSWORD_DESPACHO" \
  "demo_password_dueno=$DEMO_PASSWORD_DUENO" \
  "demo_password_cliente=$DEMO_PASSWORD_CLIENTE"
mk_token auth

vault kv put secret/gateway \
  "auth_credential=$GW_AUTH_SECRET" \
  "clients_credential=$GW_CLIENTS_SECRET" \
  "inventory_credential=$GW_INVENTORY_SECRET" \
  "orders_credential=$GW_ORDERS_SECRET" \
  "payments_credential=$GW_PAYMENTS_SECRET" \
  "graphql_credential=$GW_GRAPHQL_SECRET"
mk_token gateway

vault kv put secret/inventory \
  "accept_api-gateway=$GW_INVENTORY_SECRET" \
  "accept_orders=$ORDERS_INVENTORY_SECRET" \
  "accept_graphql-gateway=$GQL_INVENTORY_SECRET" \
  "mongo_uri=$(mongo_uri inventory "$DB_PASS_INVENTORY")"
mk_token inventory

vault kv put secret/clients \
  "accept_api-gateway=$GW_CLIENTS_SECRET" \
  "accept_orders=$ORDERS_CLIENTS_SECRET" \
  "auth_credential=$CLIENTS_AUTH_SECRET" \
  "mailcheck_key=$MAILCHECK_KEY" \
  "mongo_uri=$(mongo_uri clients "$DB_PASS_CLIENTS")"
mk_token clients

vault kv put secret/mailcheck-sim "accept_clients=$MAILCHECK_KEY"
mk_token mailcheck-sim

vault kv put secret/orders \
  "accept_api-gateway=$GW_ORDERS_SECRET" \
  "accept_graphql-gateway=$GQL_ORDERS_SECRET" \
  "accept_payments=$PAYMENTS_ORDERS_SECRET" \
  "inventory_credential=$ORDERS_INVENTORY_SECRET" \
  "clients_credential=$ORDERS_CLIENTS_SECRET" \
  "payments_credential=$ORDERS_PAYMENTS_SECRET" \
  "mongo_uri=$(mongo_uri orders "$DB_PASS_ORDERS")"
mk_token orders

vault kv put secret/payments \
  "accept_api-gateway=$GW_PAYMENTS_SECRET" \
  "accept_orders=$ORDERS_PAYMENTS_SECRET" \
  "accept_graphql-gateway=$GQL_PAYMENTS_SECRET" \
  "orders_credential=$PAYMENTS_ORDERS_SECRET" \
  "notify_credential=$PAYMENTS_NOTIFY_SECRET" \
  "servipag_key=$SERVIPAG_KEY" \
  "servipag_webhook_hmac=$SERVIPAG_WEBHOOK_HMAC" \
  "mongo_uri=$(mongo_uri payments "$DB_PASS_PAYMENTS")"
mk_token payments

vault kv put secret/servipag-sim \
  "accept_payments=$SERVIPAG_KEY" \
  "accept_tests=$SIM_CONTROL_KEY" \
  "webhook_hmac=$SERVIPAG_WEBHOOK_HMAC"
mk_token servipag-sim

vault kv put secret/notifications \
  "accept_auth=$AUTH_NOTIFY_SECRET" \
  "accept_payments=$PAYMENTS_NOTIFY_SECRET" \
  "smtp_user=$SMTP_USER" \
  "smtp_password=$SMTP_PASSWORD"
mk_token notifications

vault kv put secret/graphql \
  "accept_api-gateway=$GW_GRAPHQL_SECRET" \
  "orders_credential=$GQL_ORDERS_SECRET" \
  "inventory_credential=$GQL_INVENTORY_SECRET" \
  "payments_credential=$GQL_PAYMENTS_SECRET"
mk_token graphql

echo "Vault inicializado"
