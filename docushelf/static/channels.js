// Page "Eingangskanäle" and the phone section of the settings: small conveniences only,
// everything works without JavaScript.

const SLUG = /[^a-z0-9._-]+/g;

// The scanner folder defaults to the person's name: show it as the placeholder.
function folderFromOwner(owner) {
  return owner.split("@", 1)[0].trim().toLowerCase().replace(SLUG, "-").replace(/^[-._]+|[-._]+$/g, "").slice(0, 64);
}

for (const form of document.querySelectorAll("form.channel-add")) {
  const owner = form.querySelector("[data-channel-owner]");
  const folder = form.querySelector("[data-channel-folder]");
  if (!owner || !folder) continue;
  const fallback = folder.placeholder;
  const update = () => {
    folder.placeholder = folderFromOwner(owner.value) || fallback;
  };
  owner.addEventListener("input", update);
  update();
}

// Switching the encryption suggests the usual port as long as no other one was typed.
for (const form of document.querySelectorAll("[data-channel-mailbox]")) {
  const tls = form.querySelector("[data-channel-tls]");
  const port = form.querySelector("[data-channel-port]");
  if (!tls || !port) continue;
  const usual = { ssl: "993", starttls: "143" };
  tls.addEventListener("change", () => {
    if (!port.value || Object.values(usual).includes(port.value)) {
      port.value = usual[tls.value] || "";
    }
    port.placeholder = usual[tls.value] || "";
  });
}

// A message from the last action scrolls into view (the form above it is open).
const message = document.querySelector("[data-channel-message]");
if (message) message.scrollIntoView({ block: "center" });
