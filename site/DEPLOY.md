# Deploy Meeting Saathi (static site)

`index.html` is the whole site — one self-contained file (fonts from Google,
everything else inlined). It works from any URL/path. Pick one host below.

Owner console: URL `#/s/ms-owner-7Q2F9X`, access code `SAATHI-OWNER`
(change both in `index.html` — search for `OWNER_CODE` and `OWNER_PATH`).

---

## Option A — GitHub Pages (this repo is already wired for it)

`.github/workflows/pages.yml` deploys `site/` on every push to `master`.

1. Push the branch: `git push origin master`
2. Repo on GitHub → **Settings → Pages → Build and deployment → Source: GitHub Actions**
3. Wait for the "Deploy site to GitHub Pages" action to finish (Actions tab)
4. Live at **`https://aditya2004-source.github.io/meeting-saathi/`** — public, shareable, no login
5. Every later `git push` that touches `site/` redeploys automatically
6. Custom domain: Settings → Pages → Custom domain → `meetingsaathi.com`, then a
   CNAME record at your registrar pointing to `aditya2004-source.github.io`

---

## Option B — Netlify Drop (fastest, ~2 min, no repo needed)

1. Go to https://app.netlify.com/drop
2. Drag the **`deploy` folder** (the one with `index.html`) onto the page
3. You instantly get a live URL like `https://random-name-123.netlify.app` — **that is your live, shareable link.**
4. Sign in (GitHub/email) to keep it and rename the subdomain (Site settings → Change site name → e.g. `meeting-saathi` → `https://meeting-saathi.netlify.app`)
5. Custom domain later: Site settings → Domain management → add `meetingsaathi.com`, then at your registrar point the domain to Netlify (they show the exact records). HTTPS is automatic.

To update the site: drag the folder again onto the same site (Deploys tab → drag-and-drop area).

---

## Option C — Cloudflare Pages (free, global CDN, also easy)

1. https://dash.cloudflare.com → Workers & Pages → Create → Pages → **Upload assets**
2. Name it `meeting-saathi`, upload `index.html`
3. Live at `https://meeting-saathi.pages.dev`
4. Custom domain: Pages project → Custom domains → add `meetingsaathi.com` (if the domain's DNS is already on Cloudflare it's one click).

---

## Option D — Oracle Cloud "Always Free" VM (only worth it once there's a backend)

1. Create an **Always Free eligible** instance, Ubuntu 22.04
   (`VM.Standard.E2.1.Micro` is easiest to get; `VM.Standard.A1.Flex` is beefier but often "out of capacity").
2. **VCN Security List** → add Ingress rules for TCP **80** and **443** from `0.0.0.0/0`.
3. SSH in, then open the OS firewall too (Oracle images block 80/443 even when the Security List is open):
   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```
4. Install Caddy + drop the site:
   ```bash
   sudo apt update && sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
   sudo apt update && sudo apt install -y caddy

   sudo mkdir -p /var/www/meeting-saathi
   sudo cp index.html /var/www/meeting-saathi/
   sudo cp Caddyfile /etc/caddy/Caddyfile      # edit the domain first
   sudo systemctl reload caddy
   ```
5. At your domain registrar, set an **A record** → the VM's public IP. Caddy gets the HTTPS cert automatically within a minute.

---

## Sharing

- Netlify/Cloudflare URL: just send the link — it's public, no login needed.
- The claude.ai artifact link is private by default — open it and click **Share** to make it shareable. (That's why it didn't open on another account.)
