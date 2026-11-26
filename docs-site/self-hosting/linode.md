# Self-hosting on Linode (Akamai)

## Marketplace One-Click (preferred)

When Maximus is listed on the
[Akamai / Linode Marketplace](https://www.linode.com/marketplace/):

1. Deploy **Maximus**.
2. Enter **site domain**, **admin email**, **admin password**.
3. Open `/kamra` → `/kamra/setup`.

Use the button on [the hosting guide](https://kamrapms.com/get-started/)
for the upstream provider’s affiliate link (~$100 CPA when terms are met).

StackScript notes: `deploy/linode/` in the Maximus repo.

## Manual

### 1. Create the server

**Linode** → Ubuntu 24.04 → **Shared CPU / 4 GB** (~$24/mo; 8 GB builds faster) → region + SSH key.

### 2. DNS

**A record** `pms.yourhotel.com` → Linode IP.

### 3. Install

```bash
ssh root@<server-ip>
curl -fsSL https://raw.githubusercontent.com/Kamra-PMS/kamra-pms/main/deploy/install.sh | bash
```

Site domain, admin email, admin password - no default.

### 4. TLS

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d pms.yourhotel.com
```

Then `/kamra/setup`. See [Quickstart](/quickstart).
