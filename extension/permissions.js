// Runs in a real tab (not the popup) specifically so Chrome's native
// microphone permission prompt has a stable place to appear -- see
// extension/DESIGN.md: requesting getUserMedia from the transient popup is
// unreliable because the popup can close the instant the prompt takes
// focus, cutting off the request before the user can respond.

const grantBtn = document.getElementById("grant");
const statusEl = document.getElementById("status");

grantBtn.addEventListener("click", async () => {
  statusEl.textContent = "Requesting…";
  statusEl.style.color = "#555";
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((track) => track.stop());
    statusEl.textContent = "Microphone access granted — you can close this tab and rejoin your meeting.";
    statusEl.style.color = "#1a7a3c";
    grantBtn.style.display = "none";
  } catch (err) {
    statusEl.textContent =
      `Could not get microphone access: ${err.message || err}. If Chrome shows a blocked-mic icon in the address bar, click it, allow Microphone, then try the button again.`;
    statusEl.style.color = "#c0392b";
  }
});
