import Cocoa

protocol SettingsDelegate: AnyObject {
    func settingsDidChangeDataDir(_ url: URL)
    func settingsDidChangeDropCache(_ drop: Bool)
}

final class SettingsWindowController: NSWindowController {

    weak var delegate: SettingsDelegate?

    private let pathField = NSTextField(labelWithString: "")
    private let dropCacheBox = NSButton(checkboxWithTitle: "", target: nil, action: nil)

    init() {
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 620, height: 220),
            styleMask: [.titled, .closable],
            backing: .buffered, defer: false)
        win.title = "ClipySearch Settings"
        win.center()
        win.isReleasedWhenClosed = false
        super.init(window: win)
        build()
    }

    required init?(coder: NSCoder) { fatalError() }

    private func build() {
        guard let content = window?.contentView else { return }

        let header = NSTextField(labelWithString: "Clipy data directory")
        header.translatesAutoresizingMaskIntoConstraints = false
        header.font = NSFont.boldSystemFont(ofSize: 13)
        content.addSubview(header)

        pathField.translatesAutoresizingMaskIntoConstraints = false
        pathField.lineBreakMode = .byTruncatingMiddle
        pathField.maximumNumberOfLines = 1
        pathField.font = NSFont.systemFont(ofSize: 12)
        pathField.textColor = .secondaryLabelColor
        pathField.stringValue = ClipyStore.configuredDataDir.path
        content.addSubview(pathField)

        let browse = NSButton(title: "Browse…", target: self, action: #selector(browseClicked))
        browse.translatesAutoresizingMaskIntoConstraints = false
        browse.bezelStyle = .rounded
        content.addSubview(browse)

        let reset = NSButton(title: "Reset to default", target: self, action: #selector(resetClicked))
        reset.translatesAutoresizingMaskIntoConstraints = false
        reset.bezelStyle = .rounded
        content.addSubview(reset)

        let memHeader = NSTextField(labelWithString: "Memory")
        memHeader.translatesAutoresizingMaskIntoConstraints = false
        memHeader.font = NSFont.boldSystemFont(ofSize: 13)
        content.addSubview(memHeader)

        dropCacheBox.title = "Drop clip cache when window closes (lazy reload on next open)"
        dropCacheBox.translatesAutoresizingMaskIntoConstraints = false
        dropCacheBox.target = self
        dropCacheBox.action = #selector(dropCacheToggled)
        dropCacheBox.state = UserDefaults.standard.bool(forKey: ClipyStore.dropCacheOnCloseKey) ? .on : .off
        content.addSubview(dropCacheBox)

        let note = NSTextField(wrappingLabelWithString:
            "Idle baseline is ~48 MB (AppKit floor). With cache drop enabled, RAM returns to that baseline each time the window closes. With it disabled, parsed clips stay cached for instant reopen but add a few MB per 1000 items.")
        note.translatesAutoresizingMaskIntoConstraints = false
        note.font = NSFont.systemFont(ofSize: 11)
        note.textColor = .tertiaryLabelColor
        content.addSubview(note)

        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: content.topAnchor, constant: 16),
            header.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),

            pathField.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 6),
            pathField.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            pathField.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),

            browse.topAnchor.constraint(equalTo: pathField.bottomAnchor, constant: 8),
            browse.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),

            reset.centerYAnchor.constraint(equalTo: browse.centerYAnchor),
            reset.leadingAnchor.constraint(equalTo: browse.trailingAnchor, constant: 8),

            memHeader.topAnchor.constraint(equalTo: browse.bottomAnchor, constant: 22),
            memHeader.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),

            dropCacheBox.topAnchor.constraint(equalTo: memHeader.bottomAnchor, constant: 6),
            dropCacheBox.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            dropCacheBox.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),

            note.topAnchor.constraint(equalTo: dropCacheBox.bottomAnchor, constant: 8),
            note.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            note.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
        ])
    }

    func refreshPath() {
        pathField.stringValue = ClipyStore.configuredDataDir.path
    }

    @objc private func browseClicked() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: pathField.stringValue)
        panel.prompt = "Choose"
        panel.message = "Select the directory containing Clipy *.data files"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        pathField.stringValue = url.path
        UserDefaults.standard.set(url.path, forKey: ClipyStore.dataDirDefaultsKey)
        delegate?.settingsDidChangeDataDir(url)
    }

    @objc private func resetClicked() {
        UserDefaults.standard.removeObject(forKey: ClipyStore.dataDirDefaultsKey)
        let def = ClipyStore.defaultDataDir
        pathField.stringValue = def.path
        delegate?.settingsDidChangeDataDir(def)
    }

    @objc private func dropCacheToggled() {
        let on = dropCacheBox.state == .on
        UserDefaults.standard.set(on, forKey: ClipyStore.dropCacheOnCloseKey)
        delegate?.settingsDidChangeDropCache(on)
    }

    func show() {
        NSApp.activate(ignoringOtherApps: true)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
    }
}
