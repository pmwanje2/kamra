# Self-hosting on DigitalOcean

## Marketplace 1-Click (preferred)

When the Maximus listing is live on the
[DigitalOcean Marketplace](https://marketplace.digitalocean.com/):

1. Create Droplet → search **Maximus**.
2. Enter **site domain**, **admin email**, **admin password**.
3. Open `/kamra` → `/kamra/setup`.

Vendor / Packer notes live in the repo at `deploy/digitalocean/`.

## Manual (same result)

### 1. Create the server

**Droplet** → Ubuntu 24.04 → **Basic / 4 GB / 2 vCPU** (~$24/mo; 8 GB builds faster) → region near
the hotel → SSH key.

### 2. Point your domain

**A record** for `pms.yourhotel.com` → droplet IP.

### 3. Install

```bash
ssh root@<server-ip>
git clone --single-branch --branch maximus/v2.8.0 https://github.com/pmwanje2/kamra.git maximus-pms
cd maximus-pms
sudo bash deploy/install.sh
```

Three prompts: site domain, admin email, admin password. No default password.

### 4. TLS

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d pms.yourhotel.com
```

Then `/kamra/setup`. See [Quickstart](/quickstart).
