# Quickstart (Docker)

The fastest self-host path: one Docker-capable Linux server, **three
questions**, then the installer gets the Maximus image (framework, payment integration and PMS) and creates your site. By default it **builds the image on your
VPS**; if your deployment publishes a Maximus image it can
[pull it instead](#faster-pull-the-prebuilt-image) in a few minutes.

## What you need

- A server or VPS: **2 vCPU · 4 GB RAM · 40 GB disk** minimum (Ubuntu
  22.04/24.04); **4 vCPU · 8 GB** recommended. Under 8 GB the installer adds
  a swap file so the image build doesn't run out of memory - it just builds
  slower.
- Docker Engine ≥ 24 with Compose v2 (the installer can install Docker for you)
- A domain (e.g. `pms.yourhotel.com`) pointed at the server
- **20-45 minutes** for the first image build (a few minutes when
  [pulling the prebuilt image](#faster-pull-the-prebuilt-image))

::: tip Where to get a server
Cheapest path for India / SEA: buy Hostinger with
**[our referral link](https://www.hostinger.com/in?REFERRALCODE=kamrapms)**
to support the upstream project.
Full walkthrough: [Hostinger](/self-hosting/hostinger).

Also: [DigitalOcean](/self-hosting/digitalocean), [Linode](/self-hosting/linode),
[AWS](/self-hosting/aws). Prefer managed? [Frappe Cloud](/self-hosting/frappe-cloud).
:::

## Install from the release checkout

```bash
git clone --single-branch --branch maximus/v2.8.0 https://github.com/pmwanje2/kamra.git maximus-pms
cd maximus-pms
sudo bash deploy/install.sh
```

You will be asked for:

| Field | Example |
| --- | --- |
| Site domain | `pms.yourhotel.com` |
| Admin email | `you@yourhotel.com` |
| Admin password | (min 10 characters - **there is no default**) |

The script clones `frappe_docker`, builds a local `kamra:local` image
(payment integration and PMS from GitHub), starts MariaDB + Valkey (Redis-compatible
cache/queue) + Maximus, creates the site, enables the scheduler, and points
`/` at `/kamra`.

## Faster: pull the prebuilt image

If your organization publishes its own Maximus image, use its registry
and release tag from the same checkout. Sign in to the registry first if
the image is private.

```bash
sudo env KAMRA_PULL=1 KAMRA_IMAGE="${IMAGE_REPOSITORY:?Set the Maximus image repository}" \
  KAMRA_TAG="${RELEASE_TAG:?Set the deployed release tag}" bash deploy/install.sh
```

For a fork, set `KAMRA_GIT_URL` and `KAMRA_BRANCH` when building, or supply
the full app manifest with `KAMRA_APPS_JSON`.

## Try it on your laptop

On a Docker-capable Linux host, clone the release as above. From the
checkout, build its image and run the bundled local evaluation stack:

```bash
sudo env KAMRA_IMAGE=maximus-pms bash deploy/install.sh build
cd deploy/compose
export KAMRA_IMAGE=maximus-pms KAMRA_TAG=local
export KAMRA_ADMIN_PASSWORD='pick-a-password'
export KAMRA_DB_PASSWORD="$(openssl rand -hex 16)"
docker compose -f quickstart.yaml up -d
docker compose -f quickstart.yaml logs -f create-site   # wait for "Maximus is ready"
```

Open [the local sign-in page](http://localhost:8080/kamra) and sign in as `Administrator` with
`KAMRA_ADMIN_PASSWORD`. Nothing is published beyond `127.0.0.1`.
`docker compose -f quickstart.yaml down -v` deletes it all. This is for
evaluation - use the installer for a real hotel.

## Sign in and set up

Open `http://<server-ip>:8080/kamra` (or `https://pms.yourhotel.com` after
DNS + TLS).

| Field | Use |
| --- | --- |
| Username | `Administrator` |
| Email | the admin email you entered |
| Password | the password you set |

Then open **`/kamra/setup`** and create your property. Product UI is
`/kamra`; Frappe Desk at `/app` is an admin escape hatch (accounting apps,
etc.) - not the hotel.

Forgot the password?

```bash
cd /opt/kamra/frappe_docker
docker compose --project-name kamra --env-file /opt/kamra/kamra.env \
  -f compose.yaml -f overrides/compose.mariadb.yaml \
  -f overrides/compose.redis.yaml -f overrides/compose.noproxy.yaml \
  exec backend bench --site pms.yourhotel.com set-admin-password '<new-password>'
```

## TLS

```bash
apt install -y certbot python3-certbot-nginx
# Put nginx in front of port 8080, then:
certbot --nginx -d pms.yourhotel.com
```

## Updating

```bash
sudo /opt/kamra/install.sh update
```

Every update first takes a full backup (`bench backup --with-files`, kept
in `sites/<site>/private/backups`) and **stops if the backup fails**. Then:

- **Build installs** rebuild the image from the branch recorded in
  `/opt/kamra/apps.json` (fetching the latest Maximus code). Pin a release
  with the `KAMRA_BRANCH` override. 10-30
  minutes; the site stays up until the containers are recreated.
- **Pull installs** pull the newest `:stable`, pin the release it resolves
  to, and recreate - a few minutes. Move to a specific release with
  the `KAMRA_TAG` override.

Either way it then runs `bench migrate` on every site.

### Going back (downgrade)

There is **no Downgrade button** in the product UI (System Health only
explains how to upgrade; it never changes the site for you).

To leave a newer tag after trying it, pin the release you want and run
the same update command:

```bash
# Pull install - example: leave a trial of a newer release
sudo env KAMRA_TAG="${PREVIOUS_TAG:?Set the previous image tag}" /opt/kamra/install.sh update

# Build install - pin the previous release branch/tag
sudo env KAMRA_BRANCH="${PREVIOUS_REF:?Set the previous release branch or tag}" /opt/kamra/install.sh update
```

Use the exact `vX.Y.Z` you were on before (see
[GitHub Releases](https://github.com/pmwanje2/kamra/releases)). Do
**not** use `:nightly` on a live hotel - keep production on `:stable` or a
pinned release.

**Caveats**

- Every update already backs up first (`sites/<site>/private/backups`).
  Keep that archive off-site before you experiment.
- `bench migrate` mostly moves **forward**. An older image may not like
  schema or data written by a newer one. If the site misbehaves after
  pinning an older tag, restore the pre-upgrade backup rather than
  forcing the old code onto a newer database.
- The **nightly demo** is a shared develop channel -
  hotels do not upgrade or downgrade it themselves.

Next: [production checklist](/self-hosting/#after-install-production-checklist) ·
[email setup](/self-hosting/email) · [AI assistant](/ai-and-mcp) ·
[ERPNext & HR (optional)](/self-hosting/erpnext-hr)
