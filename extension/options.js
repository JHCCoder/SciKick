// Save / restore extension settings

document.addEventListener("DOMContentLoaded", async () => {
  const stored = await chrome.storage.local.get([
    "serverUrl",
    "driveFolderId",
    "googleCredsPath",
  ]);

  if (stored.serverUrl) {
    document.getElementById("server-url").value = stored.serverUrl;
  }
  if (stored.driveFolderId) {
    document.getElementById("drive-folder").value = stored.driveFolderId;
  }
  if (stored.googleCredsPath) {
    document.getElementById("google-creds").value = stored.googleCredsPath;
  }
});

document.getElementById("btn-save").addEventListener("click", async () => {
  const serverUrl = document.getElementById("server-url").value.trim();
  const driveFolderId = document.getElementById("drive-folder").value.trim();
  const googleCredsPath = document.getElementById("google-creds").value.trim();

  await chrome.storage.local.set({
    serverUrl: serverUrl || "http://localhost:8742",
    driveFolderId,
    googleCredsPath,
  });

  const status = document.getElementById("status");
  status.textContent = "✅ Settings saved.";
  status.className = "success";

  setTimeout(() => {
    status.textContent = "";
  }, 3000);
});
