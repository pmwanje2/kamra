# Hyperscaler marketplaces (AWS · Azure · GCP)

Plan big; ship after Hostinger / DigitalOcean / Linode 1-clicks.

## Listing

| Listing | Type | Who pays | Upstream revenue |
| --- | --- | --- | --- |
| **Maximus PMS (self-host)** | Free AMI / VM / container 1-click in *their* VPC | EC2 / VM only | Distribution (+ optional affiliate). Same first-boot: site, admin email, password. |

Paid work (implementation, support, concierge support) is sold
separately, not as a hosted SaaS listing.

**Do not** put a paid price on the Maximus software itself on the
marketplace listing (sell implementation and support separately).

## Order

1. **AWS Marketplace** - broadest buyers + ISV Accelerate co-sell.
2. **Azure Marketplace** - Microsoft-centric groups.
3. **Google Cloud Marketplace**.

Bake the image into the machine image at build time with
`deploy/install.sh build` (the GHCR package is private); each cloud needs a
thin Packer / Terraform wrapper + listing copy. Legal entity, tax forms, and a security
questionnaire are required before go-live (weeks to months).

## Near-term money

For independents, earn on **VPS affiliates** instead - see
[hosting options](/self-hosting/). Prefer Hostinger with the
[upstream referral link](https://www.hostinger.com/in?REFERRALCODE=kamrapms)
(**20% off** for the buyer; it supports the upstream project),
plus DigitalOcean recurring and Linode CPA. Hyperscaler listings are a
distribution channel, not a hosted product.
