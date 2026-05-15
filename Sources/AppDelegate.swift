import Cocoa

final class AppDelegate: NSObject, NSApplicationDelegate, SettingsDelegate {

    private var statusItem: NSStatusItem!
    private var windowController: SearchWindowController?
    private var settingsController: SettingsWindowController?
    let store = ClipyStore()
    private var contextMenu: NSMenu!

    func applicationDidFinishLaunching(_ notification: Notification) {
        UserDefaults.standard.register(defaults: [
            ClipyStore.dropCacheOnCloseKey: true
        ])
        buildStatusItem()
        buildMenu()
        store.startWatching()
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        guard let button = statusItem.button else { return }
        let img = NSImage(systemSymbolName: "magnifyingglass.circle",
                          accessibilityDescription: "ClipySearch")
        img?.isTemplate = true
        button.image = img
        button.target = self
        button.action = #selector(statusItemClicked(_:))
        button.sendAction(on: [.leftMouseUp, .rightMouseUp])
    }

    private func buildMenu() {
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Open ClipySearch",
                                action: #selector(showWindow),
                                keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Settings…",
                                action: #selector(showSettings),
                                keyEquivalent: ","))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Reload Now",
                                action: #selector(reloadNow),
                                keyEquivalent: "r"))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit ClipySearch",
                                action: #selector(quit),
                                keyEquivalent: "q"))
        for item in menu.items { item.target = self }
        contextMenu = menu
    }

    @objc private func statusItemClicked(_ sender: NSStatusBarButton) {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp {
            statusItem.menu = contextMenu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            toggleWindow()
        }
    }

    @objc private func showWindow() {
        store.ensureLoaded()
        if windowController == nil {
            windowController = SearchWindowController(store: store) { [weak self] in
                self?.windowDidHide()
            }
        }
        NSApp.activate(ignoringOtherApps: true)
        windowController?.showWindow(self)
        windowController?.window?.makeKeyAndOrderFront(nil)
        windowController?.focusSearch()
    }

    @objc private func toggleWindow() {
        if let wc = windowController, wc.window?.isVisible == true {
            wc.window?.orderOut(nil)
            windowDidHide()
        } else {
            showWindow()
        }
    }

    private func windowDidHide() {
        if store.dropCacheOnClose {
            store.dropCache()
        }
    }

    @objc private func showSettings() {
        if settingsController == nil {
            settingsController = SettingsWindowController()
            settingsController?.delegate = self
        }
        settingsController?.refreshPath()
        settingsController?.show()
    }

    @objc private func reloadNow() {
        store.reloadAsync()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    // MARK: SettingsDelegate

    func settingsDidChangeDataDir(_ url: URL) {
        store.setDataDir(url)
        store.ensureLoaded()
    }

    func settingsDidChangeDropCache(_ drop: Bool) {
        if drop, windowController?.window?.isVisible != true {
            store.dropCache()
        }
    }
}
