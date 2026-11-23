// Plaid Link loader + opener (PLAID_PLAN.md phase 4). The one external
// script in the app — lazily injected on first Connect click and promise-
// cached, so it never loads for people (or days) that don't touch finance.
// Tokens: the backend mints the link_token; the public_token goes straight
// back to the backend. Nothing secret ever lives in frontend code.

import { postJSON } from "./api";

const LINK_SRC = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";

let scriptPromise = null;

function loadPlaid() {
  if (window.Plaid) return Promise.resolve(window.Plaid);
  if (!scriptPromise) {
    scriptPromise = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = LINK_SRC;
      s.onload = () => resolve(window.Plaid);
      s.onerror = () => {
        scriptPromise = null; // allow a retry on the next click
        reject(new Error("failed to load Plaid Link"));
      };
      document.head.appendChild(s);
    });
  }
  return scriptPromise;
}

// Opens Link. itemId set = update mode (reconnect after ITEM_LOGIN_REQUIRED).
// onSuccess(publicToken, metadata) fires when the user finishes; abandoning
// the flow (onExit) is a no-op.
export async function openLink(itemId, onSuccess) {
  const Plaid = await loadPlaid();
  const { link_token } = await postJSON("/api/plaid/link_token",
    itemId ? { item_id: itemId } : {});
  Plaid.create({ token: link_token, onSuccess, onExit: () => {} }).open();
}
