#!/bin/sh
# Stores the registry credentials the distributor resolves by <host>/<namespace prefix>.
# In production these are written once by the platform team (or synced from Azure Key Vault /
# a customer onboarding pipeline); developers never see them.
set -eu
: "${HARBOR:=harbor:5000}" "${CUSTOMER:=customer:5000}" "${PUBLIC:=public:5000}"
BAO="${BAO:-bao}"

until $BAO status >/dev/null 2>&1; do echo "waiting for openbao..."; sleep 1; done

# Source: read-only robot account on the Harbor project.
$BAO kv put secret/registries/$HARBOR/acme.co            username=robot-distributor password=robot-pass
# Public registry: one publisher account for the whole host.
$BAO kv put secret/registries/$PUBLIC                    username=acme-publisher    password=publisher-pass
# Customer X: one account PER NAMESPACE on the same host.
$BAO kv put secret/registries/$CUSTOMER/vendor/acme      username=acme-prod         password=prod-pass
$BAO kv put secret/registries/$CUSTOMER/sandbox/acme     username=acme-sandbox      password=sandbox-pass

# Least privilege for the distributor itself: read-only on registries/*.
$BAO policy write distributor - <<'EOF'
path "secret/data/registries/*" { capabilities = ["read"] }
EOF
echo "credentials seeded"
