/**
 * Service worker for scikick.
 * - Keeps the side panel pinned
 * - Relays active-tab info to the side panel (side panels can't read tab URLs directly)
 */

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch(() => {});

// ---------------------------------------------------------------------------
// Active-tab relay — the side panel cannot read url/title from chrome.tabs.query
// (Chrome strips those fields outside of the service worker), so all tab
// queries go through this simple message handler.
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "getCurrentTab") {
    // Prefer the window the side panel reports it belongs to, so a tab the
    // user pulled into a second window (or focused elsewhere) doesn't hijack
    // the result. Fall back to last-focused when no windowId was provided.
    const query = message.windowId
      ? { active: true, windowId: message.windowId }
      : { active: true, lastFocusedWindow: true };
    // Use the callback form — most compatible with MV3 worker lifecycle
    chrome.tabs.query(query, (tabs) => {
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
let sidePanelWindowId = null; // window the side panel reports it belongs to

chrome.runtime.onConnect.addListener((port) => {
  if (port.name === "sidepanel") {
    activePort = port;
    sidePanelWindowId = null;

    // The panel reports its window on connect (and re-affirms it on each
    // keep-alive ping) so tab pushes target THAT window, not the last-focused
    // one. Accept the id from any message that carries it.
    port.onMessage.addListener((msg) => {
      if (msg && msg.windowId != null) {
        sidePanelWindowId = msg.windowId;
      }
    });

    // Proactively push tab changes as they happen
    const pushCurrentTab = () => {
      if (!activePort) return;
      const query = sidePanelWindowId
        ? { active: true, windowId: sidePanelWindowId }
        : { active: true, lastFocusedWindow: true };
      chrome.tabs.query(query, (tabs) => {
        const tab = tabs[0];
        if (activePort && tab && tab.url) {
          activePort.postMessage({
            type: "activeTabChanged",
            tab: { title: tab.title, url: tab.url, id: tab.id },
          });
        }
      });
    };

    // Named handler so we can remove the exact same reference on disconnect.
    // (A previously anonymous arrow here was never removed -> it accumulated
    // on every reconnect and pushCurrentTab fired N times per tab change.)
    const onTabUpdated = (tabId, changeInfo) => {
      if (changeInfo.url || changeInfo.title) pushCurrentTab();
    };

    chrome.tabs.onActivated.addListener(pushCurrentTab);
    chrome.tabs.onUpdated.addListener(onTabUpdated);

    // Clean up listeners when the port disconnects
    port.onDisconnect.addListener(() => {
      activePort = null;
      sidePanelWindowId = null;
      chrome.tabs.onActivated.removeListener(pushCurrentTab);
      chrome.tabs.onUpdated.removeListener(onTabUpdated);
    });
  }
});
