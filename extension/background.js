/**
 * Service worker for PhiDkick.
 * - Keeps the side panel pinned
 * - Relays active-tab info to the side panel (side panels can't read tab URLs directly)
 */

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch(() => {});

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") {
    chrome.runtime.openOptionsPage();
  }
});

// ---------------------------------------------------------------------------
// Active-tab relay — the side panel cannot read url/title from chrome.tabs.query
// (Chrome strips those fields outside of the service worker), so all tab
// queries go through this simple message handler.
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "getCurrentTab") {
    // Use the callback form — most compatible with MV3 worker lifecycle
    chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
      const tab = tabs[0];
      if (tab && tab.url) {
        sendResponse({ ok: true, title: tab.title, url: tab.url, id: tab.id });
      } else {
        sendResponse({ ok: false, reason: "no-url" });
      }
    });
    return true; // keep the channel open for the async callback
  }
});

// ---------------------------------------------------------------------------
// Keep-alive port — the side panel opens this port to keep the worker alive
// and receive proactive tab-change events.
// ---------------------------------------------------------------------------

let activePort = null;

chrome.runtime.onConnect.addListener((port) => {
  if (port.name === "sidepanel") {
    activePort = port;

    port.onDisconnect.addListener(() => {
      activePort = null;
    });

    // Proactively push tab changes as they happen
    const pushCurrentTab = () => {
      if (!activePort) return;
      chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
        const tab = tabs[0];
        if (activePort && tab && tab.url) {
          activePort.postMessage({
            type: "activeTabChanged",
            tab: { title: tab.title, url: tab.url, id: tab.id },
          });
        }
      });
    };

    chrome.tabs.onActivated.addListener(pushCurrentTab);
    chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
      if (changeInfo.url || changeInfo.title) pushCurrentTab();
    });

    // Clean up listeners when the port disconnects
    port.onDisconnect.addListener(() => {
      chrome.tabs.onActivated.removeListener(pushCurrentTab);
      chrome.tabs.onUpdated.removeListener(pushCurrentTab);
    });
  }
});
