// First-run screen: remember the Kiln server and open it. Runs only inside the app.
const form = document.getElementById("connect");
const input = document.getElementById("url");
const error = document.getElementById("error");
const invoke = window.__TAURI_INTERNALS__.invoke;

invoke("current_server").then((url) => { if (url) input.value = url; });

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  error.hidden = true;
  const button = form.querySelector("button");
  button.disabled = true;
  button.textContent = "Connecting…";
  try {
    await invoke("connect", { url: input.value.trim() });
  } catch (err) {
    error.textContent = String(err);
    error.hidden = false;
    button.disabled = false;
    button.textContent = "Connect";
  }
});
