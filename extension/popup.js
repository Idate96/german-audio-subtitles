(() => {
  "use strict";
  const checkbox = document.querySelector("#enabled");
  const status = document.querySelector("#status");
  const retry = document.querySelector("#retry");
  const pairing = document.querySelector("#pairing");
  const pairingSummary = document.querySelector("#pairing-summary");
  const form = document.querySelector("#pairing-form");
  const code = document.querySelector("#pairing-code");
  const connect = document.querySelector("#connect");
  const pairingError = document.querySelector("#pairing-error");
  function show(result) {
    checkbox.checked = result.enabled !== false;
    retry.disabled = !result.paired;
    pairingSummary.textContent = result.paired ? "Local service connected — change pairing" : "Connect the local service";
    pairing.open = !result.paired || Boolean(result.pairingError);
    pairingError.textContent = result.pairingError || "";
    if (result.pairingSaved) code.value = "";
    status.textContent = !result.paired ? "Connect once, then subtitles load automatically."
      : result.enabled === false ? "Subtitles are off."
      : result.status?.message || (result.status?.state === "ready" ? "German subtitles are ready."
        : "Play a Disney+ title with German audio to start.");
  }
  async function send(message) {
    try { const result = await browser.runtime.sendMessage(message); show(result); return result; }
    catch { status.textContent = "The subtitle extension needs to be reloaded."; return null; }
  }
  checkbox.addEventListener("change", () => send({type: "COCO_SET_ENABLED", enabled: checkbox.checked}));
  retry.addEventListener("click", async () => {
    retry.disabled = true;
    status.textContent = "Reconnecting…";
    await send({type: "COCO_RETRY"});
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    connect.disabled = true;
    pairingError.textContent = "";
    await send({type: "GERMAN_PAIR_SERVICE", code: code.value});
    connect.disabled = false;
  });
  void send({type: "COCO_POPUP_SETTINGS"});
})();
