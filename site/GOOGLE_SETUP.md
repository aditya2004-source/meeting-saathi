# Turn on real "Sign in with Google"

The site already has the code — it just needs a **Google OAuth Client ID**.
Only you can create it (it belongs to your Google account). ~5 minutes.

## 1. Create the client ID

1. Go to https://console.cloud.google.com/
2. Top bar → project dropdown → **New Project** → name it `meeting-saathi` → Create
3. Left menu → **APIs & Services → OAuth consent screen**
   - User type: **External** → Create
   - App name: `Meeting Saathi`, user support email: your email, developer contact: your email
   - Save and continue through Scopes / Test users (defaults are fine) → Back to dashboard
   - Click **Publish app** (so anyone can sign in, not just test users)
4. Left menu → **APIs & Services → Credentials → + Create Credentials → OAuth client ID**
   - Application type: **Web application**
   - Name: `meeting-saathi web`
   - **Authorized JavaScript origins** → Add URI:
     - `https://aditya2004-source.github.io`
     - (add your custom domain too later, e.g. `https://meetingsaathi.com`)
   - Leave "Authorized redirect URIs" empty
   - **Create**
5. Copy the **Client ID** (looks like `1234567890-abc123def456.apps.googleusercontent.com`)

## 2. Put it in the site

In `site/index.html`, find:

```js
var GOOGLE_CLIENT_ID = "";
```

Paste your ID between the quotes:

```js
var GOOGLE_CLIENT_ID = "1234567890-abc123def456.apps.googleusercontent.com";
```

Then:

```bash
git add site/index.html
git commit -m "Enable Google sign-in"
git push origin master
```

GitHub Pages redeploys in ~20s. The real Google button now appears on the
sign-in screen; each visitor signs in with their own Google account and their
real name / email / avatar show up in the app.

## Notes
- The Client ID is **not a secret** — it's meant to be public in front-end code.
- If the button appears but sign-in fails, the JavaScript origin in step 4
  doesn't exactly match the site URL (check `https://`, no trailing slash, no path).
- Google's "unverified app" screen goes away after you submit the consent screen
  for verification (only needed if you request sensitive scopes — this uses only
  basic profile, so it's usually instant once published).
