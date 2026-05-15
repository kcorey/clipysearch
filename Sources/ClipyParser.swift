import Cocoa
import Darwin

private typealias _GetTypeID = @convention(c) () -> CFTypeID
private typealias _GetValue  = @convention(c) (AnyObject) -> UInt32

private struct _UIDBridge {
    let typeID: CFTypeID
    let getValue: _GetValue
}

private let _uidBridge: _UIDBridge? = {
    let handle = dlopen(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
        RTLD_NOW)
    guard let tSym = dlsym(handle, "_CFKeyedArchiverUIDGetTypeID"),
          let vSym = dlsym(handle, "_CFKeyedArchiverUIDGetValue") else {
        return nil
    }
    let getTypeID = unsafeBitCast(tSym, to: _GetTypeID.self)
    let getValue = unsafeBitCast(vSym, to: _GetValue.self)
    return _UIDBridge(typeID: getTypeID(), getValue: getValue)
}()

private func uidIndex(_ obj: Any) -> Int? {
    guard let bridge = _uidBridge else { return nil }
    let any = obj as AnyObject
    if CFGetTypeID(any) == bridge.typeID {
        return Int(bridge.getValue(any))
    }
    return nil
}

enum ClipyContentType: String {
    case text, url, image, files
}

struct ClipyItem {
    let uuid: String
    let type: ClipyContentType
    let text: String
    let mtime: Date
    let size: Int
    let dataPath: URL
}

enum ClipyParser {

    static func parse(url: URL) -> ClipyItem? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        guard let plist = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String: Any] else {
            return nil
        }
        guard let objects = plist["$objects"] as? [Any],
              let top = plist["$top"] as? [String: Any],
              let rootRef = top["root"] else { return nil }

        guard let root = resolve(rootRef, objects: objects) as? [String: Any] else { return nil }

        let stringValue = (root["stringValue"] as? String) ?? ""
        let urlValue = (root["URL"] as? String) ?? ""

        var fileList: [String] = []
        if let filenames = root["filenames"] as? [String: Any],
           let arr = filenames["NS.objects"] as? [Any] {
            fileList = arr.compactMap { $0 as? String }.filter { !$0.isEmpty }
        } else if let arr = root["filenames"] as? [Any] {
            fileList = arr.compactMap { $0 as? String }.filter { !$0.isEmpty }
        }
        let hasFiles = !fileList.isEmpty

        var hasImage = false
        if let img = root["image"] as? [String: Any], img["NSReps"] != nil { hasImage = true }
        if let d = root["image"] as? Data, !d.isEmpty { hasImage = true }
        if !hasImage, let types = root["types"] as? [String: Any],
           let typeObjs = types["NS.objects"] as? [Any] {
            let joined = typeObjs.compactMap { $0 as? String }.joined(separator: ",")
            if joined.contains("PNG") || joined.contains("TIFF") { hasImage = true }
        }

        let hasURL = !urlValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty

        let attrs = (try? FileManager.default.attributesOfItem(atPath: url.path)) ?? [:]
        let mtime = (attrs[.modificationDate] as? Date) ?? Date(timeIntervalSince1970: 0)
        let size = (attrs[.size] as? Int) ?? 0

        let type: ClipyContentType
        let text: String
        if hasImage {
            type = .image
            text = "[Image]"
        } else if hasFiles {
            type = .files
            text = fileList.joined(separator: "\n")
        } else if hasURL {
            type = .url
            text = urlValue
        } else {
            type = .text
            let trimmed = stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty { return nil }
            text = stringValue
        }

        return ClipyItem(
            uuid: url.deletingPathExtension().lastPathComponent,
            type: type, text: text, mtime: mtime, size: size, dataPath: url)
    }

    static func extractImage(url: URL) -> NSImage? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        guard let plist = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String: Any] else {
            return nil
        }
        guard let objects = plist["$objects"] as? [Any],
              let top = plist["$top"] as? [String: Any],
              let rootRef = top["root"] else { return nil }
        guard let root = resolve(rootRef, objects: objects) as? [String: Any] else { return nil }

        if let raw = root["image"] as? Data, !raw.isEmpty {
            return NSImage(data: raw)
        }
        if let img = root["image"] as? [String: Any],
           let repsOuter = img["NSReps"] as? [String: Any],
           let outerArr = repsOuter["NS.objects"] as? [Any] {
            for outer in outerArr {
                guard let outerDict = outer as? [String: Any],
                      let inners = outerDict["NS.objects"] as? [Any] else { continue }
                for inner in inners {
                    if let innerDict = inner as? [String: Any],
                       let tiff = innerDict["NSTIFFRepresentation"] as? Data,
                       !tiff.isEmpty {
                        return NSImage(data: tiff)
                    }
                }
            }
        }
        return nil
    }

    private static func resolve(_ obj: Any, objects: [Any], depth: Int = 0) -> Any? {
        if depth > 30 { return obj }
        if let idx = uidIndex(obj) {
            guard idx >= 0 && idx < objects.count else { return nil }
            let inner = objects[idx]
            if let s = inner as? String, s == "$null" { return nil }
            return resolve(inner, objects: objects, depth: depth + 1)
        }
        if let dict = obj as? [String: Any] {
            var out: [String: Any] = [:]
            for (k, v) in dict {
                if k == "$class" { continue }
                if let r = resolve(v, objects: objects, depth: depth + 1) {
                    out[k] = r
                }
            }
            return out
        }
        if let arr = obj as? [Any] {
            return arr.compactMap { resolve($0, objects: objects, depth: depth + 1) }
        }
        return obj
    }
}
