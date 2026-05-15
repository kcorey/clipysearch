import Foundation

final class ClipyStore {
    static let maxItems = 3000
    static let dataDirDefaultsKey = "clipyDataDir"
    static let dropCacheOnCloseKey = "dropCacheOnClose"

    static var defaultDataDir: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Clipy")
    }

    static var configuredDataDir: URL {
        if let path = UserDefaults.standard.string(forKey: dataDirDefaultsKey),
           !path.isEmpty {
            return URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        }
        return defaultDataDir
    }

    private(set) var dataDir: URL = ClipyStore.configuredDataDir

    var dropCacheOnClose: Bool {
        UserDefaults.standard.bool(forKey: Self.dropCacheOnCloseKey)
    }

    private var dirSource: DispatchSourceFileSystemObject?
    private var dirFD: CInt = -1

    private let queue = DispatchQueue(label: "clipysearch.store", qos: .utility)
    private var _items: [ClipyItem] = []
    private var _lastScan: Date = .distantPast
    private var _loaded = false
    private var _dirty = true

    var onChange: (() -> Void)?

    var items: [ClipyItem] {
        queue.sync { _items }
    }

    var isLoaded: Bool {
        queue.sync { _loaded }
    }

    func ensureLoaded() {
        queue.async { [weak self] in
            guard let self else { return }
            if self._loaded && !self._dirty { return }
            self.reload()
        }
    }

    func dropCache() {
        queue.async { [weak self] in
            guard let self else { return }
            self._items.removeAll(keepingCapacity: false)
            self._loaded = false
            self._dirty = true
            DispatchQueue.main.async { self.onChange?() }
        }
    }

    func setDataDir(_ url: URL) {
        queue.async { [weak self] in
            guard let self else { return }
            self.stopWatchingInternal()
            self.dataDir = url
            self._items.removeAll(keepingCapacity: false)
            self._loaded = false
            self._dirty = true
            self.startWatchingInternal()
            DispatchQueue.main.async { self.onChange?() }
        }
    }

    func startWatching() {
        queue.async { [weak self] in self?.startWatchingInternal() }
    }

    private func startWatchingInternal() {
        let fd = open(dataDir.path, O_EVTONLY)
        guard fd >= 0 else { return }
        dirFD = fd
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .delete, .rename, .extend],
            queue: queue)
        src.setEventHandler { [weak self] in
            self?._dirty = true
            self?.coalescedReload()
        }
        src.setCancelHandler { [weak self] in
            if let f = self?.dirFD, f >= 0 { close(f) }
            self?.dirFD = -1
        }
        src.resume()
        dirSource = src
    }

    func stopWatching() {
        queue.async { [weak self] in self?.stopWatchingInternal() }
    }

    private func stopWatchingInternal() {
        dirSource?.cancel()
        dirSource = nil
    }

    private var pendingReload = false
    private func coalescedReload() {
        if pendingReload { return }
        pendingReload = true
        queue.asyncAfter(deadline: .now() + .milliseconds(250)) { [weak self] in
            guard let self else { return }
            self.pendingReload = false
            if self._loaded {
                self.reload()
            }
        }
    }

    func reloadAsync() {
        queue.async { [weak self] in self?.reload() }
    }

    private func reload() {
        let fm = FileManager.default
        guard let urls = try? fm.contentsOfDirectory(
            at: dataDir,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]) else {
            _items.removeAll()
            _loaded = true
            _dirty = false
            DispatchQueue.main.async { [weak self] in self?.onChange?() }
            return
        }
        let dataFiles = urls.filter { $0.pathExtension == "data" }
        let withDates = dataFiles.map { url -> (URL, Date) in
            let d = (try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            return (url, d)
        }
        let sorted = withDates.sorted { $0.1 > $1.1 }.prefix(Self.maxItems)
        var newItems: [ClipyItem] = []
        newItems.reserveCapacity(sorted.count)
        for (url, _) in sorted {
            autoreleasepool {
                if let item = ClipyParser.parse(url: url) {
                    newItems.append(item)
                }
            }
        }
        _items = newItems
        _loaded = true
        _dirty = false
        _lastScan = Date()
        DispatchQueue.main.async { [weak self] in self?.onChange?() }
    }

    func filter(_ query: String) -> [ClipyItem] {
        let snapshot = items
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if q.isEmpty { return snapshot }
        let needle = q.lowercased()
        return snapshot.filter { $0.text.lowercased().contains(needle) }
    }
}
