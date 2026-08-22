# Installing the Chrome Extension

The extension isn't published on the Chrome Web Store (it's a personal tool
built for this computer), so it's installed as an "unpacked" extension —
this is completely normal for Chrome and only takes a minute.

## Steps

1. Open a new Chrome tab and go to: `chrome://extensions`
2. In the top-right corner, turn on **Developer mode** (it's a toggle
   switch).
3. Three new buttons appear. Click **Load unpacked**.
4. In the file picker, navigate to and select this exact folder:
   ```
   /home/enjay/projects/sarathi-meeting-bot/extension
   ```
   (select the folder itself, not a file inside it)
5. "Meeting Saathi" now appears in your extensions list, and its icon
   shows up in Chrome's toolbar (you may need to click the puzzle-piece icon
   in the toolbar and "pin" it to always see it).

That's it — installation is done. You will **not** need to repeat this
unless you move the project folder to a different location, or Chrome
removes the extension for some reason.

## Confirming it's working

1. Open https://meet.google.com and start or join any meeting (a solo test
   meeting with yourself works fine for testing).
2. Once you're actually in the call, click the Meeting Saathi extension's icon —
   this is what actually starts recording (Chrome requires this click; it's
   not just a status check). The popup should then say **"Recording: <your
   meeting title>"** and the toolbar icon should show a small red **REC**
   badge.
3. Leave the call. Within a few seconds, check http://localhost:8420 — a
   new row should appear and start moving through processing states
   (`transcribing`, `diarizing`, `generating_docs`, ...).

## If Chrome ever un-loads it

Unpacked extensions occasionally get disabled after a Chrome restart or
update. If the icon disappears or stops working: go back to
`chrome://extensions`, find "Meeting Saathi", and toggle it back on
(or repeat "Load unpacked" if it was removed entirely — your settings
aren't lost, since there's nothing stored in the extension itself).

## Updating the extension after a code change

If the extension's code is ever changed (a new feature, a bug fix), you
don't need to reinstall it — just go to `chrome://extensions` and click the
small reload icon (↻) on the Meeting Saathi card, or reload the whole
`chrome://extensions` page.
