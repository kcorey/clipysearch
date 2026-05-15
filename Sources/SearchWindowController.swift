import Cocoa

final class KeyHandlingTableView: NSTableView {
    var onEnter: (() -> Void)?
    var onEscape: (() -> Void)?
    var onSearchFocus: (() -> Void)?

    override func keyDown(with event: NSEvent) {
        switch event.keyCode {
        case 36, 76:
            onEnter?()
        case 53:
            onEscape?()
        default:
            let chars = event.charactersIgnoringModifiers ?? ""
            if chars.count == 1, let scalar = chars.unicodeScalars.first,
               (CharacterSet.alphanumerics.contains(scalar) || scalar == " ") {
                onSearchFocus?()
                window?.firstResponder?.keyDown(with: event)
                return
            }
            super.keyDown(with: event)
        }
    }
}

final class SearchWindowController: NSWindowController, NSWindowDelegate {

    private let store: ClipyStore
    private var filtered: [ClipyItem] = []
    private let searchField = NSSearchField()
    private let tableView = KeyHandlingTableView()
    private let scrollView = NSScrollView()
    private let countLabel = NSTextField(labelWithString: "")
    private let previewText = NSTextView()
    private let previewScroll = NSScrollView()
    private let previewImage = NSImageView()

    private let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .short
        f.timeStyle = .short
        return f
    }()

    private let onHide: () -> Void

    init(store: ClipyStore, onHide: @escaping () -> Void) {
        self.store = store
        self.onHide = onHide
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 560),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered, defer: false)
        win.title = "ClipySearch"
        win.center()
        win.isReleasedWhenClosed = false
        win.titlebarAppearsTransparent = false
        super.init(window: win)
        win.delegate = self
        buildUI()
        store.onChange = { [weak self] in
            DispatchQueue.main.async { self?.refresh() }
        }
        refresh()
    }

    required init?(coder: NSCoder) { fatalError() }

    private func buildUI() {
        guard let content = window?.contentView else { return }

        searchField.translatesAutoresizingMaskIntoConstraints = false
        searchField.placeholderString = "Search clipboard history"
        searchField.target = self
        searchField.action = #selector(searchChanged)
        searchField.sendsSearchStringImmediately = true
        searchField.sendsWholeSearchString = false
        searchField.delegate = self
        content.addSubview(searchField)

        countLabel.translatesAutoresizingMaskIntoConstraints = false
        countLabel.font = NSFont.systemFont(ofSize: 11)
        countLabel.textColor = .secondaryLabelColor
        content.addSubview(countLabel)

        tableView.headerView = nil
        tableView.allowsMultipleSelection = false
        tableView.rowHeight = 28
        tableView.style = .inset
        tableView.usesAlternatingRowBackgroundColors = true
        let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("item"))
        col.width = 380
        tableView.addTableColumn(col)
        tableView.delegate = self
        tableView.dataSource = self
        tableView.target = self
        tableView.doubleAction = #selector(activateRow)
        tableView.onEnter = { [weak self] in self?.activateRow() }
        tableView.onEscape = { [weak self] in
            self?.window?.orderOut(nil)
            self?.onHide()
        }
        tableView.onSearchFocus = { [weak self] in
            self?.window?.makeFirstResponder(self?.searchField)
        }

        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.documentView = tableView
        scrollView.hasVerticalScroller = true
        scrollView.borderType = .bezelBorder
        scrollView.autohidesScrollers = true
        content.addSubview(scrollView)

        previewText.isEditable = false
        previewText.isRichText = false
        previewText.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        previewText.textContainerInset = NSSize(width: 6, height: 6)
        previewText.isAutomaticQuoteSubstitutionEnabled = false
        previewText.isAutomaticDashSubstitutionEnabled = false

        previewScroll.translatesAutoresizingMaskIntoConstraints = false
        previewScroll.documentView = previewText
        previewScroll.hasVerticalScroller = true
        previewScroll.borderType = .bezelBorder
        content.addSubview(previewScroll)

        previewImage.translatesAutoresizingMaskIntoConstraints = false
        previewImage.imageScaling = .scaleProportionallyUpOrDown
        previewImage.isHidden = true
        content.addSubview(previewImage)

        NSLayoutConstraint.activate([
            searchField.topAnchor.constraint(equalTo: content.topAnchor, constant: 10),
            searchField.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 10),
            searchField.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -10),

            countLabel.topAnchor.constraint(equalTo: searchField.bottomAnchor, constant: 4),
            countLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 12),
            countLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),

            scrollView.topAnchor.constraint(equalTo: countLabel.bottomAnchor, constant: 6),
            scrollView.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 10),
            scrollView.widthAnchor.constraint(equalTo: content.widthAnchor, multiplier: 0.45),
            scrollView.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -10),

            previewScroll.topAnchor.constraint(equalTo: scrollView.topAnchor),
            previewScroll.leadingAnchor.constraint(equalTo: scrollView.trailingAnchor, constant: 8),
            previewScroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -10),
            previewScroll.bottomAnchor.constraint(equalTo: scrollView.bottomAnchor),

            previewImage.topAnchor.constraint(equalTo: previewScroll.topAnchor),
            previewImage.leadingAnchor.constraint(equalTo: previewScroll.leadingAnchor),
            previewImage.trailingAnchor.constraint(equalTo: previewScroll.trailingAnchor),
            previewImage.bottomAnchor.constraint(equalTo: previewScroll.bottomAnchor),
        ])
    }

    func focusSearch() {
        window?.makeFirstResponder(searchField)
    }

    @objc private func searchChanged() {
        refresh()
    }

    private func refresh() {
        filtered = store.filter(searchField.stringValue)
        let total = store.items.count
        let suffix = "  ·  \(store.dataDir.path)"
        if !FileManager.default.fileExists(atPath: store.dataDir.path) {
            countLabel.stringValue = "Data dir not found:\(suffix)"
        } else if !store.isLoaded {
            countLabel.stringValue = "Scanning…\(suffix)"
        } else if searchField.stringValue.isEmpty {
            countLabel.stringValue = "\(total) items\(suffix)"
        } else {
            countLabel.stringValue = "\(filtered.count) of \(total) items\(suffix)"
        }
        tableView.reloadData()
        if !filtered.isEmpty {
            if tableView.selectedRow < 0 {
                tableView.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
            }
        }
        updatePreview()
    }

    @objc private func activateRow() {
        let row = tableView.selectedRow
        guard row >= 0, row < filtered.count else { return }
        let item = filtered[row]
        copyToClipboard(item)
        window?.orderOut(nil)
        onHide()
    }

    private func copyToClipboard(_ item: ClipyItem) {
        let pb = NSPasteboard.general
        pb.clearContents()
        switch item.type {
        case .image:
            if let img = ClipyParser.extractImage(url: item.dataPath) {
                pb.writeObjects([img])
            } else {
                pb.setString(item.text, forType: .string)
            }
        case .files:
            let urls = item.text
                .split(separator: "\n")
                .map { URL(fileURLWithPath: String($0)) as NSURL }
            if !urls.isEmpty {
                pb.writeObjects(urls)
            } else {
                pb.setString(item.text, forType: .string)
            }
        case .url, .text:
            pb.setString(item.text, forType: .string)
        }
    }

    private func updatePreview() {
        let row = tableView.selectedRow
        guard row >= 0, row < filtered.count else {
            previewText.string = ""
            previewImage.isHidden = true
            previewScroll.isHidden = false
            return
        }
        let item = filtered[row]
        if item.type == .image {
            if let img = ClipyParser.extractImage(url: item.dataPath) {
                previewImage.image = img
                previewImage.isHidden = false
                previewScroll.isHidden = true
                return
            }
        }
        previewImage.isHidden = true
        previewScroll.isHidden = false
        let header = "[\(item.type.rawValue.uppercased())]  \(dateFormatter.string(from: item.mtime))\n\n"
        previewText.string = header + item.text
    }

    func windowDidResignKey(_ notification: Notification) {
        window?.orderOut(nil)
        onHide()
    }
}

extension SearchWindowController: NSSearchFieldDelegate {
    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        switch commandSelector {
        case #selector(NSResponder.moveDown(_:)):
            if !filtered.isEmpty {
                let next = min(tableView.selectedRow + 1, filtered.count - 1)
                tableView.selectRowIndexes(IndexSet(integer: max(0, next)), byExtendingSelection: false)
                tableView.scrollRowToVisible(max(0, next))
            }
            return true
        case #selector(NSResponder.moveUp(_:)):
            if !filtered.isEmpty {
                let next = max(tableView.selectedRow - 1, 0)
                tableView.selectRowIndexes(IndexSet(integer: next), byExtendingSelection: false)
                tableView.scrollRowToVisible(next)
            }
            return true
        case #selector(NSResponder.insertNewline(_:)):
            activateRow()
            return true
        case #selector(NSResponder.cancelOperation(_:)):
            if searchField.stringValue.isEmpty {
                window?.orderOut(nil)
                onHide()
            } else {
                searchField.stringValue = ""
                refresh()
            }
            return true
        default:
            return false
        }
    }
}

extension SearchWindowController: NSTableViewDataSource, NSTableViewDelegate {
    func numberOfRows(in tableView: NSTableView) -> Int { filtered.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let id = NSUserInterfaceItemIdentifier("cell")
        let cell: NSTableCellView
        if let existing = tableView.makeView(withIdentifier: id, owner: nil) as? NSTableCellView {
            cell = existing
        } else {
            cell = NSTableCellView()
            cell.identifier = id
            let iv = NSImageView()
            iv.translatesAutoresizingMaskIntoConstraints = false
            iv.imageScaling = .scaleProportionallyDown
            cell.addSubview(iv)
            cell.imageView = iv
            let tf = NSTextField(labelWithString: "")
            tf.translatesAutoresizingMaskIntoConstraints = false
            tf.lineBreakMode = .byTruncatingTail
            tf.maximumNumberOfLines = 1
            tf.font = NSFont.systemFont(ofSize: 12)
            cell.addSubview(tf)
            cell.textField = tf
            NSLayoutConstraint.activate([
                iv.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 6),
                iv.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                iv.widthAnchor.constraint(equalToConstant: 16),
                iv.heightAnchor.constraint(equalToConstant: 16),
                tf.leadingAnchor.constraint(equalTo: iv.trailingAnchor, constant: 6),
                tf.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -6),
                tf.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            ])
        }
        let item = filtered[row]
        cell.imageView?.image = symbol(for: item.type)
        cell.imageView?.contentTintColor = .secondaryLabelColor
        let oneLine = item.text.replacingOccurrences(of: "\n", with: " ")
        cell.textField?.stringValue = oneLine
        return cell
    }

    private func symbol(for type: ClipyContentType) -> NSImage? {
        let name: String
        switch type {
        case .image: name = "photo"
        case .url:   name = "link"
        case .files: name = "folder"
        case .text:  name = "text.alignleft"
        }
        return NSImage(systemSymbolName: name, accessibilityDescription: type.rawValue)
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        updatePreview()
    }
}
