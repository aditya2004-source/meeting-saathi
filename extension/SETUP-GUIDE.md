# Meeting Saathi — Setup Guide

Google Meet record karo → apne aap **Minutes of Meeting + Meeting Analysis** ban jaate hain,
tumhari apni Gemini key se. Koi server nahi, koi login nahi, koi subscription nahi.

**Chahiye:** Chrome browser + ek Google account. Bas.

---

## Part A — Gemini API key banao (ek baar, ~3 min)

1. Kholo: **https://aistudio.google.com/apikey**
2. Apne Google account se sign in karo
3. **"Create API key"** → **"Create API key in new project"** dabao
4. Key copy karo — `AIza...` se shuru hoti hai. Kahin safe rakh lo.

> **Free tier bahut kam hai** (din mein ~5-6 chhoti meeting). Regular use ke liye
> billing add karo: `https://console.cloud.google.com/billing` → card link karo →
> pay-as-you-go ban jaata hai (~₹1-3 per meeting, koi limit nahi).
> **Ya:** naya Google Cloud account = **$300 free credits, 90 din** — mahino tak free.

---

## Part B — Extension install karo (ek baar, ~2 min)

1. Jo **folder/ZIP** mila hai usko **permanent jagah** unzip karo (Desktop pe ek folder bana lo).
   ⚠️ Folder delete kiya toh extension band ho jaayega.
2. Chrome mein nayi tab → address bar mein type karo: **`chrome://extensions`**
3. Upar-right corner mein **"Developer mode"** toggle **ON** karo
4. **"Load unpacked"** button → unzip kiya hua folder select karo
5. "Meeting Saathi" card aayega — **Version: 2.0.0** likha hona chahiye
6. Toolbar mein icon pin karo: Chrome ke upar puzzle-piece 🧩 icon → Meeting Saathi ke aage pin dabao

---

## Part C — Ek baar ki setting (~1 min)

1. Toolbar mein **Meeting Saathi icon** dabao → likha aayega "Add your Gemini API key" →
   **"Open Settings"** dabao
   *(ya icon pe right-click → "Options")*
2. **Gemini API key** field mein apni key paste karo (spaces na aaye)
3. **"Test this key"** dabao → **`✓ key + model work`** aana chahiye
   - `✗ 401` aaye → key galat hai ya API enable nahi → nayi banao (Part A)
   - `⚠ quota exhausted` → key sahi hai, bas abhi limit full hai → thodi der baad
4. *(Optional)* **Model**: `gemini-3.6-flash` rehne do, ya free tier ke liye
   `gemini-3.5-flash-lite`
5. *(Optional)* **Your name** — transcript mein tumhari awaaz ko label karega
6. **"Save"** dabao — tab apne aap band ho jaayegi
7. Popup wapas kholo → agar **microphone** wala section dikhe →
   **"Enable Microphone Access"** dabao → nayi tab khulegi →
   **"Grant Microphone Access"** → Chrome ke prompt mein **Allow** → tab band kar do

---

## Part D — Har meeting ke liye

1. **Google Meet call join karo**
2. Meeting Saathi **icon** dabao → **"Start Recording"**
   - Chrome har meeting mein **ek click** maangta hai (browser ka rule, skip nahi hota)
   - Icon pe laal **"REC"** badge dikhega
3. Meeting chalti rahe — background mein record hota rahega
4. **Meeting se leave karo** → apne aap ruk jaayega + processing shuru
5. **~1-3 min ruko** (transcribe + documents ban rahe hain) —
   sab kuch band kar sakte ho, background mein chalega
6. Notification aayega **"Documents ready"** → uspe click karo
   *(ya icon → "View My Documents")*
7. Dashboard mein **meeting row pe click** karke expand karo →
   tabs: **Minutes of Meeting** / **Meeting Analysis** / **Transcript**
8. Har ek **Download .md** ya **Save as PDF** kar sakte ho

---

## Part E — Kuch galat ho toh

| Problem | Fix |
|---|---|
| **"Gemini free-tier limit hit"** | Jitne second bola utna ruko → dashboard pe **Resume** dabao. Ya billing add karo. Ya Settings → `gemini-3.5-flash-lite`. |
| **"Key was rejected"** | Settings → key dobara check karo → **"Test this key"** |
| Meeting **"processing" pe atki** | Dashboard → **Stop** → phir **Resume**. Ya `chrome://extensions` pe extension **Remove + Load unpacked** dobara |
| Speaker names galat (**Speaker 1, 2...**) | Dashboard → meeting expand → **"Rename speakers"** → asli naam daalo → **"Apply & regenerate"** |
| Documents kahin nahi dikh rahe | Dashboard pe **Refresh** button dabao. Meeting row **collapsed** ho sakti hai — uspe click karo |

---

## Privacy

- Extension **tumhare Chrome mein** chalta hai. Tumhara meeting audio **seedha tumhare
  apne Gemini account** pe jaata hai — bas wahi.
- **Koi Meeting Saathi server nahi hai.** Kisi aur ke paas tumhara audio, transcript,
  API key, ya documents nahi jaate.
- Key sirf tumhare browser mein save hoti hai. Jiske paas tumhara computer hai woh
  padh sakta hai, koi website ya doosra extension nahi.

---

## Update kaise milega

Abhi: naya folder/ZIP bhejenge, tum `chrome://extensions` pe **Remove + Load unpacked**
dobara karoge.
Aage: Chrome Web Store pe aayega → updates apne aap.
